"""
scanner.py
Detector HEURÍSTICO de archivos sospechosos. Esto es un complemento
educativo/demostrativo, NO un antivirus real. Para protección seria,
este módulo se apoya en Windows Defender (ya instalado en Windows 11)
en vez de reinventar un motor de firmas.

Señales heurísticas que marca (no borra nada, solo informa):
- Doble extensión (ej. "factura.pdf.exe")
- Ejecutables en carpetas de descargas/temp recién creados
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
DOUBLE_EXTENSION_RE: Final = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)

# Lista blanca de extensiones potencialmente riesgosas para inspección heurística
SUSPICIOUS_EXECUTABLE_EXT: Final = {".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"}

# Procesos críticos de Windows usados para detectar suplantación de identidad
SYSTEM_LOOKALIKES: Final = {"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"}
SYSTEM32_LOWER: Final = "system32"

# Tiempo umbral para definir un archivo como "reciente" (en horas)
RECENT_FILE_THRESHOLD_HOURS: Final = 24


@dataclass
class Suspicion:
    """Representa un hallazgo sospechoso detectado durante el escaneo."""
    path: Path
    reason: str
    severity: str  # "info" | "warning"


def _is_reparse_point(entry: os.DirEntry) -> bool:
    """
    Verifica si la entrada es un punto de reparse (Junction/Symlink) en Windows.
    El bit 0x400 (FILE_ATTRIBUTE_REPARSE_POINT) identifica enlaces simbólicos y junctions.
    """
    try:
        return bool(entry.stat().st_file_attributes & 0x400)
    except (OSError, AttributeError, PermissionError):
        return False


def check_double_extension(path: Path) -> Optional[Suspicion]:
    """
    Analiza si el nombre del archivo contiene una doble extensión que intente 
    engañar al usuario sobre el tipo real de archivo.
    """
    if not path or not path.name or is_protected_path(path):
        return None
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, hours: int = RECENT_FILE_THRESHOLD_HOURS) -> Optional[Suspicion]:
    """
    Evalúa si un archivo ejecutable fue modificado recientemente. 
    Los ejecutables nuevos en carpetas de usuario tienen mayor perfil de riesgo.
    """
    if not path or is_protected_path(path) or path.suffix.lower() not in SUSPICIOUS_EXECUTABLE_EXT:
        return None
    try:
        if not path.exists():
            return None
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if datetime.now() - mtime < timedelta(hours=hours):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return None


def check_system_lookalike(path: Path) -> Optional[Suspicion]:
    """
    Detecta si un archivo tiene el nombre de un proceso crítico de sistema,
    pero se encuentra alojado fuera de la carpeta System32.
    """
    if not path or not path.name or is_protected_path(path):
        return None
    try:
        if path.name.lower() in SYSTEM_LOOKALIKES:
            parent_str = str(path.parent).lower()
            if SYSTEM32_LOWER not in parent_str:
                return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    except (AttributeError, ValueError):
        pass
    return None


def scan_file(path: Path) -> List[Suspicion]:
    """
    Aplica todos los chequeos heurísticos disponibles sobre una ruta de archivo.
    Retorna una lista de hallazgos sospechosos encontrados.
    """
    if path is None or is_protected_path(path) or not path.exists():
        return []
    
    results: List[Suspicion] = []
    checks: List[Callable[[Path], Optional[Suspicion]]] = [
        check_double_extension, 
        check_recent_executable_in_downloads, 
        check_system_lookalike
    ]
    
    for check_func in checks:
        try:
            res = check_func(path)
            if res: 
                results.append(res)
        except Exception:
            continue
    
    return results


def scan_directory(directory: Union[str, Path]) -> List[Suspicion]:
    """
    Recorre el sistema de archivos de forma iterativa (evitando recursión profunda).
    Ignora puntos de reparse para evitar bucles infinitos en el sistema de archivos.
    """
    if not directory:
        return []
        
    try:
        root = Path(directory).resolve()
        if is_protected_path(root) or not root.exists():
            return []
            
        results: List[Suspicion] = []
        stack: List[str] = [str(root)]
        
        while stack:
            current_dir = stack.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        try:
                            if is_protected_path(Path(entry.path)):
                                continue
                            
                            if entry.is_dir(follow_symlinks=False):
                                if not _is_reparse_point(entry):
                                    stack.append(entry.path)
                            elif entry.is_file():
                                results.extend(scan_file(Path(entry.path)))
                        except (PermissionError, OSError):
                            continue
            except (PermissionError, OSError):
                continue
        return results
    except (OSError, RuntimeError) as e:
        logger.error("Error crítico al inicializar el escaneo en %s: %s", directory, e)
        return []


def run_windows_defender_quick_scan() -> str:
    """
    Invoca PowerShell para ejecutar un QuickScan de Windows Defender.
    Retorna el resultado de la salida estándar o un mensaje de error.
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
