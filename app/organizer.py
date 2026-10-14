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
from typing import List, Optional, Final, Callable, Union, TypeAlias, NamedTuple, Dict, Sequence, Literal

from safety import is_safe_to_modify, ensure_safe_to_modify, is_protected_path

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger(__name__)

SortKey: TypeAlias = Union[int, datetime]
SortField: TypeAlias = Literal["size", "date"]

# Constantes de control de límites y seguridad:
WIN_ATTR_JUNCTION: Final[int] = 0x400
WIN_ATTR_SYSTEM: Final[int] = 0x04
WIN_ATTR_HIDDEN: Final[int] = 0x02
WIN_ATTR_MASK: Final[int] = WIN_ATTR_SYSTEM | WIN_ATTR_HIDDEN
MAX_PATH_LENGTH: Final[int] = 260
MAX_FILE_SIZE_BYTES: Final[int] = 100_000_000_000
MIN_FREE_SPACE_BYTES: Final[int] = 52_428_800
MAX_SCAN_DEPTH: Final[int] = 50
SYSTEM_CRITICAL_NAMES: Final[frozenset[str]] = frozenset({"pagefile.sys", "hiberfil.sys", "swapfile.sys"})

JUNK_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
})
JUNK_EXTENSIONS_TUPLE: Final[tuple[str, ...]] = tuple(JUNK_EXTENSIONS)

class SortConfig(NamedTuple):
    """Configuración que mapea un campo de ordenamiento con su función de extracción."""
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
    """Retorna una lista de letras de unidad disponibles en el sistema (solo Windows)."""
    if os.name != "nt":
        return []
    return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]

@dataclass
class JunkFile:
    """
    Representa un archivo candidato a limpieza.
    
    Captura metadatos de bajo nivel (inode/device ID) para validar que el archivo 
    no ha sido reemplazado o movido físicamente entre el escaneo y la manipulación.
    """
    path: Path
    size_bytes: int
    modified: datetime
    _ino: Optional[int] = None
    _dev: Optional[int] = None

    def __post_init__(self) -> None:
        """Captura metadatos de identidad del archivo para asegurar la inmutabilidad de la instancia."""
        try:
            stat_result = self.path.stat()
            if self._ino is None:
                self._ino = stat_result.st_ino
            if self._dev is None:
                self._dev = stat_result.st_dev
        except (OSError, RuntimeError):
            pass

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño del archivo convertido a Megabytes, redondeado a dos decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Determina si la extensión del archivo está presente en el conjunto de extensiones basura."""
        return self.path.suffix.lower() in JUNK_EXTENSIONS

def is_valid_junk_extension(filename: str) -> bool:
    """Verifica si un nombre de archivo dado posee una extensión considerada basura."""
    if not filename: return False
    return Path(filename).suffix.lower() in JUNK_EXTENSIONS

def _get_win_attributes(entry: os.DirEntry) -> int:
    """Obtiene una máscara de bits de atributos de archivo de Windows."""
    try:
        return entry.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError, ValueError):
        return 0

def _is_junction(entry: os.DirEntry) -> bool:
    """Identifica si una entrada es un reparse point/junction, evitando bucles en recursión."""
    try:
        return entry.is_symlink() or bool(_get_win_attributes(entry) & WIN_ATTR_JUNCTION)
    except (OSError, AttributeError):
        return True

def _is_unc_path(path: Path) -> bool:
    """Detecta si una ruta es UNC (red), la cual requiere protocolos de acceso restringidos."""
    if not isinstance(path, Path): return True
    try:
        p_str = str(path.absolute())
        return p_str.startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """Genera una ruta única ante colisiones de nombres añadiendo un contador numérico."""
    if not isinstance(target, Path): return target
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Valida si el nombre del directorio no figura en la lista negra de sistemas críticos."""
    if not name: return False
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _try_open_win_file(path: Path) -> bool:
    """Intenta obtener acceso de lectura exclusivo a un archivo bajo Windows para detectar bloqueos."""
    handle = ctypes.windll.kernel32.CreateFileW(
        str(path), 0x80000000, 0x00000001, None, 3, 0x00000080, None
    )
    if handle == wintypes.HANDLE(-1).value:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True

def _is_file_locked(path: Path) -> bool:
    """Determina si un archivo está actualmente en uso por el sistema o procesos externos."""
    if not path or not isinstance(path, Path) or not path.is_file(): return True
    if os.name == 'nt':
        try:
            return not _try_open_win_file(path)
        except (AttributeError, TypeError, OSError, Exception):
            return True
    try:
        with open(path, "rb"): return False
    except (OSError, PermissionError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """Previene ciclos detectando si el destino es un ancestro o el mismo que el origen."""
    try:
        src_resolved = src.resolve(strict=False)
        dest_resolved = dest.resolve(strict=False)
        if src_resolved == dest_resolved: return True
        return src_resolved in dest_resolved.parents
    except (OSError, ValueError, RuntimeError):
        return True

def _has_forbidden_chars(path: Path) -> bool:
    """Detecta caracteres no permitidos en rutas NTFS, previniendo inyecciones de ruta."""
    if not path: return True
    path_str = str(path).lower()
    return any(c in path_str for c in ["<", ">", "|", "\0"])

def _validate_path_security(src: Path, dest: Path) -> bool:
    """Ejecuta una auditoría integral de seguridad previa a operaciones de E/S."""
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > MAX_PATH_LENGTH or len(str(dest)) > MAX_PATH_LENGTH: return False
    return not (is_protected_path(src) or is_protected_path(dest))

def _is_system_hidden(entry: os.DirEntry) -> bool:
    """Verifica si la entrada tiene atributos de 'oculto' o 'sistema' definidos en Windows."""
    return bool(_get_win_attributes(entry) & WIN_ATTR_MASK)

def _is_safe_for_disk_op(junk_file: JunkFile, dest: Path) -> bool:
    """
    Valida la viabilidad y seguridad de realizar una operación de disco sobre un JunkFile.
    
    Combina validación de existencia, integridad de inodo, protección contra rutas 
    críticas, permisos de escritura y comprobación de espacio en disco.
    """
    if not junk_file or not isinstance(junk_file.path, Path): return False
    try:
        if not junk_file.path.exists(): return False
        src_real = junk_file.path.resolve(strict=True)
        dest_real = dest.resolve(strict=False)
        
        if src_real.name.lower() in SYSTEM_CRITICAL_NAMES: return False
        if src_real.is_symlink() or (os.name == 'nt' and bool(src_real.stat().st_file_attributes & WIN_ATTR_JUNCTION)): return False
        if is_protected_path(src_real): return False
        
        stat_result = src_real.stat()
        if (junk_file._ino is not None and stat_result.st_ino != junk_file._ino) or \
           (junk_file._dev is not None and stat_result.st_dev != junk_file._dev): return False
        
        if not src_real.is_file() or is_protected_path(dest_real): return False
        if not is_safe_to_modify(src_real) or not _validate_path_security(src_real, dest_real): return False
        
        target_dir = dest_real.parent if dest_real.exists() else dest_real
        if not target_dir.is_dir() or not os.access(target_dir, os.W_OK): return False
        if _is_recursive_violation(src_real, dest_real) or _is_file_locked(src_real): return False
        
        usage = shutil.disk_usage(target_dir)
        return usage.free >= (stat_result.st_size + MIN_FREE_SPACE_BYTES)
    except (OSError, AttributeError, ValueError, PermissionError):
        return False

def _should_scan_directory(entry: os.DirEntry, protected_cache: set[str]) -> bool:
    """Decide si es seguro entrar en un directorio basado en caché de protección."""
    if not _is_allowed_directory(entry.name) or _is_junction(entry): return False
    if _is_system_hidden(entry): return False
    if entry.path in protected_cache: return False
    # Cacheamos el resultado negativo también para evitar re-chequeos costosos
    try:
        path = Path(entry.path)
        if is_protected_path(path) or not is_safe_to_modify(path):
            protected_cache.add(entry.path)
            return False
    except (OSError, RuntimeError):
        return False
    return True

def _is_candidate_junk(stats: os.stat_result, entry: os.DirEntry, now_ts: float) -> bool:
    """Filtra archivos por tamaño, antigüedad y atributos de visibilidad."""
    return (0 <= stats.st_size < MAX_FILE_SIZE_BYTES and 
            stats.st_mtime <= now_ts + 3600 and
            not _is_system_hidden(entry))

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int, protected_cache: set[str], visited: set[Path]) -> None:
    """Escaneo recursivo de directorios con control de profundidad y caché para evitar redundancias."""
    if depth > MAX_SCAN_DEPTH: return
    try:
        resolved_dir = current_dir.resolve(strict=False)
        if resolved_dir in visited: return
        visited.add(resolved_dir)
        
        # Validar nodo actual antes de iterar
        if is_protected_path(resolved_dir) or not is_safe_to_modify(resolved_dir):
            protected_cache.add(str(resolved_dir))
            return
            
        now_ts: float = datetime.now().timestamp()
        
        with os.scandir(current_dir) as iterator:
            for entry in iterator:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry, protected_cache):
                            _process_directory(Path(entry.path), found, depth + 1, protected_cache, visited)
                    elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(JUNK_EXTENSIONS_TUPLE):
                        # La comprobación de protección individual se realiza mediante caché o llamada directa
                        if entry.path in protected_cache:
                            continue
                        stats = entry.stat(follow_symlinks=False)
                        if _is_candidate_junk(stats, entry, now_ts):
                            found.append(JunkFile(Path(entry.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime), stats.st_ino, stats.st_dev))
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError, RuntimeError):
        pass

def scan_for_junk(directories: Optional[Sequence[str | Path]] = None) -> List[JunkFile]:
    """Inicia el escaneo recursivo de archivos temporales en las ubicaciones especificadas."""
    found: List[JunkFile] = []
    protected_cache: set[str] = set()
    visited: set[Path] = set()
    scan_list: Sequence[str | Path] = directories if directories is not None else DEFAULT_SCAN_DIRS
    for d in scan_list:
        if d is None: continue
        try:
            p = Path(d).expanduser()
            if p.is_dir() and not _is_unc_path(p) and is_safe_to_modify(p):
                _process_directory(p, found, 0, protected_cache, visited)
        except (OSError, RuntimeError):
            continue
    return found

def sort_junk(files: Sequence[JunkFile], by: SortField = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena una lista de archivos basura según el criterio especificado (size o date)."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Mueve archivos detectados a un directorio de cuarentena para su posterior revisión.
    
    Verifica la seguridad de la ruta destino antes de efectuar cualquier movimiento.
    """
    if not isinstance(files, (list, tuple)) or not review_dir: return None
    try:
        dest_base = Path(review_dir).expanduser()
        if not dest_base.exists():
            dest_base.mkdir(parents=True, exist_ok=True)
        dest_res = dest_base.resolve(strict=False)
        
        if not is_safe_to_modify(dest_res) or is_protected_path(dest_res) or _has_forbidden_chars(dest_res): return None
        ensure_safe_to_modify(dest_res)
    except (OSError, RuntimeError, PermissionError):
        return None
    
    for junk_file in files:
        if not isinstance(junk_file, JunkFile) or not isinstance(junk_file.path, Path): continue
        try:
            if not _is_safe_for_disk_op(junk_file, dest_res): continue
            target_path = _can_move_file(junk_file, dest_res)
            if target_path and target_path != junk_file.path:
                src_abs = junk_file.path.resolve(strict=True)
                ensure_safe_to_modify(src_abs)
                shutil.move(str(src_abs), str(target_path))
        except (OSError, shutil.Error, PermissionError) as e:
            logger.error(f"Error moviendo {junk_file.path}: {e}")
            continue
    return dest_res

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Genera una ruta de destino segura, evitando colisiones de nombres."""
    if not junk_file or not junk_file.path or not dest_base: return None
    try:
        safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
        return _generate_unique_target(dest_base / safe_name)
    except (OSError, AttributeError, ValueError, OverflowError): 
        return None

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Borra permanentemente los archivos validados presentes en el directorio de revisión."""
    if not review_dir: return 0
    try:
        dest = Path(review_dir).expanduser().resolve(strict=False)
        if not dest.exists() or not dest.is_dir() or len(dest.parts) <= 2: return 0
        if not is_safe_to_modify(dest) or is_protected_path(dest): return 0
        ensure_safe_to_modify(dest)
        
        count = 0
        for item in dest.iterdir():
            try:
                if not item.is_file(): continue
                item_res = item.resolve(strict=True)
                if not is_safe_to_modify(item_res) or is_protected_path(item_res): continue
                if dest.resolve(strict=True) != item_res.parent: continue
                
                ensure_safe_to_modify(item_res)
                item_res.unlink()
                count += 1
            except (OSError, PermissionError, FileNotFoundError) as e:
                logger.error(f"Error borrando {item}: {e}")
                continue
        return count
    except (OSError, PermissionError, RuntimeError):
        return 0
