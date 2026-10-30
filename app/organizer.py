"""
organizer.py

Este módulo implementa la lógica de detección, validación y gestión de archivos 
temporales (junk files) detectados en el sistema.

Su responsabilidad es identificar archivos candidatos a limpieza basados en 
extensiones y antigüedad, moviéndolos a un entorno de revisión (`_Para_Revisar`) 
antes de cualquier borrado, garantizando siempre la integridad mediante el 
módulo `safety.py`. Toda operación de E/S está protegida contra race conditions 
y violaciones de rutas de sistema.
"""

from __future__ import annotations
import os
import shutil
import string
import logging
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Final, Callable, Union, TypeAlias, NamedTuple, Dict, Sequence, Literal, Set

from safety import is_safe_to_modify, ensure_safe_to_modify, is_protected_path

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger(__name__)

# Tipos alias para claridad en firmas de funciones
SortKey: TypeAlias = Union[int, datetime]
SortField: TypeAlias = Literal["size", "date"]

@dataclass(frozen=True)
class SecurityLimits:
    """
    Umbrales de seguridad y constantes de bajo nivel para el escaneo.
    """
    MAX_PATH_LENGTH: Final[int] = 260
    MAX_FILE_SIZE_BYTES: Final[int] = 100_000_000_000
    MIN_FREE_SPACE_BYTES: Final[int] = 52_428_800
    MAX_SCAN_DEPTH: Final[int] = 50
    # Atributos de archivo Win32 usados en consultas de sistema
    WIN_ATTR_JUNCTION: Final[int] = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
    WIN_ATTR_SYSTEM: Final[int] = 0x04     # FILE_ATTRIBUTE_SYSTEM
    WIN_ATTR_HIDDEN: Final[int] = 0x02     # FILE_ATTRIBUTE_HIDDEN
    WIN_ATTR_MASK: Final[int] = 0x04 | 0x02
    WIN_ATTR_READONLY: Final[int] = 0x01   # FILE_ATTRIBUTE_READONLY

LIMITS: Final = SecurityLimits()

SYSTEM_CRITICAL_NAMES: Final[frozenset[str]] = frozenset({"pagefile.sys", "hiberfil.sys", "swapfile.sys"})

JUNK_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
})

@dataclass(frozen=True)
class ScanSettings:
    """Configuración inmutable para el proceso de escaneo de archivos."""
    max_depth: int = LIMITS.MAX_SCAN_DEPTH
    max_size: int = LIMITS.MAX_FILE_SIZE_BYTES
    extensions: frozenset[str] = JUNK_EXTENSIONS

class SortConfig(NamedTuple):
    """Define cómo extraer un valor de ordenamiento desde un objeto JunkFile."""
    field: str
    key_func: Callable[[JunkFile], SortKey]

SORT_REGISTRY: Final[Dict[str, SortConfig]] = {
    "size": SortConfig("size", lambda f: f.size_bytes),
    "date": SortConfig("date", lambda f: f.modified)
}

DEFAULT_SCAN_DIRS: Final[List[Path]] = [
    Path(os.environ.get("TEMP", "C:\\Temp")),
    Path(os.environ.get("LOCALAPPDATA", "C:\\")) / "Temp",
    Path.home() / "Downloads",
]

SYSTEM_FOLDER_BLOCKLIST: Final[frozenset[str]] = frozenset({
    "windows", "program files", "program files (x86)", "$recycle.bin", "system volume information"
})

def list_available_drives() -> List[str]:
    """Retorna una lista de letras de unidad disponibles en el sistema (Windows)."""
    if os.name != "nt":
        return []
    try:
        return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
    except OSError:
        return []

@dataclass
class JunkFile:
    """Representa un archivo candidato a limpieza con sus metadatos de identidad."""
    path: Path
    size_bytes: int
    modified: datetime
    _ino: Optional[int] = None
    _dev: Optional[int] = None

    def __post_init__(self) -> None:
        try:
            self.path = self.path.absolute()
            stat_result = self.path.stat()
            if self._ino is None: self._ino = stat_result.st_ino
            if self._dev is None: self._dev = stat_result.st_dev
        except (OSError, RuntimeError, PermissionError):
            pass

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño del archivo en MB redondeado."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión del archivo es considerada basura."""
        return self.path.suffix.lower() in JUNK_EXTENSIONS

def is_valid_junk_extension(filename: str) -> bool:
    """Valida si la extensión del nombre de archivo es válida para ser procesada."""
    if not filename: return False
    try:
        return Path(filename).suffix.lower() in JUNK_EXTENSIONS
    except (TypeError, ValueError):
        return False

def _get_win_attributes(entry: Union[os.DirEntry, Path]) -> int:
    """Obtiene los atributos de archivo de Win32 para chequeos de sistema/oculto."""
    try:
        if isinstance(entry, os.DirEntry):
            return entry.stat(follow_symlinks=False).st_file_attributes
        return entry.lstat().st_file_attributes
    except (OSError, AttributeError, ValueError):
        return 0

def _is_junction(entry: Union[os.DirEntry, Path]) -> bool:
    """Determina si una ruta es un punto de reparse (junction o symlink)."""
    try:
        if isinstance(entry, Path) and entry.is_symlink(): return True
        return bool(_get_win_attributes(entry) & LIMITS.WIN_ATTR_JUNCTION)
    except (OSError, AttributeError):
        return True

def _is_unc_path(path: Path) -> bool:
    """Determina si la ruta es de red (UNC), evitando procesar recursos remotos."""
    if not isinstance(path, Path): return True
    try:
        return str(path.absolute()).startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """
    Genera un destino único añadiendo un sufijo incremental si la ruta existe.
    
    Args:
        target: Objeto Path de destino deseado.
    Returns:
        Objeto Path con sufijo único (ej: archivo_1.tmp).
    """
    if not isinstance(target, Path): return target
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Verifica si el nombre de directorio está bloqueado por política de sistema."""
    if not name: return False
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _try_open_win_file(path: Path) -> bool:
    """
    Intenta obtener un handle de solo lectura en Windows. 
    Retorna True si el archivo está accesible, False si está bloqueado.
    """
    try:
        handle = ctypes.windll.kernel32.CreateFileW(
            str(path), 0x80000000, 0x00000001, None, 3, 0x00000080, None
        )
        if handle == wintypes.HANDLE(-1).value:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except (AttributeError, TypeError, OSError):
        return False

def _is_file_locked(path: Path) -> bool:
    """
    Verifica si un archivo está en uso mediante la apertura de un handle de acceso.
    """
    if not path: return True
    try:
        if not path.exists() or not path.is_file(): return True
        if not os.access(path, os.R_OK): return True
        if os.name == 'nt':
            return not _try_open_win_file(path)
        with open(path, "rb"): return False
    except (OSError, PermissionError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Verifica si el destino es un ancestro del origen para prevenir bucles de movimiento.
    """
    try:
        src_resolved = src.resolve(strict=False)
        dest_resolved = dest.resolve(strict=False)
        return src_resolved == dest_resolved or src_resolved in dest_resolved.parents
    except (OSError, ValueError, RuntimeError):
        return True

def _has_forbidden_chars(path: Path) -> bool:
    """Verifica la presencia de caracteres peligrosos en la ruta."""
    if not path: return True
    try:
        path_str = str(path).lower()
        return any(c in path_str for c in ["<", ">", "|", "\0"])
    except (ValueError, TypeError):
        return True

def _validate_path_security(src: Path, dest: Path) -> bool:
    """
    Valida la integridad de la ruta, verificando rutas UNC, caracteres inválidos, 
    longitud máxima y protección del sistema.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > LIMITS.MAX_PATH_LENGTH or len(str(dest)) > LIMITS.MAX_PATH_LENGTH: return False
    return not (is_protected_path(src) or is_protected_path(dest))

def _is_system_hidden(entry: Union[os.DirEntry, Path]) -> bool:
    """Verifica si el archivo o directorio está marcado como sistema u oculto."""
    return bool(_get_win_attributes(entry) & LIMITS.WIN_ATTR_MASK)

def _validate_file_identity(junk_file: JunkFile, src_real: Path) -> bool:
    """Valida que el archivo en disco coincida con los metadatos almacenados (protección TOCTOU)."""
    try:
        stat_result = src_real.stat()
        return (junk_file._ino is None or stat_result.st_ino == junk_file._ino) and \
               (junk_file._dev is None or stat_result.st_dev == junk_file._dev)
    except (OSError, AttributeError):
        return False

def _can_write_to_destination(dest_real: Path, size: int) -> bool:
    """
    Verifica espacio disponible en disco respetando el margen de seguridad MIN_FREE_SPACE_BYTES.
    
    Args:
        dest_real: Ruta del directorio destino.
        size: Tamaño del archivo a mover en bytes.
    Returns:
        True si hay suficiente espacio.
    """
    try:
        target_dir = dest_real.parent if dest_real.exists() else dest_real
        if not target_dir.is_dir() or not os.access(target_dir, os.W_OK):
            return False
        if os.name == 'nt':
            free_bytes = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(str(target_dir), None, None, ctypes.byref(free_bytes))
            return free_bytes.value >= (size + LIMITS.MIN_FREE_SPACE_BYTES)
        else:
            stat = os.statvfs(target_dir)
            return (stat.f_bavail * stat.f_frsize) >= (size + LIMITS.MIN_FREE_SPACE_BYTES)
    except (OSError, AttributeError, TypeError):
        return False

def _is_safe_for_disk_op(junk_file: JunkFile, dest: Path) -> bool:
    """
    Valida la seguridad del movimiento de archivos. Verifica permisos, bloqueos, 
    identidad (inodos) y restricciones de sistema.
    """
    if not isinstance(junk_file, JunkFile) or not isinstance(junk_file.path, Path): return False
    try:
        if not junk_file.path.exists() or not junk_file.path.is_file(): return False
        
        if os.name == 'nt':
            attrs = _get_win_attributes(junk_file.path)
            if (attrs & LIMITS.WIN_ATTR_READONLY) or (attrs & LIMITS.WIN_ATTR_MASK): return False
        
        src_real = junk_file.path.resolve(strict=True)
        dest_real = dest.resolve(strict=False)
        
        if is_protected_path(src_real) or is_protected_path(dest_real): return False
        if not is_safe_to_modify(dest_real): return False
        
        # Evitar mover archivos que ya están en la carpeta de destino o sus subcarpetas
        if dest_real in src_real.parents or src_real == dest_real: return False
        if src_real.parent == dest_real: return False
        if src_real.name.lower() in SYSTEM_CRITICAL_NAMES: return False
        if src_real.is_symlink() or _is_junction(junk_file.path): return False
        
        if not (is_safe_to_modify(src_real) and _validate_path_security(src_real, dest_real)): return False
        if src_real.drive != dest_real.drive: return False
        if not _validate_file_identity(junk_file, src_real) or _is_file_locked(src_real): return False
        if _is_recursive_violation(src_real, dest_real) or not _can_write_to_destination(dest_real, junk_file.size_bytes): return False
        
        return True
    except (OSError, AttributeError, ValueError, PermissionError):
        return False

def _should_scan_directory(entry: os.DirEntry, protected_cache: Set[str]) -> bool:
    """Filtra directorios no permitidos o protegidos antes de ingresar."""
    if entry.path in protected_cache or not _is_allowed_directory(entry.name) or _is_junction(entry): return False
    if _is_system_hidden(entry): return False
    try:
        p = Path(entry.path)
        if is_protected_path(p) or not is_safe_to_modify(p):
            protected_cache.add(entry.path)
            return False
    except (OSError, RuntimeError):
        return False
    return True

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int, protected_cache: Set[str], visited: Set[str]) -> None:
    """
    Ejecuta un recorrido recursivo del árbol de directorios con control de profundidad.
    """
    if depth > LIMITS.MAX_SCAN_DEPTH: return
    try:
        c_abs = current_dir.absolute()
        path_str = str(c_abs)
        if path_str in visited: return
        visited.add(path_str)
        
        with os.scandir(c_abs) as iterator:
            for entry in iterator:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry, protected_cache):
                            _process_directory(Path(entry.path), found, depth + 1, protected_cache, visited)
                    elif entry.is_file(follow_symlinks=False):
                        if is_valid_junk_extension(entry.name):
                            stats = entry.stat(follow_symlinks=False)
                            if stats.st_size < LIMITS.MAX_FILE_SIZE_BYTES and not (_get_win_attributes(entry) & LIMITS.WIN_ATTR_MASK):
                                p = Path(entry.path)
                                if is_safe_to_modify(p) and not _is_file_locked(p):
                                    found.append(JunkFile(p, stats.st_size, datetime.fromtimestamp(stats.st_mtime), stats.st_ino, stats.st_dev))
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError, RuntimeError):
        pass

def scan_for_junk(directories: Optional[Sequence[Union[str, Path]]] = None) -> List[JunkFile]:
    """Realiza un escaneo recursivo buscando archivos temporales candidatos."""
    found: List[JunkFile] = []
    protected_cache: Set[str] = set()
    visited: Set[str] = set()
    scan_list = directories if directories is not None else DEFAULT_SCAN_DIRS
    if not isinstance(scan_list, (list, tuple)): return []
    for d in filter(None, scan_list):
        try:
            p = Path(d).expanduser()
            if p.exists() and p.is_dir() and not _is_unc_path(p) and is_safe_to_modify(p):
                _process_directory(p, found, 0, protected_cache, visited)
        except (OSError, RuntimeError):
            continue
    return found

def sort_junk(files: Sequence[JunkFile], by: SortField = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena una lista de objetos JunkFile según el criterio especificado."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Mueve archivos candidatos a la carpeta de revisión segura.
    
    Args:
        files: Lista de JunkFile a mover.
        review_dir: Ruta destino (soporta expansión de usuario).
    """
    if not isinstance(files, (list, tuple)) or not isinstance(review_dir, str) or not review_dir: 
        return None
    try:
        dest_base = Path(review_dir).expanduser().resolve()
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        if not is_safe_to_modify(dest_base) or is_protected_path(dest_base) or _has_forbidden_chars(dest_base): 
            return None
        
        for junk_file in files:
            if not isinstance(junk_file, JunkFile) or not isinstance(junk_file.path, Path): 
                continue
            try:
                if not _is_safe_for_disk_op(junk_file, dest_base): 
                    continue
                target_path = _can_move_file(junk_file, dest_base)
                if target_path and junk_file.path.exists(): 
                    shutil.move(str(junk_file.path), str(target_path))
            except (OSError, shutil.Error, PermissionError, TypeError) as e:
                logger.error(f"Error moviendo archivo a revisión: {junk_file.path} - {e}")
        return dest_base
    except (OSError, RuntimeError, TypeError) as e:
        logger.error(f"Falla crítica en stage_for_review: {e}")
        return None

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Calcula el destino final para el archivo, preservando metadatos para trazabilidad."""
    try:
        if not isinstance(junk_file.modified, datetime): return None
        safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
        return _generate_unique_target(dest_base / safe_name)
    except (OSError, AttributeError, ValueError, OverflowError): 
        return None

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina permanentemente archivos desde la carpeta de revisión tras validar seguridad.
    """
    if not isinstance(review_dir, str) or not review_dir: return 0
    try:
        dest = Path(review_dir).expanduser().resolve()
        ensure_safe_to_modify(dest)
        if not dest.exists() or not dest.is_dir(): return 0
        if _has_forbidden_chars(dest): return 0
        
        count = 0
        for item in dest.iterdir():
            try:
                if not item.is_file() or item.is_symlink() or _is_junction(item): 
                    continue
                if not is_valid_junk_extension(item.name):
                    continue
                if is_safe_to_modify(item) and not is_protected_path(item):
                    item.unlink()
                    count += 1
            except (OSError, PermissionError, ValueError, AttributeError) as e:
                logger.warning(f"No se pudo eliminar {item}: {e}")
                continue
        return count
    except (OSError, PermissionError, RuntimeError, TypeError) as e:
        logger.error(f"Falla crítica en delete_reviewed: {e}")
        return 0
