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
    """Valida si el nombre del archivo sugiere una extensión oculta tras una extensión de documento."""
    if path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Evalúa si un ejecutable ha sido modificado recientemente en directorios de usuario de alto riesgo."""
    # O(1) Check: Verificar si el nombre del padre está en el set de carpetas observadas
    if path.parent.name.lower() not in WATCHED_FOLDERS:
        return None
    
    try:
        stats = entry.stat(follow_symlinks=False) if entry else path.stat()
        if (now_ts - stats.st_mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
            return Suspicion(path, f"Ejecutable reciente detectado (<{RECENT_FILE_THRESHOLD_HOURS}h)", "info")
    except (OSError, AttributeError, ValueError, PermissionError):
        pass
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Detecta ejecutables con nombres de servicios críticos del sistema fuera de system32."""
    if path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        if not is_protected_path(path) and SYSTEM32_LOWER not in str(path).lower():
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None

# Registro de reglas heurísticas para ejecutables
EXECUTABLE_CHECK_REGISTRY: Final[List[SuspicionCheck]] = [
    check_system_lookalike,
    check_recent_executable_in_downloads
]

class Scanner:
    """
    Controlador de estado para el escaneo recursivo del sistema de archivos.
    Mantiene el registro de archivos visitados y la raíz base para evitar escapes.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root = base_root.resolve(strict=False)
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, path_str: str) -> bool:
        """Verifica que la ruta resuelta esté contenida en la jerarquía del directorio raíz."""
        if not path_str:
            return False
        try:
            target = Path(path_str).resolve(strict=False)
            return self.base_root in target.parents or target == self.base_root
        except (OSError, RuntimeError):
            return False

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """
        Valida criterios de seguridad antes de procesar una entrada.
        Verifica: longitud de ruta, caracteres inválidos, puntos de reparse y zonas protegidas.
        """
        if not entry or not entry.path or len(entry.path) > MAX_PATH_LENGTH or entry.path.startswith("\\\\"):
            return False
        
        try:
            if self._is_reparse_point(entry):
                return False
            if entry.name and (RTL_CHAR_RE.search(entry.name) or RESERVED_NAMES_RE.match(entry.name)):
                return False
            if not self._is_inside_base_root(entry.path):
                return False
            return not is_protected_path(Path(entry.path))
        except (ValueError, RuntimeError, OSError, TypeError, FileNotFoundError, PermissionError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si la entrada apunta a un junction o symlink usando atributos de archivo de Windows."""
        try:
            if entry.is_symlink():
                return True
            # Usar lstat evita resolver el enlace; verificamos los atributos crudos de Windows
            stats = entry.stat(follow_symlinks=False)
            return bool(stats.st_file_attributes & WIN_FILE_ATTR_REPARSE_POINT)
        except (OSError, AttributeError, TypeError, FileNotFoundError, PermissionError):
            return True 

    def _handle_directory(self, entry: os.DirEntry, stack: List[str]) -> None:
        """Añade un directorio validado a la pila de búsqueda si no ha sido visitado previamente."""
        if entry.path and entry.path not in self.seen:
            self.seen.add(entry.path)
            stack.append(entry.path)

    def process_entry(self, entry: os.DirEntry, stack: List[str]) -> None:
        """Clasifica una entrada para su procesamiento: recursión de directorios o escaneo de archivos."""
        try:
            if not self._is_safe_entry(entry):
                return

            if entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, stack)
            elif entry.is_file(follow_symlinks=False):
                ext_low = Path(entry.name).suffix.lower() if entry.name else ""
                if ext_low in SUSPICIOUS_ALL_EXTS:
                    self._run_file_heuristics(Path(entry.path), entry, ext_low)
        except (OSError, PermissionError, TypeError, FileNotFoundError):
            pass

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry, ext: str) -> None:
        """Ejecuta el conjunto de heurísticas registradas sobre un archivo sospechoso."""
        if path.name and RTL_CHAR_RE.search(path.name):
            self.results.append(Suspicion(path, "Nombre contiene caracteres de ofuscación (RTL)", "critical"))
        self.results.extend(scan_file(path, self.now_ts, entry=entry, ext=ext))

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None, ext: Optional[str] = None) -> ScanResult:
    """Orquestador de reglas para evaluar la peligrosidad de un archivo dado."""
    findings: ScanResult = []
    
    # Heurística 1: Extensión doble
    if (double_ext := check_double_extension(path, entry, now_ts)):
        findings.append(double_ext)
    
    # Heurística 2: Ejecutables
    file_ext = (ext or path.suffix.lower())
    if file_ext in SUSPICIOUS_EXECUTABLE_EXT:
        try:
            stats = entry.stat(follow_symlinks=False) if entry else path.stat()
            if stats.st_size == 0:
                findings.append(Suspicion(path, "Archivo vacío sospechoso", "warning"))
        except (OSError, PermissionError, AttributeError):
            pass

        for check_fn in EXECUTABLE_CHECK_REGISTRY:
            if (result := check_fn(path, entry, now_ts)):
                findings.append(result)
        
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Punto de entrada principal para escaneo recursivo de una ruta. Retorna lista de hallazgos."""
    if not directory:
        return []
        
    try:
        path_input = Path(directory).resolve(strict=False)
        if not path_input.exists() or not path_input.is_dir() or is_protected_path(path_input):
            return []
    except (OSError, TypeError, ValueError, RuntimeError, PermissionError):
        return []

    scanner = Scanner(base_root=path_input)
    stack: List[str] = [str(path_input)]
    scanner.seen.add(str(path_input))
    
    while stack:
        current_dir = stack.pop()
        if not current_dir:
            continue
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if entry:
                        scanner.process_entry(entry, stack)
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
    except subprocess.CalledProcessError as e:
        return f"Error ejecutando Windows Defender: {e.stderr}"
    except (FileNotFoundError, OSError):
        return "PowerShell no disponible. Este módulo requiere Windows."
    except subprocess.TimeoutExpired:
        return "El escaneo de Windows Defender excedió el tiempo límite."
