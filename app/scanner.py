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
    Obtiene metadatos del archivo sin seguir enlaces simbólicos (evita escapes del sandbox).
    
    Args:
        entry: Entrada del sistema de archivos a evaluar.
    Returns:
        os.stat_result si el archivo es local y accesible, None si es reparse point o inaccesible.
    """
    if entry is None or (entry.is_symlink() or _get_file_attributes(entry) & LIMITS.reparse_point_attr_mask):
        return None
    try:
        return entry.stat(follow_symlinks=False)
    except (OSError, PermissionError, AttributeError):
        return None

def _get_file_attributes(entry: os.DirEntry) -> int:
    """
    Extrae la máscara de bits de atributos (Windows File Attributes) del archivo.
    """
    try:
        return entry.stat(follow_symlinks=False).st_file_attributes # type: ignore
    except (AttributeError, OSError):
        return 0

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """
    Valida que la cadena de ruta cumpla con estándares de seguridad y longitud de Windows.
    """
    if not path_str or len(path_str) > LIMITS.max_path:
        return False
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Heurística: Detecta cuando un ejecutable intenta ocultarse tras una extensión benigna."""
    if path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Heurística: Alerta sobre ejecutables descargados recientemente en directorios de riesgo."""
    if not path or not path.parent or path.parent.name.lower() not in WATCHED_FOLDERS:
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
    """Heurística: Detecta procesos comunes del sistema ejecutándose fuera de System32."""
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        try:
            path_str = str(path).lower()
            if SYSTEM32_LOWER not in path_str:
                return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
        except Exception:
            return None
    return None

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Heurística: Identifica archivos ejecutables con tamaño 0 (posibles flags o placeholders)."""
    stats = _safe_stat(entry) if entry else None
    if stats is not None:
        try:
            if stats.st_size == 0:
                return Suspicion(path, "Archivo ejecutable vacío sospechoso", "warning")
        except (AttributeError, TypeError):
            return None
    return None

ALL_CHECKS: Final[List[SuspicionCheck]] = [
    check_double_extension,
    check_system_lookalike,
    check_recent_executable_in_downloads,
    check_empty_file
]

class Scanner:
    """
    Coordinador de escaneo recursivo basado en pila para recorrer el sistema de archivos.
    Mantiene el estado de rutas visitadas y resultados encontrados durante el ciclo.
    """
    def __init__(self, base_root: Path) -> None:
        self.results: List[Suspicion] = []
        self.seen: set[str] = set()
        self.protected_cache: set[str] = set()
        self.base_root: Path = base_root.resolve()
        self.base_root_str: str = str(self.base_root).lower()
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica que la entrada pertenezca al árbol de directorios raíz definido."""
        try:
            abs_path = Path(entry_path).resolve()
            return str(abs_path).lower().startswith(self.base_root_str)
        except OSError:
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Valida nombres reservados del sistema operativo o caracteres finales no permitidos."""
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si un directorio es una unión o symlink mediante atributos de sistema."""
        return bool(_get_file_attributes(entry) & LIMITS.reparse_point_attr_mask)

    def _is_safe_entry(self, entry: os.DirEntry, is_dir: bool = False) -> bool:
        """
        Realiza una validación de seguridad de la entrada con caché de directorios protegidos.
        Retorna True si la entrada puede ser analizada o descendida.
        """
        if not entry or not entry.path or not entry.name:
            return False
        if not _is_valid_path_structure(entry.path) or self._has_invalid_name(entry.name):
            return False
        if not self._is_inside_base_root(entry.path):
            return False
        if self._is_reparse_point(entry) or entry.is_symlink():
            return False
        if not is_dir and not entry.is_file():
            return False
        
        try:
            if not os.access(entry.path, os.R_OK):
                return False
            
            parent_dir = os.path.dirname(entry.path)
            if parent_dir not in self.protected_cache:
                if is_protected_path(Path(parent_dir)):
                    return False
                self.protected_cache.add(parent_dir)
        except (OSError, RuntimeError):
            return False
            
        return True

    def _handle_directory(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        """Registra una carpeta válida en la pila de procesamiento si no ha sido visitada."""
        if entry.path and entry.path.lower() not in self.seen:
            self.seen.add(entry.path.lower())
            directory_stack.append(entry.path)

    def _is_relevant_extension(self, name: str) -> bool:
        """Filtra archivos de interés basándose en las extensiones heurísticas definidas."""
        _, ext = os.path.splitext(name)
        return ext.lower() in SUSPICIOUS_ALL_EXTS

    def process_entry(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        """
        Procesa una entrada detectada durante el escaneo: si es carpeta, la apila;
        si es archivo relevante, aplica heurísticas.
        """
        try:
            if not entry.exists():
                return
            if entry.is_dir(follow_symlinks=False):
                if self._is_safe_entry(entry, is_dir=True):
                    self._handle_directory(entry, directory_stack)
            elif entry.is_file(follow_symlinks=False):
                if self._is_relevant_extension(entry.name) and self._is_safe_entry(entry):
                    self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError):
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta toda la suite de heurísticas sobre el archivo especificado."""
        if not path.exists():
            return
        for check_fn in ALL_CHECKS:
            try:
                finding = check_fn(path, entry, self.now_ts)
                if finding is not None:
                    self.results.append(finding)
            except Exception as e:
                logger.debug(f"Error en heurística {check_fn.__name__} para {path}: {e}")

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> List[Suspicion]:
    """Escaneo puntual de un archivo individual contra todas las reglas heurísticas."""
    if not isinstance(path, Path): return []
    try:
        if not path.exists() or not os.access(path, os.R_OK): return []
        if is_protected_path(path.resolve()): return []
        if not path.is_file(): return []
    except (OSError, PermissionError): return []
    
    findings: List[Suspicion] = []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts)
            if res is not None:
                findings.append(res)
        except Exception:
            continue
    return findings

def scan_directory(directory: Union[str, Path, None]) -> List[Suspicion]:
    """Coordina el recorrido recursivo (basado en pila) del árbol de directorios para escaneo."""
    if directory is None: return []
    path_str = str(directory).strip()
    if not path_str or not _is_valid_path_structure(path_str): return []
    
    try:
        base_path = Path(path_str).resolve()
        if not base_path.exists() or not base_path.is_dir() or base_path.is_symlink():
            return []
        if not os.access(base_path, os.R_OK): return []
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
    """Invoca PowerShell para ejecutar un escaneo rápido del sistema vía Defender."""
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
