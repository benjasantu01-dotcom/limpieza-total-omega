"""
organizer.py
Organiza archivos "basura" (temporales, cache, descargas viejas, etc.)
en carpetas ordenadas por tamaño o fecha, sin borrar nada automáticamente.

Filosofía de seguridad: este módulo NUNCA borra archivos por sí solo.
Solo mueve candidatos a una carpeta de revisión ("_Para_Revisar") para
que el usuario descida qué borrar. Borrar es una acción explícita y
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
    """Verifica si la entrada es un punto de reparse (Junction/Symlink) para evitar bucles infinitos."""
    try:
        return entry.is_symlink() or (os.name == "nt" and bool(entry.stat().st_file_attributes & 0x400))
    except (OSError, AttributeError):
        return False


def _is_junk_path(path: Path) -> bool:
    """Comprueba si la extensión del archivo coincide con las extensiones de basura definidas."""
    return path.suffix.lower() in JUNK_EXTENSIONS


def _generate_unique_target(target: Path) -> Path:
    """Genera una ruta única para evitar sobreescritura, añadiendo sufijo numérico si el archivo existe."""
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
    """Valida si el nombre de una carpeta no está en la lista de carpetas críticas del sistema."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST


def _is_file_locked(path: Path) -> bool:
    """Intenta abrir un archivo en modo lectura binaria para detectar si está bloqueado por otro proceso."""
    try:
        with open(path, "rb") as f:
            return False
    except (OSError, PermissionError, IOError):
        return True


def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """Previene que el destino sea la misma carpeta o una carpeta padre de la fuente para evitar recursión circular."""
    try:
        s, d = src.resolve(), dest.resolve()
        return s == d or d.is_relative_to(s)
    except (OSError, RuntimeError, ValueError):
        return True


def _passes_system_checks(src: Path) -> bool:
    """Verifica si un archivo en Windows tiene atributos especiales de sistema o de solo lectura (FILE_ATTRIBUTE_SYSTEM/READONLY)."""
    if os.name != "nt": return True
    try:
        stat = src.stat()
        return not (stat.st_file_attributes & 0x407)
    except OSError:
        return False


def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """Realiza una batería de validaciones (existencia, permisos, límites de ruta) antes de ejecutar cualquier movimiento."""
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    
    # Validaciones básicas: evitar rutas extremas que fallan en Windows (260 chars MAX_PATH)
    if len(str(src)) > 240 or len(str(dest)) > 240: return False
    if not src.is_absolute() or not dest.is_absolute(): return False
    
    try:
        if not src.exists() or not src.is_file(): return False
        
        # Validación de protección de rutas y permisos de acceso
        if is_protected_path(src.resolve()) or is_protected_path(dest.resolve()): return False
        if not is_safe_to_modify(src) or not is_safe_to_modify(dest): return False
        if _is_recursive_violation(src, dest): return False
        
        # Validar permisos de escritura en origen y destino
        target_dir = dest.parent if dest.is_file() else dest
        if not (os.access(src, os.W_OK) and os.access(target_dir, os.W_OK)): return False
        
        # Misma unidad lógica requerida para shutil.move eficiente
        if src.drive != dest.drive: return False
        
        # Validaciones de atributos SO
        stat = src.stat()
        if os.name == "nt" and (stat.st_file_attributes & 0x400): return False
        return stat.st_size > 0 and _passes_system_checks(src) and not _is_file_locked(src)
    except (OSError, RuntimeError, AttributeError):
        return False


def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """Verifica si un objeto JunkFile es seguro para mover, validando tanto la existencia del origen como los permisos del destino."""
    return isinstance(junk_file, JunkFile) and junk_file.path.exists() and _is_safe_for_disk_op(junk_file.path, dest)


def _process_directory(current_dir: Path, found: List[JunkFile]) -> None:
    """Realiza un recorrido recursivo en profundidad del sistema de archivos, ignorando rutas protegidas o puntos de unión."""
    try:
        with os.scandir(current_dir) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _is_allowed_directory(entry.name) and not _is_junction(entry):
                            child_path = Path(entry.path)
                            if not is_protected_path(child_path):
                                _process_directory(child_path, found)
                    elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(JUNK_EXTENSIONS_TUPLE):
                        stats = entry.stat()
                        if stats.st_size > 0:
                            found.append(JunkFile(Path(entry.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime)))
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError, RuntimeError):
        pass


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """Escanea los directorios especificados en busca de archivos basura, delegando la recursión a _process_directory."""
    search_dirs = [Path(d) for d in directories] if directories else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    
    for d in search_dirs:
        try:
            path_obj = Path(d).expanduser()
            if path_obj.exists():
                resolved = path_obj.resolve()
                if not is_protected_path(resolved):
                    _process_directory(resolved, found)
        except (OSError, RuntimeError):
            continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena una lista de JunkFiles según un criterio registrado en SORT_REGISTRY."""
    if not isinstance(files, list) or not all(isinstance(f, JunkFile) for f in files):
        return []
        
    key: str = by.lower() if isinstance(by, str) else "size"
    config: SortConfig = SORT_REGISTRY.get(key, SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))


def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Valida la viabilidad de un movimiento (espacio en disco, seguridad) y propone una ruta de destino única."""
    try:
        src_path = junk_file.path.resolve()
        # Validar destino existente y montable
        if not dest_base.exists() or not dest_base.is_dir(): return None
        # Chequeo de espacio: dejar al menos 50MB de buffer en la unidad destino
        if shutil.disk_usage(dest_base.anchor).free < (junk_file.size_bytes + (50 * 1024 * 1024)): return None
        if not _is_safe_to_move(junk_file, dest_base): return None
        
        safe_name = f"{src_path.stem}_{int(junk_file.modified.timestamp())}{src_path.suffix}"
        target = (_generate_unique_target(dest_base / safe_name)).resolve()
        return target if target.is_relative_to(dest_base) else None
    except (OSError, ValueError, AttributeError):
        return None


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """Traslada una lista de archivos basura hacia un directorio de revisión, validando la seguridad de cada operación individualmente."""
    if not files or not isinstance(review_dir, str): return None

    try:
        dest_base: Path = Path(review_dir).expanduser().resolve()
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        if not is_safe_to_modify(dest_base) or is_protected_path(dest_base): return None
    except (OSError, RuntimeError):
        return None

    for junk_file in files:
        try:
            if not isinstance(junk_file, JunkFile): continue
            if junk_file.path.resolve().is_relative_to(dest_base): continue
            
            target = _can_move_file(junk_file, dest_base)
            if target and is_safe_to_modify(junk_file.path):
                ensure_safe_to_modify(junk_file.path)
                shutil.move(str(junk_file.path), str(target))
        except (OSError, PermissionError, shutil.Error, RuntimeError) as e:
            logger.error(f"Error moviendo {junk_file.path}: {e}")
    return dest_base


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Elimina permanentemente los archivos contenidos en el directorio de revisión, previa validación de integridad."""
    if not isinstance(review_dir, str): return 0

    try:
        dest: Path = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not is_safe_to_modify(dest) or is_protected_path(dest): return 0
    except (OSError, RuntimeError):
        return 0

    count: int = 0
    for item in dest.iterdir():
        try:
            if item.is_file() and is_safe_to_modify(item) and not is_protected_path(item):
                if _passes_system_checks(item) and not _is_file_locked(item):
                    ensure_safe_to_modify(item)
                    item.unlink()
                    count += 1
        except (PermissionError, OSError, ValueError) as e:
            logger.error(f"Error eliminando {item}: {e}")
    return count
