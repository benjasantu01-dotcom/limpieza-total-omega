"""
scanner.py
Detector HEURÍSTICO de archivos sospechosos. Esto es un complemento
educativo/demostrativo, NO un antivirus real. Para protección seria,
este módulo se apoya en Windows Defender (ya instalado en Windows 11)
en vez de reinventar un motor de firmas.

Señales heurísticas que marca (no borra nada, solo informa):
- Doble extensión (ej. "factura.pdf.exe")
- Ejecutables en carpetas de usuario recién creados
- Nombres que imitan archivos de sistema pero fuera de System32
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
        path: Ruta absoluta del archivo analizado.
        reason: Descripción legible de por qué se considera sospechoso.
        severity: Nivel de criticidad ("info" para métricas, "warning" para alertas).
    """
    path: Path
    reason: str
    severity: str

# Alias para funciones de chequeo heurístico.
# Reciben el path del archivo, el objeto DirEntry opcional para evitar stat() extra, 
# y el timestamp de inicio del escaneo para cálculos de antigüedad.
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]
ScanResult: TypeAlias = List[Suspicion]

# REGEX para detectar extensiones dobles donde la última es ejecutable
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)

# Conjuntos para búsquedas O(1)
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
SYSTEM32_LOWER: Final[str] = "system32"
WATCHED_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})

RECENT_FILE_THRESHOLD_HOURS: Final[int] = 24


class Scanner:
    """
    Controlador de estado para el escaneo recursivo. Gestiona el rastreo de carpetas visitadas.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root = base_root.resolve()
        self.now_ts = datetime.now().timestamp()

    def _is_safe_entry(self, entry_path: Path) -> bool:
        """Verifica que la entrada esté dentro del base_root definido para evitar escapes del directorio raíz."""
        try:
            resolved = entry_path.resolve()
            return self.base_root == resolved or self.base_root in resolved.parents
        except (RuntimeError, ValueError, OSError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si una entrada es un punto de reanálisis (Junction o Symlink) para evitar ciclos infinitos."""
        try:
            # 0x400 es FILE_ATTRIBUTE_REPARSE_POINT
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return True # Asumir inseguro si no se puede verificar

    def process_entry(self, entry: os.DirEntry, stack: List[str]) -> None:
        """
        Valida y procesa una entrada del sistema de archivos, decidiendo si continuar la recursión o analizar el archivo.
        """
        if not entry:
            return
        
        # Bloqueo total de enlaces simbólicos y puntos de reanálisis por seguridad
        if entry.is_symlink() or self._is_reparse_point(entry):
            return

        try:
            target_path = Path(entry.path)
            if is_protected_path(target_path) or str(target_path).startswith("\\\\"):
                return

            if not self._is_safe_entry(target_path):
                return

            if entry.is_dir(follow_symlinks=False):
                if entry.path not in self.seen:
                    self.seen.add(entry.path)
                    stack.append(entry.path)
            elif entry.is_file(follow_symlinks=False):
                self.results.extend(scan_file(target_path, self.now_ts, entry=entry))
                
        except (PermissionError, OSError) as e:
            logger.debug(f"Acceso denegado o error de sistema en {entry.path}: {e}")

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Analiza si el nombre del archivo contiene una doble extensión que sugiere una intención de ocultamiento.
    """
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Verifica si un ejecutable ha sido creado/modificado recientemente en directorios de alta exposición.
    """
    if not entry:
        return None
    
    try:
        path_parts = {p.lower() for p in path.parts}
        if WATCHED_FOLDERS.isdisjoint(path_parts):
            return None

        # st_mtime puede fallar si el archivo fue bloqueado o eliminado por otro proceso
        file_stat = entry.stat()
        if (now_ts - file_stat.st_mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
            return Suspicion(path, f"Ejecutable reciente detectado (<{RECENT_FILE_THRESHOLD_HOURS}h)", "info")
    except (OSError, AttributeError, OverflowError, ValueError, TypeError):
        pass
    return None


def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Identifica archivos cuyo nombre coincide con procesos críticos de Windows pero residen fuera de System32.
    """
    if path.name.lower() in SYSTEM_LOOKALIKES:
        parent_path = path.parent
        if parent_path and SYSTEM32_LOWER not in str(parent_path).lower():
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """
    Ejecuta el pipeline de heurísticas sobre un archivo dado y retorna la lista de hallazgos.
    """
    findings: ScanResult = []
    
    # 1. Chequeos genéricos (ej. doble extensión)
    res = check_double_extension(path, entry, now_ts)
    if res:
        findings.append(res)
    
    # 2. Chequeos específicos de ejecutables (optimizado mediante pre-cálculo de extensión)
    suffix = path.suffix.lower()
    if suffix in SUSPICIOUS_EXECUTABLE_EXT:
        for check in (check_system_lookalike, check_recent_executable_in_downloads):
            res = check(path, entry, now_ts)
            if res:
                findings.append(res)
                
    return findings


def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Inicializa y ejecuta el proceso de escaneo recursivo en el directorio proporcionado."""
    if directory is None:
        return []
        
    try:
        path_input = Path(directory).resolve()
        if not path_input.is_dir() or is_protected_path(path_input):
            return []
    except (OSError, TypeError, ValueError) as e:
        logger.error(f"Error procesando directorio base {directory}: {e}")
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
        except (PermissionError, OSError, ValueError, RuntimeError) as e:
            logger.debug(f"Error accediendo al directorio {current_dir}: {e}")
            continue
            
    return scanner.results


def run_windows_defender_quick_scan() -> str:
    """Interactúa con Windows Defender vía PowerShell para realizar un análisis rápido del sistema."""
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
