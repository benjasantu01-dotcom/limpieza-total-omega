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
    wintypes = None

# Tipos semánticos para evitar confusión de unidades en cálculos aritméticos:
BytesValue = NewType("BytesValue", int)
MegabytesValue = NewType("MegabytesValue", float)

# Constantes de conversión y límites de seguridad:
BYTES_IN_MB: Final[int] = 1024 * 1024
BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")
MAX_VALID_PROCESS_MEM: Final[int] = 128 * 1024 * BYTES_IN_MB 

# Máscaras de acceso Win32 para operaciones seguras en procesos:
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x100
SAFE_ACCESS_MASK: Final[int] = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

STILL_ACTIVE_EXIT_CODE: Final[int] = 259
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}
ERROR_ACCESS_DENIED: Final[int] = 5

PS_QUERY_CMD: Final[List[str]] = [
    'powershell', '-NoProfile', '-NonInteractive', '-Command', 
    'Get-Process | Where-Object { $_.Id -notin 0,4 } | ForEach-Object { "$($_.Name),$($_.Id),$($_.WorkingSet)" }'
]

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
    """Estructura Win32 (GlobalMemoryStatusEx) para obtener métricas de RAM global."""
    _fields_: List[Tuple[str, type]] = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]

@dataclass(frozen=True)
class MemorySnapshot:
    """Estado inmutable de la RAM global capturado en un momento dado."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula memoria en uso: total - disponible (ajustado a >= 0)."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Retorna porcentaje de uso relativo al total físico."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Retorna porcentaje de disponibilidad relativo al total físico."""
        if self.total <= 0: return 0.0
        return round((float(self.available) / float(self.total)) * 100, 1)

@dataclass
class ProcessMemory:
    """Representación de consumo de memoria de un proceso individual."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Convierte bytes de memoria de trabajo a MiB para reportes."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte un valor numérico a representación humana (SI)."""
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"

def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """Inicializa la estructura Win32 asegurando que el campo dwLength sea correcto."""
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return stat

def _safe_int_conversion(value: Optional[str], multiplier: int = 1) -> BytesValue:
    """Convierte una cadena a BytesValue extrayendo solo dígitos, útil para parseo."""
    if value is None:
        return BytesValue(0)
    try:
        clean_val = "".join(c for c in value if c.isdigit())
        if not clean_val: return BytesValue(0)
        return BytesValue(int(clean_val) * multiplier)
    except (ValueError, OverflowError):
        return BytesValue(0)

_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Parsea el contenido crudo de /proc/meminfo en una instancia MemorySnapshot."""
    if not isinstance(meminfo_text, str) or not meminfo_text.strip():
        return _EMPTY_SNAPSHOT
    
    metrics: Dict[str, BytesValue] = {}
    for line in meminfo_text.splitlines():
        if ":" not in line: continue
        parts = line.split(":", 1)
        if len(parts) == 2:
            key, value_part = parts
            metrics[key.strip()] = _safe_int_conversion(value_part, 1024)
            
    total = metrics.get("MemTotal", BytesValue(0))
    if total <= 0: return _EMPTY_SNAPSHOT
    
    available = metrics.get("MemAvailable", metrics.get("MemFree", BytesValue(0)))
    cached = metrics.get("Cached", BytesValue(0))
    
    return MemorySnapshot(
        total=total, 
        available=BytesValue(min(available, total)), 
        cached=BytesValue(max(0, cached))
    )

def _extract_numeric_val(text: str) -> int:
    """Extrae solo dígitos de una cadena para conversión segura."""
    return int("".join(c for c in text if c.isdigit()) or 0)

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Procesa el CSV de PowerShell y retorna los N procesos con mayor consumo."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text.strip():
        return []
    
    results: List[ProcessMemory] = []
    for line in raw_csv_text.splitlines():
        parts = line.strip().split(",", 2)
        if len(parts) < 3: continue
        
        try:
            pid = _extract_numeric_val(parts[1])
            ws = _extract_numeric_val(parts[2])
            
            if pid > 0 and 0 <= ws < MAX_VALID_PROCESS_MEM:
                results.append(ProcessMemory(name=parts[0].strip("'\" "), pid=pid, working_set=BytesValue(ws)))
        except (ValueError, TypeError, OverflowError):
            continue
    
    results.sort(key=lambda p: p.working_set, reverse=True)
    return results[:limit]

def _read_windows_snapshot() -> MemorySnapshot:
    """Invoca GlobalMemoryStatusEx para obtener métricas físicas del sistema (Win32 API)."""
    kernel32 = ctypes.windll.kernel32
    if not hasattr(kernel32, "GlobalMemoryStatusEx"):
        return _EMPTY_SNAPSHOT
    
    stat = _create_mem_status_ex()
    try:
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            total, avail = stat.ullTotalPhys, stat.ullAvailPhys
            if total > 0 and avail <= total:
                return MemorySnapshot(total=BytesValue(total), available=BytesValue(avail))
    except (AttributeError, OSError, ctypes.ArgumentError):
        pass
    return _EMPTY_SNAPSHOT

_linux_available: bool = True

@lru_cache(maxsize=1)
def _get_cached_snapshot(timestamp_bucket: int) -> MemorySnapshot:
    """Obtiene el estado de RAM global, cacheando el resultado por periodos cortos."""
    if _is_windows: 
        return _read_windows_snapshot()
    
    global _linux_available
    if _linux_available:
        try:
            content = _linux_mem_path.read_text(encoding="utf-8")
            snapshot = parse_linux_meminfo(content)
            if snapshot != _EMPTY_SNAPSHOT:
                return snapshot
            _linux_available = False
        except (OSError, PermissionError, UnicodeDecodeError, RuntimeError):
            _linux_available = False
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Punto de entrada: obtiene un snapshot global, refrescado cada 5 segundos."""
    return _get_cached_snapshot(int(time.time() / 5))

_proc_cache_time: float = 0.0
_proc_cache_data: List[ProcessMemory] = []

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna los procesos que más consumen RAM con un cache de 60 segundos."""
    global _proc_cache_time, _proc_cache_data
    if not _is_windows: return []
    
    now = time.time()
    if (now - _proc_cache_time) > 60:
        try:
            proc = subprocess.run(PS_QUERY_CMD, capture_output=True, text=True, timeout=3, check=False)
            if proc.returncode == 0 and proc.stdout:
                _proc_cache_data = parse_windows_process_csv(proc.stdout, limit=50)
                _proc_cache_time = now
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired): 
            return []
            
    return _proc_cache_data[:limit]

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el nivel de estrés de memoria basado en el porcentaje disponible."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0: return "info"
    available = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"

def _generate_diagnostics_lines(snapshot: MemorySnapshot) -> List[str]:
    """Crea la lista de líneas explicativas que componen el informe visual."""
    diagnostics: Dict[str, str] = {
        "ok": "Estado: holgado. La memoria ocupada por caché mejora la velocidad.",
        "info": "Estado: normal. Windows gestiona la memoria de forma eficiente.",
        "warning": "Estado: ajustado. Conviene cerrar aplicaciones innecesarias.",
        "danger": "Estado: crítico. El sistema recurre al archivo de paginación."
    }
    return [
        f"Memoria total: {format_bytes(snapshot.total)}",
        f"En uso: {format_bytes(snapshot.used)} ({snapshot.used_percent}%)",
        f"Disponible: {format_bytes(snapshot.available)} ({snapshot.available_percent}%)",
        diagnostics.get(pressure_level(snapshot), "")
    ]

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un informe textual de salud de memoria para el usuario final."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return ["No se pudo leer el estado de la memoria en este sistema."]
    
    report = _generate_diagnostics_lines(snapshot)
    
    if processes:
        for proc in processes[:3]:
            report.append(f"  Mayor consumo: {proc.name} (PID {proc.pid}) — {proc.working_set_mb} MB")
            
    return report

def _is_system_process(pid: int) -> bool:
    """Verifica si el PID corresponde a un proceso crítico del SO o a la app actual."""
    return isinstance(pid, int) and (pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid())

def _get_process_path(proc_handle: ctypes.c_void_p) -> Optional[Path]:
    """Obtiene la ruta absoluta del ejecutable desde un handle de proceso (PSAPI)."""
    if not proc_handle: return None
    psapi = getattr(ctypes.windll, "psapi", None)
    if not psapi or not hasattr(psapi, "GetModuleFileNameExW"): return None
    
    buf = ctypes.create_unicode_buffer(1024)
    try:
        chars_written = psapi.GetModuleFileNameExW(proc_handle, None, buf, 1024)
    except (ValueError, TypeError, ctypes.ArgumentError):
        return None
    
    if 0 < chars_written < 1024:
        path_str = buf.value
        # Filtra rutas de dispositivo, UNC o caracteres no imprimibles
        if not path_str or any(path_str.startswith(p) for p in ("\\\\", "\\??\\", "\\Device\\", "\\\\?\\")):
            return None
        if any(ord(c) < 32 for c in path_str):
            return None
        
        try:
            p = Path(path_str).resolve(strict=False)
            if not p.is_absolute():
                return None
            return p
        except (OSError, RuntimeError):
            return None
    return None

def _is_safe_to_trim(proc_handle: ctypes.c_void_p) -> Tuple[bool, Optional[str]]:
    """Valida que el proceso objetivo sea seguro para manipular su working set."""
    kernel32 = ctypes.windll.kernel32
    exit_code = ctypes.c_ulong()
    try:
        if not kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(exit_code)):
            return False, "Imposible verificar estado."
    except (ctypes.ArgumentError, OSError):
        return False, "Error de sistema al verificar estado."
        
    if exit_code.value != STILL_ACTIVE_EXIT_CODE:
        return False, "El proceso no está activo."
        
    exec_path = _get_process_path(proc_handle)
    if not exec_path or is_protected_path(str(exec_path)) or not is_safe_to_modify(str(exec_path)):
        return False, "Acceso no autorizado o ruta protegida del sistema."
    
    return True, None

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """Ejecuta la API EmptyWorkingSet tras validar la seguridad del proceso."""
    if not _is_windows: return False, "Operación solo soportada en Windows."
    try:
        target_pid = int(pid)
    except (ValueError, TypeError):
        return False, "PID no válido."

    if _is_system_process(target_pid): 
        return False, "Proceso crítico protegido."
    
    kernel32 = ctypes.windll.kernel32
    proc_handle = ctypes.c_void_p(kernel32.OpenProcess(SAFE_ACCESS_MASK, False, target_pid))
    if not proc_handle: 
        return False, "Acceso denegado al proceso."
    
    try:
        psapi = getattr(ctypes.windll, "psapi", None)
        if not psapi or not hasattr(psapi, "EmptyWorkingSet"):
            return False, "Función de sistema no disponible."

        is_safe, err = _is_safe_to_trim(proc_handle)
        if not is_safe: return False, err or "Verificación de seguridad fallida."
        
        if not psapi.EmptyWorkingSet(proc_handle): 
            return False, "El sistema denegó la operación de liberación."
            
        return True, f"Working set liberado. {TRIM_WARNING}"
    finally:
        kernel32.CloseHandle(proc_handle)
