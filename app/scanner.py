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
from typing import List, Optional, Union
from app.safety import ensure_safe_to_modify

# Configuración de logger para el módulo
logger = logging.getLogger(__name__)

DOUBLE_EXTENSION_RE = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
SUSPICIOUS_EXECUTABLE_EXT = {".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"}
SYSTEM_LOOKALIKES = {"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"}
SYSTEM32_LOWER = "system32"


@dataclass
class Suspicion:
    """Representa una hallazgo sospechoso detectado durante el escaneo."""
    path: Path
    reason: str
    severity: str  # "info" | "warning"


def check_double_extension(path: Path) -> Optional[Suspicion]:
    """
    Analiza si el nombre del archivo intenta ocultar una extensión ejecutable 
    tras una extensión de documento común, técnica usada para engañar al usuario.
    """
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, hours: int = 24) -> Optional[Suspicion]:
    """
    Evalúa si un ejecutable es reciente basándose en su fecha de modificación. 
    Los ejecutables descargados recientemente tienen mayor riesgo de ser malintencionados.
    """
    if path.suffix.lower() not in SUSPICIOUS_EXECUTABLE_EXT:
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
    except (FileNotFoundError, PermissionError, OSError):
        return None
    
    if datetime.now() - mtime < timedelta(hours=hours):
        return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    return None


def check_system_lookalike(path: Path) -> Optional[Suspicion]:
    """
    Detecta archivos con nombres de procesos críticos del sistema que operan fuera 
    de System32, lo cual suele ser indicativo de técnicas de persistencia maliciosa.
    """
    if path.name.lower() in SYSTEM_LOOKALIKES and SYSTEM32_LOWER not in str(path.parent).lower():
        return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None


def scan_file(path: Path) -> List[Suspicion]:
    """
    Ejecuta el conjunto de chequeos heurísticos sobre un archivo individual.
    """
    try:
        if not path.is_file():
            return []
    except (OSError, PermissionError):
        return []

    results: List[Suspicion] = []
    res = check_double_extension(path)
    if res: results.append(res)
    
    res = check_recent_executable_in_downloads(path)
    if res: results.append(res)
    
    res = check_system_lookalike(path)
    if res: results.append(res)
    
    return results


def scan_directory(directory: Union[str, Path]) -> List[Suspicion]:
    """
    Escanea recursivamente un directorio en busca de comportamientos sospechosos.
    Verifica seguridad de la ruta antes de procesar cada archivo detectado.
    """
    if not directory:
        return []
        
    try:
        root = Path(directory).resolve()
        ensure_safe_to_modify(root)
        
        results: List[Suspicion] = []
        if not root.exists() or not root.is_dir():
            return []
            
        # Uso de pila para evitar la sobrecarga de rglob y optimizar recursos en recorridos profundos
        queue = [root]
        while queue:
            current_dir = queue.pop()
            try:
                for entry in current_dir.iterdir():
                    if entry.is_dir():
                        if not entry.is_symlink():
                            queue.append(entry)
                    elif entry.is_file():
                        results.extend(scan_file(entry))
            except (PermissionError, OSError):
                continue
        return results
    except Exception as e:
        logger.error("Error crítico al inicializar el escaneo en %s: %s", directory, e)
        return []


def run_windows_defender_quick_scan() -> str:
    """
    Dispara un escaneo rápido con Windows Defender mediante PowerShell.
    Requiere que el entorno tenga acceso a los cmdlets de Defender.
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
