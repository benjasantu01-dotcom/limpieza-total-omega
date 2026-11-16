"""
scanner.py
Detector HEURÍSTICO de archivos sospechosos. Este módulo realiza un análisis 
estático mediante heurísticas de nombre, extensión y metadatos de archivo, 
complementando la protección de Windows Defender.

El módulo utiliza un recorrido iterativo seguro para navegar el sistema de archivos, 
aplicando validaciones estrictas antes de cada acceso para evitar la resolución de 
puntos de reanálisis (Junctions/Symlink) y rutas protegidas.
"""

from __future__ import annotations
import subprocess
import re
import logging
import os
import ctypes
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from functools import lru_cache
from collections import OrderedDict
from typing import List, Optional, Union, Final, Protocol, NamedTuple, Sequence, Tuple, TypeAlias, TYPE_CHECKING
from safety import is_protected_path

if TYPE_CHECKING:
    from typing import Type

# Configuración de logger para el seguimiento de excepciones en el escaneo
logger: Final = logging.getLogger(__name__)

# Constantes de Win32 API para la identificación precisa de estructuras de archivos
FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400
FILE_ATTRIBUTE_DIRECTORY: Final = 0x10
ERROR_SHARING_VIOLATION: Final = 32
ERROR_ACCESS_DENIED: Final = 5
ERROR_FILE_NOT_FOUND: Final = 2
IO_REPARSE_TAG_MOUNT_POINT: Final = 0xA0000003
IO_REPARSE_TAG_SYMLINK: Final = 0xA000000C

class ScannerLimits(NamedTuple):
    """Define los umbrales de seguridad y rendimiento para el motor de escaneo."""
    path_length_limit: int = 260
    recent_file_window_hours: int = 24
    reparse_point_mask: int = FILE_ATTRIBUTE_REPARSE_POINT
    max_recursion_depth: int = 50
    cache_max_size: int = 1024

SCAN_LIMITS: Final = ScannerLimits()

@dataclass(frozen=True)
class Suspicion:
    """Representa una anomalía encontrada durante el análisis estático."""
    path: Path
    reason: str
    severity: str

class SuspicionCheck(Protocol):
    """
    Contrato para heurísticas de evaluación de riesgos.
    
    Args:
        path: Ruta absoluta del archivo.
        entry: Entrada de directorio (opcional para evitar stats innecesarios).
        now_ts: Timestamp actual para cálculos de antigüedad.
        stats: Metadatos del archivo.
    """
    def __call__(
        self, 
        path: Path, 
        entry: Optional[os.DirEntry] = None, 
        now_ts: float = 0.0, 
        stats: Optional[os.stat_result] = None
    ) -> Optional[Suspicion]: ...

ScanResult: TypeAlias = List[Suspicion]
TraversalStack: TypeAlias = List[Tuple[str, int]]
Timestamp: TypeAlias = float
PathInput: TypeAlias = Union[Path, str, None]
VisitedNodes: TypeAlias = set[str]

DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d\u202b\u200b\u00a0\ufeff]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
INVALID_TRAILING_CHARS_RE: Final[re.Pattern] = re.compile(r"[<>:\"|?*]")
UNC_PATH_RE: Final[re.Pattern] = re.compile(r"^\\\\[^\\\\]+\\")

SUSPICIOUS_ALL_EXTS: Final[frozenset[str]] = frozenset((".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1", ".pdf"))
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
TARGETED_DOWNLOAD_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})
SYSTEM32_LOWER: Final[str] = "system32"

@lru_cache(maxsize=4096)
def _cached_is_protected(path_str: str) -> bool:
    """Verifica si una ruta está marcada como protegida usando caché LRU."""
    try:
        return is_protected_path(Path(path_str))
    except (ValueError, TypeError, OSError):
        return True

def _validate_heuristic_input(path: Path, stats: Optional[os.stat_result]) -> bool:
    """Valida la integridad de los metadatos necesarios para ejecutar heurísticas."""
    return path is not None and stats is not None and hasattr(stats, 'st_size')

def _get_canonical_path(path_str: str) -> str:
    """Normaliza rutas asegurando consistencia y longitud máxima para evitar fallos de OS."""
    if not isinstance(path_str, str) or "\0" in path_str:
        return ""
    try:
        clean_path = os.path.normpath(path_str.strip())
        if not clean_path: return ""
        norm = os.path.normcase(os.path.abspath(clean_path))
        return norm if len(norm) <= SCAN_LIMITS.path_length_limit else ""
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return ""

def _is_safe_by_attributes(path: Path) -> bool:
    """Valida que la ruta no sea un punto de reanálisis (Junction/Symlink) vía atributos Win32."""
    try:
        attrs = path.stat().st_file_attributes
        return not (attrs & SCAN_LIMITS.reparse_point_mask)
    except (OSError, AttributeError):
        return True

def _is_valid_file_path(path: PathInput) -> bool:
    """Valida existencia, permisos y restricciones de seguridad de una ruta."""
    if not isinstance(path, (Path, str)):
        return False
    abs_path = _get_canonical_path(str(path))
    if not abs_path: return False
    target_path = Path(abs_path)
    try:
        if not target_path.name or RESERVED_NAMES_RE.match(target_path.name): return False
        return target_path.exists() and not _cached_is_protected(abs_path) and target_path.is_file() and _is_safe_by_attributes(target_path)
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError):
        return False

def _is_readable(path: PathInput) -> bool:
    """Comprueba privilegios de lectura para el usuario actual."""
    abs_path = _get_canonical_path(str(path) if path else "")
    if not abs_path or _cached_is_protected(abs_path):
        return False
    try:
        return os.access(abs_path, os.R_OK)
    except (OSError, PermissionError, ValueError, TypeError):
        return False

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Extrae atributos de bajo nivel sin seguir enlaces simbólicos."""
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError, FileNotFoundError):
        return 0

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """Obtiene metadatos asegurando que el archivo no sea un punto de reanálisis."""
    try:
        if _get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask:
            return None
        stats = entry.stat(follow_symlinks=False)
        return stats if (stats is not None and getattr(stats, 'st_size', -1) >= 0) else None
    except OSError as e:
        if e.winerror not in (ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND):
            logger.debug(f"Error crítico en stats para {entry.path}: {e}")
        return None
    except (AttributeError, PermissionError, TypeError):
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Verifica si la ruta respeta convenciones seguras (sin caracteres RTL o rutas UNC)."""
    abs_path = _get_canonical_path(path_str or "")
    if not abs_path or _cached_is_protected(abs_path):
        return False
    try:
        base_name = os.path.basename(abs_path)
        if not base_name or RESERVED_NAMES_RE.match(base_name): return False
        if UNC_PATH_RE.match(abs_path) or RTL_CHAR_RE.search(base_name): return False
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False

# --- Suite de Heurísticas ---

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Identifica archivos de 0 bytes, a veces usados como marcadores de relleno por malware."""
    if _validate_heuristic_input(path, stats) and stats.st_size == 0:
        return Suspicion(path, "Archivo vacío detectado (posible marcador de relleno)", "warning")
    return None

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Detecta ofuscación de ejecutables con extensión doble (e.g., 'documento.pdf.exe')."""
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión detectada como técnica de ofuscación", "warning")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Detecta binarios que imitan nombres de procesos críticos del sistema fuera de System32."""
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        if SYSTEM32_LOWER not in str(path).lower():
            return Suspicion(path, "Nombre de proceso del sistema fuera de ubicación legítima", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Flag: Identifica ejecutables descargados en las últimas horas en carpetas volátiles."""
    if _validate_heuristic_input(path, stats):
        try:
            parent_name = path.parent.name.lower()
            if parent_name in TARGETED_DOWNLOAD_FOLDERS:
                mtime = getattr(stats, 'st_mtime', 0.0)
                if isinstance(mtime, (int, float)) and mtime > 0:
                    age_seconds = now_ts - float(mtime)
                    if age_seconds < (SCAN_LIMITS.recent_file_window_hours * 3600):
                        return Suspicion(path, "Ejecutable reciente en carpeta volátil", "info")
        except (AttributeError, ValueError, TypeError) as e:
            logger.debug(f"Error procesando metadatos temporales para {path}: {e}")
    return None

ALL_CHECKS: Final[Sequence[SuspicionCheck]] = (
    check_empty_file,
    check_double_extension,
    check_system_lookalike,
    check_recent_executable_in_downloads
)

class Scanner:
    """Motor que coordina el recorrido recursivo y el análisis heurístico."""
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.visited_paths: set[str] = set()
        self.attr_cache: OrderedDict[str, os.stat_result] = OrderedDict()
        self.root_canonical: str = _get_canonical_path(str(base_root))
        self.now_ts: float = datetime.now().timestamp()
        self._relevant_exts: Final[frozenset[str]] = SUSPICIOUS_ALL_EXTS

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica que la ruta se mantenga bajo el directorio raíz definido."""
        return entry_path.startswith(self.root_canonical)

    def _has_invalid_name(self, name: str) -> bool:
        """Valida si el nombre del archivo contiene caracteres o patrones prohibidos."""
        return bool(not name or INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name) or RTL_CHAR_RE.search(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si una entrada es un punto de reanálisis (evita recursión infinita)."""
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask)

    def _is_safe_entry(self, entry: os.DirEntry, norm_path: str) -> bool:
        """Valida que la entrada sea segura para procesar según políticas de acceso y scope."""
        try:
            if not entry.name or self._has_invalid_name(entry.name) or self._is_reparse_point(entry): return False
            if not norm_path.startswith(self.root_canonical) or _cached_is_protected(norm_path): return False
            return os.access(entry.path, os.R_OK)
        except OSError: return False

    def _handle_directory(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: VisitedNodes, norm_path: str) -> None:
        """Gestiona la cola de directorios para evitar sobrepasar los límites de recursión."""
        if depth >= SCAN_LIMITS.max_recursion_depth: return
        if norm_path not in visited:
            visited.add(norm_path)
            stack.append((entry.path, depth + 1))

    def _is_relevant_extension(self, name: str) -> bool:
        """Filtra archivos que no tienen interés para el escaneo heurístico."""
        _, ext = os.path.splitext(name)
        return ext.lower() in self._relevant_exts

    def process_entry(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: VisitedNodes) -> None:
        """Gestiona el flujo de análisis según tipo: recursión para carpetas, heurística para archivos."""
        norm_path = os.path.normcase(entry.path)
        try:
            if not self._is_safe_entry(entry, norm_path): return
            if entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, stack, depth, visited, norm_path)
            elif entry.is_file(follow_symlinks=False) and self._is_relevant_extension(entry.name):
                self._run_file_heuristics(Path(entry.path), entry)
        except OSError:
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta toda la suite de heurísticas sobre el archivo y gestiona su caché de atributos."""
        stats = self.attr_cache.get(str(path)) or _safe_stat(entry)
        if not stats: return
        
        if len(self.attr_cache) < SCAN_LIMITS.cache_max_size:
            self.attr_cache[str(path)] = stats
            
        for check_fn in ALL_CHECKS:
            try:
                finding = check_fn(path, entry, self.now_ts, stats)
                if finding: self.results.append(finding)
            except Exception as e:
                logger.debug(f"Heurística {check_fn.__name__} falló en {path}: {e}")

def scan_file(path: Path, now_ts: Timestamp, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Punto de entrada para escanear un único archivo."""
    if not _is_valid_path_structure(str(path)) or not _is_readable(path): return []
    try:
        stats = entry.stat(follow_symlinks=False) if entry else path.stat()
    except (OSError, PermissionError): return []
    findings: ScanResult = []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts, stats)
            if res: findings.append(res)
        except Exception as e:
            logger.debug(f"Heurística {check_fn.__name__} falló: {e}")
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Escaneo recursivo completo de un directorio dado."""
    if directory is None: return []
    abs_path_str = _get_canonical_path(str(directory))
    if not abs_path_str or _cached_is_protected(abs_path_str): return []
    try:
        if not os.path.isdir(abs_path_str) or not os.access(abs_path_str, os.R_OK): return []
        scanner: Scanner = Scanner(base_root=Path(abs_path_str))
        stack: TraversalStack = [(abs_path_str, 0)]
        visited: VisitedNodes = {abs_path_str}
        while stack:
            current_dir, depth = stack.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        scanner.process_entry(entry, stack, depth, visited)
            except (PermissionError, OSError): continue
        return scanner.results
    except Exception: return []

def run_windows_defender_quick_scan() -> str:
    """Verifica estado de Defender y solicita un escaneo rápido vía PowerShell."""
    try:
        status = subprocess.run(
            ["powershell", "-Command", "Get-MpComputerStatus | Select-Object -ExpandProperty RealTimeProtectionEnabled"],
            capture_output=True, text=True, timeout=10
        )
        if status.returncode == 0 and status.stdout and status.stdout.strip() != "True":
            return "Protección en tiempo real desactivada. Escaneo omitido."
        result = subprocess.run(
            ["powershell", "-Command", "Start-MpScan -ScanType QuickScan"],
            capture_output=True, text=True, timeout=1800, check=True
        )
        return str(result.stdout) if result.stdout else "Escaneo iniciado correctamente."
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, subprocess.TimeoutExpired) as e:
        return f"Error ejecutando Windows Defender: {str(e)}"
