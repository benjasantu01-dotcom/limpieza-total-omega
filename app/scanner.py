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

class ScannerLimits(NamedTuple):
    """Parámetros de configuración para el motor de escaneo y sus límites de seguridad."""
    max_path: int = 260
    recent_hours: int = 24
    reparse_point_attr_mask: int = 0x400
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
    
    Toda implementación debe ser una función pura que no realice operaciones 
    de entrada/salida no autorizadas y opere exclusivamente sobre los 
    parámetros proporcionados (path, entry, now_ts).
    """
    def __call__(self, path: Path, entry: Optional[os.DirEntry], now_ts: float) -> Optional[Suspicion]: ...

# Alias de tipos para mejorar la legibilidad de la estructura de datos
ScanResult: TypeAlias = List[Suspicion]
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
    """Caché para la validación de rutas protegidas para evitar syscalls repetitivas."""
    return is_protected_path(Path(path_str))

def _is_valid_file_path(path: Path) -> bool:
    """Valida la integridad básica de un Path antes de realizar operaciones de I/O."""
    try:
        return path.exists() and path.is_file()
    except (OSError, PermissionError, ValueError):
        return False

def _is_locked_by_os(path: Path) -> bool:
    """
    Verifica si un archivo está bloqueado por otro proceso.
    Intenta abrir un handle exclusivo (lectura). Si falla, se asume bloqueado.
    """
    if not _is_valid_file_path(path):
        return True
    try:
        # 0x80000000: GENERIC_READ, 0x00000080: FILE_ATTRIBUTE_NORMAL
        handle = ctypes.windll.kernel32.CreateFileW(
            str(path), 0x80000000, 0, None, 3, 0x00000080, None
        )
        if handle == -1 or handle == 0: 
            return True
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    except (OSError, AttributeError, TypeError, ValueError):
        return True

def _is_readable(path: Path) -> bool:
    """Valida si el archivo existe y posee permisos de lectura mediante el sistema operativo."""
    if not _is_valid_file_path(path):
        return False
    try:
        return os.access(path, os.R_OK)
    except (OSError, PermissionError, ValueError, AttributeError):
        return False

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Extrae atributos de archivo Win32; utilizado para detectar puntos de reanálisis."""
    if not isinstance(entry, os.DirEntry):
        return 0
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError):
        return 0

def _get_file_size(path: Path) -> int:
    """Retorna el tamaño en bytes del archivo. Devuelve -1 si el archivo es inaccesible."""
    if not _is_valid_file_path(path): 
        return -1
    try:
        return int(path.stat().st_size)
    except (OSError, PermissionError, FileNotFoundError, AttributeError, ValueError, TypeError):
        return -1

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """Retorna metadatos solo si es un archivo estándar, excluyendo symlinks y reparse points."""
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
    except (OSError, PermissionError, AttributeError):
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida la integridad de una ruta contra límites de API (MAX_PATH) y caracteres maliciosos (RTL/Control)."""
    if not isinstance(path_str, str) or not path_str.strip():
        return False
    if len(path_str) > SCAN_LIMITS.max_path or "\0" in path_str:
        return False
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

def _is_target_extension(name: str) -> bool:
    """Determina si la extensión del archivo requiere inspección heurística por estar en la lista de riesgo."""
    try:
        if not name or RTL_CHAR_RE.search(name): return False
        return Path(name).suffix.lower() in SUSPICIOUS_ALL_EXTS
    except (ValueError, TypeError):
        return False

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0) -> Optional[Suspicion]:
    """
    Analiza si el nombre del archivo contiene una extensión doble sospechosa.
    Ejemplo: 'reporte.pdf.exe'. Retorna una sospecha de nivel 'warning' si coincide con el patrón.
    """
    if path is None or not path.name: return None
    try:
        if DOUBLE_EXTENSION_RE.search(path.name):
            return Suspicion(path, "Doble extensión detectada como técnica de enmascaramiento", "warning")
    except (UnicodeDecodeError, AttributeError, TypeError):
        pass
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0) -> Optional[Suspicion]:
    """
    Detecta ejecutables creados recientemente dentro de carpetas de descarga o temporales.
    Utiliza el timestamp de modificación comparado con `now_ts` para determinar frescura.
    """
    if path is None or path.parent is None: return None
    try:
        if path.parent.name.lower() not in TARGETED_DOWNLOAD_FOLDERS:
            return None
        if entry and (stats := _safe_stat(entry)):
            mtime = getattr(stats, "st_mtime", 0.0)
            if isinstance(mtime, (int, float)) and mtime > 0 and (now_ts - float(mtime)) < (SCAN_LIMITS.recent_hours * 3600):
                return Suspicion(path, "Ejecutable reciente detectado en carpeta volátil", "info")
    except (OSError, AttributeError, ValueError, TypeError) as e:
        logger.debug(f"Error analizando fecha de archivo {path}: {e}")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0) -> Optional[Suspicion]:
    """
    Detecta binarios maliciosos que intentan suplantar procesos del sistema (ej. svchost.exe) 
    ubicándose fuera del directorio System32.
    """
    if path is None or not path.name: return None
    try:
        if path.name.lower() in SYSTEM_LOOKALIKES:
            if SYSTEM32_LOWER not in str(path).lower():
                return Suspicion(path, "Nombre de proceso de sistema detectado fuera de directorio protegido", "warning")
    except (OSError, AttributeError, TypeError, UnicodeDecodeError):
        pass
    return None

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: Timestamp = 0.0) -> Optional[Suspicion]:
    """
    Identifica archivos ejecutables con tamaño de 0 bytes, práctica a veces utilizada 
    en exploits para crear marcadores o disparadores de ejecución.
    """
    if path is None: return None
    try:
        if _get_file_size(path) == 0:
            return Suspicion(path, "Ejecutable vacío detectado", "warning")
    except (OSError, PermissionError, AttributeError) as e:
        logger.debug(f"Error verificando tamaño en {path}: {e}")
    return None

ALL_CHECKS: Final[Sequence[SuspicionCheck]] = (
    check_double_extension,
    check_system_lookalike,
    check_recent_executable_in_downloads,
    check_empty_file
)

class Scanner:
    """Controlador encargado de gestionar el escaneo recursivo y persistir resultados."""
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.safe_cache: set[str] = set()
        self.path_stat_cache: Dict[str, bool] = {}
        try:
            self.base_root_str: str = str(base_root.resolve()).lower()
        except (OSError, RuntimeError):
            self.base_root_str = str(base_root).lower()
        self.now_ts: float = datetime.now().timestamp()

    @lru_cache(maxsize=2048)
    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica mediante resolución de ruta si el elemento pertenece al árbol de escaneo."""
        try:
            resolved: str = str(Path(entry_path).resolve()).lower()
            return resolved.startswith(self.base_root_str)
        except (OSError, RuntimeError):
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Valida que el nombre de archivo no sea un alias reservado por Windows o tenga caracteres ocultos."""
        if not name: return True
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name) or RTL_CHAR_RE.search(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Identifica si una entrada es un punto de reanálisis (Junction) mediante atributos Win32."""
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_attr_mask)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """
        Valida que la entrada sea segura para procesar: verifica jerarquía, 
        permisos, existencia, y ausencia de puntos de reanálisis (Junctions).
        """
        if entry is None or not hasattr(entry, 'path') or not entry.path:
            return False
        
        path_str: str = entry.path
        if path_str in self.safe_cache:
            return True
        
        try:
            # 1. Validación de enlaces y reparse points
            if entry.is_symlink():
                return False
            if self._is_reparse_point(entry):
                return False
            
            # 2. Validación de protección contra rutas del sistema
            if _cached_is_protected(path_str):
                return False

            # 3. Validación de jerarquía mediante resolución absoluta segura
            p = Path(path_str)
            if not p.exists():
                return False
            
            real_path: Path = p.resolve()
            if not str(real_path).lower().startswith(self.base_root_str):
                return False
                
            # Adicional: Chequeo de inmutabilidad básica (el disco no debe ser de solo lectura)
            if not os.access(p.parent, os.R_OK):
                return False

            # 4. Validación de integridad de nombre
            if not _is_valid_path_structure(path_str) or self._has_invalid_name(entry.name):
                return False
                
            self.safe_cache.add(path_str)
            return True
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            return False

    def _handle_directory(self, entry: os.DirEntry, directory_stack: DirectoryStack, current_depth: int) -> None:
        """Registra subdirectorios válidos en la pila si no se supera el límite de profundidad (max_depth)."""
        if current_depth >= SCAN_LIMITS.max_depth or entry is None or not hasattr(entry, 'path') or not entry.path:
            return
        
        path_lower: str = entry.path.lower()
        if path_lower not in self.seen:
            self.seen.add(path_lower)
            directory_stack.append((entry.path, current_depth + 1))

    @staticmethod
    @lru_cache(maxsize=1024)
    def _is_relevant_extension(name: str) -> bool:
        """Determina si un nombre de archivo requiere inspección heurística completa."""
        return _is_target_extension(name)

    def process_entry(self, entry: os.DirEntry, directory_stack: DirectoryStack, current_depth: int) -> None:
        """
        Gestiona la ramificación del escaneo entre directorios y archivos.
        
        Determina si la entrada es un directorio para continuar la recursión o 
        si es un archivo candidato a análisis heurístico. Valida la seguridad 
        antes de cualquier operación de I/O.
        """
        if entry is None or not hasattr(entry, 'path') or not entry.path:
            return
            
        try:
            if not self._is_safe_entry(entry):
                return

            if entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, directory_stack, current_depth)
            elif entry.is_file(follow_symlinks=False):
                if self._is_relevant_extension(entry.name):
                    self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError) as e:
            logger.debug(f"Error de acceso en entrada {entry.path}: {e}")

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """
        Ejecuta la suite completa de heurísticas (ALL_CHECKS) sobre un archivo validado,
        gestionando el estado de legibilidad para optimizar el escaneo de red o disco.
        """
        if path is None: return
        path_str = str(path)
        if path_str not in self.path_stat_cache:
            # Re-verificación estricta de seguridad ante TOCTOU
            if not _is_readable(path) or _is_locked_by_os(path):
                self.path_stat_cache[path_str] = False
                return
            self.path_stat_cache[path_str] = True

        if not self.path_stat_cache[path_str]:
            return

        for check_fn in ALL_CHECKS:
            try:
                finding: Optional[Suspicion] = check_fn(path, entry, self.now_ts)
                if finding is not None:
                    self.results.append(finding)
            except (OSError, PermissionError, AttributeError, ValueError, TypeError) as e:
                logger.debug(f"Heurística {check_fn.__name__} falló en {path}: {e}")

def scan_file(path: Path, now_ts: Timestamp, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Escaneo heurístico individual para un archivo específico, omitiendo rutas protegidas o bloqueadas."""
    if path is None or not isinstance(path, Path) or not _is_valid_file_path(path) or _cached_is_protected(str(path)): 
        return []
    if not _is_readable(path) or _is_locked_by_os(path): 
        return []
    
    findings: ScanResult = []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts)
            if res is not None: findings.append(res)
        except (OSError, PermissionError, AttributeError, ValueError, TypeError) as e:
            logger.debug(f"Heurística individual {check_fn.__name__} fallida en {path}: {e}")
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """
    Realiza el escaneo recursivo mediante una estructura de pila (stack) sobre 
    un directorio base, validando permisos y rutas antes de iniciar el proceso.
    
    Returns:
        Una lista de objetos Suspicion encontrados durante el recorrido.
    """
    if directory is None: return []
    try:
        path_str: str = str(directory).strip()
        if not path_str or not _is_valid_path_structure(path_str): return []
        base_path: Path = Path(path_str).resolve(strict=True)
        if not base_path.is_dir() or base_path.is_symlink() or not os.access(base_path, os.R_OK) or _cached_is_protected(str(base_path)):
            return []
        scanner: Scanner = Scanner(base_root=base_path)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as e:
        logger.error(f"Error inicializando escaneo en {directory}: {e}")
        return []
    
    directory_stack: DirectoryStack = [(str(base_path), 0)]
    scanner.seen.add(str(base_path).lower())
    while directory_stack:
        current_dir, depth = directory_stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if entry is not None:
                        scanner.process_entry(entry, directory_stack, depth)
        except (PermissionError, OSError) as e:
            logger.debug(f"Directorio inaccesible omitido {current_dir}: {e}")
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """
    Ejecuta un escaneo rápido del sistema mediante el framework de Windows Defender.
    Verifica primero el estado de RealTimeProtectionEnabled para evitar escaneos innecesarios.
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
