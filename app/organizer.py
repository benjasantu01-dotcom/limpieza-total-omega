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
import string
import logging
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Final, Callable, Union, TypeAlias, NamedTuple, Dict, Sequence, Literal, Set, Iterable

from safety import is_safe_to_modify, ensure_safe_to_modify, is_protected_path

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger(__name__)

# Tipos alias para claridad en firmas de funciones
SortKey: TypeAlias = Union[int, datetime]
SortField: TypeAlias = Literal["size", "date"]
PathLike: TypeAlias = Union[str, Path]

# Constantes de control de E/S
BYTES_IN_MB: Final[int] = 1024 * 1024
WIN32_READ_ONLY_FLAG: Final[int] = 0x01
WIN32_REPARSE_POINT_FLAG: Final[int] = 0x400

@dataclass(frozen=True)
class Win32Attributes:
    """Mapeo de constantes de atributos de archivo Win32 obtenidas de kernel32.h."""
    JUNCTION: Final[int] = 0x400      
    SYSTEM: Final[int] = 0x04         
    HIDDEN: Final[int] = 0x02         
    READONLY: Final[int] = 0x01       
    MASK_PROTECTED: Final[int] = 0x04 | 0x02  
    REPARSE_POINT: Final[int] = 0x400  

@dataclass(frozen=True)
class SecurityLimits:
    """Umbrales de seguridad para operaciones de disco, evitando desbordamientos o accesos indebidos."""
    MAX_PATH_LENGTH: Final[int] = 260               
    MAX_FILE_SIZE_BYTES: Final[int] = 100_000_000_000 
    MIN_FREE_SPACE_BYTES: Final[int] = 52_428_800    
    MAX_SCAN_DEPTH: Final[int] = 50                  
    ATTRS: Final[Win32Attributes] = Win32Attributes()

LIMITS: Final[SecurityLimits] = SecurityLimits()

SYSTEM_CRITICAL_NAMES: Final[frozenset[str]] = frozenset({"pagefile.sys", "hiberfil.sys", "swapfile.sys"})

JUNK_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
})

JUNK_EXTENSIONS_TUPLE: Final[tuple[str, ...]] = tuple(JUNK_EXTENSIONS)

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
    """Identifica letras de unidad montadas verificando la existencia del directorio raíz."""
    if os.name != "nt":
        return []
    try:
        return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
    except OSError:
        return []

@dataclass
class JunkFile:
    path: Path
    size_bytes: int
    modified: datetime
    _ino: Optional[int] = None
    _dev: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            self.path = Path(self.path)
        self.path = self.path.absolute()

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / BYTES_IN_MB, 2)

    @property
    def is_junk_extension(self) -> bool:
        return is_valid_junk_extension(self.path.name)

def is_valid_junk_extension(filename: str) -> bool:
    if not isinstance(filename, str) or not filename: 
        return False
    return filename.lower().endswith(JUNK_EXTENSIONS_TUPLE)

def _get_win_attributes(entry: Union[os.DirEntry, Path]) -> int:
    try:
        if isinstance(entry, os.DirEntry):
            return entry.stat(follow_symlinks=False).st_file_attributes
        return entry.lstat().st_file_attributes
    except (OSError, AttributeError, ValueError, PermissionError):
        return 0

def _is_junction(entry: Union[os.DirEntry, Path]) -> bool:
    try:
        if isinstance(entry, Path) and entry.is_symlink(): return True
        return bool(_get_win_attributes(entry) & WIN32_REPARSE_POINT_FLAG)
    except (OSError, AttributeError):
        return True

def _is_unc_path(path: Path) -> bool:
    path_str = str(path)
    return path_str.startswith(("\\\\", "//"))

def _generate_unique_target(target: Path) -> Path:
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _try_open_win_file(path: Path) -> bool:
    path_str = str(path)
    if len(path_str) >= LIMITS.MAX_PATH_LENGTH: return False
    try:
        handle = ctypes.windll.kernel32.CreateFileW(
            path_str, 0x80000000, 0x00000003, None, 3, 0x00000080, None
        )
        if handle == wintypes.HANDLE(-1).value: return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except (AttributeError, TypeError, OSError, ValueError, ctypes.WinError):
        return False

def _is_file_locked(path: Path) -> bool:
    try:
        if not path.exists(): return True
        if os.name == 'nt': return not _try_open_win_file(path)
        with open(path, "rb"): return False
    except (OSError, PermissionError, FileNotFoundError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    try:
        src_res = src.resolve(strict=False)
        dest_res = dest.resolve(strict=False)
        return src_res == dest_res or dest_res in src_res.parents
    except (OSError, ValueError, RuntimeError):
        return True

def _has_forbidden_chars(path: Path) -> bool:
    path_str = str(path).lower()
    return any(c in path_str for c in ["<", ">", "|", "\0"])

def _is_system_hidden(entry: Union[os.DirEntry, Path]) -> bool:
    return bool(_get_win_attributes(entry) & LIMITS.ATTRS.MASK_PROTECTED)

def _validate_file_identity(junk_file: JunkFile, source_path: Path) -> bool:
    try:
        stat = source_path.lstat()
        return (junk_file._ino == stat.st_ino) and (junk_file._dev == stat.st_dev)
    except (OSError, AttributeError):
        return False

def _can_write_to_destination(dest_real: Path, size: int) -> bool:
    try:
        target_dir = dest_real if dest_real.is_dir() else dest_real.parent
        if not target_dir.exists() or not os.access(target_dir, os.W_OK): return False
        if os.name == 'nt':
            free = ctypes.c_ulonglong(0)
            if ctypes.windll.kernel32.GetDiskFreeSpaceExW(str(target_dir), None, None, ctypes.byref(free)):
                return free.value >= (size + LIMITS.MIN_FREE_SPACE_BYTES)
            return False
        stat = os.statvfs(target_dir)
        return (stat.f_bavail * stat.f_frsize) >= (size + LIMITS.MIN_FREE_SPACE_BYTES)
    except (OSError, AttributeError, TypeError, OverflowError): return False

def _is_path_in_bounds(path: Path, root: Optional[Path]) -> bool:
    if not root: return True
    try:
        return str(path.resolve(strict=False)).startswith(str(root.resolve(strict=False)))
    except (OSError, RuntimeError): return False

def _is_safe_for_disk_op(junk_file: JunkFile, dest: Path) -> bool:
    """Valida integridad, permisos y disponibilidad antes de E/S."""
    try:
        src, dst = junk_file.path.resolve(strict=False), dest.resolve(strict=False)
        if not (src.exists() and dst.exists() and dst.is_dir()): return False
        
        # Validaciones de seguridad de ruta
        if _is_junction(dst) or len(str(src)) >= LIMITS.MAX_PATH_LENGTH: return False
        if is_protected_path(src) or is_protected_path(dst) or not is_safe_to_modify(src): return False
        
        # Validaciones estructurales y de estado
        if _is_recursive_violation(src, dst) or src.name.lower() in SYSTEM_CRITICAL_NAMES: return False
        if not (os.access(dst, os.W_OK) and _validate_file_identity(junk_file, src)): return False
        
        return not _is_file_locked(src) and _can_write_to_destination(dst, junk_file.size_bytes)
    except (OSError, AttributeError, ValueError, PermissionError):
        return False

def _should_scan_directory(entry: os.DirEntry) -> bool:
    try:
        return _is_allowed_directory(entry.name) and not _is_junction(entry) and not _is_system_hidden(entry)
    except (OSError, PermissionError): return False

def _is_junk_candidate(entry: os.DirEntry, stats: os.stat_result) -> bool:
    return is_valid_junk_extension(entry.name) and not _is_system_hidden(entry) and stats.st_size < LIMITS.MAX_FILE_SIZE_BYTES

def _process_directory(current_path: str, junk_candidates: List[JunkFile], depth: int, visited: Set[tuple[int, int]]) -> None:
    if depth > LIMITS.MAX_SCAN_DEPTH: return
    try:
        st = os.stat(current_path)
        if (st.st_ino, st.st_dev) in visited: return
        visited.add((st.st_ino, st.st_dev))
    except (OSError, PermissionError): return

    try:
        with os.scandir(current_path) as iterator:
            for entry in iterator:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry):
                            _process_directory(entry.path, junk_candidates, depth + 1, visited)
                    else:
                        stats = entry.stat(follow_symlinks=False)
                        if _is_junk_candidate(entry, stats):
                            junk_candidates.append(JunkFile(
                                Path(entry.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime), 
                                stats.st_ino, stats.st_dev
                            ))
                except (PermissionError, OSError): continue
    except (PermissionError, OSError, RuntimeError): pass

def scan_for_junk(directories: Optional[Iterable[PathLike]] = None) -> List[JunkFile]:
    junk_candidates: List[JunkFile] = []
    visited: Set[tuple[int, int]] = set()
    scan_list = directories if directories is not None else DEFAULT_SCAN_DIRS
    for d in scan_list:
        if not d: continue
        try:
            p = Path(d).expanduser().resolve(strict=False)
            if p.exists() and p.is_dir() and not _is_unc_path(p) and is_safe_to_modify(p) and not is_protected_path(p):
                _process_directory(str(p), junk_candidates, 0, visited)
        except (OSError, RuntimeError): continue
    return junk_candidates

def sort_junk(files: Sequence[JunkFile], by: SortField = "size", ascending: bool = True) -> List[JunkFile]:
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    try:
        dest_base = Path(review_dir).expanduser().resolve(strict=False)
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        if not is_safe_to_modify(dest_base) or is_protected_path(dest_base) or _is_junction(dest_base): return None
        
        for junk_file in files:
            if not _is_safe_for_disk_op(junk_file, dest_base): continue
            target = _can_move_file(junk_file, dest_base)
            if target and not target.exists(): 
                ensure_safe_to_modify(junk_file.path)
                os.replace(str(junk_file.path), str(target))
        return dest_base
    except (OSError, RuntimeError, TypeError, ValueError): return None

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    try:
        timestamp = int(junk_file.modified.timestamp())
        safe_name = f"{junk_file.path.stem[:128]}_{timestamp}{junk_file.path.suffix}"
        target = dest_base / safe_name
        return None if _has_forbidden_chars(target) else _generate_unique_target(target)
    except (OSError, AttributeError, ValueError, OverflowError): return None

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    try:
        dest = Path(review_dir).expanduser().resolve(strict=False)
        if not dest.exists() or not dest.is_dir() or not is_safe_to_modify(dest): return 0
        count = 0
        for item in dest.iterdir():
            if not item.is_file() or is_protected_path(item) or not is_valid_junk_extension(item.name): continue
            try:
                ensure_safe_to_modify(item)
                item.unlink()
                count += 1
            except (OSError, PermissionError): continue
        return count
    except (OSError, RuntimeError): return 0
