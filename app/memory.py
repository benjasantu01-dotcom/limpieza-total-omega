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
import heapq
from pathlib import Path
from functools import lru_cache, wraps
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, Final, Set, NewType, NamedTuple, Iterator, Any, Callable
from safety import is_protected_path

if TYPE_CHECKING:
    from ctypes import wintypes
else:
    class wintypes:
        HANDLE = ctypes.c_void_p
        DWORD = ctypes.c_ulong
        BOOL = ctypes.c_int
        PDWORD = ctypes.POINTER(ctypes.c_ulong)
        ULONGLONG = ctypes.c_ulonglong

# Alias de tipos para facilitar el entendimiento de las unidades de medida
BytesValue = NewType("BytesValue", int)
MegabytesValue = float

BYTES_IN_MB: Final[int] = 1024 * 1024
MB_DIVISOR: Final[float] = float(BYTES_IN_MB)
BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")
MAX_VALID_PROCESS_MEM_BYTES: Final[int] = 128 * 1024 * BYTES_IN_MB 
MAX_SAFE_INT: Final[int] = 2**63 - 1

INVALID_HANDLE_VALUE: Final[int] = -1
STILL_ACTIVE_EXIT_CODE: Final[int] = 259
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}

def _handle_api_errors(default: Any) -> Callable:
    """Decorador para atrapar excepciones de la API de Windows y evitar caídas en bucles de escaneo."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except (OSError, ctypes.ArgumentError, AttributeError, ValueError, OverflowError):
                return default
        return wrapper
    return decorator

def bytes_to_mb(bytes_val: int | float) -> float:
    """Convierte un valor de bytes a megabytes mediante división escalar."""
    return round(float(bytes_val) / MB_DIVISOR, 1)

class WinAccessMasks:
    """Máscaras de acceso estándar para interacciones con procesos via Win32 API."""
    QUERY_LIMITED: Final[int] = 0x1000
    QUERY_FULL: Final[int] = 0x0400
    SET_QUOTA: Final[int] = 0x100
    TRIM_REQUIRED: Final[int] = 0x00200000 | SET_QUOTA | QUERY_FULL

class WindowsAPI:
    """Envoltorio estático para las librerías dinámicas del sistema operativo (Win32)."""
    kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
    psapi = ctypes.windll.psapi if os.name == "nt" else None

class CacheEntry(NamedTuple):
    """Estructura de almacenamiento temporal para resultados de procesos con timestamp."""
    timestamp: float
    data: List[ProcessMemory]

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
    "tiene que releer del disco."
)

class MEMORYSTATUSEX(ctypes.Structure):
    """Estructura Win32 GlobalMemoryStatusEx para obtener el estado físico del sistema."""
    _fields_: List[Tuple[str, Any]] = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", wintypes.ULONGLONG),
        ("ullAvailPhys", wintypes.ULONGLONG),
        ("ullTotalPageFile", wintypes.ULONGLONG),
        ("ullAvailPageFile", wintypes.ULONGLONG),
        ("ullTotalVirtual", wintypes.ULONGLONG),
        ("ullAvailVirtual", wintypes.ULONGLONG),
        ("ullAvailExtendedVirtual", wintypes.ULONGLONG),
    ]

class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    """Estructura Win32 PROCESS_MEMORY_COUNTERS para métricas de procesos."""
    _fields_: List[Tuple[str, Any]] = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]

@dataclass(frozen=True)
class MemorySnapshot:
    """Representación inmutable de una instantánea de memoria física del sistema."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula el total de memoria física en uso (Total - Disponible)."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Porcentaje de utilización de RAM física."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Porcentaje de RAM física disponible."""
        if self.total <= 0: return 0.0
        return round((float(self.available) / float(self.total)) * 100, 1)

@dataclass
class ProcessMemory:
    """Información consolidada del consumo de memoria de un proceso específico."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> float:
        """Retorna el Working set del proceso expresado en Megabytes."""
        return bytes_to_mb(self.working_set)

    def __lt__(self, other: ProcessMemory) -> bool:
        """Ordenamiento por uso de memoria descendente para heapq."""
        return self.working_set < other.working_set

def format_bytes(num: Optional[int | float]) -> str:
    """Formatea valores numéricos a una cadena de texto amigable (ej: '1.2 GB')."""
    if not isinstance(num, (int, float)) or not math.isfinite(num) or num < 0:
        return "0 B"
    
    value = float(num)
    if value == 0:
        return "0 B"
        
    try:
        magnitude = int(math.log(value, 1024))
        idx = min(magnitude, len(BYTE_UNITS) - 1)
        scaled_value = value / (1024 ** idx)
        precision = 0 if idx == 0 else 1
        return f"{scaled_value:.{precision}f} {BYTE_UNITS[idx]}"
    except (ValueError, ZeroDivisionError, OverflowError):
        return "0 B"

def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """Retorna una instancia de MEMORYSTATUSEX correctamente inicializada."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Any, multiplier: int = 1) -> BytesValue:
    """Valida y convierte entradas a BytesValue, previniendo desbordamientos."""
    try:
        if isinstance(value, (int, float)):
            numeric_val = float(value)
        elif isinstance(value, str):
            clean_str = "".join(filter(str.isdigit, value))
            numeric_val = float(clean_str) if clean_str else 0.0
        else:
            return BytesValue(0)
            
        scaled = numeric_val * multiplier
        if 0 <= scaled < MAX_SAFE_INT:
            return BytesValue(int(scaled))
        return BytesValue(0)
    except (ValueError, TypeError, OverflowError):
        return BytesValue(0)

_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))
_CACHE_TOP_PROC: CacheEntry = CacheEntry(0.0, [])

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Parsea el formato estándar de /proc/meminfo a un objeto MemorySnapshot."""
    if not isinstance(meminfo_text, str) or not meminfo_text.strip():
        return _EMPTY_SNAPSHOT
    metrics: Dict[str, BytesValue] = {}
    for line in meminfo_text.splitlines():
        if ":" not in line: continue
        parts = line.split(":", 1)
        key = parts[0].strip()
        val_str = parts[1].strip().split()
        if key and val_str:
            metrics[key] = _safe_int_conversion(val_str[0], 1024)
    total = metrics.get("MemTotal", BytesValue(0))
    if total <= 0: return _EMPTY_SNAPSHOT
    avail_raw = metrics.get("MemAvailable", metrics.get("MemFree", BytesValue(0)))
    available = BytesValue(max(0, min(total, avail_raw)))
    cached = BytesValue(max(0, metrics.get("Cached", BytesValue(0))))
    return MemorySnapshot(total=total, available=available, cached=cached)

def _is_system_process(pid: int) -> bool:
    """Identifica procesos críticos que no deben ser manipulados por el usuario."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _is_invalid_process_name(name: str) -> bool:
    """Valida la integridad de strings de nombres de procesos (prohibe caracteres de control)."""
    return not name or any(ord(c) < 32 or ord(c) == 127 for c in name) or len(name) > 256

def _is_valid_process(name: str, pid: int, ws_bytes: int) -> bool:
    """Valida la legitimidad y los rangos operativos de los datos de un proceso."""
    return not _is_system_process(pid) and pid > 0 and not _is_invalid_process_name(name) and (0 < ws_bytes < MAX_VALID_PROCESS_MEM_BYTES)

def _extract_process_info(line: str) -> Optional[ProcessMemory]:
    """Extrae datos de proceso desde una línea CSV con validación estricta de formato."""
    if not isinstance(line, str): return None
    clean_line = line.strip()
    if not clean_line: return None
    
    parts: List[str] = [p.strip().strip("'\"") for p in clean_line.split(",")]
    if len(parts) < 3: return None
    
    try:
        proc_name = parts[0]
        pid_val = int(parts[1])
        ws_val = _safe_int_conversion(parts[2])
        
        if len(parts) > 3:
            path_str = parts[3].strip()
            if path_str and not _is_path_safe(path_str):
                return None
        
        if _is_valid_process(proc_name, pid_val, ws_val):
            return ProcessMemory(proc_name, pid_val, ws_val)
    except (ValueError, TypeError, IndexError):
        return None
    return None

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Transforma un volcado de procesos CSV en una lista ordenada por uso."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text: return []
    lines = raw_csv_text.splitlines()
    if len(lines) < 2: return []
    def _gen() -> Iterator[ProcessMemory]:
        for line in lines[1:]:
            proc = _extract_process_info(line)
            if proc: yield proc
    return heapq.nlargest(limit, _gen())

def _read_windows_snapshot() -> MemorySnapshot:
    """Interroga la API Win32 para obtener el estado actual de la memoria."""
    if WindowsAPI.kernel32 is None or not hasattr(WindowsAPI.kernel32, "GlobalMemoryStatusEx"): 
        return _EMPTY_SNAPSHOT
    mem_status = _create_mem_status_ex()
    if WindowsAPI.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_status)) != 0:
        if 0 < mem_status.ullTotalPhys < (1024**5 * 256): 
            return MemorySnapshot(
                total=BytesValue(mem_status.ullTotalPhys), 
                available=BytesValue(mem_status.ullAvailPhys)
            )
    return _EMPTY_SNAPSHOT

@lru_cache(maxsize=1)
def _get_cached_snapshot(timestamp_bucket: int) -> MemorySnapshot:
    """Implementa TTL para el snapshot global del sistema."""
    if _is_windows: return _read_windows_snapshot()
    try:
        if _linux_mem_path.exists():
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8", errors="ignore"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
    except (OSError, PermissionError): pass
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Interfaz pública para obtener un estado consolidado de la memoria."""
    return _get_cached_snapshot(int(time.time() / 5))

@lru_cache(maxsize=256)
def _is_path_safe(path_str: Optional[str]) -> bool:
    """Valida la seguridad de la ruta mediante `safety.py` con caché interna."""
    if not path_str or not isinstance(path_str, str) or len(path_str) > 1024: 
        return False
    try:
        return not is_protected_path(Path(path_str))
    except (OSError, RuntimeError, TypeError):
        return False

def _get_process_handle(pid: int, access_mask: int) -> wintypes.HANDLE:
    """Obtiene un handle para manipular o consultar un proceso, validando su estado activo."""
    if WindowsAPI.kernel32 is None: return wintypes.HANDLE(None)
    handle = WindowsAPI.kernel32.OpenProcess(access_mask, False, pid)
    if not handle:
        return wintypes.HANDLE(None)
    
    exit_code = wintypes.DWORD(0)
    if WindowsAPI.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) != 0:
        if exit_code.value == STILL_ACTIVE_EXIT_CODE:
            return wintypes.HANDLE(handle)
            
    WindowsAPI.kernel32.CloseHandle(handle)
    return wintypes.HANDLE(None)

@_handle_api_errors(default=None)
def _get_process_memory_counters(handle: wintypes.HANDLE, pmc: PROCESS_MEMORY_COUNTERS) -> Optional[int]:
    """Lee contadores de memoria mediante la API de PSAPI."""
    if not handle or handle.value is None or WindowsAPI.psapi is None:
        return None
    if not WindowsAPI.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
        return None
    ws_size = pmc.WorkingSetSize
    return ws_size if 0 < ws_size < MAX_VALID_PROCESS_MEM_BYTES else None

def _iter_process_memory() -> Iterator[ProcessMemory]:
    """Iterador optimizado que escanea procesos con validación de seguridad de ruta cacheada."""
    if WindowsAPI.psapi is None or WindowsAPI.kernel32 is None: return
    
    needed = wintypes.DWORD(0)
    WindowsAPI.psapi.EnumProcesses(None, 0, ctypes.byref(needed))
    count = needed.value // ctypes.sizeof(wintypes.DWORD)
    pids = (wintypes.DWORD * count)()
    if not WindowsAPI.psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed)):
        return
    
    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    path_buf = ctypes.create_unicode_buffer(1024)
    name_buf = ctypes.create_unicode_buffer(256)
    
    for i in range(count):
        pid = int(pids[i])
        if pid <= 0 or _is_system_process(pid): continue
        
        handle = _get_process_handle(pid, WinAccessMasks.QUERY_FULL)
        if not handle.value: continue
        
        try:
            size = wintypes.DWORD(1024)
            if WindowsAPI.kernel32.QueryFullProcessImageNameW(handle, 0, path_buf, ctypes.byref(size)) != 0:
                if not _is_path_safe(path_buf.value): continue
            
            ws = _get_process_memory_counters(handle, pmc)
            if ws is not None:
                res = WindowsAPI.psapi.GetModuleBaseNameW(handle, None, name_buf, 256)
                proc_name = name_buf.value if res > 0 else f"PID-{pid}"
                yield ProcessMemory(proc_name, pid, BytesValue(ws))
        finally:
            WindowsAPI.kernel32.CloseHandle(handle)

def _get_process_snapshot(limit: int) -> List[ProcessMemory]:
    """Genera la lista de los procesos más intensivos."""
    return heapq.nlargest(limit, _iter_process_memory())

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna los procesos de mayor consumo (cacheado 60s)."""
    global _CACHE_TOP_PROC
    if not _is_windows: return []
    now = time.time()
    if (now - _CACHE_TOP_PROC.timestamp) > 60:
        _CACHE_TOP_PROC = CacheEntry(now, _get_process_snapshot(limit))
    return _CACHE_TOP_PROC.data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Determina la criticidad del consumo de memoria según umbrales de disponibilidad."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un reporte legible sobre el estado de la RAM."""
    if snapshot.total <= 0: return ["No se pudo leer el estado de la memoria."]
    diagnostics: Dict[str, str] = {
        "ok": "Estado: holgado. La memoria ocupada por caché mejora la velocidad.",
        "info": "Estado: normal. Windows gestiona la memoria de forma eficiente.",
        "warning": "Estado: ajustado. Conviene cerrar aplicaciones innecesarias.",
        "danger": "Estado: crítico. El sistema recurre al archivo de paginación."
    }
    report: List[str] = [
        f"Memoria total: {format_bytes(snapshot.total)}",
        f"En uso: {format_bytes(snapshot.used)} ({snapshot.used_percent}%)",
        f"Disponible: {format_bytes(snapshot.available)} ({snapshot.available_percent}%)",
        diagnostics.get(pressure_level(snapshot), "")
    ]
    if processes:
        report.extend(f"  Mayor consumo: {p.name} (PID {p.pid}) — {bytes_to_mb(p.working_set)} MB" for p in processes[:3])
    return report

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """Ejecuta una liberación de Working Set segura para el proceso dado, validando la integridad del buffer."""
    if not _is_windows or WindowsAPI.kernel32 is None or WindowsAPI.psapi is None:
        return False, "Operación no soportada."
    
    try: target_pid = int(pid)
    except (ValueError, TypeError): return False, "El PID debe ser numérico."
    if _is_system_process(target_pid):
        return False, "Proceso protegido por el sistema."
    
    handle = _get_process_handle(target_pid, WinAccessMasks.TRIM_REQUIRED)
    if not handle.value:
        return False, "Acceso denegado, proceso no encontrado o en estado de terminación."
    
    try:
        path_buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if WindowsAPI.kernel32.QueryFullProcessImageNameW(handle, 0, path_buf, ctypes.byref(size)) > 0:
            if not _is_path_safe(path_buf.value):
                return False, "Seguridad: Proceso en ruta restringida o protegida."
        
        if WindowsAPI.psapi.EmptyWorkingSet(handle) != 0:
            return True, f"Working set liberado. {TRIM_WARNING}"
        return False, "El sistema operativo rechazó la solicitud."
    except (ctypes.WinError, OSError, Exception):
        return False, "Error interno al ejecutar la operación."
    finally:
        WindowsAPI.kernel32.CloseHandle(handle)
