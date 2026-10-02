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
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, Final, Set, NewType
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
# Límite heurístico para filtrar valores erróneos de lectura de procesos (128GB).
MAX_VALID_PROCESS_MEM: Final[int] = 128 * 1024 * BYTES_IN_MB 

# Máscaras de acceso Win32 (Permisos requeridos para consultar o modificar procesos).
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x0400
PROCESS_QUERY_INFORMATION: Final[int] = 0x0400

# TRIM_ACCESS_MASK combina Query para validar estado y SetQuota para ejecutar EmptyWorkingSet.
TRIM_ACCESS_MASK: Final[int] = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

STILL_ACTIVE_EXIT_CODE: Final[int] = 259
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}
ERROR_ACCESS_DENIED: Final[int] = 5
ERROR_INVALID_PARAMETER: Final[int] = 87

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

@dataclass(frozen=True)
class MemorySnapshot:
    """Representación inmutable del estado global de memoria del sistema."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula los bytes totales en uso."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Calcula el porcentaje de memoria en uso."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Calcula el porcentaje de memoria disponible."""
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
        """Convierte el working set a Megabytes."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

    def __lt__(self, other: ProcessMemory) -> bool:
        return self.working_set < other.working_set

def format_bytes(num: Optional[int | float]) -> str:
    """
    Convierte un valor de bytes a una cadena legible (ej. '10.5 MB').

    Args:
        num: Cantidad de bytes a convertir.

    Returns:
        Cadena con unidad formateada.
    """
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"

def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """Inicializa la estructura MEMORYSTATUSEX con el tamaño correcto."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Optional[str], multiplier: int = 1) -> BytesValue:
    """
    Extrae dígitos de una cadena y devuelve el valor multiplicado.

    Args:
        value: Cadena potencialmente numérica.
        multiplier: Multiplicador escalar para la conversión (ej. 1024 para KB).
    """
    if not value: return BytesValue(0)
    clean_val = "".join(c for c in value if c.isdigit())
    return BytesValue(max(0, int(clean_val)) * multiplier) if clean_val else BytesValue(0)

_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))
_linux_available: bool = True
_proc_cache_time: float = 0.0
_proc_cache_data: List[ProcessMemory] = []

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """
    Parsea el contenido de /proc/meminfo.

    Args:
        meminfo_text: Contenido crudo del archivo /proc/meminfo.

    Returns:
        Snapshot con métricas extraídas o snapshot vacío.
    """
    if not meminfo_text: return _EMPTY_SNAPSHOT
    metrics: Dict[str, BytesValue] = {}
    for line in meminfo_text.splitlines():
        if ":" not in line: continue
        key, _, value_part = line.partition(":")
        metrics[key.strip()] = _safe_int_conversion(value_part, 1024)
            
    total: BytesValue = metrics.get("MemTotal", BytesValue(0))
    if total <= 0: return _EMPTY_SNAPSHOT
    
    avail_raw = metrics.get("MemAvailable", metrics.get("MemFree", BytesValue(0)))
    available = BytesValue(min(total, avail_raw))
    
    return MemorySnapshot(total=total, available=available, cached=metrics.get("Cached", BytesValue(0)))

def _extract_process_info(line: str) -> Optional[Tuple[str, int, BytesValue]]:
    """Extrae y valida datos de una línea de CSV de proceso."""
    parts = line.split(",", 2)
    if len(parts) < 3: return None
    
    name, pid_str, ws_str = parts
    pid_digits = "".join(filter(str.isdigit, pid_str))
    if not pid_digits: return None
    
    pid = int(pid_digits)
    # Filtro rápido de procesos críticos en la salida CSV
    if pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid(): return None
    
    ws = _safe_int_conversion(ws_str)
    
    if pid > 0 and 0 < ws < MAX_VALID_PROCESS_MEM:
        return (name.strip("'\" "), pid, ws)
    return None

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """
    Parsea la salida CSV de PowerShell y extrae los top consumidores.
    """
    if not raw_csv_text: return []
    top_heap: List[ProcessMemory] = []

    # Procesamos las líneas omitiendo el header del CSV (Name,Id,WorkingSet)
    for line in (l for l in raw_csv_text.splitlines()[1:] if l and "," in l):
        data = _extract_process_info(line)
        if data:
            name, pid, ws = data
            process_data = ProcessMemory(name, pid, ws)
            if len(top_heap) < limit:
                heapq.heappush(top_heap, process_data)
            elif ws > top_heap[0].working_set:
                heapq.heapreplace(top_heap, process_data)
            
    return sorted(top_heap, key=lambda p: p.working_set, reverse=True)

def _read_windows_snapshot() -> MemorySnapshot:
    """Consulta la API de Win32 para estadísticas globales de memoria."""
    kernel32 = ctypes.windll.kernel32
    if not hasattr(kernel32, "GlobalMemoryStatusEx"): return _EMPTY_SNAPSHOT
    mem_status = _create_mem_status_ex()
    try:
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_status)) != 0 and mem_status.ullTotalPhys > 0:
            return MemorySnapshot(total=BytesValue(mem_status.ullTotalPhys), available=BytesValue(mem_status.ullAvailPhys))
    except (ctypes.ArgumentError, OSError):
        pass
    return _EMPTY_SNAPSHOT

@lru_cache(maxsize=1)
def _get_cached_snapshot(timestamp_bucket: int) -> MemorySnapshot:
    """Obtiene un snapshot global con caché temporal de 5 segundos."""
    if _is_windows: return _read_windows_snapshot()
    global _linux_available
    if _linux_available:
        try:
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
        except (OSError, PermissionError, UnicodeDecodeError): _linux_available = False
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Wrapper para obtener el estado actual de la memoria."""
    return _get_cached_snapshot(int(time.time() / 5))

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna los procesos que más memoria RAM consumen."""
    global _proc_cache_time, _proc_cache_data
    if not _is_windows: return []
    now = time.time()
    if (now - _proc_cache_time) > 60:
        ps_query = (
            "Get-Process | Sort-Object WorkingSet -Descending | "
            "Select-Object -First 100 | Select-Object Name,Id,WorkingSet | "
            "ConvertTo-Csv -NoTypeInformation"
        )
        cmd = ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps_query]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout:
                parsed = parse_windows_process_csv(res.stdout, limit=limit)
                if parsed:
                    _proc_cache_data = parsed
                    _proc_cache_time = now
        except (OSError, subprocess.SubprocessError):
            pass
    return _proc_cache_data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el estado actual de la memoria según porcentaje libre."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """
    Genera un reporte descriptivo sobre el estado de la memoria.

    Args:
        snapshot: Estado actual de la memoria.
        processes: Lista opcional de procesos principales.

    Returns:
        Reporte formateado como lista de cadenas.
    """
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
    """Verifica si un proceso es crítico del sistema o la propia app."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _get_process_path(pid: int) -> Optional[Path]:
    """Obtiene la ruta absoluta del ejecutable para un PID dado usando APIs Win32."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle: return None
    try:
        psapi = ctypes.windll.psapi
        buffer_size = 1024
        buf = ctypes.create_unicode_buffer(buffer_size)
        if psapi.GetModuleFileNameExW(handle, None, buf, buffer_size) > 0:
            path_obj = Path(buf.value).resolve()
            # Validación de seguridad: debe ser archivo existente y no estar en lista negra
            if path_obj.exists() and path_obj.is_file() and not is_protected_path(str(path_obj)):
                return path_obj
    except (ctypes.ArgumentError, OSError, ValueError):
        return None
    finally:
        kernel32.CloseHandle(handle)
    return None

def _is_safe_to_trim(pid: int) -> Tuple[bool, Optional[str]]:
    """Verifica si un proceso es candidato seguro para una operación de trimming."""
    if _is_system_process(pid):
        return False, "Proceso crítico del sistema protegido."
    if _get_process_path(pid) is None:
        return False, "Ruta del proceso inaccesible o restringida por seguridad."
    return True, None

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Intenta liberar el working set de un proceso (solo Windows).
    
    Args:
        pid: ID del proceso objetivo.

    Returns:
        Tuple indicando éxito (bool) y mensaje de estado (str).
    """
    if not _is_windows: return False, "Solo soportado en Windows."
    
    try: 
        target_pid = int(pid)
    except (ValueError, TypeError): 
        return False, "PID proporcionado no es un número válido."
    
    psapi = ctypes.windll.psapi
    if not hasattr(psapi, "EmptyWorkingSet"): return False, "API no disponible en este sistema."

    # Verificar seguridad antes de interactuar con el proceso
    is_safe, error_msg = _is_safe_to_trim(target_pid)
    if not is_safe: return False, error_msg or "Verificación de seguridad fallida."

    kernel32 = ctypes.windll.kernel32
    proc_handle = kernel32.OpenProcess(TRIM_ACCESS_MASK, False, target_pid)
    if not proc_handle: 
        error_code = ctypes.get_last_error()
        return False, f"No se pudo abrir el proceso para trimming (Código: {error_code})."
    
    try:
        # Ejecución del comando de limpieza de working set vía Win32
        if psapi.EmptyWorkingSet(proc_handle) == 0:
            error_code = ctypes.get_last_error()
            if error_code == ERROR_ACCESS_DENIED:
                return False, "Acceso denegado: requiere privilegios de administrador."
            return False, f"El sistema rechazó la solicitud (código {error_code})."
        return True, f"Working set liberado. {TRIM_WARNING}"
    finally: 
        kernel32.CloseHandle(proc_handle)
