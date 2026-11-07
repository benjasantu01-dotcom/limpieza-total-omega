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

class ScannerLimits(NamedTuple):
    """Límites operativos del motor para prevenir riesgos de recursión y desbordamiento."""
    path_length_limit: int = 260
    recent_file_window_hours: int = 24
    reparse_point_mask: int = FILE_ATTRIBUTE_REPARSE_POINT
    max_recursion_depth: int = 50

SCAN_LIMITS: Final = ScannerLimits()

@dataclass(frozen=True)
class Suspicion:
    """Representa una anomalía encontrada durante el análisis estático."""
    path: Path
    reason: str
    severity: str

class SuspicionCheck(Protocol):
    """
    Define el contrato para funciones de inspección heurística.
    Las implementaciones deben ser puras respecto al estado del escáner y devolver 
    un objeto Suspicion si se detecta una amenaza, o None en caso contrario.
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

# Expresiones regulares para detección de técnicas de evasión
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d\u202b\u200b\u00a0\ufeff]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
INVALID_TRAILING_CHARS_RE: Final[re.Pattern] = re.compile(r"[\. ]$")
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
    """Verifica si una ruta está marcada como protegida usando caché para rendimiento."""
    try:
        return is_protected_path(Path(path_str))
    except (ValueError, TypeError, OSError):
        return True

def _get_canonical_path(path_str: str) -> str:
    """Normaliza y resuelve la ruta de forma segura para evitar traversal."""
    try:
        return os.path.normcase(os.path.realpath(path_str))
    except (OSError, ValueError):
        return ""

def _is_safe_by_attributes(path: Path) -> bool:
    """
    Valida mediante atributos de archivo que la ruta no sea un reparse point.
    """
    try:
        attrs = path.stat().st_file_attributes
        return not (attrs & SCAN_LIMITS.reparse_point_mask)
    except (OSError, AttributeError):
        return True

def _is_valid_file_path(path: PathInput) -> bool:
    """
    Valida si un path es un archivo real, no protegido y físicamente accesible.
    """
    if path is None:
        return False
    try:
        path_str = str(path)
        if not path_str or "\0" in path_str or len(path_str) > SCAN_LIMITS.path_length_limit:
            return False
        abs_path = _get_canonical_path(path_str)
        if not abs_path: return False
        target_path = Path(abs_path)
        return target_path.exists() and not _cached_is_protected(abs_path) and target_path.is_file() and _is_safe_by_attributes(target_path)
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError):
        return False

def _is_readable(path: PathInput) -> bool:
    """Verifica permisos de lectura a nivel de sistema operativo para el proceso actual."""
    if path is None:
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
    """
    Obtiene atributos de Win32 sin resolver el enlace.
    """
    if not isinstance(entry, os.DirEntry) or not entry.path:
        return 0
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError, FileNotFoundError):
        return 0

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """
    Obtiene metadatos asegurando que el archivo no esté en uso.
    """
    if not isinstance(entry, os.DirEntry) or not entry.path or "\0" in entry.path:
        return None
        
    if _cached_is_protected(entry.path) or (_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask):
        return None
        
    try:
        stats = entry.stat(follow_symlinks=False)
        if stats.st_size < 0:
            return None
        return stats
    except OSError as e:
        if getattr(e, 'winerror', None) not in (ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND):
            logger.debug(f"Error accediendo a metadatos de {entry.path}: {e}")
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """
    Valida la integridad del string del path.
    """
    if not isinstance(path_str, str) or not path_str.strip() or "\0" in path_str:
        return False
    try:
        abs_path = _get_canonical_path(path_str)
        if not abs_path or _cached_is_protected(abs_path) or len(abs_path) > SCAN_LIMITS.path_length_limit:
            return False
        if UNC_PATH_RE.match(abs_path) or RTL_CHAR_RE.search(abs_path):
            return False
        return True
    except (OSError, ValueError, TypeError):
        return False

# --- Suite de Heurísticas ---

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    if stats is not None and getattr(stats, "st_size", -1) == 0:
        return Suspicion(path, "Archivo vacío detectado (posible marcador de relleno)", "warning")
    return None

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión detectada como técnica de ofuscación", "warning")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        if SYSTEM32_LOWER not in str(path).lower():
            return Suspicion(path, "Nombre de proceso del sistema fuera de ubicación legítima", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    if path is None or path.parent is None or stats is None: 
        return None
    try:
        if path.parent.name.lower() in TARGETED_DOWNLOAD_FOLDERS:
            mtime = getattr(stats, "st_mtime", None)
            if isinstance(mtime, (int, float)) and mtime > 0:
                if (now_ts - float(mtime)) < (SCAN_LIMITS.recent_file_window_hours * 3600):
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
    Motor principal de escaneo encargado de la traversal del sistema de archivos.
    Utiliza una pila para el recorrido recursivo controlado y mantiene caches 
    locales para optimizar la performance durante el análisis masivo.
    """
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen_normcase: set[str] = set()
        self.safe_cache: set[str] = set()
        self.base_root_real: str = _get_canonical_path(str(base_root))
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica que la ruta resuelta esté contenida bajo la jerarquía base."""
        try:
            norm_entry = _get_canonical_path(entry_path)
            if not norm_entry or not self.base_root_real:
                return False
            return os.path.commonpath([self.base_root_real, norm_entry]) == self.base_root_real
        except (ValueError, OSError):
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Valida que el nombre de archivo no contenga caracteres o patrones prohibidos."""
        if not isinstance(name, str) or not name or "\0" in name: return True
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name) or RTL_CHAR_RE.search(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si la entrada es un punto de reanálisis (Junction/Symlink)."""
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """Valida si una entrada es apta para ser procesada durante el escaneo."""
        try:
            path_str = getattr(entry, 'path', None)
            if not path_str or not isinstance(path_str, str) or "\0" in path_str: return False
            if path_str in self.safe_cache: return True
            if self._has_invalid_name(entry.name): return False
            if self._is_reparse_point(entry): return False
            if _cached_is_protected(path_str): return False
            if not self._is_inside_base_root(path_str): return False
            if not os.access(path_str, os.R_OK): return False
            
            self.safe_cache.add(path_str)
            return True
        except (OSError, AttributeError):
            return False

    def _handle_directory(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: set[str]) -> None:
        """Gestiona la inserción de subdirectorios en la pila de recorrido con control de profundidad."""
        if depth >= SCAN_LIMITS.max_recursion_depth: return
        norm_path = _get_canonical_path(entry.path)
        if not norm_path or norm_path in visited: return
        
        visited.add(norm_path)
        stack.append((entry.path, depth + 1))

    @lru_cache(maxsize=128)
    def _is_relevant_extension(self, name: str) -> bool:
        """Filtra archivos de interés según extensiones configuradas."""
        _, ext = os.path.splitext(name)
        return ext.lower() in SUSPICIOUS_ALL_EXTS

    def process_entry(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: set[str]) -> None:
        """Procesa una entrada del directorio, decidiendo si explorar o analizar."""
        if not self._is_safe_entry(entry):
            return
        try:
            if entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, stack, depth, visited)
            elif entry.is_file(follow_symlinks=False):
                if self._is_relevant_extension(entry.name):
                    path_norm = _get_canonical_path(entry.path)
                    if path_norm and path_norm not in self.seen_normcase:
                        self.seen_normcase.add(path_norm)
                        self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError):
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta la suite de heurísticas sobre un archivo identificado."""
        stats = _safe_stat(entry)
        if stats is None:
            return
        for check_fn in ALL_CHECKS:
            try:
                finding = check_fn(path, entry, self.now_ts, stats)
                if finding:
                    self.results.append(finding)
            except Exception as e:
                logger.debug(f"Heurística {check_fn.__name__} omitida por error: {e}")

def scan_file(path: Path, now_ts: Timestamp, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Escanea un único archivo específico aplicando la suite de heurísticas."""
    if not isinstance(path, Path) or not _is_valid_path_structure(str(path)) or not _is_readable(path):
        return []
    try:
        if not path.is_file(): return []
        stats = entry.stat(follow_symlinks=False) if entry else path.stat()
    except (OSError, PermissionError):
        return []
    findings: ScanResult = []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts, stats)
            if res:
                findings.append(res)
        except Exception as e:
            logger.debug(f"Heurística {check_fn.__name__} falló: {e}")
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """
    Inicia un recorrido de escaneo recursivo desde una ruta raíz.
    Retorna la lista agregada de todos los hallazgos encontrados.
    """
    if directory is None: return []
    try:
        base_path = Path(directory).resolve()
        if not base_path.exists() or not base_path.is_dir() or not _is_readable(str(base_path)):
            return []
        scanner: Scanner = Scanner(base_root=base_path)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return []
    
    stack: TraversalStack = [(str(base_path), 0)]
    visited: set[str] = {_get_canonical_path(str(base_path))}
    
    while stack:
        current_dir, depth = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        scanner.process_entry(entry, stack, depth, visited)
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """
    Interfase con el motor de Windows Defender para ejecutar un escaneo rápido.
    Solo invoca el comando si la protección en tiempo real está activa.
    """
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
