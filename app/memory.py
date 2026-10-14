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
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, Final, Set, NewType, Any
from safety import is_protected_path, filter_safe_paths

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

# Máscaras de acceso Win32:
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x0400
FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x0400
DRIVE_FIXED: Final[int] = 3
INVALID_HANDLE_VALUE: Final[int] = -1

# TRIM_ACCESS_MASK requiere acceso de escritura/modificación para EmptyWorkingSet.
TRIM_ACCESS_MASK: Final[int] = 0x00020000 | PROCESS_SET_QUOTA

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
    """Metadatos de consumo de memoria de un proceso específico."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

    def __lt__(self, other: ProcessMemory) -> bool:
        return self.working_set < other.working_set

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte bytes numéricos en una cadena humanamente legible con unidades."""
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
    """Inicializa la estructura MEMORYSTATUSEX con el tamaño de bytes requerido."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Optional[str], multiplier: int = 1) -> BytesValue:
    """Extrae enteros de cadenas de texto y los escala por el multiplicador dado."""
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

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Analiza /proc/meminfo para extraer métricas de memoria en sistemas Linux."""
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
    
    avail_raw = metrics.get("MemAvailable", metrics.get("MemFree", BytesValue(0)))
    available = BytesValue(max(0, min(total, avail_raw)))
    cached = BytesValue(max(0, metrics.get("Cached", BytesValue(0))))
    
    return MemorySnapshot(total=total, available=available, cached=cached)

def _is_system_process(pid: int) -> bool:
    """Verifica si el PID corresponde al sistema o al proceso actual (autoprotección)."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _extract_process_info(line: str) -> Optional[ProcessMemory]:
    """Parsea una línea CSV para crear un objeto ProcessMemory tras validar el PID."""
    if not isinstance(line, str) or not line.strip() or "," not in line: 
        return None
        
    parts = [p.strip().strip("'\"") for p in line.split(",")]
    if len(parts) < 3: 
        return None
    
    name, pid_str, ws_str = parts[0], parts[1], parts[2]
    try:
        pid = int(pid_str)
        ws = _safe_int_conversion(ws_str)
        if pid <= 0 or _is_system_process(pid):
            return None
        if 0 < ws < MAX_VALID_PROCESS_MEM:
            return ProcessMemory(name, pid, BytesValue(ws))
    except (ValueError, TypeError):
        return None
    return None

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Transforma una entrada CSV en una lista de ProcessMemory ordenada por uso de RAM."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text: 
        return []
    lines = raw_csv_text.splitlines()
    if len(lines) < 2:
        return []
    processes = []
    for line in lines[1:]:
        proc = _extract_process_info(line)
        if proc:
            processes.append(proc)
    return sorted(processes, key=lambda p: p.working_set, reverse=True)[:limit]

def _read_windows_snapshot() -> MemorySnapshot:
    """Interroga la API nativa GlobalMemoryStatusEx para obtener el estado físico."""
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
    """Selector de origen para la lectura del snapshot según la plataforma (Win/Linux)."""
    if _is_windows: return _read_windows_snapshot()
    try:
        if _linux_mem_path.exists():
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
    except (OSError, PermissionError, UnicodeDecodeError): 
        pass
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Devuelve un estado de memoria con una latencia de caché de 5 segundos."""
    return _get_cached_snapshot(int(time.time() / 5))

def _query_working_set_bytes(pid: int, kernel32: ctypes.WinDLL, psapi: ctypes.WinDLL, pmc: PROCESS_MEMORY_COUNTERS) -> Optional[BytesValue]:
    """Consulta contadores de memoria mediante API externa con validación de handle."""
    process_handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if process_handle is None or process_handle == INVALID_HANDLE_VALUE:
        return None
    try:
        if psapi.GetProcessMemoryInfo(process_handle, ctypes.byref(pmc), pmc.cb):
            val = pmc.WorkingSetSize
            return BytesValue(val) if 0 < val < MAX_VALID_PROCESS_MEM else None
    except (ctypes.ArgumentError, OSError, AttributeError):
        return None
    finally:
        kernel32.CloseHandle(process_handle)
    return None

_CACHE_TOP_PROC: Dict[str, Any] = {"time": 0.0, "data": []}

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Lista los procesos más pesados enumerando la tabla de procesos del sistema."""
    if not _is_windows: return []
    now = time.time()
    
    if (now - _CACHE_TOP_PROC["time"]) > 60:
        kernel32 = getattr(ctypes.windll, "kernel32", None)
        psapi = getattr(ctypes.windll, "psapi", None)
        if not kernel32 or not psapi or not hasattr(psapi, "EnumProcesses"): return []
        
        pids = (ctypes.c_uint32 * 1024)()
        cb_needed = ctypes.c_uint32()
        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        
        try:
            if psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(cb_needed)):
                found_procs = []
                count = min(1024, cb_needed.value // 4)
                
                candidates = []
                for i in range(count):
                    pid = pids[i]
                    if pid > 4 and not _is_system_process(pid) and _is_process_executable_safe(pid):
                        ws = _query_working_set_bytes(pid, kernel32, psapi, pmc)
                        if ws is not None:
                            candidates.append((pid, ws))
                
                candidates.sort(key=lambda x: x[1], reverse=True)
                for pid, ws in candidates[:limit]:
                    found_procs.append(ProcessMemory(f"PID {pid}", pid, ws))
                
                _CACHE_TOP_PROC["data"] = found_procs
                _CACHE_TOP_PROC["time"] = now
        except (ctypes.ArgumentError, OSError, TypeError):
            pass
            
    return _CACHE_TOP_PROC["data"]

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica la salud de la memoria según porcentaje disponible."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera una descripción legible sobre el estado de la memoria para el usuario."""
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

def _is_process_executable_safe(pid: int) -> bool:
    """
    Valida la integridad de la ruta del ejecutable para prevenir ataques de
    suplantación o saltos de directorios protegidos.
    """
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    psapi = getattr(ctypes.windll, "psapi", None)
    if not kernel32 or not psapi or not hasattr(psapi, "GetModuleFileNameExW"): return False
    
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle is None or handle == INVALID_HANDLE_VALUE: return False
    try:
        buf = ctypes.create_unicode_buffer(1024)
        if psapi.GetModuleFileNameExW(handle, None, buf, 1024) > 0 and buf.value:
            path_str = str(buf.value).strip()
            if not path_str or path_str.startswith("\\\\"):
                return False
            
            p = Path(path_str)
            if p.is_symlink():
                return False
                
            normalized = str(p.resolve())
            return not is_protected_path(normalized) and len(filter_safe_paths([normalized])) > 0
    except (ctypes.ArgumentError, OSError, ValueError, RuntimeError):
        return False
    finally:
        kernel32.CloseHandle(handle)
    return False

def _is_safe_to_trim(pid: int) -> Tuple[bool, Optional[str]]:
    """Capa de seguridad: Verifica que el PID sea apto para recibir comandos de gestión de memoria."""
    if pid <= 0: return False, "PID inválido."
    if _is_system_process(pid): return False, "Proceso protegido."
    if not _is_process_executable_safe(pid): return False, "Ruta de proceso restringida."
    return True, None

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Ejecuta EmptyWorkingSet sobre un proceso bajo estrictas normas de seguridad.
    Nota: Requiere privilegios elevados y debe usarse únicamente ante sospecha de fugas.
    """
    if not _is_windows: return False, "Solo soportado en Windows."
    
    try:
        target_pid = int(pid)
    except (ValueError, TypeError):
        return False, "PID no numérico."
    
    is_safe, error_msg = _is_safe_to_trim(target_pid)
    if not is_safe: return False, error_msg or "Verificación de seguridad fallida."
    
    psapi = getattr(ctypes.windll, "psapi", None)
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    if not psapi or not kernel32 or not hasattr(psapi, "EmptyWorkingSet"):
        return False, "API no disponible."
    
    proc_handle = kernel32.OpenProcess(TRIM_ACCESS_MASK, False, target_pid)
    if proc_handle is None or proc_handle == INVALID_HANDLE_VALUE:
        return False, "No se pudo acceder al proceso (requiere privilegios elevados)."
        
    try:
        if psapi.EmptyWorkingSet(proc_handle) == 0:
            return False, "El sistema operativo rechazó la operación."
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (ctypes.ArgumentError, OSError):
        return False, "Error inesperado al ejecutar llamada del sistema."
    finally:
        kernel32.CloseHandle(proc_handle)
