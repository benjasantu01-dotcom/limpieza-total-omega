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

# Extensiones típicas de archivos "basura" / temporales en Windows
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
    """Detecta unidades montadas en sistemas Windows.

    Returns:
        List[str]: Lista de rutas raíz (ej. ['C:\\', 'D:\\']). Retorna lista vacía en no-Windows.
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
        if not isinstance(self.path, Path):
            self.path = Path(self.path)

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño del archivo en Megabytes (redondeado a 2 decimales)."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión está dentro de la lista de candidatos JUNK_EXTENSIONS."""
        return _is_junk_path(self.path)


def _is_junk_path(path: Path) -> bool:
    """Valida si el archivo posee una extensión categorizada como 'basura'."""
    return path.suffix.lower() in _LOWER_JUNK_EXTS


def _generate_unique_target(target: Path) -> Path:
    """Resuelve colisiones de nombres mediante sufijos numéricos incrementales.
    
    Args:
        target: La ruta destino deseada que podría existir previamente.
    Returns:
        Path: Una ruta garantizada única basada en el nombre original.
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
    """Determina si un nombre de carpeta no es un directorio crítico del sistema."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST


def _is_file_accessible(path: Path) -> bool:
    """Verifica si el proceso actual tiene permisos de lectura sobre el archivo.
    
    Utiliza os.access en lugar de abrir el archivo para evitar bloqueos por 
    procesos concurrentes y reducir la sobrecarga de I/O innecesaria.
    """
    return os.access(path, os.R_OK)


def _is_valid_candidate(path: Path) -> bool:
    """Valida si un archivo cumple las políticas de seguridad y está libre para acceso."""
    return is_safe_to_modify(path) and _is_file_accessible(path)


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """Realiza un escaneo recursivo en directorios buscando archivos temporales o basura.

    Args:
        directories: Lista de rutas a escanear. Usa DEFAULT_SCAN_DIRS si es None.
    
    Returns:
        List[JunkFile]: Lista de objetos encontrados que cumplen el criterio de basura.
    """
    dirs: List[str] = directories if directories is not None else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []

    def _walk_dir(base_path: str) -> None:
        """Recorre recursivamente directorios ignorando enlaces y rutas restringidas."""
        try:
            with os.scandir(base_path) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink():
                            continue
                        
                        if entry.is_dir(follow_symlinks=False):
                            if _is_allowed_directory(entry.name):
                                _walk_dir(entry.path)
                        elif _is_junk_path(Path(entry.name)):
                            stat = entry.stat()
                            entry_path: Path = Path(entry.path)
                            
                            if _is_valid_candidate(entry_path):
                                found.append(JunkFile(
                                    path=entry_path,
                                    size_bytes=stat.st_size,
                                    modified=datetime.fromtimestamp(stat.st_mtime)
                                ))
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            pass

    for d in dirs:
        if isinstance(d, str):
            try:
                resolved_path: Path = Path(d).expanduser().resolve()
                if resolved_path.exists() and resolved_path.is_dir() and is_safe_to_modify(resolved_path):
                    _walk_dir(str(resolved_path))
            except (RuntimeError, OSError, ValueError):
                continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena los archivos encontrados por tamaño o fecha de modificación.
    
    Args:
        files: Lista de JunkFile a ordenar.
        by: Criterio de ordenamiento ('size' o 'date').
        ascending: Dirección del ordenamiento.
    """
    if not isinstance(files, list) or not files:
        return []
        
    configs: Dict[str, Callable[[JunkFile], SortKey]] = {
        "size": lambda f: f.size_bytes,
        "date": lambda f: f.modified
    }
        
    criterio: str = str(by).lower() if isinstance(by, str) else "size"
    if criterio not in configs:
        criterio = "size"
        
    return sorted(files, key=configs[criterio], reverse=not bool(ascending))


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """Mueve archivos basura a un directorio de cuarentena para revisión humana.
    
    Args:
        files: Archivos identificados para mover.
        review_dir: Ruta destino donde se almacenarán los archivos.
    Returns:
        Path: El directorio de revisión utilizado.
    """
    if not files:
        raise ValueError("La lista de archivos a procesar no puede estar vacía.")

    try:
        dest: Path = Path(review_dir).expanduser().resolve()
        ensure_safe_to_modify(dest)
        dest.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink() or not dest.is_dir():
            raise ValueError("Destino de revisión inválido o punto de reparse detectado.")
    except (OSError, RuntimeError, PermissionError) as e:
        raise ValueError(f"No se pudo preparar el directorio de revisión: {e}")

    for jf in files:
        try:
            if not isinstance(jf, JunkFile):
                continue
            current_abs: Path = jf.path.resolve()
            
            if not current_abs.exists() or not current_abs.is_file():
                continue
            
            # Verificación estricta de seguridad: evitar path traversal
            if dest == current_abs or dest in current_abs.parents:
                continue
            
            if not is_safe_to_modify(current_abs):
                continue
            
            if not _is_file_accessible(current_abs):
                continue

            target: Path = _generate_unique_target(dest / f"{current_abs.stem}_{int(jf.modified.timestamp())}{current_abs.suffix}")
            
            ensure_safe_to_modify(target)
            shutil.move(str(current_abs), str(target))
        except (PermissionError, OSError, shutil.Error, RuntimeError):
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Elimina permanentemente archivos desde la carpeta de revisión con validación robusta.
    
    Args:
        review_dir: Directorio origen donde se encuentran archivos aprobados para borrar.
    Returns:
        int: Cantidad de archivos borrados exitosamente.
    """
    if not isinstance(review_dir, str) or not review_dir.strip():
        return 0

    try:
        dest: Path = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not dest.is_dir() or dest.is_symlink() or not is_safe_to_modify(dest):
            return 0
    except (RuntimeError, OSError, ValueError):
        return 0

    count: int = 0
    try:
        for f in dest.iterdir():
            try:
                if f.is_file() and not f.is_symlink() and is_safe_to_modify(f):
                    f.unlink()
                    count += 1
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        pass
    return count
