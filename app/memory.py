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
# BytesValue: Representa el tamaño crudo en bytes.
# MegabytesValue: Representa tamaño ya convertido a MiB para presentación.
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

# Comando pre-construido para evitar recrear listas en el loop de performance
PS_QUERY_CMD: Final[List[str]] = [
    'powershell', '-NoProfile', '-NonInteractive', '-Command', 
    'Get-Process | ForEach-Object { "$($_.Name),$($_.Id),$($_.WorkingSet)" }'
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
    """
    Estructura de datos Win32 (MEMORYSTATUSEX) para GlobalMemoryStatusEx.
    Almacena las estadísticas de memoria física y virtual del sistema.
    """
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
    """
    Representación inmutable de la salud de la memoria RAM global.
    Calcula dinámicamente el uso y porcentaje basado en el estado del kernel.
    """
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula memoria ocupada: total menos disponible."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Calcula el porcentaje de uso de RAM física (0.0 a 100.0)."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Calcula el porcentaje de disponibilidad de RAM física (0.0 a 100.0)."""
        if self.total <= 0: return 0.0
        return round((float(self.available) / float(self.total)) * 100, 1)

@dataclass
class ProcessMemory:
    """
    Metadatos del consumo de memoria de un proceso individual.
    Utilizado para mapear la salida de herramientas de consulta externa.
    """
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Provee el valor de Working Set convertido a MiB para visualización."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte bytes a una cadena legible (ej: 1.5 MB)."""
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"

def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """Configura la estructura Win32 inicializando el campo dwLength."""
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return stat

def _kb_to_bytes(kb_str: str) -> BytesValue:
    """Parsea una cadena numérica con unidad (o no) a bytes."""
    if not isinstance(kb_str, str): return BytesValue(0)
    digits = "".join(c for c in kb_str if c.isdigit())
    return BytesValue(int(digits) * 1024) if digits else BytesValue(0)

_win_mem_buffer: MEMORYSTATUSEX = _create_mem_status_ex()
_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """
    Transforma el texto de /proc/meminfo en un objeto MemorySnapshot.
    Utiliza lru_cache porque las métricas de kernel Linux no cambian instantáneamente.
    """
    if not isinstance(meminfo_text, str) or not meminfo_text:
        return _EMPTY_SNAPSHOT
    
    metrics: Dict[str, BytesValue] = {}
    
    for line in meminfo_text.splitlines():
        if ":" not in line: 
            continue
        try:
            key, value_part = line.split(":", 1)
            metrics[key.strip()] = _kb_to_bytes(value_part)
        except (ValueError, TypeError, KeyError):
            continue
            
    total = metrics.get("MemTotal", BytesValue(0))
    if not isinstance(total, int) or total <= 0: 
        return _EMPTY_SNAPSHOT
    
    available = metrics.get("MemAvailable", metrics.get("MemFree", BytesValue(0)))
    cached = metrics.get("Cached", BytesValue(0))
    
    return MemorySnapshot(
        total=total, 
        available=BytesValue(min(available, total)), 
        cached=BytesValue(max(0, cached))
    )

def _is_valid_process_entry(name: object, pid_str: object, ws_str: object) -> Optional[ProcessMemory]:
    """
    Valida la integridad de una fila proveniente de PowerShell.
    Filtra procesos de sistema (PIDs críticos) y rutas protegidas (safety).
    """
    if name is None or pid_str is None or ws_str is None:
        return None
    if not isinstance(name, str) or not str(pid_str).isdigit() or not str(ws_str).isdigit():
        return None
    try:
        pid_val, ws_val = int(pid_str), int(ws_str)
        name_clean = name.strip()
        if not name_clean or pid_val <= 0 or ws_val < 0 or pid_val in SYSTEM_CRITICAL_PIDS:
            return None
        if is_protected_path(name_clean):
            return None
        return ProcessMemory(name=name_clean, pid=pid_val, working_set=BytesValue(ws_val))
    except (ValueError, TypeError):
        return None

def _clean_csv_field(field: str) -> str:
    """Elimina delimitadores de texto (comillas/espacios) de campos de datos."""
    return field.strip().strip("'\" ")

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """
    Convierte la salida cruda de PowerShell en una lista de objetos ProcessMemory.
    Ordena la lista por Working Set de forma descendente antes de retornar.
    """
    if not isinstance(raw_csv_text, str) or not raw_csv_text.strip():
        return []
    
    # Generador para filtrar y validar líneas de forma eficiente antes de materializar la lista
    gen = (
        _is_valid_process_entry(*[_clean_csv_field(x) for x in line.split(",")][:3])
        for line in raw_csv_text.splitlines() if "," in line
    )
    processed = [p for p in gen if p]
    processed.sort(key=lambda p: p.working_set, reverse=True)
    return processed[:limit]

def _read_windows_snapshot() -> MemorySnapshot:
    """Interfaz con la API nativa de Windows (kernel32.dll) para RAM total/disponible."""
    kernel32 = ctypes.windll.kernel32
    if not hasattr(kernel32, "GlobalMemoryStatusEx"):
        return _EMPTY_SNAPSHOT
    
    try:
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(_win_mem_buffer)):
            total = _win_mem_buffer.ullTotalPhys
            avail = _win_mem_buffer.ullAvailPhys
            if total > 0 and avail <= total:
                return MemorySnapshot(total=BytesValue(total), available=BytesValue(avail))
    except (AttributeError, OSError, ctypes.ArgumentError):
        pass
    return _EMPTY_SNAPSHOT

_snap_cache_time: float = 0.0
_snap_cache_data: Optional[MemorySnapshot] = None
_linux_available: bool = True

def read_snapshot() -> MemorySnapshot:
    """
    Retorna el estado global de memoria. Cachea el resultado 5 segundos
    para evitar latencia en consultas frecuentes desde la UI.
    """
    global _snap_cache_time, _snap_cache_data, _linux_available
    now = time.time()
    if (now - _snap_cache_time) < 5 and _snap_cache_data is not None:
        return _snap_cache_data

    snapshot = _EMPTY_SNAPSHOT
    if _is_windows: 
        snapshot = _read_windows_snapshot()
    elif _linux_available:
        try:
            content = _linux_mem_path.read_text(encoding="utf-8")
            snapshot = parse_linux_meminfo(content)
        except (OSError, UnicodeDecodeError, RuntimeError):
            _linux_available = False
            snapshot = _EMPTY_SNAPSHOT
    
    if snapshot != _EMPTY_SNAPSHOT:
        _snap_cache_data = snapshot
        _snap_cache_time = now
    return _snap_cache_data if _snap_cache_data else _EMPTY_SNAPSHOT

_proc_cache_time: float = 0.0
_proc_cache_data: List[ProcessMemory] = []

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """
    Obtiene los procesos de mayor consumo usando PowerShell.
    La caché se extiende a 60s dado que enumerar procesos es costoso.
    """
    global _proc_cache_time, _proc_cache_data
    if not _is_windows: return []
    
    if (time.time() - _proc_cache_time) < 60:
        return _proc_cache_data[:limit]
    
    try:
        proc = subprocess.run(PS_QUERY_CMD, capture_output=True, text=True, timeout=3, check=False)
        if proc.returncode == 0 and proc.stdout:
            _proc_cache_data = parse_windows_process_csv(proc.stdout, limit=limit)
            _proc_cache_time = time.time()
        else:
            _proc_cache_data = []
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired): 
        _proc_cache_data = []
            
    return _proc_cache_data[:limit]

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el estrés de memoria para la interfaz (colores/estados)."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0: return "info"
    available = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Crea un reporte de texto para el usuario basándose en el estado actual."""
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
    """Valida si un proceso debe protegerse de manipulaciones externas."""
    return isinstance(pid, int) and (pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid())

def _get_process_path(proc_handle: int) -> Optional[Path]:
    """
    Resuelve el ejecutable de un proceso mediante PSAPI.
    Aplica filtros de seguridad: sin rutas UNC, ni symlinks, ni áreas protegidas.
    """
    if not proc_handle or proc_handle <= 0: return None
    psapi = getattr(ctypes.windll, "psapi", None)
    if not psapi or not hasattr(psapi, "GetModuleFileNameExW"): return None
    
    buf = ctypes.create_unicode_buffer(1024)
    try:
        if psapi.GetModuleFileNameExW(proc_handle, None, buf, 1024) > 0:
            path_str = str(buf.value)
            # Prevenir rutas UNC o dispositivos especiales
            if path_str.startswith(("\\\\", "\\??\\")): return None
            
            p = Path(path_str)
            if not p.exists() or not p.is_file(): return None
            # Evitar symlinks/reparse points para prevenir recursiones o ataques de path
            if p.is_symlink(): return None
            
            p_resolved = p.resolve(strict=False)
            # Defensa contra rutas del sistema
            if is_protected_path(str(p_resolved)): return None
            
            return p_resolved
    except (OSError, ctypes.ArgumentError, ValueError, MemoryError):
        pass
    return None

def _is_safe_to_trim(proc_handle: int) -> Tuple[bool, Optional[str]]:
    """
    Verificación de seguridad para la operación 'EmptyWorkingSet'.
    Comprueba si el proceso sigue vivo y si su ejecutable es modificable.
    """
    if not isinstance(proc_handle, int) or proc_handle <= 0: return False, "Handle inválido."
    kernel32 = ctypes.windll.kernel32
    
    try:
        exit_code = ctypes.c_ulong()
        # Verificar estado operativo del proceso
        if not kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(exit_code)):
            return False, f"Imposible verificar estado (Error {kernel32.GetLastError()})."
            
        if exit_code.value != STILL_ACTIVE_EXIT_CODE:
            return False, "El proceso no está activo."
            
        exec_path = _get_process_path(proc_handle)
        if not exec_path:
            return False, "Acceso denegado o ejecutable no localizable."
        
        # Validar ruta contra la política de seguridad central
        if not is_safe_to_modify(str(exec_path)):
            return False, "Operación denegada: ruta protegida."
            
        return True, None
    except (AttributeError, ValueError, ctypes.ArgumentError, OSError):
        return False, "Error interno durante la verificación de integridad."

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Acción: Fuerza al SO a reducir el Working Set del proceso PID.
    Requiere validación de seguridad previa y privilegios del handle abierto.
    """
    if not _is_windows: return False, "Operación solo soportada en Windows."
    
    try:
        target_pid = int(pid)
    except (ValueError, TypeError):
        return False, "PID proporcionado no es un número válido."

    if _is_system_process(target_pid): 
        return False, "No se permite modificar procesos críticos del sistema."

    kernel32 = ctypes.windll.kernel32
    psapi = getattr(ctypes.windll, "psapi", None)
    if not psapi or not hasattr(psapi, "EmptyWorkingSet"): return False, "APIs no disponibles."
    
    proc_handle = kernel32.OpenProcess(SAFE_ACCESS_MASK, False, target_pid)
    if not proc_handle: 
        return False, f"Acceso denegado (Error {kernel32.GetLastError()})."
    
    try:
        is_safe, error_reason = _is_safe_to_trim(proc_handle)
        if not is_safe: 
            return False, error_reason or "Verificación de seguridad fallida."
        
        if not psapi.EmptyWorkingSet(proc_handle): 
            error_code = kernel32.GetLastError()
            return False, f"Sistema denegó la operación (Error {error_code})."
            
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (ctypes.ArgumentError, OSError, ValueError, TypeError) as e:
        return False, f"Error de sistema: {str(e)}"
    finally:
        if proc_handle:
            kernel32.CloseHandle(proc_handle)
