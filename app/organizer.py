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

SortKey: TypeAlias = Union[int, datetime]
SortField: TypeAlias = Literal["size", "date"]

@dataclass(frozen=True)
class SecurityLimits:
    """Umbrales de seguridad y constantes de bajo nivel para el escaneo.
    Define límites de recursión, tamaño y máscaras de atributos de Windows para 
    evitar el procesamiento de archivos críticos o bucles infinitos."""
    MAX_PATH_LENGTH: Final[int] = 260
    MAX_FILE_SIZE_BYTES: Final[int] = 100_000_000_000
    MIN_FREE_SPACE_BYTES: Final[int] = 52_428_800
    MAX_SCAN_DEPTH: Final[int] = 50
    # Atributos de archivo específicos de la API de Windows
    WIN_ATTR_JUNCTION: Final[int] = 0x400
    WIN_ATTR_SYSTEM: Final[int] = 0x04
    WIN_ATTR_HIDDEN: Final[int] = 0x02
    WIN_ATTR_MASK: Final[int] = 0x04 | 0x02
    WIN_ATTR_READONLY: Final[int] = 0x01

LIMITS: Final = SecurityLimits()

# Archivos de sistema que nunca deben ser considerados para limpieza ni movimiento
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
    """
    Representa un archivo candidato a limpieza. Se utiliza inodo/dev (o equivalente) 
    para identificar de forma unívoca el archivo y prevenir race conditions donde 
    el archivo es reemplazado por otro en la misma ruta antes de procesarlo.
    """
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
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        return self.path.suffix.lower() in JUNK_EXTENSIONS

def is_valid_junk_extension(filename: str) -> bool:
    """Valida si la extensión del archivo está permitida para limpieza."""
    if not filename: return False
    try:
        return Path(filename).suffix.lower() in JUNK_EXTENSIONS
    except (TypeError, ValueError):
        return False

def _get_win_attributes(entry: Union[os.DirEntry, Path]) -> int:
    """
    Extrae atributos de archivo de la API de Win32 (GetFileAttributes). 
    Es necesario para distinguir correctamente archivos con bits de sistema/oculto
    que las llamadas estándar de POSIX pueden omitir.
    """
    try:
        if isinstance(entry, os.DirEntry):
            return entry.stat(follow_symlinks=False).st_file_attributes
        return entry.lstat().st_file_attributes
    except (OSError, AttributeError, ValueError):
        return 0

def _is_junction(entry: Union[os.DirEntry, Path]) -> bool:
    """
    Detecta puntos de reparse (Junctions o symlinks). Bloquear esto es crítico
    porque seguir enlaces de sistema puede llevar a bucles infinitos o escaneos
    fuera de los directorios permitidos por el usuario.
    """
    try:
        if isinstance(entry, Path) and entry.is_symlink(): return True
        return bool(_get_win_attributes(entry) & LIMITS.WIN_ATTR_JUNCTION)
    except (OSError, AttributeError):
        return True

def _is_unc_path(path: Path) -> bool:
    """
    Valida si la ruta es UNC (e.g., \\servidor\recurso). Se bloquea explícitamente 
    porque estas rutas pueden causar bloqueos de E/S impredecibles (timeouts) 
    y no son seguras para realizar operaciones de limpieza local.
    """
    if not isinstance(path, Path): return True
    try:
        return str(path.absolute()).startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """
    Genera una ruta de destino única para evitar colisiones de nombres 
    en el directorio de revisión, añadiendo un índice numérico de 1 a 999.
    """
    if not isinstance(target, Path): return target
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Valida que el directorio no pertenezca a la lista de bloqueados críticos."""
    if not name: return False
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _try_open_win_file(path: Path) -> bool:
    """
    Intenta abrir el archivo con privilegios exclusivos de solo lectura 
    (CreateFileW) para detectar si está bloqueado por otro proceso del sistema.
    Retorna True si el archivo está accesible, False si hay un lock vigente.
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
    """Determina si un archivo está en uso por otro proceso del sistema."""
    if not path or not path.exists() or not path.is_file(): return True
    try:
        if not os.access(path, os.R_OK): return True
        if os.name == 'nt':
            return not _try_open_win_file(path)
        with open(path, "rb"): return False
    except (OSError, PermissionError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Verifica que la operación de movimiento no sea circular. Esto evita la 
    corrupción del sistema de archivos al intentar mover una carpeta padre 
    dentro de su propia estructura (subdirectorio).
    """
    try:
        src_resolved = src.resolve(strict=False)
        dest_resolved = dest.resolve(strict=False)
        return src_resolved == dest_resolved or src_resolved in dest_resolved.parents
    except (OSError, ValueError, RuntimeError):
        return True

def _has_forbidden_chars(path: Path) -> bool:
    """Detecta caracteres que pueden romper la interpretación de rutas en Win32."""
    if not path: return True
    try:
        path_str = str(path).lower()
        return any(c in path_str for c in ["<", ">", "|", "\0"])
    except (ValueError, TypeError):
        return True

def _validate_path_security(src: Path, dest: Path) -> bool:
    """
    Orquestador de seguridad: Valida longitud, caracteres, rutas UNC y 
    restricciones de `safety.py` sobre los paths implicados en la E/S.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > LIMITS.MAX_PATH_LENGTH or len(str(dest)) > LIMITS.MAX_PATH_LENGTH: return False
    return not (is_protected_path(src) or is_protected_path(dest))

def _is_system_hidden(entry: Union[os.DirEntry, Path]) -> bool:
    """Detecta atributos de sistema/oculto que indican archivos sensibles."""
    return bool(_get_win_attributes(entry) & LIMITS.WIN_ATTR_MASK)

def _validate_file_identity(junk_file: JunkFile, src_real: Path) -> bool:
    """
    Valida la identidad física comparando inodo/dev. Es la defensa final 
    contra TOCTOU (Time-of-check to time-of-use), asegurando que el archivo 
    que vamos a mover es el mismo que escaneamos inicialmente.
    """
    try:
        stat_result = src_real.stat()
        return (junk_file._ino is None or stat_result.st_ino == junk_file._ino) and \
               (junk_file._dev is None or stat_result.st_dev == junk_file._dev)
    except (OSError, AttributeError):
        return False

def _can_write_to_destination(dest_real: Path, size: int) -> bool:
    """
    Verifica disponibilidad de espacio en disco en la unidad de destino. 
    Se calcula con margen extra (`MIN_FREE_SPACE_BYTES`) para evitar dejar 
    la partición con espacio insuficiente tras el movimiento.
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
    Validador integral previo a cualquier operación de E/S.
    Verifica seguridad de permisos, exclusividad de acceso (sin locks),
    integridad de identidad y disponibilidad de espacio en disco.
    """
    if not isinstance(junk_file, JunkFile) or not isinstance(junk_file.path, Path): return False
    try:
        if not junk_file.path.exists() or not junk_file.path.is_file(): return False
        
        # Validaciones de atributos de bajo nivel y protección estricta
        if os.name == 'nt':
            attrs = _get_win_attributes(junk_file.path)
            if (attrs & LIMITS.WIN_ATTR_READONLY) or (attrs & LIMITS.WIN_ATTR_MASK): return False
        
        src_real = junk_file.path.resolve(strict=True)
        dest_real = dest.resolve(strict=False)
        
        ensure_safe_to_modify(dest_real)
        if src_real == dest_real or src_real.parent == dest_real: return False
        if src_real.name.lower() in SYSTEM_CRITICAL_NAMES or is_protected_path(src_real): return False
        if is_protected_path(dest_real): return False
        if src_real.is_symlink() or _is_junction(src_real): return False
        if not (is_safe_to_modify(src_real) and _validate_path_security(src_real, dest_real)): return False
        if src_real.drive != dest_real.drive: return False
        if not _validate_file_identity(junk_file, src_real) or _is_file_locked(src_real): return False
        if _is_recursive_violation(src_real, dest_real) or not _can_write_to_destination(dest_real, junk_file.size_bytes): return False
        
        return not is_protected_path(src_real)
    except (OSError, AttributeError, ValueError, PermissionError):
        return False

def _should_scan_directory(entry: os.DirEntry, protected_cache: Set[str]) -> bool:
    """Filtra directorios no permitidos o protegidos antes de entrar en ellos."""
    if entry.path in protected_cache or not _is_allowed_directory(entry.name) or _is_junction(entry): return False
    if _is_system_hidden(entry): return False
    try:
        path = Path(entry.path)
        if is_protected_path(path) or not is_safe_to_modify(path):
            protected_cache.add(entry.path)
            return False
    except (OSError, RuntimeError):
        return False
    return True

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int, protected_cache: Set[str], visited: Set[str]) -> None:
    """
    Procesa recursivamente el sistema de archivos buscando candidatos a limpieza. 
    Usa un caché de rutas visitadas para evitar ciclos lógicos y optimizar el rendimiento.
    """
    if depth > LIMITS.MAX_SCAN_DEPTH: return
    try:
        path_str = str(current_dir.absolute())
        if path_str in visited: return
        visited.add(path_str)
        
        with os.scandir(current_dir) as iterator:
            for entry in iterator:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry, protected_cache):
                            _process_directory(Path(entry.path), found, depth + 1, protected_cache, visited)
                    elif entry.is_file(follow_symlinks=False):
                        if is_valid_junk_extension(entry.name):
                            stats = entry.stat(follow_symlinks=False)
                            p = Path(entry.path)
                            if stats.st_size < LIMITS.MAX_FILE_SIZE_BYTES and not (_get_win_attributes(entry) & LIMITS.WIN_ATTR_MASK):
                                if not _is_file_locked(p):
                                    found.append(JunkFile(p, stats.st_size, datetime.fromtimestamp(stats.st_mtime), stats.st_ino, stats.st_dev))
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError, RuntimeError):
        pass

def scan_for_junk(directories: Optional[Sequence[Union[str, Path]]] = None) -> List[JunkFile]:
    """Inicia el escaneo recursivo de archivos en las rutas indicadas."""
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
    """Ordena los archivos encontrados utilizando el registro de configuraciones."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Ejecuta el movimiento de archivos hacia la zona de cuarentena tras 
    verificar la seguridad de cada operación de E/S de forma individual.
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
    """
    Calcula el nombre del destino evitando colisiones y manteniendo 
    la trazabilidad mediante el uso de la fecha de modificación original.
    """
    try:
        if not isinstance(junk_file.modified, datetime): return None
        safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
        return _generate_unique_target(dest_base / safe_name)
    except (OSError, AttributeError, ValueError, OverflowError): 
        return None

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina físicamente los archivos validados tras aplicar chequeos 
    adicionales de seguridad sobre la carpeta de revisión.
    """
    if not isinstance(review_dir, str) or not review_dir: return 0
    try:
        dest = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not dest.is_dir() or not is_safe_to_modify(dest) or is_protected_path(dest): 
            return 0
        count = 0
        for item in dest.iterdir():
            try:
                if not item.is_file() or item.is_symlink() or _is_junction(item): 
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
