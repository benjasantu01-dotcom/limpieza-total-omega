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

DOUBLE_EXTENSION_RE: Final = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
SUSPICIOUS_EXECUTABLE_EXT: Final = {".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"}
SYSTEM_LOOKALIKES: Final = {"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"}
SYSTEM32_LOWER: Final = "system32"


@dataclass
class Suspicion:
    """Representa un hallazgo sospechoso detectado durante el escaneo."""
    path: Path
    reason: str
    severity: str  # "info" | "warning"


def _is_reparse_point(path: Path) -> bool:
    """Verifica si la ruta es un punto de reparse (Junction/Symlink) en Windows."""
    try:
        st = path.lstat()
        # 0x400 es la máscara de atributo FILE_ATTRIBUTE_REPARSE_POINT
        return bool(getattr(st, 'st_file_attributes', 0) & 0x400)
    except (OSError, AttributeError):
        return False


def check_double_extension(path: Path) -> Optional[Suspicion]:
    """Analiza si el nombre del archivo intenta ocultar una extensión ejecutable."""
    if not path or not path.name:
        return None
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, hours: int = 24) -> Optional[Suspicion]:
    """Evalúa si un ejecutable es reciente mediante su fecha de última modificación."""
    if not path or path.suffix.lower() not in SUSPICIOUS_EXECUTABLE_EXT:
        return None
    try:
        if not path.is_file():
            return None
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if datetime.now() - mtime < timedelta(hours=hours):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return None


def check_system_lookalike(path: Path) -> Optional[Suspicion]:
    """Detecta nombres de procesos críticos del sistema fuera de su ubicación esperada."""
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
    """Ejecuta todos los chequeos heurísticos sobre un archivo dado."""
    if path is None:
        return []
        
    try:
        if not path.is_file():
            return []
    except (OSError, PermissionError, ValueError):
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
    """Escanea recursivamente un directorio buscando sospechas, saltando puntos de reparse."""
    if not directory:
        return []
        
    try:
        root: Path = Path(directory).resolve()
        if is_protected_path(root):
            return []
            
        results: List[Suspicion] = []
        if not root.exists() or not root.is_dir():
            return []
            
        stack: List[Path] = [root]
        while stack:
            current_dir = stack.pop()
            try:
                for entry in current_dir.iterdir():
                    if is_protected_path(entry) or entry.is_symlink() or _is_reparse_point(entry):
                        continue
                        
                    if entry.is_dir():
                        stack.append(entry)
                    elif entry.is_file():
                        results.extend(scan_file(entry))
            except (PermissionError, OSError):
                continue
        return results
    except (OSError, RuntimeError) as e:
        logger.error("Error crítico al inicializar el escaneo en %s: %s", directory, e)
        return []


def run_windows_defender_quick_scan() -> str:
    """Ejecuta un escaneo rápido con Windows Defender y retorna el resultado."""
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
