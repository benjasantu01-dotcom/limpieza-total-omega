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
        ULONGLONG = ctypes.c_ulonglong

# Alias de tipos para mejorar la legibilidad del dominio de memoria
BytesValue = NewType("BytesValue", int)
MegabytesValue = float

BYTES_IN_MB: Final[int] = 1024 * 1024
MB_DIVISOR: Final[float] = float(BYTES_IN_MB)
BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")
MAX_VALID_PROCESS_MEM_BYTES: Final[int] = 128 * 1024 * BYTES_IN_MB 

INVALID_HANDLE_VALUE: Final[int] = -1
STILL_ACTIVE_EXIT_CODE: Final[int] = 259
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}

def bytes_to_mb(bytes_val: int | float) -> float:
    """Convierte bytes crudos a megabytes con precisión de un decimal."""
    return round(float(bytes_val) / MB_DIVISOR, 1)

class WinAccessMasks:
    """Constantes de permisos para la Win32 API. Definen el nivel de acceso al abrir procesos."""
    PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
    PROCESS_QUERY_INFORMATION: Final[int] = 0x0400
    PROCESS_SET_QUOTA: Final[int] = 0x100
    # TRIM_REQUIRED combina acceso de consulta estándar con permisos de ajuste de cuota.
    TRIM_REQUIRED: Final[int] = 0x00200000 | PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION

class WindowsAPI:
    """Namespace que centraliza el acceso a las librerías dinámicas Win32 (kernel32.dll, psapi.dll)."""
    kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
    psapi = ctypes.windll.psapi if os.name == "nt" else None

class CacheEntry(NamedTuple):
    """Timestamp y datos asociados para gestionar la expiración de caché temporal."""
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
    """Estructura Win32 (GlobalMemoryStatusEx). Consulta métricas globales de memoria del SO."""
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
    """Estructura Win32 (GetProcessMemoryInfo). Contiene estadísticas de memoria del proceso."""
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
    """Representa un instante de la memoria física y virtual del sistema."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula los bytes en uso restando los disponibles del total."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Porcentaje de memoria física utilizada."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Porcentaje de memoria física libre."""
        if self.total <= 0: return 0.0
        return round((float(self.available) / float(self.total)) * 100, 1)

@dataclass
class ProcessMemory:
    """Representa el uso de memoria de un proceso individual."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> float:
        """Representación en MB del Working Set del proceso."""
        return bytes_to_mb(self.working_set)

    def __lt__(self, other: ProcessMemory) -> bool:
        """Ordena procesos por tamaño de Working Set."""
        return self.working_set < other.working_set

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte bytes a string legible, manejando errores y casos límite."""
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
    """Inicializa la estructura con el tamaño requerido por la API de Windows."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Any, multiplier: int = 1) -> BytesValue:
    """Convierte entradas a BytesValue, sanitizando desbordamientos y tipos."""
    if value is None: 
        return BytesValue(0)
    try:
        if isinstance(value, (int, float)):
            fval = float(value) * multiplier
            return BytesValue(int(fval)) if 0 <= fval < 2**63 - 1 else BytesValue(0)
        if isinstance(value, str):
            clean_digits = "".join(c for c in value if c.isdigit())
            return BytesValue(int(clean_digits) * multiplier) if clean_digits else BytesValue(0)
        return BytesValue(0)
    except (ValueError, OverflowError, TypeError):
        return BytesValue(0)

_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))
_CACHE_TOP_PROC: CacheEntry = CacheEntry(0.0, [])

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Parsea /proc/meminfo extrayendo métricas de memoria para entornos Linux."""
    if not isinstance(meminfo_text, str) or not meminfo_text:
        return _EMPTY_SNAPSHOT
    metrics: Dict[str, BytesValue] = {}
    for line in meminfo_text.splitlines():
        if ":" not in line: continue
        parts = line.split(":", 1)
        key = parts[0].strip()
        if key and len(parts) > 1:
            try:
                metrics[key] = _safe_int_conversion(parts[1].split()[0], 1024)
            except (ValueError, IndexError, TypeError):
                continue
    total = metrics.get("MemTotal", BytesValue(0))
    if total <= 0: return _EMPTY_SNAPSHOT
    avail_raw = metrics.get("MemAvailable", metrics.get("MemFree", BytesValue(0)))
    available = BytesValue(max(0, min(total, avail_raw)))
    cached = BytesValue(max(0, metrics.get("Cached", BytesValue(0))))
    return MemorySnapshot(total=total, available=available, cached=cached)

def _is_system_process(pid: int) -> bool:
    """Retorna True si el PID es crítico para el SO o la instancia actual."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _is_invalid_process_name(name: str) -> bool:
    """Retorna True si el nombre contiene caracteres no imprimibles o es inválido."""
    return not name or any(ord(c) < 32 or ord(c) == 127 for c in name) or len(name) > 256

def _is_valid_process(name: str, pid: int, ws_bytes: int) -> bool:
    """Valida parámetros de proceso contra restricciones de seguridad."""
    is_safe = not _is_system_process(pid) and pid > 0 and not _is_invalid_process_name(name)
    is_in_range = 0 < ws_bytes < MAX_VALID_PROCESS_MEM_BYTES
    return is_safe and is_in_range

def _extract_process_info(line: str) -> Optional[ProcessMemory]:
    """Extrae y valida datos de un proceso desde un CSV con re-validación de rutas."""
    if not isinstance(line, str) or not line.strip(): return None
    parts: List[str] = [p.strip().strip("'\"") for p in line.split(",")]
    if len(parts) < 3: return None
    try:
        pid_val, ws_val = int(parts[1]), _safe_int_conversion(parts[2])
        proc_name = parts[0]
        if len(parts) > 3:
            path_str = parts[3].strip()
            if path_str and len(path_str) < 1024 and is_protected_path(Path(path_str)):
                return None
        if _is_valid_process(proc_name, pid_val, ws_val):
            return ProcessMemory(proc_name, pid_val, ws_val)
    except (ValueError, TypeError, RuntimeError):
        return None
    return None

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Procesa un volcado de procesos y devuelve los N que consumen más memoria."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text: return []
    lines = raw_csv_text.splitlines()
    if len(lines) < 2: return []
    def _gen() -> Iterator[ProcessMemory]:
        for line in lines[1:]:
            proc = _extract_process_info(line)
            if proc: yield proc
    return heapq.nlargest(limit, _gen())

def _read_windows_snapshot() -> MemorySnapshot:
    """Captura estado global de RAM mediante GlobalMemoryStatusEx."""
    try:
        if WindowsAPI.kernel32 is None or not hasattr(WindowsAPI.kernel32, "GlobalMemoryStatusEx"): 
            return _EMPTY_SNAPSHOT
        mem_status = _create_mem_status_ex()
        if WindowsAPI.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_status)) != 0:
            if 0 < mem_status.ullTotalPhys < (1024**5 * 256): 
                return MemorySnapshot(
                    total=BytesValue(mem_status.ullTotalPhys), 
                    available=BytesValue(mem_status.ullAvailPhys)
                )
    except (AttributeError, ctypes.ArgumentError, OSError, ValueError, TypeError):
        pass
    return _EMPTY_SNAPSHOT

@lru_cache(maxsize=1)
def _get_cached_snapshot(timestamp_bucket: int) -> MemorySnapshot:
    """Gestor de caché para las métricas globales."""
    if _is_windows: return _read_windows_snapshot()
    try:
        if _linux_mem_path.exists():
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8", errors="ignore"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
    except (OSError, PermissionError): pass
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Obtiene una vista actual de la memoria (con 5s de frescura)."""
    return _get_cached_snapshot(int(time.time() / 5))

def _is_path_safe(path_str: str) -> bool:
    """Valida si la ruta está fuera de zonas protegidas."""
    if not path_str or len(path_str) > 1024: return False
    return not is_protected_path(Path(path_str))

def _get_validated_process_handle(pid: int, access_mask: int, path_buf: ctypes.Array[ctypes.c_wchar]) -> Optional[wintypes.HANDLE]:
    """Obtiene un handle de proceso tras verificar que no reside en zonas protegidas."""
    if WindowsAPI.kernel32 is None or WindowsAPI.psapi is None: return None
    handle = WindowsAPI.kernel32.OpenProcess(access_mask, False, pid)
    if not handle or handle == INVALID_HANDLE_VALUE: return None
    try:
        if WindowsAPI.psapi.GetModuleFileNameExW(handle, None, path_buf, 1024) > 0:
            if path_buf.value and _is_path_safe(path_buf.value):
                return wintypes.HANDLE(handle)
    except (ctypes.WinError, OSError, AttributeError): pass
    WindowsAPI.kernel32.CloseHandle(handle)
    return None

def _get_process_memory_counters(handle: wintypes.HANDLE, pmc: PROCESS_MEMORY_COUNTERS) -> Optional[int]:
    """Extrae contadores de memoria validando el tamaño del Working Set."""
    if WindowsAPI.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
        if 0 < pmc.WorkingSetSize < MAX_VALID_PROCESS_MEM_BYTES:
            return pmc.WorkingSetSize
    return None

def _iter_process_memory() -> Iterator[ProcessMemory]:
    """Iterador que escanea procesos del sistema con validación de seguridad."""
    if WindowsAPI.psapi is None or WindowsAPI.kernel32 is None: return
    MAX_PIDS = 2048
    pids = (wintypes.DWORD * MAX_PIDS)()
    bytes_written = wintypes.DWORD(0)
    if not WindowsAPI.psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(bytes_written)):
        return
    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    path_buf = ctypes.create_unicode_buffer(1024)
    count: int = min(bytes_written.value // ctypes.sizeof(wintypes.DWORD), MAX_PIDS)
    for i in range(count):
        pid = int(pids[i])
        if pid <= 0 or _is_system_process(pid): continue
        handle = _get_validated_process_handle(pid, WinAccessMasks.PROCESS_QUERY_LIMITED_INFORMATION, path_buf)
        if not handle: continue
        try:
            ws_size = _get_process_memory_counters(handle, pmc)
            if ws_size:
                yield ProcessMemory(f"PID-{pid}", pid, BytesValue(ws_size))
        finally:
            WindowsAPI.kernel32.CloseHandle(handle)

def _get_process_snapshot(limit: int) -> List[ProcessMemory]:
    """Devuelve ranking de procesos con mayor consumo."""
    return heapq.nlargest(limit, _iter_process_memory())

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna ranking de procesos (caché 60s) para mitigar carga de CPU."""
    global _CACHE_TOP_PROC
    if not _is_windows: return []
    now = time.time()
    if (now - _CACHE_TOP_PROC.timestamp) > 60:
        _CACHE_TOP_PROC = CacheEntry(now, _get_process_snapshot(limit))
    return _CACHE_TOP_PROC.data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica severidad de uso de RAM por porcentaje."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera diagnóstico textual de salud del sistema."""
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
    """Solicita al SO la reducción de memoria física del proceso."""
    if not _is_windows or WindowsAPI.kernel32 is None or WindowsAPI.psapi is None:
        return False, "Operación no soportada."
    try: target_pid = int(pid)
    except (ValueError, TypeError): return False, "El PID debe ser numérico."
    if _is_system_process(target_pid):
        return False, "Proceso protegido por el sistema."
    path_buf = ctypes.create_unicode_buffer(1024)
    handle = _get_validated_process_handle(target_pid, WinAccessMasks.TRIM_REQUIRED, path_buf)
    if not handle:
        return False, "Acceso denegado o proceso protegido."
    try:
        if not _is_path_safe(path_buf.value):
            return False, "Seguridad: El proceso se encuentra en zona restringida."
        if WindowsAPI.psapi.EmptyWorkingSet(handle) != 0:
            return True, f"Working set liberado. {TRIM_WARNING}"
        return False, "El sistema operativo rechazó la solicitud."
    except (ctypes.WinError, OSError, Exception):
        return False, "Error interno al ejecutar la operación."
    finally:
        WindowsAPI.kernel32.CloseHandle(handle)
