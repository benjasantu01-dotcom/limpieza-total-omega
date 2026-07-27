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
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional, Union, Final, Callable, Iterable
from safety import is_protected_path

# Configuración de logger para el módulo
logger = logging.getLogger(__name__)

DOUBLE_EXTENSION_RE: Final = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
SUSPICIOUS_EXECUTABLE_EXT: Final = {".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"}
SYSTEM_LOOKALIKES: Final = {"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"}
SYSTEM32_LOWER: Final = "system32"


@dataclass
class Suspicion:
    """Representa una hallazgo sospechoso detectado durante el escaneo."""
    path: Path
    reason: str
    severity: str  # "info" | "warning"


def check_double_extension(path: Path) -> Optional[Suspicion]:
    """
    Analiza si el nombre del archivo intenta ocultar una extensión ejecutable 
    tras una extensión de documento común, técnica de spoofing de iconos/tipo.
    """
    if not path or not path.name:
        return None
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, hours: int = 24) -> Optional[Suspicion]:
    """
    Evalúa si un ejecutable es reciente basándose en su fecha de modificación. 
    Se prioriza porque los archivos recién descargados no han sido validados 
    por heurísticas de comportamiento a largo plazo.
    """
    if not path or path.suffix.lower() not in SUSPICIOUS_EXECUTABLE_EXT:
        return None
    try:
        # mtime se consulta directamente al FS; se encapsula en try/except 
        # para manejar archivos bloqueados por otros procesos.
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if datetime.now() - mtime < timedelta(hours=hours):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return None


def check_system_lookalike(path: Path) -> Optional[Suspicion]:
    """
    Detecta nombres de procesos críticos fuera de System32.
    La validación contra SYSTEM32_LOWER previene alertas falsas en los archivos
    legítimos del sistema, permitiendo identificar archivos 'impostores'.
    """
    if not path or not path.name:
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
    Ejecuta el conjunto de chequeos heurísticos sobre un archivo individual.
    Retorna una lista vacía si el archivo no existe o no es accesible.
    """
    if path is None:
        return []
        
    try:
        if not path.is_file():
            return []
    except (OSError, PermissionError, ValueError):
        return []

    results: List[Suspicion] = []
    # Lista de validadores: desacoplados para permitir agregar nuevas heurísticas fácilmente
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
    Escanea recursivamente un directorio buscando sospechas.
    Implementa un recorrido iterativo (pila) para evitar errores de desbordamiento
    de pila en estructuras de directorios muy profundas.
    """
    if not directory:
        return []
        
    try:
        root: Path = Path(directory).resolve()
        if is_protected_path(root):
            return []
            
        results: List[Suspicion] = []
        if not root.exists() or not root.is_dir():
            return []
            
        queue: List[Path] = [root]
        while queue:
            current_dir = queue.pop()
            try:
                for entry in current_dir.iterdir():
                    try:
                        # Exclusión de symlinks para evitar ciclos infinitos o lectura externa
                        if entry.is_symlink() or is_protected_path(entry):
                            continue
                        if entry.is_dir():
                            queue.append(entry)
                        elif entry.is_file():
                            results.extend(scan_file(entry))
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
    Dispara un escaneo rápido con Windows Defender mediante PowerShell.
    La ejecución es sincrónica con un timeout de 30 minutos para evitar
    bloqueos indefinidos de la UI principal.
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
