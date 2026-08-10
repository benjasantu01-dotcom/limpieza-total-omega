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

@dataclass
class Suspicion:
    """
    Representa un hallazgo sospechoso detectado durante el escaneo.
    
    Attributes:
        path: Ruta absoluta del archivo analizado.
        reason: Descripción legible de por qué se considera sospechoso.
        severity: Nivel de criticidad ("info" para métricas, "warning" para alertas).
    """
    path: Path
    reason: str
    severity: str

# Alias de tipos para mejorar la legibilidad y mantenibilidad de la lógica de escaneo.
# Las funciones de chequeo deben ser puras: reciben contexto y devuelven una Suspicion si detectan algo.
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], Optional[str], Optional[str], float], Optional[Suspicion]]
ScanResult: TypeAlias = List[Suspicion]

# REGEX para detectar extensiones dobles donde la última es ejecutable
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)

# Conjuntos para búsquedas O(1)
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
SYSTEM32_LOWER: Final[str] = "system32"

RECENT_FILE_THRESHOLD_HOURS: Final[int] = 24


class Scanner:
    """
    Controlador de estado para el escaneo recursivo. Gestiona el rastreo de carpetas visitadas.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root = base_root.resolve()
        self.now_ts = datetime.now().timestamp()

    def _is_safe_entry(self, entry_path: str) -> bool:
        """Verifica que la entrada esté dentro del base_root definido."""
        if not entry_path:
            return False
        try:
            full_path = Path(entry_path).resolve()
            return self.base_root in full_path.parents or full_path == self.base_root
        except (RuntimeError, ValueError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si una entrada es un punto de reanálisis (Junction o Symlink)."""
        try:
            # 0x400 es FILE_ATTRIBUTE_REPARSE_POINT
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return False

    def process_entry(self, entry: os.DirEntry, stack: List[str]) -> None:
        """
        Valida y procesa una entrada del sistema de archivos.
        """
        if entry is None or stack is None:
            return
        
        try:
            if self._is_reparse_point(entry):
                return

            entry_path = entry.path
            if not entry_path or is_protected_path(Path(entry_path)) or not self._is_safe_entry(entry_path):
                return

            if entry.is_dir(follow_symlinks=False):
                if entry_path not in self.seen:
                    self.seen.add(entry_path)
                    stack.append(entry_path)
            elif entry.is_file(follow_symlinks=False):
                path_obj = Path(entry_path)
                name = entry.name
                _, ext = os.path.splitext(name)
                self.results.extend(scan_file(path_obj, self.now_ts, entry=entry, name=name, suffix=ext.lower()))
        except (PermissionError, OSError, ValueError, RuntimeError) as e:
            logger.debug(f"Saltando entrada {getattr(entry, 'path', 'desconocida')}: {e}")


def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Identifica archivos con extensiones dobles engañosas."""
    target = name or (path.name if path else None)
    if target and DOUBLE_EXTENSION_RE.search(target):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Detecta ejecutables modificados recientemente según umbral de tiempo."""
    if entry is None or path is None:
        return None
    try:
        mtime = entry.stat(follow_symlinks=False).st_mtime
        if (now_ts - mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {RECENT_FILE_THRESHOLD_HOURS}h)", "info")
    except (OSError, AttributeError, OverflowError):
        pass
    return None


def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Busca ejecutables que usurpan nombres de procesos críticos del sistema."""
    if path is None:
        return None
    target = (name or path.name or "").lower()
    if target in SYSTEM_LOOKALIKES:
        try:
            if SYSTEM32_LOWER not in str(path.parent).lower():
                return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
        except (RuntimeError, ValueError) as e:
            logger.debug(f"Error analizando ruta de padre para {path}: {e}")
    return None

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None) -> ScanResult:
    """Ejecuta el conjunto de heurísticas sobre un archivo."""
    if path is None:
        return []
        
    findings: ScanResult = []
    n = name or path.name
    s = suffix or path.suffix.lower()
    
    if (res := check_double_extension(path, entry, n, s, now_ts)):
        findings.append(res)
    
    if s in SUSPICIOUS_EXECUTABLE_EXT:
        if (res := check_recent_executable_in_downloads(path, entry, n, s, now_ts)):
            findings.append(res)
        if (res := check_system_lookalike(path, entry, n, s, now_ts)):
            findings.append(res)
                
    return findings


def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Inicia el escaneo recursivo de directorios."""
    if directory is None:
        return []
        
    try:
        path_input = Path(directory).resolve()
        if not path_input.exists() or not path_input.is_dir() or is_protected_path(path_input):
            return []
    except (OSError, RuntimeError, TypeError, ValueError):
        return []

    scanner = Scanner(base_root=path_input)
    stack: List[str] = [str(path_input)]
    scanner.seen.add(str(path_input))
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    scanner.process_entry(entry, stack)
        except (PermissionError, OSError, ValueError, RuntimeError) as e:
            logger.warning(f"No se pudo acceder al directorio {current_dir}: {e}")
            continue
            
    return scanner.results


def run_windows_defender_quick_scan() -> str:
    """Interfaz con las herramientas de seguridad nativas de Windows (Defender)."""
    try:
        status = subprocess.run(
            ["powershell", "-Command", "Get-MpComputerStatus | Select-Object -ExpandProperty RealTimeProtectionEnabled"],
            capture_output=True, text=True, timeout=10
        )
        if status.stdout.strip() != "True":
            return "Protección en tiempo real desactivada. Escaneo omitido."
            
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
