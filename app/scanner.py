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
    """Protocolo para definir heurísticas de seguridad inyectables."""
    def __call__(
        self, 
        path: Path, 
        entry: Optional[os.DirEntry] = None, 
        now_ts: float = 0.0, 
        stats: Optional[os.stat_result] = None
    ) -> Optional[Suspicion]: ...

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
    """
    Verifica si una ruta está protegida.
    Usa caché LRU para evitar E/S redundantes. Retorna True si la ruta es inválida.
    """
    try:
        if not path_str or "\0" in path_str: return True
        return is_protected_path(Path(path_str))
    except (ValueError, TypeError, OSError):
        return True

def _validate_heuristic_input(path: Path, stats: Optional[os.stat_result]) -> bool:
    """Verifica integridad de los metadatos necesarios para heurísticas."""
    return path is not None and stats is not None and hasattr(stats, 'st_size')

def _get_canonical_path(path_str: str) -> str:
    """
    Normaliza rutas para evitar ofuscación y chequea límites de longitud.
    Retorna cadena vacía si la ruta es inválida o excede el límite permitido.
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
    """Determina si el archivo es un punto de reanálisis ignorando los mismos."""
    try:
        attrs = path.stat().st_file_attributes
        return not (attrs & SCAN_LIMITS.reparse_point_mask)
    except (OSError, AttributeError):
        return True

def _is_valid_file_path(path: Union[Path, str, None]) -> bool:
    """Valida existencia, protección y tipo de archivo para escaneo seguro."""
    if path is None: return False
    # Filtro rápido de extensión antes de canonizar
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
    """Comprueba permisos de lectura en nivel de sistema de archivos."""
    if path is None: return False
    abs_path = _get_canonical_path(str(path))
    if not abs_path or _cached_is_protected(abs_path):
        return False
    try:
        return os.access(abs_path, os.R_OK)
    except (OSError, PermissionError, ValueError, TypeError):
        return False

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Extrae atributos de bajo nivel; retorna 0 si no se puede acceder."""
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError, FileNotFoundError):
        return 0

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """
    Obtiene metadatos asegurando el salto de reanálisis.
    Retorna None si el archivo está inaccesible o es un punto de reanálisis.
    """
    try:
        if _get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask:
            return None
        stats = entry.stat(follow_symlinks=False)
        if stats is not None and getattr(stats, 'st_size', -1) >= 0:
            return stats
        return None
    except OSError as e:
        # Se captura explícitamente el uso compartido denegado para evitar logs ruidosos
        win_error = getattr(e, 'winerror', 0)
        if win_error not in (ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND):
            logger.debug(f"Error accediendo a metadatos en {entry.path}: {e}")
        return None
    except (AttributeError, TypeError):
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida la estructura de ruta (no UNC, no caracteres prohibidos)."""
    if path_str is None or "\0" in path_str: return False
    abs_path = _get_canonical_path(path_str)
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

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Identifica archivos vacíos (posible marcador de relleno)."""
    if _validate_heuristic_input(path, stats) and stats.st_size == 0:
        return Suspicion(path, "Archivo vacío detectado", "warning")
    return None

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Detecta técnicas de ofuscación mediante doble extensión."""
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión detectada", "warning")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Detecta ejecutables que suplantan procesos críticos fuera de System32."""
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        if SYSTEM32_LOWER not in str(path).lower():
            return Suspicion(path, "Nombre de proceso del sistema fuera de ubicación legítima", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Identifica ejecutables recientes en carpetas volátiles."""
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
    """Motor encargado de coordinar el recorrido recursivo del disco y aplicar heurísticas."""
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.visited_paths: VisitedNodes = set()
        self.attr_cache: dict[str, os.stat_result] = {}
        self.root_canonical: str = _get_canonical_path(str(base_root))
        self.now_ts: float = datetime.now().timestamp()
        self._relevant_exts: Final[frozenset[str]] = SUSPICIOUS_ALL_EXTS

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica si la ruta analizada permanece dentro del árbol de directorios raíz."""
        return entry_path.startswith(self.root_canonical)

    def _has_invalid_name(self, name: str) -> bool:
        """Verifica si el nombre de archivo contiene caracteres inválidos o reservados."""
        return bool(not name or INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name) or RTL_CHAR_RE.search(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si la entrada es un punto de reanálisis (Junction/Symlink)."""
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask)

    def _is_safe_entry(self, entry: os.DirEntry, norm_path: str) -> bool:
        """Realiza comprobaciones de seguridad antes de procesar archivos o directorios."""
        if not norm_path or "\0" in norm_path or len(norm_path) > SCAN_LIMITS.path_length_limit: return False
        if not entry.name or self._has_invalid_name(entry.name) or self._is_reparse_point(entry): return False
        if not self._is_inside_base_root(norm_path) or _cached_is_protected(norm_path): return False
        return True

    def _handle_directory(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: VisitedNodes, norm_path: str) -> None:
        """Gestiona la profundidad para evitar recursión infinita."""
        if depth >= SCAN_LIMITS.max_recursion_depth: return
        if norm_path not in visited:
            visited.add(norm_path)
            stack.append((entry.path, depth + 1))

    def _is_relevant_extension(self, name: str) -> bool:
        """Filtra archivos por extensión."""
        return name.lower().endswith(tuple(self._relevant_exts))

    def process_entry(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: VisitedNodes) -> None:
        """
        Clasifica una entrada de sistema de archivos: si es directorio lo encola para 
        recursión (respetando límites); si es archivo relevante, dispara las heurísticas.
        """
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            if not is_dir and not self._is_relevant_extension(entry.name):
                return
            
            norm_path = _get_canonical_path(entry.path)
            if not norm_path or not self._is_safe_entry(entry, norm_path): return
            
            if is_dir:
                self._handle_directory(entry, stack, depth, visited, norm_path)
            else:
                self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError, FileNotFoundError):
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta la suite de heurísticas sobre el archivo, manejando violaciones de uso."""
        path_str = str(path)
        stats = self.attr_cache.get(path_str) or _safe_stat(entry)
        if not stats: return
        
        if len(self.attr_cache) < SCAN_LIMITS.cache_max_size:
            self.attr_cache[path_str] = stats
            
        # Generador de resultados para evitar listas intermedias pesadas
        findings = (check(path, entry, self.now_ts, stats) for check in ALL_CHECKS)
        for finding in findings:
            if finding:
                self.results.append(finding)

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Escanea un único archivo contra la suite heurística."""
    if not path or not _is_valid_path_structure(str(path)) or not _is_readable(path): return []
    try:
        stats = entry.stat(follow_symlinks=False) if entry else path.stat()
    except (OSError, PermissionError, FileNotFoundError): return []
    
    return [res for res in (check(path, entry, now_ts, stats) for check in ALL_CHECKS) if res]

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """
    Escanea recursivamente el árbol de directorios utilizando una pila (stack) para
    evitar el desbordamiento de recursión de Python, aplicando chequeos de seguridad 
    en cada nodo visitado.
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
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        scanner.process_entry(entry, stack, depth, visited)
            except (PermissionError, OSError, FileNotFoundError):
                continue
        return scanner.results
    except Exception: return []

def run_windows_defender_quick_scan() -> str:
    """Inicia un escaneo rápido nativo de Windows Defender vía PowerShell."""
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
