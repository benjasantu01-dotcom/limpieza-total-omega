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

@dataclass(frozen=True)
class Win32Attributes:
    """Mapeo de constantes de atributos de archivo Win32 (kernel32)."""
    JUNCTION: Final[int] = 0x400
    SYSTEM: Final[int] = 0x04
    HIDDEN: Final[int] = 0x02
    READONLY: Final[int] = 0x01
    MASK_PROTECTED: Final[int] = 0x04 | 0x02

@dataclass(frozen=True)
class SecurityLimits:
    """Umbrales de seguridad y constantes de bajo nivel para el escaneo."""
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
    """
    Identifica las unidades montadas en el sistema Windows.
    Retorna una lista de rutas raíz (ej. 'C:\\') verificando su existencia.
    """
    if os.name != "nt":
        return []
    try:
        return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
    except OSError:
        return []

@dataclass
class JunkFile:
    """
    Representa un archivo candidato a limpieza.
    
    Almacena metadatos críticos para detección de ataques TOCTOU (Time-of-check to time-of-use),
    utilizando identificadores únicos de inodo y dispositivo del sistema de archivos.
    """
    path: Path
    size_bytes: int
    modified: datetime
    _ino: Optional[int] = None
    _dev: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            self.path = Path(self.path)
        try:
            abs_path = self.path.absolute()
            if abs_path.exists():
                stat_result = abs_path.stat()
                self.path = abs_path
                if self._ino is None: self._ino = stat_result.st_ino
                if self._dev is None: self._dev = stat_result.st_dev
        except (OSError, PermissionError):
            pass

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño del archivo convertido a Megabytes."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Valida si el archivo posee una extensión reconocida como basura."""
        return self.path.suffix.lower() in JUNK_EXTENSIONS

def is_valid_junk_extension(filename: str) -> bool:
    """Valida si el nombre de archivo proporcionado corresponde a una extensión basura."""
    if not filename: return False
    return Path(filename).suffix.lower() in JUNK_EXTENSIONS

def _get_win_attributes(entry: Union[os.DirEntry, Path]) -> int:
    """
    Acceso de bajo nivel a los atributos del archivo en Windows.
    Utiliza lstat para evitar la resolución de symlinks durante la lectura.
    """
    try:
        if isinstance(entry, os.DirEntry):
            return entry.stat(follow_symlinks=False).st_file_attributes
        return entry.lstat().st_file_attributes
    except (OSError, AttributeError, ValueError):
        return 0

def _is_file_read_only(path: Path) -> bool:
    """Determina si un archivo tiene el atributo de solo lectura (Windows)."""
    return bool(_get_win_attributes(path) & LIMITS.ATTRS.READONLY)

def _is_junction(entry: Union[os.DirEntry, Path]) -> bool:
    """
    Detecta si la ruta es un punto de reparse (junction o symlink).
    Vital para prevenir bucles infinitos en escaneos recursivos.
    """
    try:
        if isinstance(entry, Path) and entry.is_symlink(): return True
        return bool(_get_win_attributes(entry) & LIMITS.ATTRS.JUNCTION)
    except (OSError, AttributeError):
        return True

def _is_unc_path(path: Path) -> bool:
    """Identifica rutas de red (UNC) que requieren manejo especial en Windows."""
    if not isinstance(path, Path): return True
    try:
        return str(path).startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """
    Resuelve colisiones de nombres añadiendo un sufijo numérico (1-999).
    Asegura que el archivo movido a cuarentena no sobrescriba archivos existentes.
    """
    if not isinstance(target, Path): return target
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Valida si el nombre de carpeta no está en la blocklist de seguridad."""
    if not name: return False
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _try_open_win_file(path: Path) -> bool:
    """
    Intenta abrir un handle de archivo en modo compartido.
    Retorna True si el archivo está libre; False si está bloqueado por otro proceso.
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
    """Verifica si el archivo está siendo usado por otro proceso (bloqueo exclusivo)."""
    if not isinstance(path, Path) or not path.exists(): return True
    try:
        if os.name == 'nt':
            return not _try_open_win_file(path)
        with open(path, "rb"): return False
    except (OSError, PermissionError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """Detecta si el destino está contenido dentro de la fuente para evitar recursividad."""
    try:
        src_resolved = src.resolve(strict=False)
        dest_resolved = dest.resolve(strict=False)
        return src_resolved == dest_resolved or src_resolved in dest_resolved.parents
    except (OSError, ValueError, RuntimeError):
        return True

def _has_forbidden_chars(path: Path) -> bool:
    """Valida que la ruta no contenga caracteres inválidos para el sistema de archivos."""
    if not isinstance(path, Path): return True
    try:
        path_str = str(path).lower()
        return any(c in path_str for c in ["<", ">", "|", "\0"])
    except (ValueError, TypeError):
        return True

def _validate_path_security(src: Path, dest: Path) -> bool:
    """Valida las precondiciones de seguridad necesarias antes de cualquier movimiento."""
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > LIMITS.MAX_PATH_LENGTH or len(str(dest)) > LIMITS.MAX_PATH_LENGTH: return False
    return is_safe_to_modify(dest) and not (is_protected_path(src) or is_protected_path(dest))

def _is_system_hidden(entry: Union[os.DirEntry, Path]) -> bool:
    """Determina si un archivo tiene atributos de sistema o está oculto."""
    return bool(_get_win_attributes(entry) & LIMITS.ATTRS.MASK_PROTECTED)

def _validate_file_identity(junk_file: JunkFile, src_real: Path) -> bool:
    """
    Mitigación TOCTOU: Compara los identificadores de inodo y dispositivo actuales
    con los capturados originalmente al momento del descubrimiento del archivo.
    """
    try:
        if not src_real.exists(): return False
        stat_result = src_real.stat()
        return (junk_file._ino is None or stat_result.st_ino == junk_file._ino) and \
               (junk_file._dev is None or stat_result.st_dev == junk_file._dev)
    except (OSError, AttributeError):
        return False

def _can_write_to_destination(dest_real: Path, size: int) -> bool:
    """Verifica si la unidad de destino tiene suficiente espacio libre para el archivo."""
    try:
        target_dir = dest_real.parent if dest_real.exists() else dest_real
        if not target_dir.is_dir() or not os.access(target_dir, os.W_OK):
            return False
        if os.name == 'nt':
            free_bytes = ctypes.c_ulonglong(0)
            if ctypes.windll.kernel32.GetDiskFreeSpaceExW(str(target_dir), None, None, ctypes.byref(free_bytes)):
                return free_bytes.value >= (size + LIMITS.MIN_FREE_SPACE_BYTES)
            return False
        else:
            stat = os.statvfs(target_dir)
            return (stat.f_bavail * stat.f_frsize) >= (size + LIMITS.MIN_FREE_SPACE_BYTES)
    except (OSError, AttributeError, TypeError, OverflowError):
        return False

def _is_safe_for_disk_op(junk_file: JunkFile, dest: Path) -> bool:
    """
    Validador maestro que consolida todas las verificaciones de seguridad 
    requeridas antes de realizar una operación de movimiento de archivos.
    """
    if not isinstance(junk_file, JunkFile) or not isinstance(junk_file.path, Path) or not junk_file.path.exists(): 
        return False
    try:
        src_real = junk_file.path.resolve(strict=True)
        dest_real = dest.resolve(strict=False)
        
        if not dest_real.exists() or not dest_real.is_dir(): return False
        if not os.access(dest_real, os.W_OK): return False
        
        if dest_real in src_real.parents or src_real == dest_real or src_real.parent == dest_real: return False
        
        if src_real.name.lower() in SYSTEM_CRITICAL_NAMES: return False
        if src_real.is_symlink() or _is_junction(junk_file.path): return False
        
        if not _validate_path_security(src_real, dest_real): return False
        if junk_file._dev is not None and dest_real.stat().st_dev != junk_file._dev: return False
        if not _validate_file_identity(junk_file, src_real) or _is_file_locked(src_real): return False
        if _is_recursive_violation(src_real, dest_real) or not _can_write_to_destination(dest_real, junk_file.size_bytes): return False
        
        return True
    except (OSError, AttributeError, ValueError, PermissionError):
        return False

def _should_scan_directory(entry: os.DirEntry, protected_cache: Set[str]) -> bool:
    """Determina si un directorio es candidato seguro para ser escaneado."""
    if entry.path in protected_cache or not _is_allowed_directory(entry.name) or _is_junction(Path(entry.path)): return False
    if _is_system_hidden(entry): return False
    if is_protected_path(Path(entry.path)):
        protected_cache.add(entry.path)
        return False
    return True

def _is_valid_junk_entry(entry: os.DirEntry) -> bool:
    """Filtra archivos de entrada según extensiones, tamaño y atributos de sistema."""
    try:
        if Path(entry.name).suffix.lower() not in JUNK_EXTENSIONS: return False
        stats = entry.stat(follow_symlinks=False)
        return stats.st_size < LIMITS.MAX_FILE_SIZE_BYTES and not _is_system_hidden(entry)
    except (PermissionError, OSError):
        return False

def _process_directory(current_dir: Path, junk_candidates: List[JunkFile], depth: int, protected_cache: Set[str], visited: Set[str]) -> None:
    """Escaneo recursivo de directorios limitado por profundidad y detección de ciclos."""
    if depth > LIMITS.MAX_SCAN_DEPTH: return
        
    try:
        real_path = current_dir.resolve(strict=False)
        if not real_path.exists() or not real_path.is_dir() or str(real_path) in visited: return
        
        visited.add(str(real_path))
        
        with os.scandir(real_path) as iterator:
            for entry in iterator:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry, protected_cache):
                            _process_directory(Path(entry.path), junk_candidates, depth + 1, protected_cache, visited)
                    elif entry.is_file(follow_symlinks=False) and _is_valid_junk_entry(entry):
                        stats = entry.stat(follow_symlinks=False)
                        junk_candidates.append(JunkFile(
                            Path(entry.path), 
                            stats.st_size, 
                            datetime.fromtimestamp(stats.st_mtime), 
                            stats.st_ino, 
                            stats.st_dev
                        ))
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError, RuntimeError):
        pass

def scan_for_junk(directories: Optional[Iterable[PathLike]] = None) -> List[JunkFile]:
    """Inicia el proceso de escaneo en las rutas proporcionadas."""
    junk_candidates: List[JunkFile] = []
    protected_cache: Set[str] = set()
    visited: Set[str] = set()
    scan_list = directories if directories is not None else DEFAULT_SCAN_DIRS
    if not isinstance(scan_list, (list, tuple)): return []
    for d in filter(None, scan_list):
        try:
            p = Path(d).expanduser()
            if p.exists() and p.is_dir() and not _is_unc_path(p) and not is_protected_path(p):
                _process_directory(p, junk_candidates, 0, protected_cache, visited)
        except (OSError, RuntimeError):
            continue
    return junk_candidates

def sort_junk(files: Sequence[JunkFile], by: SortField = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena el listado de archivos basura basándose en los criterios del registro."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Mueve los archivos basura confirmados al directorio de revisión.
    Realiza una validación de seguridad atómica antes de cada movimiento.
    """
    if not isinstance(files, (list, tuple)) or not isinstance(review_dir, str) or not review_dir: 
        return None
    try:
        dest_base = Path(review_dir).expanduser().resolve()
        if not dest_base.exists():
            dest_base.mkdir(parents=True, exist_ok=True)
        
        if not is_safe_to_modify(dest_base):
            return None
        
        for junk_file in files:
            if not isinstance(junk_file, JunkFile) or not isinstance(junk_file.path, Path): continue
            
            # Re-validación atómica pre-operación
            if not junk_file.path.exists() or junk_file.path.is_symlink() or _is_junction(junk_file.path):
                continue
                
            if not _is_safe_for_disk_op(junk_file, dest_base): continue
            
            target_path = _can_move_file(junk_file, dest_base)
            if target_path and not target_path.exists(): 
                os.replace(str(junk_file.path), str(target_path))
        return dest_base
    except (OSError, PermissionError) as e:
        logger.error(f"Falla en stage_for_review: {e}")
        return None

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Calcula la ruta de destino única para el archivo, basándose en su timestamp."""
    if not isinstance(junk_file.modified, datetime): return None
    try:
        timestamp = int(junk_file.modified.timestamp())
        safe_name = f"{junk_file.path.stem}_{timestamp}{junk_file.path.suffix}"
        target = dest_base / safe_name
        
        if _has_forbidden_chars(target) or len(str(target)) > LIMITS.MAX_PATH_LENGTH:
            return None
            
        return _generate_unique_target(target)
    except (OSError, AttributeError, ValueError, OverflowError): 
        return None

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina permanentemente archivos en el directorio de revisión.
    Aplica filtros de seguridad estrictos antes de invocar la operación de borrado.
    """
    if not isinstance(review_dir, str) or not review_dir: return 0
    try:
        dest = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not dest.is_dir() or not is_safe_to_modify(dest):
            return 0
        
        count = 0
        for item in dest.iterdir():
            try:
                # Validaciones estrictas antes de unlink()
                if item.is_file() and not item.is_symlink() and not _is_junction(item):
                    if is_valid_junk_extension(item.name) and is_safe_to_modify(item) and not _is_file_locked(item):
                        item.unlink()
                        count += 1
            except (OSError, PermissionError):
                continue
        return count
    except (OSError, PermissionError, RuntimeError):
        return 0
