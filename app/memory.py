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
from pathlib import Path
from functools import lru_cache
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, Final, Set, NewType
from safety import is_protected_path, is_safe_to_modify

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
# Límite heurístico para filtrar valores erróneos de lectura de procesos (128GB).
MAX_VALID_PROCESS_MEM: Final[int] = 128 * 1024 * BYTES_IN_MB 

# Máscaras de acceso Win32 para interactuar con la memoria de procesos ajenos.
# PROCESS_QUERY_LIMITED_INFORMATION: Permite obtener metadatos básicos del proceso.
# PROCESS_SET_QUOTA: Necesario para modificar límites de working set.
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x0400
FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x0400

# TRIM_ACCESS_MASK combina Query para validar estado y SetQuota para ejecutar EmptyWorkingSet.
TRIM_ACCESS_MASK: Final[int] = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}
ERROR_ACCESS_DENIED: Final[int] = 5

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
    """Estructura Win32 mapeada para la API GlobalMemoryStatusEx."""
    _fields_: List[Tuple[str, type]] = [
        ("dwLength", ctypes.c_ulong),            # Tamaño de la estructura en bytes
        ("dwMemoryLoad", ctypes.c_ulong),        # Porcentaje de uso de memoria (0-100)
        ("ullTotalPhys", ctypes.c_ulonglong),    # Memoria física total
        ("ullAvailPhys", ctypes.c_ulonglong),    # Memoria física disponible
        ("ullTotalPageFile", ctypes.c_ulonglong),# Límite del archivo de paginación
        ("ullAvailPageFile", ctypes.c_ulonglong),# Disponible en archivo de paginación
        ("ullTotalVirtual", ctypes.c_ulonglong), # Espacio virtual total
        ("ullAvailVirtual", ctypes.c_ulonglong), # Espacio virtual disponible
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong), # Siempre 0 (reservado)
    ]

@dataclass(frozen=True)
class MemorySnapshot:
    """Representación inmutable del estado global de memoria del sistema."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula el uso físico de memoria basado en el total y disponible."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Retorna el porcentaje de memoria en uso como valor flotante."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Retorna el porcentaje de memoria libre como valor flotante."""
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
        """Retorna el valor de working set convertido a Megabytes."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

    def __lt__(self, other: ProcessMemory) -> bool:
        return self.working_set < other.working_set

def format_bytes(num: Optional[int | float]) -> str:
    """
    Convierte un valor numérico de bytes a una cadena legible con unidad.
    Maneja escalas desde Bytes hasta Terabytes de forma dinámica.
    """
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"

def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """Inicializa la estructura MEMORYSTATUSEX con el tamaño requerido por la API Win32."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Optional[str], multiplier: int = 1) -> BytesValue:
    """
    Limpia cadenas provenientes de parsers para extraer enteros.
    Aplica multiplicador (ej. KB a Bytes) para normalizar unidades.
    """
    if not isinstance(value, str): return BytesValue(0)
    digits = "".join(filter(str.isdigit, value))
    return BytesValue(int(digits) * multiplier) if digits else BytesValue(0)

_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))
_linux_available: bool = True

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """
    Parsea el contenido de /proc/meminfo mediante procesamiento lineal.
    Implementación pura: procesa el texto línea por línea para evitar dependencias.
    """
    if not isinstance(meminfo_text, str) or not meminfo_text:
        return _EMPTY_SNAPSHOT
        
    metrics: Dict[str, BytesValue] = {}
    for line in meminfo_text.splitlines():
        if ":" not in line: continue
        key, _, value_part = line.partition(":")
        metrics[key.strip()] = _safe_int_conversion(value_part, 1024)
            
    total: BytesValue = metrics.get("MemTotal", BytesValue(0))
    if total <= 0: return _EMPTY_SNAPSHOT
    
    avail_raw = metrics.get("MemAvailable", metrics.get("MemFree", BytesValue(0)))
    available = BytesValue(max(0, min(total, avail_raw)))
    cached = BytesValue(max(0, metrics.get("Cached", BytesValue(0))))
    
    return MemorySnapshot(total=total, available=available, cached=cached)

def _extract_process_info(line: str) -> Optional[ProcessMemory]:
    """Extrae y valida datos de un proceso desde una línea CSV."""
    if not isinstance(line, str) or "," not in line: 
        return None
        
    parts = [p.strip().strip("'\"") for p in line.split(",")]
    if len(parts) < 3: 
        return None
    
    name, pid_str, ws_str = parts[0], parts[1], parts[2]
    
    pid_digits = "".join(filter(str.isdigit, pid_str))
    ws_digits = "".join(filter(str.isdigit, ws_str))
    
    if not pid_digits or not ws_digits:
        return None
        
    pid, ws = int(pid_digits), int(ws_digits)
    
    if _is_system_process(pid) or pid <= 0: 
        return None
    
    if 0 < ws < MAX_VALID_PROCESS_MEM:
        return ProcessMemory(name, pid, BytesValue(ws))
    return None

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Procesa el CSV de procesos y devuelve los N de mayor consumo."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text: 
        return []
    lines = raw_csv_text.splitlines()
    if len(lines) < 2:
        return []
    processes = (proc for line in lines[1:] if (proc := _extract_process_info(line)))
    return sorted(processes, key=lambda p: p.working_set, reverse=True)[:limit]

def _read_windows_snapshot() -> MemorySnapshot:
    """Invoca la API Win32 GlobalMemoryStatusEx para obtener el estado físico."""
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
    except (ctypes.ArgumentError, OSError, Exception):
        pass
    return _EMPTY_SNAPSHOT

@lru_cache(maxsize=1)
def _get_cached_snapshot(timestamp_bucket: int) -> MemorySnapshot:
    """
    Memoiza el estado del sistema con TTL de bucket temporal.
    Evita saturar la llamada a APIs de sistema en periodos cortos.
    """
    if _is_windows: return _read_windows_snapshot()
    global _linux_available
    if _linux_available:
        try:
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
        except (OSError, PermissionError, UnicodeDecodeError): _linux_available = False
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Interfaz pública: retorna un snapshot del estado de memoria actual."""
    return _get_cached_snapshot(int(time.time() / 5))

def _get_process_memory_stats(pid: int) -> Optional[BytesValue]:
    """
    Obtiene el tamaño del Working Set mediante la API PSAPI GetProcessMemoryInfo.
    Utiliza un handle de lectura restringida para evitar escalada de privilegios.
    """
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    process_handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process_handle:
        return None
    try:
        pmc = (ctypes.c_size_t * 6)()
        if psapi.GetProcessMemoryInfo(process_handle, ctypes.byref(pmc), ctypes.sizeof(pmc)):
            return BytesValue(pmc[3])
    except (ctypes.ArgumentError, OSError, AttributeError):
        pass
    finally:
        kernel32.CloseHandle(process_handle)
    return None

def _get_proc_memory_by_pid(pid: int) -> Optional[ProcessMemory]:
    """Helper para top_memory_processes que filtra procesos críticos."""
    if _is_system_process(pid): return None
    ws = _get_process_memory_stats(pid)
    return ProcessMemory(f"PID {pid}", pid, ws) if ws and 0 < ws < MAX_VALID_PROCESS_MEM else None

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Obtiene los procesos más pesados mediante enumeración de PIDs de Windows."""
    if not hasattr(top_memory_processes, "_cache"):
        top_memory_processes._cache = (0.0, [])
    
    if not _is_windows: return []
    now = time.time()
    cache_time, cache_data = top_memory_processes._cache
    
    if (now - cache_time) > 60:
        psapi = ctypes.windll.psapi
        pids = (ctypes.c_ulong * 4096)()
        cb = ctypes.sizeof(pids)
        cb_needed = ctypes.c_ulong()
        
        if psapi.EnumProcesses(ctypes.byref(pids), cb, ctypes.byref(cb_needed)):
            count = cb_needed.value // ctypes.sizeof(ctypes.c_ulong)
            valid_processes = []
            for i in range(min(count, 4096)):
                pid = pids[i]
                if not _is_system_process(pid):
                    proc = _get_proc_memory_by_pid(pid)
                    if proc:
                        valid_processes.append(proc)
            
            cache_data = sorted(valid_processes, key=lambda p: p.working_set, reverse=True)[:limit]
            top_memory_processes._cache = (now, cache_data)
            
    return cache_data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el nivel de estrés mediante umbrales porcentuales de RAM disponible."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un reporte legible por humanos sobre el estado de la memoria."""
    if snapshot.total <= 0: return ["No se pudo leer el estado de la memoria."]
    diagnostics = {
        "ok": "Estado: holgado. La memoria ocupada por caché mejora la velocidad.",
        "info": "Estado: normal. Windows gestiona la memoria de forma eficiente.",
        "warning": "Estado: ajustado. Conviene cerrar aplicaciones innecesarias.",
        "danger": "Estado: crítico. El sistema recurre al archivo de paginación."
    }
    report = [
        f"Memoria total: {format_bytes(snapshot.total)}",
        f"En uso: {format_bytes(snapshot.used)} ({snapshot.used_percent}%)",
        f"Disponible: {format_bytes(snapshot.available)} ({snapshot.available_percent}%)",
        diagnostics.get(pressure_level(snapshot), "")
    ]
    if processes:
        report.extend(f"  Mayor consumo: {p.name} (PID {p.pid}) — {p.working_set_mb} MB" for p in processes[:3])
    return report

def _is_system_process(pid: int) -> bool:
    """Filtra PIDs críticos (kernel) o el proceso actual para evitar manipulación."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _is_path_safe_and_valid(path_obj: Path) -> bool:
    """Valida integridad de seguridad: no seguir reparse points ni rutas protegidas."""
    kernel32 = ctypes.windll.kernel32
    if is_protected_path(str(path_obj)) or not is_safe_to_modify(path_obj):
        return False
    attr = kernel32.GetFileAttributesW(str(path_obj))
    return attr != -1 and not (attr & FILE_ATTRIBUTE_REPARSE_POINT)

def _get_process_path(pid: int) -> Optional[Path]:
    """
    Resuelve la ruta absoluta del ejecutable usando PSAPI GetModuleFileNameExW.
    Valida la ruta contra `is_protected_path` y `is_safe_to_modify` antes de retornar.
    """
    kernel32 = ctypes.windll.kernel32
    psapi = getattr(ctypes.windll, "psapi", None)
    if not psapi or not hasattr(psapi, "GetModuleFileNameExW"): return None
    process_handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process_handle: return None
    try:
        buffer_size = 1024
        buf = ctypes.create_unicode_buffer(buffer_size)
        length = psapi.GetModuleFileNameExW(process_handle, None, buf, buffer_size)
        if 0 < length < buffer_size:
            raw_path = buf.value
            if not raw_path or raw_path.startswith("\\\\"): return None
            
            p_test = Path(raw_path)
            if p_test.exists():
                resolved = p_test.resolve()
                if not is_protected_path(str(resolved)) and _is_path_safe_and_valid(resolved):
                    return resolved
    except (OSError, RuntimeError, ctypes.ArgumentError): pass
    finally: kernel32.CloseHandle(process_handle)
    return None

def _is_safe_to_trim(pid: int) -> Tuple[bool, Optional[str]]:
    """Evalúa si un proceso es candidato para EmptyWorkingSet sin riesgos de seguridad."""
    if pid <= 0: return False, "PID inválido."
    if _is_system_process(pid): return False, "Proceso crítico del sistema protegido."
    
    path = _get_process_path(pid)
    if path is None: return False, "Ruta del proceso inaccesible o restringida por seguridad."
    
    if not is_safe_to_modify(path):
        return False, "La ruta del proceso está protegida por la política de seguridad."
        
    return True, None

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Ejecuta el trim del working set para el PID indicado si es seguro hacerlo.
    Advertencia: esta operación fuerza la expulsión de páginas de memoria a disco.
    """
    if not _is_windows: return False, "Solo soportado en Windows."
    
    try:
        target_pid = int(pid)
    except (ValueError, TypeError):
        return False, "El PID proporcionado no es un número válido."
    
    if target_pid <= 0:
        return False, "PID no puede ser cero o negativo."

    is_safe, error_msg = _is_safe_to_trim(target_pid)
    if not is_safe: return False, error_msg or "Verificación de seguridad fallida."
    
    psapi = getattr(ctypes.windll, "psapi", None)
    if not psapi or not hasattr(psapi, "EmptyWorkingSet"): return False, "API de gestión de memoria no disponible."
    
    kernel32 = ctypes.windll.kernel32
    proc_handle = kernel32.OpenProcess(TRIM_ACCESS_MASK, False, target_pid)
    if not proc_handle:
        return False, "No se pudo acceder al proceso (posible cierre reciente)."
        
    try:
        # EmptyWorkingSet retorna un valor distinto de cero si tiene éxito
        if psapi.EmptyWorkingSet(proc_handle) == 0:
            return False, "El sistema rechazó el trim (error de privilegios o estado)."
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (ctypes.ArgumentError, OSError, Exception):
        return False, "Error inesperado al ejecutar el comando de trim."
    finally:
        kernel32.CloseHandle(proc_handle)
