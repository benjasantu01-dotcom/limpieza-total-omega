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
SYSTEM_CRITICAL_NAMES: Final[frozenset[str]] = frozenset({"pagefile.sys", "hiberfil.sys", "swapfile.sys"})

JUNK_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
})

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
    """Retorna una lista de letras de unidad disponibles en formato 'C:\\'."""
    if os.name != "nt":
        return []
    return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]

@dataclass
class JunkFile:
    """
    Representa un archivo candidato a limpieza.
    Almacena metadatos de bajo nivel (_ino, _dev) para detectar race conditions
    verificando si el archivo cambió en disco entre el escaneo y la manipulación.
    """
    path: Path
    size_bytes: int
    modified: datetime
    _ino: Optional[int] = None
    _dev: Optional[int] = None

    def __post_init__(self) -> None:
        """Captura metadatos de bajo nivel (inode/device) para validación de identidad."""
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
        """Calcula el tamaño del archivo en Megabytes."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si el archivo posee una extensión marcada como basura."""
        return self.path.suffix.lower() in JUNK_EXTENSIONS

def is_valid_junk_extension(filename: str) -> bool:
    """Valida si el nombre de archivo corresponde a una extensión de tipo basura."""
    return Path(filename).suffix.lower() in JUNK_EXTENSIONS

def _get_win_attributes(entry: os.DirEntry) -> int:
    """Obtiene el bitmask de atributos de Windows usando una llamada eficiente al sistema."""
    try:
        return entry.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError, ValueError):
        return 0

def _is_junction(entry: os.DirEntry) -> bool:
    """Determina si una entrada es un reparse point (junction) evitando recursión infinita."""
    try:
        return entry.is_symlink() or bool(_get_win_attributes(entry) & WIN_ATTR_JUNCTION)
    except (OSError, AttributeError):
        return True

def _is_unc_path(path: Path) -> bool:
    """Detecta rutas de red UNC (ej. \\servidor\recurso) con precauciones de acceso."""
    if not isinstance(path, Path): return True
    try:
        p_str = str(path.absolute())
        return p_str.startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """Añade un sufijo numérico al nombre si el archivo existe para evitar sobreescritura."""
    if not isinstance(target, Path): return target
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Filtra directorios basados en la blocklist definida de rutas críticas del sistema."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _is_file_locked(path: Path) -> bool:
    """
    Verifica si un archivo está bloqueado mediante un intento de apertura exclusiva.
    En Windows se usan handles de Kernel32; en otros sistemas se intenta un acceso de lectura.
    """
    if not path or not isinstance(path, Path) or not path.is_file(): return True
    if os.name == 'nt':
        try:
            # GENERIC_READ (0x80000000), FILE_SHARE_NONE (0), OPEN_EXISTING (3)
            handle = ctypes.windll.kernel32.CreateFileW(
                str(path), 0x80000000, 0, None, 3, 0x00000080, None
            )
            if handle == wintypes.HANDLE(-1).value:
                return True
            ctypes.windll.kernel32.CloseHandle(handle)
            return False
        except (AttributeError, TypeError, OSError, Exception):
            return True
    try:
        with open(path, "rb"): return False
    except (OSError, PermissionError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """Previene movimientos donde el destino es una carpeta dentro del origen."""
    try:
        src_resolved = src.resolve(strict=False)
        dest_resolved = dest.resolve(strict=False)
        if src_resolved == dest_resolved: return True
        return src_resolved in dest_resolved.parents
    except (OSError, ValueError, RuntimeError):
        return True

def _has_forbidden_chars(path: Path) -> bool:
    """Detecta caracteres reservados por NTFS que causan errores de manejo de archivos."""
    path_str = str(path).lower()
    return any(c in path_str for c in ["<", ">", "|", "\0"])

def _validate_path_security(src: Path, dest: Path) -> bool:
    """
    Realiza una auditoría exhaustiva antes de permitir la interacción con el archivo.
    Comprueba rutas UNC, caracteres ilegales, longitud de ruta y protección global.
    """
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > MAX_PATH_LENGTH or len(str(dest)) > MAX_PATH_LENGTH: return False
    return not (is_protected_path(src) or is_protected_path(dest))

def _is_system_hidden(entry: os.DirEntry) -> bool:
    """Verifica si el archivo está marcado como sistema u oculto en el OS."""
    return bool(_get_win_attributes(entry) & WIN_ATTR_MASK)

def _is_safe_for_disk_op(junk_file: JunkFile, dest: Path) -> bool:
    """
    Auditoría final de seguridad antes de mover un archivo.
    Realiza una validación combinada de metadatos, seguridad de ruta y recursos del sistema.
    """
    if not junk_file or not isinstance(junk_file.path, Path) or not junk_file.path.exists(): return False
    try:
        src_real = junk_file.path.resolve(strict=True)
        dest_real = dest.resolve(strict=False)
        
        # Validaciones de identidad y estado base
        if src_real.name.lower() in SYSTEM_CRITICAL_NAMES: return False
        if src_real.is_symlink() or (os.name == 'nt' and bool(src_real.stat().st_file_attributes & WIN_ATTR_JUNCTION)): return False
        
        stat_result = src_real.stat()
        if (junk_file._ino is not None and stat_result.st_ino != junk_file._ino) or \
           (junk_file._dev is not None and stat_result.st_dev != junk_file._dev): return False
        
        # Verificaciones de seguridad y permisos
        if not src_real.is_file() or is_protected_path(src_real) or is_protected_path(dest_real): return False
        if not is_safe_to_modify(src_real) or not _validate_path_security(src_real, dest_real): return False
        
        target_dir = dest_real.parent if dest_real.exists() else dest_real
        if not target_dir.is_dir() or not os.access(target_dir, os.W_OK): return False
        if _is_recursive_violation(src_real, dest_real) or _is_file_locked(src_real): return False
        
        # Validación de recursos
        usage = shutil.disk_usage(target_dir)
        return usage.free >= (stat_result.st_size + MIN_FREE_SPACE_BYTES)
    except (OSError, AttributeError, ValueError, FileNotFoundError):
        return False

def _should_scan_directory(entry: os.DirEntry, protected_cache: set[str]) -> bool:
    """Determina si un directorio es elegible para ser escaneado basándose en políticas de seguridad."""
    if not _is_allowed_directory(entry.name) or _is_junction(entry): return False
    if _is_system_hidden(entry): return False
    if entry.path in protected_cache: return False
    try:
        p = Path(entry.path)
        if not is_safe_to_modify(p) or is_protected_path(p):
            protected_cache.add(entry.path)
            return False
    except (OSError, RuntimeError):
        return False
    return True

def _is_candidate_junk(stats: os.stat_result, entry: os.DirEntry, now_ts: float) -> bool:
    """Aplica heurísticas de elegibilidad para archivos temporales (tamaño/antigüedad)."""
    return (0 <= stats.st_size < MAX_FILE_SIZE_BYTES and 
            stats.st_mtime <= now_ts + 3600 and
            not _is_system_hidden(entry))

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int, protected_cache: set[str], visited: set[Path]) -> None:
    """Realiza un escaneo recursivo limitado a una profundidad de 50 niveles."""
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
                        ext = os.path.splitext(entry.name)[1].lower()
                        if ext in JUNK_EXTENSIONS:
                            stats = entry.stat(follow_symlinks=False)
                            if _is_candidate_junk(stats, entry, now_ts):
                                found.append(JunkFile(Path(entry.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime), stats.st_ino, stats.st_dev))
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError, RuntimeError):
        pass

def scan_for_junk(directories: Optional[Sequence[str | Path]] = None) -> List[JunkFile]:
    """Inicia el escaneo de basura en las rutas especificadas o rutas predeterminadas."""
    found: List[JunkFile] = []
    protected_cache: set[str] = set()
    visited: set[Path] = set()
    scan_list: Sequence[str | Path] = directories or DEFAULT_SCAN_DIRS
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
    """Ordena el listado de archivos basándose en el registro de configuraciones definido."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """Prepara el movimiento de archivos detectados hacia una carpeta aislada de revisión."""
    if not isinstance(files, (list, tuple)) or not review_dir: return None
    try:
        dest_base = Path(review_dir).expanduser()
        if not dest_base.exists():
            dest_base.mkdir(parents=True, exist_ok=True)
        dest_res = dest_base.resolve(strict=False)
        
        if not is_safe_to_modify(dest_res): return None
        # Validación explícita antes de la acción crítica
        if is_protected_path(dest_res) or _has_forbidden_chars(dest_res): return None
        ensure_safe_to_modify(dest_res)
    except (OSError, RuntimeError, PermissionError):
        return None
    
    for junk_file in files:
        if not isinstance(junk_file, JunkFile) or not isinstance(junk_file.path, Path): continue
        try:
            if not _is_safe_for_disk_op(junk_file, dest_res): continue
            target_path = _can_move_file(junk_file, dest_res)
            if target_path and target_path != junk_file.path:
                src_abs = junk_file.path.resolve()
                ensure_safe_to_modify(src_abs)
                shutil.move(str(src_abs), str(target_path))
        except (OSError, shutil.Error, PermissionError) as e:
            logger.error(f"Error moviendo {junk_file.path}: {e}")
            continue
    return dest_res

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Genera una ruta de destino segura añadiendo un timestamp para trazabilidad."""
    if not junk_file or not junk_file.path or not dest_base: return None
    try:
        safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
        return _generate_unique_target(dest_base / safe_name)
    except (OSError, AttributeError, ValueError, OverflowError): 
        return None

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Realiza el borrado permanente tras verificar rigurosamente la seguridad de la ruta."""
    if not review_dir: return 0
    try:
        dest = Path(review_dir).expanduser().resolve(strict=False)
        if not dest.exists() or not dest.is_dir() or not is_safe_to_modify(dest): return 0
        if is_protected_path(dest): return 0
        ensure_safe_to_modify(dest)
        
        count = 0
        for item in dest.iterdir():
            try:
                if not item.is_file(): continue
                # Validación estricta de seguridad pre-borrado
                if not is_safe_to_modify(item) or is_protected_path(item): continue
                if dest.resolve(strict=True) != item.resolve(strict=True).parent: continue
                
                ensure_safe_to_modify(item)
                item.unlink()
                count += 1
            except (OSError, PermissionError, FileNotFoundError) as e:
                logger.error(f"Error borrando {item}: {e}")
                continue
        return count
    except (OSError, PermissionError, RuntimeError):
        return 0
