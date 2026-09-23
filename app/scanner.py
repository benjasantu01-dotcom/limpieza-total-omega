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
from typing import List, Optional, Union, Final, Callable, TypeAlias, NamedTuple
from safety import is_protected_path

# Configuración de logger para el módulo
logger: Final = logging.getLogger(__name__)

class ScannerLimits(NamedTuple):
    """Límites definidos para la validación de rutas y heurísticas temporales."""
    max_path: int = 260
    recent_hours: int = 24
    reparse_attr: int = 0x400

LIMITS: Final = ScannerLimits()

@dataclass
class Suspicion:
    """
    Representa un hallazgo sospechoso detectado durante el escaneo.

    Attributes:
        path: Objeto Path con la ruta completa del archivo analizado.
        reason: Descripción breve del motivo de sospecha.
        severity: Nivel de criticidad ('info', 'warning', 'critical').
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
WATCHED_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})

SYSTEM32_LOWER: Final[str] = "system32"

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """
    Intenta obtener metadatos sin seguir enlaces simbólicos mediante la API de bajo nivel.
    Retorna None si el archivo es inaccesible o no existe (archivo bloqueado/borrado).
    """
    if entry is None:
        return None
    try:
        return entry.stat(follow_symlinks=False)
    except (OSError, PermissionError, AttributeError, FileNotFoundError):
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Verifica si la cadena de ruta cumple con los límites de longitud y caracteres prohibidos de Windows."""
    if not path_str or len(path_str) > LIMITS.max_path:
        return False
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

# Registro centralizado de reglas heurísticas para archivos ejecutables
# Cada función debe aceptar: path (Path), entry (DirEntry|None), now_ts (float)
EXECUTABLE_CHECK_REGISTRY: Final[List[SuspicionCheck]] = [
    lambda p, e, t: check_system_lookalike(p, e, t),
    lambda p, e, t: check_recent_executable_in_downloads(p, e, t),
    lambda p, e, t: check_empty_file(p, e, t)
]

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Verifica si el nombre de archivo contiene múltiples extensiones sospechosas (e.g., .pdf.exe).
    Esta técnica es común en ataques de suplantación de tipo de archivo.
    """
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Analiza la fecha de modificación del archivo para alertar sobre ejecutables 
    nuevos (creados en las últimas 'recent_hours') ubicados en directorios temporales o de descarga.
    """
    if not path or not path.parent or path.parent.name.lower() not in WATCHED_FOLDERS:
        return None
    
    if entry:
        stats = _safe_stat(entry)
        if stats and hasattr(stats, 'st_mtime') and (now_ts - stats.st_mtime) < (LIMITS.recent_hours * 3600):
            return Suspicion(path, f"Ejecutable reciente detectado (<{LIMITS.recent_hours}h)", "info")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Detecta si un ejecutable tiene un nombre coincidente con procesos críticos del sistema 
    (ej: svchost.exe) pero se encuentra fuera de la ruta protegida System32.
    """
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        path_str = str(path).lower()
        if SYSTEM32_LOWER not in path_str:
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Evalúa si un archivo ejecutable tiene un tamaño de 0 bytes, comportamiento anómalo 
    utilizado a menudo para evadir el análisis de contenido de antivirus tradicionales.
    """
    if entry:
        stats = _safe_stat(entry)
        if stats and hasattr(stats, 'st_size') and stats.st_size == 0:
            return Suspicion(path, "Archivo ejecutable vacío sospechoso", "warning")
    return None

class Scanner:
    """
    Coordinador de escaneo recursivo. Utiliza un stack LIFO para procesar el sistema 
    de archivos, implementando validaciones de seguridad exhaustivas para ignorar 
    rutas protegidas, enlaces simbólicos y puntos de reanálisis.
    """
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root: Path = base_root.resolve()
        self.now_ts: float = datetime.now().timestamp()
        self._registry: List[SuspicionCheck] = EXECUTABLE_CHECK_REGISTRY

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Valida si una ruta absoluta pertenece jerárquicamente a la base escaneada."""
        try:
            return Path(entry_path).resolve().is_relative_to(self.base_root)
        except (ValueError, RuntimeError):
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Determina si el nombre de archivo contiene caracteres prohibidos o reservados por Windows."""
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """
        Detecta si la entrada actual es un punto de reanálisis (Junction o Symlink) 
        mediante inspección de atributos de archivo para evitar recursión circular.
        """
        if entry is None:
            return True
        try:
            stats = _safe_stat(entry)
            if stats and hasattr(stats, 'st_file_attributes'):
                return bool(stats.st_file_attributes & LIMITS.reparse_attr)
        except (OSError, PermissionError, AttributeError):
            return True 
        return False

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """
        Valida la seguridad de la entrada: verifica integridad de jerarquía, 
        nombres prohibidos, protección de sistema y naturaleza del enlace.
        """
        if not entry or not entry.path or not entry.name:
            return False
        if not _is_valid_path_structure(entry.path) or self._has_invalid_name(entry.name):
            return False
        if not self._is_inside_base_root(entry.path):
            return False
        if self._is_reparse_point(entry) or is_protected_path(Path(entry.path)):
            return False
        try:
            return not entry.is_symlink()
        except (OSError, PermissionError, AttributeError):
            return False

    def _handle_directory(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        """Agrega un directorio verificado a la pila de procesamiento LIFO."""
        try:
            if entry.path and entry.path.lower() not in self.seen:
                self.seen.add(entry.path.lower())
                directory_stack.append(entry.path)
        except (OSError, AttributeError):
            pass

    def _is_relevant_extension(self, name: Optional[str], is_dir: bool) -> Optional[str]:
        """Filtra archivos por extensiones definidas en el conjunto de sospecha global."""
        if is_dir or not name or "." not in name: 
            return None
        
        parts = name.rsplit(".", 1)
        if len(parts) != 2:
            return None
            
        ext_low = ("." + parts[1]).lower()
        return ext_low if ext_low in SUSPICIOUS_ALL_EXTS else None

    def process_entry(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        """
        Lógica principal de procesamiento: clasifica la entrada y delega el 
        análisis heurístico si el archivo es considerado sospechoso.
        """
        try:
            if not self._is_safe_entry(entry):
                return
            
            is_dir = entry.is_dir(follow_symlinks=False)
            ext_low = self._is_relevant_extension(entry.name, is_dir)
            
            if is_dir:
                self._handle_directory(entry, directory_stack)
            elif ext_low:
                self._run_file_heuristics(Path(entry.path), entry, ext_low)
        except (OSError, PermissionError, AttributeError):
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry, ext: str) -> None:
        """Aplica la batería de tests registrados sobre un archivo específico."""
        if not path or not path.exists():
            return
            
        if (double_ext := check_double_extension(path, entry, self.now_ts)):
            self.results.append(double_ext)
        if ext in SUSPICIOUS_EXECUTABLE_EXT:
            for check_fn in self._registry:
                try:
                    if (result := check_fn(path, entry, self.now_ts)):
                        self.results.append(result)
                except Exception as e:
                    logger.debug(f"Error en heurística para {path}: {e}")

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None, ext: Optional[str] = None) -> ScanResult:
    """Escanea un único archivo sin recorrido recursivo."""
    if not path or not path.exists() or is_protected_path(path): 
        return []
    
    findings: ScanResult = []
    if (double_ext := check_double_extension(path, entry, now_ts)):
        findings.append(double_ext)
        
    if ext and ext.lower() in SUSPICIOUS_EXECUTABLE_EXT:
        for check_fn in EXECUTABLE_CHECK_REGISTRY:
            try:
                if (result := check_fn(path, entry, now_ts)):
                    findings.append(result)
            except Exception:
                continue
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Ejecuta el escaneo de directorios utilizando una pila LIFO para control de recursos."""
    if directory is None: return []
    try:
        path_str: str = str(directory).strip()
        if not path_str or not _is_valid_path_structure(path_str):
            return []
        
        base_path = Path(path_str)
        if not base_path.is_absolute() or not base_path.exists() or not base_path.is_dir(): 
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
                        try:
                            scanner.process_entry(entry, directory_stack)
                        except (OSError, PermissionError, AttributeError):
                            continue
            except (PermissionError, OSError, AttributeError):
                continue
        return scanner.results
    except (ValueError, TypeError, RuntimeError) as e:
        logger.error(f"Error al inicializar escaneo en {directory}: {e}")
        return []

def run_windows_defender_quick_scan() -> str:
    """Consulta el estado de Windows Defender y dispara un análisis rápido vía PowerShell."""
    try:
        status = subprocess.run(
            ["powershell", "-Command", "Get-MpComputerStatus | Select-Object -ExpandProperty RealTimeProtectionEnabled"],
            capture_output=True, text=True, timeout=10
        )
        if status.returncode == 0 and status.stdout and status.stdout.strip() != "True":
            return "Protección en tiempo real desactivada. Escaneo omitido."
        
        result = subprocess.run(
            ["powershell", "-Command", "Start-MpScan -ScanType QuickScan"],
            capture_output=True, text=True, timeout=1800,
            check=True
        )
        return str(result.stdout) if result.stdout else "Escaneo iniciado correctamente."
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, subprocess.TimeoutExpired) as e:
        return f"Error ejecutando Windows Defender: {str(e)}"
