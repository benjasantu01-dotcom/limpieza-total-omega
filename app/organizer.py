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
from typing import List, Optional, Final, Callable, Union, TypeAlias, NamedTuple, Dict, Sequence

from safety import is_safe_to_modify, ensure_safe_to_modify, is_protected_path

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger(__name__)

SortKey: TypeAlias = Union[int, datetime]

# Constantes de control de límites y seguridad
WIN_ATTR_JUNCTION: Final[int] = 0x400
WIN_ATTR_SYSTEM: Final[int] = 0x04
WIN_ATTR_HIDDEN: Final[int] = 0x02
WIN_ATTR_MASK: Final[int] = WIN_ATTR_SYSTEM | WIN_ATTR_HIDDEN
MAX_PATH_LENGTH: Final[int] = 260
MAX_FILE_SIZE_BYTES: Final[int] = 100_000_000_000  # 100 GB
MIN_FREE_SPACE_BYTES: Final[int] = 52_428_800     # 50 MB

# Optimización: Tupla de extensiones para validación más rápida con endswith
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

    def __post_init__(self) -> None:
        try:
            if self.path.is_absolute():
                self.path = self.path.resolve()
        except (OSError, RuntimeError):
            pass

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño del archivo en Megabytes (redondeado a 2 decimales)."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión del archivo coincide con las heurísticas de basura."""
        return is_valid_junk_extension(self.path.name)

def is_valid_junk_extension(filename: str) -> bool:
    """Valida si el sufijo del archivo pertenece a la lista definida en JUNK_EXTENSIONS."""
    if not filename: return False
    return filename.lower().endswith(JUNK_EXT_TUPLE)

def _get_win_attributes(entry: os.DirEntry) -> int:
    """
    Extrae atributos de archivo (bitmask) usando syscall de bajo nivel.
    Retorna 0 en caso de fallos de acceso o si el atributo no está disponible.
    """
    try:
        return entry.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError, ValueError):
        return 0

def _is_junction(entry: os.DirEntry) -> bool:
    """
    Determina si una entrada de directorio es un punto de reparse (Junction) o enlace simbólico.
    Bloquear el seguimiento de estas rutas previene recursiones infinitas y accesos no deseados.
    """
    try:
        return entry.is_symlink() or bool(_get_win_attributes(entry) & WIN_ATTR_JUNCTION)
    except (OSError, AttributeError):
        return True

def _is_unc_path(path: Path) -> bool:
    """
    Detecta rutas de red (Universal Naming Convention) que requieren manejo especial.
    Las rutas UNC suelen ser inestables para operaciones de movimiento bloqueantes.
    """
    if path is None: return True
    try:
        p_str = str(path.absolute())
        return p_str.startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """
    Resuelve colisiones en el destino añadiendo un contador numérico al nombre base.
    Límite de 999 copias para evitar bucles de renombramiento accidentales.
    """
    if not isinstance(target, Path): return target
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Verifica que el nombre de la carpeta no figure en SYSTEM_FOLDER_BLOCKLIST."""
    if not name: return False
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _is_file_locked(path: Path) -> bool:
    """
    Verifica si el sistema permite acceso exclusivo de escritura abriendo el archivo.
    Retorna True si el archivo está en uso por otro proceso o no es accesible.
    """
    if not isinstance(path, Path) or not path.is_file():
        return True
    try:
        with open(path, "ab"):
            return False
    except (PermissionError, OSError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """Previene que una operación de movimiento resulte en un bucle lógico o recursivo."""
    if src is None or dest is None: return True
    try:
        s = src.resolve(strict=False)
        d = dest.resolve(strict=False)
        if s == d: return True
        return os.path.commonpath([str(s), str(d)]) == str(s)
    except (OSError, ValueError):
        return True

def _has_forbidden_chars(path: Path) -> bool:
    """Valida la ausencia de caracteres reservados que corromperían la ruta en sistemas NTFS."""
    if path is None: return True
    path_str = str(path).lower()
    return any(c in path_str for c in ["<", ">", "|", "\0"])

def _validate_path_security(src: Path, dest: Path) -> bool:
    """
    Realiza una validación de integridad sobre rutas para evitar colisiones de seguridad.
    Verifica restricciones de UNC, longitud máxima y bloqueo de rutas protegidas.
    """
    if src is None or dest is None: return False
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > MAX_PATH_LENGTH or len(str(dest)) > MAX_PATH_LENGTH: return False
    return not (is_protected_path(src) or is_protected_path(dest))

def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Auditoría de pre-condiciones necesaria antes de ejecutar cualquier escritura en disco.
    Verifica existencia, inmutabilidad de enlaces, permisos de escritura y bloqueos.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    try:
        if not src.exists(): return False
        st = src.lstat()
        if not src.is_file() or (st.st_mode & 0o170000 == 0o120000) or st.st_nlink > 1: return False
        if not is_safe_to_modify(src) or not _validate_path_security(src, dest): return False
        
        target_dir = dest.parent if dest.exists() else dest
        if not target_dir.is_dir() or not os.access(target_dir, os.W_OK): return False
        
        if src.drive != target_dir.drive or _is_recursive_violation(src, dest): return False
        if _is_file_locked(src): return False
        
        # Validación extra: asegurarse que ambos residen en el mismo volumen lógico
        if src.resolve().drive.lower() != target_dir.resolve().drive.lower(): return False
        
        return True
    except (OSError, RuntimeError, AttributeError, ValueError):
        return False

def _should_scan_directory(entry: os.DirEntry, protected_cache: set[str]) -> bool:
    """
    Filtra directorios aptos para escaneo, utilizando caché para evitar chequeos redundantes.
    Descarta rutas de sistema y puntos de reparse (junctions).
    """
    if entry is None or not _is_allowed_directory(entry.name) or _is_junction(entry): return False
    if bool(_get_win_attributes(entry) & WIN_ATTR_SYSTEM): return False
    if entry.path in protected_cache: return False
    if is_protected_path(Path(entry.path)):
        protected_cache.add(entry.path)
        return False
    return True

def _is_valid_junk_entry(name: str, stats: os.stat_result, now_ts: float) -> bool:
    """Verifica si el archivo cumple con las heurísticas de tamaño, fecha y extensión."""
    return (0 <= stats.st_size < MAX_FILE_SIZE_BYTES and 
            stats.st_mtime <= now_ts + 3600 and
            name.lower().endswith(JUNK_EXT_TUPLE))

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int, protected_cache: set[str], visited: set[Path]) -> None:
    """
    Recorrido recursivo optimizado utilizando os.scandir para minimizar syscalls.
    Límite de profundidad (50) para evitar desbordamientos y excesos de procesamiento.
    """
    if depth > 50: return
    try:
        resolved_dir = current_dir.resolve()
        if resolved_dir in visited: return
        visited.add(resolved_dir)
        
        now_ts = datetime.now().timestamp()
        with os.scandir(current_dir) as iterator:
            for item in iterator:
                try:
                    if item.is_dir(follow_symlinks=False):
                        if _should_scan_directory(item, protected_cache):
                            _process_directory(Path(item.path), found, depth + 1, protected_cache, visited)
                    elif item.is_file(follow_symlinks=False):
                        stats = item.stat(follow_symlinks=False)
                        if _is_valid_junk_entry(item.name, stats, now_ts):
                            if not (getattr(stats, 'st_file_attributes', 0) & WIN_ATTR_MASK):
                                found.append(JunkFile(Path(item.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime)))
                except (PermissionError, OSError): continue
    except (PermissionError, OSError, RuntimeError): pass

def scan_for_junk(directories: Optional[Sequence[str | Path]] = None) -> List[JunkFile]:
    """Escanea los directorios especificados en busca de archivos basura."""
    found: List[JunkFile] = []
    protected_cache: set[str] = set()
    visited: set[Path] = set()
    scan_list: Sequence[str | Path] = directories or DEFAULT_SCAN_DIRS
    for d in scan_list:
        if not d: continue
        try:
            p = Path(d).expanduser()
            if p.exists() and p.is_dir() and not _is_unc_path(p) and is_safe_to_modify(p):
                _process_directory(p, found, 0, protected_cache, visited)
        except (OSError, RuntimeError): continue
    return found

def sort_junk(files: Sequence[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena el listado de archivos basura basándose en el registro de configuración."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Prepara y mueve los archivos candidatos a un directorio de cuarentena.
    Realiza validaciones previas de seguridad en cada archivo antes de proceder con shutil.move.
    """
    if not files: return None
    try:
        dest_base = Path(review_dir).expanduser()
        if not dest_base.exists():
            dest_base.mkdir(parents=True, exist_ok=True)
        dest_res = dest_base.resolve()
        if is_protected_path(dest_res): return None
        ensure_safe_to_modify(dest_res)
    except (OSError, RuntimeError, PermissionError) as e:
        logger.error(f"Fallo en inicialización de carpeta de revisión: {e}")
        return None
    
    for junk_file in files:
        if junk_file is None or not isinstance(junk_file.path, Path): continue
        try:
            if not junk_file.path.exists() or not is_safe_to_modify(junk_file.path): continue
            if not _is_safe_for_disk_op(junk_file.path, dest_res): continue
            
            target_path = _can_move_file(junk_file, dest_res)
            if target_path:
                ensure_safe_to_modify(junk_file.path)
                shutil.move(str(junk_file.path), str(target_path))
        except (OSError, shutil.Error, PermissionError) as e:
            logger.error(f"Error moviendo {junk_file.path}: {e}")
            continue
    return dest_res

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Verifica disponibilidad de espacio en disco y genera una ruta única destino."""
    if junk_file is None or dest_base is None or not dest_base.exists(): return None
    try:
        usage = shutil.disk_usage(dest_base.anchor)
        if usage.free < (junk_file.size_bytes + MIN_FREE_SPACE_BYTES): return None
        safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
        return _generate_unique_target(dest_base / safe_name)
    except (OSError, AttributeError, ValueError): return None

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Elimina permanentemente archivos del directorio de revisión tras validación explícita de seguridad."""
    try:
        dest = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not dest.is_dir(): return 0
        if is_protected_path(dest) or not is_safe_to_modify(dest): return 0
        
        count = 0
        for item in dest.iterdir():
            try:
                if item.is_file() and is_safe_to_modify(item) and not is_protected_path(item):
                    ensure_safe_to_modify(item)
                    item.unlink()
                    count += 1
            except (OSError, PermissionError) as e:
                logger.warning(f"No se pudo eliminar {item}: {e}")
                continue
        return count
    except (OSError, PermissionError, RuntimeError) as e:
        logger.error(f"Error accediendo a directorio de revisión: {e}")
        return 0
