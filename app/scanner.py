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
        """Determina si una entrada es un punto de reanálisis (Junction o Symlink) para evitar bucles infinitos."""
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return False

    def process_entry(self, entry: os.DirEntry, stack: List[str]) -> None:
        """
        Valida y procesa una entrada del sistema de archivos. 
        Si es directorio, lo agrega al stack de recorrido; si es archivo, ejecuta las heurísticas.
        """
        if not entry or not entry.path:
            return
        
        try:
            # Validación de seguridad defensiva inicial
            if is_protected_path(Path(entry.path)):
                return

            if entry.is_dir(follow_symlinks=False):
                if not self._is_reparse_point(entry) and entry.path not in self.seen:
                    self.seen.add(entry.path)
                    stack.append(entry.path)
            elif entry.is_file(follow_symlinks=False):
                path_obj = Path(entry.path)
                # Solo realizar el chequeo pesado de is_safe_to_modify si es un archivo sospechoso
                name = entry.name
                suffix = os.path.splitext(name)[1].lower()
                self.results.extend(scan_file(path_obj, entry=entry, name=name, suffix=suffix, prevalidated=False))
        except (PermissionError, OSError, RuntimeError, FileNotFoundError):
            pass


def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None) -> Optional[Suspicion]:
    target = name or path.name
    if target and DOUBLE_EXTENSION_RE.search(target):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None, hours: int = RECENT_FILE_THRESHOLD_HOURS) -> Optional[Suspicion]:
    try:
        st = entry.stat() if entry else path.lstat()
        if datetime.now() - datetime.fromtimestamp(st.st_mtime) < timedelta(hours=hours):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    except (OSError, AttributeError, FileNotFoundError, OverflowError):
        pass
    return None


def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None) -> Optional[Suspicion]:
    try:
        target = name or path.name
        if target.lower() in SYSTEM_LOOKALIKES and SYSTEM32_LOWER not in str(path.parent).lower():
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    except (OSError, RuntimeError, AttributeError):
        pass
    return None

# Registro clasificado para optimización de rendimiento
CHECK_REGISTRY: Final[dict[str, List[SuspicionCheck]]] = {
    "all": [check_double_extension],
    "exec": [check_recent_executable_in_downloads, check_system_lookalike]
}

def scan_file(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None, prevalidated: bool = False) -> ScanResult:
    if not prevalidated:
        if is_protected_path(path) or not is_safe_to_modify(path):
            return []
    
    n = name or path.name
    s = suffix or path.suffix.lower()
    findings: ScanResult = []
    
    # Aplicar heurísticas universales
    for check_func in CHECK_REGISTRY["all"]:
        if res := check_func(path, entry, n, s):
            findings.append(res)
    
    # Aplicar heurísticas de ejecutables solo si corresponde
    if s in SUSPICIOUS_EXECUTABLE_EXT:
        for check_func in CHECK_REGISTRY["exec"]:
            if res := check_func(path, entry, n, s):
                findings.append(res)
            
    return findings


def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    if not directory:
        return []
        
    try:
        path_input = Path(directory).resolve()
        if not path_input.exists() or not path_input.is_dir() or is_protected_path(path_input):
            return []
    except (OSError, RuntimeError, TypeError):
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
