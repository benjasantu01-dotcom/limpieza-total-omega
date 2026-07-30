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
    try:
        path_str = os.path.abspath(entry.path)
        
        # Omitir protegidos usando la función que recibe texto crudo
        if is_protected_path(Path(path_str)) or not path_str.startswith(root_path):
            return
            
        if entry.is_dir(follow_symlinks=False):
            if not _is_reparse_point(entry) and path_str not in seen:
                seen.add(path_str)
                stack.append(path_str)
        elif entry.is_file():
            results.extend(scan_file(Path(path_str)))
    except (PermissionError, OSError, ValueError):
        pass


def check_double_extension(path: Path) -> Optional[Suspicion]:
    """
    Detecta nombres de archivo que terminan en una extensión ejecutable precedida 
    por una extensión documental, táctica común de suplantación visual.
    """
    if not path or not path.name:
        return None
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, hours: int = RECENT_FILE_THRESHOLD_HOURS) -> Optional[Suspicion]:
    """
    Evalúa la edad de un archivo ejecutable mediante su marca de tiempo de modificación.
    Devuelve un hallazgo si el archivo fue creado/modificado dentro del umbral definido.
    """
    if not path:
        return None
    
    suffix = path.suffix.lower()
    if suffix not in SUSPICIOUS_EXECUTABLE_EXT:
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
    Detecta archivos con nombres idénticos a binarios críticos de Windows 
    cuando se encuentran fuera de los directorios del sistema, indicando posible confusión.
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
    Retorna una lista con todos los objetos `Suspicion` hallados.
    """
    if not path or is_protected_path(path):
        return []

    results: List[Suspicion] = []
    for check_func in CHECK_FUNCS:
        try:
            if (res := check_func(path)):
                results.append(res)
        except Exception:
            continue
    
    return results


def scan_directory(directory: Union[str, Path]) -> List[Suspicion]:
    """
    Realiza un recorrido recursivo iterativo sobre `directory`.
    Utiliza un stack para la gestión del árbol y `os.scandir` para maximizar 
    el rendimiento en la enumeración de archivos.
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
    Captura la salida estándar y maneja errores de ejecución o timeout.
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
