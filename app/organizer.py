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
    Retorna True si la ruta es un vínculo simbólico o un punto de unión de Windows.
    """
    try:
        # Se requiere chequeo de reparse point para evitar seguir enlaces circulares o de sistema
        return path.is_symlink() or (os.name == "nt" and "reparse" in os.stat(path).st_file_attributes)
    except (OSError, AttributeError):
        return False


def _is_junk_path(path: Path) -> bool:
    """Verifica la extensión contra la lista global de extensiones temporales."""
    return path.suffix.lower() in _LOWER_JUNK_EXTS


def _generate_unique_target(target: Path) -> Path:
    """
    Resuelve colisiones de nombres añadiendo un contador incremental.
    Utiliza el patrón: nombre_1.ext, nombre_2.ext, etc.
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
    """Filtra directorios basándose en la lista negra de sistemas críticos."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST


def _is_file_locked(path: Path) -> bool:
    """
    Verifica si un archivo está bloqueado intentando abrirlo en modo exclusivo.
    Útil para evitar mover archivos que están en uso por el sistema o procesos.
    """
    try:
        with open(path, "rb") as f:
            return False
    except (OSError, PermissionError, IOError):
        return True


def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Valida que la operación no cause recursión accidental (ej. mover un padre a un hijo).
    """
    return src == dest or src == dest.parent or dest in src.parents


def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Validación de seguridad integral antes de cualquier operación de I/O.
    Verifica existencia, naturaleza del archivo y bloqueos de acceso antes de proceder.
    """
    try:
        if not src.exists() or not src.is_file() or _is_junction(src):
            return False
        
        # Ignorar archivos con atributo 'System' (0x40) para evitar tocar ejecutables críticos
        if os.name == "nt" and hasattr(src.stat(), 'st_file_attributes') and (src.stat().st_file_attributes & 0x40):
            return False
        
        src_abs, dest_abs = src.resolve(), dest.resolve()
        
        if _is_recursive_violation(src_abs, dest_abs):
            return False
        
        # Bloqueos de sistema (atributo de archivo del sistema en Windows)
        if os.name == "nt" and hasattr(src_abs.stat(), 'st_file_attributes') and (src_abs.stat().st_file_attributes & 0x06):
            return False
            
        return is_safe_to_modify(src_abs) and is_safe_to_modify(dest_abs) and not _is_file_locked(src_abs)
    except (OSError, RuntimeError, AttributeError):
        return False


def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """Valida la seguridad del movimiento, asegurando que ni origen ni destino estén protegidos."""
    if not isinstance(junk_file, JunkFile): return False
    current_path = junk_file.path
    dest_abs = dest.resolve()
    
    if is_protected_path(current_path) or is_protected_path(dest_abs):
        return False

    if not _is_safe_for_disk_op(current_path, dest_abs):
        return False
        
    # Impedir movimiento entre particiones diferentes si no está implementado
    if current_path.anchor != dest_abs.anchor:
        return False
        
    return True


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Escanea directorios buscando candidatos de limpieza de forma recursiva.
    Filtra automáticamente carpetas protegidas y enlaces simbólicos.
    """
    raw_dirs = directories if directories is not None else DEFAULT_SCAN_DIRS
    if not isinstance(raw_dirs, list): return []
    
    found: List[JunkFile] = []
    
    for d in raw_dirs:
        if not d or not isinstance(d, str): continue
        try:
            base = Path(d).expanduser().resolve()
            if not base.exists() or not base.is_dir() or not is_safe_to_modify(base): 
                continue
            
            for root, dirs, files in os.walk(base):
                root_path = Path(root)
                # Filtra subdirectorios in situ para optimizar el walk
                dirs[:] = [d_name for d_name in dirs if _is_allowed_directory(d_name) and not _is_junction(root_path / d_name)]
                
                for name in files:
                    # Acceso rápido a la extensión sin instanciar Path innecesariamente
                    _, ext = os.path.splitext(name)
                    if ext.lower() in _LOWER_JUNK_EXTS:
                        f_path = root_path / name
                        if is_safe_to_modify(f_path) and not _is_junction(f_path):
                            try:
                                s = f_path.stat()
                                found.append(JunkFile(f_path, s.st_size, datetime.fromtimestamp(s.st_mtime)))
                            except (OSError, PermissionError):
                                continue
        except (OSError, RuntimeError):
            continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena una lista de archivos basura según un criterio configurado."""
    if not isinstance(files, list):
        return []
        
    registry: Dict[str, SortConfig] = {
        "size": SortConfig("size", lambda f: f.size_bytes),
        "date": SortConfig("date", lambda f: f.modified)
    }
        
    criterio = by.lower() if isinstance(by, str) else "size"
    config = registry.get(criterio, registry["size"])
        
    return sorted(files, key=config.key_func, reverse=not bool(ascending))


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Mueve de forma segura los archivos a una carpeta intermedia para su revisión.
    Realiza validaciones de seguridad antes de cada operación individual.
    """
    if not files or not isinstance(review_dir, str) or not review_dir.strip():
        return None

    try:
        dest_base = Path(review_dir).expanduser().resolve()
        if not dest_base.exists():
            dest_base.mkdir(parents=True, exist_ok=True)
        if not dest_base.is_dir() or not is_safe_to_modify(dest_base): 
            return None
    except (OSError, PermissionError, RuntimeError):
        return None

    for junk_file in files:
        if not isinstance(junk_file, JunkFile):
            continue
        try:
            src_path = junk_file.path.resolve()
            if not src_path.exists() or not _is_safe_to_move(junk_file, dest_base):
                continue
            
            # Verificar espacio antes de mover
            if shutil.disk_usage(dest_base).free <= junk_file.size_bytes:
                continue
                
            target = _generate_unique_target(dest_base / f"{src_path.stem}_{int(junk_file.modified.timestamp())}{src_path.suffix}")
            
            # Verificar que el destino final sigue dentro de los límites esperados (path traversal)
            if not target.resolve().is_relative_to(dest_base):
                continue

            ensure_safe_to_modify(src_path)
            ensure_safe_to_modify(target)
            
            if src_path.exists():
                shutil.move(str(src_path), str(target))
        except (OSError, PermissionError, shutil.Error, RuntimeError):
            continue
    return dest_base


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina archivos de forma permanente tras verificar que residan en la zona de cuarentena.
    El borrado es una operación crítica validada mediante is_safe_to_modify.
    """
    if not isinstance(review_dir, str) or not review_dir.strip():
        return 0

    try:
        dest = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not dest.is_dir() or not is_safe_to_modify(dest):
            return 0
    except (OSError, RuntimeError):
        return 0

    count: int = 0
    for item in dest.iterdir():
        try:
            if not item or not item.exists() or not item.is_file() or _is_junction(item):
                continue
            
            resolved_item = item.resolve()
            # Asegurar que el ítem realmente está bajo el directorio de revisión antes de borrar
            if resolved_item.is_relative_to(dest) and is_safe_to_modify(resolved_item):
                if not _is_file_locked(resolved_item):
                    ensure_safe_to_modify(resolved_item)
                    if resolved_item.exists():
                        resolved_item.unlink()
                        count += 1
        except (PermissionError, OSError, ValueError):
            continue
    return count
