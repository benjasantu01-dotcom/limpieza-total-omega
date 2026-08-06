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
from typing import List, Optional, Union, Final, Callable, TypeAlias, Tuple
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
# SuspicionCheck: Ejecuta la heurística específica si la condición previa se cumple.
# ConditionCheck: Filtra rápidamente si un archivo debe ser sometido a un chequeo.
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], Optional[str], Optional[str]], Optional[Suspicion]]
ConditionCheck: TypeAlias = Callable[[Path, str, str], bool]
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
    Controlador de estado para el escaneo recursivo. Gestiona el rastreo de carpetas visitadas.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root_str = str(base_root.resolve())

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina si una entrada del sistema de archivos es un punto de reanálisis (Junction/Symlink)."""
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return False

    def process_entry(self, entry: os.DirEntry, stack: List[str]) -> None:
        """Procesa una entrada del directorio, filtrando rutas protegidas y analizando archivos."""
        try:
            if entry is None or not entry.path:
                return
            
            path_obj = Path(entry.path)
            # Defensa: Verificar que la ruta real no escape del base_root mediante symlinks
            if not str(path_obj.resolve()).startswith(self.base_root_str):
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
    """Analiza si el nombre del archivo contiene extensiones anidadas engañosas."""
    target = name or (path.name if path else "")
    if target and DOUBLE_EXTENSION_RE.search(target):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None, hours: int = RECENT_FILE_THRESHOLD_HOURS) -> Optional[Suspicion]:
    """Evalúa si el archivo ejecutable fue creado/modificado recientemente mediante metadatos."""
    try:
        st = entry.stat() if entry else path.lstat()
        mtime = datetime.fromtimestamp(st.st_mtime)
        if datetime.now() - mtime < timedelta(hours=hours):
            return Suspicion(path, f"Ejecutable reciente detectado (modificado hace menos de {hours}h)", "info")
    except (OSError, AttributeError, FileNotFoundError):
        pass
    return None


def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None) -> Optional[Suspicion]:
    """Verifica si el archivo intenta suplantar nombres de procesos críticos del sistema."""
    try:
        if path and SYSTEM32_LOWER not in str(path.parent).lower():
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    except (OSError, RuntimeError):
        pass
    return None

# Registro de heurísticas: Lista de tuplas (Condición, Función de Chequeo).
CHECK_REGISTRY: Final[List[Tuple[ConditionCheck, SuspicionCheck]]] = [
    (lambda p, n, s: n.lower() in SYSTEM_LOOKALIKES, check_system_lookalike),
    (lambda p, n, s: s in SUSPICIOUS_EXECUTABLE_EXT, check_recent_executable_in_downloads),
    (lambda p, n, s: bool(DOUBLE_EXTENSION_RE.search(n)), check_double_extension)
]

def scan_file(path: Path, entry: Optional[os.DirEntry] = None, name: Optional[str] = None, suffix: Optional[str] = None, prevalidated: bool = False) -> ScanResult:
    """
    Ejecuta el conjunto de heurísticas registradas sobre un archivo.
    
    Args:
        path: Objeto Path del archivo.
        prevalidated: Si es True, omite los chequeos de seguridad (is_safe_to_modify) 
                      asumiendo que ya fueron realizados por el llamador (ej: Scanner).
    """
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
    
    # Derivación diferida de metadatos si no se proveyeron en la llamada
    n = name or path.name
    s = suffix or path.suffix.lower()
    
    findings: ScanResult = []
    for condition_met, check_func in CHECK_REGISTRY:
        try:
            if condition_met(path, n, s):
                result = check_func(path, entry, n, s)
                if result:
                    findings.append(result)
        except Exception:
            continue
            
    return findings


def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """Realiza un escaneo recursivo desde un directorio raíz, recolectando hallazgos."""
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
        # Verificar estado previo para evitar fallos de ejecución
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
