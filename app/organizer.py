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
    """Identifica las unidades montadas en el sistema Windows mediante las letras de unidad."""
    if os.name != "nt":
        return []
    try:
        return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
    except OSError:
        return []

@dataclass
class JunkFile:
    """Representa un archivo detectado que califica como candidato a limpieza."""
    path: Path
    size_bytes: int
    modified: datetime
    _ino: Optional[int] = None
    _dev: Optional[int] = None

    def __post_init__(self) -> None:
        """Normaliza la ruta a absoluta y almacena metadatos de identidad (inodo/dev)."""
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
        """Calcula el tamaño del archivo convertido a Megabytes."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión del archivo coincide con las definidas en JUNK_EXTENSIONS."""
        return self.path.suffix.lower() in JUNK_EXTENSIONS

def is_valid_junk_extension(filename: str) -> bool:
    """Valida si un nombre de archivo dado posee una extensión considerada basura."""
    return isinstance(filename, str) and Path(filename).suffix.lower() in JUNK_EXTENSIONS

def _get_win_attributes(entry: Union[os.DirEntry, Path]) -> int:
    """Obtiene los atributos de archivo (bits Win32) mediante syscalls del SO."""
    try:
        if isinstance(entry, os.DirEntry):
            return entry.stat(follow_symlinks=False).st_file_attributes
        return entry.lstat().st_file_attributes
    except (OSError, AttributeError, ValueError, PermissionError):
        return 0

def _is_file_read_only(path: Path) -> bool:
    """Comprueba si el flag de solo lectura está activo en el archivo especificado."""
    return bool(_get_win_attributes(path) & LIMITS.ATTRS.READONLY)

def _is_junction(entry: Union[os.DirEntry, Path]) -> bool:
    """Detecta si un elemento es un punto de reparse (Junction) o enlace simbólico."""
    try:
        if isinstance(entry, Path) and entry.is_symlink(): return True
        return bool(_get_win_attributes(entry) & LIMITS.ATTRS.JUNCTION)
    except (OSError, AttributeError):
        return True

def _is_unc_path(path: Path) -> bool:
    """Determina si la ruta corresponde a un recurso de red (UNC) no local."""
    if not isinstance(path, Path): return True
    try:
        return str(path).startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """Crea un nombre único en el destino mediante sufijos incrementales si ya existe."""
    if not isinstance(target, Path): return target
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Valida si un nombre de carpeta no forma parte de la lista de rutas críticas."""
    if not isinstance(name, str) or not name: return False
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _try_open_win_file(path: Path) -> bool:
    """Intenta abrir un handle de lectura compartida para verificar si el archivo está bloqueado."""
    if not isinstance(path, Path) or len(str(path)) >= LIMITS.MAX_PATH_LENGTH: return False
    try:
        if not path.exists(): return False
        handle = ctypes.windll.kernel32.CreateFileW(
            str(path), 0x80000000, 0x00000001, None, 3, 0x00000080, None
        )
        if handle == wintypes.HANDLE(-1).value:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except (AttributeError, TypeError, OSError, ValueError):
        return False

def _is_file_locked(path: Path) -> bool:
    """Verifica si un archivo está en uso exclusivo por otro proceso."""
    if not isinstance(path, Path) or not path.exists(): return True
    try:
        if os.name == 'nt':
            return not _try_open_win_file(path)
        with open(path, "rb"): return False
    except (OSError, PermissionError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Detecta si la ruta destino es padre o igual a la fuente para evitar 
    que el proceso de limpieza cree bucles infinitos o destruya su propio contexto.
    """
    try:
        src_resolved = src.resolve(strict=False)
        dest_resolved = dest.resolve(strict=False)
        return src_resolved == dest_resolved or dest_resolved in src_resolved.parents
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

def _validate_path_security(src: Path, dest: Path, root: Optional[Path] = None) -> bool:
    """Valida mediante `safety.py` y reglas de formato que la ruta sea segura para operar."""
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > LIMITS.MAX_PATH_LENGTH or len(str(dest)) > LIMITS.MAX_PATH_LENGTH: return False
    
    # Prevenir path traversal: el destino debe estar bajo el directorio root de cuarentena
    if root and not str(dest.resolve(strict=False)).startswith(str(root.resolve(strict=False))):
        return False
        
    return is_safe_to_modify(dest) and not (is_protected_path(src) or is_protected_path(dest))

def _is_system_hidden(entry: Union[os.DirEntry, Path]) -> bool:
    """Determina si un objeto tiene los atributos de sistema u oculto configurados."""
    attrs = _get_win_attributes(entry)
    return bool(attrs & LIMITS.ATTRS.MASK_PROTECTED)

def _validate_file_identity(junk_file: JunkFile, src_real: Path) -> bool:
    """
    Verifica la inmutabilidad del archivo comparando inodo y dispositivo. 
    Previene el reemplazo malicioso del archivo durante la ejecución del proceso.
    """
    if not isinstance(src_real, Path): return False
    try:
        if not src_real.exists(): return False
        stat_result = src_real.stat()
        return (junk_file._ino is None or stat_result.st_ino == junk_file._ino) and \
               (junk_file._dev is None or stat_result.st_dev == junk_file._dev)
    except (OSError, AttributeError):
        return False

def _can_write_to_destination(dest_real: Path, size: int) -> bool:
    """Verifica la disponibilidad de espacio en disco en la unidad destino."""
    try:
        target_dir = dest_real.parent if dest_real.exists() else dest_real
        if not target_dir.exists() or not target_dir.is_dir() or not os.access(target_dir, os.W_OK):
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
    """Ejecuta una serie de validaciones secuenciales para verificar que la operación sea segura."""
    if not isinstance(junk_file, JunkFile) or not isinstance(junk_file.path, Path) or not junk_file.path.is_file(): 
        return False
    try:
        # 1. Comprobación de límites de ruta
        if len(str(junk_file.path)) >= LIMITS.MAX_PATH_LENGTH or len(str(dest)) >= LIMITS.MAX_PATH_LENGTH: return False
        
        src_real = junk_file.path.resolve(strict=True)
        dest_real = dest.resolve(strict=False)
        
        # 2. Verificación de entorno de destino
        if not dest_real.exists() or not dest_real.is_dir() or is_protected_path(dest_real) or not os.access(dest_real, os.W_OK): return False
        
        # 3. Prevención de riesgos estructurales (bucles, enlaces y rutas críticas)
        if _is_recursive_violation(src_real, dest_real) or src_real.name.lower() in SYSTEM_CRITICAL_NAMES: return False
        if src_real.is_symlink() or _is_junction(junk_file.path): return False
        
        # 4. Validación de seguridad contra listas bloqueadas y path traversal
        if not _validate_path_security(src_real, dest_real, root=dest_real): return False
        
        # 5. Integridad del archivo (inodo) y estado de bloqueo
        if not _validate_file_identity(junk_file, src_real) or _is_file_locked(src_real): return False
        
        # 6. Verificación de consistencia de dispositivo y espacio en disco
        if (junk_file._dev is not None and dest_real.stat().st_dev != junk_file._dev): return False
        if not _can_write_to_destination(dest_real, junk_file.size_bytes): return False
        
        return True
    except (OSError, AttributeError, ValueError, PermissionError):
        return False

def _should_scan_directory(entry: os.DirEntry, protected_cache: Set[str]) -> bool:
    """Determina si un directorio es candidato elegible para ser escaneado."""
    if entry.path in protected_cache or not _is_allowed_directory(entry.name) or _is_junction(Path(entry.path)): return False
    if _is_system_hidden(entry): return False
    return True

def _process_directory(current_path: str, junk_candidates: List[JunkFile], depth: int, protected_cache: Set[str], visited: Set[str]) -> None:
    """
    Recorrido recursivo del árbol de directorios con limitación de profundidad.
    Filtra directorios protegidos o junctions antes de continuar el escaneo.
    """
    if depth > LIMITS.MAX_SCAN_DEPTH or current_path in visited: return
        
    visited.add(current_path)
    
    try:
        with os.scandir(current_path) as iterator:
            for entry in iterator:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry, protected_cache):
                            if is_protected_path(Path(entry.path)):
                                protected_cache.add(entry.path)
                            else:
                                _process_directory(entry.path, junk_candidates, depth + 1, protected_cache, visited)
                    elif entry.name.lower().endswith(tuple(JUNK_EXTENSIONS)):
                        stats = entry.stat(follow_symlinks=False)
                        if stats.st_size < LIMITS.MAX_FILE_SIZE_BYTES and not _is_system_hidden(entry):
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
    """Inicia la búsqueda de candidatos a limpieza en las rutas proporcionadas."""
    junk_candidates: List[JunkFile] = []
    protected_cache: Set[str] = set()
    visited: Set[str] = set()
    scan_list = directories if directories is not None else DEFAULT_SCAN_DIRS
    if not isinstance(scan_list, (list, tuple)): return []
    for d in scan_list:
        if not d: continue
        try:
            p = Path(d).expanduser().resolve()
            if p.is_dir() and not _is_unc_path(p) and not is_protected_path(p):
                _process_directory(str(p), junk_candidates, 0, protected_cache, visited)
        except (OSError, RuntimeError):
            continue
    return junk_candidates

def sort_junk(files: Sequence[JunkFile], by: SortField = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena candidatos según criterio configurado (size/date) de forma estable."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """Traslada archivos candidatos al directorio de revisión antes de cualquier borrado."""
    if not isinstance(files, (list, tuple)) or not isinstance(review_dir, str) or not review_dir: 
        return None
    try:
        dest_base = Path(review_dir).expanduser().resolve()
        if not dest_base.exists():
            dest_base.mkdir(parents=True, exist_ok=True)
        
        if not is_safe_to_modify(dest_base) or not os.access(dest_base, os.W_OK):
            return None
        
        for junk_file in files:
            if not isinstance(junk_file, JunkFile) or not isinstance(junk_file.path, Path):
                continue
            try:
                if not junk_file.path.exists() or not junk_file.path.is_file():
                    continue
                if junk_file.path.is_symlink() or _is_junction(junk_file.path):
                    continue
                if not _is_safe_for_disk_op(junk_file, dest_base):
                    continue
                    
                target_path = _can_move_file(junk_file, dest_base)
                if target_path and not target_path.exists(): 
                    ensure_safe_to_modify(junk_file.path)
                    if _validate_file_identity(junk_file, junk_file.path) and os.access(junk_file.path, os.R_OK):
                        os.replace(str(junk_file.path), str(target_path))
            except (OSError, PermissionError, RuntimeError) as e:
                logger.warning(f"Error procesando {junk_file.path}: {e}")
                continue
        return dest_base
    except (OSError, PermissionError, RuntimeError) as e:
        logger.error(f"Falla crítica en stage_for_review: {e}")
        return None

def _is_same_file_identity(path: Path, expected_ino: int) -> bool:
    """Verifica si el inodo coincide con el valor previo a la operación."""
    try:
        return path.stat().st_ino == expected_ino
    except (OSError, AttributeError):
        return False

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Calcula la ruta de destino garantizando unicidad mediante un timestamp."""
    if not isinstance(junk_file.modified, datetime) or not isinstance(dest_base, Path): return None
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
    """Elimina permanentemente archivos tras la validación manual del usuario."""
    if not isinstance(review_dir, str) or not review_dir: return 0
    try:
        dest = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not dest.is_dir() or not is_safe_to_modify(dest):
            return 0
        
        count = 0
        for item in dest.iterdir():
            try:
                if not isinstance(item, Path) or not item.exists() or not item.is_file():
                    continue
                if item.is_symlink() or _is_junction(item):
                    continue
                if is_protected_path(item) or item.suffix.lower() not in JUNK_EXTENSIONS or _is_file_locked(item):
                    continue
                
                # Validación de seguridad defensiva y verificación de identidad final
                stat_before = item.stat()
                ensure_safe_to_modify(item)
                if item.exists() and _is_same_file_identity(item, stat_before.st_ino):
                    item.unlink()
                    count += 1
            except (OSError, PermissionError, RuntimeError) as e:
                logger.warning(f"Error al eliminar {getattr(item, 'name', 'archivo')}: {e}")
                continue
        return count
    except (OSError, PermissionError, RuntimeError):
        return 0
