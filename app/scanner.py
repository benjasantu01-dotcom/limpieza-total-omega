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
# now_ts: Timestamp (float) obtenido al inicio del escaneo para consistencia.
# entry: Objeto os.DirEntry opcional para evitar syscalls innecesarias.
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]

# Representa una lista acumulativa de hallazgos durante el proceso de escaneo.
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
    """Analiza el nombre del archivo en busca de extensiones dobles engañosas."""
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Verifica si un ejecutable ha sido modificado en las últimas horas en carpetas monitoreadas."""
    if not path: return None
    try:
        path_str = str(path).lower()
        if not any(f"\\{folder}\\" in path_str for folder in WATCHED_FOLDERS):
            return None
        
        stats = entry.stat(follow_symlinks=False) if entry and entry.is_file() else path.stat()
            
        if (now_ts - stats.st_mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
            return Suspicion(path, f"Ejecutable reciente detectado (<{RECENT_FILE_THRESHOLD_HOURS}h)", "info")
    except (OSError, AttributeError, ValueError, PermissionError):
        return None
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Valida si un ejecutable con nombre crítico del sistema reside fuera de directorios protegidos."""
    if not path or not path.name: return None
    try:
        if path.name.lower() in SYSTEM_LOOKALIKES:
            if is_protected_path(path):
                return None
            if SYSTEM32_LOWER not in str(path).lower():
                return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    except Exception:
        return None
    return None

# Registro de reglas heurísticas para ejecutables
EXECUTABLE_CHECK_REGISTRY: Final[List[SuspicionCheck]] = [
    check_system_lookalike,
    check_recent_executable_in_downloads
]

class Scanner:
    """
    Controlador de estado para el escaneo del sistema de archivos.
    
    Gestiona la pila de directorios pendientes, mantiene un registro de rutas 
    visitadas para evitar bucles recursivos y centraliza los hallazgos.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[Path] = set()
        self.base_root_str = str(base_root.resolve(strict=False)).lower()
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, path: Path) -> bool:
        """Valida si la ruta está contenida dentro del directorio base definido."""
        if not path: return False
        try:
            return str(path.resolve(strict=False)).lower().startswith(self.base_root_str)
        except (OSError, PermissionError, RuntimeError):
            return False

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """Valida que una entrada de directorio no viole políticas de seguridad o rutas bloqueadas."""
        if not entry or not entry.path:
            return False
        
        # Filtros de longitud de ruta y rutas UNC
        if len(entry.path) > MAX_PATH_LENGTH or entry.path.startswith("\\\\"):
            return False
        
        try:
            if self._is_reparse_point(entry):
                return False

            if entry.name and (RTL_CHAR_RE.search(entry.name) or RESERVED_NAMES_RE.match(entry.name)):
                return False

            path_obj = Path(entry.path)
            if not self._is_inside_base_root(path_obj):
                return False
            
            return not is_protected_path(path_obj)
        except (ValueError, RuntimeError, OSError, TypeError, FileNotFoundError, PermissionError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Detecta puntos de reparse (junctions, enlaces simbólicos) mediante atributos de archivo."""
        try:
            if entry.is_symlink():
                return True
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & WIN_FILE_ATTR_REPARSE_POINT)
        except (OSError, AttributeError, TypeError, FileNotFoundError, PermissionError):
            return True 

    def _handle_directory(self, entry: os.DirEntry, stack: List[Path]) -> None:
        """Agrega un directorio al stack si no ha sido visitado previamente y es seguro."""
        try:
            path = Path(entry.path)
            if path not in self.seen and not is_protected_path(path):
                self.seen.add(path)
                stack.append(path)
        except (OSError, PermissionError):
            pass

    def process_entry(self, entry: os.DirEntry, stack: List[Path]) -> None:
        """Determina si la entrada es un directorio a recorrer o un archivo a analizar."""
        try:
            if not self._is_safe_entry(entry):
                return

            if entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, stack)
            elif entry.is_file(follow_symlinks=False):
                ext_low = Path(entry.name).suffix.lower()
                if ext_low in SUSPICIOUS_ALL_EXTS:
                    try:
                        file_stat = entry.stat(follow_symlinks=False)
                        if file_stat.st_size == 0:
                            self.results.append(Suspicion(Path(entry.path), "Archivo vacío sospechoso", "warning"))
                        else:
                            self._run_file_heuristics(Path(entry.path), entry, ext_low)
                    except (OSError, PermissionError):
                        return
        except (OSError, PermissionError, TypeError, FileNotFoundError):
            logger.debug(f"Acceso denegado o archivo inaccesible: {entry.path}")

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry, ext: str) -> None:
        """Ejecuta las heurísticas registradas sobre un archivo identificado como sospechoso."""
        try:
            if path.name and RTL_CHAR_RE.search(path.name):
                self.results.append(Suspicion(path, "Nombre contiene caracteres de ofuscación (RTL)", "critical"))
            self.results.extend(scan_file(path, self.now_ts, entry=entry, ext=ext))
        except (OSError, PermissionError):
            pass

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None, ext: Optional[str] = None) -> ScanResult:
    """Orquestador de reglas: ejecuta todas las heurísticas configuradas para un archivo."""
    if not isinstance(path, Path): return []
    
    findings: ScanResult = []
    
    try:
        if (double_ext := check_double_extension(path, entry, now_ts)):
            findings.append(double_ext)
        
        file_ext = ext or path.suffix.lower()
        if file_ext in SUSPICIOUS_EXECUTABLE_EXT:
            for check_fn in EXECUTABLE_CHECK_REGISTRY:
                try:
                    if (result := check_fn(path, entry, now_ts)):
                        findings.append(result)
                except Exception as e:
                    logger.debug(f"Fallo en regla {check_fn.__name__} para {path}: {e}")
    except (OSError, PermissionError):
        pass
        
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Punto de entrada: escaneo recursivo mediante pila (Stack) del sistema."""
    if not directory:
        return []
        
    try:
        path_input = Path(directory).resolve(strict=False)
        if not path_input.exists() or not path_input.is_dir() or is_protected_path(path_input):
            return []
    except (OSError, TypeError, ValueError, RuntimeError, PermissionError):
        return []

    scanner = Scanner(base_root=path_input)
    stack: List[Path] = [path_input]
    scanner.seen.add(path_input)
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    scanner.process_entry(entry, stack)
        except (PermissionError, OSError, FileNotFoundError):
            logger.debug(f"Acceso denegado o error en el directorio {current_dir}")
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """Invoca PowerShell para ejecutar un QuickScan de Windows Defender."""
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
    except subprocess.CalledProcessError as e:
        return f"Error ejecutando Windows Defender: {e.stderr}"
    except (FileNotFoundError, OSError):
        return "PowerShell no disponible. Este módulo requiere Windows."
    except subprocess.TimeoutExpired:
        return "El escaneo de Windows Defender excedió el tiempo límite."
