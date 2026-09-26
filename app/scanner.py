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
    """Configuración de umbrales para la validación de integridad durante el escaneo."""
    max_path: int = 260
    recent_hours: int = 24
    reparse_point_attr_mask: int = 0x400

LIMITS: Final = ScannerLimits()

@dataclass
class Suspicion:
    """
    Representa un hallazgo de riesgo potencial tras una inspección heurística.

    Attributes:
        path: Ruta absoluta al archivo analizado.
        reason: Justificación técnica del hallazgo (ej. ofuscación, comportamiento).
        severity: Nivel de urgencia ('info' para monitoreo, 'warning' para atención requerida).
    """
    path: Path
    reason: str
    severity: str

# Definición de tipos para el sistema de heurísticas
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]
ScanResult: TypeAlias = List[Suspicion]

# Expresiones regulares para detección de ofuscación y estructuras no estándar
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
INVALID_TRAILING_CHARS_RE: Final[re.Pattern] = re.compile(r"[\. ]$")
UNC_PATH_RE: Final[re.Pattern] = re.compile(r"^\\\\[^\\\\]+\\")

# Conjuntos de constantes para comparación rápida de extensiones y rutas
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SUSPICIOUS_CONTENT_EXT: Final[frozenset[str]] = frozenset({".pdf"})
SUSPICIOUS_ALL_EXTS: Final[frozenset[str]] = SUSPICIOUS_EXECUTABLE_EXT.union(SUSPICIOUS_CONTENT_EXT)
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
WATCHED_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})

SYSTEM32_LOWER: Final[str] = "system32"

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """
    Ejecuta os.stat sin seguir symlinks para prevenir escape del sandbox.
    
    Returns:
        os.stat_result si es accesible, None en caso de error de permisos o acceso.
    """
    if entry is None:
        return None
    try:
        return entry.stat(follow_symlinks=False)
    except (OSError, PermissionError, AttributeError):
        return None

def _get_file_attributes(entry: os.DirEntry) -> int:
    """
    Extrae la máscara de atributos del sistema de archivos.
    
    Returns:
        Bitmask de atributos o 0 si no es posible determinar atributos.
    """
    stats = _safe_stat(entry)
    if stats and hasattr(stats, 'st_file_attributes'):
        return int(stats.st_file_attributes)
    return 0

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """
    Valida la integridad de la ruta contra límites de Windows y caracteres maliciosos.
    
    Returns:
        True si la ruta es apta para procesamiento; False si es sospechosa o inválida.
    """
    if not path_str or len(path_str) > LIMITS.max_path:
        return False
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Analiza si el nombre del archivo contiene una doble extensión (ej: documento.pdf.exe)."""
    if path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Verifica si un ejecutable fue creado recientemente en carpetas de alta exposición."""
    if not path.parent or path.parent.name.lower() not in WATCHED_FOLDERS:
        return None
    stats = _safe_stat(entry) if entry else None
    if stats:
        try:
            mtime = stats.st_mtime
            if 0 < mtime <= now_ts and (now_ts - mtime) < (LIMITS.recent_hours * 3600):
                return Suspicion(path, f"Ejecutable reciente detectado (<{LIMITS.recent_hours}h)", "info")
        except (AttributeError, TypeError):
            return None
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Compara el nombre del archivo contra una lista negra de procesos críticos de sistema."""
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        try:
            path_str = str(path).lower()
            if SYSTEM32_LOWER not in path_str:
                return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
        except Exception:
            return None
    return None

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Identifica ejecutables cuyo tamaño en disco es 0 bytes (posible placeholder malicioso)."""
    stats = _safe_stat(entry) if entry else None
    if stats is not None:
        try:
            if stats.st_size == 0:
                return Suspicion(path, "Archivo ejecutable vacío sospechoso", "warning")
        except (AttributeError, TypeError):
            return None
    return None

# Registro centralizado de reglas heurísticas
ALL_CHECKS: Final[List[SuspicionCheck]] = [
    check_double_extension,
    check_system_lookalike,
    check_recent_executable_in_downloads,
    check_empty_file
]

class Scanner:
    """
    Coordinador de escaneo recursivo basado en pila (LIFO).
    
    Implementa un mecanismo de "seen" para evitar ciclos en sistemas con 
    jerarquías complejas y asegura que todo acceso sea validado vía _is_safe_entry.
    """
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.protected_cache: set[str] = set()
        self.base_root: Path = base_root.resolve()
        self.base_root_str: str = str(self.base_root).lower()
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica el alcance (scope) del escaneo mediante string prefix matching."""
        return entry_path.lower().startswith(self.base_root_str)

    def _has_invalid_name(self, name: str) -> bool:
        """Determina si un nombre de archivo contiene caracteres prohibidos o reservados."""
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Detecta si una entrada es un punto de reanálisis mediante atributos Win32."""
        attributes = _get_file_attributes(entry)
        return bool(attributes & LIMITS.reparse_point_attr_mask)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """
        Valida que la entrada sea segura para procesar.
        """
        if not entry or not entry.path or not entry.name:
            return False
        if not _is_valid_path_structure(entry.path) or self._has_invalid_name(entry.name):
            return False
        if not self._is_inside_base_root(entry.path.lower()):
            return False
        
        path_obj = Path(entry.path)
        parent_str = str(path_obj.parent).lower()
        
        # Cache de protección para evitar resolución costosa de path.resolve() en cada archivo
        if parent_str not in self.protected_cache:
            if is_protected_path(path_obj.resolve()):
                return False
            self.protected_cache.add(parent_str)
            
        return not (self._is_reparse_point(entry) or entry.is_symlink())

    def _handle_directory(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        """Gestiona el descenso recursivo de directorios."""
        if entry.path and entry.path.lower() not in self.seen:
            self.seen.add(entry.path.lower())
            directory_stack.append(entry.path)

    def _is_relevant_extension(self, name: str) -> bool:
        """Filtra extensiones que no requieren inspección heurística."""
        _, ext = os.path.splitext(name)
        return ext.lower() in SUSPICIOUS_ALL_EXTS

    def process_entry(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        """
        Lógica de decisión para cada entrada encontrada por os.scandir.
        """
        try:
            if not entry.exists():
                return
            if entry.is_dir(follow_symlinks=False):
                if self._is_safe_entry(entry):
                    self._handle_directory(entry, directory_stack)
            elif entry.is_file(follow_symlinks=False):
                if self._is_relevant_extension(entry.name) and self._is_safe_entry(entry):
                    self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError):
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Itera todas las funciones de heurística y acumula hallazgos sin detener el escaneo."""
        for check_fn in ALL_CHECKS:
            try:
                res = check_fn(path, entry, self.now_ts)
                if isinstance(res, Suspicion):
                    self.results.append(res)
            except (Exception) as e:
                logger.debug(f"Error en heurística {check_fn.__name__} para {path}: {e}")
                continue

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Escanea un archivo puntual contra todas las heurísticas definidas."""
    if not isinstance(path, Path): return []
    try:
        if is_protected_path(path.resolve()): return []
        if not path.is_file(): return []
    except (OSError, PermissionError): return []
    
    findings: ScanResult = []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts)
            if isinstance(res, Suspicion):
                findings.append(res)
        except (Exception):
            continue
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """
    Punto de entrada para escaneo recursivo de directorios.
    """
    if directory is None: return []
    path_str = str(directory).strip()
    if not path_str or not _is_valid_path_structure(path_str): return []
    
    try:
        base_path = Path(path_str).resolve()
        if not base_path.exists() or not base_path.is_dir() or base_path.is_symlink():
            return []
        if is_protected_path(base_path): return []
    except (OSError, RuntimeError): 
        return []
    
    scanner = Scanner(base_root=base_path)
    directory_stack: List[str] = [str(base_path)]
    scanner.seen.add(str(base_path).lower())
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
    """Invoca PowerShell para ejecutar un Quick Scan de Windows Defender."""
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
