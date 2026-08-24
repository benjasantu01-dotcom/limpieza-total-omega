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
# - entry: Opcional, objeto os.DirEntry del sistema de archivos para evitar re-escaneo.
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

# Colección pre-definida de verificaciones para ejecutables
EXECUTABLE_CHECKS: Final[List[SuspicionCheck]] = [
    lambda p, e, t: check_system_lookalike(p, e, t),
    lambda p, e, t: check_recent_executable_in_downloads(p, e, t)
]


class Scanner:
    """
    Gestiona el estado del escaneo y la navegación recursiva del sistema de archivos.
    
    Attributes:
        results: Lista de hallazgos acumulados durante el escaneo.
        seen: Conjunto de rutas procesadas para evitar ciclos de enlaces simbólicos.
        base_root: Ruta absoluta y resuelta del directorio raíz de escaneo.
        now_ts: Marca de tiempo actual para cálculos de antigüedad.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root: Path = base_root.resolve()
        self.now_ts: float = datetime.now().timestamp()

    def _is_safe_entry(self, entry_path: Path) -> bool:
        """Verifica que la ruta resuelta esté contenida dentro del alcance de la raíz original."""
        try:
            return str(entry_path.resolve(strict=False)).startswith(str(self.base_root))
        except (OSError, RuntimeError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """
        Detecta si la entrada es un punto de reanálisis (reparse point) usando atributos de archivo.
        """
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError, TypeError):
            return True 

    def process_entry(self, entry: Optional[os.DirEntry], stack: List[str]) -> None:
        if entry is None or not hasattr(entry, 'path'):
            return
        
        target_path = Path(entry.path)
        
        try:
            # Validación defensiva de solo lectura: evitamos modificar el disco
            if is_protected_path(target_path):
                return
            if str(target_path).startswith("\\\\"):
                return
            if not self._is_safe_entry(target_path):
                return

            is_dir = entry.is_dir(follow_symlinks=False)
        except (OSError, PermissionError):
            return

        if is_dir:
            if not self._is_reparse_point(entry):
                try:
                    path_str = str(target_path.resolve())
                    if path_str not in self.seen:
                        self.seen.add(path_str)
                        stack.append(path_str)
                except (OSError, RuntimeError):
                    pass
            return

        # Procesamiento de archivo: verificación defensiva de metadatos
        try:
            file_stat = entry.stat(follow_symlinks=False)
            if file_stat.st_size == 0:
                return
        except (OSError, PermissionError):
            return

        if RTL_CHAR_RE.search(target_path.name):
            self.results.append(Suspicion(target_path, "Nombre de archivo contiene caracteres de control de ofuscación (RTL)", "critical"))
        
        self.results.extend(scan_file(target_path, self.now_ts, entry=entry))

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    path_parts_lower = {p.lower() for p in path.parts}
    if not (path_parts_lower & WATCHED_FOLDERS):
        return None
        
    try:
        stats = entry.stat(follow_symlinks=False) if entry and entry.path == str(path) else path.stat()
        if (now_ts - stats.st_mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
            return Suspicion(path, f"Ejecutable reciente detectado (<{RECENT_FILE_THRESHOLD_HOURS}h)", "info")
    except (OSError, PermissionError, AttributeError, ValueError):
        pass
    return None


def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    if path.name.lower() in SYSTEM_LOOKALIKES:
        if SYSTEM32_LOWER not in [p.lower() for p in path.parts]:
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None


def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> ScanResult:
    findings: ScanResult = []
    
    if (double_ext := check_double_extension(path, entry, now_ts)):
        findings.append(double_ext)
    
    if path.suffix.lower() in SUSPICIOUS_EXECUTABLE_EXT:
        for check in EXECUTABLE_CHECKS:
            try:
                if (result := check(path, entry, now_ts)):
                    findings.append(result)
            except Exception as e:
                logger.debug(f"Fallo en regla heurística {check.__name__}: {e}")
                
    return findings


def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
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
