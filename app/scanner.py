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

# Alias de tipos para mejorar la legibilidad y mantenibilidad de la lógica de escaneo
# Los chequeos deben ser funciones puras que retornan un objeto Suspicion si hay hallazgo, o None.
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
        if entry is None or not entry.path:
            return
        
        try:
            # Validar si el path es válido antes de resolver
            path_obj = Path(entry.path).resolve()
            
            # Validación de seguridad defensiva: no salir de la raíz base ni entrar en rutas protegidas
            if not str(path_obj).startswith(str(self.base_root)) or is_protected_path(path_obj):
                return

            if entry.is_dir(follow_symlinks=False):
                if not self._is_reparse_point(entry) and entry.path not in self.seen:
                    self.seen.add(entry.path)
                    stack.append(entry.path)
            elif entry.is_file(follow_symlinks=False):
                # Verificar existencia real antes de procesar para evitar I/O race conditions
                if not path_obj.exists():
                    return
                name = entry.name
                suffix = os.path.splitext(name)[1].lower()
                self.results.extend(scan_file(path_obj, entry=entry, name=name, suffix=suffix))
        except (PermissionError, OSError, RuntimeError) as e:
            logger.debug(f"Acceso denegado o error en entrada {entry.path}: {e}")


def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None) -> Optional[Suspicion]:
    target = name or path.name
    if target and DOUBLE_EXTENSION_RE.search(target):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None, hours: int = RECENT_FILE_THRESHOLD_HOURS) -> Optional[Suspicion]:
    try:
        # Uso de entry.stat si está disponible, es más eficiente y seguro que path.lstat()
        st = entry.stat() if entry else path.stat()
        mtime = st.st_mtime
        if mtime <= 0: return None
        if datetime.now() - datetime.fromtimestamp(mtime) < timedelta(hours=hours):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    except (OSError, AttributeError, ValueError, OverflowError):
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

def _run_checks(checks: List[SuspicionCheck], *args) -> ScanResult:
    """Ejecuta una lista de funciones de chequeo y recolecta las sospechas encontradas."""
    findings: ScanResult = []
    for check_func in checks:
        if res := check_func(*args):
            findings.append(res)
    return findings

def scan_file(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None) -> ScanResult:
    """Aplica el set completo de heurísticas a un archivo según su tipo."""
    n = name or path.name
    s = suffix or path.suffix.lower()
    
    # Aplicar heurísticas universales
    findings = _run_checks(CHECK_REGISTRY["all"], path, entry, n, s)
    
    # Aplicar heurísticas de ejecutables si el archivo es potencialmente ejecutable
    if s in SUSPICIOUS_EXECUTABLE_EXT:
        findings.extend(_run_checks(CHECK_REGISTRY["exec"], path, entry, n, s))
            
    return findings


def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    if directory is None:
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
        except (PermissionError, OSError) as e:
            logger.warning(f"No se pudo acceder a {current_dir}: {e}")
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
