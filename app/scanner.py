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
    """
    Define los umbrales de seguridad y rendimiento para el motor de escaneo.
    
    Attributes:
        path_length_limit: Longitud máxima permitida para rutas (Windows MAX_PATH).
        recent_file_window_hours: Ventana temporal para heurísticas de archivos recientes.
        reparse_point_mask: Máscara binaria para detectar puntos de reanálisis.
        max_recursion_depth: Límite de profundidad para prevenir desbordamiento de pila.
    """
    path_length_limit: int = 260
    recent_file_window_hours: int = 24
    reparse_point_mask: int = FILE_ATTRIBUTE_REPARSE_POINT
    max_recursion_depth: int = 50

SCAN_LIMITS: Final = ScannerLimits()

@dataclass(frozen=True)
class Suspicion:
    """
    Representa una anomalía encontrada durante el análisis estático.
    
    Attributes:
        path: Objeto Path del archivo sospechoso.
        reason: Descripción clara de la heurística que disparó la alerta.
        severity: Nivel de riesgo ('warning' o 'info').
    """
    path: Path
    reason: str
    severity: str

class SuspicionCheck(Protocol):
    """
    Contrato de interfaz para funciones de inspección heurística.
    
    Las funciones inyectadas bajo este protocolo deben ser capaces de evaluar 
    un archivo dado y retornar una instancia de Suspicion si se cumple la condición.
    """
    def __call__(
        self, 
        path: Path, 
        entry: Optional[os.DirEntry] = None, 
        now_ts: float = 0.0, 
        stats: Optional[os.stat_result] = None
    ) -> Optional[Suspicion]: ...

# Alias de tipos para mejorar la legibilidad de las firmas de funciones
ScanResult: TypeAlias = List[Suspicion]
TraversalStack: TypeAlias = List[Tuple[str, int]]
Timestamp: TypeAlias = float
PathInput: TypeAlias = Union[Path, str, None]
VisitedNodes: TypeAlias = set[str]

# Expresiones regulares para detección de técnicas de evasión y validación de nombres
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d\u202b\u200b\u00a0\ufeff]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
INVALID_TRAILING_CHARS_RE: Final[re.Pattern] = re.compile(r"[<>:\"|?*]")
UNC_PATH_RE: Final[re.Pattern] = re.compile(r"^\\\\[^\\\\]+\\")

# Listas de configuración para filtros
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SUSPICIOUS_CONTENT_EXT: Final[frozenset[str]] = frozenset({".pdf"})
SUSPICIOUS_ALL_EXTS: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1", ".pdf"})
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
    """Valida los requisitos mínimos para ejecutar una heurística."""
    return path is not None and stats is not None

def _get_canonical_path(path_str: str) -> str:
    """Normaliza una ruta a formato absoluto y canónico."""
    if not path_str or not isinstance(path_str, str) or "\0" in path_str: return ""
    try:
        clean_path = os.path.normpath(path_str.strip())
        if len(clean_path) > 32767: return ""
        norm = os.path.normcase(os.path.abspath(clean_path))
        return norm if len(norm) <= SCAN_LIMITS.path_length_limit else ""
    except (OSError, ValueError, TypeError, AttributeError):
        return ""

def _is_safe_by_attributes(path: Path) -> bool:
    """Valida si un archivo es un punto de reanálisis (Junction/Symlink)."""
    try:
        attrs = path.stat().st_file_attributes
        return not (attrs & SCAN_LIMITS.reparse_point_mask)
    except (OSError, AttributeError):
        return True

def _is_valid_file_path(path: PathInput) -> bool:
    """Realiza validaciones previas al acceso de un archivo (existencia y seguridad)."""
    if not isinstance(path, (Path, str)):
        return False
    try:
        path_str = str(path)
        abs_path = _get_canonical_path(path_str)
        if not abs_path: return False
        target_path = Path(abs_path)
        if not target_path.name or RESERVED_NAMES_RE.match(target_path.name): return False
        return target_path.exists() and not _cached_is_protected(abs_path) and target_path.is_file() and _is_safe_by_attributes(target_path)
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError):
        return False

def _is_readable(path: PathInput) -> bool:
    """Verifica permisos de lectura a nivel de sistema operativo."""
    if not isinstance(path, (Path, str)):
        return False
    try:
        path_str = str(path)
        if not path_str or "\0" in path_str or len(path_str) > SCAN_LIMITS.path_length_limit:
            return False
        if _cached_is_protected(path_str):
            return False
        return os.access(path_str, os.R_OK)
    except (OSError, PermissionError, ValueError, TypeError):
        return False

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Obtiene atributos de bajo nivel del sistema de archivos sin seguir enlaces."""
    if not isinstance(entry, os.DirEntry) or not entry.path:
        return 0
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError, FileNotFoundError):
        return 0

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """Obtiene metadatos del archivo asegurando que no sea un punto de reanálisis."""
    if not isinstance(entry, os.DirEntry) or not entry.path or "\0" in entry.path:
        return None
        
    if _get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask:
        return None
        
    try:
        stats = entry.stat(follow_symlinks=False)
        st_size = getattr(stats, 'st_size', -1)
        st_mtime = getattr(stats, 'st_mtime', -1.0)
        
        if st_size < 0 or st_mtime < 0:
            return None
        return stats
    except (OSError, PermissionError) as e:
        if getattr(e, 'winerror', None) not in (ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND):
            logger.debug(f"Error accediendo a metadatos de {entry.path}: {e}")
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida la integridad de una ruta contra caracteres prohibidos o rutas UNC."""
    if not isinstance(path_str, str) or not path_str.strip() or "\0" in path_str:
        return False
    try:
        abs_path = _get_canonical_path(path_str)
        if not abs_path or _cached_is_protected(abs_path):
            return False
        base_name = os.path.basename(abs_path)
        if not base_name or RESERVED_NAMES_RE.match(base_name):
            return False
        if UNC_PATH_RE.match(abs_path) or RTL_CHAR_RE.search(base_name):
            return False
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False

# --- Suite de Heurísticas ---

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Detecta archivos vacíos, a menudo usados para ofuscar o marcar sistemas."""
    if _validate_heuristic_input(path, stats) and stats.st_size == 0:
        return Suspicion(path, "Archivo vacío detectado (posible marcador de relleno)", "warning")
    return None

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Detecta ofuscación de ejecutables con falsas extensiones de documento."""
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión detectada como técnica de ofuscación", "warning")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Detecta ejecutables con nombres de sistema encontrados fuera de System32."""
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        try:
            if SYSTEM32_LOWER not in str(path).lower():
                return Suspicion(path, "Nombre de proceso del sistema fuera de ubicación legítima", "warning")
        except (TypeError, ValueError, AttributeError):
            return None
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Detecta nuevos ejecutables en carpetas volátiles (Downloads/Temp)."""
    if _validate_heuristic_input(path, stats):
        try:
            parent = path.parent
            if parent.name.lower() in TARGETED_DOWNLOAD_FOLDERS:
                if stats.st_mtime > 0 and (now_ts - float(stats.st_mtime)) < (SCAN_LIMITS.recent_file_window_hours * 3600):
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
    """
    Motor de escaneo que gestiona el contexto de un árbol de directorios completo.
    """
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen_normcase: set[str] = set()
        self.safe_cache: set[str] = set()
        self.attr_cache: dict[str, os.stat_result] = {}
        self.base_root_real: str = _get_canonical_path(str(base_root))
        self.now_ts: float = datetime.now().timestamp()
        self._relevant_exts: set[str] = set(SUSPICIOUS_ALL_EXTS)
        self._base_sep: str = self.base_root_real + os.sep

    def _is_inside_base_root(self, entry_path: str) -> bool:
        norm_entry = _get_canonical_path(entry_path)
        return bool(norm_entry and (norm_entry == self.base_root_real or norm_entry.startswith(self._base_sep)))

    def _has_invalid_name(self, name: str) -> bool:
        if not isinstance(name, str) or not name or "\0" in name: return True
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name) or RTL_CHAR_RE.search(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        try:
            stats = entry.stat(follow_symlinks=False)
            tag = getattr(stats, "st_reparse_tag", 0)
            if tag in (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK):
                return True
            return bool(getattr(stats, "st_file_attributes", 0) & SCAN_LIMITS.reparse_point_mask)
        except (AttributeError, OSError):
            return True

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        try:
            path_str = entry.path
            if not path_str or not isinstance(path_str, str) or "\0" in path_str: return False
            if path_str in self.safe_cache: return True
            if self._has_invalid_name(entry.name): return False
            if self._is_reparse_point(entry): return False
            
            norm_path = _get_canonical_path(path_str)
            if not norm_path or _cached_is_protected(norm_path): return False
            
            if not self._is_inside_base_root(path_str): return False
            if not os.access(path_str, os.R_OK): return False
            self.safe_cache.add(path_str)
            return True
        except (OSError, AttributeError, PermissionError):
            return False

    def _handle_directory(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: VisitedNodes) -> None:
        if depth >= SCAN_LIMITS.max_recursion_depth: return
        norm_path = _get_canonical_path(entry.path)
        if norm_path and norm_path not in visited:
            visited.add(norm_path)
            stack.append((entry.path, depth + 1))

    def _is_relevant_extension(self, name: str) -> bool:
        _, ext = os.path.splitext(name)
        return ext.lower() in self._relevant_exts

    def process_entry(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: VisitedNodes) -> None:
        if not self._is_safe_entry(entry): return
        try:
            if entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, stack, depth, visited)
            elif entry.is_file(follow_symlinks=False):
                if entry.name and self._is_relevant_extension(entry.name):
                    path_norm = _get_canonical_path(entry.path)
                    if path_norm and path_norm not in self.seen_normcase:
                        self.seen_normcase.add(path_norm)
                        self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError):
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        path_str = str(path)
        stats = self.attr_cache.get(path_str) or _safe_stat(entry)
        if stats:
            self.attr_cache[path_str] = stats
            for check_fn in ALL_CHECKS:
                try:
                    finding = check_fn(path, entry, self.now_ts, stats)
                    if finding: self.results.append(finding)
                except Exception as e:
                    logger.debug(f"Heurística {check_fn.__name__} omitida en {path}: {e}")

def scan_file(path: Path, now_ts: Timestamp, entry: Optional[os.DirEntry] = None) -> ScanResult:
    if not isinstance(path, Path) or not _is_valid_path_structure(str(path)) or not _is_readable(path):
        return []
    try:
        stats = entry.stat(follow_symlinks=False) if entry else path.stat()
    except (OSError, PermissionError):
        return []
    findings: ScanResult = []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts, stats)
            if res: findings.append(res)
        except Exception as e:
            logger.debug(f"Heurística {check_fn.__name__} falló en {path}: {e}")
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    if directory is None: return []
    try:
        abs_path_str = _get_canonical_path(str(directory))
        if not abs_path_str: return []
        base_path = Path(abs_path_str)
        if not base_path.exists() or not base_path.is_dir() or not _is_readable(abs_path_str):
            return []
        scanner: Scanner = Scanner(base_root=base_path)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return []
    stack: TraversalStack = [(str(base_path), 0)]
    visited: VisitedNodes = {abs_path_str}
    while stack:
        current_dir, depth = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        if entry.name and "\0" not in entry.name:
                            scanner.process_entry(entry, stack, depth, visited)
                    except (PermissionError, OSError): continue
        except (PermissionError, OSError): continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
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
