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
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from functools import lru_cache
from typing import List, Optional, Union, Final, Callable, TypeAlias, NamedTuple, Sequence, Tuple, Protocol
from safety import is_protected_path

# Configuración de logger para el módulo
logger: Final = logging.getLogger(__name__)

class ScannerLimits(NamedTuple):
    """
    Parámetros de configuración para el motor de escaneo y sus límites de seguridad.
    """
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
    Todas las implementaciones deben ser funciones puras, operando solo sobre
    el estado proporcionado mediante parámetros.
    """
    def __call__(self, path: Path, entry: Optional[os.DirEntry], now_ts: float) -> Optional[Suspicion]: ...

ScanResult: TypeAlias = List[Suspicion]
DirectoryStack: TypeAlias = List[Tuple[str, int]]

# Expresiones regulares para validación de nombres y riesgos
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d]")
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

def _is_file_in_use(path: Path) -> bool:
    """Verifica si el archivo está bloqueado por otro proceso intentando abrirlo en modo exclusivo."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_EXCL)
        os.close(fd)
        return False
    except (OSError, PermissionError):
        return True

def _is_readable(path: Path) -> bool:
    """Valida si la ruta es un archivo regular, legible y sin bloqueos de acceso."""
    try:
        if not path.is_file():
            return False
        return os.access(path, os.R_OK) and not _is_file_in_use(path)
    except (OSError, PermissionError, ValueError):
        return False

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Extrae atributos de archivo Win32; utilizado para detectar puntos de reanálisis."""
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError):
        return 0

def _get_file_size(path: Path) -> int:
    """Retorna el tamaño en bytes del archivo. Devuelve -1 si el archivo es inaccesible."""
    try:
        return int(path.stat().st_size)
    except (OSError, PermissionError, FileNotFoundError, AttributeError, ValueError):
        return -1

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """
    Retorna metadatos de la entrada únicamente si:
    1. Es un archivo estándar (no symlink, no punto de reanálisis).
    2. Los atributos de sistema no indican estructuras recursivas.
    """
    try:
        if not entry.is_file(follow_symlinks=False) or entry.is_symlink():
            return None
        if _get_file_attributes(entry) & SCAN_LIMITS.reparse_point_attr_mask:
            return None
        return entry.stat(follow_symlinks=False)
    except (OSError, PermissionError, AttributeError):
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida integridad de la ruta contra límites de longitud API y caracteres inseguros/RTL."""
    if not path_str or len(path_str) > SCAN_LIMITS.max_path or "\0" in path_str:
        return False
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

def _is_target_extension(name: str) -> bool:
    """Determina si la extensión del archivo es un objetivo relevante para heurísticas."""
    return Path(name).suffix.lower() in SUSPICIOUS_ALL_EXTS

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Detecta el uso de doble extensión como técnica común de enmascaramiento de ejecutables."""
    if path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión detectada como técnica de enmascaramiento", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Analiza si un ejecutable fue creado recientemente dentro de carpetas de alto riesgo."""
    try:
        if path.parent.name.lower() not in TARGETED_DOWNLOAD_FOLDERS:
            return None
        if entry and (stats := _safe_stat(entry)):
            mtime = getattr(stats, "st_mtime", 0.0)
            if isinstance(mtime, (int, float)) and mtime > 0 and (now_ts - float(mtime)) < (SCAN_LIMITS.recent_hours * 3600):
                return Suspicion(path, "Ejecutable reciente detectado en carpeta volátil", "info")
    except (OSError, AttributeError, ValueError, TypeError):
        pass
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Identifica ejecutables que intentan suplantar procesos críticos fuera de System32."""
    try:
        if path.name and path.name.lower() in SYSTEM_LOOKALIKES:
            if SYSTEM32_LOWER not in str(path).lower():
                return Suspicion(path, "Nombre de proceso de sistema detectado fuera de directorio protegido", "warning")
    except (OSError, AttributeError, TypeError):
        pass
    return None

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Identifica archivos ejecutables con tamaño cero, frecuentemente usados como marcadores maliciosos."""
    if _get_file_size(path) == 0:
        return Suspicion(path, "Ejecutable vacío detectado", "warning")
    return None

ALL_CHECKS: Final[Sequence[SuspicionCheck]] = [
    check_double_extension,
    check_system_lookalike,
    check_recent_executable_in_downloads,
    check_empty_file
]

class Scanner:
    """Controlador de estado encargado de gestionar el escaneo recursivo del sistema."""
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.safe_cache: set[str] = set()
        self.base_root_str: str = str(base_root.resolve()).lower()
        self.now_ts: float = datetime.now().timestamp()

    @lru_cache(maxsize=2048)
    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica si la ruta analizada se mantiene dentro del árbol de escaneo definido."""
        try:
            resolved = str(Path(entry_path).resolve()).lower()
            return resolved.startswith(self.base_root_str)
        except (OSError, RuntimeError):
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Valida que el nombre de archivo no sea un alias de dispositivo reservado o termine en caracteres inválidos."""
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Identifica si una entrada es un punto de reanálisis (reparse point) para evitar ciclos."""
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_attr_mask)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """Verifica que la entrada sea transitable y no viole las políticas de seguridad del proyecto."""
        if not isinstance(entry, os.DirEntry) or entry.path is None:
            return False
        
        path_str = entry.path
        if path_str in self.safe_cache:
            return True
        
        if is_protected_path(Path(path_str)) or not _is_valid_path_structure(path_str) or self._has_invalid_name(entry.name):
            return False
            
        try:
            if entry.is_symlink() or self._is_reparse_point(entry):
                return False
                
            real_path = Path(path_str).resolve(strict=True)
            if not str(real_path).lower().startswith(self.base_root_str):
                return False
                
            self.safe_cache.add(path_str)
            return True
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            return False

    def _handle_directory(self, entry: os.DirEntry, directory_stack: DirectoryStack, current_depth: int) -> None:
        """Gestiona la cola de directorios para la recursión segura, controlando la profundidad máxima."""
        if current_depth >= SCAN_LIMITS.max_depth or entry.path is None:
            return
        
        path_lower = entry.path.lower()
        if path_lower not in self.seen:
            self.seen.add(path_lower)
            directory_stack.append((entry.path, current_depth + 1))

    @staticmethod
    @lru_cache(maxsize=1024)
    def _is_relevant_extension(name: str) -> bool:
        """Filtra archivos relevantes por extensión para optimizar el análisis heurístico."""
        return _is_target_extension(name)

    def process_entry(self, entry: os.DirEntry, directory_stack: DirectoryStack, current_depth: int) -> None:
        """Despacha la entrada hacia el análisis correspondiente según su tipo."""
        if not isinstance(entry, os.DirEntry) or entry.path is None:
            return
            
        try:
            if entry.is_dir(follow_symlinks=False):
                if self._is_safe_entry(entry):
                    self._handle_directory(entry, directory_stack, current_depth)
            elif entry.is_file(follow_symlinks=False) and self._is_relevant_extension(entry.name):
                # Validar seguridad solo si la extensión es relevante para evitar I/O innecesario
                if self._is_safe_entry(entry):
                    self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError):
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta el conjunto de heurísticas definido sobre un archivo validado."""
        if not _is_readable(path):
            return
        for check_fn in ALL_CHECKS:
            try:
                finding = check_fn(path, entry, self.now_ts)
                if finding is not None:
                    self.results.append(finding)
            except (OSError, PermissionError, AttributeError, ValueError) as e:
                logger.debug(f"Heurística {check_fn.__name__} falló en {path}: {e}")

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Ejecuta un escaneo heurístico individual sobre un archivo específico."""
    if not isinstance(path, Path) or is_protected_path(path): return []
    try:
        if not path.exists() or not _is_readable(path): 
            return []
    except (OSError, RuntimeError):
        return []
    
    findings: ScanResult = []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts)
            if res is not None: findings.append(res)
        except (OSError, PermissionError, AttributeError, ValueError):
            continue
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Inicia el recorrido recursivo mediante un modelo de pila con límites de seguridad estrictos."""
    if directory is None: return []
    try:
        path_str = str(directory).strip()
        if not path_str or not _is_valid_path_structure(path_str): return []
        base_path = Path(path_str).resolve(strict=True)
        if not base_path.is_dir() or base_path.is_symlink() or not os.access(base_path, os.R_OK) or is_protected_path(base_path):
            return []
        scanner = Scanner(base_root=base_path)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError): return []
    
    directory_stack: DirectoryStack = [(str(base_path), 0)]
    scanner.seen.add(str(base_path).lower())
    while directory_stack:
        current_dir, depth = directory_stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if entry is not None:
                        scanner.process_entry(entry, directory_stack, depth)
        except (PermissionError, OSError, AttributeError):
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """Ejecuta un escaneo rápido del sistema mediante el framework de Windows Defender."""
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
