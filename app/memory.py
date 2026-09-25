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
from typing import List, Tuple, Optional, Dict, TYPE_CHECKING, Final, Set, NewType, Iterator
from safety import is_protected_path, is_safe_to_modify

if TYPE_CHECKING:
    from ctypes import wintypes
else:
    class wintypes:
        HANDLE = ctypes.c_void_p

# Tipos semánticos para prevenir errores de lógica al operar con diferentes unidades.
BytesValue = NewType("BytesValue", int)
MegabytesValue = NewType("MegabytesValue", float)

# Constantes de conversión y límites de seguridad:
BYTES_IN_MB: Final[int] = 1024 * 1024
BYTE_UNITS: Final[Tuple[str, ...]] = ("B", "KB", "MB", "GB", "TB")
MAX_VALID_PROCESS_MEM: Final[int] = 128 * 1024 * BYTES_IN_MB 

# Máscaras de acceso Win32:
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
PROCESS_SET_QUOTA: Final[int] = 0x100
PROCESS_QUERY_INFORMATION: Final[int] = 0x0400
SAFE_VALIDATION_MASK: Final[int] = PROCESS_QUERY_LIMITED_INFORMATION 
TRIM_ACCESS_MASK: Final[int] = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA

STILL_ACTIVE_EXIT_CODE: Final[int] = 259
SYSTEM_CRITICAL_PIDS: Final[Set[int]] = {0, 4}
ERROR_ACCESS_DENIED: Final[int] = 5
ERROR_INVALID_PARAMETER: Final[int] = 87

PS_QUERY_CMD: Final[List[str]] = [
    'powershell', '-NoProfile', '-NonInteractive', '-Command', 
    'Get-Process | Where-Object { $_.Id -notin 0,4 } | ForEach-Object { "$($_.Name),$($_.Id),$($_.WorkingSet)" }'
]

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
    """Estructura Win32 mapeada para la API GlobalMemoryStatusEx."""
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
    """Estado inmutable de la RAM global capturado en un momento dado."""
    total: BytesValue
    available: BytesValue
    cached: BytesValue = BytesValue(0)

    @property
    def used(self) -> BytesValue:
        """Calcula memoria en uso: total - disponible (ajustado a >= 0)."""
        return BytesValue(max(0, self.total - self.available))

    @property
    def used_percent(self) -> float:
        """Retorna porcentaje de uso relativo al total físico."""
        if self.total <= 0: return 0.0
        return round((float(self.used) / float(self.total)) * 100, 1)

    @property
    def available_percent(self) -> float:
        """Retorna porcentaje de disponibilidad relativo al total físico."""
        if self.total <= 0: return 0.0
        return round((float(self.available) / float(self.total)) * 100, 1)

@dataclass
class ProcessMemory:
    """Representación de consumo de memoria de un proceso individual."""
    name: str
    pid: int
    working_set: BytesValue
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def working_set_mb(self) -> MegabytesValue:
        """Convierte bytes de memoria de trabajo a MiB para reportes."""
        return MegabytesValue(round(self.working_set / BYTES_IN_MB, 1))

def format_bytes(num: Optional[int | float]) -> str:
    """Convierte un valor numérico a representación humana (SI)."""
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 B"
    idx: int = min(int(math.log(num, 1024)), len(BYTE_UNITS) - 1)
    val: float = num / (1024 ** idx)
    return f"{val:.{0 if idx == 0 else 1}f} {BYTE_UNITS[idx]}"

def _create_mem_status_ex() -> MEMORYSTATUSEX:
    """Instancia MEMORYSTATUSEX y configura el tamaño requerido por la API Win32."""
    mem_status = MEMORYSTATUSEX()
    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    return mem_status

def _safe_int_conversion(value: Optional[str], multiplier: int = 1) -> BytesValue:
    """Limpia caracteres no numéricos y escala el valor a BytesValue."""
    if not value: return BytesValue(0)
    clean_val = "".join(c for c in value if c.isdigit())
    return BytesValue(int(clean_val) * multiplier) if clean_val else BytesValue(0)

_is_windows: bool = os.name == "nt"
_linux_mem_path: Path = Path("/proc/meminfo")
_EMPTY_SNAPSHOT: MemorySnapshot = MemorySnapshot(BytesValue(0), BytesValue(0))
_linux_available: bool = True
_proc_cache_time: float = 0.0
_proc_cache_data: List[ProcessMemory] = []

@lru_cache(maxsize=4)
def parse_linux_meminfo(meminfo_text: str) -> MemorySnapshot:
    """Parsea el archivo /proc/meminfo para extraer métricas de RAM en Linux."""
    if not meminfo_text: return _EMPTY_SNAPSHOT
    metrics: Dict[str, BytesValue] = {}
    for line in meminfo_text.splitlines():
        if ":" not in line: continue
        key, _, value_part = line.partition(":")
        metrics[key.strip()] = _safe_int_conversion(value_part, 1024)
            
    total: BytesValue = metrics.get("MemTotal", BytesValue(0))
    if total <= 0: return _EMPTY_SNAPSHOT
    available = metrics.get("MemAvailable", metrics.get("MemFree", BytesValue(0)))
    return MemorySnapshot(total=total, available=BytesValue(min(available, total)), cached=metrics.get("Cached", BytesValue(0)))

def parse_windows_process_csv(raw_csv_text: str, limit: int = 10) -> List[ProcessMemory]:
    """Transforma el CSV generado por PowerShell a una lista de objetos ProcessMemory."""
    if not raw_csv_text: return []
    seen_pids: Set[int] = set()

    def process_generator() -> Iterator[ProcessMemory]:
        for line in raw_csv_text.splitlines():
            line = line.strip()
            if not line: continue
            parts = line.split(",", 2)
            if len(parts) == 3:
                try:
                    pid_val = int("".join(c for c in parts[1] if c.isdigit()))
                    ws_val = int("".join(c for c in parts[2] if c.isdigit()))
                    if pid_val > 0 and pid_val not in seen_pids and ws_val < MAX_VALID_PROCESS_MEM:
                        seen_pids.add(pid_val)
                        yield ProcessMemory(parts[0].strip("'\" "), pid_val, BytesValue(ws_val))
                except (ValueError, TypeError, OverflowError): 
                    continue

    return sorted(process_generator(), key=lambda p: p.working_set, reverse=True)[:limit]

def _read_windows_snapshot() -> MemorySnapshot:
    """Invoca GlobalMemoryStatusEx para obtener métricas físicas del sistema (Win32 API)."""
    kernel32 = ctypes.windll.kernel32
    if not hasattr(kernel32, "GlobalMemoryStatusEx"): return _EMPTY_SNAPSHOT
    mem_status = _create_mem_status_ex()
    if kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_status)) != 0 and mem_status.ullTotalPhys > 0:
        return MemorySnapshot(total=BytesValue(mem_status.ullTotalPhys), available=BytesValue(mem_status.ullAvailPhys))
    return _EMPTY_SNAPSHOT

@lru_cache(maxsize=1)
def _get_cached_snapshot(timestamp_bucket: int) -> MemorySnapshot:
    """Obtiene el estado de RAM global, cacheando el resultado por periodos cortos."""
    if _is_windows: return _read_windows_snapshot()
    global _linux_available
    if _linux_available:
        try:
            snapshot = parse_linux_meminfo(_linux_mem_path.read_text(encoding="utf-8"))
            return snapshot if snapshot != _EMPTY_SNAPSHOT else _EMPTY_SNAPSHOT
        except (OSError, PermissionError, UnicodeDecodeError): _linux_available = False
    return _EMPTY_SNAPSHOT

def read_snapshot() -> MemorySnapshot:
    """Punto de entrada para obtener un snapshot global, refrescado cada 5 segundos."""
    return _get_cached_snapshot(int(time.time() / 5))

def top_memory_processes(limit: int = 10) -> List[ProcessMemory]:
    """Retorna los procesos de mayor consumo con un TTL de caché de 60 segundos."""
    global _proc_cache_time, _proc_cache_data
    if not _is_windows: return []
    now = time.time()
    if (now - _proc_cache_time) > 60:
        try:
            proc = subprocess.run(PS_QUERY_CMD, capture_output=True, text=True, timeout=3, check=False)
            if proc.returncode == 0 and proc.stdout:
                _proc_cache_data = parse_windows_process_csv(proc.stdout, limit=limit)
                _proc_cache_time = now
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired): pass
    return _proc_cache_data

@lru_cache(maxsize=8)
def pressure_level(snapshot: MemorySnapshot) -> str:
    """Clasifica el nivel de estrés de memoria basado en el porcentaje disponible."""
    if snapshot.total <= 0: return "info"
    avail = snapshot.available_percent
    if avail >= 35: return "ok"
    if avail >= 20: return "info"
    return "warning" if avail >= 10 else "danger"

def diagnose(snapshot: MemorySnapshot, processes: Optional[List[ProcessMemory]] = None) -> List[str]:
    """Genera un diagnóstico en lenguaje claro sobre el estado actual de la RAM."""
    if snapshot.total <= 0: return ["No se pudo leer el estado de la memoria."]
    diagnostics = {
        "ok": "Estado: holgado. La memoria ocupada por caché mejora la velocidad.",
        "info": "Estado: normal. Windows gestiona la memoria de forma eficiente.",
        "warning": "Estado: ajustado. Conviene cerrar aplicaciones innecesarias.",
        "danger": "Estado: crítico. El sistema recurre al archivo de paginación."
    }
    report = [
        f"Memoria total: {format_bytes(snapshot.total)}",
        f"En uso: {format_bytes(snapshot.used)} ({snapshot.used_percent}%)",
        f"Disponible: {format_bytes(snapshot.available)} ({snapshot.available_percent}%)",
        diagnostics.get(pressure_level(snapshot), "")
    ]
    if processes:
        report.extend(f"  Mayor consumo: {p.name} (PID {p.pid}) — {p.working_set_mb} MB" for p in processes[:3])
    return report

def _is_system_process(pid: int) -> bool:
    """Verifica si el PID corresponde a un proceso crítico del SO o a la app actual."""
    return pid in SYSTEM_CRITICAL_PIDS or pid == os.getpid()

def _get_process_path(pid: int) -> Optional[Path]:
    """Resuelve la ruta absoluta del ejecutable de un proceso mediante Win32 API."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SAFE_VALIDATION_MASK, False, pid)
    if not handle: return None
    try:
        psapi = ctypes.windll.psapi
        buf = ctypes.create_unicode_buffer(1024)
        length = psapi.GetModuleFileNameExW(handle, None, buf, 1024)
        if length > 0 and length < 1024:
            p = Path(buf.value).resolve(strict=False)
            if p.is_file() and p.is_absolute() and not is_protected_path(str(p)):
                return p
    except (ctypes.ArgumentError, OSError, ValueError, TypeError): 
        pass
    finally: kernel32.CloseHandle(handle)
    return None

def _is_safe_to_trim(pid: int) -> Tuple[bool, Optional[str]]:
    """Valida si la ruta del proceso es modificable antes de liberar su memoria."""
    exec_path = _get_process_path(pid)
    if not exec_path or not is_safe_to_modify(str(exec_path)):
        return False, "Acceso no autorizado o ruta protegida."
    return True, None

def trim_working_set(pid: int | str) -> Tuple[bool, str]:
    """Libera el working set de un proceso específico tras validar su seguridad."""
    if not _is_windows: return False, "Solo soportado en Windows."
    try: target_pid = int(pid)
    except (ValueError, TypeError): return False, "PID no válido."
    
    if _is_system_process(target_pid): 
        return False, "Proceso crítico protegido."
    
    psapi = ctypes.windll.psapi
    if not hasattr(psapi, "EmptyWorkingSet"): 
        return False, "Función no disponible."

    is_safe, err = _is_safe_to_trim(target_pid)
    if not is_safe: return False, err or "Verificación fallida."

    kernel32 = ctypes.windll.kernel32
    proc_handle = kernel32.OpenProcess(TRIM_ACCESS_MASK, False, target_pid)
    if not proc_handle or proc_handle == 0: 
        if ctypes.GetLastError() == ERROR_ACCESS_DENIED:
            return False, "Acceso denegado: requiere privilegios elevados."
        return False, "No se pudo abrir el proceso para modificación."
    
    try:
        if not psapi.EmptyWorkingSet(proc_handle):
            return False, "Operación denegada por el sistema."
        return True, f"Working set liberado. {TRIM_WARNING}"
    finally: 
        kernel32.CloseHandle(proc_handle)
