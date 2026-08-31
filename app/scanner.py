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
# Reciben la ruta del archivo, una entrada de directorio opcional (para rendimiento)
# y el timestamp de inicio global para asegurar coherencia en comparaciones temporales.
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]

# Alias para representar una colección de hallazgos durante un proceso de escaneo.
ScanResult: TypeAlias = List[Suspicion]

# Expresiones regulares para detección de ofuscación
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)

# Conjuntos de constantes para comparación rápida
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SUSPICIOUS_CONTENT_EXT: Final[frozenset[str]] = frozenset({".pdf"})
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
WATCHED_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})

# Configuración de umbrales
SYSTEM32_LOWER: Final[str] = "system32"
RECENT_FILE_THRESHOLD_HOURS: Final[int] = 24
MAX_PATH_LENGTH: Final[int] = 260
# Constante de Windows para FILE_ATTRIBUTE_REPARSE_POINT (0x400)
WIN_FILE_ATTR_REPARSE_POINT: Final[int] = 0x400

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Evalúa si el nombre del archivo contiene una doble extensión maliciosa."""
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Verifica si un ejecutable fue modificado recientemente en carpetas monitoreadas.
    La heurística prioriza la eficiencia usando el timestamp de inicio global (now_ts).
    """
    if not path: return None
    parts = path.parts
    # Verificación eficiente: si el padre inmediato o alguno de los niveles superiores es una carpeta vigilada
    if not any(part.lower() in WATCHED_FOLDERS for part in parts):
        return None
    try:
        stats = entry.stat(follow_symlinks=False) if entry else path.stat()
        if (now_ts - stats.st_mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
            return Suspicion(path, f"Ejecutable reciente detectado (<{RECENT_FILE_THRESHOLD_HOURS}h)", "info")
    except (OSError, AttributeError, ValueError, FileNotFoundError):
        return None
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Verifica si un ejecutable utiliza nombres de procesos críticos del sistema (ej. svchost.exe)
    cuando se encuentra ubicado fuera de directorios de sistema protegidos.
    """
    if not path or not path.name: return None
    name_lower = path.name.lower()
    if name_lower in SYSTEM_LOOKALIKES:
        if is_protected_path(path):
            return None
        if SYSTEM32_LOWER not in str(path).lower():
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None

# Registro de reglas heurísticas para ejecutables
EXECUTABLE_CHECK_REGISTRY: Final[List[SuspicionCheck]] = [
    check_system_lookalike,
    check_recent_executable_in_downloads
]

class Scanner:
    """
    Controlador de estado para el escaneo del sistema de archivos.
    Mantiene el contexto del escaneo, el registro de resultados y las rutas visitadas
    para evitar ciclos y redundancias.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root = base_root.resolve(strict=False)
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, path_str: Optional[str]) -> bool:
        """
        Valida mediante resolución de rutas que el archivo esté contenido estrictamente
        dentro del árbol del directorio base de escaneo para prevenir escapes de sandbox.
        """
        if not path_str or "\0" in path_str: return False
        try:
            target = Path(path_str).resolve(strict=False)
            return self.base_root == target or self.base_root in target.parents
        except (OSError, RuntimeError, TypeError):
            return False

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """
        Valida que la entrada sea segura para el escaneo: verifica longitud de ruta,
        caracteres inválidos, límites de recursión y denegación explícita mediante 
        `is_protected_path`.
        """
        if not entry or not entry.path:
            return False
        if len(entry.path) > MAX_PATH_LENGTH or entry.path.startswith("\\\\"):
            return False
        
        try:
            if entry.name and (RTL_CHAR_RE.search(entry.name) or RESERVED_NAMES_RE.match(entry.name)):
                return False

            if not self._is_inside_base_root(entry.path):
                return False
            
            if self._is_reparse_point(entry):
                return False

            return not is_protected_path(Path(entry.path))
        except (ValueError, RuntimeError, OSError, TypeError, FileNotFoundError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """
        Detecta junctions, symlinks y puntos de reparse mediante atributos de archivo 
        de bajo nivel para evitar bucles infinitos en el sistema de archivos.
        """
        try:
            return entry.is_symlink() or bool(entry.stat(follow_symlinks=False).st_file_attributes & WIN_FILE_ATTR_REPARSE_POINT)
        except (OSError, AttributeError, TypeError, FileNotFoundError):
            return True 

    def _handle_directory(self, entry: os.DirEntry, stack: List[str]) -> None:
        """Gestiona la cola de recursión añadiendo directorios validados al stack."""
        if entry.path and entry.path not in self.seen:
            self.seen.add(entry.path)
            stack.append(entry.path)

    def process_entry(self, entry: os.DirEntry, stack: List[str]) -> None:
        """
        Clasifica una entrada. Si es directorio válido lo añade a la pila de recursión.
        Si es un archivo con extensión sospechosa, dispara el motor heurístico.
        """
        if not entry or not entry.path: return
        try:
            if entry.is_dir(follow_symlinks=False):
                if self._is_safe_entry(entry):
                    self._handle_directory(entry, stack)
            elif entry.is_file(follow_symlinks=False):
                if entry.stat().st_size == 0: return
                
                _, ext = os.path.splitext(entry.name)
                ext_low = ext.lower()
                
                # Identificar si el archivo requiere inspección heurística
                if ext_low in SUSPICIOUS_EXECUTABLE_EXT or ext_low in SUSPICIOUS_CONTENT_EXT:
                    if self._is_safe_entry(entry):
                        self._run_file_heuristics(Path(entry.path), entry, ext_low)
        except (OSError, PermissionError, TypeError, FileNotFoundError):
            logger.debug(f"Acceso denegado o entrada volátil: {entry.path}")

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry, ext: str) -> None:
        """
        Ejecuta el motor de reglas sobre un archivo candidato y consolida los hallazgos 
        en la lista interna de resultados del objeto Scanner.
        """
        if path.name and RTL_CHAR_RE.search(path.name):
            self.results.append(Suspicion(path, "Nombre de archivo contiene caracteres de control de ofuscación (RTL)", "critical"))
        self.results.extend(scan_file(path, self.now_ts, entry=entry, ext=ext))

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None, ext: Optional[str] = None) -> ScanResult:
    """Ejecuta todas las reglas heurísticas registradas sobre un archivo individual."""
    if not path: return []
    findings: ScanResult = []
    
    if (double_ext := check_double_extension(path, entry, now_ts)):
        findings.append(double_ext)
    
    file_ext = ext or path.suffix.lower()
    if file_ext in SUSPICIOUS_EXECUTABLE_EXT:
        for check in EXECUTABLE_CHECK_REGISTRY:
            try:
                if (result := check(path, entry, now_ts)):
                    findings.append(result)
            except Exception as e:
                logger.debug(f"Fallo no crítico en regla {check.__name__} para {path}: {e}")
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Punto de entrada principal para el escaneo recursivo de un directorio."""
    if not directory:
        return []
    try:
        path_input: Path = Path(directory).resolve(strict=False)
        if not path_input.exists() or not path_input.is_dir() or is_protected_path(path_input):
            return []
    except (OSError, TypeError, ValueError, RuntimeError):
        return []

    scanner = Scanner(base_root=path_input)
    stack: List[str] = [str(path_input)]
    scanner.seen.add(str(path_input))
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if entry:
                        scanner.process_entry(entry, stack)
        except (PermissionError, OSError):
            logger.debug(f"Acceso denegado al directorio {current_dir}")
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """Invoca la API de PowerShell para verificar el estado de Defender y ejecutar un escaneo rápido."""
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
