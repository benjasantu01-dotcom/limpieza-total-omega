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
    """
    Representa una instantánea de la memoria física del sistema.
    Valores almacenados internamente siempre en bytes para mantener consistencia.
    """
    total: BytesValue
    available: BytesValue
    cached: BytesValue = 0

    @property
    def used(self) -> BytesValue:
        """Calcula memoria ocupada en bytes (Total - Disponible). No retorna negativos."""
        return max(0, self.total - self.available)

    @property
    def used_percent(self) -> float:
        """Porcentaje de uso (0-100) calculado sobre la capacidad total."""
        if self.total <= 0:
            return 0.0
        return round((self.used / self.total) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Porcentaje disponible (0-100) calculado sobre la capacidad total."""
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
        """Representación del Working Set escalada a Megabytes."""
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
    Procesa un archivo tipo /proc/meminfo. 
    Busca métricas clave (kB), las convierte a bytes y genera un MemorySnapshot.
    """
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
    """Valida que una fila CSV de proceso tenga columnas Name, PID y WorkingSet (int)."""
    return (len(parts) >= 3 and 
            parts[1].strip().isdigit() and 
            parts[2].strip().isdigit() and 
            int(parts[2].strip()) >= 0)


def parse_windows_process_csv(text: str, limit: int = 10) -> List[ProcessMemory]:
    """
    Interpreta el output CSV crudo de 'Get-Process' (PowerShell).
    Retorna una lista ordenada por consumo (descendente) de objetos ProcessMemory.
    """
    if not text:
        return []

    lines = text.splitlines()
    if len(lines) < 2:
        return []

    def _gen_proc():
        for line in lines[1:]:
            clean_line = line.strip()
            if not clean_line: continue
            parts = [p.strip().strip('"') for p in clean_line.split(",")]
            if _is_valid_process_row(parts):
                try:
                    yield ProcessMemory(name=parts[0] or "Unknown", pid=int(parts[1]), working_set=int(parts[2]))
                except (ValueError, IndexError):
                    continue

    return sorted(_gen_proc(), key=lambda p: p.working_set, reverse=True)[:limit]


def _read_windows_snapshot() -> MemorySnapshot:
    """
    Invoca la API Win32 'GlobalMemoryStatusEx' vía ctypes.
    Es la forma estándar y precisa de consultar memoria en Windows.
    """
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
    Punto de entrada unificado para obtener el estado de memoria actual.
    Detecta OS y delega la lógica de lectura cruda a funciones específicas.
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
                if content:
                    return parse_linux_meminfo(content)
        except (OSError, PermissionError, IOError):
            return MemorySnapshot(total=0, available=0)
            
    return MemorySnapshot(total=0, available=0)


def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """
    Retorna los procesos más pesados. Implementa una caché de 5s para evitar
    la sobrecarga de lanzar procesos hijos de PowerShell constantemente.
    """
    if os.name != "nt":
        return []

    now = time.time()
    ts, cached_processes = _PROCESS_CACHE["data"]
    if now - ts < 5.0:
        return cached_processes[:limit]

    # Command optimization: Access directly via calculated properties to avoid heavy CSV piping
    command: str = (
        "Get-Process | Sort-Object WorkingSet -Descending | Select-Object -First 20 "
        "| ForEach-Object { \"$($_.Name),$($_.Id),$($_.WorkingSet)\" }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            lines = result.stdout.splitlines()
            processes = []
            for line in lines:
                parts = line.split(",")
                if len(parts) == 3:
                    processes.append(ProcessMemory(name=parts[0], pid=int(parts[1]), working_set=int(parts[2])))
            
            _PROCESS_CACHE["data"] = (now, processes)
            return processes[:limit]
    except (OSError, subprocess.SubprocessError, Exception):
        pass
    return []


def pressure_level(snapshot: MemorySnapshot) -> str:
    """
    Clasifica el estado de presión basándose en el % de RAM libre.
    Retorna un identificador (ok, info, warning, danger) para el reporte.
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
    Transforma un MemorySnapshot y una lista de procesos en líneas de texto
    listas para ser renderizadas en la interfaz (UI).
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
    """Verifica si el PID es crítico (kernel/system) para prevenir manipulación."""
    return pid in SYSTEM_CRITICAL_PIDS or pid <= 100


def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """
    Solicita al S.O. reducir el Working Set (RAM asignada en memoria física) 
    de un proceso, forzando la paginación de páginas inactivas al disco.

    Args:
        pid: Identificador del proceso objetivo (entero o cadena).

    Returns:
        Tupla (éxito: bool, mensaje: str) describiendo el resultado de la operación.
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
    
    # Intenta obtener acceso de lectura y escritura al proceso
    handle = kernel32.OpenProcess(REQUIRED_ACCESS, False, target_pid)
    if not handle or handle <= 0:
        return False, "Acceso denegado: permisos insuficientes o el proceso ya no existe."
    
    try:
        # Validación: evita tocar procesos residentes en directorios protegidos del sistema
        buf = ctypes.create_unicode_buffer(2048)
        bytes_copied = psapi.GetModuleFileNameExW(handle, 0, buf, 2048)
        if bytes_copied > 0:
            path_str = buf.value
            if not path_str or is_protected_path(path_str):
                return False, "Operación denegada: el ejecutable está en una ruta protegida."
        else:
            return False, "Operación denegada: no se pudo verificar la integridad del proceso."

        # Validación: verifica que el proceso siga activo
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
            return False, "El proceso seleccionado ya no está activo."

        # Invocación de la API crítica: EmptyWorkingSet
        if not psapi.EmptyWorkingSet(handle):
            return False, "Error al intentar liberar memoria del proceso."
        return True, f"Working set liberado. {TRIM_WARNING}"
    except Exception:
        return False, "Ocurrió un error inesperado al gestionar el proceso."
    finally:
        kernel32.CloseHandle(handle)
