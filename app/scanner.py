"""
scanner.py
Detector HEURÍSTICO de archivos sospechosos. Este módulo realiza un análisis 
estático mediante heurísticas de nombre, extensión y metadatos de archivo, 
complementando la protección de Windows Defender.

El módulo utiliza un recorrido iterativo seguro para navegar el sistema de archivos, 
aplicando validaciones estrictas antes de cada acceso para evitar la resolución de 
puntos de reanálisis (Junctions/Symlinks) y rutas protegidas.
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
from typing import List, Optional, Union, Final, Callable, TypeAlias, NamedTuple, Sequence, Tuple, Protocol, Dict
from safety import is_protected_path

# Configuración de logger para el módulo
logger: Final = logging.getLogger(__name__)

# Constantes de API de Windows para control de acceso (Win32 API)
FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400

class ScannerLimits(NamedTuple):
    """
    Parámetros de configuración para el motor de escaneo.
    
    Attributes:
        max_path: Longitud máxima permitida de ruta (bytes).
        recent_hours: Ventana temporal para considerar un archivo como 'reciente'.
        reparse_point_attr_mask: Máscara bitwise para identificar puntos de reanálisis.
        max_depth: Profundidad máxima del árbol de directorios a explorar.
    """
    max_path: int = 260
    recent_hours: int = 24
    reparse_point_attr_mask: int = FILE_ATTRIBUTE_REPARSE_POINT
    max_depth: int = 50

SCAN_LIMITS: Final = ScannerLimits()

@dataclass(frozen=True)
class Suspicion:
    """
    Representa una anomalía detectada por las heurísticas.
    
    Attributes:
        path: Ruta absoluta donde se localizó el archivo sospechoso.
        reason: Descripción técnica del criterio de detección.
        severity: Nivel de riesgo ('info', 'warning', 'critical').
    """
    path: Path
    reason: str
    severity: str

class SuspicionCheck(Protocol):
    """
    Interfaz para funciones de análisis heurístico.
    
    Las implementaciones deben ser funciones puras, inyectando 'now_ts' 
    para mantener consistencia temporal durante un escaneo completo.
    """
    def __call__(self, path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]: ...

# Alias de tipos para mejorar la legibilidad de la estructura de datos
ScanResult: TypeAlias = List[Suspicion]
# Directorio a explorar y profundidad actual alcanzada
DirectoryStack: TypeAlias = List[Tuple[str, int]]
Timestamp: TypeAlias = float

# Expresiones regulares para validación de nombres y riesgos
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d\u200b\u00a0\ufeff]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
INVALID_TRAILING_CHARS_RE: Final[re.Pattern] = re.compile(r"[\. ]$")
UNC_PATH_RE: Final[re.Pattern] = re.compile(r"^\\\\[^\\\\]+\\")

# Constantes de categorización para heurísticas
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SUSPICIOUS_CONTENT_EXT: Final[frozenset[str]] = frozenset({".pdf"})
SUSPICIOUS_ALL_EXTS: Final[frozenset[str]] = SUSPICIOUS_EXECUTABLE_EXT.union(SUSPICIOUS_CONTENT_EXT)
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
TARGETED_DOWNLOAD_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})

SYSTEM32_LOWER: Final[str] = "system32"

@lru_cache(maxsize=4096)
def _cached_is_protected(path_str: str) -> bool:
    """Valida si una ruta está protegida mediante el módulo de seguridad, con caché LRU."""
    return is_protected_path(Path(path_str))

def _is_valid_file_path(path: Path) -> bool:
    """Valida la existencia y accesibilidad de un archivo antes de procesarlo."""
    if not isinstance(path, (Path, str)):
        return False
    try:
        p = Path(path)
        if _cached_is_protected(str(p)):
            return False
        return p.is_file()
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError, AttributeError):
        return False

def _is_readable(path: Path) -> bool:
    """Verifica si el sistema operativo permite lectura en la ruta indicada."""
    if not isinstance(path, (Path, str)):
        return False
    try:
        p = Path(path)
        if _cached_is_protected(str(p)):
            return False
        return os.access(str(p), os.R_OK)
    except (OSError, PermissionError, ValueError, AttributeError, TypeError):
        return False

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Obtiene atributos de Win32 para detectar reparse points mediante stat."""
    if not isinstance(entry, os.DirEntry):
        return 0
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError, FileNotFoundError):
        return 0

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """Obtiene metadatos de archivo de forma segura, ignorando enlaces simbólicos."""
    if entry is None:
        return None
    try:
        if entry.is_symlink():
            return None
        if not entry.is_file(follow_symlinks=False):
            return None
        if _cached_is_protected(entry.path) or (_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_attr_mask):
            return None
        return entry.stat(follow_symlinks=False)
    except (OSError, PermissionError, AttributeError, FileNotFoundError):
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Verifica límites estructurales y caracteres ilegales en cadenas de rutas."""
    if not isinstance(path_str, str) or not path_str.strip():
        return False
    if len(path_str) > SCAN_LIMITS.max_path or "\0" in path_str:
        return False
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

def _is_target_extension(name: str) -> bool:
    """Comprueba si el archivo posee una extensión bajo monitoreo heurístico."""
    if not isinstance(name, str) or not name: return False
    try:
        if RTL_CHAR_RE.search(name): return False
        return Path(name).suffix.lower() in SUSPICIOUS_ALL_EXTS
    except (ValueError, TypeError):
        return False

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0) -> Optional[Suspicion]:
    """Heurística: Identifica archivos ejecutables cuya carga útil es nula (0 bytes)."""
    if entry:
        try:
            if entry.stat(follow_symlinks=False).st_size == 0:
                return Suspicion(path, "Ejecutable vacío detectado", "warning")
        except (OSError, PermissionError): pass
    return None

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0) -> Optional[Suspicion]:
    """Heurística: Detecta doble extensión para prevenir engaños al usuario sobre el tipo de archivo."""
    if not path or not path.name or _cached_is_protected(str(path)): return None
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión detectada como técnica de enmascaramiento", "warning")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0) -> Optional[Suspicion]:
    """Heurística: Detecta ejecutables con nombres de procesos del sistema fuera de 'System32'."""
    if not path or not path.name or _cached_is_protected(str(path)): return None
    if path.name.lower() in SYSTEM_LOOKALIKES:
        try:
            if SYSTEM32_LOWER not in str(path).lower():
                return Suspicion(path, "Nombre de proceso de sistema detectado fuera de directorio protegido", "warning")
        except (TypeError, AttributeError):
            return None
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0) -> Optional[Suspicion]:
    """Heurística: Señala ejecutables creados recientemente en carpetas de alta volatilidad."""
    if not path or not path.parent or _cached_is_protected(str(path)): return None
    if path.parent.name.lower() not in TARGETED_DOWNLOAD_FOLDERS:
        return None
    try:
        if entry:
            stats = _safe_stat(entry)
            if stats:
                mtime = getattr(stats, "st_mtime", 0.0)
                if isinstance(mtime, (int, float)) and mtime > 0 and (now_ts - float(mtime)) < (SCAN_LIMITS.recent_hours * 3600):
                    return Suspicion(path, "Ejecutable reciente detectado en carpeta volátil", "info")
    except (OSError, PermissionError, AttributeError, TypeError):
        return None
    return None

ALL_CHECKS: Final[Sequence[SuspicionCheck]] = (
    check_empty_file,
    check_double_extension,
    check_system_lookalike,
    check_recent_executable_in_downloads
)

class Scanner:
    """Controlador central del motor de escaneo con gestión de estado para el recorrido del disco."""
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.safe_cache: set[str] = set()
        self.base_root_norm: str = os.path.normcase(str(base_root.absolute()))
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Valida mediante normalización de rutas si el archivo pertenece al árbol de escaneo."""
        return os.path.normcase(os.path.abspath(entry_path)).startswith(self.base_root_norm)

    def _has_invalid_name(self, name: str) -> bool:
        """Valida que el nombre de archivo no contenga caracteres o patrones prohibidos en Windows."""
        if not isinstance(name, str) or not name: return True
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name) or RTL_CHAR_RE.search(name) or "\0" in name)

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si una entrada es un punto de reanálisis basándose en atributos Win32."""
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_attr_mask)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """Aplica filtros de seguridad y validación de estructura para decidir si procesar una entrada."""
        if entry is None or not entry.path:
            return False
        
        path_str: str = entry.path
        if path_str in self.safe_cache:
            return True
        
        try:
            if self._has_invalid_name(entry.name): return False
            if entry.is_symlink(): return False
            if self._is_reparse_point(entry): return False
            if _cached_is_protected(path_str): return False
            if not _is_valid_path_structure(path_str): return False
            if not self._is_inside_base_root(path_str): return False
                
            self.safe_cache.add(path_str)
            return True
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            return False

    def _handle_directory(self, entry: os.DirEntry, directory_stack: DirectoryStack, current_depth: int) -> None:
        """Gestiona la pila de directorios para búsqueda en profundidad evitando ciclos."""
        if current_depth >= SCAN_LIMITS.max_depth or entry is None or not entry.path:
            return
        
        path_norm: str = os.path.normcase(entry.path)
        if path_norm not in self.seen:
            self.seen.add(path_norm)
            directory_stack.append((entry.path, current_depth + 1))

    @staticmethod
    @lru_cache(maxsize=1024)
    def _is_relevant_extension(name: str) -> bool:
        """Verifica si la extensión del archivo es candidata a ser analizada heurísticamente."""
        return _is_target_extension(name)

    def process_entry(self, entry: os.DirEntry, directory_stack: DirectoryStack, current_depth: int) -> None:
        """Procesa una entrada del sistema de archivos bifurcando entre directorio y archivo."""
        if entry is None or not entry.path or not self._is_safe_entry(entry):
            return
            
        path_norm: str = os.path.normcase(entry.path)
        if path_norm in self.seen:
            return
        
        try:
            if self._is_reparse_point(entry):
                return
            if entry.is_file(follow_symlinks=False):
                if not self._is_relevant_extension(entry.name):
                    return
                self.seen.add(path_norm)
                self._run_file_heuristics(Path(entry.path), entry)
            elif entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, directory_stack, current_depth)
        except (OSError, PermissionError, FileNotFoundError):
            logger.debug(f"Acceso denegado a {entry.path}")

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta iterativamente todos los chequeos de seguridad registrados sobre un archivo."""
        for check_fn in ALL_CHECKS:
            try:
                finding = check_fn(path, entry, self.now_ts)
                if finding:
                    self.results.append(finding)
            except (OSError, ValueError, AttributeError) as e:
                logger.debug(f"Heurística {check_fn.__name__} falló en {path}: {e}")

def scan_file(path: Path, now_ts: Timestamp, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Punto de entrada aislado para analizar archivos individualmente."""
    if not isinstance(path, Path) or not _is_valid_path_structure(str(path)):
        return []
    try:
        if not path.exists() or _cached_is_protected(str(path)) or not _is_readable(path):
            return []
        
        findings: ScanResult = []
        for check_fn in ALL_CHECKS:
            try:
                res = check_fn(path, entry, now_ts)
                if res:
                    findings.append(res)
            except (OSError, ValueError, AttributeError) as e:
                logger.debug(f"Heurística puntual {check_fn.__name__} fallida en {path}: {e}")
        return findings
    except (OSError, PermissionError):
        return []

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Escaneo recursivo de directorios utilizando una pila interna y control de duplicados."""
    if directory is None: return []
    try:
        base_path = Path(directory)
        if not base_path.exists() or not base_path.is_dir():
            return []
        base_path = base_path.resolve(strict=True)
        if base_path.is_symlink() or not os.access(base_path, os.R_OK) or _cached_is_protected(str(base_path)):
            return []
        scanner: Scanner = Scanner(base_root=base_path)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return []
    
    directory_stack: DirectoryStack = [(str(base_path), 0)]
    scanner.seen.add(os.path.normcase(str(base_path)))
    while directory_stack:
        current_dir, depth = directory_stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if entry: scanner.process_entry(entry, directory_stack, depth)
        except (PermissionError, OSError):
            logger.debug(f"Error de acceso en directorio {current_dir}")
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """Invoca un análisis rápido de Windows Defender mediante comandos de PowerShell."""
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
