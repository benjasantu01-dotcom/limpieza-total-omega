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
from safety import is_protected_path, is_safe_to_modify

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

# Alias de tipos para mejorar la legibilidad y mantenibilidad de la lógica de escaneo
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], Optional[str], Optional[str]], Optional[Suspicion]]
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

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return False

    def process_entry(self, entry: os.DirEntry, stack: List[str]) -> None:
        try:
            if entry is None or not entry.path:
                return
            
            # Verificación de integridad: asegurar que la entrada no es un symlink/reparse y sigue existiendo
            if entry.is_symlink():
                return
            
            path_obj = Path(entry.path)
            
            # Validar confinamiento estricto
            if self.base_root not in path_obj.resolve().parents and path_obj.resolve() != self.base_root:
                return

            if not is_safe_to_modify(path_obj) or is_protected_path(path_obj):
                return

            if entry.is_dir(follow_symlinks=False):
                if not self._is_reparse_point(entry):
                    if entry.path not in self.seen:
                        self.seen.add(entry.path)
                        stack.append(entry.path)
            elif entry.is_file(follow_symlinks=False):
                name = entry.name
                suffix = os.path.splitext(name)[1].lower()
                self.results.extend(scan_file(path_obj, entry=entry, name=name, suffix=suffix, prevalidated=True))
        except (PermissionError, OSError):
            pass


def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None) -> Optional[Suspicion]:
    if not path: return None
    target = name or path.name
    if target and DOUBLE_EXTENSION_RE.search(target):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None, hours: int = RECENT_FILE_THRESHOLD_HOURS) -> Optional[Suspicion]:
    if not path: return None
    try:
        st = entry.stat() if entry else path.lstat()
        mtime = datetime.fromtimestamp(st.st_mtime)
        if datetime.now() - mtime < timedelta(hours=hours):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    except (OSError, AttributeError, FileNotFoundError):
        pass
    return None


def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None) -> Optional[Suspicion]:
    if not path: return None
    try:
        if SYSTEM32_LOWER not in str(path.parent).lower():
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    except (OSError, RuntimeError):
        pass
    return None

CHECK_REGISTRY: Final[List[SuspicionCheck]] = [check_double_extension]

def scan_file(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None, prevalidated: bool = False) -> ScanResult:
    if not isinstance(path, Path):
        return []
        
    try:
        if not path.exists() or path.is_symlink():
            return []
    except (OSError, PermissionError):
        return []
    
    if not prevalidated:
        if not is_safe_to_modify(path) or is_protected_path(path):
            return []
    
    n = name or path.name
    s = suffix or path.suffix.lower()
    
    findings: ScanResult = []
    
    is_executable = s in SUSPICIOUS_EXECUTABLE_EXT
    is_lookalike = n.lower() in SYSTEM_LOOKALIKES
    
    if is_lookalike:
        if res := check_system_lookalike(path, entry, n, s): findings.append(res)
    
    if is_executable:
        if res := check_recent_executable_in_downloads(path, entry, n, s): findings.append(res)
        for check_func in CHECK_REGISTRY:
            try:
                if res := check_func(path, entry, n, s):
                    findings.append(res)
            except (OSError, AttributeError, TypeError):
                continue
            
    return findings


def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    if directory is None:
        return []
        
    try:
        path_input = Path(directory)
        if not path_input.exists():
            return []
        root_path = path_input.resolve()
        if not root_path.is_dir() or root_path.is_symlink() or is_protected_path(root_path) or not is_safe_to_modify(root_path):
            return []
    except (OSError, RuntimeError):
        return []

    scanner = Scanner(base_root=root_path)
    stack: List[str] = [str(root_path)]
    scanner.seen.add(str(root_path))
    
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
