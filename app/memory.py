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
import bisect
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

# Comando optimizado: filtra IDs críticos y vacíos directamente en el host para reducir carga en Python
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
    """Estructura Win32 (GlobalMemoryStatusEx) para estadísticas de memoria."""
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
    """Estado inmutable de la RAM global calculado tras una lectura de kernel."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula memoria en uso restando la disponible a la total."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Porcentaje de RAM utilizada respecto al total."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Porcentaje de RAM disponible respecto al total."""
        if self.total <= 0: return 0.0
        return round((float(self.available) / float(self.total)) * 100, 1)

@dataclass
class ProcessMemory:
    """Consumo de memoria de un proceso identificado por PID."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Retorna el valor de Working Set convertido de bytes a MiB."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte un valor numérico de bytes a una cadena legible con sufijo SI."""
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"

def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """Prepara la estructura nativa para la llamada al kernel de Windows."""
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return stat

def _kb_to_bytes(kb_str: str) -> BytesValue:
    """Convierte una cadena de texto representando KB a bytes."""
    if not isinstance(kb_str, str): return BytesValue(0)
    val_str = "".join(c for c in kb_str if c.isdigit())
    if not val_str: return BytesValue(0)
    try:
        return BytesValue(int(val_str) * 1024)
    except (ValueError, OverflowError):
        return BytesValue(0)

_win_mem_buffer: MEMORYSTATUSEX = _create_mem_status_ex()
_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Parsea el contenido crudo de /proc/meminfo para extraer métricas de RAM."""
    if not isinstance(meminfo_text, str) or not meminfo_text.strip():
        return _EMPTY_SNAPSHOT
    
    metrics: Dict[str, BytesValue] = {}
    
    for line in meminfo_text.splitlines():
        if ":" not in line: 
            continue
        try:
            parts = line.split(":", 1)
            if len(parts) == 2:
                key, value_part = parts
                metrics[key.strip()] = _kb_to_bytes(value_part)
        except Exception:
            continue
            
    total = metrics.get("MemTotal", BytesValue(0))
    if total <= 0: 
        return _EMPTY_SNAPSHOT
    
    available = metrics.get("MemAvailable", metrics.get("MemFree", BytesValue(0)))
    cached = metrics.get("Cached", BytesValue(0))
    
    return MemorySnapshot(
        total=total, 
        available=BytesValue(min(available, total)), 
        cached=BytesValue(max(0, cached))
    )

def _is_valid_process_entry(fields: List[str]) -> Optional[ProcessMemory]:
    """Valida los campos crudos extraídos de PowerShell para un proceso."""
    if len(fields) < 3:
        return None
    try:
        name_clean = fields[0].strip()
        pid_val = int(fields[1])
        ws_val = int(fields[2])
        
        if not name_clean or pid_val <= 0 or ws_val < 0 or is_protected_path(name_clean):
            return None
        return ProcessMemory(name=name_clean, pid=pid_val, working_set=BytesValue(ws_val))
    except (ValueError, TypeError):
        return None

def _clean_csv_field(field: str) -> str:
    """Limpia caracteres de escape y espacios de campos CSV de PowerShell."""
    if not field: return ""
    return field.strip().strip("'\" ")

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Transforma la salida CSV de Get-Process en una lista de objetos ProcessMemory."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text.strip():
        return []
    
    top_procs: List[ProcessMemory] = []
    for line in raw_csv_text.splitlines():
        try:
            parts = [_clean_csv_field(x) for x in line.split(",")]
            entry = _is_valid_process_entry(parts)
            if entry:
                # Mantener orden descendente mediante insort (invertimos el valor para usar bisect default)
                bisect.insort(top_procs, entry, key=lambda p: -p.working_set)
                if len(top_procs) > limit:
                    top_procs.pop()
        except (AttributeError, IndexError, ValueError):
            continue
                
    return top_procs

def _read_windows_snapshot() -> MemorySnapshot:
    """Ejecuta la API Win32 GlobalMemoryStatusEx para obtener RAM física."""
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
    """Obtiene un snapshot global de RAM con una caché de 5 segundos."""
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
            if snapshot == _EMPTY_SNAPSHOT:
                _linux_available = False
        except (OSError, PermissionError, UnicodeDecodeError, RuntimeError):
            _linux_available = False
            snapshot = _EMPTY_SNAPSHOT
    
    if snapshot != _EMPTY_SNAPSHOT:
        _snap_cache_data = snapshot
        _snap_cache_time = now
    return _snap_cache_data if _snap_cache_data else _EMPTY_SNAPSHOT

_proc_cache_time: float = 0.0
_proc_cache_data: List[ProcessMemory] = []

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Obtiene los procesos más pesados mediante PowerShell, con caché de 60s."""
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
    """Evalúa el nivel de estrés de memoria basado en el porcentaje disponible."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0: return "info"
    available = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un reporte textual descriptivo sobre el estado de la memoria."""
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
    """Verifica si un PID pertenece a un proceso crítico para protegerlo."""
    return isinstance(pid, int) and (pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid())

def _get_process_path(proc_handle: int) -> Optional[Path]:
    """
    Resuelve la ruta absoluta del ejecutable de un proceso mediante PSAPI.
    
    Utiliza GetModuleFileNameExW para obtener la ruta y valida:
    1. Que no sea una ruta virtual o UNC (bloqueo de seguridad).
    2. Que el archivo exista y no sea un enlace simbólico.
    3. Que la ruta final pase el filtro de seguridad (is_safe_to_modify).
    """
    if not proc_handle or proc_handle <= 0: return None
    psapi = getattr(ctypes.windll, "psapi", None)
    if not psapi or not hasattr(psapi, "GetModuleFileNameExW"): return None
    
    buf = ctypes.create_unicode_buffer(1024)
    try:
        if psapi.GetModuleFileNameExW(proc_handle, None, buf, 1024) > 0:
            path_str = str(buf.value)
            # Defensa: Rechazar rutas UNC o de dispositivos virtuales antes de instanciar Path
            if any(path_str.startswith(prefix) for prefix in ("\\\\", "\\??\\", "\\Device\\")):
                return None
            
            p = Path(path_str)
            if not p.exists() or not p.is_file() or p.is_symlink(): return None
            
            p_resolved = p.resolve(strict=False)
            if is_protected_path(str(p_resolved)) or not is_safe_to_modify(str(p_resolved)): 
                return None
            
            return p_resolved
    except (OSError, ctypes.ArgumentError, ValueError, MemoryError):
        pass
    return None

def _is_safe_to_trim(proc_handle: int) -> Tuple[bool, Optional[str]]:
    """
    Verifica los requisitos de seguridad y estado antes de intentar un trim.
    
    1. Verifica mediante GetExitCodeProcess si el proceso sigue en ejecución.
    2. Valida la integridad de la ruta del ejecutable mediante _get_process_path.
    
    Retorna (True, None) si es seguro, o (False, razón) si está bloqueado.
    """
    if not isinstance(proc_handle, int) or proc_handle <= 0: return False, "Handle inválido."
    kernel32 = ctypes.windll.kernel32
    
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(exit_code)):
            return False, f"Imposible verificar estado (Error {kernel32.GetLastError()})."
            
        if exit_code.value != STILL_ACTIVE_EXIT_CODE:
            return False, "El proceso no está activo."
            
        exec_path = _get_process_path(proc_handle)
        if not exec_path:
            return False, "Acceso denegado o ejecutable no localizable."
        
        return True, None
    except (AttributeError, ValueError, ctypes.ArgumentError, OSError):
        return False, "Error interno durante la verificación de integridad."

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Solicita al kernel la liberación del working set de un proceso mediante `EmptyWorkingSet`.
    
    NOTA: Esta función interactúa con la API Win32 `psapi!EmptyWorkingSet`.
    El sistema operativo puede denegar esta petición si el proceso tiene un
    nivel de prioridad alto o es esencial para la estabilidad del sistema.
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
        return False, f"Acceso denegado o proceso inexistente (Error {kernel32.GetLastError()})."
    
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
        kernel32.CloseHandle(proc_handle)
