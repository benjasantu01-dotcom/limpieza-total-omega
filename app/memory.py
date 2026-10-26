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

# Tipos semánticos para prevenir errores de lógica al operar con diferentes unidades.
BytesValue = NewType("BytesValue", int)
MegabytesValue = NewType("MegabytesValue", float)

# Constantes de conversión y límites de seguridad:
BYTES_IN_MB: Final[int] = 1024 * 1024
BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")
MAX_VALID_PROCESS_MEM_BYTES: Final[int] = 128 * 1024 * BYTES_IN_MB 

# Máscaras de acceso Win32:
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x0400
PROCESS_QUERY_INFORMATION: Final[int] = 0x0400
TRIM_ACCESS_MASK: Final[int] = 0x020000 | PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION

FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x0400
INVALID_HANDLE_VALUE: Final[int] = -1
STILL_ACTIVE_EXIT_CODE: Final[int] = 259

# PIDs reservados por el kernel de Windows que no deben ser consultados.
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}

class WindowsAPI:
    """Namespace contenedor para acceder a las librerías Win32 de forma controlada."""
    kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
    psapi = ctypes.windll.psapi if os.name == "nt" else None

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
    """Estructura de datos para la API GlobalMemoryStatusEx de Windows."""
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
    """Estructura utilizada para recibir estadísticas de memoria de un proceso mediante GetProcessMemoryInfo."""
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
    """Representación inmutable del estado global de memoria del sistema."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula el uso neto: total menos disponible (estimado)."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Porcentaje de memoria RAM en uso respecto al total."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Porcentaje de memoria RAM disponible respecto al total."""
        if self.total <= 0: return 0.0
        return round((float(self.available) / float(self.total)) * 100, 1)

@dataclass
class ProcessMemory:
    """Datos de consumo de memoria y metadatos de un proceso activo."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Retorna el working set convertido a Megabytes."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

    def __lt__(self, other: ProcessMemory) -> bool:
        """Permite ordenar instancias de ProcessMemory por uso de memoria."""
        return self.working_set < other.working_set

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte bytes a string legible (ej. '10.5 MB') usando escala decimal."""
    if not isinstance(num, (int, float)) or math.isnan(num) or num < 0:
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
    """Inicializa la estructura Win32 con el tamaño requerido por el sistema para GlobalMemoryStatusEx."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Any, multiplier: int = 1) -> BytesValue:
    """Normaliza inputs crudos (str/int) a BytesValue aplicando un multiplicador de escala."""
    if isinstance(value, int): return BytesValue(max(0, value * multiplier))
    if not isinstance(value, str): return BytesValue(0)
    
    digits = "".join(filter(str.isdigit, value))
    if not digits: return BytesValue(0)
    try:
        val = int(digits) * multiplier
        return BytesValue(val) if val >= 0 else BytesValue(0)
    except (ValueError, OverflowError, TypeError):
        return BytesValue(0)

_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))
_CACHE_TOP_PROC: CacheEntry = CacheEntry(0.0, [])

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """
    Parsea el contenido de /proc/meminfo. 
    Extrae valores clave y los normaliza a bytes mediante _safe_int_conversion.
    """
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
    """Retorna True si el PID corresponde al kernel o al proceso actual."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _extract_process_info(line: str) -> Optional[ProcessMemory]:
    """
    Parsea una línea de texto crudo de procesos (CSV format).
    Valida que el PID sea legítimo y que el consumo de memoria esté dentro de rangos lógicos.
    """
    if not isinstance(line, str) or not line.strip() or "," not in line: 
        return None
        
    parts = [p.strip().strip("'\"") for p in line.split(",")]
    if len(parts) < 3: 
        return None
    
    try:
        name = parts[0]
        if not name: return None
        pid = int(parts[-2])
        ws = _safe_int_conversion(parts[-1])
        
        if _is_system_process(pid) or pid <= 0:
            return None
        if not (0 < ws < MAX_VALID_PROCESS_MEM_BYTES):
            return None
            
        return ProcessMemory(name, pid, ws)
    except (ValueError, TypeError, IndexError):
        return None

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Toma un volcado de procesos (str), lo procesa línea a línea y retorna el top N."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text: 
        return []
    
    lines = raw_csv_text.splitlines()
    if len(lines) < 2:
        return []
    
    def _gen() -> Iterator[ProcessMemory]:
        for line in lines[1:]:
            proc = _extract_process_info(line)
            if proc: 
                yield proc
            
    return heapq.nlargest(limit, _gen())

def _read_windows_snapshot() -> MemorySnapshot:
    """Consulta la API GlobalMemoryStatusEx de Windows mediante FFI."""
    try:
        if WindowsAPI.kernel32 is None or not hasattr(WindowsAPI.kernel32, "GlobalMemoryStatusEx"): 
            return _EMPTY_SNAPSHOT
        
        mem_status = _create_mem_status_ex()
        if WindowsAPI.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_status)) != 0:
            if 0 < mem_status.ullTotalPhys and mem_status.ullAvailPhys <= mem_status.ullTotalPhys:
                return MemorySnapshot(
                    total=BytesValue(mem_status.ullTotalPhys), 
                    available=BytesValue(mem_status.ullAvailPhys)
                )
    except (AttributeError, ctypes.ArgumentError, OSError, ValueError, TypeError):
        pass
    return _EMPTY_SNAPSHOT

@lru_cache(maxsize=1)
def _get_cached_snapshot(timestamp_bucket: int) -> MemorySnapshot:
    """Caché temporal interna que alterna entre lectura de archivos (Linux) o API (Win)."""
    if _is_windows: return _read_windows_snapshot()
    try:
        if _linux_mem_path.exists():
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8", errors="ignore"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
    except (OSError, PermissionError): 
        pass
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Retorna el estado global de memoria con granularidad de 5 segundos."""
    return _get_cached_snapshot(int(time.time() / 5))

def _is_process_accessible(pid: int, access_mask: int) -> Optional[wintypes.HANDLE]:
    """
    Solicita un handle de proceso con privilegios específicos.
    Retorna el handle como un objeto ctypes o None si el acceso es denegado o el proceso no existe.
    """
    try:
        handle = WindowsAPI.kernel32.OpenProcess(access_mask, False, pid)
        if not handle or handle == INVALID_HANDLE_VALUE:
            return None
        return wintypes.HANDLE(handle)
    except (ctypes.WinError, OSError):
        return None

def _query_working_set_bytes(h_process: wintypes.HANDLE, pmc: PROCESS_MEMORY_COUNTERS) -> Optional[BytesValue]:
    """
    Invoca la API de Windows GetProcessMemoryInfo para extraer el WorkingSet actual de un proceso.
    Recibe un handle abierto y una estructura pre-inicializada como buffer.
    """
    if h_process is None: return None
    try:
        if WindowsAPI.psapi.GetProcessMemoryInfo(h_process, ctypes.byref(pmc), ctypes.c_uint32(pmc.cb)):
            val = pmc.WorkingSetSize
            return BytesValue(val) if 0 < val < MAX_VALID_PROCESS_MEM_BYTES else None
    except (ctypes.WinError, OSError, ValueError):
        return None
    return None

def _is_process_executable_safe(h_process: wintypes.HANDLE) -> bool:
    """Verifica si el ejecutable reside en zonas protegidas mediante seguridad defensiva."""
    if h_process is None: return False
    
    buffer_path = ctypes.create_unicode_buffer(4096)
    try:
        if WindowsAPI.psapi.GetModuleFileNameExW(h_process, None, buffer_path, 4096) <= 0:
            return False
        
        exe_path_str = buffer_path.value.strip()
        if not exe_path_str: return False
        
        exe_path = Path(exe_path_str).absolute()
        if not exe_path.exists(): return False
        
        if is_protected_path(exe_path): return False
            
        return True
    except (OSError, RuntimeError, ValueError, TypeError):
        return False

def _iter_process_memory(pmc: PROCESS_MEMORY_COUNTERS) -> Iterator[ProcessMemory]:
    """Enumera procesos mediante EnumProcesses y filtra aquellos significativos."""
    MAX_PIDS = 4096
    pids = (wintypes.DWORD * MAX_PIDS)()
    cb_needed = wintypes.DWORD(0)
    
    if not WindowsAPI.psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(cb_needed)):
        return
            
    count = cb_needed.value // ctypes.sizeof(wintypes.DWORD)
    for i in range(count):
        pid = int(pids[i])
        if _is_system_process(pid): continue
            
        h_process = _is_process_accessible(pid, PROCESS_QUERY_LIMITED_INFORMATION)
        if not h_process: continue
        
        try:
            ws = _query_working_set_bytes(h_process, pmc)
            # Filtro de peso primero: es más barato que GetModuleFileNameExW y validaciones de ruta
            if ws is not None and ws > (10 * BYTES_IN_MB):
                if _is_process_executable_safe(h_process):
                    yield ProcessMemory(f"PID {pid}", pid, ws)
        except Exception:
            continue
        finally:
            WindowsAPI.kernel32.CloseHandle(h_process)

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna los procesos más pesados. Implementa caché de 60 segundos."""
    global _CACHE_TOP_PROC
    if not _is_windows: return []
    now = time.time()
    
    if (now - _CACHE_TOP_PROC.timestamp) > 60:
        try:
            pmc = PROCESS_MEMORY_COUNTERS(cb=ctypes.sizeof(PROCESS_MEMORY_COUNTERS))
            candidates = heapq.nlargest(limit, _iter_process_memory(pmc))
            _CACHE_TOP_PROC = CacheEntry(now, candidates)
        except (AttributeError, OSError):
            pass
            
    return _CACHE_TOP_PROC.data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el estado de presión de RAM como 'ok', 'info', 'warning' o 'danger'."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un reporte textual de salud basado en el snapshot y procesos pesados."""
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
    """
    Solicita al SO liberar RAM de un proceso específico (EmptyWorkingSet).
    Verifica seguridad del ejecutable y estado del proceso antes de actuar.
    """
    if not _is_windows: return False, "Solo soportado en Windows."
    
    try: 
        target_pid = int(pid)
    except (ValueError, TypeError): 
        return False, "El PID debe ser un valor numérico."
        
    if _is_system_process(target_pid): 
        return False, "Operación no permitida en procesos del sistema."
    
    try:
        h_process = _is_process_accessible(target_pid, TRIM_ACCESS_MASK)
        if not h_process:
            return False, "Acceso denegado o proceso no encontrado."

        try:
            exit_code = wintypes.DWORD()
            if WindowsAPI.kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code)) == 0:
                return False, "No se pudo verificar el estado del proceso."
                
            if exit_code.value != STILL_ACTIVE_EXIT_CODE:
                return False, "El proceso no está activo."
            
            if not _is_process_executable_safe(h_process):
                return False, "Proceso protegido o ubicación insegura."
                
            if WindowsAPI.psapi.EmptyWorkingSet(h_process) == 0:
                return False, "El sistema operativo rechazó la operación de limpieza."
            return True, f"Working set liberado. {TRIM_WARNING}"
        finally:
            WindowsAPI.kernel32.CloseHandle(h_process)
    except (ctypes.WinError, OSError, Exception):
        return False, "Error inesperado al intentar interactuar con el proceso."
