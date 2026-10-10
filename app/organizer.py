"""
organizer.py
Este módulo implementa la lógica de detección y gestión de archivos temporales.

Su objetivo es identificar candidatos a limpieza y moverlos a un entorno de 
revisión aislado (_Para_Revisar). Toda operación crítica se apoya en 
`safety.py` para garantizar que no se manipulen rutas de sistema o archivos 
bloqueados por procesos críticos del SO.
"""

from __future__ import annotations
import os
import shutil
import string
import logging
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
SYSTEM_CRITICAL_NAMES: Final[frozenset[str]] = frozenset({"pagefile.sys", "hiberfil.sys", "swapfile.sys"})

JUNK_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
})
JUNK_EXT_TUPLE: Final[tuple[str, ...]] = tuple(JUNK_EXTENSIONS)

class SortConfig(NamedTuple):
    """Configuración para criterios de ordenamiento de archivos."""
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
    """Retorna una lista de letras de unidad disponibles en formato 'C:\\'."""
    if os.name != "nt":
        return []
    return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]

@dataclass
class JunkFile:
    """Representa un archivo candidato a limpieza con sus metadatos básicos."""
    path: Path
    size_bytes: int
    modified: datetime
    _ino: Optional[int] = None
    _dev: Optional[int] = None

    def __post_init__(self) -> None:
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
        """Calcula el tamaño del archivo en Megabytes (redondeado a 2 decimales)."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión del archivo coincide con las heurísticas de basura."""
        return self.path.name.lower().endswith(JUNK_EXT_TUPLE)

def is_valid_junk_extension(filename: str) -> bool:
    """Valida si el sufijo del archivo pertenece a la lista definida en JUNK_EXTENSIONS."""
    return filename.lower().endswith(JUNK_EXT_TUPLE)

def _get_win_attributes(entry: os.DirEntry) -> int:
    """Extrae atributos de archivo (bitmask) usando syscall de bajo nivel para Windows."""
    try:
        return entry.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError, ValueError):
        return 0

def _is_junction(entry: os.DirEntry) -> bool:
    """Determina si una entrada de directorio es un punto de reparse (Junction)."""
    try:
        return entry.is_symlink() or bool(_get_win_attributes(entry) & WIN_ATTR_JUNCTION)
    except (OSError, AttributeError):
        return True

def _is_unc_path(path: Path) -> bool:
    """Detecta rutas de red (UNC) que no deben procesarse como directorios locales."""
    if not isinstance(path, Path): return True
    try:
        p_str = str(path.absolute())
        return p_str.startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """Resuelve colisiones de nombre añadiendo un contador al sufijo para evitar sobreescritura."""
    if not isinstance(target, Path): return target
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Verifica que el nombre de la carpeta no figure en SYSTEM_FOLDER_BLOCKLIST."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _is_file_locked(path: Path) -> bool:
    """Valida si un archivo está bloqueado mediante apertura exclusiva a nivel de OS."""
    if not path or not path.is_file(): return True
    try:
        with open(path, "rb"):
            return False
    except (OSError, PermissionError, FileNotFoundError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """Verifica si el destino es un subdirectorio del origen para evitar recursión infinita."""
    try:
        src_resolved = src.resolve(strict=False)
        dest_resolved = dest.resolve(strict=False)
        if src_resolved == dest_resolved: return True
        return src_resolved in dest_resolved.parents
    except (OSError, ValueError, RuntimeError):
        return True

def _has_forbidden_chars(path: Path) -> bool:
    """Valida la ausencia de caracteres reservados de NTFS que impiden rutas válidas."""
    path_str = str(path).lower()
    return any(c in path_str for c in ["<", ">", "|", "\0"])

def _validate_path_security(src: Path, dest: Path) -> bool:
    """Verifica restricciones de ruta (longitud, caracteres, protección) previo a movimiento."""
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > MAX_PATH_LENGTH or len(str(dest)) > MAX_PATH_LENGTH: return False
    return not (is_protected_path(src) or is_protected_path(dest))

def _is_system_hidden(entry: os.DirEntry) -> bool:
    """Determina si una entrada tiene atributos de sistema u ocultos activados."""
    return bool(_get_win_attributes(entry) & WIN_ATTR_MASK)

def _is_safe_for_disk_op(junk_file: JunkFile, dest: Path) -> bool:
    """
    Realiza una auditoría de seguridad multietapa antes de cualquier operación física:
    1. Valida existencia y consistencia de inodos para evitar race conditions.
    2. Aplica filtros de seguridad de `safety.py`.
    3. Asegura espacio en disco suficiente y previene recursión de directorios.
    """
    if not junk_file or not isinstance(junk_file.path, Path): return False
    src = junk_file.path
    try:
        if not src.exists() or src.name.lower() in SYSTEM_CRITICAL_NAMES: return False
        stat_result = src.stat()
        if (junk_file._ino is not None and stat_result.st_ino != junk_file._ino) or \
           (junk_file._dev is not None and stat_result.st_dev != junk_file._dev): return False
        
        if not src.is_file() or src.is_symlink(): return False
        
        try:
            if src.resolve(strict=True) != src: return False
        except (OSError, RuntimeError):
            return False
        
        if not is_safe_to_modify(src) or is_protected_path(dest) or is_protected_path(dest.parent) or not _validate_path_security(src, dest): return False
        
        target_dir = dest.parent if dest.exists() else dest
        if not target_dir.is_dir() or not os.access(target_dir, os.W_OK): return False
        if _is_recursive_violation(src, dest) or _is_file_locked(src): return False
        
        usage = shutil.disk_usage(target_dir)
        if usage.free < (stat_result.st_size + MIN_FREE_SPACE_BYTES): return False
        return True
    except (OSError, AttributeError, ValueError, FileNotFoundError):
        return False

def _should_scan_directory(entry: os.DirEntry, protected_cache: set[str]) -> bool:
    """Filtra directorios para escaneo basándose en heurísticas de seguridad y caché."""
    if not _is_allowed_directory(entry.name) or _is_junction(entry): return False
    if _is_system_hidden(entry): return False
    if entry.path in protected_cache: return False
    try:
        if is_protected_path(Path(entry.path)):
            protected_cache.add(entry.path)
            return False
    except (OSError, RuntimeError):
        return False
    return True

def _is_candidate_junk(stats: os.stat_result, entry: os.DirEntry, now_ts: float) -> bool:
    """Evalúa criterios heurísticos para marcar un archivo como basura."""
    return (0 <= stats.st_size < MAX_FILE_SIZE_BYTES and 
            stats.st_mtime <= now_ts + 3600 and
            not _is_system_hidden(entry))

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int, protected_cache: set[str], visited: set[Path]) -> None:
    """
    Recorre recursivamente directorios para recolectar archivos candidatos.
    Implementa control de profundidad máxima y caché de rutas visitadas para
    evitar bucles infinitos y optimizar el rendimiento en estructuras profundas.
    """
    if depth > 50: return
    try:
        resolved_dir = current_dir.resolve(strict=False)
        if resolved_dir in visited: return
        visited.add(resolved_dir)
        now_ts: float = datetime.now().timestamp()
        
        with os.scandir(current_dir) as iterator:
            for entry in iterator:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry, protected_cache):
                            _process_directory(Path(entry.path), found, depth + 1, protected_cache, visited)
                    elif entry.is_file(follow_symlinks=False):
                        if entry.name.lower().endswith(JUNK_EXT_TUPLE):
                            stats = entry.stat(follow_symlinks=False)
                            if _is_candidate_junk(stats, entry, now_ts):
                                found.append(JunkFile(Path(entry.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime), stats.st_ino, stats.st_dev))
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError, RuntimeError):
        pass

def scan_for_junk(directories: Optional[Sequence[str | Path]] = None) -> List[JunkFile]:
    """Escanea directorios en busca de basura, aplicando validaciones de seguridad previas."""
    found: List[JunkFile] = []
    protected_cache: set[str] = set()
    visited: set[Path] = set()
    scan_list: Sequence[str | Path] = directories or DEFAULT_SCAN_DIRS
    for d in scan_list:
        if not d: continue
        try:
            p = Path(d).expanduser()
            if p.is_dir() and not _is_unc_path(p) and is_safe_to_modify(p):
                _process_directory(p, found, 0, protected_cache, visited)
        except (OSError, RuntimeError):
            continue
    return found

def sort_junk(files: Sequence[JunkFile], by: SortField = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena los archivos encontrados basándose en el registro de configuración."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """Gestiona el movimiento de archivos validados a la zona de cuarentena."""
    if not files or not review_dir: return None
    try:
        dest_base = Path(review_dir).expanduser()
        if not dest_base.exists():
            dest_base.mkdir(parents=True, exist_ok=True)
        dest_res = dest_base.resolve(strict=False)
        
        # Validar destino antes de procesar archivos
        if not is_safe_to_modify(dest_res): return None
        ensure_safe_to_modify(dest_res)
    except (OSError, RuntimeError, PermissionError):
        return None
    
    for junk_file in files:
        if not junk_file or not isinstance(junk_file.path, Path): continue
        try:
            if not _is_safe_for_disk_op(junk_file, dest_res): continue
            target_path = _can_move_file(junk_file, dest_res)
            if target_path and target_path != junk_file.path and is_safe_to_modify(junk_file.path):
                ensure_safe_to_modify(junk_file.path)
                shutil.move(str(junk_file.path), str(target_path))
        except (OSError, shutil.Error, PermissionError):
            continue
    return dest_res

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Genera un nombre de archivo único en destino evitando colisiones."""
    if not junk_file or not junk_file.path or not dest_base: return None
    try:
        safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
        return _generate_unique_target(dest_base / safe_name)
    except (OSError, AttributeError, ValueError, OverflowError): 
        return None

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Elimina permanentemente archivos tras verificación de seguridad obligatoria."""
    if not review_dir: return 0
    try:
        dest = Path(review_dir).expanduser().resolve(strict=False)
        if not dest.exists() or not dest.is_dir() or not is_safe_to_modify(dest): return 0
        ensure_safe_to_modify(dest)
        
        count = 0
        for item in dest.iterdir():
            try:
                if item.is_file() and is_safe_to_modify(item) and not is_protected_path(item):
                    ensure_safe_to_modify(item)
                    item.unlink()
                    count += 1
            except (OSError, PermissionError, FileNotFoundError):
                continue
        return count
    except (OSError, PermissionError, RuntimeError):
        return 0
