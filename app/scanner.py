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
    Evita que el escáner entre en bucles infinitos por enlaces simbólicos.
    """
    try:
        return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
    except (OSError, AttributeError):
        return False


def _process_directory_entry(entry: os.DirEntry, results: List[Suspicion], stack: List[str], seen: set[str]) -> None:
    """
    Procesa una entrada de directorio: filtra rutas protegidas, evita recursión infinita
    mediante tracking de 'seen' y delega el escaneo de archivos a `scan_file`.
    """
    try:
        if entry.is_dir(follow_symlinks=False):
            if not _is_reparse_point(entry):
                path_str = entry.path
                if path_str and path_str not in seen and not is_protected_path(Path(path_str)):
                    seen.add(path_str)
                    stack.append(path_str)
        elif entry.is_file():
            path_obj = Path(entry.path)
            # Validar existencia real antes de escanear por si hubo race condition
            if path_obj.exists() and not is_protected_path(path_obj):
                results.extend(scan_file(path_obj))
    except (PermissionError, OSError):
        pass


def check_double_extension(path: Path) -> Optional[Suspicion]:
    """
    Detecta archivos con doble extensión (ej: 'foto.jpg.exe').
    La técnica busca confundir al usuario ocultando la extensión real.
    """
    if path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, hours: int = RECENT_FILE_THRESHOLD_HOURS) -> Optional[Suspicion]:
    """
    Identifica ejecutables creados o modificados recientemente.
    Un ejecutable nuevo en carpetas de usuario suele requerir inspección del usuario.
    """
    if path.suffix.lower() in SUSPICIOUS_EXECUTABLE_EXT:
        try:
            mtime = datetime.fromtimestamp(path.stat(follow_symlinks=False).st_mtime)
            if datetime.now() - mtime < timedelta(hours=hours):
                return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
        except (FileNotFoundError, PermissionError, OSError):
            pass
    return None


def check_system_lookalike(path: Path) -> Optional[Suspicion]:
    """
    Detecta archivos ejecutables que utilizan nombres de procesos críticos del sistema.
    Los atacantes intentan ocultar procesos maliciosos usando nombres de servicios legítimos.
    """
    try:
        if path.name and path.name.lower() in SYSTEM_LOOKALIKES:
            parent = path.parent
            if parent and SYSTEM32_LOWER not in str(parent).lower():
                return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    except (AttributeError, ValueError, OSError):
        pass
    return None

# Lista inmutable de funciones de análisis heurístico
CHECK_FUNCS: Final[List[SuspicionCheck]] = [
    check_double_extension, 
    check_recent_executable_in_downloads, 
    check_system_lookalike
]

def scan_file(path: Path) -> List[Suspicion]:
    """Aplica secuencialmente todas las funciones de `CHECK_FUNCS` sobre una ruta."""
    try:
        if not path or not path.is_file() or is_protected_path(path):
            return []
    except (OSError, PermissionError):
        return []
        
    results: List[Suspicion] = []
    for check_func in CHECK_FUNCS:
        try:
            res = check_func(path)
            if res:
                results.append(res)
        except (PermissionError, OSError, AttributeError):
            continue
    return results


def scan_directory(directory: Union[str, Path]) -> List[Suspicion]:
    """
    Realiza un recorrido recursivo iterativo optimizado sobre un directorio,
    utilizando una pila para evitar desbordamiento de memoria y `is_protected_path`
    para asegurar el cumplimiento de la política de seguridad global.
    """
    if not directory:
        return []

    path_obj = Path(directory)
    try:
        if not path_obj.exists() or not path_obj.is_dir() or is_protected_path(path_obj):
            return []
        # Resolver ruta canónica para asegurar que no escape del punto de inicio
        root_str = str(path_obj.resolve())
    except (OSError, RuntimeError):
        return []

    results: List[Suspicion] = []
    stack: List[str] = [root_str]
    seen: set[str] = {root_str}
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    _process_directory_entry(entry, results, stack, seen)
        except (PermissionError, OSError):
            continue
            
    return results


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
