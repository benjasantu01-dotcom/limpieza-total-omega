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

# Constantes para Win32 API: permisos mínimos necesarios para diagnóstico y gestión
PROCESS_QUERY_LIMITED_INFORMATION: int = 0x1000
PROCESS_SET_QUOTA: int = 0x0100
SAFE_ACCESS_MASK: int = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

STILL_ACTIVE_EXIT_CODE: int = 259
ERROR_ACCESS_DENIED: int = 5

# PIDs reservados: 0 (System Idle), 4 (System)
SYSTEM_CRITICAL_PIDS: Tuple[int, ...] = (0, 4)

class MEMORYSTATUSEX(ctypes.Structure):
    """Estructura binaria para la API Win32 GlobalMemoryStatusEx."""
    _fields_: List[Tuple[str, ctypes._SimpleCData]] = [
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

def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """Prepara la estructura con el tamaño necesario para el registro de memoria."""
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return stat

@dataclass
class MemorySnapshot:
    """Representa el estado global de la memoria física y virtual del host."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = 0

    @property
    def used(self) -> BytesValue:
        """Calcula el uso real restando la memoria disponible al total."""
        return max(0, self.total - self.available)

    @property
    def used_percent(self) -> float:
        """Retorna el porcentaje de ocupación física actual (0-100)."""
        if self.total <= 0:
            return 0.0
        return round((self.used / self.total) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Retorna el porcentaje de disponibilidad de memoria (0-100)."""
        if self.total <= 0:
            return 0.0
        return round((self.available / self.total) * 100, 1)


@dataclass
class ProcessMemory:
    """Metadatos de consumo de memoria de un proceso específico."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Convierte la memoria residente (Working Set) de bytes a MB."""
        return round(self.working_set / BYTES_IN_MB, 1)


def format_bytes(num: Optional[int | float]) -> str:
    """Convierte una magnitud de bytes a formato human-readable (ej: 1.5 MB)."""
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"


@lru_cache(maxsize=4)
def parse_linux_meminfo(text: str) -> MemorySnapshot:
    """Lógica pura para procesar el formato de /proc/meminfo en sistemas Unix."""
    if not text:
        return MemorySnapshot(0, 0)
    
    metrics: Dict[str, int] = {}
    for line in text.splitlines():
        if match := re.match(r"^(\w+):\s+(\d+)", line):
            key, value = match.groups()
            metrics[key] = int(value) * 1024
    
    total = metrics.get("MemTotal", 0)
    available = metrics.get("MemAvailable", metrics.get("MemFree", 0))
    cached = metrics.get("Cached", 0)
    
    return MemorySnapshot(
        total=total,
        available=max(0, min(available, total)),
        cached=cached
    )


def _parse_csv_row(line: str) -> Optional[ProcessMemory]:
    """Helper para extraer datos de proceso desde una fila de CSV cruda."""
    if not isinstance(line, str):
        return None
    line = line.strip()
    if not line:
        return None
    
    parts = line.split(",")
    if len(parts) < 3 or parts[0].strip().lower() == "name":
        return None
        
    try:
        ws_raw = parts[-1].strip().strip("'\"")
        pid_raw = parts[-2].strip().strip("'\"")
        name = ",".join(parts[:-2]).strip().strip("'\"")
        
        return ProcessMemory(name=name, pid=int(pid_raw), working_set=int(ws_raw))
    except (ValueError, TypeError, IndexError):
        return None


def parse_windows_process_csv(text: str, limit: int = 10) -> List[ProcessMemory]:
    """Transforma la salida de PowerShell en objetos ProcessMemory ordenados."""
    if not isinstance(text, str) or not text:
        return []
    
    processes = [p for line in text.splitlines() if (p := _parse_csv_row(line))]
    processes.sort(key=lambda p: p.working_set, reverse=True)
    return processes[:limit]


def _read_windows_snapshot() -> MemorySnapshot:
    """Interactúa con la API de kernel32 para obtener métricas globales en Windows."""
    stat = _create_mem_status_ex()
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    if kernel32 is None or not hasattr(kernel32, "GlobalMemoryStatusEx") or not kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return MemorySnapshot(total=0, available=0)
    
    total = int(stat.ullTotalPhys)
    avail = int(stat.ullAvailPhys)
    return MemorySnapshot(total=total, available=min(avail, total) if total > 0 else 0)


def read_snapshot() -> MemorySnapshot:
    """Función polimórfica que abstrae el origen de la info según el SO."""
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


@lru_cache(maxsize=1)
def _fetch_raw_process_data() -> str:
    """Ejecuta comando de PowerShell y devuelve el CSV crudo."""
    cmd = "Get-Process | Sort-Object WorkingSet -Descending | Select-Object -First 20 Name,Id,WorkingSet | ForEach-Object { \"$($_.Name),$($_.Id),$($_.WorkingSet)\" }"
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=5)
        return proc.stdout if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return ""


def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Consulta los procesos más pesados vía PowerShell, con caché de resultados."""
    if os.name != "nt":
        return []
    
    raw_data = _fetch_raw_process_data()
    return parse_windows_process_csv(raw_data, limit=limit)


def pressure_level(snapshot: MemorySnapshot) -> str:
    """Categoriza el estado de presión de memoria en etiquetas de severidad."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return "info"
    
    available: float = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"


def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Crea una narrativa legible sobre la salud de la memoria actual."""
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
    """Determina si el PID pertenece al núcleo o servicios protegidos."""
    return pid <= 0 or pid in SYSTEM_CRITICAL_PIDS or pid < 100


def _get_process_path(handle: int) -> Optional[str]:
    """Usa QueryFullProcessImageNameW para obtener la ruta del ejecutable mediante su handle."""
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    if kernel32 is None:
        return None
    buf = ctypes.create_unicode_buffer(4096)
    size = ctypes.c_ulong(4096)
    try:
        if hasattr(kernel32, "QueryFullProcessImageNameW") and kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) > 0:
            return str(buf.value)
    except (OSError, ctypes.ArgumentError):
        return None
    return None


def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """Intenta liberar RAM residente del proceso, sujeto a validaciones de seguridad."""
    if os.name != "nt":
        return False, "Solo disponible en Windows."
    
    try:
        target_pid = int(pid)
    except (ValueError, TypeError):
        return False, "El PID debe ser un número entero válido."
    
    if _is_system_process(target_pid) or target_pid == os.getpid():
        return False, "Operación denegada: PID fuera de rango o protegido."
    
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    psapi = getattr(ctypes.windll, "psapi", None)
    if not kernel32 or not psapi or not hasattr(psapi, "EmptyWorkingSet"):
        return False, "Error de sistema: APIs de memoria no disponibles."

    proc_handle = kernel32.OpenProcess(SAFE_ACCESS_MASK, False, target_pid)
    if not proc_handle:
        return False, "Acceso denegado: no se pudo obtener control sobre el proceso."
        
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(exit_code)) or exit_code.value != STILL_ACTIVE_EXIT_CODE:
            return False, "El proceso seleccionado ya no está activo."
            
        path = _get_process_path(proc_handle)
        if not path or is_protected_path(os.path.normpath(path)):
            return False, "Operación denegada: ruta de ejecutable protegida."
            
        if not psapi.EmptyWorkingSet(proc_handle):
            err = kernel32.GetLastError()
            msg = f"Acceso denegado: privilegios insuficientes (error {err})." if err == ERROR_ACCESS_DENIED else f"Error al liberar memoria (código {err})."
            return False, msg
            
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (ctypes.ArgumentError, MemoryError, OSError) as e:
        return False, f"Ocurrió un error técnico al gestionar el proceso: {str(e)}"
    finally:
        if proc_handle:
            kernel32.CloseHandle(proc_handle)
