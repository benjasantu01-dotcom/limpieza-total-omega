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

# Máscaras de acceso Win32 para operaciones seguras en procesos
# 0x1000 (PROCESS_QUERY_LIMITED_INFORMATION): Info básica necesaria para lectura sin elevar privilegios
# 0x100 (PROCESS_SET_QUOTA): Permiso requerido estrictamente por la API EmptyWorkingSet
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x100
SAFE_ACCESS_MASK: Final[int] = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

STILL_ACTIVE_EXIT_CODE: Final[int] = 259
SYSTEM_CRITICAL_PIDS: Set[int] = {0, 4}
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
    """
    Representa el layout de memoria del sistema según la estructura MEMORYSTATUSEX 
    de la API de Windows para GlobalMemoryStatusEx.
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
    """Representación inmutable de la salud de la memoria RAM global del sistema."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula memoria ocupada total mediante la resta de total menos disponible."""
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
    """Metadatos del consumo de memoria de un proceso individual."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Provee el valor de Working Set convertido a MiB para visualización humana."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte un valor numérico de bytes a una cadena legible con unidad escalada."""
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"

def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """Crea una instancia de MEMORYSTATUSEX inicializando su campo dwLength correctamente."""
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return stat

_win_mem_buffer: MEMORYSTATUSEX = _create_mem_status_ex()
_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """
    Parsea el contenido de /proc/meminfo de forma determinista para sistemas Linux.
    Convierte las métricas de kB a bytes para mantener consistencia global.
    """
    if not isinstance(meminfo_text, str) or not meminfo_text:
        return _EMPTY_SNAPSHOT
    
    metrics: Dict[str, int] = {}
    
    for line in meminfo_text.splitlines():
        if ":" not in line: 
            continue
        try:
            key, value_part = line.split(":", 1)
            digits = "".join(c for c in value_part if c.isdigit())
            if digits:
                metrics[key.strip()] = int(digits) * 1024
        except (ValueError, TypeError, KeyError):
            continue
            
    total = metrics.get("MemTotal", 0)
    if not isinstance(total, int) or total <= 0: 
        return _EMPTY_SNAPSHOT
    
    available = metrics.get("MemAvailable", metrics.get("MemFree", 0))
    cached = metrics.get("Cached", 0)
    
    return MemorySnapshot(
        total=BytesValue(total), 
        available=BytesValue(min(available, total)), 
        cached=BytesValue(max(0, cached))
    )

def _is_valid_process_entry(name: str, pid_str: str, ws_str: str) -> Optional[ProcessMemory]:
    """
    Filtra entradas de procesos basándose en PIDs críticos, rutas protegidas 
    y validación de tipos de los datos de origen.
    """
    if not isinstance(name, str) or not isinstance(pid_str, str) or not isinstance(ws_str, str):
        return None
    
    try:
        pid_val, ws_val = int(pid_str), int(ws_str)
    except (ValueError, TypeError):
        return None
    
    if not name.strip() or pid_val <= 0 or ws_val < 0 or pid_val in SYSTEM_CRITICAL_PIDS:
        return None
    
    if is_protected_path(name):
        return None
        
    return ProcessMemory(name=name, pid=pid_val, working_set=BytesValue(ws_val))

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """
    Parsea una salida CSV (formato esperado: nombre,pid,ws) proveniente de 
    PowerShell, devolviendo una lista ordenada de mayor a menor consumo de RAM.
    """
    if not isinstance(raw_csv_text, str) or not raw_csv_text.strip():
        return []
    
    processes: List[ProcessMemory] = []
    for line in raw_csv_text.splitlines():
        clean = line.strip()
        if not clean: continue
        try:
            parts = [x.strip().strip("'\"") for x in clean.split(",")]
            if len(parts) == 3:
                proc = _is_valid_process_entry(parts[0], parts[1], parts[2])
                if proc:
                    processes.append(proc)
        except Exception:
            continue
    
    processes.sort(key=lambda p: p.working_set, reverse=True)
    return processes[:limit]

def _read_windows_snapshot() -> MemorySnapshot:
    """Interroga a la API GlobalMemoryStatusEx para obtener el estado físico de la RAM."""
    kernel32 = ctypes.windll.kernel32
    if not hasattr(kernel32, "GlobalMemoryStatusEx"):
        return _EMPTY_SNAPSHOT
    
    try:
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(_win_mem_buffer)):
            total = _win_mem_buffer.ullTotalPhys
            avail = _win_mem_buffer.ullAvailPhys
            if total > 0 and avail <= total:
                return MemorySnapshot(total=BytesValue(total), available=BytesValue(avail))
    except (AttributeError, ValueError, TypeError, OverflowError, OSError):
        pass
    return _EMPTY_SNAPSHOT

_snap_cache_time: float = 0.0
_snap_cache_data: Optional[MemorySnapshot] = None
_linux_available: bool = True

def read_snapshot() -> MemorySnapshot:
    """Obtiene un snapshot global de memoria, utilizando caché de 5s para evitar bloqueos de I/O."""
    global _snap_cache_time, _snap_cache_data, _linux_available
    now = time.time()
    if (now - _snap_cache_time) < 5 and _snap_cache_data is not None:
        return _snap_cache_data

    if _is_windows: 
        _snap_cache_data = _read_windows_snapshot()
    elif _linux_available:
        try:
            content = _linux_mem_path.read_text(encoding="utf-8")
            _snap_cache_data = parse_linux_meminfo(content)
        except (OSError, UnicodeDecodeError, RuntimeError):
            _linux_available = False
            _snap_cache_data = _EMPTY_SNAPSHOT
    
    _snap_cache_time = now
    return _snap_cache_data if _snap_cache_data else _EMPTY_SNAPSHOT

_proc_cache_time: float = 0.0
_proc_cache_data: List[ProcessMemory] = []

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna los procesos que más memoria consumen mediante consulta PowerShell (caché 60s)."""
    global _proc_cache_time, _proc_cache_data
    if not _is_windows: return []
    
    if (time.time() - _proc_cache_time) < 60:
        return _proc_cache_data[:limit]
    
    # Optimizamos filtrando procesos de usuario y limitando la carga de trabajo de PS
    ps_cmd = (
        "Get-Process | Where-Object {$_.WorkingSet -ne $null} | "
        "Sort-Object WorkingSet -Descending | Select-Object -First 20 -Property Name, Id, WorkingSet | "
        "ForEach-Object { \"$($_.Name),$($_.Id),$($_.WorkingSet)\" }"
    )
    cmd = ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps_cmd]
    
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=False)
        if proc.returncode == 0 and proc.stdout:
            parsed = parse_windows_process_csv(proc.stdout, limit=limit)
            if parsed:
                _proc_cache_data = parsed
                _proc_cache_time = time.time()
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired): 
        pass
            
    return _proc_cache_data[:limit]

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica la presión de memoria actual basándose en el porcentaje de RAM disponible."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0: return "info"
    available = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un reporte legible de diagnóstico de memoria con recomendaciones."""
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
    """Determina si el PID corresponde a procesos del sistema o a la propia instancia."""
    return isinstance(pid, int) and (pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid())

def _get_process_path(proc_handle: int) -> Optional[str]:
    """Obtiene la ruta completa del ejecutable del proceso dado mediante la API PSAPI."""
    if not proc_handle: return None
    psapi = getattr(ctypes.windll, "psapi", None)
    if not psapi or not hasattr(psapi, "GetModuleFileNameExW"): return None
    
    buf = ctypes.create_unicode_buffer(1024)
    try:
        if psapi.GetModuleFileNameExW(proc_handle, None, buf, 1024) > 0:
            path = Path(str(buf.value))
            if not path.is_absolute():
                return None
            return str(path.resolve())
    except (OSError, ctypes.ArgumentError, ValueError, MemoryError):
        pass
    return None

def _is_safe_to_trim(proc_handle: int) -> Tuple[bool, Optional[str]]:
    """
    Verifica si un proceso es candidato seguro para la liberación de memoria,
    auditando su estado activo y la ubicación del ejecutable.
    """
    if not isinstance(proc_handle, int) or proc_handle <= 0: return False, "Handle inválido."
    kernel32 = ctypes.windll.kernel32
    
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(exit_code)):
            err = kernel32.GetLastError()
            return False, f"Imposible obtener estado del proceso (Error {err})."
            
        if exit_code.value != STILL_ACTIVE_EXIT_CODE:
            return False, "El proceso no está activo."
            
        exec_path_str = _get_process_path(proc_handle)
        if not exec_path_str:
            return False, "Acceso denegado, proceso inexistente o no válido."
        
        # Validaciones de seguridad exigentes sobre la ruta del ejecutable
        if is_protected_path(exec_path_str) or not is_safe_to_modify(exec_path_str):
            return False, "Operación denegada por política de seguridad."
            
        return True, None
    except (AttributeError, ValueError, ctypes.ArgumentError, OSError):
        return False, "Error interno durante la verificación de integridad."

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Ejecuta el trim del Working Set para un proceso dado si cumple las validaciones de seguridad.
    Esta acción es destructiva en rendimiento y debe ser invocada solo bajo demanda.
    """
    if not _is_windows: return False, "Operación solo soportada en Windows."
    
    try:
        target_pid = int(pid)
    except (ValueError, TypeError):
        return False, "PID no válido."

    if _is_system_process(target_pid): 
        return False, "Proceso protegido."

    kernel32 = ctypes.windll.kernel32
    psapi = getattr(ctypes.windll, "psapi", None)
    if not psapi or not hasattr(psapi, "EmptyWorkingSet"): return False, "APIs no disponibles."
    
    proc_handle = kernel32.OpenProcess(SAFE_ACCESS_MASK, False, target_pid)
    if not proc_handle: 
        err = kernel32.GetLastError()
        reason = "Acceso denegado" if err == ERROR_ACCESS_DENIED else f"Error {err}"
        return False, f"{reason} al abrir el proceso."
    
    try:
        is_safe, error_reason = _is_safe_to_trim(proc_handle)
        if not is_safe: 
            return False, error_reason or "Verificación de seguridad fallida."
        
        if not psapi.EmptyWorkingSet(proc_handle): 
            err = kernel32.GetLastError()
            return False, f"El sistema denegó la operación (Error {err})."
            
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (Exception, ctypes.ArgumentError):
        return False, "Error inesperado al intentar liberar el proceso."
    finally:
        kernel32.CloseHandle(proc_handle)
