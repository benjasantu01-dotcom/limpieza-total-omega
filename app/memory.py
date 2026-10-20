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
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, Final, Set, NewType, NamedTuple, Iterator, Any
from safety import is_protected_path

if TYPE_CHECKING:
    from ctypes import wintypes
else:
    class wintypes:
        HANDLE = ctypes.c_void_p
        DWORD = ctypes.c_ulong
        BOOL = ctypes.c_int

# Tipos semánticos para prevenir errores de lógica al operar con diferentes unidades.
BytesValue = NewType("BytesValue", int)
MegabytesValue = NewType("MegabytesValue", float)

# Constantes de conversión y límites de seguridad:
BYTES_IN_MB: Final[int] = 1024 * 1024
BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")
MAX_VALID_PROCESS_MEM: Final[int] = 128 * 1024 * BYTES_IN_MB 

# Máscaras de acceso Win32 para interactuar con el subsistema de procesos:
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x0400
TRIM_ACCESS_MASK: Final[int] = 0x00020000 | PROCESS_SET_QUOTA

FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x0400
DRIVE_FIXED: Final[int] = 3
INVALID_HANDLE_VALUE: Final[int] = -1

# PIDs reservados por el kernel de Windows que no deben ser consultados.
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}
ERROR_ACCESS_DENIED: Final[int] = 5

class CacheEntry(NamedTuple):
    """Contenedor temporal con timestamp para implementar caché basada en expiración."""
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
    "tiene que releer del disco. El número de 'RAM libre' sube, pero el "
    "rendimiento suele empeorar. Solo tiene sentido antes de medir algo "
    "puntual, no como mantenimiento."
)

class MEMORYSTATUSEX(ctypes.Structure):
    """Mapeo de la estructura Win32 GlobalMemoryStatusEx para reporte global de RAM."""
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
    """Mapeo de la estructura Win32 PSAPI para métricas detalladas de consumo por proceso."""
    _fields_: List[Tuple[str, Any]] = [
        ("cb", ctypes.c_size_t),
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
    """Representación inmutable del estado global de memoria del sistema."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula el uso neto restando el disponible del total."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Calcula el porcentaje de memoria RAM en uso sobre el total."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Calcula el porcentaje de memoria RAM disponible sobre el total."""
        if self.total <= 0: return 0.0
        return round((float(self.available) / float(self.total)) * 100, 1)

@dataclass
class ProcessMemory:
    """Metadatos de consumo de memoria de un proceso específico."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Retorna el working set convertido a Megabytes para visualización."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

    def __lt__(self, other: ProcessMemory) -> bool:
        """Criterio de ordenación basado en uso de memoria."""
        return self.working_set < other.working_set

def format_bytes(num: Optional[int | float]) -> str:
    """Formatea valores en bytes a strings legibles (ej: 1024 -> '1.0 KB')."""
    if not isinstance(num, (int, float)) or num < 0:
        return "0 B"
    if num == 0:
        return "0 B"
    try:
        idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
        val: float = num / (1024 ** idx)
        return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"
    except (ValueError, ZeroDivisionError, OverflowError):
        return "0 B"

def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """Crea una instancia de MEMORYSTATUSEX inicializando su tamaño obligatorio."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Optional[str], multiplier: int = 1) -> BytesValue:
    """Limpia strings numéricos y los escala, descartando caracteres no digitales."""
    if not isinstance(value, str): return BytesValue(0)
    digits = "".join(filter(str.isdigit, value))
    try:
        if not digits: return BytesValue(0)
        return BytesValue(int(digits) * multiplier)
    except (ValueError, OverflowError):
        return BytesValue(0)

_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))
_CACHE_TOP_PROC: CacheEntry = CacheEntry(0.0, [])

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Parsea el formato /proc/meminfo de Linux para extraer métricas de memoria."""
    if not isinstance(meminfo_text, str) or not meminfo_text:
        return _EMPTY_SNAPSHOT
        
    metrics: Dict[str, BytesValue] = {}
    for line in meminfo_text.splitlines():
        if ":" not in line: continue
        try:
            key, _, value_part = line.partition(":")
            key_stripped = key.strip()
            if not key_stripped: continue
            metrics[key_stripped] = _safe_int_conversion(value_part, 1024)
        except Exception:
            continue
            
    total: BytesValue = metrics.get("MemTotal", BytesValue(0))
    if total <= 0: return _EMPTY_SNAPSHOT
    
    avail_raw = metrics.get("MemAvailable", metrics.get("MemFree", BytesValue(0)))
    available = BytesValue(max(0, min(total, avail_raw)))
    cached = BytesValue(max(0, metrics.get("Cached", BytesValue(0))))
    
    return MemorySnapshot(total=total, available=available, cached=cached)

def _is_system_process(pid: int) -> bool:
    """Verifica si un PID corresponde al kernel, procesos críticos o a la propia app."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _extract_process_info(line: str) -> Optional[ProcessMemory]:
    """Valida y convierte una línea CSV de procesos en una instancia ProcessMemory."""
    if not isinstance(line, str) or not line.strip() or "," not in line: 
        return None
        
    parts = [p.strip().strip("'\"") for p in line.split(",")]
    if len(parts) < 3: 
        return None
    
    name, pid_str, ws_str = parts[0], parts[1], parts[2]
    try:
        pid = int(pid_str)
        ws = _safe_int_conversion(ws_str)
        
        if _is_system_process(pid) or pid <= 0:
            return None
        if not (0 < ws < MAX_VALID_PROCESS_MEM):
            return None
            
        return ProcessMemory(name, pid, BytesValue(ws))
    except (ValueError, TypeError):
        return None

def _filter_and_sort_processes(processes: Iterator[ProcessMemory], limit: int) -> List[ProcessMemory]:
    """Selecciona los procesos con mayor uso de memoria mediante HeapQ."""
    if not isinstance(limit, int) or limit < 0: return []
    return heapq.nlargest(limit, processes)

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Procesa reportes tabulares de procesos y retorna los N más demandantes."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text: 
        return []
    
    lines = raw_csv_text.splitlines()
    if len(lines) < 2:
        return []
    
    def _gen() -> Iterator[ProcessMemory]:
        for line in lines[1:]:
            if line and isinstance(line, str):
                proc = _extract_process_info(line)
                if proc: yield proc
            
    return _filter_and_sort_processes(_gen(), limit)

def _read_windows_snapshot() -> MemorySnapshot:
    """Consulta GlobalMemoryStatusEx vía ctypes para obtener el estado actual en Windows."""
    kernel32 = ctypes.windll.kernel32
    if not hasattr(kernel32, "GlobalMemoryStatusEx"): return _EMPTY_SNAPSHOT
    mem_status = _create_mem_status_ex()
    try:
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_status)) != 0:
            if mem_status.ullTotalPhys > 0 and mem_status.ullAvailPhys <= mem_status.ullTotalPhys:
                return MemorySnapshot(
                    total=BytesValue(mem_status.ullTotalPhys), 
                    available=BytesValue(mem_status.ullAvailPhys)
                )
    except (ctypes.ArgumentError, OSError, ValueError):
        pass
    return _EMPTY_SNAPSHOT

@lru_cache(maxsize=1)
def _get_cached_snapshot(timestamp_bucket: int) -> MemorySnapshot:
    """Implementa una capa de persistencia temporal para evitar lecturas de hardware frecuentes."""
    if _is_windows: return _read_windows_snapshot()
    try:
        if _linux_mem_path.exists():
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8", errors="ignore"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
    except (OSError, PermissionError): 
        pass
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Retorna el estado de memoria actual con una latencia de caché de 5 segundos."""
    return _get_cached_snapshot(int(time.time() / 5))

def _is_process_accessible(pid: int, access_mask: int, kernel32: ctypes.CDLL) -> Optional[wintypes.HANDLE]:
    """Intenta abrir un handle de proceso con privilegios específicos."""
    if _is_system_process(pid): return None
    handle = kernel32.OpenProcess(access_mask, False, pid)
    return handle if handle not in (0, INVALID_HANDLE_VALUE) else None

def _query_working_set_bytes(pid: int, handle: wintypes.HANDLE, psapi: ctypes.CDLL, pmc: PROCESS_MEMORY_COUNTERS) -> Optional[BytesValue]:
    """Consulta el valor actual de Working Set para un proceso mediante su handle."""
    try:
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), ctypes.c_uint32(pmc.cb)):
            val = pmc.WorkingSetSize
            return BytesValue(val) if 0 < val < MAX_VALID_PROCESS_MEM else None
    except (ctypes.WinError, OSError, ValueError):
        return None
    return None

def _is_process_executable_safe(pid: int, handle: wintypes.HANDLE, psapi: ctypes.CDLL) -> bool:
    """Valida que el archivo ejecutable asociado no pertenezca a rutas protegidas."""
    if not hasattr(psapi, "GetModuleFileNameExW"): return False
    
    buffer_path = ctypes.create_unicode_buffer(4096)
    length = psapi.GetModuleFileNameExW(handle, None, buffer_path, 4095)
    
    if length <= 0 or length > 4095:
        return False
        
    exe_str = buffer_path.value.strip()
    if not exe_str: 
        return False
        
    try:
        executable_path = Path(exe_str).absolute().resolve(strict=False)
        return not is_protected_path(executable_path)
    except (OSError, RuntimeError, ValueError, TypeError):
        return False

def _process_from_pid(pid: int, kernel32: ctypes.CDLL, psapi: ctypes.CDLL, pmc: PROCESS_MEMORY_COUNTERS) -> Optional[ProcessMemory]:
    """Obtiene y valida la seguridad de un proceso a partir de su PID."""
    proc_handle = _is_process_accessible(pid, PROCESS_QUERY_LIMITED_INFORMATION, kernel32)
    if proc_handle is None:
        return None
    
    try:
        ws = _query_working_set_bytes(pid, proc_handle, psapi, pmc)
        if ws is not None and ws > (10 * BYTES_IN_MB):
            if not _is_process_executable_safe(pid, proc_handle, psapi):
                return None
        return ProcessMemory(f"PID {pid}", pid, ws) if ws is not None else None
    except Exception:
        return None
    finally:
        if proc_handle is not None and proc_handle != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(proc_handle)

def _iter_process_memory(kernel32: ctypes.CDLL, psapi: ctypes.CDLL, pmc: PROCESS_MEMORY_COUNTERS) -> Iterator[ProcessMemory]:
    """Enumera procesos activos del sistema aplicando filtros de seguridad."""
    buf_size = 4096
    while buf_size <= 65536:
        pids = (wintypes.DWORD * (buf_size // 4))()
        cb_needed = wintypes.DWORD(0)
        if psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(cb_needed)):
            if cb_needed.value < ctypes.sizeof(pids):
                count = cb_needed.value // 4
                for i in range(count):
                    pid_val = int(pids[i])
                    if _is_system_process(pid_val) or pid_val <= 0: continue
                    proc = _process_from_pid(pid_val, kernel32, psapi, pmc)
                    if proc: yield proc
                break
            buf_size *= 2
        else:
            break

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna la lista de procesos con mayor consumo de memoria, aplicando validación."""
    global _CACHE_TOP_PROC
    if not _is_windows: return []
    now = time.time()
    
    if (now - _CACHE_TOP_PROC.timestamp) > 60:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        pmc = PROCESS_MEMORY_COUNTERS(cb=ctypes.sizeof(PROCESS_MEMORY_COUNTERS))
        
        candidates = _filter_and_sort_processes(_iter_process_memory(kernel32, psapi, pmc), limit)
        _CACHE_TOP_PROC = CacheEntry(now, candidates)
            
    return _CACHE_TOP_PROC.data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el estado de presión del sistema según el porcentaje de RAM disponible."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un reporte de texto con el diagnóstico actual del subsistema de memoria."""
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
        report.extend(f"  Mayor consumo: {p.name} (PID {p.pid}) — {p.working_set_mb} MB" for p in processes[:3])
        
    return report

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """Libera el working set de un proceso tras verificar que no sea un sistema crítico."""
    if not _is_windows: return False, "Solo soportado en Windows."
    
    try: 
        target_pid = int(pid)
    except (ValueError, TypeError): 
        return False, "El PID debe ser un valor numérico."
        
    if _is_system_process(target_pid): 
        return False, "Operación no permitida en procesos del sistema."
    
    psapi, kernel32 = ctypes.windll.psapi, ctypes.windll.kernel32
    proc_handle = _is_process_accessible(target_pid, TRIM_ACCESS_MASK, kernel32)
    
    if proc_handle is None:
        return False, "Acceso denegado o proceso no encontrado."

    try:
        if not _is_process_executable_safe(target_pid, proc_handle, psapi):
            return False, "Proceso protegido o ubicación insegura."
            
        if psapi.EmptyWorkingSet(proc_handle) == 0:
            return False, "El sistema operativo rechazó la operación de limpieza."
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (ctypes.WinError, OSError, Exception):
        return False, "Error inesperado al intentar interactuar con el proceso."
    finally:
        if proc_handle != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(proc_handle)
