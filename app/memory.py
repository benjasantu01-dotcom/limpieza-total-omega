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
from functools import lru_cache
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, TypeVar, TypeAlias, Final, Iterator, Set
from safety import is_protected_path

if TYPE_CHECKING:
    from ctypes import wintypes

_T = TypeVar("_T", int, float)
BytesValue: TypeAlias = int
MegabytesValue: TypeAlias = float

BYTES_IN_MB: Final[int] = 1024 * 1024
BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")

# Constantes para Win32 API: permisos mínimos necesarios para diagnóstico y gestión
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x0100
SAFE_ACCESS_MASK: Final[int] = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

STILL_ACTIVE_EXIT_CODE: Final[int] = 259
ERROR_ACCESS_DENIED: Final[int] = 5

# PIDs reservados: 0 (System Idle), 4 (System)
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}

_last_proc_fetch: float = 0.0
_cached_proc_output: str = ""

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
    """Estructura de datos Win32 (GlobalMemoryStatusEx) para consultar RAM global."""
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
    """Instantánea inmutable del estado global de memoria del sistema."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = 0

    @property
    def used(self) -> BytesValue:
        """Bytes en uso calculados como total menos disponible."""
        return max(0, self.total - self.available)

    @property
    def used_percent(self) -> float:
        """Porcentaje de RAM utilizada."""
        if self.total <= 0: return 0.0
        return round((self.used / self.total) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Porcentaje de RAM disponible."""
        if self.total <= 0: return 0.0
        return round((self.available / self.total) * 100, 1)

@dataclass
class ProcessMemory:
    """Contenedor de información sobre el uso de memoria de un proceso específico."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Consumo de Working Set del proceso expresado en MB."""
        return round(self.working_set / BYTES_IN_MB, 1)

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte un valor en bytes a una cadena legible (ej: 1.5 MB)."""
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"

def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """Instancia la estructura requerida por la API de Windows con dwLength inicializado."""
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return stat

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Parsea el contenido de /proc/meminfo devolviendo un objeto MemorySnapshot."""
    if not isinstance(meminfo_text, str) or not meminfo_text:
        return MemorySnapshot(0, 0)
    
    vals: Dict[str, int] = {"MemTotal": 0, "MemAvailable": 0, "MemFree": 0, "Cached": 0}
    
    for line in meminfo_text.splitlines():
        if ":" not in line: continue
        key, rest = line.split(":", 1)
        k = key.strip()
        if k in vals:
            try:
                vals[k] = int(rest.split()[0]) * 1024
            except (ValueError, IndexError):
                continue
    
    total = vals["MemTotal"]
    available = vals["MemAvailable"] if vals["MemAvailable"] > 0 else vals["MemFree"]
    return MemorySnapshot(total=total, available=min(available, total), cached=vals["Cached"])

def _parse_csv_row(csv_line: str) -> Optional[ProcessMemory]:
    """Convierte una línea CSV (formato Name,ID,WS) a un modelo ProcessMemory."""
    if not isinstance(csv_line, str) or not csv_line.strip():
        return None
    parts = [p.strip().strip("'\"") for p in csv_line.split(",")]
    if len(parts) != 3:
        return None
    try:
        name, pid_str, ws_str = parts
        if not name or not pid_str.isdigit() or not ws_str.isdigit():
            return None
        return ProcessMemory(name=name, pid=int(pid_str), working_set=int(ws_str))
    except (ValueError, TypeError):
        return None

def _yield_processes(raw_csv_text: str) -> Iterator[ProcessMemory]:
    """Generador que filtra procesos válidos a partir de salida cruda de PowerShell."""
    if not isinstance(raw_csv_text, str):
        return
    for line in raw_csv_text.splitlines():
        try:
            proc = _parse_csv_row(line)
            if proc and proc.working_set > 0 and proc.pid not in SYSTEM_CRITICAL_PIDS:
                yield proc
        except Exception:
            continue

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Ordena procesos por consumo descendente y aplica un límite."""
    if not isinstance(raw_csv_text, str) or not raw_csv_text:
        return []
    return sorted(_yield_processes(raw_csv_text), key=lambda p: p.working_set, reverse=True)[:max(0, limit)]

def _read_windows_snapshot() -> MemorySnapshot:
    """Consulta la API nativa GlobalMemoryStatusEx para obtener RAM global."""
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    if kernel32 is None or not hasattr(kernel32, "GlobalMemoryStatusEx"):
        return MemorySnapshot(0, 0)
    
    stat = _create_mem_status_ex()
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return MemorySnapshot(0, 0)
    
    try:
        total = int(stat.ullTotalPhys)
        avail = int(stat.ullAvailPhys)
        if total <= 0 or avail > total: 
            return MemorySnapshot(0, 0)
        return MemorySnapshot(total=total, available=avail)
    except (ValueError, TypeError, OverflowError):
        return MemorySnapshot(0, 0)

def read_snapshot() -> MemorySnapshot:
    """Detecta el SO y lee el estado de memoria actual."""
    if os.name == "nt":
        try: return _read_windows_snapshot()
        except (AttributeError, OSError, ctypes.ArgumentError): return MemorySnapshot(0, 0)
    
    try:
        if os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo", encoding="utf-8", errors="replace") as f:
                return parse_linux_meminfo(f.read())
    except (OSError, PermissionError):
        pass
    return MemorySnapshot(0, 0)

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Obtiene los procesos más pesados (usa cacheo de 30s para evitar saturar el sistema)."""
    global _last_proc_fetch, _cached_proc_output
    if os.name != "nt": return []
    
    if (time.time() - _last_proc_fetch) > 30:
        cmd = 'Get-Process | Select-Object -Property Name,Id,WorkingSet | ForEach-Object { "$($_.Name),$($_.Id),$($_.WorkingSet)" }'
        try:
            proc = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0:
                _cached_proc_output = proc.stdout
                _last_proc_fetch = time.time()
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            pass 
            
    return parse_windows_process_csv(_cached_proc_output, limit=limit)

@lru_cache(maxsize=2)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el estrés del sistema (ok, info, warning, danger)."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return "info"
    available = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un informe descriptivo sobre el estado de la RAM."""
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
    """Valida si un PID pertenece a procesos críticos o protegidos."""
    if not isinstance(pid, int) or pid <= 0: return True
    return pid in SYSTEM_CRITICAL_PIDS or pid < 100

def _get_process_path(handle: wintypes.HANDLE) -> Optional[str]:
    """Obtiene la ruta absoluta del ejecutable vía API de Windows."""
    if not handle or handle == -1: return None
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    if not kernel32 or not hasattr(kernel32, "QueryFullProcessImageNameW"): return None
    buffer_size = 1024
    size = ctypes.c_ulong(buffer_size)
    buf = ctypes.create_unicode_buffer(buffer_size)
    try:
        if kernel32.QueryFullProcessImageNameW(handle, 0, ctypes.byref(buf), ctypes.byref(size)) > 0:
            if size.value > 0: return str(buf.value)
    except (OSError, ctypes.ArgumentError): pass
    return None

def _is_safe_to_trim(proc_handle: wintypes.HANDLE, pid: int) -> Tuple[bool, Optional[str]]:
    """
    Realiza una auditoría defensiva antes de aplicar EmptyWorkingSet.
    Verifica la identidad del proceso, estado de actividad, y protege contra 
    rutas de sistema, junctions o caracteres sospechosos que intenten evadir la seguridad.
    """
    if not proc_handle: return False, "Handle inválido."
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    if not kernel32: return False, "No se pudo acceder a la API del sistema."
    try:
        actual_pid = kernel32.GetProcessId(proc_handle)
        if actual_pid != pid:
            return False, "Error de validación: el proceso identificado cambió."

        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(exit_code)):
            return False, "No se pudo obtener el estado del proceso."
        if exit_code.value != STILL_ACTIVE_EXIT_CODE:
            return False, "El proceso seleccionado ya no está activo."
        
        path = _get_process_path(proc_handle)
        if not path or not os.path.exists(path): 
            return False, "No se pudo verificar la ubicación del ejecutable."
        
        if os.path.islink(path):
            return False, "Ruta bloqueada: el ejecutable reside en un punto de reparse."
        
        # Filtro contra técnicas de ofuscación de nombres (RTL overrides)
        forbidden_sequences = [b"\xe2\x80\xae", b"\xe2\x80\xad", b"\xe2\x80\xab", b"\xe2\x80\xaa"]
        if any(seq in path.encode("utf-8", errors="ignore") for seq in forbidden_sequences):
            return False, "Ruta de proceso sospechosa."
        
        normalized_path = os.normcase(os.path.abspath(path))
        if is_protected_path(normalized_path):
            return False, "Operación denegada: ruta de ejecutable protegida."
    except (ctypes.ArgumentError, Exception):
        return False, "Error interno durante la validación de seguridad."
    return True, None

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """Solicita al sistema la liberación de memoria residente (Working Set)."""
    if os.name != "nt": return False, "Solo disponible en Windows."
    kernel32, psapi = getattr(ctypes.windll, "kernel32", None), getattr(ctypes.windll, "psapi", None)
    if not kernel32 or not psapi or not hasattr(psapi, "EmptyWorkingSet"):
        return False, "Error de sistema: APIs de memoria no disponibles."
    
    try:
        target_pid = int(pid)
    except (ValueError, TypeError): return False, "El PID debe ser un número entero válido."
    
    if _is_system_process(target_pid) or target_pid == os.getpid():
        return False, "Operación denegada: PID fuera de rango o protegido."

    proc_handle = None
    try:
        proc_handle = kernel32.OpenProcess(SAFE_ACCESS_MASK, False, target_pid)
        if not proc_handle or proc_handle == -1: 
            return False, "Acceso denegado al proceso (podría requerir privilegios elevados)."
            
        valid, reason = _is_safe_to_trim(proc_handle, target_pid)
        if not valid: 
            return False, reason or "Validación de proceso fallida."
        if not psapi.EmptyWorkingSet(proc_handle):
            return False, "Error al liberar memoria del proceso seleccionado."
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (ctypes.ArgumentError, Exception):
        return False, "Ocurrió un error técnico al gestionar el proceso."
    finally:
        if proc_handle and proc_handle != -1:
            kernel32.CloseHandle(proc_handle)

if __name__ == "__main__":
    snap = read_snapshot()
    print(f"Estado: {pressure_level(snap)}")
    for line in diagnose(snap):
        print(f"  {line}")
