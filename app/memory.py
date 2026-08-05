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
import re
import subprocess
import time
import math
from functools import lru_cache
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, TypeVar, TypeAlias
from safety import is_protected_path

if TYPE_CHECKING:
    import ctypes

_T = TypeVar("_T", int, float)
BytesValue: TypeAlias = int
MegabytesValue: TypeAlias = float

BYTES_IN_MB: int = 1024 * 1024

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

TRIM_WARNING: str = (
    "Liberar el working set NO acelera la PC: fuerza a Windows a expulsar "
    "memoria que los programas están usando, y al volver a necesitarla la "
    "tiene que releer del disco. El número de 'RAM libre' sube, pero el "
    "rendimiento suele empeorar. Solo tiene sentido antes de medir algo "
    "puntual, no como mantenimiento."
)

BYTE_UNITS: Tuple[str, ...] = ("B", "KB", "MB", "GB", "TB")

# Constantes de acceso para Win32 API (Flags de permisos para OpenProcess)
PROCESS_QUERY_INFO: int = 0x0400
PROCESS_SET_QUOTA: int = 0x0100
PROCESS_VM_WRITE: int = 0x0020
REQUIRED_ACCESS: int = PROCESS_QUERY_INFO | PROCESS_SET_QUOTA | PROCESS_VM_WRITE

# Lista de PIDs críticos de Windows que nunca deben ser intervenidos
SYSTEM_CRITICAL_PIDS: Tuple[int, ...] = (0, 4)

_PROCESS_CACHE: Dict[str, Tuple[float, List[ProcessMemory]]] = {"data": (0.0, [])}

@dataclass
class MemorySnapshot:
    """Representa un instante de la memoria física del sistema en bytes."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = 0

    @property
    def used(self) -> BytesValue:
        """Calcula memoria ocupada en bytes. Asegura no retornar valores negativos."""
        return max(0, self.total - self.available)

    @property
    def used_percent(self) -> float:
        """Retorna porcentaje de uso (0-100) sobre capacidad total."""
        if self.total <= 0:
            return 0.0
        return round((self.used / self.total) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Retorna porcentaje disponible (0-100) sobre capacidad total."""
        if self.total <= 0:
            return 0.0
        return round((self.available / self.total) * 100, 1)


@dataclass
class ProcessMemory:
    """Estructura de datos para el consumo de memoria (Working Set) de un proceso."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Convierte el valor de Working Set a MB para representación visual."""
        return round(self.working_set / BYTES_IN_MB, 1)


def format_bytes(num: Optional[_T]) -> str:
    """
    Convierte un valor numérico de bytes a una cadena legible (ej: '10.5 MB').
    Aplica escala logarítmica base 1024 y redondeo a un decimal si no es entero.
    """
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    
    idx = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"


@lru_cache(maxsize=4)
def parse_linux_meminfo(text: str) -> MemorySnapshot:
    """
    Parsea el contenido crudo de /proc/meminfo. 
    Convierte kB a bytes y retorna un objeto MemorySnapshot.
    """
    if not text:
        return MemorySnapshot(0, 0)
    values: Dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    return MemorySnapshot(
        total=values.get("MemTotal", 0),
        available=values.get("MemAvailable", values.get("MemFree", 0)),
        cached=values.get("Cached", 0)
    )


def _is_valid_process_row(parts: List[str]) -> bool:
    """
    Valida la integridad de una línea procesada desde Get-Process.
    Requiere al menos 3 columnas: [Name, PID, WorkingSet].
    PID y WorkingSet deben ser enteros no negativos.
    """
    return (len(parts) >= 3 and 
            parts[1].strip().isdigit() and 
            parts[2].strip().isdigit() and 
            int(parts[2].strip()) >= 0)


def parse_windows_process_csv(text: str, limit: int = 10) -> List[ProcessMemory]:
    """
    Transforma texto CSV proveniente de PowerShell (Get-Process) a objetos ProcessMemory.
    Los resultados son ordenados por WorkingSet (de mayor a menor) y truncados según limit.
    """
    if not text:
        return []

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return []

    candidates: List[Tuple[str, int, int]] = []
    for line in lines[1:]:
        parts = [p.strip().strip('"') for p in line.split(",")]
        if _is_valid_process_row(parts):
            try:
                name = parts[0] if parts[0] else "Unknown"
                candidates.append((name, int(parts[1]), int(parts[2])))
            except (ValueError, IndexError):
                continue

    candidates.sort(key=lambda x: x[2], reverse=True)
    return [ProcessMemory(name=c[0], pid=c[1], working_set=c[2]) for c in candidates[:limit]]


def _create_memstat_struct(ctypes_lib: "ctypes") -> "ctypes.Structure":
    """
    Define y retorna una estructura C compatible con la API Win32 MEMORYSTATUSEX.
    Requiere una instancia de ctypes inyectada para realizar el mapeo de tipos.
    """
    class MEMORYSTATUSEX(ctypes_lib.Structure):
        _fields_ = [
            ("dwLength", ctypes_lib.c_ulong),
            ("dwMemoryLoad", ctypes_lib.c_ulong),
            ("ullTotalPhys", ctypes_lib.c_ulonglong),
            ("ullAvailPhys", ctypes_lib.c_ulonglong),
            ("ullTotalPageFile", ctypes_lib.c_ulonglong),
            ("ullAvailPageFile", ctypes_lib.c_ulonglong),
            ("ullTotalVirtual", ctypes_lib.c_ulonglong),
            ("ullAvailVirtual", ctypes_lib.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes_lib.c_ulonglong),
        ]
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes_lib.sizeof(MEMORYSTATUSEX)
    return stat


def _read_windows_snapshot() -> MemorySnapshot:
    """Consulta la API GlobalMemoryStatusEx de Windows mediante ctypes."""
    import ctypes

    stat = _create_memstat_struct(ctypes)
    kernel32 = ctypes.windll.kernel32
    
    if not hasattr(kernel32, "GlobalMemoryStatusEx") or not kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return MemorySnapshot(total=0, available=0)
        
    return MemorySnapshot(total=int(stat.ullTotalPhys), available=int(stat.ullAvailPhys))


def read_snapshot() -> MemorySnapshot:
    """Detecta la plataforma actual y retorna un snapshot del estado de memoria física."""
    if os.name == "nt":
        try:
            return _read_windows_snapshot()
        except Exception:
            return MemorySnapshot(total=0, available=0)
            
    meminfo_path: str = "/proc/meminfo"
    if os.path.exists(meminfo_path):
        try:
            with open(meminfo_path, encoding="utf-8", errors="replace") as f:
                return parse_linux_meminfo(f.read())
        except (OSError, PermissionError, IOError):
            pass
            
    return MemorySnapshot(total=0, available=0)


def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """
    Obtiene los procesos que más memoria consumen en Windows. 
    Implementa una caché de 5 segundos para evitar sobrecarga por invocación frecuente.
    """
    if os.name != "nt":
        return []

    now = time.time()
    ts, cached_processes = _PROCESS_CACHE["data"]
    if now - ts < 5.0:
        return cached_processes[:limit]

    command: str = (
        "Get-Process | Select-Object -Property Name,Id,WorkingSet | "
        "ConvertTo-Csv -NoTypeInformation"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            processes = parse_windows_process_csv(result.stdout, limit=limit)
            _PROCESS_CACHE["data"] = (now, processes)
            return processes
    except (OSError, subprocess.SubprocessError, Exception):
        pass
    return []


def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el estado de presión de memoria basándose en el porcentaje disponible."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return "info"
    available: float = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"


def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """
    Genera un informe textual descriptivo para la interfaz de usuario.
    Recibe el estado actual (snapshot) y, opcionalmente, la lista de procesos intensivos.
    """
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return ["No se pudo leer el estado de la memoria en este sistema."]

    level: str = pressure_level(snapshot)
    lines: List[str] = [
        f"Memoria total: {format_bytes(snapshot.total)}",
        f"En uso: {format_bytes(snapshot.used)} ({snapshot.used_percent}%)",
        f"Disponible: {format_bytes(snapshot.available)} ({snapshot.available_percent}%)",
    ]

    diagnosticos: Dict[str, str] = {
        "ok": "Estado: holgado. La memoria ocupada por caché mejora la velocidad.",
        "info": "Estado: normal. Windows gestiona la memoria de forma eficiente.",
        "warning": "Estado: ajustado. Conviene cerrar aplicaciones innecesarias.",
        "danger": "Estado: crítico. El sistema recurre al archivo de paginación."
    }
    
    lines.append(diagnosticos.get(level, ""))

    if processes:
        for proc in processes[:3]:
            lines.append(f"  Mayor consumo: {proc.name} (PID {proc.pid}) — {proc.working_set_mb} MB")

    return lines


def _is_system_process(pid: int) -> bool:
    """Verifica si un PID está en la lista de procesos críticos o es una tarea de bajo nivel."""
    return pid in SYSTEM_CRITICAL_PIDS or pid <= 100


def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Intenta reducir el Working Set de un proceso mediante la API de Windows `EmptyWorkingSet`.
    
    Esta función es de alto riesgo: realiza una validación estricta de seguridad contra PIDs
    protegidos y requiere permisos administrativos para interactuar con procesos externos.
    """
    if os.name != "nt":
        return False, "Solo disponible en Windows."
    
    try:
        target_pid: int = int(pid)
    except (ValueError, TypeError):
        return False, "El PID debe ser un número entero válido."

    if target_pid == os.getpid() or _is_system_process(target_pid):
        return False, "Operación denegada: PID crítico o protegido."
    
    import ctypes
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    
    handle = kernel32.OpenProcess(REQUIRED_ACCESS, False, target_pid)
    if not handle:
        error_code = kernel32.GetLastError()
        return False, f"Acceso denegado (Error {error_code}). Requiere permisos de administrador."
    
    try:
        if not psapi.EmptyWorkingSet(handle):
            return False, "Error al intentar liberar memoria del proceso."
        return True, f"Working set liberado. {TRIM_WARNING}"
    finally:
        kernel32.CloseHandle(handle)
