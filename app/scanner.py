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

# Alias para funciones que evalúan un archivo.
# - path: Objeto Path del archivo a inspeccionar.
# - entry: Opcional, objeto os.DirEntry para acceso eficiente a metadatos.
# - now_ts: Timestamp en formato float (Unix epoch) para cálculos de antigüedad.
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]

# Alias para representar una colección de hallazgos.
ScanResult: TypeAlias = List[Suspicion]

# Expresiones regulares para detección de ofuscación
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d]")

# Conjuntos de constantes para comparación rápida
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
WATCHED_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})

# Configuración de umbrales
SYSTEM32_LOWER: Final[str] = "system32"
RECENT_FILE_THRESHOLD_HOURS: Final[int] = 24
MAX_PATH_LENGTH: Final[int] = 260

class Scanner:
    """
    Gestiona el estado del escaneo y la navegación recursiva del sistema de archivos.
    Mantiene el estado de rutas visitadas para evitar ciclos en sistemas con enlaces simbólicos.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root: Path = base_root.resolve(strict=False)
        self.base_root_str: str = str(self.base_root).casefold()
        self.now_ts: float = datetime.now().timestamp()

    def _is_safe_entry(self, entry_path: Path) -> bool:
        """
        Valida que una ruta esté dentro del árbol del directorio base.
        Usa resolución de rutas para evitar escapes mediante '..' o enlaces simbólicos.
        """
        if is_protected_path(entry_path) or len(str(entry_path)) > MAX_PATH_LENGTH:
            return False
        try:
            resolved = str(entry_path.resolve(strict=False)).casefold()
            return resolved == self.base_root_str or resolved.startswith(self.base_root_str + os.sep)
        except (OSError, RuntimeError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """
        Detecta si un directorio es un 'reparse point' (junction/symlink).
        Se bloquea el seguimiento de estos para prevenir bucles infinitos en el escaneo.
        """
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError, TypeError):
            return True 

    def process_entry(self, entry: Optional[os.DirEntry], stack: List[str]) -> None:
        """
        Analiza una entrada del sistema de archivos mediante una pila (DFS).
        Si es un directorio, añade a la pila si es seguro; si es archivo, aplica heurísticas.
        """
        if entry is None or not hasattr(entry, 'path') or not entry.path:
            return
        
        try:
            target_path = Path(entry.path)
            # Validación de seguridad: rutas externas, protegidas o con UNC bloqueadas
            if str(target_path).startswith("\\\\") or not self._is_safe_entry(target_path):
                return

            if entry.is_dir(follow_symlinks=False):
                if not self._is_reparse_point(entry):
                    path_str = entry.path
                    if path_str not in self.seen:
                        self.seen.add(path_str)
                        stack.append(path_str)
                return

            if entry.is_file(follow_symlinks=False):
                self._run_file_heuristics(target_path, entry)

        except (OSError, PermissionError, TypeError, FileNotFoundError) as e:
            logger.debug(f"Acceso denegado o entrada inválida {entry.path}: {e}")
            return

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """
        Aplica filtros de seguridad sobre un archivo específico y almacena los hallazgos.
        Ejecuta comprobaciones de nombre (RTL) y orquesta las reglas de `scan_file`.
        """
        if RTL_CHAR_RE.search(path.name):
            self.results.append(Suspicion(path, "Nombre de archivo contiene caracteres de control de ofuscación (RTL)", "critical"))
        
        self.results.extend(scan_file(path, self.now_ts, entry=entry))

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Detecta archivos con extensiones dobles, práctica común para ocultar binarios ejecutables."""
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Evalúa si un archivo ejecutable es 'reciente' basándose en la fecha de modificación."""
    parts = path.parts
    if any(p.lower() in WATCHED_FOLDERS for p in parts):
        try:
            stats = entry.stat(follow_symlinks=False) if entry else path.stat()
            if (now_ts - stats.st_mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
                return Suspicion(path, f"Ejecutable reciente detectado (<{RECENT_FILE_THRESHOLD_HOURS}h)", "info")
        except (OSError, PermissionError, AttributeError, ValueError, FileNotFoundError):
            pass
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Compara nombres de archivos contra ejecutables críticos fuera de directorios de sistema protegidos."""
    if path.name.lower() in SYSTEM_LOOKALIKES:
        if SYSTEM32_LOWER not in str(path).lower():
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """
    Orquestador de reglas heurísticas. Aplica validaciones genéricas y
    específicas según el tipo de archivo (ejecutable).
    """
    findings: ScanResult = []
    
    # Reglas aplicables a todo archivo
    if (double_ext := check_double_extension(path, entry, now_ts)):
        findings.append(double_ext)
    
    # Reglas exclusivas para ejecutables
    if path.suffix.lower() in SUSPICIOUS_EXECUTABLE_EXT:
        # Registro de verificaciones específicas para binarios
        executable_check_registry: List[SuspicionCheck] = [
            check_system_lookalike,
            check_recent_executable_in_downloads
        ]
        
        for check in executable_check_registry:
            try:
                if (result := check(path, entry, now_ts)):
                    findings.append(result)
            except Exception as e:
                logger.debug(f"Fallo en regla heurística {check.__name__} para {path}: {e}")
        
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """
    Punto de entrada para el escaneo. Inicializa la pila de directorios
    y coordina la ejecución de `Scanner` para procesar el árbol de archivos.
    """
    if not directory:
        return []
        
    try:
        raw_path = Path(directory)
        path_input: Path = raw_path.resolve(strict=False)
        if not path_input.is_dir() or is_protected_path(path_input):
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
                    scanner.process_entry(entry, stack)
        except (PermissionError, OSError) as e:
            logger.debug(f"Error accediendo a directorio {current_dir}: {e}")
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """Invoca mediante PowerShell el estado y escaneo rápido de Windows Defender."""
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
    except FileNotFoundError:
        return "PowerShell no disponible. Este módulo requiere Windows."
    except subprocess.TimeoutExpired:
        return "El escaneo de Windows Defender excedió el tiempo límite."
