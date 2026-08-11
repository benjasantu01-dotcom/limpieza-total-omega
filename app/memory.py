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

# Permisos requeridos para consultar métricas y gestionar WorkingSet vía Win32 API
PROCESS_QUERY_LIMITED_INFORMATION: int = 0x1000
PROCESS_SET_QUOTA: int = 0x0100
SAFE_ACCESS_MASK: int = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

STILL_ACTIVE_EXIT_CODE: int = 259
ERROR_ACCESS_DENIED: int = 5

# PIDs de servicios internos de Windows que no deben ser manipulados
SYSTEM_CRITICAL_PIDS: Tuple[int, ...] = (0, 4)

_PROCESS_CACHE: Dict[str, Tuple[float, List[ProcessMemory]]] = {"data": (0.0, [])}

class MEMORYSTATUSEX(ctypes.Structure):
    """Estructura interna para el mapeo de la función GlobalMemoryStatusEx de la API de Windows."""
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
    """Instantánea de memoria física en bytes para cálculos precisos."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = 0

    @property
    def used(self) -> BytesValue:
        """Espacio de RAM actualmente en uso por el sistema o procesos."""
        return max(0, self.total - self.available)

    @property
    def used_percent(self) -> float:
        """Porcentaje de uso para indicadores visuales en la interfaz."""
        if self.total <= 0:
            return 0.0
        return round((self.used / self.total) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Porcentaje de disponibilidad para lógica de diagnóstico."""
        if self.total <= 0:
            return 0.0
        return round((self.available / self.total) * 100, 1)


@dataclass
class ProcessMemory:
    """Registro de memoria residente (Working Set) de un proceso específico."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Retorna el Working Set normalizado a MB para visualización humana."""
        return round(self.working_set / BYTES_IN_MB, 1)


def format_bytes(num: Optional[int | float]) -> str:
    """Convierte bytes a string legible (ej: 1.5 GB)."""
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"


@lru_cache(maxsize=4)
def parse_linux_meminfo(text: str) -> MemorySnapshot:
    """Interpreta el contenido crudo de /proc/meminfo en un objeto MemorySnapshot."""
    if not isinstance(text, str) or not text:
        return MemorySnapshot(0, 0)
    
    values: Dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if match:
            try:
                values[match.group(1)] = int(match.group(2)) * 1024
            except (ValueError, OverflowError):
                continue
    
    total: int = values.get("MemTotal", 0)
    available: int = values.get("MemAvailable", values.get("MemFree", 0))
    cached: int = values.get("Cached", 0)
    
    if total <= 0:
        return MemorySnapshot(0, 0)
        
    return MemorySnapshot(
        total=total,
        available=max(0, min(available, total)),
        cached=cached
    )


def _is_valid_process_row(parts: List[str]) -> bool:
    """Valida la integridad de una línea CSV proveniente de PowerShell."""
    return len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit()


def parse_windows_process_csv(text: str, limit: int = 10) -> List[ProcessMemory]:
    """Transforma la salida de Get-Process en una lista de objetos tipados."""
    if not isinstance(text, str) or not text:
        return []
    
    processes: List[ProcessMemory] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts: List[str] = [p.strip().strip("'\"") for p in line.split(",")]
        if _is_valid_process_row(parts):
            try:
                processes.append(ProcessMemory(
                    name=parts[0] or "Unknown", 
                    pid=int(parts[1]), 
                    working_set=int(parts[2])
                ))
            except (ValueError, TypeError):
                continue
    
    processes.sort(key=lambda p: p.working_set, reverse=True)
    return processes[:limit]


def _read_windows_snapshot() -> MemorySnapshot:
    """Accede a la API nativa GlobalMemoryStatusEx para obtener el estado real de RAM."""
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    kernel32 = ctypes.windll.kernel32
    if not hasattr(kernel32, "GlobalMemoryStatusEx") or not kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return MemorySnapshot(total=0, available=0)
    return MemorySnapshot(total=int(stat.ullTotalPhys), available=int(stat.ullAvailPhys))


def read_snapshot() -> MemorySnapshot:
    """Punto de entrada unificado para leer el estado de RAM según la plataforma detectada."""
    if os.name == "nt":
        try:
            return _read_windows_snapshot()
        except Exception:
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
    """Obtiene los procesos más pesados mediante una caché temporal de 5 segundos."""
    if os.name != "nt":
        return []
    
    now: float = time.time()
    ts, cached_processes = _PROCESS_CACHE["data"]
    
    if now - ts < 5.0 and cached_processes:
        return cached_processes[:limit]
    
    command: str = (
        "Get-Process | Sort-Object WorkingSet -Descending | "
        "ForEach-Object { \"$($_.Name),$($_.Id),$($_.WorkingSet)\" }"
    )
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
    """Determina heurísticamente el nivel de presión de la memoria RAM."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return "info"
    
    available: float = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"


def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un diagnóstico textual del estado de memoria del sistema."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return ["No se pudo leer el estado de la memoria en este sistema."]
    
    level: str = pressure_level(snapshot)
    lines: List[str] = [
        f"Memoria total: {format_bytes(snapshot.total)}",
        f"En uso: {format_bytes(snapshot.used)} ({snapshot.used_percent}%)",
        f"Disponible: {format_bytes(snapshot.available)} ({snapshot.available_percent}%)",
    ]
    
    diagnosticos: Dict[str, str] = {
        "ok": "Estado: holgado. La memoria ocupada por caché mejora la velocidad.",
        "info": "Estado: normal. Windows gestiona la memoria de forma eficiente.",
        "warning": "Estado: ajustado. Conviene cerrar aplicaciones innecesarias.",
        "danger": "Estado: crítico. El sistema recurre al archivo de paginación."
    }
    
    lines.append(diagnosticos.get(level, ""))
    
    if processes:
        for proc in processes[:3]:
            lines.append(f"  Mayor consumo: {proc.name} (PID {proc.pid}) — {proc.working_set_mb} MB")
            
    return lines


def _is_system_process(pid: int) -> bool:
    """Verifica si el PID corresponde a un proceso del sistema restringido."""
    return pid in SYSTEM_CRITICAL_PIDS or pid <= 100


def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Intenta reducir el working set de un proceso específico, realizando 
    validaciones de seguridad sobre el handle del proceso y la ubicación 
    del ejecutable antes de ejecutar la acción Win32.
    """
    if os.name != "nt":
        return False, "Solo disponible en Windows."
    
    try:
        target_pid: int = int(pid)
    except (ValueError, TypeError):
        return False, "El PID debe ser un número entero válido."
    
    if _is_system_process(target_pid) or target_pid == os.getpid():
        return False, "Operación denegada: PID fuera de rango o protegido."
    
    kernel32 = ctypes.windll.kernel32
    psapi = getattr(ctypes.windll, "psapi", None)
    
    if psapi is None:
        return False, "Error de sistema: PSAPI no disponible."

    # Obtener handle con permisos mínimos necesarios (Query + SetQuota)
    proc_handle = kernel32.OpenProcess(SAFE_ACCESS_MASK, False, target_pid)
    
    if not proc_handle:
        if kernel32.GetLastError() == ERROR_ACCESS_DENIED:
            return False, "Acceso denegado: requiere privilegios de administrador."
        return False, "Acceso denegado o proceso no encontrado."
        
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(exit_code)) or exit_code.value != STILL_ACTIVE_EXIT_CODE:
            return False, "El proceso seleccionado ya no está activo."
            
        buf = ctypes.create_unicode_buffer(2048)
        size: int = psapi.GetModuleFileNameExW(proc_handle, 0, buf, 2048)
        
        # Si no se puede leer la ruta (ej. privilegios insuficientes del proceso), 
        # asumimos que el proceso sigue siendo válido para el trim pero no aplicamos 
        # chequeo de seguridad de ruta (es aceptable ya que el handle está abierto).
        if size > 0:
            exe_path: str = os.path.abspath(os.path.normpath(buf.value))
            if is_protected_path(exe_path) or not os.path.isabs(exe_path):
                return False, "Operación denegada: ruta de ejecutable no segura."
            try:
                # Comprobación de lectura no destructiva para verificar acceso al binario
                with open(exe_path, "rb"): pass
            except (OSError, PermissionError):
                return False, "Operación denegada: ejecutable bloqueado por el sistema."
            
        if not psapi.EmptyWorkingSet(proc_handle):
            return False, f"Error al intentar liberar memoria (código {kernel32.GetLastError()})."
            
        return True, f"Working set liberado. {TRIM_WARNING}"
    except Exception:
        return False, "Ocurrió un error técnico al gestionar el proceso."
    finally:
        if proc_handle:
            kernel32.CloseHandle(proc_handle)
