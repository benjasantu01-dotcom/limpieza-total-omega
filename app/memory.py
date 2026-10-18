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
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, Final, Set, NewType, Any, NamedTuple, Iterator
from safety import is_protected_path

if TYPE_CHECKING:
    from ctypes import wintypes
else:
    class wintypes:
        HANDLE = ctypes.c_void_p

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
    """Contenedor de estado con marca de tiempo para cachear resultados de consultas costosas."""
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
    """Estructura Win32 GlobalMemoryStatusEx: refleja el estado global de la RAM física y paginación."""
    _fields_: List[Tuple[str, Any]] = [
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

class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    """Estructura Win32 PSAPI GetProcessMemoryInfo: métricas detalladas de consumo por proceso."""
    _fields_: List[Tuple[str, Any]] = [
        ("cb", ctypes.c_size_t),
        ("PageFaultCount", ctypes.c_ulong),
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
        """Retorna el porcentaje de memoria RAM en uso sobre el total."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Retorna el porcentaje de memoria RAM disponible sobre el total."""
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
        """Convierte el working set de bytes a megabytes para lectura humana."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

    def __lt__(self, other: ProcessMemory) -> bool:
        """Ordenamiento por tamaño de memoria (útil para heapq)."""
        return self.working_set < other.working_set

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte bytes a string legible (ej: 1024 -> '1.0 KB')."""
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
    """Instancia la estructura Win32 inicializando obligatoriamente el campo dwLength."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Optional[str], multiplier: int = 1) -> BytesValue:
    """Extrae números de strings y aplica un factor multiplicador, ignorando basura textual."""
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
    """Procesa el contenido de /proc/meminfo (Linux) extrayendo métricas globales."""
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
    """Identifica procesos críticos del kernel o el propio proceso de la app."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _extract_process_info(line: str) -> Optional[ProcessMemory]:
    """Valida y parsea una línea CSV para extraer datos de un proceso."""
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

def _get_top_n(processes: Iterator[ProcessMemory], limit: int) -> List[ProcessMemory]:
    """Retorna una lista ordenada con los N procesos de mayor consumo de memoria."""
    if not isinstance(limit, int) or limit < 0: return []
    return heapq.nlargest(limit, processes)

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Procesa un reporte CSV tabular de procesos y retorna los N más pesados."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text: 
        return []
    
    lines = raw_csv_text.splitlines()
    if len(lines) < 2:
        return []
    
    def _gen() -> Iterator[ProcessMemory]:
        for line in lines[1:]:
            if line:
                proc = _extract_process_info(line)
                if proc: yield proc
            
    return _get_top_n(_gen(), limit)

def _read_windows_snapshot() -> MemorySnapshot:
    """Ejecuta GlobalMemoryStatusEx via ctypes para obtener el estado real de RAM."""
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    if not kernel32 or not hasattr(kernel32, "GlobalMemoryStatusEx"): return _EMPTY_SNAPSHOT
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
    """Retorna un snapshot global utilizando caché basada en tiempo."""
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

def _is_process_accessible(pid: int, access_mask: int, kernel32: Any) -> Optional[int]:
    """
    Intenta obtener un handle Win32 al proceso solicitando el acceso especificado.
    Retorna el handle si es posible, o None si el acceso es denegado o el proceso es crítico.
    """
    if _is_system_process(pid): return None
    return kernel32.OpenProcess(access_mask, False, pid)

def _query_working_set_bytes(pid: int, handle: int, psapi: Any, pmc: PROCESS_MEMORY_COUNTERS) -> Optional[BytesValue]:
    """
    Consulta los contadores de memoria del proceso mediante PSAPI usando un handle abierto.
    Retorna BytesValue del working set o None si la consulta falla o es inválida.
    """
    try:
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), ctypes.c_uint32(pmc.cb)):
            val = pmc.WorkingSetSize
            return BytesValue(val) if 0 < val < MAX_VALID_PROCESS_MEM else None
    except (ctypes.WinError, OSError):
        return None
    return None

def _is_process_executable_safe(pid: int, handle: int) -> bool:
    """
    Verifica si el ejecutable asociado al proceso reside en una ruta segura.
    Retorna True si la ruta NO es protegida, False de lo contrario.
    """
    psapi = getattr(ctypes.windll, "psapi", None)
    if not psapi or not hasattr(psapi, "GetModuleFileNameExW"): return False
    
    buffer_path = ctypes.create_unicode_buffer(4096)
    try:
        length = psapi.GetModuleFileNameExW(handle, None, buffer_path, 4095)
        if length > 0:
            exe_str = buffer_path.value.strip()
            if not exe_str: return False
            executable_path = Path(os.path.abspath(exe_str))
            return executable_path.is_file() and not is_protected_path(executable_path)
    except (ctypes.ArgumentError, OSError, ValueError):
        pass
    return False

def _iter_process_memory(kernel32: Any, psapi: Any, pmc: PROCESS_MEMORY_COUNTERS) -> Iterator[ProcessMemory]:
    """Enumera procesos del sistema validando su seguridad al obtener métricas."""
    max_pids = 4096
    pids = (ctypes.c_uint32 * max_pids)()
    cb_needed = ctypes.c_uint32(0)
    
    if psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(cb_needed)):
        count = cb_needed.value // 4
        for i in range(count):
            pid_val = pids[i]
            if _is_system_process(pid_val) or pid_val <= 0: continue
            
            proc_handle = _is_process_accessible(pid_val, PROCESS_QUERY_LIMITED_INFORMATION, kernel32)
            if proc_handle:
                try:
                    if _is_process_executable_safe(pid_val, proc_handle):
                        ws = _query_working_set_bytes(pid_val, proc_handle, psapi, pmc)
                        if ws is not None:
                            yield ProcessMemory(f"PID {pid_val}", pid_val, ws)
                finally:
                    kernel32.CloseHandle(proc_handle)

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Obtiene los procesos más pesados tras validación de seguridad."""
    global _CACHE_TOP_PROC
    if not _is_windows: return []
    now = time.time()
    
    if (now - _CACHE_TOP_PROC.timestamp) > 60:
        kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        pmc = PROCESS_MEMORY_COUNTERS(cb=ctypes.sizeof(PROCESS_MEMORY_COUNTERS))
        
        candidates = _get_top_n(_iter_process_memory(kernel32, psapi, pmc), limit)
        _CACHE_TOP_PROC = CacheEntry(now, candidates)
            
    return _CACHE_TOP_PROC.data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el estrés de memoria basado en el porcentaje de RAM libre."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un reporte de diagnóstico textual basado en el estado actual de RAM."""
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
    """Solicita al SO forzar la liberación del working set tras validar seguridad."""
    if not _is_windows: return False, "Solo soportado en Windows."
    
    try: target_pid = int(pid)
    except (ValueError, TypeError): return False, "El PID debe ser un valor numérico."
        
    if _is_system_process(target_pid): return False, "Operación no permitida en procesos del sistema."
    
    psapi, kernel32 = ctypes.windll.psapi, ctypes.windll.kernel32
    proc_handle = kernel32.OpenProcess(TRIM_ACCESS_MASK, False, target_pid)
    
    if not proc_handle or proc_handle == INVALID_HANDLE_VALUE:
        return False, "Acceso denegado o proceso no encontrado."

    try:
        if not _is_process_executable_safe(target_pid, proc_handle):
            return False, "Proceso protegido o ubicación insegura."
            
        if psapi.EmptyWorkingSet(proc_handle) == 0:
            return False, "El sistema operativo rechazó la operación de limpieza."
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (ctypes.WinError, OSError):
        return False, "Error al intentar interactuar con el proceso."
    finally:
        kernel32.CloseHandle(proc_handle)
