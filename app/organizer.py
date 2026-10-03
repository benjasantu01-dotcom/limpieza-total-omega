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

class SortConfig(NamedTuple):
    """Configuración para criterios de ordenamiento de archivos."""
    field: str
    key_func: Callable[[JunkFile], SortKey]

SORT_REGISTRY: Final[Dict[str, SortConfig]] = {
    "size": SortConfig("size", lambda f: f.size_bytes),
    "date": SortConfig("date", lambda f: f.modified)
}

JUNK_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
})

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
    
    Almacena metadatos esenciales para que el usuario pueda decidir sobre la 
    seguridad y utilidad de mover o eliminar el archivo.
    """
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
    """Valida si el sufijo del archivo pertenece a JUNK_EXTENSIONS."""
    if not filename: return False
    return any(filename.lower().endswith(ext) for ext in JUNK_EXTENSIONS)

def _get_win_attributes(entry: os.DirEntry) -> int:
    """
    Extrae atributos de archivo (bitmask) usando stat de bajo nivel.
    
    Args:
        entry: Objeto DirEntry del cual extraer atributos (sin seguir enlaces).
    Returns:
        Entero representando la máscara de bits de atributos (Windows).
    """
    try:
        if hasattr(os, 'stat_result') and hasattr(os.stat_result, 'st_file_attributes'):
            return entry.stat(follow_symlinks=False).st_file_attributes
        return 0
    except (OSError, AttributeError, ValueError):
        return 0

def _is_junction(entry: os.DirEntry) -> bool:
    """
    Determina si una entrada es un punto de reparse (Junction) o enlace simbólico.
    
    Uso: Previene la recursión infinita y la salida del ámbito de escaneo seguro.
    """
    return entry.is_symlink() or bool(_get_win_attributes(entry) & WIN_ATTR_JUNCTION)

def _is_unc_path(path: Path) -> bool:
    """
    Detecta rutas de red (Universal Naming Convention).
    
    Uso: Las rutas UNC son inestables para operaciones de archivo masivas;
    siempre se marcan como inseguras para evitar latencias o fallos de red.
    """
    if path is None: return True
    try:
        return str(path.absolute()).startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """
    Resuelve colisiones de nombres añadiendo sufijos numéricos (1-999).
    
    Garantiza atomicidad: evita sobrescribir archivos existentes en el 
    directorio de cuarentena.
    """
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Verifica que el nombre de la carpeta no esté en el listado de exclusión del sistema."""
    if not name: return False
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _is_file_locked(path: Path) -> bool:
    """
    Intenta abrir un archivo en modo lectura exclusiva para verificar uso.
    
    Retorna True si el archivo está bloqueado (en uso), False si es accesible.
    """
    if path is None or not path.is_file():
        return True
    try:
        fd = os.open(path, os.O_RDONLY)
        os.close(fd)
        return False
    except (PermissionError, OSError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Verifica que el destino no sea un subdirectorio del origen (bucle cíclico).
    
    Previene errores catastróficos donde el origen podría quedar dentro de su 
    propio destino, corrompiendo el sistema de archivos.
    """
    if src is None or dest is None: return True
    try:
        if src.exists() and dest.exists() and os.path.samefile(src, dest):
            return True
        s, d = str(src.resolve()), str(dest.resolve())
        return os.path.commonpath([s, d]) == s
    except (OSError, ValueError):
        return True

def _has_forbidden_chars(path: Path) -> bool:
    """Valida la ausencia de caracteres reservados que invalidarían la ruta en NTFS."""
    if path is None: return True
    path_str = str(path).lower()
    return any(c in path_str for c in ["<", ">", "|", "\0"])

def _validate_path_security(src: Path, dest: Path) -> bool:
    """
    Validación de seguridad compuesta sobre rutas de origen y destino.
    
    Comprueba: UNC, caracteres prohibidos, límites de longitud (260) y bloqueo de sistema.
    """
    if src is None or dest is None: return False
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > MAX_PATH_LENGTH or len(str(dest)) > MAX_PATH_LENGTH: return False
    return not (is_protected_path(src) or is_protected_path(dest))

def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Auditoría exhaustiva previa a escritura/movimiento.
    
    Verifica: Permisos de escritura, espacio disponible, unicidad de enlaces (st_nlink==1) 
    y bloqueo de archivo para evitar corrupción de datos durante el movimiento.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    try:
        if src.exists() and dest.exists() and src.samefile(dest): return False
        st = src.lstat()
        # Rechaza enlaces simbólicos (st_mode máscara 0o170000 -> 0o120000)
        if not src.is_file() or (st.st_mode & 0o170000 == 0o120000): return False
        if not is_safe_to_modify(src) or not _validate_path_security(src, dest): return False
        
        target_dir = dest.parent if dest.exists() else dest
        if not target_dir.is_dir() or not os.access(target_dir, os.W_OK): return False
        
        usage = shutil.disk_usage(target_dir.anchor)
        if usage.free < (src.stat().st_size + MIN_FREE_SPACE_BYTES): return False
        
        # Validar que no estemos moviendo dentro de la misma unidad o en bucle
        if src.drive != target_dir.drive or _is_recursive_violation(src, dest): return False
        if not os.access(src, os.R_OK) or _is_file_locked(src): return False
        
        return st.st_nlink == 1
    except (OSError, RuntimeError, AttributeError, ValueError):
        return False

def _should_scan_directory(entry: os.DirEntry, protected_cache: set[str]) -> bool:
    """Filtra directorios aptos para escaneo (excluye junctions y rutas protegidas)."""
    if entry is None or not _is_allowed_directory(entry.name) or _is_junction(entry): return False
    path_str = entry.path
    if path_str in protected_cache: return False
    if is_protected_path(Path(path_str)):
        protected_cache.add(path_str)
        return False
    return True

def _is_valid_junk_entry(entry: os.DirEntry, stats: os.stat_result, now_ts: float) -> bool:
    """Valida si un archivo cumple criterios temporales, tamaño y extensión."""
    if entry is None or stats is None: return False
    _, ext = os.path.splitext(entry.name)
    return (0 <= stats.st_size < MAX_FILE_SIZE_BYTES and 
            stats.st_mtime <= now_ts + 3600 and
            not (_get_win_attributes(entry) & WIN_ATTR_MASK) and
            ext.lower() in JUNK_EXTENSIONS)

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int, protected_cache: set[str], visited: set[Path]) -> None:
    """Recorrido recursivo limitado para encontrar archivos basura."""
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
                        if _is_valid_junk_entry(item, stats, now_ts):
                            found.append(JunkFile(Path(item.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime)))
                except (PermissionError, OSError): continue
    except (PermissionError, OSError, RuntimeError): pass

def scan_for_junk(directories: Optional[Sequence[str | Path]] = None) -> List[JunkFile]:
    """Inicia la detección de archivos basura en los directorios configurados."""
    found: List[JunkFile] = []
    protected_cache: set[str] = set()
    visited: set[Path] = set()
    scan_list: Sequence[str | Path] = directories or DEFAULT_SCAN_DIRS
    for d in scan_list:
        if not d: continue
        p = Path(d).expanduser()
        if p.exists() and p.is_dir() and not _is_unc_path(p) and is_safe_to_modify(p):
            _process_directory(p, found, 0, protected_cache, visited)
    return found

def sort_junk(files: Sequence[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena el listado de archivos basura."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """Valida y mueve los archivos candidatos a un directorio de cuarentena."""
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
        if not junk_file.path.exists() or not is_safe_to_modify(junk_file.path): continue
        if not _is_safe_for_disk_op(junk_file.path, dest_res): continue
        
        target_path = _can_move_file(junk_file, dest_res)
        if target_path:
            try:
                ensure_safe_to_modify(junk_file.path)
                shutil.move(str(junk_file.path), str(target_path))
            except (OSError, shutil.Error, PermissionError) as e:
                logger.error(f"Error moviendo {junk_file.path}: {e}")
    return dest_res

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Verifica disponibilidad de espacio y genera una ruta única."""
    if junk_file is None or dest_base is None: return None
    try:
        anchor = dest_base.anchor
        usage = shutil.disk_usage(anchor)
        if usage.free < (junk_file.size_bytes + MIN_FREE_SPACE_BYTES): return None
        safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
        return _generate_unique_target(dest_base / safe_name)
    except (OSError, AttributeError, ValueError): return None

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Ejecuta la eliminación permanente de archivos en el directorio de revisión."""
    try:
        dest = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not dest.is_dir(): return 0
        if is_protected_path(dest) or not is_safe_to_modify(dest): return 0
        
        count = 0
        for item in dest.iterdir():
            if item.is_file():
                try:
                    if is_safe_to_modify(item) and not is_protected_path(item):
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
