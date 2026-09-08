"""
scanner.py
Detector HEURÍSTICO de archivos sospechosos. Este módulo realiza un análisis 
estático mediante heurísticas de nombre, extensión y metadatos de archivo, 
complementando la protección de Windows Defender.
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
# La firma espera la ruta, un objeto DirEntry opcional (para rendimiento) y el timestamp actual.
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]

# Lista acumulativa de hallazgos durante el proceso de escaneo.
ScanResult: TypeAlias = List[Suspicion]

# Expresiones regulares para detección de ofuscación
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)

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

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Valida si el nombre del archivo termina con una extensión 'inocente' seguida de una ejecutable."""
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Verifica si un ejecutable fue creado recientemente en carpetas sensibles (ej. Descargas)."""
    if path.parent.name.lower() not in WATCHED_FOLDERS:
        return None
    
    try:
        stats = entry.stat(follow_symlinks=False) if entry else path.stat()
        if (now_ts - stats.st_mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
            return Suspicion(path, f"Ejecutable reciente detectado (<{RECENT_FILE_THRESHOLD_HOURS}h)", "info")
    except (OSError, AttributeError, ValueError, PermissionError, FileNotFoundError):
        pass
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Identifica ejecutables con nombres de servicios críticos que se encuentran fuera de system32."""
    if path.name.lower() in SYSTEM_LOOKALIKES:
        path_str = str(path).lower()
        if not is_protected_path(path) and SYSTEM32_LOWER not in path_str:
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None

# Registro formal de reglas heurísticas para ejecutables
EXECUTABLE_CHECK_REGISTRY: Final[List[SuspicionCheck]] = [
    check_system_lookalike,
    check_recent_executable_in_downloads
]

class Scanner:
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root: Path = base_root.resolve(strict=False)
        self.base_root_str: str = str(self.base_root).lower() + os.sep
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, entry_path: str) -> bool:
        return entry_path.lower().startswith(self.base_root_str)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        try:
            path_str: str = entry.path
            if not path_str or not entry.name or len(path_str) > MAX_PATH_LENGTH or path_str.startswith(("\\\\", "//")):
                return False
            
            if RTL_CHAR_RE.search(entry.name) or RESERVED_NAMES_RE.match(entry.name):
                return False
            
            if not self._is_inside_base_root(path_str):
                return False
            
            return not is_protected_path(Path(path_str))
        except (OSError, AttributeError, TypeError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        try:
            if entry.is_symlink():
                return True
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & WIN_FILE_ATTR_REPARSE_POINT)
        except (OSError, AttributeError, TypeError, FileNotFoundError, PermissionError):
            return True 

    def _handle_directory(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        if entry.path and entry.path not in self.seen:
            self.seen.add(entry.path)
            directory_stack.append(entry.path)

    def process_entry(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        if not self._is_safe_entry(entry):
            return
        
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            
            if is_dir:
                if not self._is_reparse_point(entry):
                    self._handle_directory(entry, directory_stack)
                return

            name = entry.name
            ext_idx = name.rfind('.')
            if ext_idx != -1:
                ext_low = name[ext_idx:].lower()
                if ext_low in SUSPICIOUS_ALL_EXTS:
                    self._run_file_heuristics(Path(entry.path), entry, ext_low)
        except (OSError, PermissionError, FileNotFoundError):
            return

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry, ext: str) -> None:
        self.results.extend(scan_file(path, self.now_ts, entry=entry, ext=ext))

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None, ext: Optional[str] = None) -> ScanResult:
    if is_protected_path(path):
        return []
    findings: ScanResult = []
    if (double_ext := check_double_extension(path, entry, now_ts)):
        findings.append(double_ext)
    
    if (ext or path.suffix.lower()) in SUSPICIOUS_EXECUTABLE_EXT:
        try:
            stats = entry.stat(follow_symlinks=False) if entry else path.stat()
            if stats.st_size == 0:
                findings.append(Suspicion(path, "Archivo vacío sospechoso", "warning"))
        except (OSError, PermissionError, AttributeError, FileNotFoundError):
            pass

        for check_fn in EXECUTABLE_CHECK_REGISTRY:
            if (result := check_fn(path, entry, now_ts)):
                findings.append(result)
        
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    if directory is None or (isinstance(directory, str) and not directory.strip()):
        return []
    
    try:
        base_path = Path(directory)
        if not base_path.exists() or not base_path.is_dir(): 
            return []
        
        root_input = base_path.resolve(strict=False)
        if str(root_input).startswith(("\\\\", "//")) or is_protected_path(root_input):
            return []
    except (OSError, TypeError, ValueError, RuntimeError, PermissionError):
        return []

    scanner = Scanner(base_root=root_input)
    directory_stack: List[str] = [str(root_input)]
    scanner.seen.add(str(root_input))
    
    while directory_stack:
        current_dir = directory_stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    scanner.process_entry(entry, directory_stack)
        except (PermissionError, OSError, FileNotFoundError):
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """Invoca PowerShell para ejecutar un escaneo rápido del sistema mediante Defender."""
    try:
        status = subprocess.run(
            ["powershell", "-Command", "Get-MpComputerStatus | Select-Object -ExpandProperty RealTimeProtectionEnabled"],
            capture_output=True, text=True, timeout=10
        )
        if status.stdout.strip() != "True":
            return "Protección en tiempo real desactivada. Escaneo omitido."
        result = subprocess.run(
            ["powershell", "-Command", "Start-MpScan -ScanType QuickScan"],
            capture_output=True, text=True, timeout=1800,
            check=True
        )
        return result.stdout or result.stderr
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, subprocess.TimeoutExpired) as e:
        return f"Error ejecutando Windows Defender: {str(e)}"
