"""
scanner.py
Detector HEURÍSTICO de archivos sospechosos. Este módulo realiza un análisis 
estático mediante heurísticas de nombre, extensión y metadatos de archivo, 
complementando la protección de Windows Defender.
"""

from __future__ import annotations
import subprocess
import re
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Union, Final, Callable, TypeAlias
from safety import is_protected_path

# Configuración de logger para el módulo
logger = logging.getLogger(__name__)

@dataclass
class Suspicion:
    """
    Representa un hallazgo sospechoso detectado durante el escaneo.

    Attributes:
        path: Ruta completa del archivo analizado.
        reason: Descripción breve del motivo de sospecha.
        severity: Nivel de criticidad ('info', 'warning', 'critical').
    """
    path: Path
    reason: str
    severity: str

# Alias para funciones que evalúan un archivo.
# Una 'SuspicionCheck' debe ser una función pura que reciba la ruta, 
# una entrada de directorio opcional para optimización y un timestamp base.
# Retorna un objeto Suspicion si se detecta riesgo, o None en caso contrario.
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]

# Alias para representar una colección de hallazgos durante un proceso de escaneo.
ScanResult: TypeAlias = List[Suspicion]

# Expresiones regulares para detección de ofuscación
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)

# Conjuntos de constantes para comparación rápida
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
WATCHED_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})

# Configuración de umbrales
SYSTEM32_LOWER: Final[str] = "system32"
RECENT_FILE_THRESHOLD_HOURS: Final[int] = 24
MAX_PATH_LENGTH: Final[int] = 260
# Constante de Windows para FILE_ATTRIBUTE_REPARSE_POINT (0x400)
WIN_FILE_ATTR_REPARSE_POINT: Final[int] = 0x400

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Evalúa si el nombre del archivo contiene una doble extensión maliciosa."""
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Verifica si un ejecutable fue modificado recientemente en carpetas monitoreadas.
    Utiliza el timestamp actual (now_ts) para evitar llamadas a disco redundantes por cada regla.
    """
    if any(part.lower() in WATCHED_FOLDERS for part in path.parts):
        try:
            # Validar existencia antes de consultar metadatos
            if entry and not entry.is_file():
                return None
            stats = entry.stat(follow_symlinks=False) if entry else path.stat()
            if (now_ts - stats.st_mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
                return Suspicion(path, f"Ejecutable reciente detectado (<{RECENT_FILE_THRESHOLD_HOURS}h)", "info")
        except (OSError, AttributeError, ValueError):
            return None
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Verifica si un ejecutable intenta suplantar procesos críticos fuera de System32."""
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        if is_protected_path(path):
            return None
        if SYSTEM32_LOWER not in str(path).lower():
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None

# Registro de reglas heurísticas para ejecutables
EXECUTABLE_CHECK_REGISTRY: Final[List[SuspicionCheck]] = [
    check_system_lookalike,
    check_recent_executable_in_downloads
]

class Scanner:
    """
    Gestiona el estado del escaneo y la navegación recursiva del sistema de archivos.
    
    Attributes:
        results: Lista acumulativa de hallazgos (Suspicion).
        seen: Conjunto de rutas ya procesadas para prevenir recursión infinita en ciclos.
        base_root: Ruta absoluta de inicio definida para limitar el alcance del escaneo.
        now_ts: Timestamp capturado al inicio para consistencia temporal.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root = base_root.resolve()
        self.now_ts: float = datetime.now().timestamp()

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """
        Valida si una entrada es apta para el análisis.
        Rechaza explícitamente: rutas UNC, nombres reservados, rutas fuera del árbol base_root
        y puntos de reanálisis.
        """
        if not entry or not entry.path:
            return False
        
        try:
            path_obj = Path(entry.path).resolve()
            
            if len(str(path_obj)) > MAX_PATH_LENGTH or entry.path.startswith("\\\\"):
                return False
            
            if entry.name and RESERVED_NAMES_RE.match(entry.name):
                return False

            # Verificación estricta de subdirectorio
            try:
                path_obj.relative_to(self.base_root)
            except ValueError:
                return False
            
            if self._is_reparse_point(entry):
                return False

            return not is_protected_path(path_obj)
        except (ValueError, RuntimeError, OSError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """
        Detecta si una entrada es un punto de reanálisis (junction/symlink).
        """
        try:
            is_sym = entry.is_symlink()
            attr = entry.stat(follow_symlinks=False).st_file_attributes
            return is_sym or bool(attr & WIN_FILE_ATTR_REPARSE_POINT)
        except (OSError, AttributeError, TypeError):
            return True 

    def _handle_directory(self, entry: os.DirEntry, stack: List[str]) -> None:
        """Extrae la lógica de apilado de directorios para modularizar la navegación."""
        if entry.path not in self.seen:
            self.seen.add(entry.path)
            stack.append(entry.path)

    def process_entry(self, entry: os.DirEntry, stack: List[str]) -> None:
        """
        Gestiona la lógica de recursión y filtrado inicial de archivos.
        """
        try:
            if not self._is_safe_entry(entry):
                return
            if entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, stack)
            elif entry.is_file(follow_symlinks=False):
                name = entry.name
                _, ext = os.path.splitext(name)
                ext_low = ext.lower()
                if ext_low in SUSPICIOUS_EXECUTABLE_EXT or ext_low == ".pdf":
                    self._run_file_heuristics(Path(entry.path), entry, ext_low)
        except (OSError, PermissionError, TypeError):
            logger.debug(f"Acceso denegado o entrada inválida {getattr(entry, 'path', 'unknown')}")

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry, ext: str) -> None:
        """
        Coordina las pruebas de ofuscación de nombres y las reglas registradas.
        """
        if path.name and RTL_CHAR_RE.search(path.name):
            self.results.append(Suspicion(path, "Nombre de archivo contiene caracteres de control de ofuscación (RTL)", "critical"))
        self.results.extend(scan_file(path, self.now_ts, entry=entry, ext=ext))

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None, ext: Optional[str] = None) -> ScanResult:
    """
    Ejecuta todas las reglas heurísticas registradas sobre un archivo.
    """
    findings: ScanResult = []
    
    if (double_ext := check_double_extension(path, entry, now_ts)):
        findings.append(double_ext)
    
    file_ext = ext or path.suffix.lower()
    if file_ext in SUSPICIOUS_EXECUTABLE_EXT:
        for check in EXECUTABLE_CHECK_REGISTRY:
            try:
                if (result := check(path, entry, now_ts)):
                    findings.append(result)
            except (Exception, AttributeError) as e:
                logger.debug(f"Fallo no crítico en regla {check.__name__} para {path}: {e}")
    return findings

def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """
    Punto de entrada principal para escaneo recursivo. Implementa una estructura
    de pila (LIFO) para evitar la profundidad de recursión del sistema.
    """
    if not directory:
        return []
    try:
        path_input: Path = Path(directory).resolve()
        if not path_input.exists() or not path_input.is_dir() or is_protected_path(path_input):
            return []
    except (OSError, TypeError, ValueError, RuntimeError):
        return []

    scanner = Scanner(base_root=path_input)
    stack: List[str] = [str(path_input)]
    scanner.seen.add(str(path_input))
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if entry:
                        scanner.process_entry(entry, stack)
        except (PermissionError, OSError):
            logger.debug(f"Error accediendo a directorio {current_dir}")
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """
    Invoca la API de PowerShell para verificar el estado de Defender y ejecutar 
    un escaneo rápido (QuickScan).
    """
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
    except (FileNotFoundError, OSError):
        return "PowerShell no disponible. Este módulo requiere Windows."
    except subprocess.TimeoutExpired:
        return "El escaneo de Windows Defender excedió el tiempo límite."
