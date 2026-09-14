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
from typing import List, Optional, Union, Final, Callable, TypeAlias
from safety import is_protected_path

# Configuración de logger para el módulo
logger = logging.getLogger(__name__)

@dataclass
class Suspicion:
    """
    Representa un hallazgo sospechoso detectado durante el escaneo.

    Attributes:
        path: Ruta completa del archivo analizado.
        reason: Descripción breve del motivo de sospecha.
        severity: Nivel de criticidad ('info', 'warning', 'critical').
    """
    path: Path
    reason: str
    severity: str

# Alias para funciones de chequeo heurístico.
# La firma espera la ruta, un objeto DirEntry opcional y el timestamp actual de la corrida.
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]

# Lista acumulativa de hallazgos durante el proceso de escaneo.
ScanResult: TypeAlias = List[Suspicion]

# Expresiones regulares para detección de ofuscación
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
# Validación de nombres de archivos terminados en espacios o puntos (vulnerabilidad de Windows)
INVALID_TRAILING_CHARS_RE: Final[re.Pattern] = re.compile(r"[\. ]$")

# Conjuntos de constantes para comparación rápida
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SUSPICIOUS_CONTENT_EXT: Final[frozenset[str]] = frozenset({".pdf"})
SUSPICIOUS_ALL_EXTS: Final[frozenset[str]] = SUSPICIOUS_EXECUTABLE_EXT.union(SUSPICIOUS_CONTENT_EXT)
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
WATCHED_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})

# Configuración de umbrales
SYSTEM32_LOWER: Final[str] = "system32"
RECENT_FILE_THRESHOLD_HOURS: Final[int] = 24
MAX_PATH_LENGTH: Final[int] = 260
# Constante de Windows para FILE_ATTRIBUTE_REPARSE_POINT (0x400)
WIN_FILE_ATTR_REPARSE_POINT: Final[int] = 0x400

# Registro formal de reglas heurísticas para ejecutables
EXECUTABLE_CHECK_REGISTRY: Final[List[SuspicionCheck]] = [
    lambda p, e, t: check_system_lookalike(p, e, t),
    lambda p, e, t: check_recent_executable_in_downloads(p, e, t),
    lambda p, e, t: check_empty_file(p, e, t)
]

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Analiza si el nombre del archivo contiene una extensión legítima seguida de una ejecutable.
    """
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Verifica si un ejecutable fue creado recientemente en carpetas temporales o de descargas.
    """
    if not path or not path.parent or path.parent.name.lower() not in WATCHED_FOLDERS:
        return None
    
    try:
        if entry and entry.is_file(follow_symlinks=False):
            stats = entry.stat()
            if (now_ts - stats.st_mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
                return Suspicion(path, f"Ejecutable reciente detectado (<{RECENT_FILE_THRESHOLD_HOURS}h)", "info")
    except (OSError, AttributeError, PermissionError):
        pass
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Busca archivos cuyos nombres coinciden con binarios críticos del sistema fuera de System32.
    """
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        path_str = str(path).lower()
        if SYSTEM32_LOWER not in path_str:
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Detecta ejecutables con tamaño 0.
    """
    try:
        if entry and entry.is_file(follow_symlinks=False):
            if entry.stat().st_size == 0:
                return Suspicion(path, "Archivo ejecutable vacío sospechoso", "warning")
    except (OSError, PermissionError, AttributeError):
        pass
    return None

class Scanner:
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root: Path = base_root.resolve()
        self.base_root_str: str = str(self.base_root).lower() + os.sep
        self.now_ts: float = datetime.now().timestamp()
        self._registry = EXECUTABLE_CHECK_REGISTRY

    def _is_inside_base_root(self, entry_path: str) -> bool:
        try:
            full_path = Path(entry_path).resolve()
            return str(full_path).lower().startswith(self.base_root_str)
        except OSError:
            return False

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        if not entry or not entry.name or not entry.path or len(entry.path) > MAX_PATH_LENGTH:
            return False
        if entry.path.startswith(("\\\\", "//")):
            return False
        if INVALID_TRAILING_CHARS_RE.search(entry.name) or RTL_CHAR_RE.search(entry.path) or RESERVED_NAMES_RE.match(entry.name):
            return False
        try:
            return not (entry.is_symlink() or not self._is_inside_base_root(entry.path) or is_protected_path(Path(entry.path)))
        except (OSError, PermissionError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & WIN_FILE_ATTR_REPARSE_POINT)
        except (OSError, AttributeError, PermissionError):
            return True 

    def _handle_directory(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        if entry.path and entry.path.lower() not in self.seen:
            self.seen.add(entry.path.lower())
            directory_stack.append(entry.path)

    def process_entry(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        if not self._is_safe_entry(entry):
            return
        try:
            if entry.is_dir(follow_symlinks=False):
                if not self._is_reparse_point(entry):
                    self._handle_directory(entry, directory_stack)
            elif entry.is_file(follow_symlinks=False):
                ext_low = os.path.splitext(entry.name)[1].lower()
                if ext_low in SUSPICIOUS_ALL_EXTS:
                    self._run_file_heuristics(Path(entry.path), entry, ext_low)
        except (OSError, PermissionError):
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry, ext: str) -> None:
        if (double_ext := check_double_extension(path, entry, self.now_ts)):
            self.results.append(double_ext)
        if ext in SUSPICIOUS_EXECUTABLE_EXT:
            for check_fn in self._registry:
                if (result := check_fn(path, entry, self.now_ts)):
                    self.results.append(result)

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None, ext: Optional[str] = None) -> ScanResult:
    if not path: return []
    findings: ScanResult = []
    if (double_ext := check_double_extension(path, entry, now_ts)):
        findings.append(double_ext)
    if ext and ext.lower() in SUSPICIOUS_EXECUTABLE_EXT:
        for check_fn in EXECUTABLE_CHECK_REGISTRY:
            if (result := check_fn(path, entry, now_ts)):
                findings.append(result)
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    if directory is None: return []
    try:
        path_str: str = str(directory).strip()
        if not path_str or len(path_str) > MAX_PATH_LENGTH or path_str.startswith(("\\\\", "//")) or RTL_CHAR_RE.search(path_str): 
            return []
        base_path: Path = Path(path_str)
        if not base_path.exists() or not base_path.is_dir(): 
            return []
        root_input: Path = base_path.resolve()
        if is_protected_path(root_input): 
            return []
        scanner = Scanner(base_root=root_input)
        directory_stack: List[str] = [str(root_input)]
        scanner.seen.add(str(root_input).lower())
        while directory_stack:
            current_dir = directory_stack.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        scanner.process_entry(entry, directory_stack)
            except (PermissionError, OSError):
                continue
        return scanner.results
    except Exception:
        return []

def run_windows_defender_quick_scan() -> str:
    try:
        status = subprocess.run(
            ["powershell", "-Command", "Get-MpComputerStatus | Select-Object -ExpandProperty RealTimeProtectionEnabled"],
            capture_output=True, text=True, timeout=10
        )
        if status.stdout and status.stdout.strip() != "True":
            return "Protección en tiempo real desactivada. Escaneo omitido."
        result = subprocess.run(
            ["powershell", "-Command", "Start-MpScan -ScanType QuickScan"],
            capture_output=True, text=True, timeout=1800,
            check=True
        )
        return result.stdout or result.stderr
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, subprocess.TimeoutExpired) as e:
        return f"Error ejecutando Windows Defender: {str(e)}"
