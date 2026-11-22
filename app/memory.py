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

INVALID_HANDLE_VALUE: Final[int] = -1
STILL_ACTIVE_EXIT_CODE: Final[int] = 259
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}

def _handle_api_errors(default: Any) -> Callable:
    """Decorador para atrapar excepciones de la API de Windows y evitar caídas en bucles de escaneo."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except (OSError, ctypes.ArgumentError, AttributeError, ValueError, OverflowError):
                return default
        return wrapper
    return decorator

def bytes_to_mb(bytes_val: int | float) -> float:
    """Convierte bytes a megabytes con precisión de un decimal."""
    return round(float(bytes_val) / MB_DIVISOR, 1)

class WinAccessMasks:
    """Máscaras de acceso para la API de Windows al manipular handles de procesos."""
    QUERY_LIMITED: Final[int] = 0x1000
    QUERY_FULL: Final[int] = 0x0400
    SET_QUOTA: Final[int] = 0x100
    TRIM_REQUIRED: Final[int] = 0x00200000 | SET_QUOTA | QUERY_FULL

class WindowsAPI:
    """Envoltorio estático para las DLLs del sistema operativo."""
    kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
    psapi = ctypes.windll.psapi if os.name == "nt" else None

class CacheEntry(NamedTuple):
    """Timestamp y datos asociados para gestionar la expiración de caché de procesos."""
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
    """Estructura Win32 PROCESS_MEMORY_COUNTERS para métricas de procesos individuales."""
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
    """Instancia inmutable que representa el estado del sistema en un momento dado."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula la cantidad total de memoria física en uso (Bytes)."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Retorna el porcentaje de utilización de RAM física actual."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Retorna el porcentaje de RAM física disponible actualmente."""
        if self.total <= 0: return 0.0
        return round((float(self.available) / float(self.total)) * 100, 1)

@dataclass
class ProcessMemory:
    """Almacena el consumo de memoria de un proceso y su identidad."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> float:
        """Retorna el Working set del proceso expresado en Megabytes."""
        return bytes_to_mb(self.working_set)

    def __lt__(self, other: ProcessMemory) -> bool:
        """Ordenamiento comparativo para identificar procesos de mayor impacto."""
        return self.working_set < other.working_set

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte un valor numérico de bytes a una cadena legible (ej: '1.2 GB')."""
    if not isinstance(num, (int, float)) or not math.isfinite(num) or num < 0:
        return "0 B"
    num_f = float(num)
    if num_f == 0:
        return "0 B"
    try:
        idx: int = min(int(math.log(num_f, 1024)), len(BYTE_UNITS) - 1)
        val: float = num_f / (1024 ** idx)
        return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"
    except (ValueError, ZeroDivisionError, OverflowError):
        return "0 B"

def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """Instancia la estructura Win32 inicializada con su tamaño obligatorio."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Any, multiplier: int = 1) -> BytesValue:
    """Sanitiza entradas numéricas o strings, aplicando escalado y control de overflow."""
    if value is None: 
        return BytesValue(0)
    try:
        if isinstance(value, (int, float)):
            fval = float(value) * multiplier
            return BytesValue(int(fval)) if 0 <= fval < 2**63 - 1 else BytesValue(0)
        if isinstance(value, str):
            clean = "".join(c for c in value if c.isdigit())
            val = int(clean) if clean else 0
            res = float(val) * multiplier
            return BytesValue(int(res)) if 0 <= res < 2**63 - 1 else BytesValue(0)
        return BytesValue(0)
    except (ValueError, OverflowError, TypeError):
        return BytesValue(0)

_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))
_CACHE_TOP_PROC: CacheEntry = CacheEntry(0.0, [])

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Procesa el contenido de /proc/meminfo para extraer métricas de memoria del sistema Linux."""
    if not isinstance(meminfo_text, str) or not meminfo_text:
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
    """Verifica si un PID pertenece a procesos críticos del SO o a la app actual."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _is_invalid_process_name(name: str) -> bool:
    """Valida la integridad del nombre del proceso para evitar procesar entradas corruptas."""
    return not name or any(ord(c) < 32 or ord(c) == 127 for c in name) or len(name) > 256

def _is_valid_process(name: str, pid: int, ws_bytes: int) -> bool:
    """Valida la legitimidad y los rangos operativos de un proceso encontrado."""
    return not _is_system_process(pid) and pid > 0 and not _is_invalid_process_name(name) and (0 < ws_bytes < MAX_VALID_PROCESS_MEM_BYTES)

def _extract_process_info(line: str) -> Optional[ProcessMemory]:
    """Extrae datos de proceso desde una línea de texto CSV previamente validada."""
    if not isinstance(line, str) or not line.strip(): return None
    parts: List[str] = [p.strip().strip("'\"") for p in line.split(",")]
    if len(parts) < 3: return None
    try:
        pid_val = int(parts[1])
        ws_val = _safe_int_conversion(parts[2])
        proc_name = parts[0]
        
        if len(parts) > 3:
            path_str = parts[3].strip()
            if not path_str or not _is_path_safe(path_str):
                return None
        
        if _is_valid_process(proc_name, pid_val, ws_val):
            return ProcessMemory(proc_name, pid_val, ws_val)
    except (ValueError, TypeError):
        return None
    return None

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Transforma un volcado de procesos CSV en una lista ordenada por uso descendente."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text: return []
    lines = raw_csv_text.splitlines()
    if len(lines) < 2: return []
    def _gen() -> Iterator[ProcessMemory]:
        for line in lines[1:]:
            proc = _extract_process_info(line)
            if proc: yield proc
    return heapq.nlargest(limit, _gen())

def _read_windows_snapshot() -> MemorySnapshot:
    """Recupera estadísticas de memoria global del sistema usando Win32 API."""
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
    """Implementa almacenamiento temporal para resultados de memoria (TTL estimado)."""
    if _is_windows: return _read_windows_snapshot()
    try:
        if _linux_mem_path.exists():
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8", errors="ignore"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
    except (OSError, PermissionError): pass
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Interfaz pública para obtener un snapshot del estado de memoria del sistema."""
    return _get_cached_snapshot(int(time.time() / 5))

@lru_cache(maxsize=128)
def _is_path_safe(path_str: str) -> bool:
    """Valida si una ruta está protegida, usando caché para minimizar llamadas I/O."""
    if not path_str or len(path_str) > 1024: return False
    try:
        p = Path(path_str)
        return not is_protected_path(p)
    except (OSError, RuntimeError):
        return False

def _get_process_handle(pid: int, access_mask: int) -> wintypes.HANDLE:
    """Solicita un handle de proceso con los permisos específicos requeridos."""
    if WindowsAPI.kernel32 is None: return wintypes.HANDLE(INVALID_HANDLE_VALUE)
    handle = WindowsAPI.kernel32.OpenProcess(access_mask, False, pid)
    return wintypes.HANDLE(handle if handle else INVALID_HANDLE_VALUE)

@_handle_api_errors(default=None)
def _get_process_memory_counters(handle: wintypes.HANDLE, pmc: PROCESS_MEMORY_COUNTERS) -> Optional[int]:
    """Lee y valida contadores de memoria de un handle de proceso activo."""
    if not handle or handle == INVALID_HANDLE_VALUE or WindowsAPI.psapi is None:
        return None
    if not WindowsAPI.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
        return None
    ws_size = pmc.WorkingSetSize
    return ws_size if 0 < ws_size < MAX_VALID_PROCESS_MEM_BYTES else None

def _iter_process_memory() -> Iterator[ProcessMemory]:
    """
    Escaneo iterativo de procesos en tiempo real.
    
    Obtiene PIDs del sistema, abre handles con permisos limitados y extrae
    información de memoria validando cada ruta contra políticas de seguridad.
    
    Yields:
        ProcessMemory: Datos del proceso si es seguro para leer y procesar.
    """
    if WindowsAPI.psapi is None or WindowsAPI.kernel32 is None: return
    MAX_PIDS = 2048
    pids = (wintypes.DWORD * MAX_PIDS)()
    bytes_written = wintypes.DWORD(0)
    if not WindowsAPI.psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(bytes_written)):
        return
    
    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    path_buf = ctypes.create_unicode_buffer(1024)
    
    count = min(bytes_written.value // ctypes.sizeof(wintypes.DWORD), MAX_PIDS)
    for i in range(count):
        pid = int(pids[i])
        if pid <= 0 or _is_system_process(pid): continue
        
        handle = _get_process_handle(pid, WinAccessMasks.QUERY_LIMITED)
        if handle.value == INVALID_HANDLE_VALUE: continue
        
        try:
            ws = _get_process_memory_counters(handle, pmc)
            if ws:
                res = WindowsAPI.psapi.GetModuleFileNameExW(handle, None, path_buf, 1024)
                if res > 0 and path_buf.value and _is_path_safe(path_buf.value):
                    yield ProcessMemory(Path(path_buf.value).name, pid, BytesValue(ws))
        finally:
            WindowsAPI.kernel32.CloseHandle(handle)

def _get_process_snapshot(limit: int) -> List[ProcessMemory]:
    """Genera un ranking de los procesos con mayor uso de memoria."""
    return heapq.nlargest(limit, _iter_process_memory())

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna procesos de mayor consumo con una estrategia de caché de 60 segundos."""
    global _CACHE_TOP_PROC
    if not _is_windows: return []
    now = time.time()
    if (now - _CACHE_TOP_PROC.timestamp) > 60:
        _CACHE_TOP_PROC = CacheEntry(now, _get_process_snapshot(limit))
    return _CACHE_TOP_PROC.data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el nivel de estrés del sistema basado en el porcentaje de RAM disponible."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Crea una descripción textual detallada sobre el estado actual de la memoria."""
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
    """Ejecuta una solicitud de liberación de Working Set tras validar condiciones de seguridad."""
    if not _is_windows or WindowsAPI.kernel32 is None or WindowsAPI.psapi is None:
        return False, "Operación no soportada."
    if not hasattr(WindowsAPI.psapi, "EmptyWorkingSet"):
        return False, "API del sistema no disponible."
        
    try: target_pid = int(pid)
    except (ValueError, TypeError): return False, "El PID debe ser numérico."
    if _is_system_process(target_pid):
        return False, "Proceso protegido por el sistema."
    
    path_buf = ctypes.create_unicode_buffer(1024)
    handle = WindowsAPI.kernel32.OpenProcess(WinAccessMasks.TRIM_REQUIRED, False, target_pid)
    
    if not handle or handle == INVALID_HANDLE_VALUE:
        return False, "Acceso denegado o proceso protegido."
    
    try:
        res = WindowsAPI.psapi.GetModuleFileNameExW(handle, None, path_buf, 1024)
        if res == 0 or not path_buf.value or not _is_path_safe(path_buf.value):
            return False, "Seguridad: Proceso no válido o en zona restringida."
        if WindowsAPI.psapi.EmptyWorkingSet(handle) != 0:
            return True, f"Working set liberado. {TRIM_WARNING}"
        return False, "El sistema operativo rechazó la solicitud."
    except (ctypes.WinError, OSError, Exception):
        return False, "Error interno al ejecutar la operación."
    finally:
        WindowsAPI.kernel32.CloseHandle(handle)
