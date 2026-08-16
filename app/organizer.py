"""
organizer.py
Organiza archivos "basura" (temporales, cache, descargas viejas, etc.)
en carpetas ordenadas por tamaño o fecha, sin borrar nada automáticamente.

Filosofía de seguridad: este módulo NUNCA borra archivos por sí solo.
Solo mueve candidatos a una carpeta de revisión ("_Para_Revisar") para
que el usuario decida qué borrar. Borrar es una acción explícita y
separada (ver delete_reviewed()).
"""

from __future__ import annotations
import os
import shutil
import string
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Final, Callable, Union, TypeAlias, NamedTuple, Dict

from safety import is_safe_to_modify, ensure_safe_to_modify

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Definiciones de tipo para claridad en firmas complejas
SortKey: TypeAlias = Union[int, datetime]

class SortConfig(NamedTuple):
    """Define los criterios permitidos para el ordenamiento de archivos."""
    field: str
    key_func: Callable[[JunkFile], SortKey]

# Extensiones típicas de archivos "basura" / temporales
JUNK_EXTENSIONS: Final[set[str]] = {
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
}
# Pre-calculado para eficiencia en loops
_LOWER_JUNK_EXTS: Final[set[str]] = {ext.lower() for ext in JUNK_EXTENSIONS}

# Carpetas típicas donde se acumula basura
DEFAULT_SCAN_DIRS: Final[List[str]] = [
    os.path.expandvars(r"%TEMP%"),
    os.path.expandvars(r"%LOCALAPPDATA%\Temp"),
    os.path.expanduser("~/Downloads"),
]

# Carpetas de sistema críticas que nunca se recorren para prevenir daños al SO
SYSTEM_FOLDER_BLOCKLIST: Final[set[str]] = {
    "windows", "program files", "program files (x86)", "$recycle.bin", "system volume information"
}


def list_available_drives() -> List[str]:
    """
    Detecta unidades montadas en Windows.

    Returns:
        Lista de rutas raíz (ej: ['C:\\', 'D:\\']). Retorna lista vacía si no es Windows.
    """
    if os.name != "nt":
        return []
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive: str = f"{letter}:\\"
        if os.path.exists(drive):
            drives.append(drive)
    return drives


@dataclass
class JunkFile:
    """Representa un archivo candidato a limpieza con metadatos asociados."""
    path: Path
    size_bytes: int
    modified: datetime

    def __post_init__(self) -> None:
        """Normaliza la ruta a un objeto Path tras la instanciación."""
        if not isinstance(self.path, Path):
            self.path = Path(self.path)

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño en MB redondeado a 2 decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Retorna True si la extensión está en la lista de candidatos."""
        return _is_junk_path(self.path)


def _is_junction(path: Path) -> bool:
    """
    Verifica si una entrada es un punto de reparse (Junction/Symlink).
    Evita la recursión en estructuras de sistema fuera de control.
    """
    try:
        return path.is_symlink() or (os.name == "nt" and "reparse" in os.stat(path).st_file_attributes)
    except (OSError, AttributeError):
        return False


def _is_junk_path(path: Path) -> bool:
    """Valida si el archivo posee una extensión categorizada como 'basura'."""
    return path.suffix.lower() in _LOWER_JUNK_EXTS


def _generate_unique_target(target: Path) -> Path:
    """
    Genera una ruta única para un archivo destino, añadiendo un sufijo
    numérico si el nombre ya existe para evitar colisiones.
    """
    if not target.exists():
        return target
        
    parent: Path = target.parent
    stem: str = target.stem
    suffix: str = target.suffix
    counter: int = 1
    
    while (candidate := parent / f"{stem}_{counter}{suffix}").exists():
        counter += 1
    return candidate


def _is_allowed_directory(name: str) -> bool:
    """Valida si el directorio no está en la lista de bloqueo del sistema."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST


def _is_file_locked(path: Path) -> bool:
    """
    Comprueba si un archivo está en uso exclusivo intentando abrirlo en modo binario.
    Devuelve True si el archivo está bloqueado o es inaccesible.
    """
    if not path.exists():
        return True
    try:
        with open(path, "rb"):
            return False
    except (OSError, PermissionError, IOError):
        return True


def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """
    Evalúa la seguridad de una operación de movimiento: verifica existencia, 
    permisos, estado de bloqueo, atributos de sistema y seguridad de rutas.
    """
    try:
        if not junk_file.path.exists() or not junk_file.path.is_file():
            return False
        
        current_abs = junk_file.path.resolve()
        dest_abs = dest.resolve()
        
        # Evitar operaciones sobre la raíz o rutas inválidas
        if current_abs.parent == current_abs:
            return False
        
        # Verificar atributos ocultos o de sistema en Windows (0x06 = Hidden | System)
        if os.name == "nt":
            if current_abs.stat().st_file_attributes & 0x06: 
                return False

        # Prevenir movimientos circulares o recursivos entre carpetas
        if current_abs == dest_abs or dest_abs in current_abs.parents or current_abs.parent == dest_abs:
            return False
        
        # Restricción: No mover archivos bloqueados o entre particiones distintas
        if _is_file_locked(current_abs) or current_abs.anchor != dest_abs.anchor:
            return False
            
        return is_safe_to_modify(current_abs) and is_safe_to_modify(dest_abs)
    except (OSError, RuntimeError, AttributeError):
        return False


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Escanea rutas recursivamente buscando archivos basura de forma eficiente.
    """
    raw_dirs = directories if directories is not None else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    
    for d in raw_dirs:
        if not d: continue
        try:
            base = Path(d).expanduser().resolve()
            if not base.exists() or not base.is_dir() or not is_safe_to_modify(base): 
                continue
            
            for root, dirs, files in os.walk(base):
                root_path = Path(root)
                dirs[:] = [d for d in dirs if _is_allowed_directory(d) and not _is_junction(root_path / d)]
                
                for name in files:
                    f_path = root_path / name
                    if _is_junk_path(f_path) and is_safe_to_modify(f_path):
                        try:
                            s = f_path.stat()
                            found.append(JunkFile(f_path, s.st_size, datetime.fromtimestamp(s.st_mtime)))
                        except (OSError, PermissionError):
                            continue
        except (OSError, RuntimeError):
            continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """
    Ordena una lista de archivos basura según un criterio.
    """
    if not isinstance(files, list):
        return []
        
    registry: Dict[str, SortConfig] = {
        "size": SortConfig("size", lambda f: f.size_bytes),
        "date": SortConfig("date", lambda f: f.modified)
    }
        
    criterio = by.lower() if isinstance(by, str) else "size"
    config = registry.get(criterio, registry["size"])
        
    valid_files = [f for f in files if isinstance(f, JunkFile) and f.path is not None]
    return sorted(valid_files, key=config.key_func, reverse=not bool(ascending))


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """
    Mueve los archivos detectados a un directorio de staging tras validar seguridad.
    """
    if not isinstance(files, list) or not isinstance(review_dir, str) or not review_dir.strip():
        return Path(".")

    try:
        dest_base = Path(review_dir).expanduser().resolve()
        if not dest_base.exists():
            dest_base.mkdir(parents=True, exist_ok=True)
        if not is_safe_to_modify(dest_base): return Path(".")
        dest = dest_base.resolve()
    except (OSError, PermissionError, RuntimeError):
        return Path(".")

    for junk_file in files:
        if not isinstance(junk_file, JunkFile) or junk_file.path is None:
            continue
        try:
            if junk_file.path.exists() and junk_file.path.is_file() and os.access(junk_file.path, os.R_OK):
                if _is_safe_to_move(junk_file, dest):
                    usage = shutil.disk_usage(dest)
                    if usage.free > junk_file.size_bytes:
                        target = _generate_unique_target(dest / f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}")
                        if is_safe_to_modify(target):
                            ensure_safe_to_modify(target)
                            if junk_file.path.exists():
                                shutil.move(str(junk_file.path), str(target))
        except (OSError, PermissionError, shutil.Error, RuntimeError):
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina archivos de la carpeta de revisión tras validar la integridad.
    """
    if not isinstance(review_dir, str) or not review_dir.strip():
        return 0

    try:
        dest: Path = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not dest.is_dir() or not is_safe_to_modify(dest):
            return 0
    except (OSError, RuntimeError):
        return 0

    count: int = 0
    for item in dest.iterdir():
        try:
            if item.is_file() and not item.is_symlink():
                path_to_delete = item.resolve()
                if path_to_delete.parent == dest and is_safe_to_modify(path_to_delete):
                    path_to_delete.unlink()
                    count += 1
        except (PermissionError, OSError, ValueError):
            continue
    return count
