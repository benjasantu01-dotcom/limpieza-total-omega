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
IO_REPARSE_TAG_SYMLINK: Final = 0xA000000C
IO_REPARSE_TAG_MOUNT_POINT: Final = 0xA0000003
ERROR_SHARING_VIOLATION: Final = 32

class ScannerLimits(NamedTuple):
    """
    Límites operativos del motor de escaneo para prevenir riesgos de recursión y desbordamiento.
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
    """
    path: Path
    reason: str
    severity: str

class SuspicionCheck(Protocol):
    """
    Protocolo para definir las heurísticas de detección.
    """
    def __call__(self, path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]: ...

ScanResult: TypeAlias = List[Suspicion]
DirectoryStack: TypeAlias = List[Tuple[str, int]]
Timestamp: TypeAlias = float

# Expresiones regulares para detección de técnicas de evasión y riesgos de nombres
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d\u200b\u00a0\ufeff]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
INVALID_TRAILING_CHARS_RE: Final[re.Pattern] = re.compile(r"[\. ]$")
UNC_PATH_RE: Final[re.Pattern] = re.compile(r"^\\\\[^\\\\]+\\")

# Listas blancas y negras para filtrar candidatos de escaneo
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SUSPICIOUS_CONTENT_EXT: Final[frozenset[str]] = frozenset({".pdf"})
SUSPICIOUS_ALL_EXTS: Final[frozenset[str]] = SUSPICIOUS_EXECUTABLE_EXT.union(SUSPICIOUS_CONTENT_EXT)
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
TARGETED_DOWNLOAD_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})

SYSTEM32_LOWER: Final[str] = "system32"

@lru_cache(maxsize=4096)
def _cached_is_protected(path_str: str) -> bool:
    """Verifica si una ruta está protegida, usando caché LRU para optimizar el rendimiento."""
    try:
        return is_protected_path(Path(path_str))
    except (ValueError, TypeError, OSError):
        return True

def _is_valid_file_path(path: Union[Path, str, None]) -> bool:
    """Valida la existencia del archivo y su no pertenencia a zonas protegidas."""
    if not isinstance(path, (Path, str)) or not path:
        return False
    try:
        target_path = Path(path)
        return not _cached_is_protected(str(target_path)) and target_path.is_file()
    except (OSError, PermissionError, ValueError, TypeError):
        return False

def _is_readable(path: Union[Path, str, None]) -> bool:
    """Verifica si la ruta es accesible y tiene permisos de lectura."""
    if not isinstance(path, (Path, str)) or not path:
        return False
    try:
        target_str = str(path)
        return not _cached_is_protected(target_str) and os.access(target_str, os.R_OK)
    except (OSError, PermissionError, ValueError, TypeError):
        return False

def _get_file_attributes(entry: os.DirEntry) -> int:
    """
    Obtiene los atributos de archivo mediante la llamada stat de bajo nivel.
    """
    if not isinstance(entry, os.DirEntry):
        return 0
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError, FileNotFoundError):
        return 0

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """
    Obtiene metadatos de archivo de forma segura, ignorando enlaces simbólicos 
    y puntos de reanálisis para prevenir saltos fuera del directorio base.
    """
    if not isinstance(entry, os.DirEntry):
        return None
    try:
        if entry.is_symlink():
            return None
        attrs = _get_file_attributes(entry)
        if attrs & SCAN_LIMITS.reparse_point_mask:
            return None
        if not entry.is_file() or _cached_is_protected(entry.path):
            return None
        return entry.stat(follow_symlinks=False)
    except (OSError, PermissionError, FileNotFoundError):
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida la integridad de la cadena contra límites de longitud y caracteres prohibidos."""
    if not isinstance(path_str, str) or not path_str.strip():
        return False
    if len(path_str) > SCAN_LIMITS.path_length_limit or "\0" in path_str:
        return False
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0) -> Optional[Suspicion]:
    """Detecta ejecutables con tamaño 0 bytes (posible indicio de ofuscación)."""
    if entry is None or path is None: return None
    stats = _safe_stat(entry)
    if stats is not None and getattr(stats, "st_size", -1) == 0:
        return Suspicion(path, "Ejecutable vacío detectado", "warning")
    return None

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0) -> Optional[Suspicion]:
    """Detecta doble extensión para enmascarar tipos de archivo ejecutables."""
    if path is None or not path.name: return None
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión detectada como técnica de enmascaramiento", "warning")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0) -> Optional[Suspicion]:
    """Alerta sobre procesos de sistema ubicados fuera de System32."""
    if path is None or not path.name: return None
    try:
        if path.name.lower() in SYSTEM_LOOKALIKES:
            if SYSTEM32_LOWER not in str(path).lower():
                return Suspicion(path, "Nombre de proceso de sistema detectado fuera de directorio protegido", "warning")
    except (AttributeError, ValueError):
        return None
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0) -> Optional[Suspicion]:
    """Identifica ejecutables recientes en carpetas temporales o de descargas."""
    if path is None or entry is None: return None
    try:
        parent_name = path.parent.name.lower()
        if parent_name not in TARGETED_DOWNLOAD_FOLDERS:
            return None
    except (AttributeError, ValueError):
        return None
        
    stats = _safe_stat(entry)
    if stats:
        mtime = getattr(stats, "st_mtime", 0.0)
        if isinstance(mtime, (int, float)) and mtime > 0:
            if (now_ts - float(mtime)) < (SCAN_LIMITS.recent_file_window_hours * 3600):
                return Suspicion(path, "Ejecutable reciente detectado en carpeta volátil", "info")
    return None

ALL_CHECKS: Final[Sequence[SuspicionCheck]] = (
    check_empty_file,
    check_double_extension,
    check_system_lookalike,
    check_recent_executable_in_downloads
)

class Scanner:
    """Controlador central para la ejecución de heurísticas en el sistema de archivos."""
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen_normcase: set[str] = set()
        self.safe_cache: set[str] = set()
        # Se normaliza la ruta base de forma defensiva ante rutas que puedan fallar al resolver
        self.base_root_norm: str = os.path.normcase(os.path.abspath(base_root))
        self.now_ts: float = datetime.now().timestamp()

    @lru_cache(maxsize=1024)
    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica que la ruta sea descendiente del directorio raíz escaneado."""
        try:
            abs_path = os.path.normcase(os.path.abspath(entry_path))
            return abs_path.startswith(self.base_root_norm)
        except (OSError, RuntimeError, ValueError):
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Comprueba si un nombre de archivo es inválido para el sistema operativo."""
        if not isinstance(name, str) or not name: return True
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name) or RTL_CHAR_RE.search(name) or "\0" in name)

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si la entrada es un punto de reanálisis (Junction o Symlink)."""
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """Valida que una entrada no sea un enlace y respete las restricciones de seguridad."""
        if not isinstance(entry, os.DirEntry) or not entry.path:
            return False
        
        path_str: str = entry.path
        if path_str in self.safe_cache:
            return True
        
        try:
            if self._has_invalid_name(entry.name): return False
            if entry.is_symlink() or self._is_reparse_point(entry): return False
            if _cached_is_protected(path_str): return False
            if not _is_valid_path_structure(path_str): return False
            
            # Validación robusta de contención
            if not self._is_inside_base_root(path_str):
                return False
                
            self.safe_cache.add(path_str)
            return True
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            return False

    def _handle_directory(self, entry: os.DirEntry, directory_stack: List[Tuple[str, int]], current_depth: int, visited_dirs: set[str]) -> None:
        """Gestiona la inserción de subdirectorios en la pila de exploración evitando ciclos."""
        if current_depth >= SCAN_LIMITS.max_recursion_depth or not isinstance(entry, os.DirEntry) or not entry.path:
            return
        try:
            norm_path = os.path.normcase(entry.path)
            if norm_path not in visited_dirs and os.access(entry.path, os.R_OK):
                visited_dirs.add(norm_path)
                directory_stack.append((entry.path, current_depth + 1))
        except (OSError, PermissionError):
            pass

    def _is_relevant_extension(self, name: str) -> bool:
        """Filtra archivos relevantes para las heurísticas configuradas."""
        _, ext = os.path.splitext(name)
        return ext.lower() in SUSPICIOUS_ALL_EXTS

    def process_entry(self, entry: os.DirEntry, directory_stack: List[Tuple[str, int]], current_depth: int, visited_dirs: set[str]) -> None:
        """Procesa una entrada individual (directorio o archivo) del sistema."""
        if not isinstance(entry, os.DirEntry) or not entry.path or not self._is_safe_entry(entry):
            return
        try:
            # Uso de los métodos de DirEntry ya cacheada (is_file/is_dir) para evitar stat extra
            if entry.is_file():
                if self._is_relevant_extension(entry.name):
                    path_norm = os.path.normcase(entry.path)
                    if path_norm not in self.seen_normcase:
                        self.seen_normcase.add(path_norm)
                        self._run_file_heuristics(Path(entry.path), entry)
            elif entry.is_dir():
                self._handle_directory(entry, directory_stack, current_depth, visited_dirs)
        except (OSError, PermissionError, AttributeError):
            logger.debug(f"Error accediendo a entrada: {entry.path}")

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta la suite de heurísticas sobre un archivo validado."""
        for check_fn in ALL_CHECKS:
            try:
                finding = check_fn(path, entry, self.now_ts)
                if finding:
                    self.results.append(finding)
            except Exception as e:
                logger.debug(f"Heurística {check_fn.__name__} falló en {path}: {type(e).__name__}")

def scan_file(path: Path, now_ts: Timestamp, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Realiza un escaneo heurístico individual sobre un archivo específico."""
    if not isinstance(path, Path) or not _is_valid_path_structure(str(path)) or _cached_is_protected(str(path)) or not _is_readable(path):
        return []
    findings: ScanResult = []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts)
            if res:
                findings.append(res)
        except Exception as e:
            logger.debug(f"Heurística puntual {check_fn.__name__} fallida en {path}: {type(e).__name__}")
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Realiza un escaneo recursivo y seguro de un directorio dado."""
    if directory is None: return []
    try:
        base_path = Path(directory)
        if not base_path.exists() or not base_path.is_dir() or not os.access(base_path, os.R_OK):
            return []
        # Usar resolución no estricta para manejar rutas que podrían bloquearse por sistema
        resolved_path = base_path.resolve(strict=False)
        if _cached_is_protected(str(resolved_path)):
            return []
        scanner: Scanner = Scanner(base_root=resolved_path)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return []
    
    directory_stack: List[Tuple[str, int]] = [(str(resolved_path), 0)]
    visited_dirs: set[str] = {os.path.normcase(str(resolved_path))}
    
    while directory_stack:
        current_dir, depth = directory_stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if entry: scanner.process_entry(entry, directory_stack, depth, visited_dirs)
        except (PermissionError, OSError):
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """Interactúa con Windows Defender vía PowerShell para reportar estado o iniciar escaneo."""
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
