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
    """
    Parámetros operativos críticos para el escaneo.
    
    max_path: Límite de longitud de ruta según estándar Windows MAX_PATH.
    recent_hours: Ventana temporal para considerar un archivo como 'reciente'.
    reparse_point_attr_mask: Máscara binaria para detectar puntos de reanálisis.
    """
    max_path: int = 260
    recent_hours: int = 24
    reparse_point_attr_mask: int = 0x400

LIMITS: Final = ScannerLimits()

@dataclass
class Suspicion:
    """
    Reporte de hallazgo tras inspección heurística.

    Attributes:
        path: Ruta absoluta al archivo analizado.
        reason: Justificación técnica del hallazgo (ej. ofuscación, comportamiento).
        severity: Nivel de urgencia ('info' para monitoreo, 'warning' para atención requerida).
    """
    path: Path
    reason: str
    severity: str

# Definición de tipos para el sistema de heurísticas
# SuspicionCheck recibe: (archivo a analizar, objeto DirEntry asociado, timestamp actual)
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
    Obtiene metadatos asegurando que no se sigan enlaces simbólicos.
    Captura excepciones de acceso denegado comunes en archivos del sistema.
    """
    if not isinstance(entry, os.DirEntry):
        return None
    try:
        if not entry.is_symlink() and not (_get_file_attributes(entry) & LIMITS.reparse_point_attr_mask):
            return entry.stat(follow_symlinks=False)
        return None
    except (OSError, PermissionError):
        return None

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Consulta la máscara de bits de atributos Win32 (File Attributes) de forma pasiva."""
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError):
        return 0

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida que la ruta sea conforme a las restricciones del sistema de archivos local."""
    if not path_str or len(path_str) > LIMITS.max_path:
        return False
    # Evitar rutas de red (UNC) y ofuscación por caracteres invisibles RTL
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Heurística: Detecta doble extensión (ej. archivo.pdf.exe) utilizada para engañar al usuario."""
    if path is None or path.name is None:
        return None
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Heurística: Identifica archivos ejecutables nuevos en carpetas de descarga temporales."""
    if path is None or path.parent is None or path.parent.name.lower() not in WATCHED_FOLDERS:
        return None
    stats = _safe_stat(entry) if entry else None
    if stats:
        try:
            mtime = getattr(stats, "st_mtime", 0.0)
            if isinstance(mtime, (int, float)) and mtime > 0:
                if (now_ts - float(mtime)) < (LIMITS.recent_hours * 3600):
                    return Suspicion(path, f"Ejecutable reciente detectado (<{LIMITS.recent_hours}h)", "info")
        except (AttributeError, TypeError, ValueError):
            return None
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Heurística: Detecta ejecutables con nombres de procesos críticos fuera de System32."""
    if path is not None and path.name is not None and path.name.lower() in SYSTEM_LOOKALIKES:
        try:
            path_str = str(path).lower()
            if SYSTEM32_LOWER not in path_str:
                return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
        except Exception:
            return None
    return None

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Heurística: Detecta archivos de 0 bytes que podrían actuar como marcadores o ejecutables vacíos."""
    stats = _safe_stat(entry) if entry else None
    if stats is not None:
        try:
            size = getattr(stats, "st_size", -1)
            if isinstance(size, int) and size == 0:
                return Suspicion(path, "Archivo ejecutable vacío sospechoso", "warning")
        except Exception:
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
        self.safe_cache: set[str] = set()
        # Se guarda el Path resuelto para asegurar comparaciones canónicas
        self.base_root: Path = base_root.resolve()
        self.base_root_str: str = str(self.base_root).lower()
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica que la ruta visitada no escape del directorio raíz definido (sandbox)."""
        try:
            target = Path(entry_path).resolve()
            return str(target).lower().startswith(self.base_root_str)
        except (OSError, RuntimeError):
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Valida que el archivo no utilice nombres reservados de sistema (ej. CON, NUL)."""
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina mediante bits de atributo si la entrada apunta fuera del árbol actual."""
        return bool(_get_file_attributes(entry) & LIMITS.reparse_point_attr_mask)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """Realiza chequeo de seguridad completo antes de procesar un nodo."""
        if not entry or not entry.path or not entry.name:
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
            
            # Verificación contra lista de rutas del sistema (safety.py)
            if is_protected_path(Path(entry.path)):
                return False
            
            self.safe_cache.add(entry.path)
            return True
        except (OSError, RuntimeError):
            return False

    def _handle_directory(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        """Añade directorio validado a la pila de exploración para recorrerlo."""
        if entry.path and entry.path.lower() not in self.seen:
            self.seen.add(entry.path.lower())
            directory_stack.append(entry.path)

    def _is_relevant_extension(self, name: str) -> bool:
        """Filtra extensiones que no tienen interés para el motor de heurísticas."""
        idx = name.rfind('.')
        return name[idx:].lower() in SUSPICIOUS_ALL_EXTS if idx != -1 else False

    def process_entry(self, entry: os.DirEntry, directory_stack: List[str]) -> None:
        """Despacha la entrada según su tipo para aplicar heurísticas o seguir recursión."""
        try:
            if not self._is_safe_entry(entry):
                return
            if entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, directory_stack)
            elif entry.is_file(follow_symlinks=False):
                if self._is_relevant_extension(entry.name):
                    self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError):
            return

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta todas las funciones heurísticas registradas sobre el archivo actual."""
        if not path or not entry:
            return
        for check_fn in ALL_CHECKS:
            try:
                finding = check_fn(path, entry, self.now_ts)
                if finding is not None:
                    self.results.append(finding)
            except Exception as e:
                logger.warning(f"Error en heurística {check_fn.__name__} para {path}: {e}")

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> List[Suspicion]:
    """Realiza un análisis estático de un archivo puntual sin recursión."""
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
    """Escanea el árbol de directorios de forma iterativa, gestionando el stack manualmente."""
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
    """Interroga el estado de Defender y solicita una revisión rápida si es posible."""
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
