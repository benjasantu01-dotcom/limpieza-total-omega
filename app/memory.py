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
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, Final, Set, NewType, NamedTuple, Iterator, Any, Callable, TypeVar
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

T = TypeVar("T")

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

def _handle_api_errors(default: T) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorador para envolver llamadas a la API de Win32.
    Captura excepciones de tipo sistema o ctypes, retornando un valor seguro
    predefinido para evitar que el bucle de escaneo colapse ante procesos inestables.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            try:
                return func(*args, **kwargs)
            except (OSError, ctypes.ArgumentError, AttributeError, ValueError, OverflowError, ctypes.WinError):
                return default
        return wrapper
    return decorator

def bytes_to_mb(bytes_val: int | float) -> float:
    """Convierte un valor de bytes a megabytes (MB) redondeado a un decimal."""
    if not isinstance(bytes_val, (int, float)) or bytes_val < 0 or not math.isfinite(bytes_val):
        return 0.0
    return round(float(bytes_val) / MB_DIVISOR, 1)

class WinAccessMasks:
    """Máscaras de permisos para apertura de procesos mediante OpenProcess."""
    QUERY_LIMITED: Final[int] = 0x1000
    QUERY_FULL: Final[int] = 0x0400
    SET_QUOTA: Final[int] = 0x100
    TRIM_REQUIRED: Final[int] = 0x00200000 | SET_QUOTA | QUERY_FULL

class WindowsAPI:
    """Acceso centralizado a librerías de sistema (kernel32, psapi) bajo Windows."""
    kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
    psapi = ctypes.windll.psapi if os.name == "nt" else None

class CacheEntry(NamedTuple):
    """Contenedor de resultados cacheados con timestamp para invalidación temporal."""
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
    """Estructura Win32 GlobalMemoryStatusEx para reportar el estado global de RAM."""
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
    """Estructura PSAPI para consultar contadores de memoria de un proceso específico."""
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
    """Estado inmutable de la memoria física del sistema en un momento dado."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula la cantidad de memoria física actualmente ocupada."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Calcula el porcentaje de RAM física utilizada."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Calcula el porcentaje de RAM física disponible."""
        if self.total <= 0: return 0.0
        return round((float(self.available) / float(self.total)) * 100, 1)

@dataclass
class ProcessMemory:
    """Registro del consumo de memoria de un proceso identificado por PID."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> float:
        """Retorna el Working Set del proceso convertido a megabytes."""
        return bytes_to_mb(self.working_set)

    def __lt__(self, other: ProcessMemory) -> bool:
        """Comparador para ordenar procesos de mayor a menor consumo de RAM."""
        return self.working_set < other.working_set

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte bytes a una cadena legible (ej: '1.2 GB') usando formato escalado."""
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
    """Prepara la estructura MEMORYSTATUSEX configurando su longitud para la API."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Any, multiplier: int = 1) -> BytesValue:
    """Convierte entradas heterogéneas a BytesValue validando límites seguros."""
    if value is None: return BytesValue(0)
    try:
        if isinstance(value, (int, float)):
            numeric_val = float(value)
        elif isinstance(value, str):
            clean_str = "".join(filter(str.isdigit, value))
            numeric_val = float(clean_str) if clean_str else 0.0
        else:
            return BytesValue(0)
            
        if not math.isfinite(numeric_val) or numeric_val < 0:
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
    """Interpreta el archivo /proc/meminfo de sistemas basados en Linux."""
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
    """Determina si un PID pertenece a servicios críticos del sistema o a la app."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _is_invalid_process_name(name: str) -> bool:
    """Valida que el nombre de un proceso no contenga caracteres de control."""
    return not name or any(ord(c) < 32 or ord(c) == 127 for c in name) or len(name) > 256

def _is_valid_process(name: str, pid: int, ws_bytes: int) -> bool:
    """Verifica si un proceso es un candidato legítimo para ser listado."""
    return not _is_system_process(pid) and pid > 0 and not _is_invalid_process_name(name) and (0 < ws_bytes < MAX_VALID_PROCESS_MEM_BYTES)

def _extract_process_info(line: str) -> Optional[ProcessMemory]:
    """Extrae datos de un proceso a partir de una línea CSV."""
    if not isinstance(line, str) or not line.strip(): return None
    
    try:
        parts: List[str] = [p.strip().strip("'\"") for p in line.split(",")]
        if len(parts) < 3: return None
        
        proc_name = str(parts[0])
        pid_val = int(parts[1])
        ws_val = _safe_int_conversion(parts[2])
        
        if len(parts) > 3:
            path_str = str(parts[3]).strip()
            if path_str and not _is_path_safe(path_str):
                return None
        
        if _is_valid_process(proc_name, pid_val, ws_bytes=ws_val):
            return ProcessMemory(proc_name, pid_val, ws_val)
    except (ValueError, TypeError, IndexError):
        pass
    return None

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Convierte CSV generado externamente a lista de objetos ProcessMemory."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text: return []
    lines = raw_csv_text.splitlines()
    if len(lines) < 2: return []
    # Usamos una lista de comprensión para evitar múltiples llamadas a generador dentro de nlargest
    results = [proc for line in lines[1:] if (proc := _extract_process_info(line))]
    return heapq.nlargest(limit, results)

def _read_windows_snapshot() -> MemorySnapshot:
    """Obtiene el estado de memoria del sistema mediante la API nativa de Windows."""
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
    """Implementa TTL para el snapshot global, reduciendo llamadas a la API."""
    if _is_windows: return _read_windows_snapshot()
    try:
        if _linux_mem_path.exists():
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8", errors="ignore"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
    except (OSError, PermissionError): pass
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Interfaz pública para obtener el estado actual de la memoria."""
    return _get_cached_snapshot(int(time.time() / 5))

@lru_cache(maxsize=256)
def _is_path_safe(path_str: Optional[str]) -> bool:
    """Consulta al módulo safety si una ruta de ejecutable es segura."""
    if not path_str or not isinstance(path_str, str) or len(path_str) > 1024: 
        return False
    try:
        return not is_protected_path(Path(path_str))
    except (OSError, RuntimeError, TypeError):
        return False

def _ensure_safe_handle_access(handle: wintypes.HANDLE) -> bool:
    """Valida que el binario vinculado a un handle sea seguro."""
    if WindowsAPI.psapi is None: return False
    path_buf = ctypes.create_unicode_buffer(1024)
    if WindowsAPI.psapi.GetModuleFileNameExW(handle, None, path_buf, 1024) != 0:
        return _is_path_safe(path_buf.value)
    return False

def _get_process_handle(pid: int, access_mask: int) -> wintypes.HANDLE:
    """Abre un handle de proceso con validaciones de seguridad integradas."""
    if WindowsAPI.kernel32 is None: return wintypes.HANDLE(None)
    handle_val = WindowsAPI.kernel32.OpenProcess(access_mask, False, pid)
    if not handle_val: return wintypes.HANDLE(None)
    
    handle = wintypes.HANDLE(handle_val)
    try:
        if not _ensure_safe_handle_access(handle):
            return wintypes.HANDLE(None)
        
        exit_code = wintypes.DWORD(0)
        if WindowsAPI.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0 or exit_code.value != STILL_ACTIVE_EXIT_CODE:
            return wintypes.HANDLE(None)
        return handle
    except Exception:
        WindowsAPI.kernel32.CloseHandle(handle)
        return wintypes.HANDLE(None)

def _is_handle_valid(handle: wintypes.HANDLE) -> bool:
    """Verifica si el handle es válido para retorno."""
    return handle.value is not None and handle.value != 0

@_handle_api_errors(default=None)
def _get_process_memory_counters(handle: wintypes.HANDLE, pmc: PROCESS_MEMORY_COUNTERS) -> Optional[int]:
    """Obtiene contadores específicos mediante la estructura PSAPI de Windows."""
    if not handle or handle.value is None or WindowsAPI.psapi is None:
        return None
    if not WindowsAPI.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
        return None
    ws_size = pmc.WorkingSetSize
    return ws_size if 0 < ws_size < MAX_VALID_PROCESS_MEM_BYTES else None

def _iter_process_memory() -> Iterator[ProcessMemory]:
    """Generador que recorre los procesos activos en el sistema."""
    if WindowsAPI.psapi is None or WindowsAPI.kernel32 is None: return
    
    needed = wintypes.DWORD(0)
    if not WindowsAPI.psapi.EnumProcesses(None, 0, ctypes.byref(needed)): return
    
    count = needed.value // ctypes.sizeof(wintypes.DWORD)
    pids = (wintypes.DWORD * count)()
    if not WindowsAPI.psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed)):
        return
    
    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    name_buf = ctypes.create_unicode_buffer(256)
    
    for i in range(count):
        pid = int(pids[i])
        if pid <= 0 or _is_system_process(pid): continue
        
        handle = _get_process_handle(pid, WinAccessMasks.QUERY_FULL)
        if not handle or not handle.value: continue
        
        try:
            ws = _get_process_memory_counters(handle, pmc)
            if ws is not None:
                res = WindowsAPI.psapi.GetModuleBaseNameW(handle, None, name_buf, 256)
                proc_name = name_buf.value if res > 0 else f"PID-{pid}"
                yield ProcessMemory(proc_name, pid, BytesValue(ws))
        finally:
            WindowsAPI.kernel32.CloseHandle(handle)

def _get_process_snapshot(limit: int) -> List[ProcessMemory]:
    """Captura los N procesos con mayor uso de RAM."""
    return heapq.nlargest(limit, _iter_process_memory())

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Obtiene la lista de los procesos que más memoria consumen (cacheado 60s)."""
    global _CACHE_TOP_PROC
    if not _is_windows: return []
    now = time.time()
    if (now - _CACHE_TOP_PROC.timestamp) > 60:
        _CACHE_TOP_PROC = CacheEntry(now, _get_process_snapshot(limit))
    return _CACHE_TOP_PROC.data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Determina el estado de presión de RAM según porcentaje disponible."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Retorna una lista de cadenas descriptivas sobre el diagnóstico de memoria."""
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
    """Libera el conjunto de trabajo de un proceso, acción solo para usuarios expertos."""
    if not _is_windows or WindowsAPI.kernel32 is None or WindowsAPI.psapi is None:
        return False, "Operación no soportada."
    
    try: target_pid = int(pid)
    except (ValueError, TypeError): return False, "El PID debe ser numérico."
    if _is_system_process(target_pid):
        return False, "Proceso protegido por el sistema."
    
    handle = _get_process_handle(target_pid, WinAccessMasks.TRIM_REQUIRED)
    if not handle or not handle.value:
        return False, "Acceso denegado, proceso no encontrado o ruta restringida."
    
    try:
        if WindowsAPI.psapi.EmptyWorkingSet(handle) != 0:
            return True, f"Working set liberado. {TRIM_WARNING}"
        return False, "El sistema operativo rechazó la solicitud."
    except Exception:
        return False, "Error interno al ejecutar la operación."
    finally:
        WindowsAPI.kernel32.CloseHandle(handle)
