"""
scanner.py
Detector HEURÍSTICO de archivos sospechosos. Este módulo realiza un análisis 
estático mediante heurísticas de nombre, extensión y metadatos de archivo, 
complementando la protección de Windows Defender.

El módulo utiliza un recorrido iterativo seguro para navegar el sistema de archivos, 
aplicando validaciones estrictas antes de cada acceso para evitar la resolución de 
puntos de reanálisis (Junctions/Symlinks) y rutas protegidas.
"""

from __future__ import annotations
import subprocess
import re
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Union, Final, Callable, TypeAlias, NamedTuple, Dict
from safety import is_protected_path

# Configuración de logger para el módulo
logger: Final = logging.getLogger(__name__)

class ScannerLimits(NamedTuple):
    """
    Parámetros operativos críticos para el escaneo.
    
    max_path: Límite de longitud de ruta según estándar Windows MAX_PATH.
    recent_hours: Ventana temporal para considerar un archivo como 'reciente'.
    reparse_point_attr_mask: Máscara binaria (FILE_ATTRIBUTE_REPARSE_POINT) para detectar puntos de reanálisis.
    max_depth: Profundidad máxima de recursión permitida para evitar desbordamiento de pila.
    """
    max_path: int = 260
    recent_hours: int = 24
    reparse_point_attr_mask: int = 0x400
    max_depth: int = 50

SCAN_LIMITS: Final = ScannerLimits()

@dataclass
class Suspicion:
    """
    Reporte de hallazgo tras inspección heurística.

    Attributes:
        path: Ruta absoluta al archivo analizado.
        reason: Justificación técnica del hallazgo (ej. ofuscación, comportamiento).
        severity: Nivel de urgencia ('info' para monitoreo, 'warning' para atención requerida).
    """
    path: Path
    reason: str
    severity: str

# Definición de tipos para el sistema de heurísticas
# SuspicionCheck recibe: (archivo a analizar, objeto DirEntry asociado, timestamp actual)
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]
ScanResult: TypeAlias = List[Suspicion]

# Expresiones regulares para detección de ofuscación y estructuras no estándar
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d]")
RESERVED_NAMES_RE: Final[re.Pattern] = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE)
INVALID_TRAILING_CHARS_RE: Final[re.Pattern] = re.compile(r"[\. ]$")
UNC_PATH_RE: Final[re.Pattern] = re.compile(r"^\\\\[^\\\\]+\\")

# Conjuntos de constantes para comparación rápida
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SUSPICIOUS_CONTENT_EXT: Final[frozenset[str]] = frozenset({".pdf"})
SUSPICIOUS_ALL_EXTS: Final[frozenset[str]] = SUSPICIOUS_EXECUTABLE_EXT.union(SUSPICIOUS_CONTENT_EXT)
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
TARGETED_DOWNLOAD_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})

SYSTEM32_LOWER: Final[str] = "system32"

def _safe_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """
    Obtiene metadatos asegurando que no se sigan enlaces simbólicos y 
    verificando que el archivo no sea un hard link múltiple hacia el sistema.
    """
    if not isinstance(entry, os.DirEntry):
        return None
    try:
        # Verificación explícita de tipos de archivo antes de estatizar
        if entry.is_symlink():
            return None
            
        attr = _get_file_attributes(entry)
        if attr & SCAN_LIMITS.reparse_point_attr_mask:
            return None
            
        stats = entry.stat(follow_symlinks=False)
        # Defensa contra hard link spoofing: archivos del sistema suelen tener enlaces múltiples
        if getattr(stats, "st_nlink", 1) > 1:
            return None
        return stats
    except (OSError, PermissionError):
        return None

def _get_file_attributes(entry: os.DirEntry) -> int:
    """Consulta la máscara de bits de atributos Win32 (File Attributes) de forma pasiva."""
    try:
        stat_res = entry.stat(follow_symlinks=False)
        return int(getattr(stat_res, "st_file_attributes", 0))
    except (AttributeError, OSError, PermissionError):
        return 0

def _is_valid_path_structure(path_str: Optional[str]) -> bool:
    """Valida que la ruta sea conforme a las restricciones del sistema de archivos local."""
    if not path_str or len(path_str) > SCAN_LIMITS.max_path:
        return False
    # Evitar rutas de red (UNC) y ofuscación por caracteres invisibles RTL
    if UNC_PATH_RE.match(path_str) or RTL_CHAR_RE.search(path_str):
        return False
    return True

def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Heurística: Detecta doble extensión (ej. archivo.pdf.exe).
    Retorna un objeto Suspicion si se detecta ofuscación de tipo, None en caso contrario.
    """
    if path and path.name and DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None

def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Heurística: Identifica ejecutables nuevos en carpetas temporales definidas en TARGETED_DOWNLOAD_FOLDERS.
    Analiza mtime contra now_ts para determinar si es reciente según SCAN_LIMITS.recent_hours.
    """
    if not path or not path.parent or path.parent.name.lower() not in TARGETED_DOWNLOAD_FOLDERS:
        return None
    stats = _safe_stat(entry) if entry else None
    if stats:
        mtime = getattr(stats, "st_mtime", None)
        if isinstance(mtime, (int, float)) and mtime > 0:
            if (now_ts - float(mtime)) < (SCAN_LIMITS.recent_hours * 3600):
                return Suspicion(path, f"Ejecutable reciente detectado (<{SCAN_LIMITS.recent_hours}h)", "info")
    return None

def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Heurística: Detecta ejecutables con nombres críticos fuera de System32.
    Previene el spoofing de procesos críticos del sistema operativo.
    """
    if path and path.name and path.name.lower() in SYSTEM_LOOKALIKES:
        path_str = str(path).lower()
        if SYSTEM32_LOWER not in path_str:
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None

def check_empty_file(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Heurística: Detecta archivos de 0 bytes.
    A menudo utilizados como placeholders o marcadores de ejecución maliciosa.
    """
    stats = _safe_stat(entry) if entry else None
    if stats is not None:
        size = getattr(stats, "st_size", -1)
        if isinstance(size, int) and size == 0:
            return Suspicion(path, "Archivo ejecutable vacío sospechoso", "warning")
    return None

ALL_CHECKS: Final[List[SuspicionCheck]] = [
    check_double_extension,
    check_system_lookalike,
    check_recent_executable_in_downloads,
    check_empty_file
]

class Scanner:
    """
    Motor recursivo de escaneo para recorrer el sistema de archivos de forma segura.
    Implementa una estrategia de caja de arena (sandbox) basada en `base_root` para 
    evitar el escape del directorio raíz durante la iteración.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: List[Suspicion] = []
        self.seen: set[str] = set()
        self.safe_cache: set[str] = set()
        self._root_cache: Dict[str, bool] = {}
        # Se guarda el Path resuelto para asegurar comparaciones canónicas
        self.base_root: Path = base_root.resolve()
        self.base_root_str: str = str(self.base_root).lower()
        self.now_ts: float = datetime.now().timestamp()

    def _is_inside_base_root(self, entry_path: str) -> bool:
        """Verifica que la ruta visitada no escape del directorio raíz definido."""
        if entry_path in self._root_cache:
            return self._root_cache[entry_path]
        try:
            # Resolucion estricta: canonicalización antes de comparar contra base_root
            target = Path(entry_path).resolve(strict=False)
            result = str(target).lower().startswith(self.base_root_str)
            if len(self._root_cache) < 1000:
                self._root_cache[entry_path] = result
            return result
        except (OSError, RuntimeError):
            return False

    def _has_invalid_name(self, name: str) -> bool:
        """Valida que el archivo no utilice nombres reservados de sistema (ej. CON, NUL) o trailing invalido."""
        return bool(INVALID_TRAILING_CHARS_RE.search(name) or RESERVED_NAMES_RE.match(name))

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """Determina mediante bits de atributo si la entrada es un punto de reanálisis."""
        return bool(_get_file_attributes(entry) & SCAN_LIMITS.reparse_point_attr_mask)

    def _is_safe_entry(self, entry: os.DirEntry) -> bool:
        """
        Realiza chequeo de seguridad completo antes de procesar un nodo.
        Valida estructura, límites de profundidad, pertenencia a base_root y listas de protección.
        """
        if not entry or not entry.path or not entry.name:
            return False
        if not _is_valid_path_structure(entry.path) or self._has_invalid_name(entry.name):
            return False
        
        try:
            if self._is_reparse_point(entry) or entry.is_symlink():
                return False
            
            # Verificación de confinamiento de ruta (sandboxing)
            if not self._is_inside_base_root(entry.path):
                return False
                
            if entry.path in self.safe_cache:
                return True
            
            # Verificación contra lista de rutas del sistema (safety.py)
            if is_protected_path(Path(entry.path)):
                return False
            
            self.safe_cache.add(entry.path)
            return True
        except (OSError, RuntimeError):
            return False

    def _handle_directory(self, entry: os.DirEntry, directory_stack: List[tuple[str, int]], current_depth: int) -> None:
        """Añade directorio validado a la pila de exploración para recorrerlo, respetando max_depth."""
        if current_depth >= SCAN_LIMITS.max_depth:
            return
        if entry.path and entry.path.lower() not in self.seen:
            self.seen.add(entry.path.lower())
            directory_stack.append((entry.path, current_depth + 1))

    def _is_relevant_extension(self, name: str) -> bool:
        """Filtra extensiones que no tienen interés para el motor de heurísticas."""
        name_lower = name.lower()
        return any(name_lower.endswith(ext) for ext in SUSPICIOUS_ALL_EXTS)

    def process_entry(self, entry: os.DirEntry, directory_stack: List[tuple[str, int]], current_depth: int) -> None:
        """
        Despacha la entrada según su tipo (directorio o archivo) para aplicar heurísticas 
        o continuar la exploración recursiva.
        """
        try:
            if not self._is_safe_entry(entry):
                return
            
            if entry.is_dir(follow_symlinks=False):
                self._handle_directory(entry, directory_stack, current_depth)
            elif entry.is_file(follow_symlinks=False):
                if self._is_relevant_extension(entry.name):
                    self._run_file_heuristics(Path(entry.path), entry)
        except (OSError, PermissionError):
            return

    def _run_file_heuristics(self, path: Path, entry: os.DirEntry) -> None:
        """Ejecuta todas las funciones registradas en ALL_CHECKS sobre el archivo actual."""
        if not path or not entry:
            return
        for check_fn in ALL_CHECKS:
            try:
                # Validar existencia de la entrada previo a la heurística
                if not entry.exists():
                    continue
                finding = check_fn(path, entry, self.now_ts)
                if finding is not None:
                    self.results.append(finding)
            except (OSError, PermissionError) as e:
                logger.debug(f"Acceso denegado al procesar {path}: {e}")
            except Exception as e:
                logger.warning(f"Error inesperado en heurística {check_fn.__name__} para {path}: {e}")

def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> List[Suspicion]:
    """Realiza un análisis estático de un archivo puntual sin recursión."""
    if not isinstance(path, Path): return []
    try:
        resolved = path.resolve()
        if not resolved.is_file() or not os.access(resolved, os.R_OK): return []
        if is_protected_path(resolved): return []
    except (OSError, PermissionError): return []
    
    findings: List[Suspicion] = []
    for check_fn in ALL_CHECKS:
        try:
            res = check_fn(path, entry, now_ts)
            if res is not None:
                findings.append(res)
        except Exception:
            continue
    return findings

def scan_directory(directory: Union[str, Path, None]) -> List[Suspicion]:
    """
    Escanea el árbol de directorios de forma iterativa, gestionando el stack manualmente.
    Este método actúa como punto de entrada público para la inspección de carpetas.
    """
    if directory is None: return []
    path_str = str(directory).strip()
    if not path_str or not _is_valid_path_structure(path_str): return []
    
    try:
        base_path = Path(path_str).resolve()
        if not base_path.exists() or not base_path.is_dir() or base_path.is_symlink() or not os.access(base_path, os.R_OK):
            return []
        if is_protected_path(base_path): return []
    except (OSError, RuntimeError): 
        return []
    
    scanner = Scanner(base_root=base_path)
    # Stack guarda tuplas (ruta, profundidad_actual)
    directory_stack: List[tuple[str, int]] = [(str(base_path), 0)]
    scanner.seen.add(str(base_path).lower())
    
    while directory_stack:
        current_dir, depth = directory_stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        if entry is not None:
                            scanner.process_entry(entry, directory_stack, depth)
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            continue
    return scanner.results

def run_windows_defender_quick_scan() -> str:
    """Interroga el estado de Defender y solicita una revisión rápida si es posible."""
    try:
        status = subprocess.run(
            ["powershell", "-Command", "Get-MpComputerStatus | Select-Object -ExpandProperty RealTimeProtectionEnabled"],
            capture_output=True, text=True, timeout=10
        )
        if status.returncode == 0 and status.stdout and status.stdout.strip() != "True":
            return "Protección en tiempo real desactivada. Escaneo omitido."
        result = subprocess.run(
            ["powershell", "-Command", "Start-MpScan -ScanType QuickScan"],
            capture_output=True, text=True, timeout=1800, check=True
        )
        return str(result.stdout) if result.stdout else "Escaneo iniciado correctamente."
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, subprocess.TimeoutExpired) as e:
        return f"Error ejecutando Windows Defender: {str(e)}"
