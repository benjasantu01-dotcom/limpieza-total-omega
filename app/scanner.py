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
            # Verifica si la ruta es la base o está dentro de ella, manejando casos de path traversal
            return self.base_root == resolved or self.base_root in resolved.parents
        except (RuntimeError, ValueError, OSError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """
        Consulta los atributos de archivo mediante syscall para detectar puntos de reanálisis.
        Evita seguir symlinks o junctions para prevenir bucles de recursión.
        """
        try:
            # 0x400 es el bit de FILE_ATTRIBUTE_REPARSE_POINT en Windows
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
            
            # Filtros de seguridad iniciales: symlinks, reparse points y directorios protegidos
            if entry.is_symlink() or self._is_reparse_point(entry):
                return

            if is_protected_path(target_path) or str(target_path).startswith("\\\\"):
                return

            # Validar integridad contra base_root antes de proceder
            if not self._is_safe_entry(target_path):
                return

            if entry.is_dir(follow_symlinks=False):
                path_str = str(target_path)
                if path_str not in self.seen:
                    self.seen.add(path_str)
                    stack.append(path_str)
            elif entry.is_file(follow_symlinks=False):
                self.results.extend(scan_file(target_path, self.now_ts, entry=entry))
                
        except (PermissionError, OSError, FileNotFoundError, UnicodeDecodeError) as e:
            logger.debug(f"Acceso denegado, error de sistema o codificación en {getattr(entry, 'path', 'unknown')}: {e}")


def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Identifica archivos que utilizan doble extensión para ocultar ejecutables sospechosos."""
    if path is None or path.name is None:
        return None
    try:
        if DOUBLE_EXTENSION_RE.search(path.name):
            return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    except Exception:
        pass
    return None


def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Analiza si un ejecutable fue creado en carpetas de alta descarga recientemente.
    Se basa en st_mtime de los metadatos del sistema de archivos.
    """
    if not isinstance(entry, os.DirEntry) or path is None:
        return None
    
    # Optimización: buscar coincidencia directa sin crear sets nuevos por archivo
    if not any(part.lower() in WATCHED_FOLDERS for part in path.parts):
        return None
        
    try:
        file_stat = entry.stat()
        if (now_ts - file_stat.st_mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
            return Suspicion(path, f"Ejecutable reciente detectado (<{RECENT_FILE_THRESHOLD_HOURS}h)", "info")
    except (OSError, AttributeError, OverflowError, ValueError, TypeError):
        pass
    return None


def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Valida si un ejecutable usa nombres protegidos del sistema fuera de su ubicación legítima (System32)."""
    if path is None or path.name is None:
        return None
    try:
        if path.name.lower() in SYSTEM_LOOKALIKES:
            if SYSTEM32_LOWER not in [part.lower() for part in path.parts]:
                return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    except Exception:
        pass
    return None


def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """
    Pipeline principal para el análisis de un archivo único.
    Ejecuta heurísticas generales y, si el archivo es un ejecutable, aplica reglas de contexto.
    """
    if path is None:
        return []
        
    findings: ScanResult = []
    
    # Reglas universales
    if (res := check_double_extension(path, entry, now_ts)):
        findings.append(res)
    
    # Reglas específicas para ejecutables sospechosos
    try:
        if path.suffix and path.suffix.lower() in SUSPICIOUS_EXECUTABLE_EXT:
            heuristic_suite: List[SuspicionCheck] = [check_system_lookalike, check_recent_executable_in_downloads]
            for check in heuristic_suite:
                try:
                    if (res := check(path, entry, now_ts)):
                        findings.append(res)
                except Exception as e:
                    logger.debug(f"Fallo en regla {check.__name__} para {path}: {e}")
    except (AttributeError, ValueError):
        pass
                
    return findings


def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """
    Inicia el escaneo recursivo mediante un stack.
    Realiza validaciones de seguridad iniciales sobre la ruta origen antes de comenzar.
    """
    if not directory:
        return []
        
    try:
        path_input = Path(directory).resolve(strict=True)
        # Verificación doble contra el módulo de seguridad
        if not path_input.is_dir() or is_protected_path(path_input):
            return []
    except (OSError, TypeError, ValueError, RuntimeError) as e:
        logger.error(f"Error inicializando escaneo en {directory}: {e}")
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
            logger.debug(f"Error de acceso en directorio {current_dir}: {e}")
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
