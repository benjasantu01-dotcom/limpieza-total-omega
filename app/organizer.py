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
from typing import List, Optional, Final, Callable, Union, TypeAlias, NamedTuple

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
_JUNK_EXTS_TUPLE: Final = tuple(_LOWER_JUNK_EXTS)

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
    Detecta unidades montadas en sistemas Windows mediante comprobación de existencia de raíz.

    Returns:
        List[str]: Lista de rutas raíz (ej. ['C:\\', 'D:\\']). 
    """
    if os.name != "nt":
        return []
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
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
        if not isinstance(self.path, Path):
            self.path = Path(self.path)
        # Aseguramos que la ruta esté resuelta para evitar inconsistencias durante el movimiento
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
    """
    if not target.exists():
        return target
        
    parent, stem, suffix = target.parent, target.stem, target.suffix
    counter = 1
    
    while (candidate := parent / f"{stem}_{counter}{suffix}").exists():
        counter += 1
    return candidate


def _is_valid_junk(entry: os.DirEntry[str]) -> bool:
    """
    Filtro de seguridad previo al procesamiento de directorios.
    """
    if entry.is_symlink() or not entry.is_file():
        return False
    if not entry.name.lower().endswith(_JUNK_EXTS_TUPLE):
        return False
    return is_safe_to_modify(Path(entry.path))


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Recorre recursivamente los directorios provistos buscando archivos basura.
    """
    dirs = directories or DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    blocklist = SYSTEM_FOLDER_BLOCKLIST

    def _walk_dir(base_path: str) -> None:
        try:
            with os.scandir(base_path) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() not in blocklist:
                                _walk_dir(entry.path)
                        elif entry.name.lower().endswith(_JUNK_EXTS_TUPLE):
                            if is_safe_to_modify(Path(entry.path)):
                                stat = entry.stat()
                                found.append(JunkFile(
                                    path=Path(entry.path),
                                    size_bytes=stat.st_size,
                                    modified=datetime.fromtimestamp(stat.st_mtime)
                                ))
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            pass

    for d in dirs:
        if d:
            try:
                p = Path(d).expanduser().resolve()
                if p.exists() and p.is_dir() and is_safe_to_modify(p):
                    _walk_dir(str(p))
            except (RuntimeError, OSError):
                continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """
    Ordena la lista de JunkFile según el criterio especificado.
    """
    if not files:
        return []
        
    configs = {
        "size": SortConfig("size", lambda f: f.size_bytes),
        "date": SortConfig("date", lambda f: f.modified)
    }
        
    config = configs.get(by.lower(), configs["size"])
    return sorted(files, key=config.key_func, reverse=not ascending)


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """
    Prepara archivos para ser eliminados moviéndolos a un directorio de cuarentena.
    """
    if not review_dir:
        raise ValueError("La ruta de revisión no puede estar vacía")

    try:
        dest = Path(review_dir).expanduser().resolve()
    except (RuntimeError, OSError) as e:
        raise ValueError(f"Ruta de revisión inválida: {e}")

    if dest.is_symlink():
        raise PermissionError("Ruta de destino inválida: symlink detectado.")
        
    ensure_safe_to_modify(dest)
    dest.mkdir(parents=True, exist_ok=True)
    canonical_dest = dest.resolve()

    for jf in files:
        # Validación estricta de entrada antes de operar
        if not isinstance(jf, JunkFile) or not jf.path:
            continue
            
        try:
            # Re-verificar existencia y accesibilidad tras el tiempo transcurrido desde el escaneo
            if not jf.path.exists() or jf.path.is_symlink() or not jf.path.is_file():
                continue
            
            # Impedir movimiento si la fuente es el mismo destino o está fuera de zonas seguras
            if not is_safe_to_modify(jf.path):
                continue
            
            if canonical_dest == jf.path or canonical_dest in jf.path.parents:
                continue
            
            # Impedir movimiento si el archivo está bloqueado (en uso)
            try:
                with open(jf.path, 'rb+'): pass
            except (IOError, OSError):
                continue

            target_base = canonical_dest / f"{jf.path.stem}_{int(jf.modified.timestamp())}{jf.path.suffix}"
            target = _generate_unique_target(target_base)
            
            if canonical_dest not in target.resolve().parents:
                continue
            
            ensure_safe_to_modify(jf.path)
            ensure_safe_to_modify(target)
            shutil.move(str(jf.path), str(target))
        except (PermissionError, OSError, shutil.Error):
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina físicamente los archivos dentro del directorio de revisión.
    """
    if not review_dir:
        return 0

    try:
        dest = Path(review_dir).expanduser().resolve()
    except (RuntimeError, OSError):
        return 0
        
    if not dest.exists() or not dest.is_dir() or dest.is_symlink():
        return 0

    count = 0
    for f in dest.iterdir():
        try:
            if f.is_file() and not f.is_symlink() and is_safe_to_modify(f):
                f.unlink()
                count += 1
        except (PermissionError, OSError):
            continue
    return count
