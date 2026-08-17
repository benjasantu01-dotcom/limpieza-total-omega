"""
scanner.py
Detector HEURÍSTICO de archivos sospechosos. Este módulo realiza un análisis 
estático mediante heurísticas de nombre, extensión y metadatos de archivo, 
complementando la protección de Windows Defender.

Señales heurísticas analizadas:
- Doble extensión engañosa (ej. "documento.pdf.exe")
- Ejecutables en carpetas de usuario (descargas, temp) creados recientemente
- Nombres de procesos críticos de sistema fuera de directorios protegidos
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
    Contiene la ubicación, el motivo del hallazgo y el nivel de criticidad.
    """
    path: Path
    reason: str
    severity: str

# Alias de tipo para las funciones que evalúan un archivo.
# Se espera que reciban la ruta, opcionalmente el objeto DirEntry para evitar stat adicional,
# y un timestamp de referencia para cálculos de antigüedad.
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]
ScanResult: TypeAlias = List[Suspicion]

# Expresiones regulares y constantes de configuración
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
SYSTEM32_LOWER: Final[str] = "system32"
WATCHED_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})
RECENT_FILE_THRESHOLD_HOURS: Final[int] = 24


class Scanner:
    """
    Controlador de estado para el escaneo recursivo del sistema de archivos.
    Gestiona la pila de directorios pendientes y el registro de rutas visitadas.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root = base_root.resolve()
        self.now_ts = datetime.now().timestamp()

    def _is_safe_entry(self, entry_path: Path) -> bool:
        """Verifica que la ruta resuelta esté contenida dentro del directorio base de escaneo."""
        try:
            resolved = entry_path.resolve()
            # Validar que sea subdirectorio o el mismo mediante comparación de padres
            return self.base_root == resolved or self.base_root in resolved.parents
        except (OSError, RuntimeError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """
        Consulta los atributos de archivo mediante syscall para detectar puntos de reanálisis.
        Evita seguir symlinks o junctions para prevenir bucles de recursión.
        """
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return True 

    def process_entry(self, entry: Optional[os.DirEntry], stack: List[str]) -> None:
        """
        Ejecuta el pipeline de filtrado y procesamiento para una entrada individual.
        Si la entrada es directorio, la agrega al stack de recorrido; si es archivo,
        ejecuta el análisis heurístico.
        """
        if entry is None or not hasattr(entry, 'path') or not entry.path:
            return
        
        try:
            target_path = Path(entry.path)
            
            if entry.is_symlink() or self._is_reparse_point(entry):
                return

            if is_protected_path(target_path) or str(target_path).startswith("\\\\"):
                return

            if not self._is_safe_entry(target_path):
                return

            if entry.is_dir(follow_symlinks=False):
                path_str = str(target_path)
                if path_str not in self.seen:
                    self.seen.add(path_str)
                    stack.append(path_str)
            elif entry.is_file(follow_symlinks=False):
                self.results.extend(scan_file(target_path, self.now_ts, entry=entry))
                
        except (PermissionError, OSError, FileNotFoundError, UnicodeDecodeError):
            pass


def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Evalúa si el nombre del archivo contiene una doble extensión sospechosa.
    Retorna un objeto Suspicion si coincide con DOUBLE_EXTENSION_RE, caso contrario None.
    """
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Verifica si un archivo ejecutable se encuentra en una carpeta monitorizada y fue modificado 
    recientemente. Requiere el objeto os.DirEntry para acceder a metadatos de tiempo sin realizar 
    llamadas adicionales al sistema. Retorna None si no es sospechoso o si faltan atributos.
    """
    if not isinstance(entry, os.DirEntry):
        return None
    
    if WATCHED_FOLDERS.isdisjoint(part.lower() for part in path.parts):
        return None
        
    try:
        file_stat = entry.stat()
        if (now_ts - file_stat.st_mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
            return Suspicion(path, f"Ejecutable reciente detectado (<{RECENT_FILE_THRESHOLD_HOURS}h)", "info")
    except (OSError, AttributeError, OverflowError, ValueError, TypeError):
        return None
    return None


def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Compara el nombre del archivo contra una lista negra de ejecutables del sistema.
    Marca como sospechoso cualquier coincidencia que no resida dentro de 'System32'.
    """
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        if SYSTEM32_LOWER not in [part.lower() for part in path.parts]:
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None


def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """
    Pipeline principal para el análisis de un archivo único. Ejecuta todas las 
    reglas registradas y retorna una lista acumulada de hallazgos (Suspicion).
    """
    if not path:
        return []

    findings: ScanResult = []
    
    if (res := check_double_extension(path, entry, now_ts)):
        findings.append(res)
    
    if path.suffix and path.suffix.lower() in SUSPICIOUS_EXECUTABLE_EXT:
        heuristic_suite: List[SuspicionCheck] = [check_system_lookalike, check_recent_executable_in_downloads]
        for check in heuristic_suite:
            try:
                if (res := check(path, entry, now_ts)):
                    findings.append(res)
            except Exception as e:
                logger.debug(f"Fallo inesperado en regla {check.__name__}: {e}")
                
    return findings


def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """
    Inicia el escaneo recursivo mediante un stack. Valida la existencia y 
    seguridad de la ruta de entrada antes de procesar recursivamente.
    """
    if not directory or not str(directory).strip():
        return []
        
    try:
        path_input = Path(directory).resolve(strict=True)
        if not path_input.is_dir() or is_protected_path(path_input):
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
                    scanner.process_entry(entry, stack)
        except (PermissionError, OSError, ValueError, RuntimeError):
            continue
            
    return scanner.results


def run_windows_defender_quick_scan() -> str:
    """Interacción externa con PowerShell para consultar estado y disparar escaneo de Defender."""
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
