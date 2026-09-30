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
# SuspicionCheck: (Ruta, Entrada opcional de dir, Timestamp actual) -> Objeto de hallazgo o None
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]
ScanResult: TypeAlias = List[Suspicion]

# Expresiones regulares para detección de ofuscación y estructuras no estándar
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
    Obtiene metadatos del archivo usando el descriptor ya abierto por scandir.
    
    Usa follow_symlinks=False para evitar seguir accesos directos o puntos de 
    reanalice que podrían llevar fuera del árbol autorizado.
    """
    if not isinstance(entry, os.DirEntry):
        return None
    try:
        # La máscara de bits evita procesar junctions/reparse points detectados por sistema
        if not entry.is_symlink() and not (_get_file_attributes(entry) & LIMITS.reparse_point_attr_mask):
            return entry.stat(follow_symlinks=False)
        return None
    except (OSError, PermissionError):
        return None

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Extrae la máscara de bits de atributos Win32 desde el stat ya cacheado."""
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError):
        return 0

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida la integridad de la cadena de ruta según estándares de Windows."""
    if not path_str or len(path_str) > LIMITS.max_path:
        return False
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Detecta archivos con extensiones dobles que intentan disfrazar el tipo real."""
    if path is None or path.name is None:
        return None
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Identifica ejecutables creados recientemente en carpetas monitoreadas."""
    if path is None or path.parent is None or path.parent.name.lower() not in WATCHED_FOLDERS:
        return None
    stats = _safe_stat(entry) if entry else None
    if stats:
        try:
            mtime = float(getattr(stats, "st_mtime", 0.0))
            if 0 < mtime <= now_ts and (now_ts - mtime) < (LIMITS.recent_hours * 3600):
                return Suspicion(path, f"Ejecutable reciente detectado (<{LIMITS.recent_hours}h)", "info")
        except (AttributeError, TypeError, ValueError):
            return None
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Detecta nombres de procesos críticos del sistema ubicados fuera de System32."""
    if path is not None and path.name is not None and path.name.lower() in SYSTEM_LOOKALIKES:
        try:
            path_str = str(path).lower()
            if SYSTEM32_LOWER not in path_str:
                return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
        except Exception:
            return None
    return None

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Detecta ejecutables de 0 bytes, usados a veces para señuelos."""
    stats = _safe_stat(entry) if entry else None
    if stats is not None:
        try:
            if int(getattr(stats, "st_size", -1)) == 0:
                return Suspicion(path, "Archivo ejecutable vacío sospechoso", "warning")
        except (AttributeError, TypeError, ValueError):
            return None
    return None

ALL_CHECKS: Final[List[SuspicionCheck]] = [
    check_double_extension,
    check_system_lookalike,
    check_recent_executable_in_downloads,
    check_empty_file
]

class Scanner:
    """Motor recursivo de escaneo para recorrer el sistema de archivos de forma segura."""
    
    def __init__(self, base_root: Path) -> None:
        self.results: List[Suspicion] = []
        self.seen: set[str] = set()
        self.protected_cache: set[str] = set()
        self.base_root: Path = base_root.resolve()
        self.base_root_str: str = str(self.base_root).lower()
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica recursivamente si la entrada está contenida en el árbol base."""
        try:
            abs_path = Path(entry_path).resolve()
            return str(abs_path).lower().startswith(self.base_root_str)
        except (OSError, RuntimeError):
            return False

    def _has_invalid_name(self, name: str) -> bool:
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        return bool(_get_file_attributes(entry) & LIMITS.reparse_point_attr_mask)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """
        Realiza chequeo de seguridad antes de procesar una ruta.
        
        Usa cache de padres (`protected_cache`) para minimizar llamadas repetitivas
        al módulo de seguridad externo `safety.py`.
        """
        if not entry or not entry.path or not entry.name:
            return False
        if not _is_valid_path_structure(entry.path) or self._has_invalid_name(entry.name):
            return False
        if not self._is_inside_base_root(entry.path):
            return False
        
        try:
            if self._is_reparse_point(entry) or entry.is_symlink():
                return False
            if not os.access(entry.path, os.R_OK):
                return False
            
            resolved_path = Path(entry.path).resolve()
            parent_dir = str(resolved_path.parent)
            if parent_dir not in self.protected_cache:
                if is_protected_path(resolved_path.parent):
                    return False
                self.protected_cache.add(parent_dir)
            
            return not is_protected_path(resolved_path)
        except (OSError, RuntimeError, FileNotFoundError):
            return False

    def _handle_directory(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        if entry.path and entry.path.lower() not in self.seen:
            self.seen.add(entry.path.lower())
            directory_stack.append(entry.path)

    def _is_relevant_extension(self, name: str) -> bool:
        _, ext = os.path.splitext(name)
        return ext.lower() in SUSPICIOUS_ALL_EXTS

    def process_entry(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        """Procesa una entrada del sistema; delega a heurísticas si es archivo."""
        try:
            if not self._is_safe_entry(entry):
                return
            if entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, directory_stack)
            elif entry.is_file(follow_symlinks=False):
                if self._is_relevant_extension(entry.name):
                    self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError):
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        for check_fn in ALL_CHECKS:
            try:
                finding = check_fn(path, entry, self.now_ts)
                if finding is not None:
                    self.results.append(finding)
            except Exception as e:
                logger.debug(f"Error en heurística {check_fn.__name__} para {path}: {e}")

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> List[Suspicion]:
    """Análisis puntual de un archivo único; omite recursión."""
    if not isinstance(path, Path): return []
    try:
        resolved = path.resolve()
        if not resolved.is_file() or not os.access(resolved, os.R_OK): return []
        if is_protected_path(resolved): return []
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
    """Recorrido iterativo de un árbol de directorios usando una pila manual."""
    if directory is None: return []
    path_str = str(directory).strip()
    if not path_str or not _is_valid_path_structure(path_str): return []
    
    try:
        base_path = Path(path_str).resolve()
        if not base_path.is_dir() or base_path.is_symlink() or not os.access(base_path, os.R_OK):
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
                    if entry is not None:
                        scanner.process_entry(entry, directory_stack)
        except (PermissionError, OSError):
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
