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

from safety import is_safe_to_modify, ensure_safe_to_modify, is_protected_path

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Definiciones de tipo para claridad en firmas complejas
SortKey: TypeAlias = Union[int, datetime]

class SortConfig(NamedTuple):
    """Define los criterios permitidos para el ordenamiento de archivos."""
    field: str
    key_func: Callable[[JunkFile], SortKey]

# Mapeo centralizado de criterios de ordenamiento para facilitar la extensión
SORT_REGISTRY: Final[Dict[str, SortConfig]] = {
    "size": SortConfig("size", lambda f: f.size_bytes),
    "date": SortConfig("date", lambda f: f.modified)
}

# Extensiones típicas de archivos "basura" / temporales
JUNK_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
})

# Carpetas típicas donde se acumula basura
DEFAULT_SCAN_DIRS: Final[List[Path]] = [
    Path(os.environ.get("TEMP", "C:\\Temp")),
    Path(os.environ.get("LOCALAPPDATA", "C:\\")) / "Temp",
    Path.home() / "Downloads",
]

# Carpetas de sistema críticas que nunca se recorren para prevenir daños al SO
SYSTEM_FOLDER_BLOCKLIST: Final[frozenset[str]] = frozenset({
    "windows", "program files", "program files (x86)", "$recycle.bin", "system volume information"
})


def list_available_drives() -> List[str]:
    """
    Detecta unidades montadas en Windows mediante el barrido de letras de unidad.
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
        if self.path:
            self.path = self.path.resolve()

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        return _is_junk_path(self.path)


def _is_junction(path: Path) -> bool:
    """Verifica si la ruta es un punto de reparse (Junction/Symlink) para evitar bucles."""
    try:
        return path.is_symlink() or (os.name == "nt" and bool(path.stat().st_file_attributes & 0x400))
    except (OSError, AttributeError):
        return False


def _is_junk_path(path: Path) -> bool:
    """Valida la extensión del archivo contra la lista permitida de basura."""
    return path.suffix.lower() in JUNK_EXTENSIONS


def _generate_unique_target(target: Path) -> Path:
    """Genera una ruta única añadiendo un sufijo numérico si el archivo ya existe."""
    if not target.exists():
        return target
        
    parent: Path = target.parent
    stem: str = target.stem
    suffix: str = target.suffix
    counter: int = 1
    
    while (candidate := parent / f"{stem}_{counter}{suffix}").exists():
        counter += 1
        if counter > 999: break 
    return candidate


def _is_allowed_directory(name: str) -> bool:
    """Filtra directorios basándose en la lista negra de sistemas críticos."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST


def _is_file_locked(path: Path) -> bool:
    """Verifica si un archivo está en uso exclusivo mediante intento de apertura."""
    try:
        with open(path, "rb") as f:
            return False
    except (OSError, PermissionError, IOError):
        return True


def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """Verifica que el destino no sea un subdirectorio de la fuente para evitar recursividad."""
    try:
        s, d = src.resolve(), dest.resolve()
        return s == d or d.is_relative_to(s)
    except (OSError, RuntimeError, ValueError):
        return True


def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """Valida si una operación de E/S cumple con las reglas de seguridad de la app."""
    try:
        if not is_safe_to_modify(src) or not is_safe_to_modify(dest):
            return False
        if _is_recursive_violation(src, dest) or src.anchor != dest.anchor:
            return False
        
        stat = src.stat()
        if not stat.st_mode: return False
        
        if os.name == "nt" and (stat.st_file_attributes & 0x46): 
            return False
        
        return not _is_file_locked(src)
    except (OSError, RuntimeError, AttributeError):
        return False


def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """Chequeo de seguridad de alto nivel antes de mover un archivo."""
    if not isinstance(junk_file, JunkFile) or not junk_file.path: return False
    try:
        current_path: Path = junk_file.path
        if not current_path.exists() or is_protected_path(current_path) or is_protected_path(dest):
            return False
        return _is_safe_for_disk_op(current_path, dest)
    except (OSError, RuntimeError):
        return False


def _process_directory(current_dir: Path, found: List[JunkFile]) -> None:
    """Recorrido recursivo optimizado recolectando archivos basura."""
    try:
        with os.scandir(current_dir) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _is_allowed_directory(entry.name) and not _is_junction(Path(entry.path)):
                            _process_directory(Path(entry.path), found)
                    elif entry.is_file() and entry.name.lower().endswith(tuple(JUNK_EXTENSIONS)):
                        stats = entry.stat()
                        found.append(JunkFile(Path(entry.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime)))
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        pass


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """Recorre recursivamente los directorios buscando archivos clasificados como basura."""
    search_dirs = [Path(d) for d in directories] if directories else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    
    for d in search_dirs:
        try:
            base = Path(d).expanduser().resolve()
            if base.is_dir():
                _process_directory(base, found)
        except (OSError, RuntimeError):
            continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena la lista de archivos basada en el registro `SORT_REGISTRY`."""
    if not isinstance(files, list) or not all(isinstance(f, JunkFile) for f in files):
        return []
        
    key: str = by.lower() if isinstance(by, str) else "size"
    config: SortConfig = SORT_REGISTRY.get(key, SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """Mueve archivos candidatos a una carpeta de revisión validando espacio y seguridad."""
    if not files or not isinstance(review_dir, str) or not review_dir.strip():
        return None

    try:
        dest_base: Path = Path(review_dir).expanduser().resolve()
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        if not dest_base.is_dir() or not is_safe_to_modify(dest_base) or is_protected_path(dest_base): return None
    except (OSError, PermissionError, RuntimeError):
        return None

    for junk_file in files:
        if not isinstance(junk_file, JunkFile) or not junk_file.path: continue
        try:
            src_path: Path = junk_file.path.resolve()
            # Validación de existencia y espacio antes de mover
            if not src_path.exists(): continue
            if shutil.disk_usage(dest_base.anchor).free < src_path.stat().st_size: continue
            
            if src_path.anchor != dest_base.anchor or not _is_safe_to_move(junk_file, dest_base):
                continue
            
            safe_name = f"{src_path.stem}_{int(junk_file.modified.timestamp())}{src_path.suffix}"
            target = _generate_unique_target(dest_base / safe_name).resolve()
            
            if not target.is_relative_to(dest_base): continue
            ensure_safe_to_modify(src_path)
            shutil.move(str(src_path), str(target))
        except (OSError, PermissionError, shutil.Error, RuntimeError):
            continue
    return dest_base


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Elimina archivos de forma segura tras la revisión explícita del usuario."""
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
            # Validar que sea un archivo real, que no sea una carpeta y que exista
            if not item.is_file() or _is_junction(item) or not item.exists():
                continue
            # Asegurar que el ítem pertenece a la carpeta de cuarentena para evitar borrado accidental
            if not item.resolve().is_relative_to(dest.resolve()):
                continue
                
            if is_safe_to_modify(item) and not _is_file_locked(item):
                ensure_safe_to_modify(item)
                item.unlink()
                count += 1
        except (PermissionError, OSError, ValueError):
            continue
    return count
