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
from typing import List, Optional, Union, Final, Protocol, Sequence, Tuple, TYPE_CHECKING, TypeAlias

# Importación local según reglas de proyecto (imports planos)
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

# Alias de tipos para mejorar la legibilidad y el análisis estático
ScanResult: TypeAlias = List["Suspicion"]
TraversalStack: TypeAlias = List[Tuple[str, int]]
VisitedNodes: TypeAlias = set[str]

@dataclass(frozen=True)
class ScannerLimits:
    """Configuración inmutable de los límites operativos del motor de escaneo."""
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
    Protocolo funcional para heurísticas de seguridad.
    Las implementaciones deben ser funciones puras que evalúan un archivo
    sin modificar el disco, retornando una instancia de Suspicion si se detecta riesgo.
    """
    def __call__(
        self, 
        path: Path, 
        entry: Optional[os.DirEntry] = None, 
        now_ts: float = 0.0, 
        stats: Optional[os.stat_result] = None
    ) -> Optional[Suspicion]: ...

# Expresiones regulares para detección de patrones de ofuscación
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d\u202b\u200b\u00a0\ufeff]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
INVALID_TRAILING_CHARS_RE: Final[re.Pattern] = re.compile(r"[<>:\"|?*]")
UNC_PATH_RE: Final[re.Pattern] = re.compile(r"^\\\\[^\\\\]+\\")

# Conjuntos inmutables para filtrado rápido de heurísticas
SUSPICIOUS_ALL_EXTS: Final[frozenset[str]] = frozenset((".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1", ".pdf"))
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
TARGETED_DOWNLOAD_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})
SYSTEM32_LOWER: Final[str] = "system32"

@lru_cache(maxsize=4096)
def _cached_is_protected(path_str: str) -> bool:
    """
    Verifica si una ruta está protegida mediante el módulo de seguridad.
    El uso de caché es crítico dado que esta función se invoca en cada nodo del árbol.
    """
    try:
        if not path_str or "\0" in path_str: return True
        return is_protected_path(Path(path_str))
    except (ValueError, TypeError, OSError):
        return True

def _validate_heuristic_input(path: Path, stats: Optional[os.stat_result]) -> bool:
    """Valida la presencia de metadatos mínimos requeridos para análisis heurístico."""
    return path is not None and stats is not None and hasattr(stats, 'st_size')

def _get_canonical_path(path_str: str) -> str:
    """
    Normaliza rutas para evitar ofuscación por path traversal.
    Aplica límites de longitud estándar de Windows (MAX_PATH) para estabilidad.
    """
    if not isinstance(path_str, str) or not path_str or "\0" in path_str:
        return ""
    try:
        clean_path = os.path.abspath(os.path.normpath(path_str.strip()))
        norm = os.path.normcase(clean_path)
        if len(norm) > SCAN_LIMITS.path_length_limit:
            return ""
        return norm
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return ""

def _is_safe_by_attributes(path: Path) -> bool:
    """Determina si el archivo es un punto de reanálisis (Junction/Symlink) para evitar bucles."""
    try:
        attrs = path.stat().st_file_attributes
        return not (attrs & SCAN_LIMITS.reparse_point_mask)
    except (OSError, AttributeError):
        return True

def _is_valid_file_path(path: Union[Path, str, None]) -> bool:
    """Valida existencia, seguridad de ruta y tipo de archivo para escaneo heurístico."""
    if path is None: return False
    if isinstance(path, Path) and path.suffix.lower() not in SUSPICIOUS_ALL_EXTS: return False
    abs_path = _get_canonical_path(str(path))
    if not abs_path: return False
    target_path = Path(abs_path)
    try:
        if not target_path.name or RESERVED_NAMES_RE.match(target_path.name): return False
        return target_path.exists() and not _cached_is_protected(abs_path) and target_path.is_file() and _is_safe_by_attributes(target_path)
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError):
        return False

def _is_readable(path: Union[Path, str, None]) -> bool:
    """Verifica permisos de lectura efectivos antes de acceder al contenido."""
    if path is None: return False
    abs_path = _get_canonical_path(str(path))
    if not abs_path or _cached_is_protected(abs_path):
        return False
    try:
        return os.access(abs_path, os.R_OK)
    except (OSError, PermissionError, ValueError, TypeError):
        return False

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Extrae atributos de bajo nivel del sistema de archivos Win32."""
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError, FileNotFoundError):
        return 0

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """
    Obtiene metadatos asegurando el salto de puntos de reanálisis.
    Filtra errores comunes de acceso denegado para evitar ruido en logs.
    """
    try:
        if _get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask:
            return None
        stats = entry.stat(follow_symlinks=False)
        if stats is not None and getattr(stats, 'st_size', -1) >= 0:
            return stats
        return None
    except (OSError, TypeError, ValueError) as e:
        win_error = getattr(e, 'winerror', 0)
        if win_error not in (ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND):
            logger.debug(f"Error accediendo a metadatos en {entry.path}: {e}")
        return None
    except (AttributeError):
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Validación de estructura de ruta contra ofuscación de nombres o rutas UNC."""
    if path_str is None or "\0" in path_str: return False
    abs_path_str = _get_canonical_path(path_str)
    if not abs_path_str or _cached_is_protected(abs_path_str):
        return False
    try:
        p = Path(abs_path_str)
        if not p.name or RESERVED_NAMES_RE.match(p.name): return False
        if UNC_PATH_RE.match(abs_path_str) or RTL_CHAR_RE.search(p.name): return False
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False

# --- Suite de Heurísticas ---

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Identifica archivos de tamaño cero como posibles marcadores de relleno (filler)."""
    if _validate_heuristic_input(path, stats) and stats.st_size == 0:
        return Suspicion(path, "Archivo vacío detectado", "warning")
    return None

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Detecta técnicas de suplantación de tipo mediante el uso de doble extensión."""
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión detectada", "warning")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Identifica ejecutables que suplantan nombres de procesos críticos del SO fuera de System32."""
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        if SYSTEM32_LOWER not in str(path).lower():
            return Suspicion(path, "Nombre de proceso del sistema fuera de ubicación legítima", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Identifica ejecutables de reciente creación en directorios volátiles (Descargas, Temp)."""
    if _validate_heuristic_input(path, stats):
        if path.parent.name.lower() in TARGETED_DOWNLOAD_FOLDERS:
            mtime = getattr(stats, 'st_mtime', 0.0)
            if isinstance(mtime, (int, float)) and mtime > 0:
                age_seconds = now_ts - float(mtime)
                if age_seconds < (SCAN_LIMITS.recent_file_window_hours * 3600):
                    return Suspicion(path, "Ejecutable reciente en carpeta volátil", "info")
    return None

ALL_CHECKS: Final[Sequence[SuspicionCheck]] = (
    check_empty_file,
    check_double_extension,
    check_system_lookalike,
    check_recent_executable_in_downloads
)

class Scanner:
    """
    Motor coordinador del escaneo. Gestiona el estado del recorrido, la caché 
    de atributos y la ejecución de la suite heurística.
    """
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.visited_paths: VisitedNodes = set()
        self.attr_cache: dict[str, os.stat_result] = {}
        self.root_canonical: str = _get_canonical_path(str(base_root))
        self.now_ts: float = datetime.now().timestamp()
        self._relevant_exts: Final[frozenset[str]] = SUSPICIOUS_ALL_EXTS

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica la restricción de alcance para no escapar del directorio base."""
        try:
            return os.path.commonpath([self.root_canonical, entry_path]) == self.root_canonical
        except ValueError:
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Valida nombres de archivo contra caracteres reservados o peligrosos."""
        return bool(not name or INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name) or RTL_CHAR_RE.search(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si una entrada es un punto de reanálisis según atributos de sistema."""
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask)

    def _is_safe_entry(self, entry: os.DirEntry, norm_path: str) -> bool:
        """Validación de seguridad para decidir si procesar un nodo del sistema de archivos."""
        if not norm_path or "\0" in norm_path or len(norm_path) > SCAN_LIMITS.path_length_limit: return False
        p = Path(norm_path)
        if not p.name or self._has_invalid_name(p.name) or self._is_reparse_point(entry): return False
        if not self._is_inside_base_root(norm_path) or _cached_is_protected(norm_path): return False
        return True

    def _handle_directory(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: VisitedNodes, norm_path: str) -> None:
        """Gestiona el control de profundidad de la pila para prevenir stack overflow."""
        if depth >= SCAN_LIMITS.max_recursion_depth: return
        if norm_path not in visited:
            visited.add(norm_path)
            stack.append((entry.path, depth + 1))

    def _is_relevant_extension(self, name: str) -> bool:
        """Filtra archivos de interés según las heurísticas configuradas."""
        _, ext = os.path.splitext(name)
        return ext.lower() in self._relevant_exts

    def process_entry(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: VisitedNodes) -> None:
        """
        Clasifica una entrada del sistema de archivos: recursión en directorios 
        o ejecución de heurísticas en archivos sospechosos.
        """
        try:
            norm_path = _get_canonical_path(entry.path)
            if not norm_path or not self._is_safe_entry(entry, norm_path): return
            
            if entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, stack, depth, visited, norm_path)
            elif self._is_relevant_extension(entry.name):
                self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError, FileNotFoundError):
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """
        Ejecuta la suite de heurísticas sobre el archivo, utilizando una caché 
        de metadatos para optimizar la performance durante el escaneo.
        """
        path_str = str(path)
        stats = self.attr_cache.get(path_str) or _safe_stat(entry)
        if not stats: return
        
        if len(self.attr_cache) < SCAN_LIMITS.cache_max_size:
            self.attr_cache[path_str] = stats
            
        findings = (check(path, entry, self.now_ts, stats) for check in ALL_CHECKS)
        for finding in findings:
            if finding:
                self.results.append(finding)

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Escanea un archivo puntual contra la suite de heurísticas."""
    if not path or not _is_valid_path_structure(str(path)) or not _is_readable(path): return []
    try:
        stats = entry.stat(follow_symlinks=False) if entry else path.stat()
    except (OSError, PermissionError, FileNotFoundError, TypeError, ValueError): return []
    
    return [res for res in (check(path, entry, now_ts, stats) for check in ALL_CHECKS) if res]

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """
    Escaneo recursivo del árbol de directorios utilizando una estructura de pila 
    para una navegación segura, evitando el desbordamiento de recursión de Python.
    """
    if directory is None: return []
    abs_path_str = _get_canonical_path(str(directory))
    if not abs_path_str or _cached_is_protected(abs_path_str): return []
    try:
        if not os.path.isdir(abs_path_str): return []
        scanner: Scanner = Scanner(base_root=Path(abs_path_str))
        stack: TraversalStack = [(abs_path_str, 0)]
        visited: VisitedNodes = {abs_path_str}
        while stack:
            current_dir, depth = stack.pop()
            if _cached_is_protected(current_dir): continue
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        scanner.process_entry(entry, stack, depth, visited)
            except (PermissionError, OSError, FileNotFoundError):
                continue
        return scanner.results
    except (OSError, RuntimeError, ValueError): return []

def run_windows_defender_quick_scan() -> str:
    """
    Interfaz con Windows Defender para escaneos nativos mediante PowerShell.
    Solo ejecuta la acción tras confirmar que la protección en tiempo real está activa.
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
