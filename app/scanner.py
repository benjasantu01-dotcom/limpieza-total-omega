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

# Alias para facilitar la lectura de tipos de funciones de chequeo
SuspicionCheck: TypeAlias = Callable[[Path], Optional["Suspicion"]]

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


def _is_reparse_point(entry: os.DirEntry) -> bool:
    """
    Verifica si la entrada es un punto de reparse (Junction/Symlink).
    
    Usa el atributo FILE_ATTRIBUTE_REPARSE_POINT (0x400) para evitar recursiones
    infinitas en el sistema de archivos al encontrar enlaces simbólicos o junctions.
    
    Args:
        entry: Objeto DirEntry que representa el archivo o carpeta.
        
    Returns:
        True si es un punto de reparse, False en caso contrario o error.
    """
    try:
        return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
    except (OSError, AttributeError):
        return False


def _process_directory_entry(entry: os.DirEntry, root_path: str, results: List[Suspicion], stack: List[str], seen: set[str]) -> None:
    """
    Procesa una entrada de directorio: filtra rutas protegidas, evita recursión infinita
    mediante tracking de 'seen' y delega el escaneo de archivos a `scan_file`.
    """
    if not entry or not isinstance(entry.path, str):
        return

    try:
        # Resolvemos ruta absoluta para evitar ataques de salto de directorio
        path_str = os.path.abspath(entry.path)
        path_obj = Path(path_str)
        
        # Validar sandbox lógico: la ruta debe estar dentro de root_path
        if not path_str.startswith(root_path) or is_protected_path(path_obj):
            return
            
        if entry.is_dir(follow_symlinks=False):
            if not _is_reparse_point(entry) and path_str not in seen:
                seen.add(path_str)
                stack.append(path_str)
        elif entry.is_file():
            results.extend(scan_file(path_obj))
    except (PermissionError, OSError, ValueError):
        pass


def check_double_extension(path: Path) -> Optional[Suspicion]:
    """
    Analiza si el nombre del archivo contiene una extensión doble sospechosa.
    
    Args:
        path: Objeto Path del archivo a inspeccionar.
    Returns:
        Un objeto Suspicion si se detecta doble extensión, None en caso contrario.
    """
    if not path or not path.name:
        return None
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, hours: int = RECENT_FILE_THRESHOLD_HOURS) -> Optional[Suspicion]:
    """
    Evalúa si un archivo ejecutable ha sido modificado recientemente.
    
    Args:
        path: Objeto Path del archivo a inspeccionar.
        hours: Límite de tiempo en horas para considerar el archivo como 'reciente'.
    Returns:
        Un objeto Suspicion si el archivo fue modificado dentro del umbral definido.
    """
    if not path:
        return None
    
    suffix = path.suffix.lower()
    if suffix not in SUSPICIOUS_EXECUTABLE_EXT:
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat(follow_symlinks=False).st_mtime)
        if datetime.now() - mtime < timedelta(hours=hours):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return None


def check_system_lookalike(path: Path) -> Optional[Suspicion]:
    """
    Detecta archivos que imitan nombres de procesos críticos del sistema operativo
    estando ubicados fuera del directorio System32.
    
    Args:
        path: Objeto Path del archivo a validar.
    Returns:
        Un objeto Suspicion si el nombre coincide con un proceso crítico fuera de lugar.
    """
    if not path or not path.name:
        return None
    try:
        if path.name.lower() in SYSTEM_LOOKALIKES:
            parent_str = str(path.parent).lower()
            if SYSTEM32_LOWER not in parent_str:
                return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    except (AttributeError, ValueError, OSError):
        return None
    return None

# Lista inmutable de funciones de análisis heurístico
CHECK_FUNCS: Final[List[SuspicionCheck]] = [
    check_double_extension, 
    check_recent_executable_in_downloads, 
    check_system_lookalike
]

def scan_file(path: Path) -> List[Suspicion]:
    """
    Aplica secuencialmente todas las funciones de `CHECK_FUNCS` sobre una ruta.
    
    Args:
        path: Ruta absoluta al archivo a escanear.
        
    Returns:
        Lista de objetos Suspicion encontrados.
    """
    try:
        # Validación defensiva estricta: normalizar y verificar protección
        abs_path = path.resolve()
        if not abs_path.exists() or is_protected_path(abs_path):
            return []
    except (PermissionError, OSError):
        return []

    results: List[Suspicion] = []
    for check_func in CHECK_FUNCS:
        try:
            res = check_func(abs_path)
            if res is not None:
                results.append(res)
        except (PermissionError, OSError):
            continue
        except Exception as e:
            logger.debug(f"Error inesperado en chequeo {check_func.__name__} para {abs_path}: {e}")
            continue
    
    return results


def scan_directory(directory: Union[str, Path]) -> List[Suspicion]:
    """
    Realiza un recorrido recursivo iterativo sobre `directory`.
    Utiliza un stack para la gestión del árbol y `os.scandir` para maximizar 
    el rendimiento en la enumeración de archivos.
    
    Args:
        directory: Ruta base desde donde comenzar el escaneo.
        
    Returns:
        Lista de todos los objetos Suspicion encontrados en el árbol.
    """
    if not directory:
        return []
        
    try:
        path_obj = Path(directory)
        if not path_obj.exists() or not path_obj.is_dir() or is_protected_path(path_obj):
            return []
            
        root_path = path_obj.resolve()
        root_str = os.path.abspath(str(root_path))
    except (TypeError, ValueError, OSError):
        return []
        
    results: List[Suspicion] = []
    stack: List[str] = [root_str]
    seen: set[str] = {root_str}
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    _process_directory_entry(entry, root_str, results, stack, seen)
        except (PermissionError, OSError):
            continue
            
    return results


def run_windows_defender_quick_scan() -> str:
    """
    Invoca `Start-MpScan` mediante PowerShell para disparar un escaneo de Defender.
    
    Returns:
        Cadena con el resultado del comando o mensaje de error.
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
