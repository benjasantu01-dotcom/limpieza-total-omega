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
# Una función de chequeo debe recibir una ruta absoluta y retornar una sospecha o None
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


class Scanner:
    """Encapsula el estado y la lógica de recorrido del sistema de archivos."""
    
    def __init__(self) -> None:
        self.results: List[Suspicion] = []
        self.seen: set[str] = set()

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Verifica si la entrada es un enlace simbólico o junction para evitar bucles."""
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return False

    def process_entry(self, entry: os.DirEntry, stack: List[str]) -> None:
        """
        Analiza una entrada: si es directorio, lo apila; si es archivo, ejecuta los checks.
        """
        try:
            if entry.is_dir(follow_symlinks=False):
                if not self._is_reparse_point(entry):
                    path_str = entry.path
                    if path_str:
                        resolved_path = Path(path_str).resolve()
                        if str(resolved_path) not in self.seen and not is_protected_path(resolved_path):
                            self.seen.add(str(resolved_path))
                            stack.append(path_str)
            elif entry.is_file():
                path_obj = Path(entry.path).resolve()
                if not is_protected_path(path_obj):
                    self.results.extend(scan_file(path_obj))
        except (PermissionError, OSError):
            pass


def check_double_extension(path: Path) -> Optional[Suspicion]:
    """Detecta archivos con doble extensión que intentan engañar al usuario."""
    if path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, hours: int = RECENT_FILE_THRESHOLD_HOURS) -> Optional[Suspicion]:
    """Detecta ejecutables nuevos; su presencia reciente suele ser un indicador de riesgo."""
    if path.suffix.lower() not in SUSPICIOUS_EXECUTABLE_EXT:
        return None
        
    try:
        # Verificamos estado actual antes de obtener atributos
        if not path.exists():
            return None
        mtime = datetime.fromtimestamp(path.stat(follow_symlinks=False).st_mtime)
        if datetime.now() - mtime < timedelta(hours=hours):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return None


def check_system_lookalike(path: Path) -> Optional[Suspicion]:
    """Detecta ejecutables que suplantan nombres de procesos críticos fuera de System32."""
    if path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        try:
            # Validamos que el parent sea accesible
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
    """Ejecuta los tests definidos en CHECK_FUNCS sobre un archivo individual."""
    if not path:
        return []
        
    try:
        resolved_path = path.resolve()
        if is_protected_path(resolved_path) or not resolved_path.exists():
            return []
            
        findings: List[Suspicion] = []
        for check_func in CHECK_FUNCS:
            try:
                res = check_func(resolved_path)
                if res:
                    findings.append(res)
            except (PermissionError, OSError):
                continue
        return findings
    except (RuntimeError, OSError):
        return []


def scan_directory(directory: Union[str, Path]) -> List[Suspicion]:
    """Realiza el escaneo recursivo iterativo utilizando la clase Scanner para mantener estado."""
    if not directory:
        return []
        
    path_obj = Path(directory).resolve()
    if not path_obj.exists() or not path_obj.is_dir() or is_protected_path(path_obj):
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
