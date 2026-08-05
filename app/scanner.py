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
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry]], Optional[Suspicion]]
ScanResult: TypeAlias = List[Suspicion]

# REGEX para detectar extensiones dobles donde la última es ejecutable,
# evitando falsos positivos de archivos comunes.
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)

# Lista de extensiones que, al encontrarse en carpetas no protegidas, ameritan una revisión heurística.
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})

# Nombres de archivos binarios críticos que, si aparecen fuera de System32, son indicadores de suplantación.
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
SYSTEM32_LOWER: Final[str] = "system32"

# Margen de tiempo para considerar un ejecutable como "reciente" (posible descarga o amenaza activa).
RECENT_FILE_THRESHOLD_HOURS: Final[int] = 24


class Scanner:
    """
    Controlador de estado para el escaneo recursivo.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root = base_root

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si una entrada del sistema de archivos es un punto de reanálisis (Junction/Symlink)."""
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return False

    def process_entry(self, entry: os.DirEntry, stack: List[str]) -> None:
        """Procesa una entrada del directorio, filtrando rutas protegidas y analizando archivos."""
        try:
            path_obj = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                if not self._is_reparse_point(entry):
                    if not is_safe_to_modify(path_obj) or is_protected_path(path_obj):
                        return
                    path_key = str(path_obj)
                    if path_key not in self.seen:
                        self.seen.add(path_key)
                        stack.append(entry.path)
            elif entry.is_file(follow_symlinks=False):
                if is_safe_to_modify(path_obj) and not is_protected_path(path_obj):
                    self.results.extend(scan_file(path_obj, entry=entry, prevalidated=True))
        except (PermissionError, OSError):
            pass


def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None) -> Optional[Suspicion]:
    """Valida si el archivo posee extensiones dobles engañosas (ej: .pdf.exe)."""
    if path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, hours: int = RECENT_FILE_THRESHOLD_HOURS) -> Optional[Suspicion]:
    """Evalúa si un archivo ejecutable fue modificado recientemente usando metadatos del DirEntry."""
    try:
        st = entry.stat() if entry else path.lstat()
        mtime = datetime.fromtimestamp(st.st_mtime)
        if datetime.now() - mtime < timedelta(hours=hours):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    except (OSError, AttributeError, FileNotFoundError):
        pass
    return None


def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None) -> Optional[Suspicion]:
    """Detecta archivos con nombres de procesos críticos del sistema fuera del directorio System32."""
    try:
        parent = path.parent
        if parent and SYSTEM32_LOWER not in str(parent).lower():
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    except (OSError, RuntimeError):
        pass
    return None

# Registro de heurísticas para desacoplar la ejecución de los chequeos de la función scan_file
CHECK_REGISTRY: Final[List[tuple[Callable[[Path], bool], SuspicionCheck]]] = [
    (lambda p: p.name.lower() in SYSTEM_LOOKALIKES, check_system_lookalike),
    (lambda p: p.suffix.lower() in SUSPICIOUS_EXECUTABLE_EXT, check_recent_executable_in_downloads),
    (lambda p: bool(DOUBLE_EXTENSION_RE.search(p.name)), check_double_extension)
]

def scan_file(path: Path, entry: Optional[os.DirEntry] = None, prevalidated: bool = False) -> ScanResult:
    """
    Ejecuta todos los chequeos heurísticos registrados contra un archivo específico.
    """
    if not isinstance(path, Path):
        return []
    
    if not prevalidated:
        if not is_safe_to_modify(path) or is_protected_path(path):
            return []
    
    findings: ScanResult = []
    
    for condition_met, check_func in CHECK_REGISTRY:
        if condition_met(path):
            result = check_func(path, entry)
            if result:
                findings.append(result)
            
    return findings


def scan_directory(directory: Union[str, Path]) -> ScanResult:
    """Realiza un escaneo recursivo de un directorio, recolectando hallazgos sospechosos."""
    if not directory:
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
    root_str = str(root_path)
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
    """Invoca la herramienta de escaneo rápido de Windows Defender mediante PowerShell."""
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
