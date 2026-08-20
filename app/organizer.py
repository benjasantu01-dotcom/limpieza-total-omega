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
    """Detecta unidades montadas en Windows. Retorna lista de rutas raíz."""
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
        """Asegura que la ruta almacenada sea un objeto Path absoluto."""
        self.path = Path(self.path).resolve()

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
    Detecta si la ruta es un punto de reparse (Junction/Symlink).
    Previene bucles infinitos y operaciones accidentales sobre redirecciones de sistema.
    """
    try:
        if not path.exists(): return False
        # Windows: Verifica atributo FILE_ATTRIBUTE_REPARSE_POINT (0x400) o si es symlink
        return path.is_symlink() or (os.name == "nt" and bool(path.stat().st_file_attributes & 0x400))
    except (OSError, AttributeError):
        return False


def _is_junk_path(path: Path) -> bool:
    """Verifica la extensión contra la lista global de extensiones temporales."""
    return path.suffix.lower() in _LOWER_JUNK_EXTS


def _generate_unique_target(target: Path) -> Path:
    """
    Resuelve colisiones de nombres añadiendo un contador incremental.
    Evita sobrescritura accidental durante la fase de movimiento a revisión.
    """
    if not target.exists():
        return target
        
    parent: Path = target.parent
    stem: str = target.stem
    suffix: str = target.suffix
    counter: int = 1
    
    while (candidate := parent / f"{stem}_{counter}{suffix}").exists():
        counter += 1
        if counter > 999: break # Safety break
    return candidate


def _is_allowed_directory(name: str) -> bool:
    """Filtra directorios basándose en la lista negra de sistemas críticos."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST


def _is_file_locked(path: Path) -> bool:
    """
    Intenta abrir el archivo en modo escritura exclusiva. 
    Si falla, se asume ocupado por el sistema o por otro proceso.
    """
    try:
        with open(path, "rb") as f:
            return False
    except (OSError, PermissionError, IOError):
        return True


def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Valida que el destino no sea un contenedor del origen, lo que causaría
    una recursión infinita o un bucle lógico al mover archivos.
    """
    try:
        src_abs = src.resolve()
        dest_abs = dest.resolve()
        return src_abs == dest_abs or dest_abs.is_relative_to(src_abs)
    except (OSError, RuntimeError, ValueError):
        return True


def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Validación de seguridad integral antes de cualquier operación de I/O.
    Filtra atributos ocultos/sistema (Win) y verifica permisos de escritura.
    """
    try:
        if not src.exists() or not src.is_file() or _is_junction(src):
            return False
        if os.name == "nt":
            attrs = src.stat().st_file_attributes
            # 0x02: Hidden, 0x04: System, 0x40: Reparse point
            if (attrs & 0x02) or (attrs & 0x04) or (attrs & 0x40):
                return False
        if _is_recursive_violation(src, dest):
            return False
        return is_safe_to_modify(src.resolve()) and is_safe_to_modify(dest.resolve()) and not _is_file_locked(src)
    except (OSError, RuntimeError, AttributeError):
        return False


def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """Valida la seguridad del movimiento, asegurando que ni origen ni destino estén protegidos."""
    if not isinstance(junk_file, JunkFile): return False
    try:
        current_path = junk_file.path
        if is_protected_path(current_path) or is_protected_path(dest.resolve()):
            return False
        if not _is_safe_for_disk_op(current_path, dest):
            return False
        # No permitir cruce entre diferentes volúmenes para evitar errores de copia
        return current_path.anchor == dest.resolve().anchor
    except (OSError, RuntimeError):
        return False


def _is_valid_junk_candidate(path: Path) -> bool:
    """Determina si un archivo es un candidato legítimo para ser clasificado como basura."""
    try:
        return _is_junk_path(path) and not _is_junction(path) and path.is_file()
    except (OSError, RuntimeError):
        return False


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """Escanea directorios buscando candidatos de limpieza de forma recursiva."""
    raw_dirs: List[str] = directories if directories is not None else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    
    for d in raw_dirs:
        if not isinstance(d, str) or not d: continue
        try:
            base: Path = Path(d).expanduser().resolve()
            # Validación única inicial del directorio base para evitar re-chequeos innecesarios
            if not base.exists() or not base.is_dir() or not is_safe_to_modify(base): 
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
    """Ordena una lista de archivos basura según un criterio configurado."""
    if not isinstance(files, list): return []
        
    registry: Dict[str, SortConfig] = {
        "size": SortConfig("size", lambda f: f.size_bytes),
        "date": SortConfig("date", lambda f: f.modified)
    }
    config: SortConfig = registry.get(by.lower() if isinstance(by, str) else "size", registry["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Mueve los archivos a una carpeta intermedia de revisión.
    Utiliza el motor de seguridad para asegurar que no se manipule fuera del scope permitido.
    """
    if not files or not isinstance(review_dir, str) or not review_dir.strip():
        return None

    try:
        dest_base: Path = Path(review_dir).expanduser().resolve()
        if not dest_base.exists():
            dest_base.mkdir(parents=True, exist_ok=True)
        if not dest_base.is_dir() or not is_safe_to_modify(dest_base): 
            return None
    except (OSError, PermissionError, RuntimeError):
        return None

    for junk_file in files:
        if not isinstance(junk_file, JunkFile): continue
        try:
            src_path: Path = junk_file.path.resolve()
            if not src_path.is_file() or not _is_safe_to_move(junk_file, dest_base):
                continue
            
            target: Path = _generate_unique_target(dest_base / f"{src_path.stem}_{int(junk_file.modified.timestamp())}{src_path.suffix}")
            
            ensure_safe_to_modify(src_path)
            shutil.move(str(src_path), str(target))
        except (OSError, PermissionError, shutil.Error, RuntimeError):
            continue
    return dest_base


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina archivos tras verificar que residen exclusivamente en la zona de cuarentena.
    Solo se permiten archivos; se ignoran subcarpetas por seguridad.
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
            if not item.is_file() or _is_junction(item):
                continue
            resolved_item: Path = item.resolve()
            # Validar que el archivo no haya sido movido fuera del directorio de revisión
            if resolved_item.is_relative_to(dest) and is_safe_to_modify(resolved_item):
                if not _is_file_locked(resolved_item):
                    ensure_safe_to_modify(resolved_item)
                    resolved_item.unlink()
                    count += 1
        except (PermissionError, OSError, ValueError):
            continue
    return count
