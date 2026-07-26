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

# Configuración de logger para el módulo
logger = logging.getLogger(__name__)

DOUBLE_EXTENSION_RE = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
SUSPICIOUS_EXECUTABLE_EXT = {".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"}
SYSTEM_LOOKALIKES = {"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"}


@dataclass
class Suspicion:
    """Representa una hallazgo sospechoso detectado durante el escaneo."""
    path: Path
    reason: str
    severity: str  # "info" | "warning"


def check_double_extension(path: Path) -> Suspicion | None:
    """Verifica si el nombre de archivo sugiere una extensión doble sospechosa."""
    if not path or not path.name:
        return None
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, hours: int = 24) -> Suspicion | None:
    """Verifica si el archivo ejecutable fue modificado en las últimas horas."""
    if not path or path.suffix.lower() not in SUSPICIOUS_EXECUTABLE_EXT:
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
    except (FileNotFoundError, PermissionError, OSError) as e:
        logger.debug("No se pudo acceder a los metadatos de %s: %s", path, e)
        return None
    
    if datetime.now() - mtime < timedelta(hours=hours):
        return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    return None


def check_system_lookalike(path: Path) -> Suspicion | None:
    """Verifica si el nombre imita archivos de sistema fuera de su ubicación legítima."""
    if not path or not path.name:
        return None
    if path.name.lower() in SYSTEM_LOOKALIKES and "system32" not in str(path.parent).lower():
        return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None


def scan_file(path: Path) -> list[Suspicion]:
    """Ejecuta todos los chequeos heurísticos sobre un archivo dado."""
    if not isinstance(path, Path):
        return []
    
    checks = [check_double_extension, check_recent_executable_in_downloads, check_system_lookalike]
    results = []
    
    for check in checks:
        try:
            r = check(path)
            if r:
                results.append(r)
        except Exception as e:
            logger.error("Error inesperado en chequeo %s sobre archivo %s: %s", check.__name__, path, e)
    return results


def scan_directory(directory: str | Path) -> list[Suspicion]:
    """
    Escanea recursivamente un directorio en busca de sospechas.
    
    Args:
        directory: La ruta raíz donde comenzar el escaneo (puede ser str o Path).
        
    Returns:
        Lista de objetos Suspicion encontrados. Retorna una lista vacía si el 
        directorio es inválido o si no se encuentran amenazas.
    """
    if not directory:
        return []
        
    results = []
    try:
        root = Path(directory).resolve()
        if not root.exists():
            logger.warning("El path proporcionado '%s' no existe.", directory)
            return []
        if not root.is_dir():
            logger.warning("El path proporcionado '%s' no es un directorio.", directory)
            return []
            
        for p in root.rglob("*"):
            try:
                # Seguridad: Resolver ruta para evitar escapes mediante enlaces simbólicos
                # y verificar que se mantenga dentro del árbol de la raíz del escaneo.
                resolved_p = p.resolve()
                if not any(part == ".." for part in p.parts) and root in resolved_p.parents:
                    if p.is_symlink():
                        continue
                    if p.is_file():
                        results.extend(scan_file(p))
            except (PermissionError, OSError) as e:
                logger.debug("Acceso denegado o error de sistema al procesar %s: %s", p, e)
                continue
    except (TypeError, ValueError) as e:
        logger.error("Error al inicializar la ruta de escaneo %s: %s", directory, e)
    except OSError as e:
        logger.error("Error crítico al acceder al directorio base %s: %s", directory, e)
            
    return results


def run_windows_defender_quick_scan() -> str:
    """
    Dispara un escaneo rápido con Windows Defender mediante PowerShell.
    
    Nota: Requiere privilegios de administrador para ser efectivo según la política
    de ejecución de PowerShell del sistema.
    
    Returns:
        String conteniendo la salida estándar del comando o un mensaje de error
        si el proceso falla o no está soportado.
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
