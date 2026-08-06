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
JUNK_EXTENSIONS: Final = {
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
}
# Pre-calculado para eficiencia en loops
_LOWER_JUNK_EXTS: Final = {ext.lower() for ext in JUNK_EXTENSIONS}

# Carpetas típicas donde se acumula basura
DEFAULT_SCAN_DIRS: Final = [
    os.path.expandvars(r"%TEMP%"),
    os.path.expandvars(r"%LOCALAPPDATA%\Temp"),
    os.path.expanduser("~/Downloads"),
]

# Carpetas de sistema críticas que nunca se recorren para prevenir daños al SO
SYSTEM_FOLDER_BLOCKLIST: Final = {
    "windows", "program files", "program files (x86)", "$recycle.bin", "system volume information"
}


def list_available_drives() -> List[str]:
    """
    Detecta unidades montadas en sistemas Windows comprobando la existencia de la raíz.

    Returns:
        List[str]: Lista de rutas raíz (ej. ['C:\\', 'D:\\']).
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
    """
    Representa un archivo candidato a limpieza.
    Almacena metadatos necesarios para filtrado y ordenamiento.
    """
    path: Path
    size_bytes: int
    modified: datetime

    def __post_init__(self) -> None:
        """Normaliza la ruta a absoluta tras la inicialización."""
        if not isinstance(self.path, Path):
            self.path = Path(self.path)
        try:
            self.path = self.path.resolve()
        except (OSError, RuntimeError):
            pass

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño del archivo en Megabytes (redondeado a 2 decimales)."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Valida si la extensión del archivo coincide con las permitidas."""
        return self.path.suffix.lower() in _LOWER_JUNK_EXTS


def _generate_unique_target(target: Path) -> Path:
    """
    Resuelve colisiones de nombres añadiendo un sufijo numérico incremental.
    
    Args:
        target: La ruta destino deseada.

    Returns:
        Path: Ruta garantizada como inexistente para evitar sobrescrituras.
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
    """Verifica si el nombre de una carpeta no está en la blocklist de sistema."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST


def _is_file_accessible(path: Path) -> bool:
    """
    Intenta abrir el archivo en modo lectura binaria para verificar si está bloqueado.
    
    Args:
        path: Ruta del archivo a testear.
        
    Returns:
        bool: True si el archivo se puede abrir, False si está bloqueado o inaccesible.
    """
    try:
        with open(path, 'rb'):
            return True
    except (OSError, PermissionError):
        return False


def _is_valid_candidate(path: Path) -> bool:
    """Valida si un archivo es seguro, accesible y tiene una extensión de basura."""
    return is_safe_to_modify(path) and _is_file_accessible(path)


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Realiza un escaneo recursivo de directorios buscando candidatos a limpieza.
    
    Args:
        directories: Lista de rutas a escanear. Si es None, usa DEFAULT_SCAN_DIRS.
    
    Returns:
        List[JunkFile]: Colección de objetos JunkFile encontrados.
    """
    dirs: List[str] = directories if directories is not None else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    junk_exts: Final = _LOWER_JUNK_EXTS

    def _walk_dir(base_path: str) -> None:
        """Explora recursivamente el árbol de archivos excluyendo bloqueos y enlaces simbólicos."""
        try:
            with os.scandir(base_path) as it:
                for entry in it:
                    try:
                        # Saltar enlaces simbólicos para evitar bucles o salidas de contexto
                        if entry.is_symlink():
                            continue
                        
                        if entry.is_dir(follow_symlinks=False):
                            if _is_allowed_directory(entry.name):
                                _walk_dir(entry.path)
                        else:
                            # Filtrado inicial por extensión antes de validaciones pesadas de IO
                            if entry.name.lower().endswith(tuple(junk_exts)):
                                entry_path: Path = Path(entry.path)
                                if _is_valid_candidate(entry_path):
                                    stat = entry.stat()
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
                p: Path = Path(d).expanduser().resolve()
                if p.exists() and p.is_dir() and is_safe_to_modify(p):
                    _walk_dir(str(p))
            except (RuntimeError, OSError, ValueError):
                continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """
    Ordena una lista de archivos basura según un criterio y dirección.

    Args:
        files: Lista de objetos JunkFile a ordenar.
        by: Criterio de ordenación, 'size' (bytes) o 'date' (modificación).
        ascending: Dirección (True para ascendente, False para descendente).
        
    Returns:
        List[JunkFile]: Lista ordenada.
    """
    if not files:
        return []
        
    configs: Dict[str, SortConfig] = {
        "size": SortConfig("size", lambda f: f.size_bytes),
        "date": SortConfig("date", lambda f: f.modified)
    }
        
    config: SortConfig = configs.get(by.lower(), configs["size"])
    return sorted(files, key=config.key_func, reverse=not ascending)


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """
    Mueve archivos candidatos a un directorio de cuarentena (revisión).
    
    Args:
        files: Archivos a mover.
        review_dir: Ruta donde se almacenarán temporalmente.
        
    Returns:
        Path: Ruta del directorio de revisión preparado.
    """
    if not files:
        raise ValueError("La lista de archivos a procesar no puede estar vacía.")

    try:
        dest: Path = Path(review_dir).expanduser().resolve()
        ensure_safe_to_modify(dest)
        dest.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError, PermissionError) as e:
        raise ValueError(f"No se pudo preparar el directorio de revisión: {e}")

    for jf in files:
        try:
            if not jf.path.exists() or not jf.path.is_file():
                continue
                
            current_abs: Path = jf.path.resolve()
            
            # Verificación de seguridad antes de procesar cada archivo
            if not is_safe_to_modify(current_abs):
                continue
            
            if current_abs.parent == dest or dest in current_abs.parents or current_abs == dest:
                continue
            
            if not _is_file_accessible(current_abs):
                continue

            target: Path = _generate_unique_target(dest / f"{current_abs.stem}_{int(jf.modified.timestamp())}{current_abs.suffix}")
            
            if not is_safe_to_modify(target.parent):
                continue

            shutil.move(str(current_abs), str(target))
        except (PermissionError, OSError, shutil.Error, RuntimeError):
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina físicamente los archivos del directorio de revisión tras confirmación.
    
    Returns:
        int: Cantidad total de archivos eliminados con éxito.
    """
    if not isinstance(review_dir, str):
        return 0

    try:
        dest: Path = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not dest.is_dir() or not is_safe_to_modify(dest):
            return 0
    except (RuntimeError, OSError):
        return 0

    count: int = 0
    for f in dest.iterdir():
        try:
            if f.is_file() and not f.is_symlink() and is_safe_to_modify(f):
                f.unlink()
                count += 1
        except (PermissionError, OSError):
            continue
    return count
