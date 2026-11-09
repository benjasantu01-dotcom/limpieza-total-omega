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
# Límite heurístico para filtrar procesos anómalos o ruido en mediciones.
MAX_VALID_PROCESS_MEM_BYTES: Final[int] = 128 * 1024 * BYTES_IN_MB 

INVALID_HANDLE_VALUE: Final[int] = -1
STILL_ACTIVE_EXIT_CODE: Final[int] = 259
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}

class WindowsAPI:
    """
    Namespace que centraliza el acceso a librerías nativas Win32.
    Define máscaras de acceso y métodos de API para operaciones de memoria.
    """
    kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
    psapi = ctypes.windll.psapi if os.name == "nt" else None
    
    # Máscaras de acceso para OpenProcess
    QUERY_LIMITED: Final[int] = 0x1000  # Acceso restringido para solo lectura
    QUERY_INFO: Final[int] = 0x0400     # Acceso a información del proceso
    SET_QUOTA: Final[int] = 0x0100      # Requerido para modificar cuotas/trimming
    
    # Máscara combinada para operaciones de Trim
    TRIM_ACCESS: Final[int] = 0x00200000 | SET_QUOTA | QUERY_INFO

class CacheEntry(NamedTuple):
    """Caché temporal con timestamp para controlar la expiración de datos."""
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
    """Estructura Win32 GlobalMemoryStatusEx para obtener estado global de RAM."""
    _fields_: List[Tuple[str, Any]] = [
        ("dwLength", wintypes.DWORD),               # Tamaño de esta estructura
        ("dwMemoryLoad", wintypes.DWORD),           # % de RAM en uso
        ("ullTotalPhys", ctypes.c_ulonglong),       # RAM física total en bytes
        ("ullAvailPhys", ctypes.c_ulonglong),       # RAM física libre
        ("ullTotalPageFile", ctypes.c_ulonglong),   # Tamaño del archivo de paginación
        ("ullAvailPageFile", ctypes.c_ulonglong),   # Espacio libre en archivo de paginación
        ("ullTotalVirtual", ctypes.c_ulonglong),    # Espacio virtual total
        ("ullAvailVirtual", ctypes.c_ulonglong),    # Espacio virtual libre
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong), # Reservado
    ]

class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    """Contadores detallados de memoria de proceso usados por GetProcessMemoryInfo."""
    _fields_: List[Tuple[str, Any]] = [
        ("cb", wintypes.DWORD),               # Tamaño de la estructura en bytes
        ("PageFaultCount", wintypes.DWORD),   # Total de fallos de página del proceso
        ("PeakWorkingSetSize", ctypes.c_size_t), # Tamaño máximo alcanzado del working set
        ("WorkingSetSize", ctypes.c_size_t),     # Memoria física actualmente asignada (RSS)
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),      # Confirmación de commit del proceso
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]

@dataclass(frozen=True)
class MemorySnapshot:
    """Instantánea inmutable del estado global de memoria RAM."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula el uso neto: total menos disponible."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Retorna el porcentaje de uso actual."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Retorna el porcentaje disponible actual."""
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
        """Retorna el Working Set convertido a MB."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

    def __lt__(self, other: ProcessMemory) -> bool:
        """Comparador para ordenamiento por uso de memoria descendente."""
        return self.working_set < other.working_set

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte bytes a formato humano legible (ej. 1.5 GB)."""
    if not isinstance(num, (int, float)) or math.isnan(num) or math.isinf(num) or num < 0:
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
    """Instancia la estructura requerida por la API Win32 con el tamaño correcto."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Any, multiplier: int = 1) -> BytesValue:
    """
    Valida entradas (string, int, float) y las convierte a BytesValue.
    Se asegura de evitar desbordamientos y valores negativos o inválidos.
    """
    if value is None: return BytesValue(0)
    
    try:
        if isinstance(value, (int, float)):
            if math.isnan(value) or math.isinf(value): return BytesValue(0)
            # Evitar desbordamiento al multiplicar por el factor
            fval = float(value) * multiplier
            if fval > 9223372036854775807: return BytesValue(0)
            val = int(fval)
        elif isinstance(value, str):
            clean_val = "".join(filter(str.isdigit, value.split(".")[0]))
            val = int(clean_val) * multiplier if clean_val else 0
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
    """
    Interpreta /proc/meminfo de Linux.
    Calcula el total, disponible y caché basándose en las etiquetas de texto del kernel.
    """
    if not isinstance(meminfo_text, str) or not meminfo_text:
        return _EMPTY_SNAPSHOT
        
    metrics: Dict[str, BytesValue] = {}
    for line in meminfo_text.splitlines():
        if ":" not in line: continue
        key, _, value_part = line.partition(":")
        key_stripped = key.strip()
        if not key_stripped: continue
        metrics[key_stripped] = _safe_int_conversion(value_part, 1024)
            
    total: BytesValue = metrics.get("MemTotal", BytesValue(0))
    if total <= 0: return _EMPTY_SNAPSHOT
    
    # MemAvailable es la métrica de referencia, si falta se estima con MemFree
    avail_raw = metrics.get("MemAvailable", metrics.get("MemFree", BytesValue(0)))
    available = BytesValue(max(0, min(total, avail_raw)))
    cached = BytesValue(max(0, metrics.get("Cached", BytesValue(0))))
    
    return MemorySnapshot(total=total, available=available, cached=cached)

def _is_system_process(pid: int) -> bool:
    """Verifica si el PID es crítico o corresponde a la instancia actual."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _is_invalid_process_name(name: str) -> bool:
    """Valida la sanidad del nombre del proceso evitando inyecciones de control."""
    return not name or any(ord(c) < 32 or ord(c) == 127 for c in name) or len(name) > 256

def _is_valid_process(name: str, pid: int, ws: int) -> bool:
    """Verifica límites de seguridad y lógica de los datos de proceso."""
    if _is_system_process(pid) or pid <= 0 or _is_invalid_process_name(name):
        return False
    return 0 < ws < MAX_VALID_PROCESS_MEM_BYTES

def _extract_process_info(line: str) -> Optional[ProcessMemory]:
    """
    Parsea una línea CSV con formato 'nombre,pid,working_set' y retorna
    un objeto de proceso validado si la línea es íntegra y segura.
    """
    if not isinstance(line, str): return None
    try:
        parts = [p.strip().strip("'\"") for p in line.split(",")]
        if len(parts) < 3: return None
        
        proc_name = str(parts[0]).strip()
        # Validación de tipo explícita antes de usar el valor
        try:
            pid_val = int(parts[1])
        except (ValueError, TypeError):
            return None
        
        ws_val = _safe_int_conversion(parts[2])
        if _is_valid_process(proc_name, pid_val, ws_val):
            return ProcessMemory(proc_name, pid_val, ws_val)
    except (AttributeError, IndexError):
        pass
    return None

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Extrae top procesos desde un volcado CSV generado por el sistema."""
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
    """Obtiene métricas mediante GlobalMemoryStatusEx de Win32."""
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
    """Implementa caché temporal para snapshots globales con bucket de 5 seg."""
    if _is_windows: return _read_windows_snapshot()
    try:
        if _linux_mem_path.exists():
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8", errors="ignore"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
    except (OSError, PermissionError): 
        pass
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Retorna estado actual de memoria, cacheado durante 5 segundos."""
    return _get_cached_snapshot(int(time.time() / 5))

def _get_process_handle(pid: int, access_mask: int) -> Optional[wintypes.HANDLE]:
    """Abre handle de proceso usando máscara de acceso Win32 específica."""
    if WindowsAPI.kernel32 is None: return None
    try:
        handle = WindowsAPI.kernel32.OpenProcess(access_mask, False, pid)
        if not handle or handle == INVALID_HANDLE_VALUE:
            return None
        return wintypes.HANDLE(handle)
    except (ctypes.WinError, OSError):
        return None

# Definición de alias de tipo para callback de Windows: GetModuleFileNameEx
GetModNameFunc = Callable[[wintypes.HANDLE, Any, ctypes.Array, int], int]

def _is_process_executable_safe(handle: wintypes.HANDLE, path_buffer: ctypes.Array, get_mod_name_func: GetModNameFunc) -> bool:
    """
    Verifica que el ejecutable del proceso no se encuentre en ruta protegida.
    Utiliza el callback `get_mod_name_func` para resolver la ruta real del ejecutable.
    """
    if handle is None or get_mod_name_func is None: return False
    try:
        length = get_mod_name_func(handle, None, path_buffer, len(path_buffer))
        if length <= 0: return False 
        raw_path = path_buffer.value
        if not raw_path: return False
        return not is_protected_path(Path(raw_path.decode('utf-8', 'ignore')))
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return False

def _get_handle_for_safe_proc(pid: int, access: int, path_buf: ctypes.Array, get_mod_name_func: GetModNameFunc) -> Optional[wintypes.HANDLE]:
    """
    Helper que abre, valida y cierra el handle de un proceso.
    Si el proceso está en zona protegida o ha finalizado, devuelve None y asegura la limpieza.
    """
    handle = _get_process_handle(pid, access)
    if not handle: return None
    
    # Verificación de estado vivo sin cerrar handle aún
    exit_code = wintypes.DWORD()
    if WindowsAPI.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0 or exit_code.value != STILL_ACTIVE_EXIT_CODE:
        WindowsAPI.kernel32.CloseHandle(handle)
        return None
            
    if _is_process_executable_safe(handle, path_buf, get_mod_name_func):
        return handle
        
    WindowsAPI.kernel32.CloseHandle(handle)
    return None

def _iter_process_memory() -> Iterator[ProcessMemory]:
    """
    Generador de procesos del sistema que filtra por uso de RAM y seguridad.
    Pre-aloca buffers compartidos para minimizar el overhead en el escaneo.
    """
    if WindowsAPI.psapi is None or WindowsAPI.kernel32 is None: return
    
    pids = (wintypes.DWORD * 2048)()
    bytes_written = wintypes.DWORD(0)
    
    if not WindowsAPI.psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(bytes_written)):
        return
            
    pmc = PROCESS_MEMORY_COUNTERS(cb=ctypes.sizeof(PROCESS_MEMORY_COUNTERS))
    count = min(bytes_written.value // ctypes.sizeof(wintypes.DWORD), 2048)
    
    # Pre-instancia buffer compartido para reducir alocación en el loop
    path_buf = ctypes.create_unicode_buffer(512)
    get_mod_name = WindowsAPI.psapi.GetModuleFileNameExW
    
    for pid in (p for p in pids[:count] if p > 0 and not _is_system_process(p)):
        handle = _get_handle_for_safe_proc(pid, WindowsAPI.QUERY_LIMITED, path_buf, get_mod_name)
        if handle is None: continue
        
        try:
            if WindowsAPI.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), ctypes.sizeof(pmc)):
                ws = pmc.WorkingSetSize
                if 0 < ws < MAX_VALID_PROCESS_MEM_BYTES:
                    raw_path = path_buf.value
                    name = raw_path.split('\\')[-1] if '\\' in raw_path else raw_path
                    yield ProcessMemory(name, pid, BytesValue(ws))
        except (ctypes.ArgumentError, OSError):
            continue
        finally:
            WindowsAPI.kernel32.CloseHandle(handle)

def _get_process_snapshot(limit: int) -> List[ProcessMemory]:
    """Ejecuta el escaneo de procesos y retorna el top N ordenado."""
    return heapq.nlargest(limit, _iter_process_memory())

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna los procesos de mayor consumo con caché de 60 segundos."""
    global _CACHE_TOP_PROC
    if not _is_windows: return []
    now = time.time()
    
    if (now - _CACHE_TOP_PROC.timestamp) > 60:
        _CACHE_TOP_PROC = CacheEntry(now, _get_process_snapshot(limit))
            
    return _CACHE_TOP_PROC.data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica la severidad del uso de memoria en niveles semánticos."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un informe descriptivo sobre el estado de memoria del sistema."""
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
    Solicita al sistema liberar páginas del Working Set de un proceso.
    Acción manual que requiere privilegios y validación previa de seguridad.
    """
    if not _is_windows or WindowsAPI.kernel32 is None or WindowsAPI.psapi is None:
        return False, "Operación no soportada."
    
    try: 
        target_pid = int(pid)
    except (ValueError, TypeError): 
        return False, "El PID debe ser numérico."
        
    # Validación estricta de seguridad sobre el PID antes de operar
    if target_pid <= 0 or _is_system_process(target_pid) or not hasattr(WindowsAPI.psapi, "EmptyWorkingSet"):
        return False, "Operación no permitida para este PID o no disponible."
        
    path_buf = ctypes.create_unicode_buffer(512)
    # Reutilizamos el helper que garantiza la liberación del handle
    handle = _get_handle_for_safe_proc(target_pid, WindowsAPI.TRIM_ACCESS, path_buf, WindowsAPI.psapi.GetModuleFileNameExW)
    
    if handle is None:
        return False, "Acceso denegado o proceso en zona restringida."

    try:
        if WindowsAPI.psapi.EmptyWorkingSet(handle) != 0:
            return True, f"Working set liberado. {TRIM_WARNING}"
        return False, "El sistema operativo rechazó la operación."
    except (ctypes.ArgumentError, OSError, Exception):
        return False, "Error inesperado al intentar liberar memoria."
    finally:
        WindowsAPI.kernel32.CloseHandle(handle)
