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
_JUNK_EXT_TUPLE: Final[tuple[str, ...]] = tuple(_LOWER_JUNK_EXTS)

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
        List[str]: Lista de rutas raíz (ej: ['C:\\', 'D:\\']). Retorna lista vacía si no es Windows.
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
        """Asegura la normalización de la ruta tras la instanciación."""
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


def _is_junction(entry: os.DirEntry[str]) -> bool:
    """
    Determina si una entrada de sistema de archivos es un punto de reparse (Junction/Symlink).
    
    Args:
        entry: La entrada del directorio a evaluar.
    Returns:
        bool: True si es un enlace, False en caso contrario o error de acceso.
    """
    try:
        return entry.is_symlink() or (os.name == "nt" and "reparse" in os.stat(entry.path).st_file_attributes)
    except (OSError, AttributeError):
        return False


def _is_junk_path(path: Path) -> bool:
    """Valida si el archivo posee una extensión categorizada como 'basura'."""
    return path.suffix.lower() in _LOWER_JUNK_EXTS


def _generate_unique_target(target: Path) -> Path:
    """
    Genera una ruta única para un archivo destino evitando colisiones por nombre.
    Si el destino existe, añade un sufijo numérico incremental.
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
    """Valida si un nombre de directorio no forma parte de la lista de bloqueo del sistema."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST


def _is_file_locked(path: Path) -> bool:
    """
    Verifica si un archivo está en uso exclusivo intentando abrirlo en modo lectura exclusiva.
    
    Args:
        path: Ruta del archivo a verificar.
    Returns:
        bool: True si el archivo está bloqueado por otro proceso o inaccesible.
    """
    try:
        # Intentar abrir en modo lectura exclusiva sin modificar metadatos
        with open(path, "rb") as f:
            # Si el archivo está vacío, no hay mucho que testear, asumimos libre.
            return False
    except (OSError, PermissionError, IOError):
        return True

def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """
    Evalúa si el movimiento es seguro basándose en:
    1. Existencia y atributos de archivo (no oculto/sistema).
    2. Evitación de ciclos (no mover dentro de sí mismo).
    3. Bloqueos de archivo y consistencia de unidad (debe ser la misma).
    4. Validación de seguridad mediante `is_safe_to_modify`.
    """
    try:
        current_abs = junk_file.path.resolve()
        dest_abs = dest.resolve()
        
        if not current_abs.exists() or not current_abs.is_file() or current_abs.parent == current_abs:
            return False
        
        if os.name == "nt":
            # 0x02: Hidden, 0x04: System
            if current_abs.stat().st_file_attributes & 0x06: 
                return False

        # Evitar ciclos o jerarquías no válidas: mover a sí mismo o subcarpetas
        if current_abs == dest_abs or dest_abs in current_abs.parents or current_abs.parent == dest_abs:
            return False
        
        # Validar bloqueos y atomicidad (misma unidad de disco necesaria)
        if _is_file_locked(current_abs) or current_abs.anchor != dest_abs.anchor:
            return False
            
        return is_safe_to_modify(current_abs) and is_safe_to_modify(dest_abs)
    except (OSError, RuntimeError, AttributeError):
        return False


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Escanea rutas recursivamente buscando archivos basura.
    
    Args:
        directories: Lista opcional de rutas a escanear. Si es None, usa DEFAULT_SCAN_DIRS.
    Returns:
        List[JunkFile]: Lista de objetos JunkFile encontrados y validados.
    """
    raw_dirs = directories if directories is not None else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    
    unique_dirs: set[Path] = set()
    for d in raw_dirs:
        if d and isinstance(d, str):
            try:
                p = Path(d).expanduser().resolve()
                if p.exists() and p.is_dir() and is_safe_to_modify(p):
                    unique_dirs.add(p)
            except (RuntimeError, OSError, ValueError):
                continue

    def _walk_dir(base_path: Path) -> None:
        try:
            with os.scandir(base_path) as it:
                for entry in it:
                    if _is_junction(entry):
                        continue
                    if entry.is_dir():
                        if _is_allowed_directory(entry.name):
                            _walk_dir(Path(entry.path))
                    elif entry.is_file() and entry.name.lower().endswith(_JUNK_EXT_TUPLE):
                        path_obj = Path(entry.path)
                        if is_safe_to_modify(path_obj):
                            try:
                                stat = entry.stat()
                                found.append(JunkFile(
                                    path=path_obj,
                                    size_bytes=stat.st_size,
                                    modified=datetime.fromtimestamp(stat.st_mtime)
                                ))
                            except OSError:
                                continue
        except (PermissionError, OSError):
            pass

    for d in unique_dirs:
        _walk_dir(d)
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """
    Ordena una lista de archivos basura según un criterio dado.

    Args:
        files: Lista de objetos JunkFile a ordenar.
        by: Campo por el cual ordenar ("size" o "date").
        ascending: Booleano para orden ascendente o descendente.
    Returns:
        List[JunkFile]: Nueva lista ordenada.
    """
    if not isinstance(files, list):
        return []
        
    registry: Dict[str, SortConfig] = {
        "size": SortConfig("size", lambda f: f.size_bytes),
        "date": SortConfig("date", lambda f: f.modified)
    }
        
    criterio = by.lower() if isinstance(by, str) else "size"
    config = registry.get(criterio, registry["size"])
        
    valid_files = [f for f in files if isinstance(f, JunkFile)]
    return sorted(valid_files, key=config.key_func, reverse=not bool(ascending))


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """
    Traslada archivos basura detectados a un directorio de revisión.

    Args:
        files: Lista de objetos JunkFile a procesar.
        review_dir: Ruta donde se moverán los archivos.
    Returns:
        Path: Ruta final del directorio de revisión.
    """
    if not isinstance(files, list) or not isinstance(review_dir, str) or not review_dir.strip():
        return Path(".")

    dest: Path = Path(review_dir).expanduser().resolve()
    
    # Validar que la ruta de destino no sea raíz ni esté protegida
    if not dest or dest == dest.parent or not is_safe_to_modify(dest):
        return dest
        
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        return dest

    for junk_file in files:
        if not isinstance(junk_file, JunkFile):
            continue
        try:
            # Validar que el archivo aún exista y sea un archivo real antes de mover
            if junk_file.path.exists() and junk_file.path.is_file():
                if os.access(junk_file.path, os.R_OK) and _is_safe_to_move(junk_file, dest):
                    usage = shutil.disk_usage(dest)
                    if usage.free > junk_file.size_bytes:
                        target = _generate_unique_target(dest / f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}")
                        # Verifica nuevamente la seguridad absoluta de la ruta resultante
                        if dest == target.parent and is_safe_to_modify(target):
                            shutil.move(str(junk_file.path), str(target))
        except (PermissionError, OSError, shutil.Error, RuntimeError):
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina archivos de la carpeta de revisión tras validación de seguridad.
    
    Args:
        review_dir: Directorio de donde borrar los archivos revisados.
    Returns:
        int: Cantidad de archivos eliminados exitosamente.
    """
    if not isinstance(review_dir, str) or not review_dir.strip():
        return 0

    dest: Path = Path(review_dir).expanduser().resolve()
    # Verifica que la carpeta exista, sea directorio y no sea ruta crítica
    if not dest.exists() or not dest.is_dir() or not is_safe_to_modify(dest):
        return 0

    count: int = 0
    try:
        dest_str = str(dest)
        for item in dest.iterdir():
            try:
                # Validar tipo y seguridad antes de intentar borrar
                if item.is_file() and not item.is_symlink():
                    path_to_delete = item.resolve()
                    # Verificación de Sandbox: asegurarse que el archivo está estrictamente bajo dest
                    if os.path.commonpath([dest_str, str(path_to_delete)]) == dest_str:
                        if is_safe_to_modify(path_to_delete):
                            path_to_delete.unlink()
                            count += 1
            except (PermissionError, OSError, ValueError):
                continue
    except (PermissionError, OSError):
        pass
    return count
