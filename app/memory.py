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
import subprocess
import math
import ctypes
import time
from functools import lru_cache
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, TypeVar, TypeAlias, Final
from safety import is_protected_path

if TYPE_CHECKING:
    from ctypes import wintypes

_T = TypeVar("_T", int, float)
BytesValue: TypeAlias = int
MegabytesValue: TypeAlias = float

BYTES_IN_MB: Final[int] = 1024 * 1024

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

TRIM_WARNING: Final[str] = (
    "Liberar el working set NO acelera la PC: fuerza a Windows a expulsar "
    "memoria que los programas están usando, y al volver a necesitarla la "
    "tiene que releer del disco. El número de 'RAM libre' sube, pero el "
    "rendimiento suele empeorar. Solo tiene sentido antes de medir algo "
    "puntual, no como mantenimiento."
)

BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")

# Constantes para Win32 API: permisos mínimos necesarios para diagnóstico y gestión
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x0100
SAFE_ACCESS_MASK: Final[int] = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

STILL_ACTIVE_EXIT_CODE: Final[int] = 259
ERROR_ACCESS_DENIED: Final[int] = 5

# PIDs reservados: 0 (System Idle), 4 (System)
SYSTEM_CRITICAL_PIDS: Final[Tuple[int, ...]] = (0, 4)

_last_proc_fetch: float = 0.0
_cached_proc_output: str = ""

class MEMORYSTATUSEX(ctypes.Structure):
    """
    Estructura binaria para la API Win32 GlobalMemoryStatusEx.
    Utilizada para consultar el estado global de la memoria física y virtual.
    """
    _fields_: List[Tuple[str, type]] = [
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
    """Instancia la estructura MEMORYSTATUSEX configurando su tamaño requerido."""
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return stat

@dataclass(frozen=True)
class MemorySnapshot:
    """Representa el estado global de la memoria física y virtual del sistema."""
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
    """Metadatos de consumo de memoria de un proceso individual."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Retorna el Working Set normalizado en Megabytes."""
        return round(self.working_set / BYTES_IN_MB, 1)


def format_bytes(num: Optional[int | float]) -> str:
    """
    Formatea una cantidad de bytes a una cadena legible (ej: 1.5 MB).
    """
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"


@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """
    Analiza el contenido crudo de /proc/meminfo en sistemas Linux.
    """
    if not meminfo_text:
        return MemorySnapshot(0, 0)
    
    metrics: Dict[str, int] = {}
    target_keys = {"MemTotal", "MemAvailable", "MemFree", "Cached"}
    
    for line in meminfo_text.splitlines():
        parts = line.split(":")
        if len(parts) == 2 and parts[0] in target_keys:
            metrics[parts[0]] = int(parts[1].split()[0]) * 1024
    
    total = metrics.get("MemTotal", 0)
    available = metrics.get("MemAvailable", metrics.get("MemFree", 0))
    cached = metrics.get("Cached", 0)
    
    return MemorySnapshot(total=total, available=min(available, total), cached=cached)


def _parse_csv_row(csv_line: str) -> Optional[ProcessMemory]:
    """
    Extrae un objeto ProcessMemory a partir de una línea CSV (Name,PID,WorkingSet).
    """
    if not isinstance(csv_line, str):
        return None
    line = csv_line.strip()
    if not line or "," not in line:
        return None
    
    parts = line.rsplit(",", 2)
    if len(parts) < 3:
        return None
        
    try:
        name = parts[0].strip().strip("'\"")
        pid_raw = parts[1].strip().strip("'\"")
        ws_raw = parts[2].strip().strip("'\"")
        
        if not name or not ws_raw.isdigit() or not pid_raw.isdigit():
            return None
            
        return ProcessMemory(name=name, pid=int(pid_raw), working_set=int(ws_raw))
    except (ValueError, TypeError, IndexError):
        return None


def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """
    Procesa múltiples líneas CSV de procesos y los ordena por mayor uso de RAM.
    """
    if not isinstance(raw_csv_text, str) or not raw_csv_text:
        return []
    
    processes = [proc for line in raw_csv_text.splitlines() if (proc := _parse_csv_row(line))]
    processes.sort(key=lambda p: p.working_set, reverse=True)
    return processes[:max(0, limit)]


def _read_windows_snapshot() -> MemorySnapshot:
    """
    Obtiene el estado de memoria vía llamada nativa GlobalMemoryStatusEx.
    """
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    if kernel32 is None or not hasattr(kernel32, "GlobalMemoryStatusEx"):
        return MemorySnapshot(0, 0)
    
    stat = _create_mem_status_ex()
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return MemorySnapshot(0, 0)
    
    try:
        total = int(stat.ullTotalPhys)
        avail = int(stat.ullAvailPhys)
        if total <= 0:
            return MemorySnapshot(0, 0)
        return MemorySnapshot(total=total, available=min(avail, total))
    except (ValueError, TypeError, OverflowError):
        return MemorySnapshot(0, 0)


def read_snapshot() -> MemorySnapshot:
    """
    Función de acceso unificada para obtener una instantánea de memoria del host.
    """
    if os.name == "nt":
        try:
            return _read_windows_snapshot()
        except (AttributeError, OSError, ctypes.ArgumentError):
            return MemorySnapshot(0, 0)
    
    try:
        with open("/proc/meminfo", encoding="utf-8", errors="replace") as f:
            return parse_linux_meminfo(f.read())
    except (OSError, PermissionError):
        return MemorySnapshot(0, 0)


def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """
    Obtiene los procesos más pesados mediante una consulta PowerShell cacheada.
    """
    global _last_proc_fetch, _cached_proc_output
    
    if os.name != "nt":
        return []

    if (time.time() - _last_proc_fetch) > 30:
        cmd = "Get-Process | Sort-Object WorkingSet -Descending | Select-Object -First 20 Name,Id,WorkingSet | ForEach-Object { \"$($_.Name),$($_.Id),$($_.WorkingSet)\" }"
        try:
            proc = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0:
                _cached_proc_output = proc.stdout
                _last_proc_fetch = time.time()
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            pass 
    
    return parse_windows_process_csv(_cached_proc_output, limit=limit)


@lru_cache(maxsize=2)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """
    Clasifica el nivel de estrés de memoria según el porcentaje disponible.
    """
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return "info"
    
    available = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"


def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """
    Genera un diagnóstico narrativo sobre el estado de la RAM.
    """
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
    """Verifica si un proceso dado es crítico para la estabilidad del SO."""
    if not isinstance(pid, int) or pid <= 0:
        return True
    return pid in SYSTEM_CRITICAL_PIDS or pid < 100


def _get_process_path(handle: wintypes.HANDLE) -> Optional[str]:
    """Resuelve la ruta absoluta del ejecutable desde un handle de Win32."""
    if not handle:
        return None
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    if not kernel32 or not hasattr(kernel32, "QueryFullProcessImageNameW"):
        return None
    
    size = ctypes.c_ulong(4096)
    buf = ctypes.create_unicode_buffer(size.value)
    
    try:
        # Usamos handle explícito para asegurar compatibilidad de tipos
        if kernel32.QueryFullProcessImageNameW(handle, 0, ctypes.byref(buf), ctypes.byref(size)) > 0:
            return str(buf.value)
    except (OSError, ctypes.ArgumentError):
        return None
    return None


def _is_valid_trim_target(proc_handle: wintypes.HANDLE) -> Tuple[bool, Optional[str]]:
    """Realiza chequeos de seguridad antes de permitir la liberación de RAM."""
    if not proc_handle:
        return False, "Handle inválido."

    kernel32 = ctypes.windll.kernel32
    exit_code = ctypes.c_ulong()
    
    if not kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(exit_code)):
        return False, "No se pudo obtener el estado del proceso."
    
    if exit_code.value != STILL_ACTIVE_EXIT_CODE:
        return False, "El proceso seleccionado ya no está activo."
        
    path = _get_process_path(proc_handle)
    if not path:
        return False, "No se pudo verificar la ubicación del ejecutable."
    
    normalized_path = os.path.normcase(os.path.normpath(path))
    if is_protected_path(normalized_path):
        return False, "Operación denegada: ruta de ejecutable protegida."
        
    return True, None


def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """Solicita al S.O. liberar el Working Set (RAM residente) de un proceso."""
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
        return False, "Acceso denegado al proceso (podría haber finalizado)."
        
    try:
        valid, reason = _is_valid_trim_target(proc_handle)
        if not valid:
            return False, reason or "Validación de proceso fallida."
            
        if not psapi.EmptyWorkingSet(proc_handle):
            error = kernel32.GetLastError()
            return False, f"Error al liberar memoria (código {error})."
            
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (ctypes.ArgumentError, Exception):
        return False, "Ocurrió un error técnico al gestionar el proceso."
    finally:
        if proc_handle:
            kernel32.CloseHandle(proc_handle)
