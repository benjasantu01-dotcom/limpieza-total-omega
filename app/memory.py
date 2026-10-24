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
        PDWORD = ctypes.POINTER(ctypes.c_ulong)

# Tipos semánticos para prevenir errores de lógica al operar con diferentes unidades.
BytesValue = NewType("BytesValue", int)
MegabytesValue = NewType("MegabytesValue", float)

# Constantes de conversión y límites de seguridad:
BYTES_IN_MB: Final[int] = 1024 * 1024
BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")
MAX_VALID_PROCESS_MEM_BYTES: Final[int] = 128 * 1024 * BYTES_IN_MB 

# Máscaras de acceso Win32 para interactuar con el subsistema de procesos:
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x0400
PROCESS_QUERY_INFORMATION: Final[int] = 0x0400
TRIM_ACCESS_MASK: Final[int] = 0x00020000 | PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION

FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x0400
INVALID_HANDLE_VALUE: Final[int] = -1
STILL_ACTIVE_EXIT_CODE: Final[int] = 259

# PIDs reservados por el kernel de Windows que no deben ser consultados.
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}

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
    """Estructura de datos para la API GetProcessMemoryInfo de Windows."""
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
    """Convierte un valor numérico en bytes a formato de cadena legible."""
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
    """Instancia y prepara la estructura de memoria con dwLength requerido por Win32."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Optional[str | int], multiplier: int = 1) -> BytesValue:
    """Extrae dígitos de una cadena o normaliza enteros, aplicando factor de escala."""
    if isinstance(value, int): return BytesValue(max(0, value * multiplier))
    if not isinstance(value, str): return BytesValue(0)
    
    digits = "".join(filter(str.isdigit, value))
    if not digits: return BytesValue(0)
    try:
        val = int(digits) * multiplier
        return BytesValue(val) if val >= 0 else BytesValue(0)
    except (ValueError, OverflowError):
        return BytesValue(0)

_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))
_CACHE_TOP_PROC: CacheEntry = CacheEntry(0.0, [])

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Parser puro: convierte el volcado de /proc/meminfo a un MemorySnapshot."""
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
    """Verifica si el PID es crítico para el SO o corresponde a la instancia actual."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _extract_process_info(line: str) -> Optional[ProcessMemory]:
    """Parser puro: extrae información de línea CSV; valida existencia del proceso."""
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
    """Parser puro: procesa volcado de procesos (CSV) y retorna top procesos."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text: 
        return []
    
    lines = raw_csv_text.splitlines()
    if len(lines) < 2:
        return []
    
    # Procesamiento eficiente mediante generador para evitar listas intermedias masivas
    def _gen() -> Iterator[ProcessMemory]:
        for line in lines[1:]:
            proc = _extract_process_info(line)
            if proc: 
                yield proc
            
    return heapq.nlargest(limit, _gen())

def _read_windows_snapshot() -> MemorySnapshot:
    """Consulta la API GlobalMemoryStatusEx mediante ctypes (Win32)."""
    try:
        if not hasattr(ctypes, "windll"): return _EMPTY_SNAPSHOT
        kernel32 = ctypes.windll.kernel32
        if not hasattr(kernel32, "GlobalMemoryStatusEx"): return _EMPTY_SNAPSHOT
        mem_status = _create_mem_status_ex()
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_status)) != 0:
            if mem_status.ullTotalPhys > 0 and mem_status.ullAvailPhys <= mem_status.ullTotalPhys:
                return MemorySnapshot(
                    total=BytesValue(mem_status.ullTotalPhys), 
                    available=BytesValue(mem_status.ullAvailPhys)
                )
    except (AttributeError, ctypes.ArgumentError, OSError, ValueError):
        pass
    return _EMPTY_SNAPSHOT

@lru_cache(maxsize=1)
def _get_cached_snapshot(timestamp_bucket: int) -> MemorySnapshot:
    """Mantiene en caché el estado de memoria con latencia definida (Time-Bucket)."""
    if _is_windows: return _read_windows_snapshot()
    try:
        if _linux_mem_path.exists():
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8", errors="ignore"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
    except (OSError, PermissionError): 
        pass
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Obtiene el estado de memoria actual con una caché de 5 segundos."""
    return _get_cached_snapshot(int(time.time() / 5))

def _is_process_accessible(pid: int, access_mask: int, kernel32: ctypes.CDLL) -> Optional[wintypes.HANDLE]:
    """Intenta abrir un proceso con permisos específicos mediante OpenProcess."""
    handle = kernel32.OpenProcess(access_mask, False, pid)
    return handle if handle is not None and handle != 0 and handle != INVALID_HANDLE_VALUE else None

def _query_working_set_bytes(pid: int, handle: wintypes.HANDLE, psapi: ctypes.CDLL, pmc: PROCESS_MEMORY_COUNTERS) -> Optional[BytesValue]:
    """Consulta consumo real del proceso llamando a GetProcessMemoryInfo."""
    try:
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), ctypes.c_uint32(pmc.cb)):
            val = pmc.WorkingSetSize
            return BytesValue(val) if 0 < val < MAX_VALID_PROCESS_MEM_BYTES else None
    except (ctypes.WinError, OSError, ValueError):
        return None
    return None

def _is_process_executable_safe(pid: int, handle: wintypes.HANDLE, psapi: ctypes.CDLL) -> bool:
    """Verifica que el ejecutable del proceso resida en una ruta autorizada."""
    if not handle or not hasattr(psapi, "GetModuleFileNameExW"): 
        return False
    
    buffer_path = ctypes.create_unicode_buffer(4096)
    try:
        if psapi.GetModuleFileNameExW(handle, None, buffer_path, 4096) <= 0:
            return False
    except (ctypes.WinError, OSError):
        return False
    
    exe_str = buffer_path.value.strip()
    if not exe_str: 
        return False
        
    try:
        # Resolver ruta canónica para evitar ataques mediante enlaces simbólicos
        executable_path = Path(os.path.realpath(exe_str))
        if not executable_path.exists(): 
            return False
            
        # Validación de puntos de reparse (junctions/symlinks detectados por stat)
        file_stat = executable_path.stat()
        if (file_stat.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT):
            return False
            
        return not is_protected_path(executable_path)
    except (OSError, AttributeError, RuntimeError, ValueError, TypeError):
        return False

def _process_from_pid(pid: int, kernel32: ctypes.CDLL, psapi: ctypes.CDLL, pmc: PROCESS_MEMORY_COUNTERS) -> Optional[ProcessMemory]:
    """Crea instancia ProcessMemory verificando seguridad y accesibilidad del binario."""
    if _is_system_process(pid):
        return None

    proc_handle = _is_process_accessible(pid, PROCESS_QUERY_LIMITED_INFORMATION, kernel32)
    if not proc_handle:
        return None
    
    try:
        ws = _query_working_set_bytes(pid, proc_handle, psapi, pmc)
        if ws is None or ws <= (10 * BYTES_IN_MB):
            return None
            
        if not _is_process_executable_safe(pid, proc_handle, psapi):
            return None
            
        return ProcessMemory(f"PID {pid}", pid, ws)
    except (ctypes.WinError, OSError, ValueError, RuntimeError):
        return None
    finally:
        kernel32.CloseHandle(proc_handle)

def _iter_process_memory(kernel32: ctypes.CDLL, psapi: ctypes.CDLL, pmc: PROCESS_MEMORY_COUNTERS) -> Iterator[ProcessMemory]:
    """Enumera procesos activos del sistema aplicando filtros de seguridad."""
    MAX_PIDS = 4096
    pids = (wintypes.DWORD * MAX_PIDS)()
    cb_needed = wintypes.DWORD(0)
    
    try:
        if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(cb_needed)):
            return
            
        count = min(cb_needed.value // ctypes.sizeof(wintypes.DWORD), MAX_PIDS)
        for i in range(count):
            proc = _process_from_pid(int(pids[i]), kernel32, psapi, pmc)
            if proc:
                yield proc
    except Exception:
        pass

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna top procesos por consumo de RAM (caché: 60s)."""
    global _CACHE_TOP_PROC
    if not _is_windows: return []
    now = time.time()
    
    if (now - _CACHE_TOP_PROC.timestamp) > 60:
        try:
            if not hasattr(ctypes, "windll"): return []
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            pmc = PROCESS_MEMORY_COUNTERS(cb=ctypes.sizeof(PROCESS_MEMORY_COUNTERS))
            # Uso eficiente de heap para obtener los N elementos más pesados directamente
            candidates = heapq.nlargest(limit, _iter_process_memory(kernel32, psapi, pmc))
            _CACHE_TOP_PROC = CacheEntry(now, candidates)
        except (AttributeError, OSError):
            pass
            
    return _CACHE_TOP_PROC.data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el estado de presión de la RAM según el porcentaje disponible."""
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
        report.extend(f"  Mayor consumo: {p.name} (PID {p.pid}) — {p.working_set_mb} MB" for p in processes[:3])
        
    return report

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Intenta liberar el 'working set' de un proceso específico (Acción manual).
    Requiere procesos activos y permisos de escritura en la cuota del proceso.
    """
    if not _is_windows: return False, "Solo soportado en Windows."
    
    try: 
        target_pid = int(pid)
    except (ValueError, TypeError): 
        return False, "El PID debe ser un valor numérico."
        
    if _is_system_process(target_pid): 
        return False, "Operación no permitida en procesos del sistema."
    
    try:
        psapi, kernel32 = ctypes.windll.psapi, ctypes.windll.kernel32
    except AttributeError:
        return False, "No se pudo acceder a las APIs de Windows."
        
    proc_handle = _is_process_accessible(target_pid, TRIM_ACCESS_MASK, kernel32)
    if proc_handle is None:
        return False, "Acceso denegado o proceso no encontrado."

    try:
        exit_code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(exit_code)) == 0 or exit_code.value != STILL_ACTIVE_EXIT_CODE:
            return False, "El proceso no está activo."
        
        if not _is_process_executable_safe(target_pid, proc_handle, psapi):
            return False, "Proceso protegido o ubicación insegura."
            
        if psapi.EmptyWorkingSet(proc_handle) == 0:
            return False, "El sistema operativo rechazó la operación de limpieza."
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (ctypes.WinError, OSError, Exception):
        return False, "Error inesperado al intentar interactuar con el proceso."
    finally:
        if proc_handle is not None and proc_handle != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(proc_handle)
