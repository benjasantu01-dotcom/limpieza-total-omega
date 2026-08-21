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
        List[str]: Lista de rutas raíz (ej. ['C:\\', 'D:\\']). Retorna lista vacía si no es Windows.
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
        """Normaliza la ruta a absoluta para evitar ambigüedades en comparaciones."""
        self.path = self.path.resolve()

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño en MB redondeado a 2 decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión del archivo coincide con los criterios de limpieza."""
        return _is_junk_path(self.path)


def _is_junction(path: Path) -> bool:
    """
    Detecta puntos de reparse (Junctions/Symlinks).
    Evita que el escáner siga punteros hacia fuera de la estructura de archivos local.
    """
    try:
        return path.is_symlink() or (os.name == "nt" and bool(path.stat().st_file_attributes & 0x400))
    except (OSError, AttributeError):
        return False


def _is_junk_path(path: Path) -> bool:
    """Valida la extensión del archivo contra la lista permitida."""
    return path.suffix.lower() in _LOWER_JUNK_EXTS


def _generate_unique_target(target: Path) -> Path:
    """
    Calcula un nombre de archivo único para evitar colisiones durante el movimiento.
    Si el destino existe, añade un sufijo numérico incremental hasta 999.
    """
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
    Intenta verificar si el archivo está siendo usado por otro proceso.
    Si el archivo no se puede abrir en modo lectura, se asume bloqueado.
    """
    try:
        with open(path, "rb") as f:
            return False
    except (OSError, PermissionError, IOError):
        return True


def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Verifica si el destino de la operación es hijo o el mismo directorio del origen,
    lo cual causaría bucles infinitos en el sistema de archivos.
    """
    try:
        s, d = src.resolve(), dest.resolve()
        return s == d or d.is_relative_to(s)
    except (OSError, RuntimeError, ValueError):
        return True


def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Valida la seguridad de la operación de E/S.
    Rechaza archivos ocultos, de sistema, reparse points o archivos bloqueados.
    """
    try:
        if not src.exists() or not src.is_file() or _is_junction(src):
            return False
        if os.name == "nt":
            attrs: int = src.stat().st_file_attributes
            if (attrs & 0x02) or (attrs & 0x04) or (attrs & 0x40):
                return False
        if _is_recursive_violation(src, dest):
            return False
        return is_safe_to_modify(src) and is_safe_to_modify(dest) and not _is_file_locked(src)
    except (OSError, RuntimeError, AttributeError):
        return False


def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """
    Validación final antes del movimiento: verifica que no se violen las reglas 
    de seguridad del módulo `safety` y que el movimiento no cruce volúmenes físicos.
    """
    if not isinstance(junk_file, JunkFile): return False
    try:
        current_path: Path = junk_file.path
        if not current_path.exists() or is_protected_path(current_path) or is_protected_path(dest):
            return False
        if not _is_safe_for_disk_op(current_path, dest):
            return False
        return current_path.anchor == dest.anchor
    except (OSError, RuntimeError):
        return False


def _is_valid_junk_candidate(path: Path) -> bool:
    """Determina si una ruta es un candidato legítimo para ser procesado."""
    try:
        return _is_junk_path(path) and not _is_junction(path) and path.is_file()
    except (OSError, RuntimeError):
        return False


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Recorre recursivamente los directorios buscando archivos clasificados como basura.
    
    Args:
        directories: Lista de rutas a escanear. Si es None, usa DEFAULT_SCAN_DIRS.
        
    Returns:
        List[JunkFile]: Lista de objetos JunkFile encontrados.
    """
    raw_dirs: List[str] = directories if directories is not None else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    
    for d in raw_dirs:
        if not isinstance(d, str) or not d: continue
        try:
            base: Path = Path(d).expanduser().resolve()
            if not base.is_dir() or not is_safe_to_modify(base): 
                continue
            
            for root, dirs, files in os.walk(base):
                root_path: Path = Path(root)
                dirs[:] = [dn for dn in dirs if _is_allowed_directory(dn) and not _is_junction(root_path / dn)]
                
                for name in files:
                    file_path: Path = root_path / name
                    if _is_valid_junk_candidate(file_path):
                        try:
                            stats: os.stat_result = file_path.stat()
                            found.append(JunkFile(file_path, stats.st_size, datetime.fromtimestamp(stats.st_mtime)))
                        except (OSError, PermissionError):
                            continue
        except (OSError, RuntimeError):
            continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """
    Ordena los archivos encontrados según un criterio definido (tamaño o fecha).
    
    Args:
        files: Lista de JunkFile a ordenar.
        by: 'size' para tamaño, 'date' para fecha de modificación.
        ascending: True para orden ascendente, False para descendente.
    """
    if not isinstance(files, list): return []
        
    registry: Dict[str, SortConfig] = {
        "size": SortConfig("size", lambda f: f.size_bytes),
        "date": SortConfig("date", lambda f: f.modified)
    }
    config: SortConfig = registry.get(by.lower() if isinstance(by, str) else "size", registry["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Mueve los archivos a una carpeta de revisión.
    """
    if not files or not isinstance(review_dir, str) or not review_dir.strip():
        return None

    try:
        dest_base: Path = Path(review_dir).expanduser().resolve()
        if not dest_base.exists():
            dest_base.mkdir(parents=True, exist_ok=True)
        # Validación de seguridad y permisos de escritura
        if not dest_base.is_dir() or not is_safe_to_modify(dest_base) or not os.access(dest_base, os.W_OK): 
            return None
    except (OSError, PermissionError, RuntimeError):
        return None

    for junk_file in files:
        if not isinstance(junk_file, JunkFile): continue
        try:
            src_path: Path = junk_file.path
            if not src_path.exists() or _is_recursive_violation(src_path, dest_base) or not _is_safe_to_move(junk_file, dest_base):
                continue
            
            safe_name = f"{src_path.stem}_{int(junk_file.modified.timestamp())}{src_path.suffix}"
            target: Path = _generate_unique_target(dest_base / safe_name)
            
            ensure_safe_to_modify(src_path)
            shutil.move(str(src_path), str(target))
        except (OSError, PermissionError, shutil.Error, RuntimeError):
            continue
    return dest_base


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina archivos de forma segura. 
    Solo procesa archivos dentro de la carpeta de revisión verificada.
    """
    if not isinstance(review_dir, str) or not review_dir.strip():
        return 0

    try:
        dest: Path = Path(review_dir).expanduser().resolve()
        if not dest.is_dir() or not is_safe_to_modify(dest):
            return 0
    except (OSError, RuntimeError):
        return 0

    count: int = 0
    for item in dest.iterdir():
        try:
            # Asegurar que el ítem es un archivo y no un directorio o reparse point
            if not item.is_file() or _is_junction(item):
                continue
            if item.is_relative_to(dest) and is_safe_to_modify(item):
                if not _is_file_locked(item):
                    ensure_safe_to_modify(item)
                    item.unlink()
                    count += 1
        except (PermissionError, OSError, ValueError):
            continue
    return count
