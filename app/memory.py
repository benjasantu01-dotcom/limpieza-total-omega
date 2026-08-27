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
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, TypeVar, TypeAlias, Final, Set
from safety import is_protected_path

if TYPE_CHECKING:
    from ctypes import wintypes

_T = TypeVar("_T", int, float)
BytesValue: TypeAlias = int
MegabytesValue: TypeAlias = float

BYTES_IN_MB: Final[int] = 1024 * 1024
BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")

PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x0100
SAFE_ACCESS_MASK: Final[int] = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

STILL_ACTIVE_EXIT_CODE: Final[int] = 259
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}

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
    total: BytesValue  # Capacidad total instalada
    available: BytesValue  # Memoria libre + en caché fácilmente recuperable
    cached: BytesValue = 0  # Memoria usada como búfer de archivos

    @property
    def used(self) -> BytesValue:
        return max(0, self.total - self.available)

    @property
    def used_percent(self) -> float:
        if self.total <= 0: return 0.0
        return round((self.used / self.total) * 100, 1)

    @property
    def available_percent(self) -> float:
        if self.total <= 0: return 0.0
        return round((self.available / self.total) * 100, 1)

@dataclass
class ProcessMemory:
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        return round(self.working_set / BYTES_IN_MB, 1)

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte bytes a un formato legible (KB, MB, GB, etc)."""
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"

@lru_cache(maxsize=1)
def _create_mem_status_ex() -> MEMORYSTATUSEX:
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return stat

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Interpreta el contenido de /proc/meminfo en Linux para obtener el estado de RAM."""
    if not isinstance(meminfo_text, str) or not meminfo_text:
        return MemorySnapshot(0, 0)
    vals: Dict[str, int] = {"MemTotal": 0, "MemAvailable": 0, "MemFree": 0, "Cached": 0}
    for line in meminfo_text.splitlines():
        if ":" not in line: continue
        key, rest = line.split(":", 1)
        k = key.strip()
        if k in vals:
            parts = rest.strip().split()
            if parts and parts[0].isdigit():
                vals[k] = int(parts[0]) * 1024
    total = vals["MemTotal"]
    if total <= 0: return MemorySnapshot(0, 0)
    available = vals["MemAvailable"] if vals["MemAvailable"] > 0 else vals["MemFree"]
    return MemorySnapshot(total=total, available=min(available, total), cached=max(0, vals["Cached"]))

@lru_cache(maxsize=1)
def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Convierte el CSV de procesos de PowerShell en objetos ProcessMemory."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text:
        return []
    processes: List[ProcessMemory] = []
    for line in raw_csv_text.splitlines():
        parts = [p.strip().strip("'\"") for p in line.split(",")]
        if len(parts) >= 3:
            try:
                name, pid_val, ws_val = parts[0], int(parts[1]), int(parts[2])
                if ws_val > 0 and pid_val not in SYSTEM_CRITICAL_PIDS:
                    processes.append(ProcessMemory(name=name, pid=pid_val, working_set=ws_val))
            except (ValueError, TypeError):
                continue
    return sorted(processes, key=lambda p: p.working_set, reverse=True)[:limit]

def _read_windows_snapshot() -> MemorySnapshot:
    """Solicita estadísticas de memoria mediante la API GlobalMemoryStatusEx de Windows."""
    try:
        kernel32 = getattr(ctypes.windll, "kernel32", None)
        if kernel32 is None or not hasattr(kernel32, "GlobalMemoryStatusEx"):
            return MemorySnapshot(0, 0)
        stat = _create_mem_status_ex()
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return MemorySnapshot(0, 0)
        return MemorySnapshot(total=int(stat.ullTotalPhys), available=int(stat.ullAvailPhys))
    except (AttributeError, ValueError, TypeError, OverflowError, OSError):
        return MemorySnapshot(0, 0)

def read_snapshot() -> MemorySnapshot:
    """Obtiene un snapshot global de la memoria según el sistema operativo actual."""
    if os.name == "nt": 
        return _read_windows_snapshot()
    if os.path.exists("/proc/meminfo") and os.access("/proc/meminfo", os.R_OK):
        try:
            with open("/proc/meminfo", encoding="utf-8", errors="replace") as f:
                return parse_linux_meminfo(f.read())
        except (OSError, PermissionError, IOError): 
            pass
    return MemorySnapshot(0, 0)

_proc_cache_time: float = 0.0
_proc_cache_data: str = ""

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Obtiene la lista de procesos con mayor consumo mediante caché temporal de 60s."""
    global _proc_cache_time, _proc_cache_data
    if os.name != "nt": return []
    if (time.time() - _proc_cache_time) > 60:
        cmd = ['powershell', '-NoProfile', '-Command', 
               "Get-Process | Where-Object {$_.Id -notin 0,4} | Select-Object Name, Id, WorkingSet | ForEach-Object { \"$($_.Name),$($_.Id),$($_.WorkingSet)\" }"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=False)
            if proc.returncode == 0:
                _proc_cache_data, _proc_cache_time = proc.stdout, time.time()
                parse_windows_process_csv.cache_clear()
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired): pass
    return parse_windows_process_csv(_proc_cache_data, limit=limit)

@lru_cache(maxsize=2)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el estado de la RAM basándose en el porcentaje disponible."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0: return "info"
    available = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un reporte legible sobre el estado actual de la memoria."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return ["No se pudo leer el estado de la memoria en este sistema."]
    report = [
        f"Memoria total: {format_bytes(snapshot.total)}",
        f"En uso: {format_bytes(snapshot.used)} ({snapshot.used_percent}%)",
        f"Disponible: {format_bytes(snapshot.available)} ({snapshot.available_percent}%)",
    ]
    diagnostics = {
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
    """Verifica si el PID corresponde a un proceso crítico del sistema."""
    return isinstance(pid, int) and (pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid())

def _get_process_path(handle: wintypes.HANDLE) -> Optional[str]:
    """Resuelve la ruta completa del archivo ejecutable de un proceso."""
    if not handle or handle == -1: return None
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    if not kernel32 or not hasattr(kernel32, "QueryFullProcessImageNameW"): return None
    size, buf = ctypes.c_ulong(4096), ctypes.create_unicode_buffer(4096)
    try:
        if kernel32.QueryFullProcessImageNameW(handle, 0, ctypes.byref(buf), ctypes.byref(size)) > 0:
            return str(buf.value)
    except (OSError, ctypes.ArgumentError): pass
    return None

def _validate_path_security(path: str) -> Tuple[bool, Optional[str]]:
    """Valida la seguridad de la ruta, bloqueando rutas relativas, UNC o protegidas."""
    if not isinstance(path, str) or not os.path.isabs(path) or path.startswith("\\\\"):
        return False, "Ruta inválida o en red."
    if any(seq in path.encode("utf-8", errors="ignore") for seq in [b"\xe2\x80\xae", b"\xe2\x80\xad", b"\xe2\x80\xab", b"\xe2\x80\xaa"]):
        return False, "Ruta de proceso sospechosa."
    try:
        p = Path(path).resolve()
        for parent in [p] + list(p.parents):
            if is_protected_path(str(parent)): return False, f"Ruta protegida en {parent.name}."
    except Exception: return False, "Error resolviendo ruta."
    return True, None

def _is_safe_to_trim(proc_handle: wintypes.HANDLE, pid: int) -> Tuple[bool, Optional[str]]:
    """Valida integridad del proceso y seguridad de su ubicación antes de operar."""
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    if not kernel32 or kernel32.GetProcessId(proc_handle) != pid: return False, "PID mismatch."
    exit_code = ctypes.c_ulong()
    if not kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(exit_code)) or exit_code.value != STILL_ACTIVE_EXIT_CODE:
        return False, "Proceso inactivo."
    path = _get_process_path(proc_handle)
    if not path: return False, "Ruta inaccesible."
    return _validate_path_security(path)

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """Intenta reducir el working set de un proceso específico tras validar su seguridad."""
    if os.name != "nt": return False, "Solo disponible en Windows."
    kernel32, psapi = getattr(ctypes.windll, "kernel32", None), getattr(ctypes.windll, "psapi", None)
    if not kernel32 or not psapi or not hasattr(psapi, "EmptyWorkingSet"): return False, "APIs no disponibles."
    
    try: target_pid = int(pid)
    except (ValueError, TypeError): return False, "PID no válido."
    if _is_system_process(target_pid): return False, "Proceso protegido."
    
    proc_handle = kernel32.OpenProcess(SAFE_ACCESS_MASK, False, target_pid)
    if not proc_handle or proc_handle == -1: 
        return False, "Permisos insuficientes o proceso inaccesible."
    
    try:
        valid, reason = _is_safe_to_trim(proc_handle, target_pid)
        if not valid: return False, reason or "Validación fallida."
        if not psapi.EmptyWorkingSet(proc_handle): 
            return False, f"Error del sistema {ctypes.get_last_error()}."
        return True, f"Working set liberado. {TRIM_WARNING}"
    except Exception as e:
        return False, f"Excepción inesperada durante el trim: {str(e)}"
    finally:
        if proc_handle and proc_handle != -1: kernel32.CloseHandle(proc_handle)

if __name__ == "__main__":
    snap = read_snapshot()
    print(f"Estado: {pressure_level(snap)}")
    for line in diagnose(snap): print(f"  {line}")
