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

# Alias para funciones que evalúan un archivo y retornan una sospecha o None.
# Argumentos: 
#   path: Ruta del archivo.
#   entry: Objeto DirEntry opcional (usar para evitar llamadas a stat si está disponible).
#   now_ts: Timestamp actual (epoch) para cálculos de antigüedad.
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]

# Alias para representar una colección de hallazgos.
ScanResult: TypeAlias = List[Suspicion]

# Expresiones regulares y constantes de configuración
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d]")
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
SYSTEM32_LOWER: Final[str] = "system32"
WATCHED_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})
RECENT_FILE_THRESHOLD_HOURS: Final[int] = 24

# Colección pre-definida de verificaciones para ejecutables
EXECUTABLE_CHECKS: Final[List[SuspicionCheck]] = [
    lambda p, e, t: check_system_lookalike(p, e, t),
    lambda p, e, t: check_recent_executable_in_downloads(p, e, t)
]


class Scanner:
    """
    Gestiona el estado y la navegación recursiva del sistema de archivos.
    
    Attributes:
        results: Lista acumulada de hallazgos encontrados.
        seen: Conjunto de rutas ya procesadas para evitar ciclos en enlaces simbólicos.
        base_root: Ruta raíz resuelta desde donde se inicia el escaneo.
        now_ts: Timestamp para cálculos de antigüedad.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root = base_root.resolve()
        self.now_ts = datetime.now().timestamp()

    def _is_safe_entry(self, entry_path: Path) -> bool:
        """
        Verifica que la ruta resuelta se mantenga dentro de la jerarquía de `base_root`
        para evitar escapes de directorio durante la recursión.
        """
        try:
            resolved = entry_path.resolve()
            return self.base_root == resolved or self.base_root in resolved.parents
        except (OSError, RuntimeError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """
        Detecta puntos de reanálisis (reparse points) como Junctions o Symlinks
        evitando el procesamiento recursivo de carpetas fuera del sistema de archivos local.
        """
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return True 

    def process_entry(self, entry: Optional[os.DirEntry], stack: List[str]) -> None:
        """
        Analiza un elemento del sistema de archivos, aplicando filtros de seguridad
        y determinando si debe ser explorado (directorio) o analizado (archivo).
        """
        if entry is None or not entry.path:
            return
        
        try:
            target_path = Path(entry.path)
            
            # Chequeo de seguridad preventivo contra rutas protegidas o rutas UNC
            if is_protected_path(target_path) or str(target_path).startswith("\\\\"):
                return

            # Control de navegación: evitar punteros que puedan causar bucles infinitos
            if entry.is_symlink() or self._is_reparse_point(entry):
                return

            # Restricción de alcance al directorio raíz definido
            if not self._is_safe_entry(target_path):
                return
            
            # Detectar ofuscación por caracteres de control RTL en el nombre
            if RTL_CHAR_RE.search(target_path.name):
                self.results.append(Suspicion(target_path, "Nombre de archivo contiene caracteres de control de ofuscación (RTL)", "critical"))

            # Validar tipo de entrada antes de operar
            if entry.is_dir(follow_symlinks=False):
                path_str = str(target_path)
                if path_str not in self.seen:
                    self.seen.add(path_str)
                    stack.append(path_str)
            elif entry.is_file(follow_symlinks=False):
                self.results.extend(scan_file(target_path, self.now_ts, entry=entry))
                
        except (PermissionError, OSError) as e:
            logger.debug(f"Acceso denegado o error en ruta {entry.path}: {e}")
        except Exception as e:
            logger.error(f"Error inesperado procesando {entry.path}: {e}")


def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Detecta extensiones dobles que intentan ofuscar el tipo real del ejecutable."""
    if path is None or not path.name:
        return None
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Evalúa si un archivo ejecutable en zonas de riesgo fue creado recientemente."""
    if path is None or is_protected_path(path) or path.suffix.lower() not in SUSPICIOUS_EXECUTABLE_EXT:
        return None
    
    path_lower = str(path).lower()
    if not any(f"\\{folder}\\" in path_lower for folder in WATCHED_FOLDERS):
        return None
        
    try:
        stats = entry.stat() if (entry and hasattr(entry, 'stat')) else path.stat()
        if (now_ts - stats.st_mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
            return Suspicion(path, f"Ejecutable reciente detectado (<{RECENT_FILE_THRESHOLD_HOURS}h)", "info")
    except (OSError, PermissionError, AttributeError, ValueError):
        logger.debug(f"Acceso restringido a metadatos de {path}")
    return None


def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Detecta archivos con nombres de procesos del sistema ubicados fuera de System32."""
    if path is None or not path.name:
        return None
        
    if path.name.lower() in SYSTEM_LOOKALIKES:
        if SYSTEM32_LOWER not in str(path).lower():
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None


def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """
    Orquesta la ejecución de reglas heurísticas sobre un archivo dado.
    """
    if path is None:
        return []

    findings: ScanResult = []
    
    if (res := check_double_extension(path, entry, now_ts)):
        findings.append(res)
    
    if path.suffix.lower() in SUSPICIOUS_EXECUTABLE_EXT:
        for check in EXECUTABLE_CHECKS:
            try:
                if (res := check(path, entry, now_ts)):
                    findings.append(res)
            except Exception as e:
                logger.debug(f"Fallo en regla heurística {check.__name__}: {e}")
                
    return findings


def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """
    Inicializa el motor de escaneo, valida la raíz y gestiona la cola de directorios
    para una exploración profunda no recursiva (iterativa) del sistema de archivos.
    """
    if not directory:
        return []
        
    try:
        raw_path = Path(directory)
        if not raw_path.exists():
            return []
        path_input = raw_path.resolve(strict=True)
        # Filtro de seguridad inicial crítico antes de iniciar el escaneo
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
                    if entry:
                        scanner.process_entry(entry, stack)
        except (PermissionError, OSError) as e:
            logger.debug(f"Error accediendo a directorio {current_dir}: {e}")
            continue
            
    return scanner.results


def run_windows_defender_quick_scan() -> str:
    """
    Interacción externa con PowerShell para consultar estado de protección
    en tiempo real y disparar un escaneo de Windows Defender.
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
    except FileNotFoundError:
        return "PowerShell no disponible. Este módulo requiere Windows."
    except subprocess.TimeoutExpired:
        return "El escaneo de Windows Defender excedió el tiempo límite."
