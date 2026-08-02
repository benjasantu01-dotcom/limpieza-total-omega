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
    Encapsula el estado y la lógica de recorrido del sistema de archivos.
    Mantiene un set de rutas visitadas para evitar ciclos en estructuras de archivos complejas.
    """
    
    def __init__(self) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Verifica si la entrada es un enlace simbólico o junction mediante atributos de archivo."""
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return False

    def process_entry(self, entry: os.DirEntry, stack: List[str]) -> None:
        """
        Analiza una entrada: si es directorio, lo apila para recorrido; si es archivo, ejecuta heurísticas.
        
        :param entry: Entrada del sistema de archivos proporcionada por os.scandir.
        :param stack: Lista mutable que actúa como pila para el recorrido recursivo (DFS).
        """
        try:
            path_str = entry.path
            
            if entry.is_dir(follow_symlinks=False):
                if not self._is_reparse_point(entry):
                    resolved_path = Path(path_str).resolve()
                    path_key = str(resolved_path)
                    if path_key not in self.seen and not is_protected_path(resolved_path):
                        self.seen.add(path_key)
                        stack.append(path_str)
            elif entry.is_file(follow_symlinks=False):
                path_obj = Path(path_str).resolve()
                if not is_protected_path(path_obj):
                    self.results.extend(scan_file(path_obj))
        except (PermissionError, OSError):
            pass


def check_double_extension(path: Path) -> Optional[Suspicion]:
    """
    Analiza si el nombre del archivo contiene una extensión de documento seguida
    por una de ejecución, práctica común para engañar usuarios.
    """
    if path is None or path.name is None:
        return None
        
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, hours: int = RECENT_FILE_THRESHOLD_HOURS) -> Optional[Suspicion]:
    """
    Evalúa la fecha de modificación del archivo. Un ejecutable descargado 
    recientemente aumenta el perfil de riesgo según heurísticas estándar.
    """
    if path is None or is_protected_path(path) or path.suffix.lower() not in SUSPICIOUS_EXECUTABLE_EXT:
        return None
        
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if datetime.now() - mtime < timedelta(hours=hours):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return None


def check_system_lookalike(path: Path) -> Optional[Suspicion]:
    """
    Identifica si un archivo utiliza nombres de procesos críticos del sistema
    (ej. svchost.exe) estando ubicado fuera del directorio System32 oficial.
    """
    if path is None or path.name is None or path.name.lower() not in SYSTEM_LOOKALIKES:
        return None
        
    if is_protected_path(path):
        return None
        
    try:
        parent = path.parent
        if parent is not None and SYSTEM32_LOWER not in str(parent).lower():
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
    """Aplica todas las heurísticas registradas sobre un archivo único."""
    if path is None or not path.is_file():
        return []
        
    findings: ScanResult = []
    for check_func in CHECK_FUNCS:
        try:
            result = check_func(path)
            if result:
                findings.append(result)
        except (PermissionError, OSError):
            continue
    return findings


def scan_directory(directory: Union[str, Path]) -> ScanResult:
    """Realiza el escaneo recursivo iterativo de un directorio completo."""
    if not directory:
        return []
        
    try:
        path_obj = Path(directory).resolve()
        if not path_obj.is_dir() or is_protected_path(path_obj):
            return []
    except (OSError, RuntimeError):
        return []

    scanner = Scanner()
    root_str = str(path_obj)
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
