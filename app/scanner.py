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
from datetime import datetime, timedelta
from typing import List, Optional, Union, Final, Callable, TypeAlias
from safety import is_protected_path

# Configuración de logger para el módulo
logger = logging.getLogger(__name__)

# Alias de tipos para mejorar la legibilidad y mantenibilidad de la lógica de escaneo
SuspicionCheck: TypeAlias = Callable[[Path], Optional["Suspicion"]]
ScanResult: TypeAlias = List["Suspicion"]

# Expresión regular para detectar extensiones dobles donde la final es ejecutable
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)

# Lista blanca de extensiones potencialmente riesgosas para inspección heurística
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})

# Procesos críticos de Windows usados para detectar suplantación de identidad
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
SYSTEM32_LOWER: Final[str] = "system32"

# Tiempo umbral para definir un archivo como "reciente" (en horas)
RECENT_FILE_THRESHOLD_HOURS: Final[int] = 24


@dataclass
class Suspicion:
    """Representa un hallazgo sospechoso detectado durante el escaneo."""
    path: Path
    reason: str
    severity: str  # "info" | "warning"


class Scanner:
    """
    Controlador del estado del escaneo. 
    Mantiene el conjunto 'seen' para evitar el procesamiento redundante de rutas
    y el seguimiento infinito en enlaces simbólicos cíclicos.
    """
    
    def __init__(self) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """
        Verifica si el sistema de archivos marca esta entrada como reparse point.
        No sigue enlaces para evitar escapes fuera del árbol de directorios permitido.
        
        Args:
            entry: La entrada de directorio (`os.DirEntry`) a inspeccionar.
            
        Returns:
            bool: True si la entrada es un punto de reparseo, False en caso contrario.
        """
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return False

    def process_entry(self, entry: os.DirEntry, stack: List[str]) -> None:
        """
        Clasifica una entrada de directorio o archivo. 
        Si es directorio: valida seguridad y añade a la pila si no es un punto de reparseo.
        Si es archivo: aplica las heurísticas registradas.

        Args:
            entry: Entrada de sistema de archivos (`os.DirEntry`) a procesar.
            stack: Lista de directorios pendientes de escaneo (mutada in-place).
        """
        try:
            if entry.is_dir(follow_symlinks=False):
                if not self._is_reparse_point(entry):
                    path_key = os.path.normpath(entry.path)
                    if path_key not in self.seen and not is_protected_path(Path(path_key)):
                        self.seen.add(path_key)
                        stack.append(path_key)
            elif entry.is_file(follow_symlinks=False):
                # La seguridad se pre-valida en el bucle para evitar llamadas redundantes
                self.results.extend(scan_file(Path(entry.path)))
        except (PermissionError, OSError):
            pass


def check_double_extension(path: Path) -> Optional[Suspicion]:
    """
    Detecta nombres de archivos con doble extensión que ocultan ejecutables.

    Args:
        path: Ruta completa del archivo a evaluar.

    Returns:
        Un objeto `Suspicion` si se detecta el patrón, `None` si es seguro.
    """
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, hours: int = RECENT_FILE_THRESHOLD_HOURS) -> Optional[Suspicion]:
    """
    Analiza la fecha de modificación de ejecutables para identificar descargas recientes.

    Args:
        path: Ruta del archivo.
        hours: Límite de horas para considerar un archivo como "reciente".

    Returns:
        `Suspicion` si el archivo fue modificado dentro del umbral dado, `None` caso contrario.
    """
    if not path or path.suffix.lower() not in SUSPICIOUS_EXECUTABLE_EXT:
        return None
        
    try:
        # Usamos lstat para no resolver enlaces y evitar errores adicionales en archivos bloqueados
        mtime = datetime.fromtimestamp(path.lstat().st_mtime)
        if datetime.now() - mtime < timedelta(hours=hours):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return None


def check_system_lookalike(path: Path) -> Optional[Suspicion]:
    """
    Identifica ejecutables con nombres de procesos críticos fuera de System32.

    Args:
        path: Ruta del archivo a inspeccionar.

    Returns:
        `Suspicion` si el nombre imita a un proceso de sistema crítico, `None` caso contrario.
    """
    if not path or path.name.lower() not in SYSTEM_LOOKALIKES:
        return None
        
    try:
        if SYSTEM32_LOWER not in str(path.parent).lower():
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    except (OSError, AttributeError):
        pass
    return None

# Lista inmutable de funciones de análisis heurístico
CHECK_FUNCS: Final[List[SuspicionCheck]] = [
    check_double_extension, 
    check_recent_executable_in_downloads, 
    check_system_lookalike
]

def scan_file(path: Path) -> ScanResult:
    """
    Ejecuta el conjunto de heurísticas sobre un archivo específico.
    Solo procesa archivos existentes y validados por seguridad.
    """
    if not path or is_protected_path(path):
        return []

    # Validar existencia antes de procesar para evitar ruido en logs o excepciones en checks
    if not path.exists():
        return []

    findings: ScanResult = []
    for check_func in CHECK_FUNCS:
        try:
            result = check_func(path)
            if result:
                findings.append(result)
        except (PermissionError, OSError, FileNotFoundError):
            continue
    return findings


def scan_directory(directory: Union[str, Path]) -> ScanResult:
    """
    Inicia un escaneo recursivo desde un punto de entrada dado.

    Args:
        directory: Ruta base (str o Path) para comenzar el análisis.

    Returns:
        Lista de objetos `Suspicion` acumulados en el escaneo.
    """
    if not directory:
        return []
        
    try:
        root_path = Path(directory).resolve(strict=True)
        # Validación defensiva extra: no procesar nada que no sea un directorio real
        if not root_path.is_dir() or root_path.is_symlink() or is_protected_path(root_path):
            return []
    except (OSError, RuntimeError):
        return []

    scanner = Scanner()
    root_str = str(root_path)
    stack: List[str] = [root_str]
    scanner.seen.add(root_str)
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    scanner.process_entry(entry, stack)
        except (PermissionError, OSError):
            continue
            
    return scanner.results


def run_windows_defender_quick_scan() -> str:
    """Invoca `Start-MpScan` mediante PowerShell para disparar un escaneo de Defender."""
    try:
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
