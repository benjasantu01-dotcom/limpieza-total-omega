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
JUNK_EXTENSIONS: Final[set[str]] = {
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
}
# Pre-calculado para eficiencia en loops
_LOWER_JUNK_EXTS: Final[set[str]] = {ext.lower() for ext in JUNK_EXTENSIONS}

# Carpetas típicas donde se acumula basura
DEFAULT_SCAN_DIRS: Final[List[Path]] = [
    Path(os.environ.get("TEMP", "C:\\Temp")),
    Path(os.environ.get("LOCALAPPDATA", "C:\\")) / "Temp",
    Path.home() / "Downloads",
]

# Carpetas de sistema críticas que nunca se recorren para prevenir daños al SO
SYSTEM_FOLDER_BLOCKLIST: Final[set[str]] = {
    "windows", "program files", "program files (x86)", "$recycle.bin", "system volume information"
}


def list_available_drives() -> List[str]:
    """
    Detecta unidades montadas en Windows mediante el barrido de letras de unidad.
    
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
    Utiliza el atributo 0x400 (FILE_ATTRIBUTE_REPARSE_POINT) en Windows para evitar 
    entrar en bucles infinitos de montaje de disco.
    """
    try:
        return path.is_symlink() or (os.name == "nt" and bool(path.stat().st_file_attributes & 0x400))
    except (OSError, AttributeError):
        return False


def _is_junk_path(path: Path) -> bool:
    """Valida la extensión del archivo contra la lista permitida de basura."""
    return path.suffix.lower() in _LOWER_JUNK_EXTS


def _generate_unique_target(target: Path) -> Path:
    """
    Calcula un nombre de archivo único para evitar colisiones durante el movimiento.
    Implementa una estrategia de sufijo numérico (_1, _2...) para prevenir sobrescrituras 
    accidentales si el archivo ya existe en la carpeta de destino.
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
    Verifica si un archivo está bloqueado intentando abrirlo en modo lectura binaria exclusiva.
    La incapacidad de obtener un descriptor de archivo indica que el SO tiene el archivo 
    en uso (ej. proceso activo, log abierto).
    """
    try:
        with open(path, "rb") as f:
            return False
    except (OSError, PermissionError, IOError):
        return True


def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Previene la recursividad infinita durante operaciones de archivo verificando 
    que el destino no sea un ancestro del origen (evitando mover carpetas dentro de sí mismas).
    """
    try:
        s, d = src.resolve(), dest.resolve()
        return s == d or d.is_relative_to(s)
    except (OSError, RuntimeError, ValueError):
        return True


def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Valida la integridad de una operación de E/S.
    Combina checks de `safety.py` con verificaciones de sistema (archivos ocultos/sistema) 
    y validaciones de estado de archivo (locks) para asegurar que el movimiento es seguro.
    """
    try:
        if not is_safe_to_modify(src) or not is_safe_to_modify(dest):
            return False
        if _is_recursive_violation(src, dest) or src.anchor != dest.anchor:
            return False
        
        stat = src.stat()
        if not stat.st_mode: return False
        
        if os.name == "nt":
            # 0x02: Hidden, 0x04: System, 0x40: Reparse Point
            if stat.st_file_attributes & 0x46: 
                return False
        
        return not _is_file_locked(src)
    except (OSError, RuntimeError, AttributeError):
        return False


def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """
    Validación de alto nivel antes del movimiento.
    Verifica la existencia del objeto, protección de rutas mediante listas blancas/negras 
    y validación final de la operación de E/S.
    """
    if not isinstance(junk_file, JunkFile): return False
    try:
        current_path: Path = junk_file.path
        if not current_path.exists() or is_protected_path(current_path) or is_protected_path(dest):
            return False
        return _is_safe_for_disk_op(current_path, dest)
    except (OSError, RuntimeError):
        return False


def _is_valid_junk_candidate(entry: os.DirEntry) -> bool:
    """Filtro de archivos optimizado usando os.DirEntry para reducir llamadas a stat()."""
    return entry.is_file() and entry.name.lower().endswith(tuple(_LOWER_JUNK_EXTS))


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Recorre recursivamente los directorios buscando archivos clasificados como basura.
    Usa `os.scandir` para minimizar el impacto de I/O aprovechando metadatos en caché del sistema.
    """
    search_dirs = [Path(d) for d in directories] if directories else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    
    def _traverse(current_dir: Path):
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if _is_allowed_directory(entry.name) and not _is_junction(Path(entry.path)):
                                _traverse(Path(entry.path))
                        elif _is_valid_junk_candidate(entry):
                            stats = entry.stat()
                            found.append(JunkFile(Path(entry.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime)))
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            pass

    for d in search_dirs:
        try:
            base = Path(d).expanduser().resolve()
            if base.is_dir():
                _traverse(base)
        except (OSError, RuntimeError):
            continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """
    Ordena la lista de archivos basada en el registro `SORT_REGISTRY`.
    Permite extender los criterios de ordenamiento sin modificar la lógica interna.
    """
    if not isinstance(files, list) or not all(isinstance(f, JunkFile) for f in files):
        return []
        
    key: str = by.lower() if isinstance(by, str) else "size"
    config: SortConfig = SORT_REGISTRY.get(key, SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Mueve los archivos candidatos a una carpeta de revisión segura.
    Realiza una verificación de espacio disponible en disco y validación de seguridad 
    `ensure_safe_to_modify` antes de cada operación de mover archivo.
    """
    if not files or not isinstance(review_dir, str) or not review_dir.strip():
        return None

    try:
        dest_base: Path = Path(review_dir).expanduser().resolve()
        # Validación de seguridad: el destino debe ser una ruta permitida
        ensure_safe_to_modify(dest_base.parent)
        if is_protected_path(dest_base): return None
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        if not dest_base.is_dir() or not is_safe_to_modify(dest_base): return None
    except (OSError, PermissionError, RuntimeError):
        return None

    total_size = sum(f.size_bytes for f in files if isinstance(f, JunkFile))
    try:
        if shutil.disk_usage(dest_base).free < total_size:
            return None
    except OSError:
        return None

    for junk_file in files:
        if not isinstance(junk_file, JunkFile): continue
        try:
            src_path: Path = junk_file.path.resolve()
            if src_path.anchor != dest_base.anchor: continue
            if not src_path.exists() or not _is_safe_to_move(junk_file, dest_base): continue
            
            safe_name = f"{src_path.stem}_{int(junk_file.modified.timestamp())}{src_path.suffix}"
            target = _generate_unique_target(dest_base / safe_name).resolve()
            
            if not target.is_relative_to(dest_base): continue
                
            ensure_safe_to_modify(src_path)
            shutil.move(str(src_path), str(target))
        except (OSError, PermissionError, shutil.Error, RuntimeError):
            logger.warning(f"No se pudo mover el archivo: {junk_file.path}")
            continue
    return dest_base


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina archivos de forma segura tras la revisión explícita del usuario.
    Verifica mediante `ensure_safe_to_modify` que el archivo sea seguro antes de la 
    operación unlink, garantizando que no se eliminen elementos protegidos.
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
            if not item.is_file() or _is_junction(item): continue
            if not item.is_relative_to(dest): continue
            
            # Validación lógica: check de seguridad antes de ejecutar unlink
            if is_safe_to_modify(item) and not _is_file_locked(item):
                ensure_safe_to_modify(item)
                item.unlink()
                count += 1
        except (PermissionError, OSError, ValueError):
            continue
    return count
