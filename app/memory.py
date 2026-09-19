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

BYTES_IN_MB: Final[int] = 1024 * 1024
BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")

# Máscaras de acceso Win32 para operaciones seguras en procesos:
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x100
SAFE_ACCESS_MASK: Final[int] = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

STILL_ACTIVE_EXIT_CODE: Final[int] = 259
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}
ERROR_ACCESS_DENIED: Final[int] = 5

PS_QUERY_CMD: Final[List[str]] = [
    'powershell', '-NoProfile', '-NonInteractive', '-Command', 
    'Get-Process | Where-Object { $_.Id -notin 0,4 } | Select-Object -First 50 | ForEach-Object { "$($_.Name),$($_.Id),$($_.WorkingSet)" }'
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
    """Estructura Win32 (GlobalMemoryStatusEx) para mapear memoria del sistema."""
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
        """Convierte bytes de memoria de trabajo a MiB."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte un valor numérico a representación humana (SI)."""
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"

def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """Inicializa la estructura Win32 necesaria para la llamada GlobalMemoryStatusEx."""
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return stat

def _kb_to_bytes(kb_str: str) -> BytesValue:
    """
    Convierte cadenas del estilo '1024 kB' a bytes. 
    Retorna 0 si el formato es inválido o el valor desborda.
    """
    if not isinstance(kb_str, str): return BytesValue(0)
    # Extrae solo dígitos para robustez ante etiquetas de unidad variadas
    val_str = "".join(c for c in kb_str if c.isdigit())
    if not val_str: return BytesValue(0)
    try:
        return BytesValue(int(val_str) * 1024)
    except (ValueError, OverflowError):
        return BytesValue(0)

_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Parsea el formato estándar de /proc/meminfo a un objeto MemorySnapshot."""
    if not isinstance(meminfo_text, str) or not meminfo_text.strip():
        return _EMPTY_SNAPSHOT
    
    metrics: Dict[str, BytesValue] = {}
    for line in meminfo_text.splitlines():
        if ":" not in line: continue
        parts = line.split(":", 1)
        if len(parts) == 2:
            key, value_part = parts
            metrics[key.strip()] = _kb_to_bytes(value_part)
            
    total = metrics.get("MemTotal", BytesValue(0))
    if total <= 0: return _EMPTY_SNAPSHOT
    
    available = metrics.get("MemAvailable", metrics.get("MemFree", BytesValue(0)))
    cached = metrics.get("Cached", BytesValue(0))
    
    return MemorySnapshot(
        total=total, 
        available=BytesValue(min(available, total)), 
        cached=BytesValue(max(0, cached))
    )

def _is_valid_process_entry(fields: List[str]) -> Optional[ProcessMemory]:
    """Valida integridad de registro CSV y filtra procesos según seguridad."""
    if fields is None or len(fields) < 3:
        return None
    try:
        name_raw = fields[0].strip()
        if not name_raw: return None
        
        if not fields[1].isdigit() or not fields[2].isdigit():
            return None
            
        pid_val, ws_val = int(fields[1]), int(fields[2])
        
        # Filtros de seguridad: ignorar procesos inválidos, protegidos o con estado negativo
        if pid_val <= 0 or ws_val < 0 or is_protected_path(name_raw):
            return None
            
        return ProcessMemory(name=name_raw, pid=pid_val, working_set=BytesValue(ws_val))
    except (ValueError, TypeError):
        return None

def _clean_csv_field(field: str) -> str:
    """Limpia caracteres de escape y espacios en campos de texto CSV."""
    return field.strip().strip("'\" ") if field else ""

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Convierte volcado PowerShell en lista de procesos, ordenados de mayor a menor uso."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text.strip():
        return []
    
    results: List[ProcessMemory] = []
    for line in raw_csv_text.splitlines():
        if not line.strip(): continue
        try:
            parts = [_clean_csv_field(x) for x in line.split(",")]
            entry = _is_valid_process_entry(parts)
            if entry:
                results.append(entry)
        except Exception:
            continue
    
    results.sort(key=lambda p: p.working_set, reverse=True)
    return results[:limit]

def _read_windows_snapshot() -> MemorySnapshot:
    """
    Consulta la API Win32 GlobalMemoryStatusEx para obtener estadísticas de RAM.
    Se asegura de verificar la existencia de la función y de la estructura de datos.
    """
    kernel32 = ctypes.windll.kernel32
    if not hasattr(kernel32, "GlobalMemoryStatusEx"):
        return _EMPTY_SNAPSHOT
    
    stat = _create_mem_status_ex()
    try:
        # La función devuelve un valor booleano indicando éxito
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
    """Implementa TTL de 5 segundos para lecturas de estado del sistema."""
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
    """Interfaz pública: retorna estado actual de RAM con caché temporal."""
    return _get_cached_snapshot(int(time.time() / 5))

_proc_cache_time: float = 0.0
_proc_cache_data: List[ProcessMemory] = []

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Lista los procesos más pesados mediante PowerShell con caché de 60s."""
    global _proc_cache_time, _proc_cache_data
    if not _is_windows: return []
    
    if (time.time() - _proc_cache_time) > 60:
        try:
            proc = subprocess.run(PS_QUERY_CMD, capture_output=True, text=True, timeout=3, check=False)
            if proc.returncode == 0 and proc.stdout:
                _proc_cache_data = parse_windows_process_csv(proc.stdout, limit=50)
                _proc_cache_time = time.time()
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired): 
            _proc_cache_data = []
            
    return _proc_cache_data[:limit]

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Determina severidad de uso de RAM basándose en disponibilidad relativa."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0: return "info"
    available = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Crea un informe textual legible para el usuario final."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return ["No se pudo leer el estado de la memoria en este sistema."]
    
    report: List[str] = [
        f"Memoria total: {format_bytes(snapshot.total)}",
        f"En uso: {format_bytes(snapshot.used)} ({snapshot.used_percent}%)",
        f"Disponible: {format_bytes(snapshot.available)} ({snapshot.available_percent}%)",
    ]
    
    diagnostics: Dict[str, str] = {
        "ok": "Estado: holgado. La memoria ocupada por caché mejora la velocidad.",
        "info": "Estado: normal. Windows gestiona la memoria de forma eficiente.",
        "warning": "Estado: ajustado. Conviene cerrar aplicaciones innecesarias.",
        "danger": "Estado: crítico. El sistema recurre al archivo de paginación."
    }
    
    report.append(diagnostics.get(pressure_level(snapshot), ""))
    
    if processes:
        for proc in processes[:3]:
            report.append(f"  Mayor consumo: {proc.name} (PID {proc.pid}) — {proc.working_set_mb} MB")
            
    return report

def _is_system_process(pid: int) -> bool:
    """Verifica si un PID pertenece a procesos protegidos o a la app actual."""
    return isinstance(pid, int) and (pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid())

def _get_process_path(proc_handle: int) -> Optional[Path]:
    """Resuelve la ruta física de un proceso mediante Psapi.GetModuleFileNameExW."""
    if not proc_handle: return None
    try:
        psapi = getattr(ctypes.windll, "psapi", None)
        if not psapi or not hasattr(psapi, "GetModuleFileNameExW"): return None
        
        buf = ctypes.create_unicode_buffer(1024)
        if psapi.GetModuleFileNameExW(ctypes.c_void_p(proc_handle), None, buf, 1024) > 0:
            path_str = buf.value
            if not path_str or any(path_str.startswith(prefix) for prefix in ("\\\\", "\\??\\", "\\Device\\")):
                return None
            
            if not os.path.isabs(path_str): return None
            
            p = Path(path_str)
            if not p.is_file() or p.is_symlink(): return None
            
            # Verificación de seguridad robusta
            p_resolved = p.resolve(strict=False)
            if is_protected_path(str(p_resolved)) or not is_safe_to_modify(str(p_resolved)): 
                return None
            
            return p_resolved
    except (OSError, ctypes.ArgumentError, ValueError, AttributeError, RuntimeError):
        pass
    return None

def _is_safe_to_trim(proc_handle: int) -> Tuple[bool, Optional[str]]:
    """Verifica permisos y estado de un proceso antes de intentar liberar su RAM."""
    if not isinstance(proc_handle, int) or proc_handle == 0: return False, "Handle inválido."
    kernel32 = ctypes.windll.kernel32
    
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(ctypes.c_void_p(proc_handle), ctypes.byref(exit_code)):
            return False, f"Imposible verificar estado (Error {kernel32.GetLastError()})."
            
        if exit_code.value != STILL_ACTIVE_EXIT_CODE:
            return False, "El proceso no está activo."
            
        exec_path = _get_process_path(proc_handle)
        if not exec_path:
            return False, "Acceso denegado o ejecutable no localizable."
        
        if not is_safe_to_modify(str(exec_path)):
            return False, "Operación no autorizada sobre este proceso."
        
        return True, None
    except (AttributeError, ValueError, ctypes.ArgumentError, OSError):
        return False, "Error interno durante la verificación de integridad."

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """Ejecuta EmptyWorkingSet con validaciones de seguridad exhaustivas."""
    if not _is_windows: return False, "Operación solo soportada en Windows."
    
    try:
        target_pid = int(pid)
    except (ValueError, TypeError):
        return False, "PID proporcionado no es un número válido."

    if _is_system_process(target_pid): 
        return False, "No se permite modificar procesos críticos del sistema."
    
    kernel32 = ctypes.windll.kernel32
    proc_handle = kernel32.OpenProcess(SAFE_ACCESS_MASK, False, target_pid)
    if not proc_handle: 
        return False, f"Acceso denegado (Error {kernel32.GetLastError()})."
    
    try:
        psapi = getattr(ctypes.windll, "psapi", None)
        if not psapi or not hasattr(psapi, "EmptyWorkingSet"): return False, "APIs no disponibles."
        
        is_safe, error_reason = _is_safe_to_trim(proc_handle)
        if not is_safe: 
            return False, error_reason or "Verificación de seguridad fallida."
        
        if not psapi.EmptyWorkingSet(ctypes.c_void_p(proc_handle)): 
            error_code = kernel32.GetLastError()
            return False, f"Sistema denegó la operación (Error {error_code})."
            
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (ctypes.ArgumentError, OSError, ValueError, TypeError) as e:
        return False, f"Error de ejecución: {str(e)}"
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(proc_handle))
