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
from typing import List, Optional, Union, Final, Callable
from safety import is_protected_path

# Configuración de logger para el módulo
logger = logging.getLogger(__name__)

# Expresión regular para detectar extensiones dobles donde la final es ejecutable
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)

# Lista blanca de extensiones potencialmente riesgosas para inspección heurística
SUSPICIOUS_EXECUTABLE_EXT: Final[set[str]] = {".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"}

# Procesos críticos de Windows usados para detectar suplantación de identidad
SYSTEM_LOOKALIKES: Final[set[str]] = {"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"}
SYSTEM32_LOWER: Final[str] = "system32"

# Tiempo umbral para definir un archivo como "reciente" (en horas)
RECENT_FILE_THRESHOLD_HOURS: Final[int] = 24


@dataclass
class Suspicion:
    """Representa un hallazgo sospechoso detectado durante el escaneo."""
    path: Path
    reason: str
    severity: str  # "info" | "warning"


def _is_reparse_point(entry: os.DirEntry) -> bool:
    """
    Verifica si la entrada es un punto de reparse (Junction/Symlink) en Windows.
    Utiliza el bit 0x400 (FILE_ATTRIBUTE_REPARSE_POINT) para identificar enlaces 
    simbólicos y junctions, evitando la recursión infinita durante el escaneo.
    """
    try:
        return bool(entry.stat().st_file_attributes & 0x400)
    except (OSError, AttributeError):
        return False


def check_double_extension(path: Path) -> Optional[Suspicion]:
    """
    Analiza si el nombre del archivo contiene una doble extensión (ej. .pdf.exe).
    """
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, hours: int = RECENT_FILE_THRESHOLD_HOURS) -> Optional[Suspicion]:
    """
    Evalúa si un archivo ejecutable fue modificado recientemente.
    """
    if path.suffix.lower() not in SUSPICIOUS_EXECUTABLE_EXT:
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if datetime.now() - mtime < timedelta(hours=hours):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return None


def check_system_lookalike(path: Path) -> Optional[Suspicion]:
    """
    Detecta suplantación de identidad mediante nombres de procesos críticos.
    """
    if path.name.lower() in SYSTEM_LOOKALIKES:
        try:
            parent_str = str(path.parent).lower()
            if SYSTEM32_LOWER not in parent_str:
                return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
        except (AttributeError, ValueError):
            return None
    return None

# Lista inmutable de funciones de análisis heurístico
CHECK_FUNCS: Final[List[Callable[[Path], Optional[Suspicion]]]] = [
    check_double_extension, 
    check_recent_executable_in_downloads, 
    check_system_lookalike
]

def scan_file(path: Path) -> List[Suspicion]:
    """
    Ejecuta el conjunto de reglas heurísticas sobre un archivo.
    Se asume que la ruta ya fue validada contra is_protected_path por el llamador.
    """
    results: List[Suspicion] = []
    for check_func in CHECK_FUNCS:
        if (res := check_func(path)):
            results.append(res)
    
    return results


def scan_directory(directory: Union[str, Path]) -> List[Suspicion]:
    """
    Realiza un escaneo profundo (recursivo) de un directorio buscando archivos sospechosos.
    
    Utiliza un enfoque de pila para evitar la recursión del stack de llamadas y 'os.scandir'
    para minimizar las llamadas al sistema. Ignora puntos de reparse y rutas protegidas 
    según la política de seguridad vigente.
    """
    if not directory:
        return []
        
    try:
        root = Path(directory).resolve()
        if not root.exists() or not root.is_dir() or is_protected_path(root):
            return []
            
        results: List[Suspicion] = []
        stack: List[Path] = [root]
        
        while stack:
            current_dir = stack.pop()
            try:
                # Se utiliza os.scandir para eficiencia en grandes volúmenes de archivos
                with os.scandir(current_dir) as it:
                    for entry in it:
                        try:
                            entry_path = Path(entry.path)
                            if is_protected_path(entry_path):
                                continue
                                
                            if entry.is_dir(follow_symlinks=False):
                                if not _is_reparse_point(entry):
                                    stack.append(entry_path)
                            elif entry.is_file():
                                results.extend(scan_file(entry_path))
                        except (PermissionError, OSError, ValueError):
                            continue
            except (PermissionError, OSError):
                # Se omiten directorios sin permisos de lectura para continuar el escaneo
                continue
        return results
    except (OSError, RuntimeError, TypeError, ValueError) as e:
        logger.error("Error al acceder al directorio %s: %s", directory, e)
        return []


def run_windows_defender_quick_scan() -> str:
    """
    Invoca la API de PowerShell `Start-MpScan` para ejecutar un análisis de Defender.
    """
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
