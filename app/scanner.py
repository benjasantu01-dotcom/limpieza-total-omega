"""
scanner.py
Detector HEURÍSTICO de archivos sospechosos. Este módulo realiza un análisis 
estático mediante heurísticas de nombre, extensión y metadatos de archivo, 
complementando la protección de Windows Defender.

El módulo utiliza un recorrido iterativo seguro para navegar el sistema de archivos, 
aplicando validaciones estrictas antes de cada acceso para evitar la resolución de 
puntos de reanálisis (Junction/Symlink) y rutas protegidas.
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
from typing import List, Optional, Union, Final, Protocol, Sequence, Tuple, TYPE_CHECKING, TypeAlias, TypeGuard

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
    Las implementaciones deben ser funciones puras o aisladas que no modifiquen el estado
    del sistema de archivos y retornen un objeto Suspicion si se detecta riesgo.
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

def _is_valid_stat(stats: Optional[os.stat_result]) -> TypeGuard[os.stat_result]:
    """Verifica que el objeto os.stat_result contenga atributos necesarios para el análisis."""
    return stats is not None and hasattr(stats, 'st_size')

@lru_cache(maxsize=4096)
def _cached_is_protected(path_str: str) -> bool:
    """Wrapper cacheado para is_protected_path, minimizando llamadas repetitivas al filesystem."""
    try:
        if not path_str or "\0" in path_str: return True
        return is_protected_path(Path(path_str))
    except (ValueError, TypeError, OSError):
        return True

def _get_canonical_path(path_str: str) -> str:
    """
    Resuelve rutas relativas y simbólicas a su forma absoluta y canonizada.
    Retorna cadena vacía si la ruta excede límites o contiene caracteres prohibidos.
    """
    if not isinstance(path_str, str) or not path_str or "\0" in path_str:
        return ""
    try:
        norm = os.path.realpath(path_str)
        if len(norm) > SCAN_LIMITS.path_length_limit: return ""
        return norm
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return ""

def _is_safe_by_attributes(path: Path) -> bool:
    """Determina si un archivo debe ser ignorado por ser un punto de reanálisis (Junction/Symlink)."""
    try:
        attrs = path.stat().st_file_attributes
        return not (attrs & SCAN_LIMITS.reparse_point_mask)
    except (OSError, AttributeError):
        return True

def _is_valid_file_path(path: Union[Path, str, None]) -> bool:
    """Verifica si la ruta es un archivo candidato válido para escaneo heurístico (no protegido, no sistema)."""
    if path is None: return False
    abs_path = _get_canonical_path(str(path))
    if not abs_path or _cached_is_protected(abs_path): return False
    
    ext = os.path.splitext(abs_path)[1].lower()
    if ext not in SUSPICIOUS_ALL_EXTS: return False
    
    try:
        target_path = Path(abs_path)
        if not target_path.name or RESERVED_NAMES_RE.match(target_path.name): return False
        return target_path.exists() and target_path.is_file() and _is_safe_by_attributes(target_path)
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError):
        return False

def _is_readable(path: Union[Path, str, None]) -> bool:
    """Verifica si el proceso tiene permisos efectivos de lectura sobre la ruta indicada."""
    if path is None: return False
    abs_path = _get_canonical_path(str(path))
    if not abs_path or _cached_is_protected(abs_path):
        return False
    try:
        return os.access(abs_path, os.R_OK)
    except (OSError, PermissionError, ValueError, TypeError):
        return False

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Extrae atributos Win32 nativos sin seguir enlaces simbólicos."""
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError, FileNotFoundError, TypeError):
        return 0

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """
    Obtiene metadatos de archivo saltando puntos de reanálisis y manejando errores de acceso comunes.
    """
    if _get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask:
        return None
    try:
        return entry.stat(follow_symlinks=False)
    except (OSError, PermissionError, FileNotFoundError) as e:
        win_error = getattr(e, 'winerror', 0)
        if win_error not in (ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND):
            logger.debug(f"Error accediendo a metadatos en {entry.path}: {e}")
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida la integridad de la ruta contra ataques de path traversal y ofuscación de nombres."""
    if path_str is None or "\0" in path_str: return False
    abs_path_str = _get_canonical_path(path_str)
    if not abs_path_str or _cached_is_protected(abs_path_str):
        return False
    try:
        name = os.path.basename(abs_path_str)
        if not name or RESERVED_NAMES_RE.match(name): return False
        if UNC_PATH_RE.match(abs_path_str) or RTL_CHAR_RE.search(name): return False
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False

# --- Suite de Heurísticas ---

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Heurística: Detecta archivos con tamaño 0, a menudo usados como marcadores de infección."""
    if _is_valid_stat(stats) and stats.st_size == 0:
        return Suspicion(path, "Archivo vacío detectado", "warning")
    return None

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Heurística: Identifica archivos con doble extensión (ej: .jpg.exe) usada para suplantación de identidad de archivos."""
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión detectada", "warning")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Heurística: Identifica ejecutables con nombres de procesos del sistema fuera de su ubicación legítima en System32."""
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        if SYSTEM32_LOWER not in str(path).lower():
            return Suspicion(path, "Nombre de proceso del sistema fuera de ubicación legítima", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Heurística: Detecta ejecutables recientemente modificados dentro de directorios volátiles (Descargas, Temp)."""
    if _is_valid_stat(stats):
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
    """Motor coordinador del escaneo: gestiona el estado de la búsqueda y la ejecución de heurísticas."""
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.visited_paths: VisitedNodes = set()
        self.attr_cache: dict[str, os.stat_result] = {}
        self.root_canonical: str = _get_canonical_path(str(base_root))
        self.now_ts: float = datetime.now().timestamp()
        self._relevant_exts: Final[frozenset[str]] = SUSPICIOUS_ALL_EXTS

    def _is_inside_base_root(self, entry_path: str) -> bool:
        return entry_path.startswith(self.root_canonical)

    def _has_invalid_name(self, name: str) -> bool:
        return bool(not name or INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name) or RTL_CHAR_RE.search(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask)

    def _is_safe_entry(self, entry: os.DirEntry, norm_path: str) -> bool:
        """Valida que una entrada del sistema de archivos sea segura para procesar."""
        if not norm_path or "\0" in norm_path or len(norm_path) > SCAN_LIMITS.path_length_limit: return False
        name = os.path.basename(norm_path)
        if not name or self._has_invalid_name(name) or self._is_reparse_point(entry): return False
        if not self._is_inside_base_root(norm_path) or _cached_is_protected(norm_path): return False
        return True

    def _handle_directory(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: VisitedNodes, norm_path: str) -> None:
        """Gestiona el descenso recursivo en directorios controlando la profundidad máxima."""
        if depth >= SCAN_LIMITS.max_recursion_depth: return
        if norm_path not in visited:
            visited.add(norm_path)
            stack.append((entry.path, depth + 1))

    def _is_relevant_extension(self, name: str) -> bool:
        return os.path.splitext(name)[1].lower() in self._relevant_exts

    def process_entry(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: VisitedNodes) -> None:
        """Procesa una entrada individual (archivo o directorio) aplicando validaciones de seguridad."""
        try:
            if "\0" in entry.path: return
            
            # Pre-validar extensión antes de resolución costosa de rutas si es archivo
            if not entry.is_dir() and not self._is_relevant_extension(entry.name):
                return
                
            norm_path = _get_canonical_path(entry.path)
            # Validación global de seguridad contra puntos de reanálisis
            if not norm_path or self._is_reparse_point(entry) or not self._is_safe_entry(entry, norm_path):
                return
            
            if entry.is_dir():
                self._handle_directory(entry, stack, depth, visited, norm_path)
            else:
                self._run_file_heuristics(Path(entry.path), entry)
        except (PermissionError, OSError):
            return

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta la suite de heurísticas sobre el archivo detectado, aplicando caching local."""
        if not path: return
        
        path_str = str(path)
        stats = self.attr_cache.get(path_str) or _safe_stat(entry)
        if not stats: return
        
        if len(self.attr_cache) < SCAN_LIMITS.cache_max_size:
            self.attr_cache[path_str] = stats
            
        for check in ALL_CHECKS:
            try:
                finding = check(path, entry, self.now_ts, stats)
                if isinstance(finding, Suspicion):
                    self.results.append(finding)
            except Exception as e:
                logger.debug(f"Heurística {check.__name__} falló en {path}: {e}")

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Escanea un archivo único mediante la suite completa de reglas heurísticas."""
    if not path or not _is_valid_path_structure(str(path)) or not _is_readable(path): return []
    try:
        stats = entry.stat(follow_symlinks=False) if entry else path.stat()
    except (OSError, PermissionError, FileNotFoundError, TypeError, ValueError): return []
    
    results: ScanResult = []
    for check in ALL_CHECKS:
        try:
            res = check(path, entry, now_ts, stats)
            if isinstance(res, Suspicion): results.append(res)
        except Exception:
            continue
    return results

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Realiza un escaneo recursivo mediante un recorrido iterativo usando una pila de exploración."""
    if directory is None or "\0" in str(directory): return []
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
                        try:
                            scanner.process_entry(entry, stack, depth, visited)
                        except (PermissionError, OSError):
                            continue
            except (PermissionError, OSError):
                continue
        return scanner.results
    except (OSError, RuntimeError, ValueError): return []

def run_windows_defender_quick_scan() -> str:
    """Interfaz con Windows Defender para ejecutar un escaneo rápido mediante comandos de PowerShell."""
    try:
        status = subprocess.run(
            ["powershell", "-Command", "Get-MpComputerStatus | Select-Object -ExpandProperty RealTimeProtectionEnabled"],
            capture_output=True, text=True, timeout=10
        )
        if status.returncode != 0 or not status.stdout or status.stdout.strip() != "True":
            return "Protección en tiempo real desactivada o estado desconocido. Escaneo omitido."
            
        result = subprocess.run(
            ["powershell", "-Command", "Start-MpScan -ScanType QuickScan"],
            capture_output=True, text=True, timeout=1800
        )
        if result.returncode != 0:
            return f"Error ejecutando escaneo: {result.stderr.strip() if result.stderr else 'Código de error desconocido'}"
        return str(result.stdout).strip() if result.stdout else "Escaneo finalizado."
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return f"Error de sistema al ejecutar Defender: {str(e)}"
