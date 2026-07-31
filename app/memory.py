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
from functools import lru_cache
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, Iterator
from safety import is_protected_path

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
    """Consumo de memoria de un proceso (RSS/Working Set)."""
    name: str
    pid: int
    working_set: int
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> float:
        """Convierte bytes a MB para legibilidad en la UI."""
        return round(self.working_set / 1048576, 1)


def format_bytes(num: int | float | None) -> str:
    """
    Convierte un valor numérico en bytes a formato de cadena legible.
    Optimizado para evitar cálculos logarítmicos innecesarios.
    """
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    
    val = float(num)
    idx = 0
    while val >= 1024 and idx < len(BYTE_UNITS) - 1:
        val /= 1024
        idx += 1
        
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"


@lru_cache(maxsize=4)
def parse_linux_meminfo(text: str) -> MemorySnapshot:
    """
    Parsea /proc/meminfo. Convierte la escala original en kB a bytes.
    Retorna un MemorySnapshot normalizado.
    """
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


def parse_windows_process_csv(text: str, limit: int = 10) -> List[ProcessMemory]:
    """
    Convierte CSV generado por PowerShell a objetos ProcessMemory.
    Filtra entradas inválidas y ordena por consumo descendente.
    """
    if not text:
        return []

    def _safe_create_proc(parts: List[str]) -> Optional[ProcessMemory]:
        try:
            return ProcessMemory(
                name=parts[0] or "Unknown",
                pid=int(parts[1]),
                working_set=int(parts[2])
            )
        except (ValueError, IndexError):
            return None

    def _extract_rows() -> Iterator[ProcessMemory]:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("Name,"):
                continue
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) >= 3:
                proc = _safe_create_proc(parts)
                if proc:
                    yield proc

    return sorted(_extract_rows(), key=lambda p: p.working_set, reverse=True)[:max(0, limit)]


def _read_windows_snapshot() -> MemorySnapshot:
    """
    Accede a la API Win32 GlobalMemoryStatusEx vía ctypes.
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
    """Determina el SO y captura las métricas de memoria correspondientes."""
    if os.name == "nt":
        try:
            return _read_windows_snapshot()
        except (AttributeError, OSError, MemoryError):
            return MemorySnapshot(total=0, available=0)
            
    meminfo = "/proc/meminfo"
    if os.path.exists(meminfo):
        try:
            with open(meminfo, encoding="utf-8", errors="replace") as f:
                content = f.read()
                if content:
                    return parse_linux_meminfo(content)
        except (OSError, ValueError):
            pass
            
    return MemorySnapshot(total=0, available=0)


def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """
    Obtiene los procesos de mayor consumo en Windows ejecutando PowerShell.
    """
    if os.name != "nt":
        return []
    command = (
        f"Get-Process | Select-Object -Property Name,Id,WorkingSet | "
        "Sort-Object -Property WorkingSet -Descending | "
        f"Select-Object -First {max(1, int(limit))} | "
        "ConvertTo-Csv -NoTypeInformation"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=10,
        )
        return parse_windows_process_csv(result.stdout or "", limit=limit)
    except (OSError, subprocess.SubprocessError):
        return []


def pressure_level(snapshot: MemorySnapshot) -> str:
    """
    Clasifica la salud de la RAM basándose en el porcentaje disponible.
    """
    if snapshot.total <= 0:
        return "info"
    available = snapshot.available_percent
    if available >= 35: return "ok"
    if available >= 20: return "info"
    if available >= 10: return "warning"
    return "danger"


def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Crea un informe textual legible para el usuario final."""
    if not isinstance(snapshot, MemorySnapshot) or snapshot.total <= 0:
        return ["No se pudo leer el estado de la memoria en este sistema."]

    level = pressure_level(snapshot)
    lines = [
        f"Memoria total: {format_bytes(snapshot.total)}",
        f"En uso: {format_bytes(snapshot.used)} ({snapshot.used_percent}%)",
        f"Disponible: {format_bytes(snapshot.available)} ({snapshot.available_percent}%)",
    ]

    diagnosticos = {
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
    Solicita al OS liberar el working set de un proceso específico tras validación de seguridad.
    """
    if os.name != "nt":
        return False, "Solo disponible en Windows."
    
    try:
        target_pid = int(pid)
    except (ValueError, TypeError):
        return False, "El PID debe ser un número entero válido."

    if target_pid <= 4 or is_protected_path(str(target_pid)):
        return False, "Operación denegada: PID de sistema protegido."
    
    import ctypes
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi

    if target_pid == os.getpid():
        return False, "Operación denegada: proceso de la app."

    if not hasattr(kernel32, "OpenProcess") or not hasattr(psapi, "EmptyWorkingSet"):
        return False, "APIs de sistema no disponibles."

    # PROCESS_QUERY_INFORMATION (0x0400) | PROCESS_SET_QUOTA (0x0100) | PROCESS_VM_WRITE (0x0020)
    # Requerido para invocar EmptyWorkingSet sobre un proceso externo.
    handle = kernel32.OpenProcess(0x0520, False, target_pid)
    if not handle:
        return False, f"No se pudo acceder al proceso {target_pid}."
    
    try:
        if not psapi.EmptyWorkingSet(handle):
            err_code = kernel32.GetLastError()
            return False, f"Error al limpiar memoria (WinError {err_code})."
        return True, f"Working set liberado. {TRIM_WARNING}"
    except (ctypes.ArgumentError, Exception):
        return False, "Error inesperado al intentar limpiar la memoria."
    finally:
        kernel32.CloseHandle(handle)
