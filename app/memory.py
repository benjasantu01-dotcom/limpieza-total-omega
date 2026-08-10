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
import ctypes
from functools import lru_cache
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, TypeVar, TypeAlias
from safety import is_protected_path

if TYPE_CHECKING:
    from ctypes import wintypes

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

# Constantes de acceso para Win32 API
PROCESS_QUERY_INFO: int = 0x0400
PROCESS_SET_QUOTA: int = 0x0100
SAFE_ACCESS: int = PROCESS_QUERY_INFO | PROCESS_SET_QUOTA
STILL_ACTIVE: int = 259
ERROR_ACCESS_DENIED: int = 5

# Lista de PIDs críticos de Windows que nunca deben ser intervenidos
SYSTEM_CRITICAL_PIDS: Tuple[int, ...] = (0, 4)

_PROCESS_CACHE: Dict[str, Tuple[float, List[ProcessMemory]]] = {"data": (0.0, [])}

class MEMORYSTATUSEX(ctypes.Structure):
    """Estructura de la API Win32 para GlobalMemoryStatusEx."""
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

@dataclass
class MemorySnapshot:
    """
    Representa una instantánea de la memoria física del sistema.
    Valores almacenados internamente siempre en bytes.
    """
    total: BytesValue
    available: BytesValue
    cached: BytesValue = 0

    @property
    def used(self) -> BytesValue:
        """Calcula los bytes ocupados restando la memoria disponible al total."""
        return max(0, self.total - self.available)

    @property
    def used_percent(self) -> float:
        """Calcula el porcentaje de memoria en uso respecto al total."""
        if self.total <= 0:
            return 0.0
        return round((self.used / self.total) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Calcula el porcentaje de memoria disponible respecto al total."""
        if self.total <= 0:
            return 0.0
        return round((self.available / self.total) * 100, 1)


@dataclass
class ProcessMemory:
    """Consumo de memoria 'Working Set' de un proceso específico."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Convierte el valor de Working Set (bytes) a Megabytes (float)."""
        return round(self.working_set / BYTES_IN_MB, 1)


def format_bytes(num: Optional[int | float]) -> str:
    """Convierte bytes crudos a una representación legible (ej. 1.2 MB)."""
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"


@lru_cache(maxsize=4)
def parse_linux_meminfo(text: str) -> MemorySnapshot:
    """Parsea el contenido de /proc/meminfo en una estructura MemorySnapshot."""
    if not isinstance(text, str) or not text:
        return MemorySnapshot(0, 0)
    values: Dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if match:
            try:
                values[match.group(1)] = int(match.group(2)) * 1024
            except (ValueError, OverflowError, TypeError):
                continue
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    if not isinstance(total, int) or total <= 0:
        return MemorySnapshot(0, 0)
    return MemorySnapshot(
        total=total,
        available=max(0, min(available if isinstance(available, int) else 0, total)),
        cached=values.get("Cached", 0) if isinstance(values.get("Cached"), int) else 0
    )


def _is_valid_process_row(parts: List[str]) -> bool:
    """Verifica si una fila CSV procesada contiene datos numéricos válidos."""
    return (len(parts) >= 3 and 
            parts[1].strip().isdigit() and 
            parts[2].strip().isdigit() and 
            int(parts[2].strip()) >= 0)


def parse_windows_process_csv(text: str, limit: int = 10) -> List[ProcessMemory]:
    """Convierte la salida de PowerShell Get-Process en una lista de ProcessMemory."""
    if not isinstance(text, str) or not text:
        return []
    
    processes = []
    # Usar generador para procesar líneas solo una vez
    for line in (l for l in text.splitlines() if l.strip()):
        parts = [p.strip().strip('"') for p in line.split(",")]
        if _is_valid_process_row(parts):
            try:
                processes.append(ProcessMemory(
                    name=parts[0] or "Unknown", 
                    pid=int(parts[1]), 
                    working_set=int(parts[2])
                ))
            except (ValueError, IndexError):
                continue
    
    # Ordenar solo una vez antes de retornar el slice
    processes.sort(key=lambda p: p.working_set, reverse=True)
    return processes[:limit]


def _read_windows_snapshot() -> MemorySnapshot:
    """Obtiene el estado de la RAM llamando a GlobalMemoryStatusEx de Win32 API."""
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    kernel32 = ctypes.windll.kernel32
    if not hasattr(kernel32, "GlobalMemoryStatusEx") or not kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return MemorySnapshot(total=0, available=0)
    return MemorySnapshot(total=int(stat.ullTotalPhys), available=int(stat.ullAvailPhys))


def read_snapshot() -> MemorySnapshot:
    """Lee el snapshot actual del sistema operativo (Windows o Linux)."""
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
                if content:
                    return parse_linux_meminfo(content)
        except (OSError, PermissionError, IOError):
            return MemorySnapshot(total=0, available=0)
    return MemorySnapshot(total=0, available=0)


def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Obtiene procesos con mayor consumo, usando caché temporal de 5s."""
    if os.name != "nt":
        return []
    
    now = time.time()
    ts, cached_processes = _PROCESS_CACHE["data"]
    
    # Retornar desde caché si es reciente
    if now - ts < 5.0 and cached_processes:
        return cached_processes[:limit]
    
    # PowerShell optimizado: limitar el procesamiento en origen para no cargar memoria de más
    command = "Get-Process | Select-Object -Property Name, Id, WorkingSet | Sort-Object WorkingSet -Descending | Select-Object -First 20 | ForEach-Object { \"$($_.Name),$($_.Id),$($_.WorkingSet)\" }"
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            new_processes = parse_windows_process_csv(proc.stdout, limit=20)
            _PROCESS_CACHE["data"] = (now, new_processes)
            return new_processes[:limit]
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def pressure_level(snapshot: MemorySnapshot) -> str:
    """Categoriza el estado de la RAM basado en el porcentaje disponible."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return "info"
    available: float = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"


def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera una lista de strings legibles describiendo el estado de memoria."""
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
    """Verifica si un proceso es crítico del SO basándose en su PID."""
    return pid in SYSTEM_CRITICAL_PIDS or pid <= 100


def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Intenta liberar el Working Set de un proceso.
    NOTA: Ver advertencia TRIM_WARNING antes de usar.
    """
    if os.name != "nt":
        return False, "Solo disponible en Windows."
    try:
        target_pid = int(pid)
    except (ValueError, TypeError):
        return False, "El PID debe ser un número entero válido."
    if target_pid == os.getpid() or _is_system_process(target_pid):
        return False, "Operación denegada: PID crítico o protegido."
    
    kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    handle = kernel32.OpenProcess(SAFE_ACCESS, False, target_pid)
    
    if not handle:
        reason = "Acceso denegado: requiere privilegios elevados." if kernel32.GetLastError() == ERROR_ACCESS_DENIED else "No se pudo abrir el proceso."
        return False, reason
        
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != STILL_ACTIVE:
            return False, "El proceso seleccionado ya no está activo."
            
        buf = ctypes.create_unicode_buffer(2048)
        if psapi.GetModuleFileNameExW(handle, 0, buf, 2048) > 0:
            exe_path = os.path.normpath(buf.value)
            if is_protected_path(exe_path):
                return False, "Operación denegada: ejecutable en ruta protegida."
        else:
            return False, "Operación denegada: no se pudo verificar la ubicación del ejecutable."
            
        if not psapi.EmptyWorkingSet(handle):
            return False, f"Error al intentar liberar memoria (código {kernel32.GetLastError()})."
            
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (ctypes.ArgumentError, Exception):
        return False, "Ocurrió un error técnico al gestionar el proceso."
    finally:
        kernel32.CloseHandle(handle)
