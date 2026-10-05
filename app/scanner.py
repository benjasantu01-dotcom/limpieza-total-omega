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
from typing import List, Optional, Union, Final, Callable, TypeAlias, NamedTuple, Dict, Sequence, Tuple
from safety import is_protected_path

# Configuración de logger para el módulo
logger: Final = logging.getLogger(__name__)

class ScannerLimits(NamedTuple):
    """Parámetros operativos críticos para limitar el alcance y evitar desbordamientos."""
    max_path: int = 260
    recent_hours: int = 24
    reparse_point_attr_mask: int = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
    max_depth: int = 50

SCAN_LIMITS: Final = ScannerLimits()

@dataclass(frozen=True)
class Suspicion:
    """
    Representación inmutable de un hallazgo heurístico.
    
    Attributes:
        path: Ruta absoluta donde se detectó el comportamiento.
        reason: Descripción clara y accionable del riesgo.
        severity: Nivel de urgencia ('info', 'warning', 'critical').
    """
    path: Path
    reason: str
    severity: str

# Definición de tipos para el sistema de heurísticas:
# Reciben (ruta, entrada_dir, timestamp_actual) y devuelven un objeto Suspicion o None
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]
ScanResult: TypeAlias = List[Suspicion]
DirectoryStack: TypeAlias = List[Tuple[str, int]]

# Expresiones regulares para detección de ofuscación de nombres
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
INVALID_TRAILING_CHARS_RE: Final[re.Pattern] = re.compile(r"[\. ]$")
UNC_PATH_RE: Final[re.Pattern] = re.compile(r"^\\\\[^\\\\]+\\")

# Conjuntos de constantes para comparación rápida O(1)
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SUSPICIOUS_CONTENT_EXT: Final[frozenset[str]] = frozenset({".pdf"})
SUSPICIOUS_ALL_EXTS: Final[frozenset[str]] = SUSPICIOUS_EXECUTABLE_EXT.union(SUSPICIOUS_CONTENT_EXT)
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
TARGETED_DOWNLOAD_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})

SYSTEM32_LOWER: Final[str] = "system32"

def _is_readable(path: Path) -> bool:
    """Verifica si el archivo existe y posee permisos de lectura."""
    if not isinstance(path, Path):
        return False
    try:
        return path.is_file() and os.access(path, os.R_OK)
    except (OSError, PermissionError, ValueError, AttributeError):
        return False

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Consulta la máscara de bits de atributos Win32 del archivo mediante syscall."""
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError):
        return 0

def _get_file_size(path: Path) -> int:
    """Obtiene el tamaño del archivo con manejo robusto de excepciones de concurrencia."""
    if not isinstance(path, Path):
        return -1
    try:
        size = path.stat().st_size
        return int(size) if size >= 0 else -1
    except (OSError, PermissionError, FileNotFoundError, AttributeError):
        return -1

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """Extrae metadatos validando que el archivo no sea un enlace simbólico o reanálisis."""
    if not isinstance(entry, os.DirEntry):
        return None
    try:
        if entry.is_symlink():
            return None
        if _get_file_attributes(entry) & SCAN_LIMITS.reparse_point_attr_mask:
            return None
        stats = entry.stat(follow_symlinks=False)
        if getattr(stats, "st_nlink", 1) > 1:
            return None
        return stats
    except (OSError, PermissionError, AttributeError):
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida la integridad de la cadena de la ruta (largo y caracteres especiales)."""
    if not path_str or len(path_str) > SCAN_LIMITS.max_path:
        return False
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

def _is_target_extension(name: str) -> bool:
    """Verifica si la extensión del archivo es candidata para el análisis heurístico."""
    return Path(name).suffix.lower() in SUSPICIOUS_ALL_EXTS

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Heurística: Detecta doble extensión que oculta el tipo real de archivo."""
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Heurística: Identifica ejecutables recientes (<24h) en carpetas de alto riesgo."""
    if not path or not path.parent:
        return None
    if path.parent.name.lower() not in TARGETED_DOWNLOAD_FOLDERS:
        return None
    stats = _safe_stat(entry) if entry else None
    if stats:
        mtime = getattr(stats, "st_mtime", None)
        if isinstance(mtime, (int, float)) and mtime > 0:
            if (now_ts - float(mtime)) < (SCAN_LIMITS.recent_hours * 3600):
                return Suspicion(path, f"Ejecutable reciente (<{SCAN_LIMITS.recent_hours}h)", "info")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Heurística: Detecta procesos críticos (ej: svchost.exe) ubicados fuera de System32."""
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        path_str = str(path).lower()
        if SYSTEM32_LOWER not in path_str:
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Heurística: Identifica archivos ejecutables de 0 bytes."""
    size = _get_file_size(path)
    if size == 0:
        return Suspicion(path, "Archivo ejecutable vacío sospechoso", "warning")
    return None

ALL_CHECKS: Final[Sequence[SuspicionCheck]] = [
    check_double_extension,
    check_system_lookalike,
    check_recent_executable_in_downloads,
    check_empty_file
]

class Scanner:
    """Motor principal que gestiona el estado y la recursión segura del escaneo."""
    
    def __init__(self, base_root: Path) -> None:
        self.results: List[Suspicion] = []
        self.seen: set[str] = set()
        self.safe_cache: set[str] = set()
        self.base_root_str: str = str(base_root.resolve()).lower()
        self.now_ts: float = datetime.now().timestamp()

    @lru_cache(maxsize=2048)
    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica mediante resolución de ruta que el archivo reside en el árbol de escaneo."""
        try:
            resolved = str(Path(entry_path).resolve()).lower()
            return resolved.startswith(self.base_root_str)
        except (OSError, RuntimeError):
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Valida contra nombres de dispositivos reservados por el SO (ej: NUL, CON)."""
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si un objeto es un punto de reanálisis (Junction o Symlink)."""
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_attr_mask)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """Filtro de seguridad: Valida integridad, reanálisis y exclusiones (whitelist)."""
        if not isinstance(entry, os.DirEntry) or not entry.path or "\0" in entry.path:
            return False
        if entry.path in self.safe_cache:
            return True
        if not _is_valid_path_structure(entry.path) or self._has_invalid_name(entry.name):
            return False
        try:
            if self._is_reparse_point(entry) or entry.is_symlink():
                return False
            if not self._is_inside_base_root(entry.path):
                return False
            if is_protected_path(Path(entry.path)):
                return False
            self.safe_cache.add(entry.path)
            return True
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            return False

    def _handle_directory(self, entry: os.DirEntry, directory_stack: DirectoryStack, current_depth: int) -> None:
        """Apila directorios para procesarlos iterativamente, respetando el límite de profundidad."""
        if current_depth >= SCAN_LIMITS.max_depth:
            return
        path_lower = entry.path.lower() if entry.path else ""
        if path_lower and path_lower not in self.seen:
            self.seen.add(path_lower)
            directory_stack.append((entry.path, current_depth + 1))

    @staticmethod
    @lru_cache(maxsize=1024)
    def _is_relevant_extension(name: str) -> bool:
        """Cachea si una extensión pertenece al conjunto de archivos que requieren heurística."""
        return _is_target_extension(name)

    def process_entry(self, entry: os.DirEntry, directory_stack: DirectoryStack, current_depth: int) -> None:
        """Orquestador: Decide si explorar subdirectorio o analizar archivo."""
        try:
            if not entry.path or not os.path.exists(entry.path): 
                return
            
            is_dir = entry.is_dir(follow_symlinks=False)
            if not is_dir and not self._is_relevant_extension(entry.name):
                return
            
            if not self._is_safe_entry(entry):
                return
                
            if is_dir:
                self._handle_directory(entry, directory_stack, current_depth)
            else:
                self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError, AttributeError):
            return

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta toda la suite de heurísticas sobre el archivo indicado."""
        if not _is_readable(path):
            return
        for check_fn in ALL_CHECKS:
            try:
                finding = check_fn(path, entry, self.now_ts)
                if finding is not None:
                    self.results.append(finding)
            except (OSError, PermissionError, AttributeError, ValueError) as e:
                logger.debug(f"Error en {check_fn.__name__} para {path}: {e}")

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> List[Suspicion]:
    """Realiza un análisis heurístico único sobre un archivo validado individualmente."""
    if not isinstance(path, Path) or not _is_readable(path) or is_protected_path(path): 
        return []
    
    findings: List[Suspicion] = []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts)
            if res is not None: findings.append(res)
        except (OSError, PermissionError, AttributeError, ValueError):
            continue
    return findings

def scan_directory(directory: Union[str, Path, None]) -> List[Suspicion]:
    """
    Escaneo recursivo mediante stack manual para prevenir desbordamiento de pila.
    Es la función de entrada principal para el análisis masivo de directorios.
    """
    if directory is None: return []
    try:
        path_str = str(directory).strip()
        if not path_str or not _is_valid_path_structure(path_str): return []
        base_path = Path(path_str).resolve()
        if not base_path.exists() or not base_path.is_dir() or base_path.is_symlink() or not os.access(base_path, os.R_OK):
            return []
        if is_protected_path(base_path): return []
        scanner = Scanner(base_root=base_path)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError): return []
    
    directory_stack: DirectoryStack = [(str(base_path), 0)]
    scanner.seen.add(str(base_path).lower())
    while directory_stack:
        current_dir, depth = directory_stack.pop()
        try:
            with os.scandir(current_dir) as it:
                while True:
                    try:
                        entry = next(it)
                        if entry: scanner.process_entry(entry, directory_stack, depth)
                    except StopIteration:
                        break
                    except (UnicodeDecodeError, OSError):
                        continue
        except (PermissionError, OSError, AttributeError):
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """Invoca la API de PowerShell de Windows Defender para iniciar un QuickScan."""
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
