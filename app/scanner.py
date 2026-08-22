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
from safety import is_protected_path, is_safe_to_modify

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
SuspicionCheck: TypeAlias = Callable[[Path, Optional[os.DirEntry], float], Optional[Suspicion]]

# Alias para representar una colección de hallazgos.
ScanResult: TypeAlias = List[Suspicion]

# Expresiones regulares para detección de ofuscación
DOUBLE_EXTENSION_RE: Final[re.Pattern] = re.compile(r"\.(pdf|jpg|png|docx|xlsx|txt)\.(exe|scr|bat|cmd|js|vbs)$", re.IGNORECASE)
RTL_CHAR_RE: Final[re.Pattern] = re.compile(r"[\u200f\u202e\u202d]")

# Conjuntos de constantes para comparación rápida
SUSPICIOUS_EXECUTABLE_EXT: Final[frozenset[str]] = frozenset({".exe", ".scr", ".bat", ".cmd", ".js", ".vbs", ".ps1"})
SYSTEM_LOOKALIKES: Final[frozenset[str]] = frozenset({"svchost.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe"})
WATCHED_FOLDERS: Final[frozenset[str]] = frozenset({"downloads", "temp", "desktop"})

# Configuración de umbrales
SYSTEM32_LOWER: Final[str] = "system32"
RECENT_FILE_THRESHOLD_HOURS: Final[int] = 24

# Colección pre-definida de verificaciones para ejecutables
EXECUTABLE_CHECKS: Final[List[SuspicionCheck]] = [
    lambda p, e, t: check_system_lookalike(p, e, t),
    lambda p, e, t: check_recent_executable_in_downloads(p, e, t)
]


class Scanner:
    """
    Gestiona el estado del escaneo y la navegación recursiva del sistema de archivos.
    
    Attributes:
        results: Lista de hallazgos acumulados durante el escaneo.
        seen: Conjunto de rutas procesadas para evitar ciclos.
        base_root: Ruta absoluta y resuelta del directorio raíz de escaneo.
        now_ts: Marca de tiempo actual para cálculos de antigüedad.
    """
    
    def __init__(self, base_root: Path) -> None:
        self.results: ScanResult = []
        self.seen: set[str] = set()
        self.base_root: Path = base_root.resolve()
        self.now_ts: float = datetime.now().timestamp()

    def _is_safe_entry(self, entry_path: Path) -> bool:
        """Verifica que la entrada esté dentro del alcance de la raíz original."""
        try:
            resolved = entry_path.resolve()
            return self.base_root == resolved or self.base_root in resolved.parents
        except (OSError, RuntimeError):
            return False

    def _is_reparse_point(self, entry: os.DirEntry) -> bool:
        """
        Detecta si la entrada es un punto de reanálisis (reparse point).
        
        Usa flags de sistema para identificar Junciones o Symlinks y prevenir
        que el escáner salga del árbol de directorios asignado.
        """
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        except (OSError, AttributeError, TypeError):
            return True 

    def process_entry(self, entry: Optional[os.DirEntry], stack: List[str]) -> None:
        """
        Analiza un elemento del sistema de archivos.
        
        Si es un directorio, lo agrega al stack para exploración profunda.
        Si es un archivo, ejecuta las heurísticas de seguridad pertinentes.
        """
        if entry is None or not hasattr(entry, 'path') or not entry.path:
            return
        
        try:
            target_path = Path(entry.path)
            
            # Chequeo de seguridad: omitir si está protegido, fuera de raíz o es ruta inválida
            if is_protected_path(target_path) or not is_safe_to_modify(target_path) or entry.path.startswith("\\\\"):
                return
            if not self._is_safe_entry(target_path):
                return

            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except (OSError, PermissionError):
                return

            if is_dir:
                if not self._is_reparse_point(entry):
                    path_str = str(target_path)
                    if path_str not in self.seen:
                        self.seen.add(path_str)
                        stack.append(path_str)
                return

            if is_file:
                try:
                    # Validar existencia y tamaño antes de analizar
                    stats = entry.stat(follow_symlinks=False)
                    if stats.st_size == 0:
                        return
                except (OSError, PermissionError, FileNotFoundError):
                    return

                # Aplicar heurísticas de nombre y extensión
                if RTL_CHAR_RE.search(target_path.name):
                    self.results.append(Suspicion(target_path, "Nombre de archivo contiene caracteres de control de ofuscación (RTL)", "critical"))
                
                suffix = target_path.suffix.lower()
                if suffix in SUSPICIOUS_EXECUTABLE_EXT or DOUBLE_EXTENSION_RE.search(target_path.name):
                    self.results.extend(scan_file(target_path, self.now_ts, entry=entry))
                
        except Exception as e:
            logger.debug(f"Error procesando {entry.path if entry else 'unknown'}: {e}")


def check_double_extension(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """Detecta nombres con extensiones múltiples engañosas (ej: .jpg.exe)."""
    if path is None or not path.name:
        return None
    if DOUBLE_EXTENSION_RE.search(path.name):
        return Suspicion(path, "Doble extensión disfrazando el tipo real de archivo", "warning")
    return None


def check_recent_executable_in_downloads(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Verifica la antigüedad de un ejecutable en carpetas de alto riesgo.
    
    Busca ejecutables descargados recientemente, ya que son vectores comunes 
    de infección inicial.
    """
    if path is None or is_protected_path(path):
        return None
    
    # Optimización: evitar crear sets o listas, usar any() con verificación de substrings lower
    path_str_lower = str(path).lower()
    if not any(folder in path_str_lower for folder in WATCHED_FOLDERS):
        return None
        
    try:
        # Usar os.path.exists para verificar existencia sin levantar excepción si es un enlace roto
        if not os.path.exists(path):
            return None
            
        stats = entry.stat(follow_symlinks=False) if (entry and hasattr(entry, 'stat')) else path.stat()
        if (now_ts - stats.st_mtime) < (RECENT_FILE_THRESHOLD_HOURS * 3600):
            return Suspicion(path, f"Ejecutable reciente detectado (<{RECENT_FILE_THRESHOLD_HOURS}h)", "info")
    except (OSError, PermissionError, AttributeError, ValueError) as e:
        logger.debug(f"Acceso restringido a metadatos de {path}: {e}")
    return None


def check_system_lookalike(path: Path, entry: Optional[os.DirEntry] = None, now_ts: float = 0.0) -> Optional[Suspicion]:
    """
    Detecta archivos con nombres de procesos del sistema ubicados fuera de System32.
    
    Identifica intentos de engaño mediante typosquatting de binarios críticos.
    """
    if path is None or not path.name:
        return None
        
    if path.name.lower() in SYSTEM_LOOKALIKES:
        if SYSTEM32_LOWER not in str(path).lower():
            return Suspicion(path, "Nombre de proceso de sistema fuera de System32", "warning")
    return None


def scan_file(path: Path, now_ts: float, entry: Optional[os.DirEntry] = None) -> ScanResult:
    """
    Orquestador de reglas heurísticas para un archivo dado.
    
    Agrupa chequeos genéricos y específicos para ejecutables.
    """
    if path is None:
        return []
    
    findings: ScanResult = []
    
    # Chequeo genérico para todos los archivos
    if (double_ext := check_double_extension(path, entry, now_ts)):
        findings.append(double_ext)
    
    # Chequeo específico para ejecutables
    if path.suffix.lower() in SUSPICIOUS_EXECUTABLE_EXT:
        for check in EXECUTABLE_CHECKS:
            try:
                if (result := check(path, entry, now_ts)):
                    findings.append(result)
            except Exception as e:
                logger.debug(f"Fallo en regla heurística {check.__name__}: {e}")
                
    return findings


def scan_directory(directory: Union[str, Path, None]) -> ScanResult:
    """
    Realiza un escaneo recursivo del sistema de archivos.
    
    Valida la integridad de la ruta raíz y utiliza una pila para evitar
    la recursividad profunda que podría causar un StackOverflow.
    """
    if not directory:
        return []
        
    try:
        raw_path = Path(directory)
        if str(raw_path).startswith(("\\\\", "//")):
            return []
        if not raw_path.exists():
            return []
        path_input: Path = raw_path.resolve(strict=True)
        if not path_input.is_dir() or is_protected_path(path_input) or not is_safe_to_modify(path_input):
            return []
    except (OSError, TypeError, ValueError, RuntimeError) as e:
        logger.debug(f"Entrada de directorio inválida o inaccesible: {e}")
        return []

    scanner = Scanner(base_root=path_input)
    stack: List[str] = [str(path_input)]
    scanner.seen.add(str(path_input))
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if entry is not None:
                        scanner.process_entry(entry, stack)
        except (PermissionError, OSError) as e:
            logger.debug(f"Error accediendo a directorio {current_dir}: {e}")
            continue
            
    return scanner.results


def run_windows_defender_quick_scan() -> str:
    """
    Ejecuta un escaneo rápido mediante la API de Windows Defender.
    
    Requiere PowerShell para invocar los cmdlets de seguridad nativos.
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
