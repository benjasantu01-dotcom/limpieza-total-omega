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
    """Límites operativos definidos para prevenir riesgos de recursión, 
    desbordamiento de buffer y carga excesiva de I/O."""
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
    Protocolo funcional para definir contratos de inspección heurística.
    Cada función debe ser inyectable en la iteración principal.
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
    """Verifica si una ruta está marcada como protegida usando caché LRU para evitar llamadas repetitivas."""
    try:
        return is_protected_path(Path(path_str))
    except (ValueError, TypeError, OSError):
        return True

def _get_canonical_path(path_str: str) -> str:
    """
    Normaliza y resuelve la ruta a formato absoluto, validando límites de longitud de Windows.
    
    Args:
        path_str: La ruta sin normalizar.
        
    Returns:
        La ruta absoluta normalizada o un string vacío si es inválida/demasiado larga.
    """
    if not path_str or not isinstance(path_str, str) or "\0" in path_str: return ""
    try:
        clean_path = os.path.normpath(path_str.strip())
        if len(clean_path) > 32767: return ""
        norm = os.path.normcase(os.path.abspath(clean_path))
        return norm if len(norm) <= SCAN_LIMITS.path_length_limit else ""
    except (OSError, ValueError, TypeError, AttributeError):
        return ""

def _is_safe_by_attributes(path: Path) -> bool:
    """Verifica si el archivo es un punto de reanálisis (reparse point) mediante atributos de archivo."""
    try:
        attrs = path.stat().st_file_attributes
        return not (attrs & SCAN_LIMITS.reparse_point_mask)
    except (OSError, AttributeError):
        return True

def _is_valid_file_path(path: PathInput) -> bool:
    """Valida la existencia física del archivo y que esté dentro de una ruta permitida (no protegida)."""
    if not isinstance(path, (Path, str)):
        return False
    try:
        path_str = str(path)
        abs_path = _get_canonical_path(path_str)
        if not abs_path: return False
        target_path = Path(abs_path)
        if RESERVED_NAMES_RE.match(target_path.name): return False
        return target_path.exists() and not _cached_is_protected(abs_path) and target_path.is_file() and _is_safe_by_attributes(target_path)
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError):
        return False

def _is_readable(path: PathInput) -> bool:
    """Verifica permisos de acceso de lectura a nivel de SO utilizando el proceso actual."""
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
    """Extrae atributos del sistema de archivos sin seguir enlaces simbólicos."""
    if not isinstance(entry, os.DirEntry) or not entry.path:
        return 0
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError, FileNotFoundError):
        return 0

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """
    Obtiene metadatos del archivo verificando la coherencia y previniendo colisiones de acceso.
    
    Returns:
        Un objeto stat_result si es válido, None en caso de error de acceso o archivo bloqueado.
    """
    if not isinstance(entry, os.DirEntry) or not entry.path or "\0" in entry.path:
        return None
        
    if _get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask:
        return None
        
    try:
        stats = entry.stat(follow_symlinks=False)
        # Validación estricta de metadatos básicos para prevenir datos corruptos o fuera de rango
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
    """Valida la integridad de una ruta de archivo frente a caracteres prohibidos y rutas UNC."""
    if not isinstance(path_str, str) or not path_str.strip() or "\0" in path_str:
        return False
    try:
        abs_path = _get_canonical_path(path_str)
        if not abs_path or _cached_is_protected(abs_path):
            return False
        if UNC_PATH_RE.match(abs_path) or RTL_CHAR_RE.search(os.path.basename(abs_path)):
            return False
        if RESERVED_NAMES_RE.match(os.path.basename(abs_path)):
            return False
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False

# --- Suite de Heurísticas ---

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Marca archivos con tamaño 0 bytes, utilizados frecuentemente como marcadores de infección."""
    if stats is not None and getattr(stats, "st_size", -1) == 0:
        return Suspicion(path, "Archivo vacío detectado (posible marcador de relleno)", "warning")
    return None

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Identifica la técnica de ofuscación donde se usan extensiones legítimas previas a la real."""
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión detectada como técnica de ofuscación", "warning")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Detecta ejecutables que intentan suplantar nombres de procesos críticos fuera de sus carpetas nativas."""
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        try:
            p_str = str(path)
            if SYSTEM32_LOWER not in p_str.lower():
                return Suspicion(path, "Nombre de proceso del sistema fuera de ubicación legítima", "warning")
        except (TypeError, ValueError, AttributeError):
            return None
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Evalúa la temporalidad de ejecutables encontrados en directorios de descarga habituales."""
    if path is None or stats is None: 
        return None
    try:
        parent_name = getattr(path, "parent", None)
        if parent_name and parent_name.name.lower() in TARGETED_DOWNLOAD_FOLDERS:
            mtime = getattr(stats, "st_mtime", 0.0)
            if isinstance(mtime, (int, float)) and mtime > 0 and (now_ts - float(mtime)) < (SCAN_LIMITS.recent_file_window_hours * 3600):
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
    Motor central del escaneo. Gestiona el estado persistente durante el recorrido 
    del árbol (visto, caché de seguridad, resultados) para optimizar el rendimiento 
    y evitar redundancias en el análisis.
    """
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen_normcase: set[str] = set()
        self.safe_cache: set[str] = set()
        self.base_root_real: str = _get_canonical_path(str(base_root))
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica mediante rutas canónicas que la entrada no escape al árbol base solicitado."""
        try:
            norm_entry = _get_canonical_path(entry_path)
            if not norm_entry or not self.base_root_real:
                return False
            return os.path.commonpath([self.base_root_real, norm_entry]) == self.base_root_real
        except (ValueError, OSError):
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Valida que el nombre de archivo sea conforme a las restricciones del sistema Windows."""
        if not isinstance(name, str) or not name or "\0" in name: return True
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name) or RTL_CHAR_RE.search(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Identifica puntos de reanálisis que deben ignorarse para evitar ciclos o incursiones no deseadas."""
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """
        Filtro de seguridad de primer nivel para directorios y archivos.
        Valida que la entrada sea accesible, no esté protegida y respete la estructura.
        Utiliza `safe_cache` para minimizar llamadas repetidas a validadores costosos.
        """
        try:
            path_str = getattr(entry, 'path', None)
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

    def _handle_directory(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: set[str]) -> None:
        """Registra directorios válidos en el stack para el recorrido posterior evitando bucles infinitos."""
        if depth >= SCAN_LIMITS.max_recursion_depth: return
        norm_path = _get_canonical_path(entry.path)
        if not norm_path or norm_path in visited: return
        visited.add(norm_path)
        stack.append((entry.path, depth + 1))

    def _is_relevant_extension(self, name: str) -> bool:
        """Determina si el archivo debe ser sometido a heurísticas basándose en su extensión."""
        _, ext = os.path.splitext(name)
        return ext.lower() in SUSPICIOUS_ALL_EXTS

    def process_entry(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: set[str]) -> None:
        """
        Clasifica la entrada: si es directorio, lo encola para inspección recursiva; 
        si es archivo relevante, dispara el motor de heurísticas.
        """
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
        """Ejecuta en secuencia todas las funciones heurísticas registradas en `ALL_CHECKS`."""
        stats: Optional[os.stat_result] = _safe_stat(entry)
        if stats is None: return
        for check_fn in ALL_CHECKS:
            try:
                finding: Optional[Suspicion] = check_fn(path, entry, self.now_ts, stats)
                if finding: self.results.append(finding)
            except Exception as e:
                logger.debug(f"Heurística {check_fn.__name__} omitida por error en {path}: {e}")

def scan_file(path: Path, now_ts: Timestamp, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Escanea un único archivo de forma aislada."""
    if not isinstance(path, Path) or not _is_valid_path_structure(str(path)) or not _is_readable(path):
        return []
    try:
        stats: os.stat_result = entry.stat(follow_symlinks=False) if entry else path.stat()
    except (OSError, PermissionError):
        return []
    findings: ScanResult = []
    for check_fn in ALL_CHECKS:
        try:
            res: Optional[Suspicion] = check_fn(path, entry, now_ts, stats)
            if res: findings.append(res)
        except Exception as e:
            logger.debug(f"Heurística {check_fn.__name__} falló en {path}: {e}")
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Punto de entrada para el escaneo de directorios completos mediante un stack de recorrido."""
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
    visited: set[str] = {abs_path_str}
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
    """Interfaz para ejecutar una exploración rápida mediante las herramientas nativas de Windows (Defender)."""
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
