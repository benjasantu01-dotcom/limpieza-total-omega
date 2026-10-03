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
from typing import List, Optional, Union, Final, Callable, TypeAlias, NamedTuple, Dict, Sequence, Tuple
from safety import is_protected_path

# Configuración de logger para el módulo
logger: Final = logging.getLogger(__name__)

class ScannerLimits(NamedTuple):
    """
    Parámetros operativos críticos para el escaneo.
    """
    max_path: int = 260
    recent_hours: int = 24
    reparse_point_attr_mask: int = 0x400
    max_depth: int = 50

SCAN_LIMITS: Final = ScannerLimits()

@dataclass
class Suspicion:
    """
    Reporte de hallazgo tras inspección heurística.
    """
    path: Path
    reason: str
    severity: str

# Definición de tipos para el sistema de heurísticas
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]
ScanResult: TypeAlias = List[Suspicion]

# Expresiones regulares para detección de ofuscación
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
INVALID_TRAILING_CHARS_RE: Final[re.Pattern] = re.compile(r"[\. ]$")
UNC_PATH_RE: Final[re.Pattern] = re.compile(r"^\\\\[^\\\\]+\\")

# Conjuntos de constantes para comparación rápida
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SUSPICIOUS_CONTENT_EXT: Final[frozenset[str]] = frozenset({".pdf"})
SUSPICIOUS_ALL_EXTS: Final[frozenset[str]] = SUSPICIOUS_EXECUTABLE_EXT.union(SUSPICIOUS_CONTENT_EXT)
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
TARGETED_DOWNLOAD_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})

SYSTEM32_LOWER: Final[str] = "system32"

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """
    Obtiene metadatos asegurando que no se sigan enlaces simbólicos.
    Retorna None si la entrada es un enlace, punto de reanálisis o falla el acceso.
    """
    if not isinstance(entry, os.DirEntry):
        return None
    try:
        if entry.is_symlink():
            return None
        attr = _get_file_attributes(entry)
        if attr & SCAN_LIMITS.reparse_point_attr_mask:
            return None
        stats = entry.stat(follow_symlinks=False)
        if getattr(stats, "st_nlink", 1) > 1:
            return None
        return stats
    except (OSError, PermissionError):
        return None

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Consulta la máscara de bits de atributos Win32 de forma pasiva mediante stat."""
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError):
        return 0

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida la integridad de la cadena de la ruta antes de intentar cualquier operación de I/O."""
    if not path_str or len(path_str) > SCAN_LIMITS.max_path:
        return False
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Analiza nombres de archivo buscando patrones que ocultan extensiones ejecutables tras formatos comunes."""
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Verifica si un ejecutable fue creado recientemente en directorios de alta exposición."""
    if not path or not path.parent or path.parent.name.lower() not in TARGETED_DOWNLOAD_FOLDERS:
        return None
    stats = _safe_stat(entry) if entry else None
    if stats:
        mtime = getattr(stats, "st_mtime", None)
        if isinstance(mtime, (int, float)) and mtime > 0:
            if (now_ts - float(mtime)) < (SCAN_LIMITS.recent_hours * 3600):
                return Suspicion(path, f"Ejecutable reciente (<{SCAN_LIMITS.recent_hours}h)", "info")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Detecta archivos que imitan nombres de procesos críticos del sistema fuera de su ubicación legítima."""
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        path_str = str(path).lower()
        if SYSTEM32_LOWER not in path_str:
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Identifica archivos ejecutables de 0 bytes, a menudo utilizados en técnicas de engaño."""
    stats = _safe_stat(entry) if entry else None
    if stats is not None:
        size = getattr(stats, "st_size", -1)
        if isinstance(size, int) and size == 0:
            return Suspicion(path, "Archivo ejecutable vacío sospechoso", "warning")
    return None

ALL_CHECKS: Final[Sequence[SuspicionCheck]] = [
    check_double_extension,
    check_system_lookalike,
    check_recent_executable_in_downloads,
    check_empty_file
]

class Scanner:
    """
    Clase responsable de gestionar el estado del escaneo recursivo.
    Mantiene cachés para prevenir el procesamiento redundante y asegurar el confinamiento.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: List[Suspicion] = []
        self.seen: set[str] = set()
        self.safe_cache: set[str] = set()
        self._root_cache: Dict[str, bool] = {}
        self.base_root: Path = base_root.resolve()
        self.base_root_str: str = str(self.base_root).lower()
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica que la entrada se mantenga dentro del directorio raíz inicial (Prevención de escape)."""
        if entry_path in self._root_cache:
            return self._root_cache[entry_path]
        try:
            target = Path(entry_path).resolve(strict=False)
            result = str(target).lower().startswith(self.base_root_str)
            if len(self._root_cache) < 1000:
                self._root_cache[entry_path] = result
            return result
        except (OSError, RuntimeError):
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Determina si el nombre del archivo contiene caracteres o etiquetas prohibidas por el sistema."""
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Verifica la existencia de atributos de puntos de reanálisis en el sistema de archivos."""
        try:
            return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_attr_mask)
        except (OSError, PermissionError):
            return True 

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """Filtro de seguridad central. Valida estructura, permisos y exclusión de rutas protegidas."""
        if not isinstance(entry, os.DirEntry) or not entry.path:
            return False
        # Prevenir Null-byte injection
        if "\0" in entry.path:
            return False
        if not _is_valid_path_structure(entry.path) or self._has_invalid_name(entry.name):
            return False
        try:
            if self._is_reparse_point(entry) or entry.is_symlink():
                return False
            if not self._is_inside_base_root(entry.path):
                return False
            if entry.path in self.safe_cache:
                return True
            if is_protected_path(Path(entry.path)):
                return False
            self.safe_cache.add(entry.path)
            return True
        except (OSError, RuntimeError):
            return False

    def _handle_directory(self, entry: os.DirEntry, directory_stack: List[Tuple[str, int]], current_depth: int) -> None:
        """Gestiona el stack de exploración de directorios aplicando límites de profundidad."""
        if current_depth >= SCAN_LIMITS.max_depth:
            return
        if entry.path and entry.path.lower() not in self.seen:
            self.seen.add(entry.path.lower())
            directory_stack.append((entry.path, current_depth + 1))

    def _is_relevant_extension(self, name: str) -> bool:
        """Optimización: solo analiza archivos con extensiones consideradas potencialmente riesgosas."""
        _, ext = os.path.splitext(name)
        return ext.lower() in SUSPICIOUS_ALL_EXTS

    def process_entry(self, entry: os.DirEntry, directory_stack: List[Tuple[str, int]], current_depth: int) -> None:
        """Analiza la entrada y despacha a la acción correspondiente según su tipo (Directorio vs Archivo)."""
        try:
            if not self._is_safe_entry(entry):
                return
            if entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, directory_stack, current_depth)
            elif entry.is_file(follow_symlinks=False):
                if self._is_relevant_extension(entry.name):
                    self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError):
            return

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta de forma aislada cada una de las heurísticas registradas."""
        for check_fn in ALL_CHECKS:
            try:
                finding = check_fn(path, entry, self.now_ts)
                if finding is not None:
                    self.results.append(finding)
            except (OSError, PermissionError, AttributeError, ValueError) as e:
                logger.debug(f"Error de acceso/dato en {check_fn.__name__} para {path}: {e}")

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> List[Suspicion]:
    """Análisis estático de un archivo puntual con pre-validación de acceso seguro."""
    if not isinstance(path, Path): 
        return []
    try:
        if not path.is_file() or not os.access(path, os.R_OK): 
            return []
        if is_protected_path(path): 
            return []
    except (OSError, PermissionError): 
        return []
    
    findings: List[Suspicion] = []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts)
            if res is not None:
                findings.append(res)
        except (OSError, PermissionError, AttributeError, ValueError):
            continue
    return findings

def scan_directory(directory: Union[str, Path, None]) -> List[Suspicion]:
    """
    Punto de entrada de alto nivel para escaneo recursivo. 
    Inicializa el motor de escaneo y procesa el sistema de archivos de forma iterativa.
    """
    if directory is None: return []
    path_str = str(directory).strip()
    if not path_str or not _is_valid_path_structure(path_str): return []
    try:
        base_path = Path(path_str).resolve()
        if not base_path.exists() or not base_path.is_dir() or base_path.is_symlink() or not os.access(base_path, os.R_OK):
            return []
        if is_protected_path(base_path): return []
    except (OSError, RuntimeError): 
        return []
    scanner = Scanner(base_root=base_path)
    directory_stack: List[Tuple[str, int]] = [(str(base_path), 0)]
    scanner.seen.add(str(base_path).lower())
    while directory_stack:
        current_dir, depth = directory_stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if entry is not None:
                        scanner.process_entry(entry, directory_stack, depth)
        except (PermissionError, OSError):
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """Interroga el estado de Defender y solicita una ejecución de escaneo rápido via PowerShell."""
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
