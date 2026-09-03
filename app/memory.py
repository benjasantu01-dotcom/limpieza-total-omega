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

# Definición de tipos para seguridad semántica en cálculos de memoria
BytesValue = NewType("BytesValue", int)
MegabytesValue = NewType("MegabytesValue", float)

BYTES_IN_MB: Final[int] = 1024 * 1024
BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")

# Máscaras de acceso para operaciones de proceso seguro en Win32
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x0100
SAFE_ACCESS_MASK: Final[int] = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

STILL_ACTIVE_EXIT_CODE: Final[int] = 259
SYSTEM_CRITICAL_PIDS: Set[int] = {0, 4}

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
    Estructura de datos utilizada por GlobalMemoryStatusEx para reportar el
    estado físico y virtual de la memoria del sistema en Windows.
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
    """Representación inmutable de la salud de la memoria RAM global."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula memoria ocupada total (física). Garantiza resultado no negativo."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Calcula el porcentaje de RAM física ocupada, normalizado a 0-100."""
        if self.total <= 0: return 0.0
        ratio: float = float(self.used) / float(self.total)
        return round(ratio * 100, 1)

    @property
    def available_percent(self) -> float:
        """Calcula el porcentaje de RAM física disponible, normalizado a 0-100."""
        if self.total <= 0: return 0.0
        ratio: float = float(self.available) / float(self.total)
        return round(ratio * 100, 1)

@dataclass
class ProcessMemory:
    """Metadatos básicos de consumo de memoria de un proceso individual."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Calcula la conversión de bytes a MiB para reportes de usuario."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte bytes a una representación legible (ej. 1.2 MB) mediante logaritmo base 1024."""
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"

@lru_cache(maxsize=1)
def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """
    Instancia y pre-configura la estructura de memoria de Windows.
    El campo dwLength es obligatorio para que la API de Win32 acepte la estructura.
    """
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return stat

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """
    Analizador determinista del contenido de /proc/meminfo.
    Convierte el formato de texto plano de Linux en un objeto MemorySnapshot unificado.
    """
    if not isinstance(meminfo_text, str) or not meminfo_text:
        return MemorySnapshot(BytesValue(0), BytesValue(0))
    
    metric_map: Dict[str, int] = {"MemTotal": 0, "MemAvailable": 0, "MemFree": 0, "Cached": 0}
    
    for line in meminfo_text.splitlines():
        if ":" not in line: continue
        parts = line.split(":", 1)
        k_normalized = parts[0].strip()
        
        if k_normalized in metric_map:
            val_parts = parts[1].split()
            if val_parts and val_parts[0].isdigit():
                metric_map[k_normalized] = int(val_parts[0]) * 1024
            
    total_mem = metric_map["MemTotal"]
    if total_mem <= 0: 
        return MemorySnapshot(BytesValue(0), BytesValue(0))
    
    available = metric_map["MemAvailable"] if metric_map["MemAvailable"] > 0 else metric_map["MemFree"]
    return MemorySnapshot(
        total=BytesValue(total_mem), 
        available=BytesValue(min(available, total_mem)), 
        cached=BytesValue(max(0, metric_map["Cached"]))
    )

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """
    Convierte la salida de consola de PowerShell en una lista de objetos ProcessMemory.
    Implementa filtros de seguridad contra procesos críticos y rutas protegidas.
    """
    if not isinstance(raw_csv_text, str) or not raw_csv_text.strip():
        return []
    
    proc_list: List[ProcessMemory] = []
    for line in raw_csv_text.splitlines():
        line = line.strip()
        if not line: continue
        
        parts = [p.strip().strip("'\"") for p in line.split(",", 2)]
        if len(parts) < 3: continue
        
        try:
            name_val = str(parts[0])
            pid_val = int(parts[1])
            ws_val = int(parts[2])
            
            if not name_val: continue
            
            if is_protected_path(name_val): continue
            
            if pid_val > 0 and ws_val >= 0 and pid_val not in SYSTEM_CRITICAL_PIDS:
                proc_list.append(ProcessMemory(name=name_val, pid=pid_val, working_set=BytesValue(ws_val)))
        except (ValueError, TypeError):
            continue
    
    proc_list.sort(key=lambda p: p.working_set, reverse=True)
    return proc_list[:max(0, int(limit))]

def _read_windows_snapshot() -> MemorySnapshot:
    """Invoca la API de Windows GlobalMemoryStatusEx mediante ctypes para lectura de estado global."""
    kernel32 = ctypes.windll.kernel32
    if not hasattr(kernel32, "GlobalMemoryStatusEx"):
        return MemorySnapshot(BytesValue(0), BytesValue(0))
    
    try:
        stat = _create_mem_status_ex()
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return MemorySnapshot(total=BytesValue(stat.ullTotalPhys), available=BytesValue(stat.ullAvailPhys))
    except (AttributeError, ValueError, TypeError, OverflowError, OSError):
        pass
    return MemorySnapshot(BytesValue(0), BytesValue(0))

_snap_cache_time: float = 0.0
_snap_cache_data: Optional[MemorySnapshot] = None

def read_snapshot() -> MemorySnapshot:
    """
    Lee el estado actual de la memoria. Utiliza caché por 5 segundos para evitar 
    sobrecarga en el acceso a APIs de sistema o lectura de archivos proc.
    """
    global _snap_cache_time, _snap_cache_data
    now = time.time()
    if (now - _snap_cache_time) < 5 and _snap_cache_data:
        return _snap_cache_data

    if os.name == "nt": 
        _snap_cache_data = _read_windows_snapshot()
    else:
        try:
            mem_path = Path("/proc/meminfo")
            if mem_path.exists():
                content = mem_path.read_text(encoding="utf-8")
                _snap_cache_data = parse_linux_meminfo(content) if content else MemorySnapshot(BytesValue(0), BytesValue(0))
            else:
                _snap_cache_data = MemorySnapshot(BytesValue(0), BytesValue(0))
        except (OSError, UnicodeDecodeError, RuntimeError):
            _snap_cache_data = MemorySnapshot(BytesValue(0), BytesValue(0))
    
    _snap_cache_time = now
    return _snap_cache_data or MemorySnapshot(BytesValue(0), BytesValue(0))

_proc_cache_time: float = 0.0
_proc_cache_data: List[ProcessMemory] = []

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Consulta procesos pesados mediante PowerShell. Cachea resultados por 60s."""
    global _proc_cache_time, _proc_cache_data
    if os.name != "nt": return []
    
    now = time.time()
    if (now - _proc_cache_time) > 60:
        cmd = [
            'powershell', '-NoProfile', '-NonInteractive', '-Command', 
            "Get-Process | Select-Object Name, Id, WorkingSet | ForEach-Object { \"$($_.Name),$($_.Id),$($_.WorkingSet)\" }"
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
            if proc.returncode == 0 and proc.stdout:
                _proc_cache_data = parse_windows_process_csv(proc.stdout)
                _proc_cache_time = now
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired): 
            pass
            
    return _proc_cache_data[:limit]

@lru_cache(maxsize=2)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el nivel de presión de memoria en cuatro estados semánticos."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0: return "info"
    available = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera reporte textual legible con recomendaciones basadas en el estado de memoria."""
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
    """Verifica si un PID es crítico (kernel/idle) o el proceso actual."""
    return isinstance(pid, int) and (pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid())

def _get_process_path(proc_handle: wintypes.HANDLE) -> Optional[str]:
    """Intenta recuperar la ruta absoluta del ejecutable desde un handle de proceso."""
    if not proc_handle: return None
    kernel32 = ctypes.windll.kernel32
    if not hasattr(kernel32, "QueryFullProcessImageNameW"): return None
    
    buf = ctypes.create_unicode_buffer(4096)
    size = ctypes.c_ulong(4096)
    try:
        if kernel32.QueryFullProcessImageNameW(proc_handle, 0, buf, ctypes.byref(size)) > 0:
            return str(buf.value)
    except (OSError, ctypes.ArgumentError, ValueError, BufferError): 
        pass
    return None

def _is_safe_to_trim(proc_handle: wintypes.HANDLE, pid: int) -> Tuple[bool, Optional[str]]:
    """
    Validación de seguridad antes de modificar el working set.
    Verifica estado activo, mismatches de PID y exclusión de rutas protegidas.
    """
    if not proc_handle: return False, "Handle inválido."
    kernel32 = ctypes.windll.kernel32
    
    try:
        if kernel32.GetProcessId(proc_handle) != pid: return False, "Mismatch de PID."
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(exit_code)) or exit_code.value != STILL_ACTIVE_EXIT_CODE:
            return False, "El proceso no está activo."
        exec_path = _get_process_path(proc_handle)
        if not exec_path or is_protected_path(exec_path) or not is_safe_to_modify(exec_path):
            return False, "Operación denegada por política de seguridad."
        return True, None
    except (OSError, ctypes.ArgumentError, Exception):
        return False, "Error interno durante la verificación de integridad."

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Intenta liberar páginas de memoria física del working set de un proceso.
    Requiere permisos de administrador y validaciones de seguridad previas.
    """
    if os.name != "nt": return False, "Operación solo soportada en Windows."
    kernel32 = ctypes.windll.kernel32
    psapi = getattr(ctypes.windll, "psapi", None)
    if not psapi or not hasattr(psapi, "EmptyWorkingSet"): return False, "APIs no disponibles."
    
    try:
        target_pid = int(pid)
    except (ValueError, TypeError):
        return False, "PID no válido."
        
    if _is_system_process(target_pid) or is_protected_path(str(target_pid)): 
        return False, "Proceso protegido o inválido."
    
    proc_handle = kernel32.OpenProcess(SAFE_ACCESS_MASK, False, target_pid)
    if not proc_handle: 
        return False, "Acceso denegado."
    
    try:
        is_safe, error_reason = _is_safe_to_trim(proc_handle, target_pid)
        if not is_safe: 
            return False, error_reason or "Verificación de seguridad fallida."
        if not psapi.EmptyWorkingSet(proc_handle): 
            return False, "El sistema denegó la operación (EmptyWorkingSet falló)."
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (OSError, ctypes.ArgumentError, Exception) as e:
        return False, f"Error del sistema: {type(e).__name__}"
    finally:
        kernel32.CloseHandle(proc_handle)
