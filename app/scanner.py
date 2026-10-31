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
from typing import List, Optional, Union, Final, Protocol, NamedTuple, Sequence, Tuple, TypeAlias
from safety import is_protected_path

# Configuración de logger para el seguimiento de excepciones en el escaneo
logger: Final = logging.getLogger(__name__)

# Constantes de Win32 API para la identificación precisa de estructuras de archivos
FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400
ERROR_SHARING_VIOLATION: Final = 32

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
    Protocolo de interfaz para definir una nueva heurística de seguridad.
    
    Toda implementación debe ser pura, procesando únicamente los datos de entrada
    para determinar riesgos, garantizando la seguridad en el escaneo.
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
DirectoryStack: TypeAlias = List[Tuple[str, int]]
Timestamp: TypeAlias = float
PathInput: TypeAlias = Union[Path, str, None]

# Expresiones regulares para detección de técnicas de evasión
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d\u200b\u00a0\ufeff]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
INVALID_TRAILING_CHARS_RE: Final[re.Pattern] = re.compile(r"[\. ]$")
UNC_PATH_RE: Final[re.Pattern] = re.compile(r"^\\\\[^\\\\]+\\")

# Listas de configuración para filtros
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SUSPICIOUS_CONTENT_EXT: Final[frozenset[str]] = frozenset({".pdf"})
SUSPICIOUS_ALL_EXTS: Final[frozenset[str]] = SUSPICIOUS_EXECUTABLE_EXT.union(SUSPICIOUS_CONTENT_EXT)
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
TARGETED_DOWNLOAD_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})
SYSTEM32_LOWER: Final[str] = "system32"

@lru_cache(maxsize=4096)
def _cached_is_protected(path_str: str) -> bool:
    """Verifica si una ruta está protegida usando caché para optimizar el rendimiento."""
    try:
        return is_protected_path(Path(path_str))
    except (ValueError, TypeError, OSError):
        return True

def _is_valid_file_path(path: PathInput) -> bool:
    """Valida existencia, legibilidad y exclusión de rutas protegidas."""
    if not isinstance(path, (Path, str)) or not path:
        return False
    try:
        target_path = Path(path)
        return target_path.exists() and not _cached_is_protected(str(target_path)) and target_path.is_file()
    except (OSError, PermissionError, ValueError, TypeError):
        return False

def _is_readable(path: PathInput) -> bool:
    """Verifica si el sistema permite la lectura de la ruta dada."""
    if not isinstance(path, (Path, str)) or not path:
        return False
    try:
        target_str = str(path)
        return not _cached_is_protected(target_str) and os.access(target_str, os.R_OK)
    except (OSError, PermissionError, ValueError, TypeError):
        return False

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Obtiene los atributos de archivo de nivel sistema sin seguir enlaces simbólicos."""
    if not isinstance(entry, os.DirEntry) or _cached_is_protected(entry.path):
        return 0
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError, FileNotFoundError):
        return 0

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """Obtiene metadatos asegurando la exclusión de puntos de reanálisis y manejo de bloqueos."""
    if not isinstance(entry, os.DirEntry) or _cached_is_protected(entry.path):
        return None
    try:
        if entry.is_symlink():
            return None
        attrs = _get_file_attributes(entry)
        if attrs & SCAN_LIMITS.reparse_point_mask:
            return None
        if not entry.is_file():
            return None
        return entry.stat(follow_symlinks=False)
    except OSError as e:
        if getattr(e, 'winerror', None) == ERROR_SHARING_VIOLATION:
            logger.debug(f"Archivo bloqueado (sharing violation): {entry.path}")
            return None
        return None
    except (PermissionError, FileNotFoundError):
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida que la cadena de ruta cumpla con los límites de longitud y formato."""
    if not isinstance(path_str, str) or not path_str.strip() or _cached_is_protected(path_str):
        return False
    if len(path_str) > SCAN_LIMITS.path_length_limit or "\0" in path_str:
        return False
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

# --- Suite de Heurísticas ---

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Identifica archivos de 0 bytes que pueden ser marcadores de relleno."""
    if stats and getattr(stats, "st_size", -1) == 0:
        return Suspicion(path, "Archivo vacío detectado (posible marcador de relleno)", "warning")
    return None

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Detecta ofuscación de tipo de archivo mediante extensiones múltiples."""
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión detectada como técnica de ofuscación", "warning")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Verifica si procesos de sistema residen en ubicaciones inusuales."""
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        if SYSTEM32_LOWER not in str(path).lower():
            return Suspicion(path, "Nombre de proceso del sistema fuera de ubicación legítima", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Identifica ejecutables de reciente creación en carpetas de descarga volátiles."""
    if not path or not path.parent or not stats: return None
    try:
        if path.parent.name.lower() in TARGETED_DOWNLOAD_FOLDERS:
            mtime = getattr(stats, "st_mtime", 0.0)
            if isinstance(mtime, (int, float)) and mtime > 0:
                if (now_ts - float(mtime)) < (SCAN_LIMITS.recent_file_window_hours * 3600):
                    return Suspicion(path, "Ejecutable reciente en carpeta volátil", "info")
    except (AttributeError, ValueError, TypeError):
        pass
    return None

ALL_CHECKS: Final[Sequence[SuspicionCheck]] = (
    check_empty_file,
    check_double_extension,
    check_system_lookalike,
    check_recent_executable_in_downloads
)

class Scanner:
    """Controlador encargado de la orquestación del análisis recursivo del sistema."""
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen_normcase: set[str] = set()
        self.safe_cache: set[str] = set()
        self.base_root_norm: str = os.path.normcase(str(base_root))
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, entry_path: Optional[str]) -> bool:
        """Verifica que la entrada permanezca dentro del alcance del directorio base."""
        if not entry_path: return False
        return os.path.normcase(entry_path).startswith(self.base_root_norm)

    def _has_invalid_name(self, name: str) -> bool:
        """Determina si un nombre contiene caracteres o nombres reservados del SO."""
        if not isinstance(name, str) or not name: return True
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name) or RTL_CHAR_RE.search(name) or "\0" in name)

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Detecta si la entrada es un punto de reanálisis para evitar ciclos de recursión."""
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """Valida que la entrada cumpla con todos los criterios de seguridad definidos."""
        path_str: str = entry.path
        if path_str in self.safe_cache:
            return True
        if self._has_invalid_name(entry.name): return False
        if entry.is_symlink() or self._is_reparse_point(entry): return False
        if _cached_is_protected(path_str): return False
        if not _is_valid_path_structure(path_str): return False
        if not self._is_inside_base_root(path_str): return False
        self.safe_cache.add(path_str)
        return True

    def _handle_directory(self, entry: os.DirEntry, directory_stack: DirectoryStack, current_depth: int, visited_dirs: set[str]) -> None:
        """Gestiona la inserción de directorios en el stack para el recorrido iterativo."""
        if current_depth >= SCAN_LIMITS.max_recursion_depth:
            return
        if _cached_is_protected(entry.path):
            return
        if not self._is_inside_base_root(entry.path):
            return
        norm_path = os.path.normcase(entry.path)
        try:
            if norm_path not in visited_dirs and os.access(entry.path, os.R_OK):
                visited_dirs.add(norm_path)
                directory_stack.append((entry.path, current_depth + 1))
        except (OSError, PermissionError):
            pass

    @lru_cache(maxsize=128)
    def _is_relevant_extension(self, name: str) -> bool:
        """Filtra archivos de interés según extensiones definidas como sospechosas."""
        _, ext = os.path.splitext(name)
        return ext.lower() in SUSPICIOUS_ALL_EXTS

    def process_entry(self, entry: os.DirEntry, directory_stack: DirectoryStack, current_depth: int, visited_dirs: set[str]) -> None:
        """Procesa una entrada del sistema de archivos, delegando según su tipo."""
        if not self._is_safe_entry(entry):
            return
        try:
            if entry.is_file():
                if self._is_relevant_extension(entry.name):
                    path_norm = os.path.normcase(entry.path)
                    if path_norm not in self.seen_normcase:
                        self.seen_normcase.add(path_norm)
                        self._run_file_heuristics(Path(entry.path), entry)
            elif entry.is_dir():
                self._handle_directory(entry, directory_stack, current_depth, visited_dirs)
        except (OSError, PermissionError):
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta la suite de heurísticas sobre el archivo indicado."""
        try:
            stats = _safe_stat(entry)
            if stats is None:
                return
            for check_fn in ALL_CHECKS:
                try:
                    finding = check_fn(path, entry, self.now_ts, stats)
                    if finding:
                        self.results.append(finding)
                except Exception as e:
                    logger.debug(f"Heurística {check_fn.__name__} falló en {path}: {type(e).__name__}")
        except Exception as e:
            logger.debug(f"Error fatal procesando metadatos de {path}: {type(e).__name__}")

def scan_file(path: Path, now_ts: Timestamp, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Escaneo heurístico puntual para archivos aislados fuera del flujo recursivo."""
    if not isinstance(path, Path) or not _is_valid_path_structure(str(path)) or _cached_is_protected(str(path)) or not _is_readable(path):
        return []
    findings: ScanResult = []
    try:
        if not path.is_file(): return []
        stats = entry.stat() if entry else path.stat()
    except (OSError, PermissionError):
        return []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts, stats)
            if res:
                findings.append(res)
        except Exception as e:
            logger.debug(f"Heurística puntual {check_fn.__name__} fallida en {path}: {type(e).__name__}")
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Escaneo recursivo de un directorio, devolviendo todos los hallazgos encontrados."""
    if directory is None: return []
    try:
        base_path = Path(directory).resolve()
        if not base_path.exists() or not base_path.is_dir() or not os.access(base_path, os.R_OK):
            return []
        if _cached_is_protected(str(base_path)):
            return []
        scanner: Scanner = Scanner(base_root=base_path)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return []
    directory_stack: DirectoryStack = [(str(base_path), 0)]
    visited_dirs: set[str] = {os.path.normcase(str(base_path))}
    while directory_stack:
        current_dir, depth = directory_stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    scanner.process_entry(entry, directory_stack, depth, visited_dirs)
        except (PermissionError, OSError):
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """Invoca la API de PowerShell para ejecutar un escaneo rápido del sistema."""
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
