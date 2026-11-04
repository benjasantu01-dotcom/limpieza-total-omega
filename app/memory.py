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
MegabytesValue = NewType("MegabytesValue", float)

BYTES_IN_MB: Final[int] = 1024 * 1024
BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")
MAX_VALID_PROCESS_MEM_BYTES: Final[int] = 128 * 1024 * BYTES_IN_MB 

# Máscaras de acceso para operaciones con procesos nativos en Windows
WIN32_PROCESS_QUERY_LIMITED: Final[int] = 0x1000
WIN32_PROCESS_SET_QUOTA: Final[int] = 0x0100
WIN32_PROCESS_QUERY_INFO: Final[int] = 0x0400
# Máscara combinada requerida para ejecutar la API EmptyWorkingSet
TRIM_ACCESS_MASK: Final[int] = 0x00200000 | WIN32_PROCESS_SET_QUOTA | WIN32_PROCESS_QUERY_INFO

INVALID_HANDLE_VALUE: Final[int] = -1
STILL_ACTIVE_EXIT_CODE: Final[int] = 259

SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}

class WindowsAPI:
    """Namespace contenedor para acceder a las librerías nativas Win32 de forma controlada."""
    kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
    psapi = ctypes.windll.psapi if os.name == "nt" else None

class CacheEntry(NamedTuple):
    """Caché temporal con timestamp para controlar la expiración de datos pesados."""
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
    """
    Estructura Win32 (GlobalMemoryStatusEx) utilizada por la API kernel32 para
    reportar información sobre el uso actual de memoria física y virtual.
    """
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
    """
    Estructura Win32 (PROCESS_MEMORY_COUNTERS) utilizada por PSAPI para
    retornar estadísticas de memoria de un proceso específico, incluyendo
    su Working Set actual y paginación.
    """
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
    """Representa una instantánea inmutable del estado global de memoria RAM."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula el uso neto: total menos disponible."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Calcula el porcentaje de memoria ocupada respecto al total."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Calcula el porcentaje de memoria disponible respecto al total."""
        if self.total <= 0: return 0.0
        return round((float(self.available) / float(self.total)) * 100, 1)

@dataclass
class ProcessMemory:
    """Metadatos de memoria y consumo de un proceso específico."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Retorna el Working Set normalizado a Megabytes."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

    def __lt__(self, other: ProcessMemory) -> bool:
        """Ordenamiento descendente por uso de memoria para facilitar listados 'top'."""
        return self.working_set < other.working_set

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte un valor numérico de bytes a una cadena amigable con unidad (ej. 1.5 GB)."""
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
    """Inicializa la estructura MEMORYSTATUSEX requerida por la API de Windows."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Any, multiplier: int = 1) -> BytesValue:
    """Convierte entradas heterogéneas a un valor de Bytes válido de forma defensiva."""
    if value is None: return BytesValue(0)
    
    if isinstance(value, (int, float)):
        try:
            val = int(value * multiplier)
            return BytesValue(max(0, val))
        except (ValueError, OverflowError):
            return BytesValue(0)
            
    if not isinstance(value, str): return BytesValue(0)
    
    digits = "".join(filter(str.isdigit, value))
    if not digits: return BytesValue(0)
    
    try:
        val = int(digits) * multiplier
        return BytesValue(val) if 0 <= val < (2**63 - 1) else BytesValue(0)
    except (ValueError, OverflowError, TypeError):
        return BytesValue(0)

_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))
_CACHE_TOP_PROC: CacheEntry = CacheEntry(0.0, [])

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Interpreta el contenido de /proc/meminfo de Linux. Función pura y testeable."""
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
    """Determina si un PID pertenece al núcleo del sistema o a nuestra propia app."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _is_invalid_process_name(name: str) -> bool:
    """Valida si el nombre del proceso contiene caracteres no permitidos o es de sistema."""
    return not name or any(ord(c) < 32 or ord(c) == 127 for c in name) or len(name) > 256

def _extract_process_info(line: str) -> Optional[ProcessMemory]:
    """Valida y parsea una línea CSV de procesos con verificaciones de seguridad."""
    if not isinstance(line, str) or not line.strip(): 
        return None
        
    parts = [p.strip().strip("'\"") for p in line.split(",")]
    if len(parts) < 3: 
        return None
    
    try:
        proc_name = str(parts[0]).strip()
        pid_val = int(parts[1])
        if _is_system_process(pid_val) or pid_val <= 0 or _is_invalid_process_name(proc_name):
            return None
        
        ws = _safe_int_conversion(parts[2])
        if not (0 < ws < MAX_VALID_PROCESS_MEM_BYTES):
            return None
            
        return ProcessMemory(proc_name, pid_val, ws)
    except (ValueError, TypeError, IndexError, OverflowError):
        return None

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Procesa el volcado CSV de procesos y retorna una lista filtrada y ordenada."""
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
    """Consulta la API GlobalMemoryStatusEx de Win32 para obtener métricas de hardware."""
    try:
        if WindowsAPI.kernel32 is None or not hasattr(WindowsAPI.kernel32, "GlobalMemoryStatusEx"): 
            return _EMPTY_SNAPSHOT
        
        mem_status = _create_mem_status_ex()
        if WindowsAPI.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_status)) != 0:
            if 0 < mem_status.ullTotalPhys < (1024 * 1024 * 1024 * 1024 * 256): 
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
    """Cachea la instantánea global para evitar sobrecarga en llamadas frecuentes."""
    if _is_windows: return _read_windows_snapshot()
    try:
        if _linux_mem_path.exists():
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8", errors="ignore"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
    except (OSError, PermissionError): 
        pass
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Obtiene el estado actual de la memoria del sistema."""
    return _get_cached_snapshot(int(time.time() / 5))

def _get_process_handle(pid: int, access_mask: int) -> Optional[wintypes.HANDLE]:
    """Solicita un handle de Win32 con permisos de acceso específicos."""
    if WindowsAPI.kernel32 is None: return None
    try:
        handle = WindowsAPI.kernel32.OpenProcess(access_mask, False, pid)
        if not handle or handle == INVALID_HANDLE_VALUE:
            return None
        return wintypes.HANDLE(handle)
    except (ctypes.WinError, OSError):
        return None

def _query_working_set_bytes(handle: wintypes.HANDLE, counters: PROCESS_MEMORY_COUNTERS) -> Optional[BytesValue]:
    """Consulta el WorkingSet de un proceso mediante PSAPI.GetProcessMemoryInfo."""
    if handle is None or WindowsAPI.psapi is None: return None
    try:
        if WindowsAPI.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), ctypes.sizeof(counters)):
            val = counters.WorkingSetSize
            return BytesValue(val) if 0 < val < MAX_VALID_PROCESS_MEM_BYTES else None
    except (ctypes.WinError, OSError):
        return None
    return None

def _is_process_executable_safe(handle: wintypes.HANDLE, path_buffer: ctypes.Array, get_mod_name_func: Callable) -> bool:
    """Verifica que el ejecutable del proceso no se encuentre en rutas protegidas."""
    if handle is None or get_mod_name_func is None: return False
    try:
        if get_mod_name_func(handle, None, path_buffer, len(path_buffer)) <= 0:
            return False 
        
        normalized_path = os.path.normpath(str(path_buffer.value))
        return not is_protected_path(Path(normalized_path))
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return False

def _get_handle_for_safe_proc(pid: int, access: int, path_buf: ctypes.Array, get_mod_name_func: Callable) -> Optional[wintypes.HANDLE]:
    """Gestiona de forma segura la obtención de un handle a un proceso externo."""
    try:
        handle = _get_process_handle(pid, access)
        if handle:
            exit_code = wintypes.DWORD()
            if WindowsAPI.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0 or exit_code.value != STILL_ACTIVE_EXIT_CODE:
                WindowsAPI.kernel32.CloseHandle(handle)
                return None
                
            if _is_process_executable_safe(handle, path_buf, get_mod_name_func):
                return handle
            WindowsAPI.kernel32.CloseHandle(handle)
    except (ctypes.WinError, OSError, Exception):
        pass
    return None

def _iter_process_memory() -> Iterator[ProcessMemory]:
    """Generador que recorre los procesos activos, reutilizando buffers para optimizar memoria."""
    if WindowsAPI.psapi is None or WindowsAPI.kernel32 is None: return
    
    pids = (wintypes.DWORD * 1024)()
    bytes_written = wintypes.DWORD(0)
    
    if not WindowsAPI.psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(bytes_written)):
        return
            
    pmc = PROCESS_MEMORY_COUNTERS(cb=ctypes.sizeof(PROCESS_MEMORY_COUNTERS))
    count = min(bytes_written.value // ctypes.sizeof(wintypes.DWORD), 1024)
    path_buf = ctypes.create_unicode_buffer(512)
    excluded_pids = SYSTEM_CRITICAL_PIDS | {os.getpid()}
    
    for i in range(count):
        pid = int(pids[i])
        if pid in excluded_pids or pid <= 0:
            continue
        
        handle = _get_handle_for_safe_proc(pid, WIN32_PROCESS_QUERY_LIMITED, path_buf, WindowsAPI.psapi.GetModuleFileNameExW)
        if not handle: continue
        
        try:
            if WindowsAPI.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), ctypes.sizeof(pmc)):
                ws = pmc.WorkingSetSize
                if 0 < ws < MAX_VALID_PROCESS_MEM_BYTES:
                    yield ProcessMemory(str(path_buf.value).split('\\')[-1], pid, BytesValue(ws))
        except (ctypes.WinError, OSError):
            continue
        finally:
            WindowsAPI.kernel32.CloseHandle(handle)

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna una lista ordenada de procesos según su consumo, con caché de 60 segundos."""
    global _CACHE_TOP_PROC
    if not _is_windows: return []
    now = time.time()
    
    if (now - _CACHE_TOP_PROC.timestamp) > 60:
        _CACHE_TOP_PROC = CacheEntry(now, heapq.nlargest(limit, _iter_process_memory()))
            
    return _CACHE_TOP_PROC.data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el estado de presión de memoria en niveles semánticos (ok, info, warning, danger)."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un informe descriptivo y clínico basado en la salud actual de la memoria."""
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
    """Intenta reducir manualmente el Working Set del proceso. Solo para casos específicos."""
    if not _is_windows or WindowsAPI.kernel32 is None or WindowsAPI.psapi is None:
        return False, "Operación no soportada o librería PSAPI no disponible."
    
    try: 
        target_pid = int(pid)
    except (ValueError, TypeError): 
        return False, "El PID debe ser un valor numérico."
        
    if _is_system_process(target_pid): 
        return False, "Operación no permitida en procesos del sistema."
    
    if not hasattr(WindowsAPI.psapi, "EmptyWorkingSet"):
        return False, "Función de sistema no disponible."
        
    path_buf = ctypes.create_unicode_buffer(1024)
    # Reutilizamos TRIM_ACCESS_MASK para asegurar permisos de set y query
    handle = _get_handle_for_safe_proc(target_pid, TRIM_ACCESS_MASK, path_buf, WindowsAPI.psapi.GetModuleFileNameExW)
    
    if not handle:
        return False, "Acceso denegado o proceso en zona restringida."

    # Seguridad defensiva adicional: re-verificar ruta protegida explícitamente tras normalización
    if is_protected_path(Path(os.path.normpath(path_buf.value))):
        WindowsAPI.kernel32.CloseHandle(handle)
        return False, "Operación denegada sobre archivos protegidos."

    try:
        process_exit_code = wintypes.DWORD()
        if WindowsAPI.kernel32.GetExitCodeProcess(handle, ctypes.byref(process_exit_code)) == 0:
            return False, "Error al consultar estado del proceso."
            
        if process_exit_code.value != STILL_ACTIVE_EXIT_CODE:
            return False, "El proceso ya ha finalizado."
            
        if WindowsAPI.psapi.EmptyWorkingSet(handle) != 0:
            return True, f"Working set liberado. {TRIM_WARNING}"
        else:
            return False, "El sistema operativo rechazó la operación de limpieza."
            
    except (ctypes.WinError, OSError, Exception):
        return False, "Error inesperado al comunicarse con el proceso."
    finally:
        WindowsAPI.kernel32.CloseHandle(handle)
