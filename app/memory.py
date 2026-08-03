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
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, TypeVar
from safety import is_protected_path

if TYPE_CHECKING:
    import ctypes

_T = TypeVar("_T", int, float)

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

_PROCESS_CACHE: Dict[str, Tuple[float, List[ProcessMemory]]] = {}

@dataclass
class MemorySnapshot:
    """Representa un instante de la memoria física del sistema en bytes."""
    total: int
    available: int
    cached: int = 0

    @property
    def used(self) -> int:
        """Calcula memoria ocupada. Se asegura de no retornar valores negativos."""
        return max(0, self.total - self.available)

    @property
    def used_percent(self) -> float:
        """Retorna el porcentaje de uso (0-100) basado en la capacidad total."""
        if self.total <= 0:
            return 0.0
        return round((self.used / self.total) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Retorna el porcentaje disponible (0-100) basado en la capacidad total."""
        if self.total <= 0:
            return 0.0
        return round((self.available / self.total) * 100, 1)


@dataclass
class ProcessMemory:
    """Estructura de datos para el consumo de memoria de un proceso (RSS/Working Set)."""
    name: str
    pid: int
    working_set: int
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> float:
        """Convierte el valor de Working Set de bytes a MB para legibilidad en la UI."""
        return round(self.working_set / 1048576, 1)


def format_bytes(num: Optional[_T]) -> str:
    """
    Convierte un valor numérico en bytes a una cadena legible.
    Utiliza logaritmo para determinar la unidad directamente.
    """
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    
    idx = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"


@lru_cache(maxsize=4)
def parse_linux_meminfo(text: str) -> MemorySnapshot:
    """
    Interpreta el contenido de /proc/meminfo (texto crudo). 
    Convierte valores expresados originalmente en kB a bytes para consistencia.
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
    """Valida que una lista de campos extraídos de un CSV sea un proceso procesable."""
    return (len(parts) >= 3 and 
            parts[1].isdigit() and 
            parts[2].isdigit() and 
            int(parts[2]) >= 0)


def parse_windows_process_csv(text: str, limit: int = 10) -> List[ProcessMemory]:
    """
    Transforma la salida CSV de PowerShell en una lista de objetos ProcessMemory.
    Filtra entradas corruptas y ordena los resultados por uso de memoria descendente.
    """
    if not isinstance(text, str) or not text.strip():
        return []

    lines = text.splitlines()
    if len(lines) < 2:
        return []

    processes: List[ProcessMemory] = []
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        parts = [p.strip().strip('"') for p in line.split(",")]
        
        if _is_valid_process_row(parts):
            try:
                processes.append(ProcessMemory(
                    name=parts[0] if parts[0] else "Unknown", 
                    pid=int(parts[1]), 
                    working_set=int(parts[2])
                ))
            except (ValueError, TypeError):
                continue

    processes.sort(key=lambda p: p.working_set, reverse=True)
    return processes[:max(1, limit)]


def _read_windows_snapshot() -> MemorySnapshot:
    """Implementa la consulta a la API de Windows GlobalMemoryStatusEx mediante ctypes."""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
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

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    kernel32 = ctypes.windll.kernel32
    
    if not hasattr(kernel32, "GlobalMemoryStatusEx") or not kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return MemorySnapshot(total=0, available=0)
        
    return MemorySnapshot(total=int(stat.ullTotalPhys), available=int(stat.ullAvailPhys))


def read_snapshot() -> MemorySnapshot:
    """
    Detecta el sistema operativo y delega la captura de métricas de RAM al 
    método correspondiente según la plataforma (Win32 o /proc/meminfo).
    """
    if os.name == "nt":
        try:
            return _read_windows_snapshot()
        except Exception:
            return MemorySnapshot(total=0, available=0)
            
    meminfo_path: str = "/proc/meminfo"
    if os.path.exists(meminfo_path):
        try:
            with open(meminfo_path, encoding="utf-8", errors="replace") as f:
                content = f.read()
                return parse_linux_meminfo(content)
        except (OSError, PermissionError):
            pass
            
    return MemorySnapshot(total=0, available=0)


def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """
    Consulta procesos de alto consumo en Windows. Implementa caché TTL de 5 segundos
    para reducir la carga de llamadas a subprocesos del sistema.
    """
    global _PROCESS_CACHE
    if os.name != "nt":
        return []

    now = time.time()
    if "data" in _PROCESS_CACHE:
        ts, data = _PROCESS_CACHE["data"]
        if now - ts < 5.0:
            return data[:limit]

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
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def pressure_level(snapshot: MemorySnapshot) -> str:
    """
    Evalúa el estado de salud de la RAM basado en el porcentaje disponible.
    Retorna un identificador de criticidad: 'ok', 'info', 'warning', 'danger'.
    """
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return "info"
    available: float = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"


def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """
    Genera un informe unificado sobre el estado de la RAM, incluyendo métricas
    totales y una lista de los procesos que mayor presión generan.
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


def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Solicita al SO (Windows) liberar el espacio de trabajo de un proceso específico.
    
    Aplica verificaciones de seguridad previas para evitar la manipulación de 
    procesos del sistema operativo (PIDs <= 100) o procesos de la propia aplicación.
    """
    if os.name != "nt":
        return False, "Solo disponible en Windows."
    
    try:
        target_pid: int = int(pid)
    except (ValueError, TypeError):
        return False, "El PID debe ser un número entero válido."

    # Seguridad: Bloquear PIDs de sistema y procesos críticos (bajo 100 es común en Windows)
    if target_pid <= 100:
        return False, "Operación denegada: PID de sistema crítico protegido."
    
    if target_pid == os.getpid():
        return False, "Operación denegada: proceso de la app."
    
    if is_protected_path(str(target_pid)):
        return False, "Operación denegada: PID protegido por política de seguridad."
    
    import ctypes
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi

    handle = kernel32.OpenProcess(REQUIRED_ACCESS, False, target_pid)
    if not handle:
        error_code = kernel32.GetLastError()
        if error_code == 5: # ERROR_ACCESS_DENIED
            return False, "Acceso denegado: se requieren privilegios de administrador."
        return False, f"No se pudo acceder al proceso {target_pid} (Error {error_code})."
    
    try:
        if not psapi.EmptyWorkingSet(handle):
            return False, "Error interno al intentar liberar memoria del proceso."
        return True, f"Working set liberado. {TRIM_WARNING}"
    finally:
        kernel32.CloseHandle(handle)
