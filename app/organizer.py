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

# Tupla precalculada para optimizar las comparaciones de extensiones en bucles
JUNK_EXTENSIONS_TUPLE: Final[tuple[str, ...]] = tuple(JUNK_EXTENSIONS)

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
    """Detecta unidades montadas en Windows mediante el barrido de letras de unidad."""
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
        """Retorna el tamaño del archivo en MB redondeado a dos decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión del archivo está en JUNK_EXTENSIONS."""
        return _is_junk_path(self.path)


def _is_junction(entry: os.DirEntry) -> bool:
    """Verifica si la entrada es un punto de reparse (Junction/Symlink)."""
    try:
        return entry.is_symlink() or (os.name == "nt" and bool(entry.stat().st_file_attributes & 0x400))
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
    """
    Intenta abrir el archivo en modo lectura binaria para detectar bloqueos exclusivos.
    """
    try:
        with open(path, "rb") as f:
            return False
    except (OSError, PermissionError, IOError):
        return True


def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Determina si la operación de destino implica un riesgo de recursividad.
    """
    try:
        s, d = src.resolve(), dest.resolve()
        return s == d or d.is_relative_to(s)
    except (OSError, RuntimeError, ValueError):
        return True


def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Realiza una validación exhaustiva de seguridad antes de operaciones de I/O.
    """
    try:
        if not src or not dest or not src.exists() or not src.is_file(): return False
        if not is_safe_to_modify(src) or not is_safe_to_modify(dest):
            return False
        if is_protected_path(src) or is_protected_path(dest):
            return False
            
        if _is_recursive_violation(src, dest):
            return False
            
        if not src.anchor or not dest.anchor or src.anchor != dest.anchor:
            return False
        
        stat = src.stat()
        if stat.st_size == 0: return False
        
        if os.name == "nt" and (stat.st_file_attributes & 0x46): 
            return False
        
        return not _is_file_locked(src)
    except (OSError, RuntimeError, AttributeError):
        return False


def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """Realiza un chequeo de alto nivel validando la integridad del JunkFile previo a moverlo."""
    if not isinstance(junk_file, JunkFile) or not junk_file.path: return False
    try:
        current_path: Path = junk_file.path
        if not current_path.exists() or is_protected_path(current_path) or is_protected_path(dest):
            return False
        return _is_safe_for_disk_op(current_path, dest)
    except (OSError, RuntimeError):
        return False


def _process_directory(current_dir: Path, found: List[JunkFile]) -> None:
    """
    Realiza un recorrido eficiente usando os.scandir para evitar syscalls innecesarias.
    """
    if is_protected_path(current_dir):
        return
        
    try:
        with os.scandir(current_dir) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _is_allowed_directory(entry.name) and not _is_junction(entry):
                            _process_directory(Path(entry.path), found)
                    elif entry.is_file() and entry.name.lower().endswith(JUNK_EXTENSIONS_TUPLE):
                        stats = entry.stat()
                        if stats.st_size > 0:
                            found.append(JunkFile(Path(entry.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime)))
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        pass


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """Escanea rutas específicas en busca de archivos basura."""
    search_dirs = [Path(d) for d in directories] if directories else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    
    for d in search_dirs:
        try:
            path_obj = Path(d).expanduser()
            if path_obj.exists() and not is_protected_path(path_obj):
                _process_directory(path_obj.resolve(), found)
        except (OSError, RuntimeError):
            continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena la lista de archivos basada en el registro SORT_REGISTRY."""
    if not isinstance(files, list) or not all(isinstance(f, JunkFile) for f in files):
        return []
        
    key: str = by.lower() if isinstance(by, str) else "size"
    config: SortConfig = SORT_REGISTRY.get(key, SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))


def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Evalúa la viabilidad de mover un archivo."""
    if not isinstance(junk_file.path, Path) or not dest_base: return None
    try:
        src_path = junk_file.path.resolve()
        if not src_path.exists() or not src_path.is_file() or not dest_base.is_dir(): 
            return None
        
        if not src_path.anchor or not dest_base.anchor or src_path.anchor != dest_base.anchor:
            return None

        if shutil.disk_usage(dest_base.anchor).free < src_path.stat().st_size: return None
        if not _is_safe_to_move(junk_file, dest_base):
            return None
        
        safe_name = f"{src_path.stem}_{int(junk_file.modified.timestamp())}{src_path.suffix}"
        target = (_generate_unique_target(dest_base / safe_name)).resolve()
        
        return target if target.is_relative_to(dest_base) else None
    except (OSError, ValueError, AttributeError):
        return None


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """Mueve los archivos especificados a un directorio de cuarentena para revisión."""
    if not files or not isinstance(review_dir, str) or not review_dir.strip():
        return None

    try:
        dest_base: Path = Path(review_dir).expanduser().resolve()
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        if not dest_base.is_dir() or not is_safe_to_modify(dest_base) or is_protected_path(dest_base): 
            return None
    except (OSError, PermissionError, RuntimeError):
        return None

    for junk_file in files:
        if not isinstance(junk_file, JunkFile) or not junk_file.path: continue
        try:
            target = _can_move_file(junk_file, dest_base)
            if target:
                ensure_safe_to_modify(junk_file.path)
                shutil.move(str(junk_file.path), str(target))
        except (OSError, PermissionError, shutil.Error, RuntimeError) as e:
            logger.error(f"Error moviendo {junk_file.path}: {e}")
            continue
    return dest_base


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Elimina permanentemente archivos desde la carpeta de revisión."""
    if not isinstance(review_dir, str) or not review_dir.strip():
        return 0

    try:
        dest: Path = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not dest.is_dir() or not is_safe_to_modify(dest) or is_protected_path(dest):
            return 0
    except (OSError, RuntimeError):
        return 0

    count: int = 0
    for item in dest.iterdir():
        try:
            # Validar existencia, tipo y pertenencia estricta a la carpeta de revisión
            if not item.is_file() or not item.exists():
                continue
            
            resolved_item = item.resolve()
            if not resolved_item.is_relative_to(dest):
                continue
            
            stat = item.stat()
            # Bloquear archivos de sistema o atributos especiales en Windows
            if os.name == "nt" and (stat.st_file_attributes & 0x46):
                continue

            if is_safe_to_modify(item) and not is_protected_path(item) and not _is_file_locked(item):
                ensure_safe_to_modify(item)
                item.unlink()
                count += 1
        except (PermissionError, OSError, ValueError) as e:
            logger.error(f"Error eliminando {item}: {e}")
            continue
    return count
