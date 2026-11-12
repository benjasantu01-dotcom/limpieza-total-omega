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
from functools import lru_cache
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

BytesValue = NewType("BytesValue", int)
MegabytesValue = MegabytesValue = float

BYTES_IN_MB: Final[int] = 1024 * 1024
BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")
MAX_VALID_PROCESS_MEM_BYTES: Final[int] = 128 * 1024 * BYTES_IN_MB 

INVALID_HANDLE_VALUE: Final[int] = -1
STILL_ACTIVE_EXIT_CODE: Final[int] = 259
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}

class WinAccessMasks:
    """Constantes de permisos de acceso para abrir procesos mediante la Win32 API."""
    PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
    PROCESS_QUERY_INFORMATION: Final[int] = 0x0400
    PROCESS_SET_QUOTA: Final[int] = 0x100
    TRIM_REQUIRED: Final[int] = 0x00200000 | PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION

class WindowsAPI:
    """Namespace que centraliza el acceso a librerías nativas Win32 (kernel32/psapi)."""
    kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
    psapi = ctypes.windll.psapi if os.name == "nt" else None

class CacheEntry(NamedTuple):
    """Caché temporal con timestamp para limitar la frecuencia de consultas costosas."""
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
    _fields_: List[Tuple[str, Any]] = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]

class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
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
    """Representación inmutable de la salud de memoria del sistema."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        if self.total <= 0: return 0.0
        return round((float(self.available) / float(self.total)) * 100, 1)

@dataclass
class ProcessMemory:
    """Datos de consumo de un proceso detectado en ejecución."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> float:
        return round(self.working_set / BYTES_IN_MB, 1)

    def __lt__(self, other: ProcessMemory) -> bool:
        return self.working_set < other.working_set

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte un valor numérico a una cadena legible con unidades de medida."""
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
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Any, multiplier: int = 1) -> BytesValue:
    """Convierte entradas heterogéneas a BytesValue validando rangos y tipos."""
    if value is None: return BytesValue(0)
    try:
        val: int = 0
        if isinstance(value, (int, float)):
            if not math.isfinite(value): return BytesValue(0)
            fval = float(value) * multiplier
            if fval < 0 or fval > float(2**63 - 1): return BytesValue(0)
            val = int(fval)
        elif isinstance(value, str):
            clean_val = "".join(c for c in value if c.isdigit())
            if not clean_val: return BytesValue(0)
            val = int(clean_val) * multiplier
        else:
            return BytesValue(0)
        return BytesValue(max(0, val))
    except (ValueError, OverflowError, TypeError):
        return BytesValue(0)

_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))
_CACHE_TOP_PROC: CacheEntry = CacheEntry(0.0, [])

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Interpreta el contenido de /proc/meminfo para extraer métricas clave."""
    if not isinstance(meminfo_text, str) or not meminfo_text:
        return _EMPTY_SNAPSHOT
    metrics: Dict[str, BytesValue] = {}
    try:
        for line in meminfo_text.splitlines():
            if ":" not in line: continue
            key, _, value_part = line.partition(":")
            key_stripped = key.strip()
            if not key_stripped: continue
            metrics[key_stripped] = _safe_int_conversion(value_part, 1024)
    except Exception:
        return _EMPTY_SNAPSHOT
    total: BytesValue = metrics.get("MemTotal", BytesValue(0))
    if total <= 0: return _EMPTY_SNAPSHOT
    avail_raw = metrics.get("MemAvailable", metrics.get("MemFree", BytesValue(0)))
    available = BytesValue(max(0, min(total, avail_raw)))
    cached = BytesValue(max(0, metrics.get("Cached", BytesValue(0))))
    return MemorySnapshot(total=total, available=available, cached=cached)

def _is_system_process(pid: int) -> bool:
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _is_invalid_process_name(name: str) -> bool:
    return not name or any(ord(c) < 32 or ord(c) == 127 for c in name) or len(name) > 256

def _is_valid_process(name: str, pid: int, ws_bytes: int) -> bool:
    if _is_system_process(pid) or pid <= 0 or _is_invalid_process_name(name):
        return False
    return 0 < ws_bytes < MAX_VALID_PROCESS_MEM_BYTES

def _extract_process_info(line: str) -> Optional[ProcessMemory]:
    if not isinstance(line, str): return None
    try:
        parts = [p.strip().strip("'\"") for p in line.split(",")]
        if len(parts) < 3: return None
        proc_name = str(parts[0]).strip()
        try:
            pid_val = int(parts[1])
        except (ValueError, TypeError):
            return None
        ws_val = _safe_int_conversion(parts[2])
        if len(parts) > 3 and parts[3].strip():
            path_check = Path(parts[3].strip())
            if is_protected_path(path_check):
                return None
        if _is_valid_process(proc_name, pid_val, ws_val):
            return ProcessMemory(proc_name, pid_val, ws_val)
    except (AttributeError, IndexError, ValueError):
        return None
    return None

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Procesa volcado CSV de memoria retornando los N procesos de mayor consumo."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text: return []
    lines = raw_csv_text.splitlines()
    if len(lines) < 2: return []
    def _gen() -> Iterator[ProcessMemory]:
        for line in lines[1:]:
            proc = _extract_process_info(line)
            if proc: yield proc
    return heapq.nlargest(limit, _gen())

def _read_windows_snapshot() -> MemorySnapshot:
    try:
        if WindowsAPI.kernel32 is None or not hasattr(WindowsAPI.kernel32, "GlobalMemoryStatusEx"): 
            return _EMPTY_SNAPSHOT
        mem_status = _create_mem_status_ex()
        if WindowsAPI.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_status)) != 0:
            if 0 < mem_status.ullTotalPhys < (1024**5 * 256): 
                if mem_status.ullAvailPhys <= mem_status.ullTotalPhys:
                    return MemorySnapshot(
                        total=BytesValue(mem_status.ullTotalPhys), 
                        available=BytesValue(mem_status.ullAvailPhys)
                    )
    except (AttributeError, ctypes.ArgumentError, OSError, ValueError, TypeError):
        pass
    return _EMPTY_SNAPSHOT

@lru_cache(maxsize=1)
def _get_cached_snapshot(timestamp_bucket: int) -> MemorySnapshot:
    if _is_windows: return _read_windows_snapshot()
    try:
        if _linux_mem_path.exists():
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8", errors="ignore"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
    except (OSError, PermissionError): 
        pass
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Retorna estado actual de memoria, con caché temporal de 5 segundos."""
    return _get_cached_snapshot(int(time.time() / 5))

def _get_process_handle(pid: int, access_mask: int) -> Optional[wintypes.HANDLE]:
    if WindowsAPI.kernel32 is None: return None
    try:
        handle = WindowsAPI.kernel32.OpenProcess(access_mask, False, pid)
        if not handle or handle == INVALID_HANDLE_VALUE: return None
        return wintypes.HANDLE(handle)
    except (ctypes.WinError, OSError): return None

GetModNameFunc = Callable[[wintypes.HANDLE, Any, ctypes.Array, int], int]

def _is_process_executable_safe(handle: wintypes.HANDLE, path_buffer: ctypes.Array, get_mod_name_func: GetModNameFunc) -> bool:
    """Valida que el ejecutable del proceso no se encuentre en rutas protegidas."""
    if not handle or handle == INVALID_HANDLE_VALUE:
        return False
    try:
        ctypes.memset(path_buffer, 0, ctypes.sizeof(path_buffer))
        result = get_mod_name_func(handle, None, path_buffer, len(path_buffer))
        if result == 0:
            return True # Si no podemos leer el nombre, asumimos inofensivo por ahora
        path_str = path_buffer.value.decode('utf-16', 'ignore') if isinstance(path_buffer, ctypes.Array) else ""
        if not path_str: return False
        return not is_protected_path(Path(path_str))
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return True
    return False

def _get_handle_for_safe_proc(pid: int, access: int, path_buf: ctypes.Array, get_mod_name_func: GetModNameFunc) -> Optional[wintypes.HANDLE]:
    handle = _get_process_handle(pid, access)
    if not handle: return None
    exit_code = wintypes.DWORD()
    try:
        if WindowsAPI.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0 or exit_code.value != STILL_ACTIVE_EXIT_CODE:
            WindowsAPI.kernel32.CloseHandle(handle)
            return None
        if _is_process_executable_safe(handle, path_buf, get_mod_name_func):
            return handle
    except (ctypes.ArgumentError, OSError):
        pass
    WindowsAPI.kernel32.CloseHandle(handle)
    return None

def _init_memory_counters() -> PROCESS_MEMORY_COUNTERS:
    """Inicializa la estructura de contadores de memoria nativa."""
    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    return pmc

def _iter_process_memory() -> Iterator[ProcessMemory]:
    if WindowsAPI.psapi is None or WindowsAPI.kernel32 is None: return
    pids = (wintypes.DWORD * 2048)()
    bytes_written = wintypes.DWORD(0)
    if not WindowsAPI.psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(bytes_written)):
        return
    
    pmc = _init_memory_counters()
    count = min(bytes_written.value // ctypes.sizeof(wintypes.DWORD), 2048)
    get_mod_name = WindowsAPI.psapi.GetModuleFileNameExW
    get_mem_info = WindowsAPI.psapi.GetProcessMemoryInfo
    path_buf = ctypes.create_unicode_buffer(1024)
    
    for pid in pids[:count]:
        if pid <= 0 or _is_system_process(pid): continue
        handle = _get_process_handle(pid, WinAccessMasks.PROCESS_QUERY_LIMITED_INFORMATION)
        if not handle: continue
        try:
            # Solo procesar si el proceso está activo y responde a la consulta
            if get_mem_info(handle, ctypes.byref(pmc), pmc.cb):
                if 0 < pmc.WorkingSetSize < MAX_VALID_PROCESS_MEM_BYTES:
                    if _is_process_executable_safe(handle, path_buf, get_mod_name):
                        val_name = path_buf.value
                        name = Path(val_name).name if val_name else f"PID-{pid}"
                        if not _is_invalid_process_name(name):
                            yield ProcessMemory(name, pid, BytesValue(pmc.WorkingSetSize))
        except (ctypes.ArgumentError, OSError, ValueError, RuntimeError, Exception):
            continue
        finally:
            WindowsAPI.kernel32.CloseHandle(handle)

def _get_process_snapshot(limit: int) -> List[ProcessMemory]:
    return heapq.nlargest(limit, _iter_process_memory())

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna lista de procesos top, con caché de 60 segundos."""
    global _CACHE_TOP_PROC
    if not _is_windows: return []
    now = time.time()
    if (now - _CACHE_TOP_PROC.timestamp) > 60:
        _CACHE_TOP_PROC = CacheEntry(now, _get_process_snapshot(limit))
    return _CACHE_TOP_PROC.data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica severidad de uso según porcentaje de RAM disponible."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera reporte textual de salud de memoria basado en snapshot actual."""
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
        report.extend(f"  Mayor consumo: {p.name} (PID {p.pid}) — {round(p.working_set / BYTES_IN_MB, 1)} MB" for p in processes[:3])
    return report

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Solicita la liberación del working set físico de un proceso tras validar permisos.
    Usa la API EmptyWorkingSet, que es una operación de bajo nivel del subsistema de memoria.
    """
    if not _is_windows or WindowsAPI.kernel32 is None or WindowsAPI.psapi is None:
        return False, "Operación no soportada."
    try: 
        target_pid = int(pid)
    except (ValueError, TypeError): 
        return False, "El PID debe ser un valor numérico válido."
    if target_pid <= 0 or _is_system_process(target_pid) or not hasattr(WindowsAPI.psapi, "EmptyWorkingSet"):
        return False, "Operación no permitida: PID protegido o función no disponible."
    
    path_buf = ctypes.create_unicode_buffer(1024)
    handle = _get_handle_for_safe_proc(target_pid, WinAccessMasks.TRIM_REQUIRED, path_buf, WindowsAPI.psapi.GetModuleFileNameExW)
    
    if handle is None:
        return False, "Acceso denegado o proceso inexistente/en zona restringida."
        
    try:
        exit_code = wintypes.DWORD()
        if WindowsAPI.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0 or exit_code.value != STILL_ACTIVE_EXIT_CODE:
            return False, "El proceso terminó antes de poder liberar su memoria."
            
        if WindowsAPI.psapi.EmptyWorkingSet(handle) != 0:
            return True, f"Working set liberado. {TRIM_WARNING}"
        return False, "El sistema operativo rechazó la solicitud de liberación."
    except (ctypes.ArgumentError, OSError, Exception):
        return False, "Error interno al intentar liberar memoria del proceso."
    finally:
        WindowsAPI.kernel32.CloseHandle(handle)
