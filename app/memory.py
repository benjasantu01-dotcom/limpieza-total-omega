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
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict
from app.safety import ensure_safe_to_modify

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

# Advertencia que la interfaz debe mostrar junto al botón de trim.
TRIM_WARNING = (
    "Liberar el working set NO acelera la PC: fuerza a Windows a expulsar "
    "memoria que los programas están usando, y al volver a necesitarla la "
    "tiene que releer del disco. El número de 'RAM libre' sube, pero el "
    "rendimiento suele empeorar. Solo tiene sentido antes de medir algo "
    "puntual, no como mantenimiento."
)


@dataclass
class MemorySnapshot:
    """Representa un instante de la memoria física del sistema."""
    total: int
    available: int
    cached: int = 0

    @property
    def used(self) -> int:
        """Cantidad de memoria en uso en bytes."""
        return max(0, self.total - self.available)

    @property
    def used_percent(self) -> float:
        """Porcentaje de memoria física utilizada (0.0 a 100.0)."""
        if self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def available_percent(self) -> float:
        """Porcentaje de memoria física disponible (0.0 a 100.0)."""
        if self.total <= 0:
            return 0.0
        return round(self.available / self.total * 100, 1)


@dataclass
class ProcessMemory:
    """Información de consumo de memoria de un proceso específico."""
    name: str
    pid: int
    working_set: int  # Memoria física residente (RSS) en bytes
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> float:
        """Representación del Working Set en Megabytes para visualización."""
        return round(self.working_set / (1024 * 1024), 1)


def format_bytes(num: int | float) -> str:
    """Convierte bytes a una cadena legible humanamente con unidades."""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "0 B"
    if value < 0:
        value = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            decimals = 0 if unit == "B" else 1
            return f"{value:.{decimals}f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def parse_linux_meminfo(text: str) -> MemorySnapshot:
    """
    Procesa el contenido de /proc/meminfo. 
    Convierte valores expresados en kB a bytes.
    """
    values: Dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    cached = values.get("Cached", 0)
    return MemorySnapshot(total=total, available=available, cached=cached)


def parse_windows_process_csv(text: str, limit: int = 10) -> List[ProcessMemory]:
    """
    Parsea salida CSV cruda de PowerShell (Name,Id,WorkingSet).
    Filtra entradas inválidas y retorna lista ordenada por consumo.
    """
    if not text:
        return []

    processes: List[ProcessMemory] = []
    lines = text.splitlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 3:
            continue
        
        name, raw_pid, raw_ws = parts[0], parts[1], parts[2]
        
        if name.lower() in {"name", "processname"} or not raw_pid or not raw_ws:
            continue
            
        try:
            pid = int(raw_pid)
            working_set = int(float(raw_ws))
            
            if pid < 0 or working_set < 0:
                continue
                
            processes.append(ProcessMemory(
                name=name if name else "Unknown", 
                pid=pid, 
                working_set=working_set
            ))
        except (ValueError, OverflowError):
            continue
            
    processes.sort(key=lambda p: p.working_set, reverse=True)
    return processes[:limit]


def _read_windows_snapshot() -> MemorySnapshot:
    """Llamada a la API nativa de Win32 GlobalMemoryStatusEx vía ctypes."""
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
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return MemorySnapshot(total=0, available=0)
    return MemorySnapshot(total=int(stat.ullTotalPhys), available=int(stat.ullAvailPhys))


def read_snapshot() -> MemorySnapshot:
    """Captura el estado actual de la memoria del sistema detectando el SO."""
    try:
        if os.name == "nt":
            return _read_windows_snapshot()
        meminfo = "/proc/meminfo"
        if os.path.exists(meminfo):
            with open(meminfo, encoding="utf-8", errors="replace") as f:
                return parse_linux_meminfo(f.read())
    except (OSError, AttributeError, ValueError):
        pass
    return MemorySnapshot(total=0, available=0)


def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Consulta los procesos más pesados mediante PowerShell."""
    if os.name != "nt":
        return []
    command = (
        "Get-Process | Sort-Object -Property WorkingSet -Descending | "
        f"Select-Object -First {int(limit)} Name,Id,WorkingSet | "
        "ConvertTo-Csv -NoTypeInformation"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=30,
        )
        return parse_windows_process_csv(result.stdout or "", limit=limit)
    except (OSError, subprocess.SubprocessError):
        return []


def pressure_level(snapshot: MemorySnapshot) -> str:
    """
    Clasifica el estado de presión:
    - >= 35% disponible: OK
    - >= 20% disponible: Info
    - >= 10% disponible: Warning
    - < 10% disponible: Danger
    """
    if snapshot.total <= 0:
        return "info"
    available = snapshot.available_percent
    if available >= 35:
        return "ok"
    if available >= 20:
        return "info"
    if available >= 10:
        return "warning"
    return "danger"


def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un reporte textual descriptivo basado en el estado actual."""
    if snapshot.total <= 0:
        return ["No se pudo leer el estado de la memoria en este sistema."]

    level = pressure_level(snapshot)
    lines = [
        f"Memoria total: {format_bytes(snapshot.total)}",
        f"En uso: {format_bytes(snapshot.used)} ({snapshot.used_percent}%)",
        f"Disponible: {format_bytes(snapshot.available)} ({snapshot.available_percent}%)",
    ]

    diagnosticos = {
        "ok": "Estado: holgado. No hace falta 'liberar' nada: la memoria ocupada por caché es la que hace que los programas abran rápido.",
        "info": "Estado: normal. Windows está usando la memoria como corresponde. Liberarla a la fuerza no mejoraría el rendimiento.",
        "warning": "Estado: ajustado. Conviene cerrar programas que no estés usando; eso sí libera memoria de verdad, a diferencia de un 'limpiador'.",
        "danger": "Estado: crítico. El sistema probablemente esté yendo al disco. Cerrá los procesos más pesados de la lista o considerá más RAM."
    }
    
    lines.append(diagnosticos.get(level, ""))

    for proc in (processes or [])[:3]:
        lines.append(f"  Mayor consumo: {proc.name} (PID {proc.pid}) — {proc.working_set_mb} MB")

    return lines


def trim_working_set(pid: int) -> Tuple[bool, str]:
    """
    Ejecuta el trim (purga) del working set de un proceso en Windows.
    Requiere validación de seguridad previa para evitar tocar procesos críticos.
    """
    if os.name != "nt":
        return False, "Solo disponible en Windows."
    
    try:
        target_pid = int(pid)
        if target_pid <= 0:
            return False, "PID inválido proporcionado."
        
        # Verificación de seguridad: no manipular procesos del núcleo o críticos
        if not ensure_safe_to_modify(f"pid:{target_pid}"):
            return False, "Operación rechazada: el proceso está protegido por seguridad."

    except (ValueError, TypeError):
        return False, "El PID debe ser un número entero."

    try:
        import ctypes

        PROCESS_SET_QUOTA = 0x0100
        PROCESS_QUERY_INFORMATION = 0x0400
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, target_pid
        )
        if not handle:
            return False, f"No se pudo abrir el proceso {target_pid} (¿permisos insuficientes?)."
        
        try:
            if not ctypes.windll.psapi.EmptyWorkingSet(handle):
                error_code = ctypes.GetLastError()
                return False, f"Windows rechazó la operación (código: {error_code})."
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        
        return True, f"Working set del proceso {target_pid} liberado. {TRIM_WARNING}"
    except (OSError, AttributeError) as e:
        return False, f"Error al interactuar con el sistema: {e}"
