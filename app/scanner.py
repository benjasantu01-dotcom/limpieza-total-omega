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
    Protocolo de interfaz para definir una nueva heurística de seguridad.
    
    Cada chequeo debe recibir el contexto del archivo y retornar un objeto 
    Suspicion si se detecta un riesgo, o None en caso contrario.
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
# Stack compuesto por (ruta_absoluta, nivel_profundidad_actual)
TraversalStack: TypeAlias = List[Tuple[str, int]]
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
SUSPICIOUS_ALL_EXTS: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1", ".pdf"})
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
TARGETED_DOWNLOAD_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})
SYSTEM32_LOWER: Final[str] = "system32"

@lru_cache(maxsize=4096)
def _cached_is_protected(path_str: str) -> bool:
    """Valida contra la política de seguridad central usando caché LRU para evitar llamadas repetitivas."""
    try:
        return is_protected_path(Path(path_str))
    except (ValueError, TypeError, OSError):
        return True

def _is_safe_by_attributes(path: Path) -> bool:
    """
    Verifica mediante la API de archivos de Win32 que la entrada no es un 
    punto de reanálisis (Junction o Symlink) para evitar bucles o escape de raíz.
    """
    try:
        attrs = path.stat().st_file_attributes
        return not (attrs & SCAN_LIMITS.reparse_point_mask)
    except (OSError, AttributeError):
        return True

def _is_valid_file_path(path: PathInput) -> bool:
    """Realiza validación exhaustiva de existencia, permisos y seguridad de la ruta."""
    if path is None:
        return False
    try:
        path_str = str(path)
        if not path_str or "\0" in path_str:
            return False
        abs_path = os.path.abspath(path_str)
        if len(abs_path) > SCAN_LIMITS.path_length_limit:
            return False
        target_path = Path(abs_path)
        return target_path.exists() and not _cached_is_protected(abs_path) and target_path.is_file() and _is_safe_by_attributes(target_path)
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError):
        return False

def _is_readable(path: PathInput) -> bool:
    """Verifica si el proceso actual tiene privilegios para acceder en modo solo lectura."""
    if path is None:
        return False
    try:
        path_str = str(path)
        if not path_str or "\0" in path_str or len(path_str) > SCAN_LIMITS.path_length_limit:
            return False
        if _cached_is_protected(path_str):
            return False
        return os.access(path_str, os.R_OK) and _is_safe_by_attributes(Path(path_str))
    except (OSError, PermissionError, ValueError, TypeError):
        return False

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Recupera los atributos de archivo mediante syscall sin seguir enlaces simbólicos."""
    if not isinstance(entry, os.DirEntry):
        return 0
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError, FileNotFoundError):
        return 0

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """
    Obtiene metadatos de archivo. Valida que no sea un punto de reanálisis 
    y maneja errores de sistema comunes (bloqueos, acceso denegado).
    """
    if not isinstance(entry, os.DirEntry) or not entry.path or "\0" in entry.path:
        return None
        
    if _cached_is_protected(entry.path) or (_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask):
        return None
        
    try:
        # Usamos lstat/stat sin seguir enlaces para evitar recursión inesperada fuera del árbol
        return entry.stat(follow_symlinks=False)
    except OSError as e:
        if getattr(e, 'winerror', None) in (ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND):
            return None
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida la integridad de una ruta contra las restricciones de longitud, caracteres y seguridad."""
    if not isinstance(path_str, str) or not path_str.strip() or "\0" in path_str:
        return False
    try:
        abs_path = os.path.abspath(path_str)
        if _cached_is_protected(abs_path) or len(abs_path) > SCAN_LIMITS.path_length_limit:
            return False
        if UNC_PATH_RE.match(abs_path) or RTL_CHAR_RE.search(abs_path):
            return False
        return True
    except (OSError, ValueError, TypeError):
        return False

# --- Suite de Heurísticas ---

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Riesgo: Archivos de 0 bytes utilizados como marcadores de posición o punteros."""
    if stats is not None and getattr(stats, "st_size", -1) == 0:
        return Suspicion(path, "Archivo vacío detectado (posible marcador de relleno)", "warning")
    return None

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Riesgo: Ofuscación mediante doble extensión (ej: informe.pdf.exe)."""
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión detectada como técnica de ofuscación", "warning")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Riesgo: Ejecución de procesos críticos fuera de System32."""
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        if SYSTEM32_LOWER not in str(path).lower():
            return Suspicion(path, "Nombre de proceso del sistema fuera de ubicación legítima", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0, stats: Optional[os.stat_result] = None) -> Optional[Suspicion]:
    """Riesgo: Ejecutables descargados recientemente en directorios volátiles."""
    if path is None or path.parent is None or stats is None: 
        return None
    try:
        if path.parent.name.lower() in TARGETED_DOWNLOAD_FOLDERS:
            mtime = getattr(stats, "st_mtime", None)
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
    """Controlador central para la ejecución del escaneo recursivo mediante stack."""
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen_normcase: set[str] = set()
        self.safe_cache: set[str] = set()
        self.base_root_real: str = os.path.normcase(os.path.realpath(str(base_root)))
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica que la ruta resuelta esté contenida bajo la raíz de escaneo."""
        try:
            real_entry = os.path.normcase(os.path.realpath(entry_path))
            return real_entry.startswith(self.base_root_real)
        except OSError:
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Valida caracteres prohibidos o técnicas de ofuscación en nombres de archivo."""
        if not isinstance(name, str) or not name or "\0" in name: return True
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name) or RTL_CHAR_RE.search(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si la entrada es un punto de reanálisis para detener el recorrido."""
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_mask)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """
        Realiza filtros de seguridad iniciales sobre la entrada del sistema de archivos.
        Verifica que no sea un reparse point, no esté en rutas protegidas y
        permanezca dentro de los límites de la raíz de escaneo definida.
        """
        path_str: str = entry.path
        if path_str in self.safe_cache: return True
        
        # Validaciones de integridad antes de acceder al sistema de archivos
        if self._has_invalid_name(entry.name): return False
        if self._is_reparse_point(entry): return False
        if _cached_is_protected(path_str): return False
        if not self._is_inside_base_root(path_str): return False
        
        self.safe_cache.add(path_str)
        return True

    def _handle_directory(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: set[str]) -> None:
        """
        Gestiona la inserción de subdirectorios en el stack de búsqueda (DFS).
        Controla la profundidad máxima permitida para evitar ataques de recursión infinita.
        """
        if depth >= SCAN_LIMITS.max_recursion_depth: return
        norm_path = os.path.normcase(entry.path)
        if norm_path in visited: return
        try:
            # Re-verificar reparse point antes de encolar
            if os.access(entry.path, os.R_OK) and not _cached_is_protected(entry.path):
                visited.add(norm_path)
                stack.append((entry.path, depth + 1))
        except (OSError, PermissionError):
            pass

    @lru_cache(maxsize=128)
    def _is_relevant_extension(self, name: str) -> bool:
        """Filtra extensiones que no requieren análisis heurístico."""
        return os.path.splitext(name)[1].lower() in SUSPICIOUS_ALL_EXTS

    def process_entry(self, entry: os.DirEntry, stack: TraversalStack, depth: int, visited: set[str]) -> None:
        """
        Clasifica la entrada actual: si es un archivo de interés, ejecuta la
        suite heurística; si es un directorio, lo encola para continuar el recorrido.
        """
        if not self._is_safe_entry(entry):
            return
        
        # Optimización: Filtrado de extensión antes de cualquier I/O adicional
        if entry.is_dir():
            self._handle_directory(entry, stack, depth, visited)
        elif self._is_relevant_extension(entry.name):
            path_norm = os.path.normcase(entry.path)
            if path_norm not in self.seen_normcase:
                self.seen_normcase.add(path_norm)
                self._run_file_heuristics(Path(entry.path), entry)

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """
        Ejecuta todas las funciones de detección (heurísticas) configuradas sobre
        un archivo específico y guarda los resultados si se detectan anomalías.
        """
        stats = _safe_stat(entry)
        if stats is None:
            return
        for check_fn in ALL_CHECKS:
            try:
                finding = check_fn(path, entry, self.now_ts, stats)
                if finding:
                    self.results.append(finding)
            except Exception as e:
                logger.debug(f"Heurística {check_fn.__name__} omitida para {path.name} por fallo crítico: {e}")

def scan_file(path: Path, now_ts: Timestamp, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Escaneo puntual para un archivo específico, fuera de la recursión."""
    if not isinstance(path, Path) or not _is_valid_path_structure(str(path)) or not _is_readable(path):
        return []
    try:
        if not path.is_file(): return []
        stats = entry.stat() if entry else path.stat()
    except (OSError, PermissionError):
        return []
        
    findings: ScanResult = []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts, stats)
            if res:
                findings.append(res)
        except Exception as e:
            logger.debug(f"Heurística {check_fn.__name__} omitida en archivo {path} por fallo: {e}")
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """
    Orquestador principal que recorre directorios recursivamente mediante un stack 
    para evitar desbordamientos de pila de Python.
    """
    if directory is None: return []
    try:
        base_path = Path(directory).resolve()
        if not base_path.exists() or not base_path.is_dir() or not _is_readable(str(base_path)):
            return []
        scanner: Scanner = Scanner(base_root=base_path)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return []
    
    # Estructura de pila (DFS) para el recorrido del árbol
    stack: TraversalStack = [(str(base_path), 0)]
    visited: set[str] = {os.path.normcase(str(base_path))}
    
    while stack:
        current_dir, depth = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    scanner.process_entry(entry, stack, depth, visited)
        except (PermissionError, OSError):
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """Invoca la API de PowerShell para ejecutar un escaneo rápido del sistema mediante Defender."""
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
