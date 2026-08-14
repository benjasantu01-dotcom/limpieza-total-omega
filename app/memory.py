"""
memory.py — diagnóstico honesto de memoria RAM.

POR QUÉ ESTE MÓDULO NO ES UN "LIMPIADOR DE RAM"
-----------------------------------------------
Las apps que prometen "liberar RAM" suelen llamar a EmptyWorkingSet sobre
todos los procesos. Eso hace subir el número de "memoria libre", que se ve
lindo, pero **empeora el rendimiento**: Windows tiene que volver a leer del
disco todo lo que acaba de expulsar. En un sistema moderno la RAM ocupada
como caché es lo que hace que las cosas abran rápido; RAM libre de más es
RAM desperdiciada.

Así que este módulo hace lo que sí sirve:
  - Medir el estado real de la memoria (total, disponible, presión).
  - Mostrar qué procesos son los que realmente consumen.
  - Dar un diagnóstico en lenguaje claro.
  - Ofrecer el "trim" del working set solo como acción manual, explicando
    cuándo tiene sentido (casi nunca) y qué costo tiene.

Diseño para que se pueda testear: las funciones que interpretan datos
reciben el texto crudo por parámetro (`parse_*`, `_read_meminfo_text`), así
la lógica se prueba en CI sobre Linux sin depender de Windows.
"""

from __future__ import annotations
import os
import re
import subprocess
import time
import math
import ctypes
from functools import lru_cache
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, TypeVar, TypeAlias
from safety import is_protected_path

if TYPE_CHECKING:
    from ctypes import wintypes

_T = TypeVar("_T", int, float)
BytesValue: TypeAlias = int
MegabytesValue: TypeAlias = float

BYTES_IN_MB: int = 1024 * 1024

__all__ = [
    "MemorySnapshot",
    "ProcessMemory",
    "format_bytes",
    "parse_linux_meminfo",
    "parse_windows_process_csv",
    "read_snapshot",
    "top_memory_processes",
    "pressure_level",
    "diagnose",
    "trim_working_set",
    "TRIM_WARNING",
]

TRIM_WARNING: str = (
    "Liberar el working set NO acelera la PC: fuerza a Windows a expulsar "
    "memoria que los programas están usando, y al volver a necesitarla la "
    "tiene que releer del disco. El número de 'RAM libre' sube, pero el "
    "rendimiento suele empeorar. Solo tiene sentido antes de medir algo "
    "puntual, no como mantenimiento."
)

BYTE_UNITS: Tuple[str, ...] = ("B", "KB", "MB", "GB", "TB")

PROCESS_QUERY_LIMITED_INFORMATION: int = 0x1000
PROCESS_SET_QUOTA: int = 0x0100
SAFE_ACCESS_MASK: int = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

STILL_ACTIVE_EXIT_CODE: int = 259
ERROR_ACCESS_DENIED: int = 5

SYSTEM_CRITICAL_PIDS: Tuple[int, ...] = (0, 4)

_PROCESS_CACHE: Dict[str, Tuple[float, List[ProcessMemory]]] = {"data": (0.0, [])}

class MEMORYSTATUSEX(ctypes.Structure):
    """Estructura de Windows para reportar el estado de memoria global."""
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]

@dataclass
class MemorySnapshot:
    """Representa el estado instantáneo de la memoria del sistema."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = 0

    @property
    def used(self) -> BytesValue:
        """Calcula los bytes en uso restando la memoria disponible de la total."""
        return max(0, self.total - self.available)

    @property
    def used_percent(self) -> float:
        """Porcentaje de memoria actualmente ocupada."""
        if self.total <= 0:
            return 0.0
        return round((self.used / self.total) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Porcentaje de memoria disponible para el sistema."""
        if self.total <= 0:
            return 0.0
        return round((self.available / self.total) * 100, 1)


@dataclass
class ProcessMemory:
    """Almacena el uso de memoria de un proceso individual."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Retorna el working set convertido a Megabytes."""
        return round(self.working_set / BYTES_IN_MB, 1)


def format_bytes(num: Optional[int | float]) -> str:
    """Convierte un valor de bytes en una cadena legible con su unidad correspondiente."""
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"


@lru_cache(maxsize=4)
def parse_linux_meminfo(text: str) -> MemorySnapshot:
    """
    Parsea la salida de /proc/meminfo extrayendo métricas clave.
    
    Args:
        text: Contenido crudo del archivo /proc/meminfo.
    """
    if not text:
        return MemorySnapshot(0, 0)
    
    metrics: Dict[str, int] = {}
    for line in text.splitlines():
        if match := re.match(r"^(\w+):\s+(\d+)", line):
            key, value = match.groups()
            metrics[key] = int(value) * 1024
    
    total = metrics.get("MemTotal", 0)
    # MemAvailable es preferido, seguido de MemFree como fallback
    available = metrics.get("MemAvailable", metrics.get("MemFree", 0))
    cached = metrics.get("Cached", 0)
    
    return MemorySnapshot(
        total=total,
        available=max(0, min(available, total)),
        cached=cached
    )


def _parse_csv_row(line: str) -> Optional[ProcessMemory]:
    """
    Deserializa una línea CSV (formato: Name, Id, WorkingSet) proveniente de PowerShell.
    
    Args:
        line: Una línea única de texto CSV sin procesar.
    Returns:
        Un objeto ProcessMemory si la línea es válida, None en caso contrario.
    """
    if not line or not line.strip():
        return None
    
    parts = [p.strip().strip("'\"") for p in line.split(",")]
    
    # Header check or malformed
    if len(parts) < 3 or parts[0].lower() == "name":
        return None
        
    try:
        # Se asume formato Name,Id,WorkingSet
        ws_str, pid_str = parts[-1], parts[-2]
        ws, pid = int(ws_str), int(pid_str)
        
        if ws < 0 or pid < 0:
            return None
            
        # El nombre podría contener comas, reconstruir si es necesario
        name = ",".join(parts[:-2])
        return ProcessMemory(name if name else "Unknown", pid, ws)
    except (ValueError, TypeError):
        return None


def parse_windows_process_csv(text: str, limit: int = 10) -> List[ProcessMemory]:
    """
    Convierte la salida cruda de PowerShell (CSV) a una lista ordenada de ProcessMemory.
    
    Args:
        text: Salida completa del comando PowerShell Get-Process convertido a CSV.
        limit: Número máximo de procesos a retornar.
    """
    if not isinstance(text, str) or not text:
        return []
    
    processes = []
    for line in text.splitlines():
        if p := _parse_csv_row(line):
            processes.append(p)
            
    processes.sort(key=lambda p: p.working_set, reverse=True)
    return processes[:limit]


def _read_windows_snapshot() -> MemorySnapshot:
    """Ejecuta API de Windows (GlobalMemoryStatusEx) para obtener estado de memoria global."""
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    kernel32 = ctypes.windll.kernel32
    if not hasattr(kernel32, "GlobalMemoryStatusEx") or not kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return MemorySnapshot(total=0, available=0)
    return MemorySnapshot(total=int(stat.ullTotalPhys), available=int(stat.ullAvailPhys))


def read_snapshot() -> MemorySnapshot:
    """Captura una instantánea de memoria adaptada al sistema operativo (Windows/Linux)."""
    if os.name == "nt":
        try:
            return _read_windows_snapshot()
        except (AttributeError, OSError, ctypes.ArgumentError):
            return MemorySnapshot(total=0, available=0)
    
    meminfo_path: str = "/proc/meminfo"
    if os.path.exists(meminfo_path):
        try:
            with open(meminfo_path, encoding="utf-8", errors="replace") as f:
                content = f.read()
                return parse_linux_meminfo(content) if content else MemorySnapshot(0, 0)
        except (OSError, PermissionError):
            return MemorySnapshot(total=0, available=0)
    return MemorySnapshot(total=0, available=0)


def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna los procesos con mayor consumo usando caché de 5 segundos."""
    if os.name != "nt":
        return []
    
    now: float = time.time()
    cache_ref = _PROCESS_CACHE["data"]
    
    if now - cache_ref[0] < 5.0 and cache_ref[1]:
        return cache_ref[1][:limit]
    
    command: str = "Get-Process | Select-Object Name,Id,WorkingSet | ConvertTo-Csv -NoTypeInformation"
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0 and proc.stdout:
            new_processes = parse_windows_process_csv(proc.stdout, limit=limit)
            _PROCESS_CACHE["data"] = (now, new_processes)
            return new_processes
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        pass
    return []


def pressure_level(snapshot: MemorySnapshot) -> str:
    """Mapea el porcentaje de memoria disponible a una etiqueta de severidad (ok/info/warning/danger)."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return "info"
    
    available: float = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"


def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un reporte descriptivo legible basado en las métricas de memoria."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return ["No se pudo leer el estado de la memoria en este sistema."]
    
    report: List[str] = [
        f"Memoria total: {format_bytes(snapshot.total)}",
        f"En uso: {format_bytes(snapshot.used)} ({snapshot.used_percent}%)",
        f"Disponible: {format_bytes(snapshot.available)} ({snapshot.available_percent}%)",
    ]
    
    diagnostics: Dict[str, str] = {
        "ok": "Estado: holgado. La memoria ocupada por caché mejora la velocidad.",
        "info": "Estado: normal. Windows gestiona la memoria de forma eficiente.",
        "warning": "Estado: ajustado. Conviene cerrar aplicaciones innecesarias.",
        "danger": "Estado: crítico. El sistema recurre al archivo de paginación."
    }
    
    report.append(diagnostics.get(pressure_level(snapshot), ""))
    
    if processes:
        for proc in processes[:3]:
            report.append(f"  Mayor consumo: {proc.name} (PID {proc.pid}) — {proc.working_set_mb} MB")
            
    return report


def _is_system_process(pid: int) -> bool:
    """
    Verifica si un PID pertenece a servicios críticos protegidos del SO.
    Considera los PIDs del núcleo (0, 4) y procesos de sistema base.
    """
    return pid <= 0 or pid in SYSTEM_CRITICAL_PIDS or pid <= 100


def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Intenta liberar el 'working set' de un proceso específico mediante llamadas a la API de Windows.
    
    Args:
        pid: Identificador de proceso (PID) a intervenir.
    Returns:
        Tupla (éxito: bool, mensaje: str).
    """
    if os.name != "nt":
        return False, "Solo disponible en Windows."
    
    try:
        target_pid = int(pid)
    except (ValueError, TypeError):
        return False, "El PID debe ser un número entero válido."
    
    if _is_system_process(target_pid) or target_pid == os.getpid():
        return False, "Operación denegada: PID fuera de rango o protegido."
    
    kernel32 = ctypes.windll.kernel32
    psapi = getattr(ctypes.windll, "psapi", None)
    
    if psapi is None or not hasattr(psapi, "EmptyWorkingSet"):
        return False, "Error de sistema: PSAPI no disponible o incompatible."

    # Pre-check: intentar abrir proceso con privilegios mínimos necesarios
    proc_handle = kernel32.OpenProcess(SAFE_ACCESS_MASK, False, target_pid)
    if not proc_handle:
        return False, "Acceso denegado: no se pudo obtener control sobre el proceso."
        
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(exit_code)) or exit_code.value != STILL_ACTIVE_EXIT_CODE:
            return False, "El proceso seleccionado ya no está activo."
            
        buf = ctypes.create_unicode_buffer(4096)
        size = ctypes.c_ulong(4096)
        
        # Validación de seguridad: obtener ruta y chequear contra lista protegida
        if kernel32.QueryFullProcessImageNameW(proc_handle, 0, buf, ctypes.byref(size)) > 0:
            exe_path = os.path.normpath(buf.value)
            if is_protected_path(exe_path):
                return False, "Operación denegada: ruta de ejecutable protegida."
        else:
            return False, "Error interno: no se pudo verificar la identidad del proceso."
            
        if not psapi.EmptyWorkingSet(proc_handle):
            error_code = kernel32.GetLastError()
            if error_code == ERROR_ACCESS_DENIED:
                return False, "Acceso denegado: privilegios insuficientes."
            return False, f"Error al intentar liberar memoria (código {error_code})."
            
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (ctypes.ArgumentError, MemoryError, OSError) as e:
        return False, f"Ocurrió un error técnico al gestionar el proceso: {str(e)}"
    finally:
        if proc_handle:
            kernel32.CloseHandle(proc_handle)
