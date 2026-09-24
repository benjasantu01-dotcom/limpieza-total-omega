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
    reparse_attr: int = 0x400

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
    Ejecuta un stat sobre el DirEntry sin seguir enlaces (prevención de fugas de contexto).
    Retorna None si la entrada es inaccesible o el enlace es inválido.
    """
    if entry is None:
        return None
    try:
        return entry.stat(follow_symlinks=False)
    except (OSError, PermissionError, AttributeError, FileNotFoundError):
        return None

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida la integridad de una ruta según estándares de Windows antes de su procesamiento."""
    if not path_str or len(path_str) > LIMITS.max_path:
        return False
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Detecta si el nombre del archivo contiene extensiones anidadas sospechosas.
    """
    if path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Evalúa la frescura de un archivo ejecutable en directorios críticos de usuario.
    """
    if not path.parent or path.parent.name.lower() not in WATCHED_FOLDERS:
        return None
    stats = _safe_stat(entry) if entry else None
    if stats and hasattr(stats, 'st_mtime'):
        if (now_ts - stats.st_mtime) < (LIMITS.recent_hours * 3600):
            return Suspicion(path, f"Ejecutable reciente detectado (<{LIMITS.recent_hours}h)", "info")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Detecta ejecutables que intentan suplantar procesos críticos fuera de 'System32'.
    """
    if path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        path_str = str(path).lower()
        if SYSTEM32_LOWER not in path_str:
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Identifica archivos con tamaño 0 bytes que suelen ser marcadores de procesos maliciosos.
    """
    stats = _safe_stat(entry) if entry else None
    if stats and hasattr(stats, 'st_size') and stats.st_size == 0:
        return Suspicion(path, "Archivo ejecutable vacío sospechoso", "warning")
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
    Coordinador de escaneo recursivo basado en pila (LIFO). Gestiona el estado
    del recorrido y delega validaciones de seguridad a los métodos de clase.
    """
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root: Path = base_root.resolve()
        self.base_root_str: str = str(self.base_root).lower()
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Confirma que la ruta se mantiene dentro del alcance definido por el usuario."""
        try:
            p = Path(entry_path).resolve()
            return str(p).lower().startswith(self.base_root_str)
        except (OSError, RuntimeError):
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Detecta nombres de archivo reservados o inválidos según el SO."""
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si un directorio es un punto de reanálisis (Junction) para evitar recursión circular."""
        stats = _safe_stat(entry)
        if stats and hasattr(stats, 'st_file_attributes'):
            return bool(stats.st_file_attributes & LIMITS.reparse_attr)
        return False

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """Valida que la entrada de sistema cumple con todas las políticas de seguridad actuales."""
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
        """Gestiona el registro de directorios visitados para evitar ciclos durante el escaneo."""
        if entry.path and entry.path.lower() not in self.seen:
            self.seen.add(entry.path.lower())
            directory_stack.append(entry.path)

    def _is_relevant_extension(self, name: str) -> bool:
        """Filtra archivos que coinciden con las extensiones bajo monitoreo."""
        return os.path.splitext(name)[1].lower() in SUSPICIOUS_ALL_EXTS

    def process_entry(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        """Procesa una única entrada, delegando el manejo a directorios o ejecutando heurísticas."""
        if not self._is_safe_entry(entry): return
        try:
            if entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, directory_stack)
            elif entry.is_file(follow_symlinks=False) and self._is_relevant_extension(entry.name):
                self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError, AttributeError):
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta el conjunto registrado de heurísticas sobre el archivo detectado."""
        for check_fn in ALL_CHECKS:
            try:
                res = check_fn(path, entry, self.now_ts)
                if isinstance(res, Suspicion):
                    self.results.append(res)
            except Exception as e:
                logger.debug(f"Error silencioso en heurística {check_fn.__name__}: {e}")

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """Escanea un único archivo sin recorrido recursivo, validando previamente su seguridad."""
    if not path or is_protected_path(path): return []
    try:
        if not path.is_file(): return []
    except (OSError, PermissionError): return []
    
    findings: ScanResult = []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts)
            if isinstance(res, Suspicion):
                findings.append(res)
        except Exception as e:
            logger.error(f"Error procesando {path.name} con {check_fn.__name__}: {e}")
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Punto de entrada principal para escaneo recursivo de una jerarquía de directorios."""
    if directory is None: return []
    path_str: str = str(directory).strip()
    if not path_str or not _is_valid_path_structure(path_str): return []
    
    try:
        base_path = Path(path_str).resolve()
        if not base_path.exists() or not base_path.is_dir() or base_path.is_symlink():
            return []
    except (OSError, RuntimeError): 
        return []
    
    if is_protected_path(base_path): return []
    scanner = Scanner(base_root=base_path)
    directory_stack: List[str] = [str(base_path)]
    scanner.seen.add(str(base_path).lower())
    while directory_stack:
        current_dir = directory_stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    scanner.process_entry(entry, directory_stack)
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
