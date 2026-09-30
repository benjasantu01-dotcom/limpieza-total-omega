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
    name_lower = filename.lower()
    return any(name_lower.endswith(ext) for ext in JUNK_EXTENSIONS)

def _get_win_attributes(entry: os.DirEntry) -> int:
    """Extrae atributos de archivo (Windows API) sin lanzar excepciones."""
    try:
        if hasattr(os, 'stat_result') and hasattr(os.stat_result, 'st_file_attributes'):
            return entry.stat(follow_symlinks=False).st_file_attributes
        return 0
    except (OSError, AttributeError, ValueError):
        return 0

def _is_junction(entry: os.DirEntry) -> bool:
    """Detecta enlaces simbólicos o junctions para prevenir recursión infinita."""
    return entry.is_symlink() or bool(_get_win_attributes(entry) & WIN_ATTR_JUNCTION)

def _is_unc_path(path: Path) -> bool:
    """Valida si la ruta es una ruta de red (UNC), desaconsejada para operaciones locales."""
    try:
        return str(path.absolute()).startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """
    Resuelve colisiones de nombres añadiendo sufijos numéricos (1-999).
    Si 'archivo.tmp' existe, intenta 'archivo_1.tmp', 'archivo_2.tmp', etc.
    """
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Verifica que el nombre de la carpeta no esté en el listado de exclusión del sistema."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _is_file_locked(path: Path) -> bool:
    """
    Determina si un archivo está bloqueado por otro proceso.
    
    Usa apertura en modo 'rb' para testear accesibilidad.
    Devuelve True si el archivo es inalcanzable, está bloqueado o es un directorio.
    """
    if not path.is_file():
        return True
    try:
        with open(path, 'rb') as f:
            f.peek(1)
            return False
    except (PermissionError, OSError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Previene movimientos cíclicos donde el destino contenga al origen.
    Verifica si el path del destino es un sub-directorio del origen.
    """
    try:
        s, d = str(src.resolve()), str(dest.resolve())
        return os.path.commonpath([s, d]) == s
    except (OSError, ValueError):
        return True

def _has_forbidden_chars(path: Path) -> bool:
    """Verifica la ausencia de caracteres reservados en el sistema de archivos Windows."""
    path_str = str(path).lower()
    return any(c in path_str for c in ["<", ">", "|", "\0"])

def _validate_path_security(src: Path, dest: Path) -> bool:
    """
    Valida requisitos técnicos de seguridad sobre rutas antes de operar.
    Verifica: formato UNC, caracteres prohibidos, longitud máxima y protección de sistema.
    """
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > MAX_PATH_LENGTH or len(str(dest)) > MAX_PATH_LENGTH: return False
    return not (is_protected_path(src) or is_protected_path(dest))

def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Auditoría completa de seguridad antes de cualquier movimiento de archivos.
    
    Realiza validaciones de integridad, pertenencia a zonas protegidas, 
    accesibilidad del SO, evita cruce de unidades y comprueba bloqueos activos.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    try:
        if not src.exists() or not src.is_file() or src.is_symlink(): return False
        if not is_safe_to_modify(src) or not _validate_path_security(src, dest): return False
        
        target_dir = dest.parent if dest.exists() else dest
        if is_protected_path(target_dir) or not target_dir.is_dir() or not os.access(target_dir, os.W_OK): 
            return False
        
        if _is_unc_path(target_dir) or src.drive != target_dir.drive: return False
        if _is_recursive_violation(src, dest) or not os.access(src, os.R_OK): return False
        
        stats = src.stat()
        if stats.st_nlink > 1: return False
        if not (0 <= stats.st_size < MAX_FILE_SIZE_BYTES): return False
        if stats.st_mtime > datetime.now().timestamp() + 3600: return False
        
        return not _is_file_locked(src)
    except (OSError, RuntimeError, AttributeError, ValueError):
        return False

def _should_scan_directory(entry: os.DirEntry, protected_cache: set[str]) -> bool:
    """
    Filtra directorios aptos para escaneo recursivo.
    
    Utiliza un caché de rutas protegidas para optimizar la recursión y 
    evitar el re-procesamiento de subárboles bloqueados.
    """
    if not _is_allowed_directory(entry.name) or _is_junction(entry): return False
    if entry.path in protected_cache: return False
    if is_protected_path(Path(entry.path)):
        protected_cache.add(entry.path)
        return False
    return True

def _is_valid_junk_entry(entry: os.DirEntry, stats: os.stat_result) -> bool:
    """Valida si un archivo candidato cumple los criterios de tamaño, fecha y extensión."""
    return (0 <= stats.st_size < MAX_FILE_SIZE_BYTES and 
            stats.st_mtime <= datetime.now().timestamp() + 3600 and
            not (_get_win_attributes(entry) & WIN_ATTR_MASK) and
            is_valid_junk_extension(entry.name))

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int, protected_cache: set[str], visited: set[Path]) -> None:
    """
    Recorrido recursivo del sistema de archivos limitado a 50 niveles de profundidad.
    
    Maneja excepciones de permiso y errores de sistema por entrada de directorio.
    """
    if depth > 50 or not current_dir.exists(): return
    try:
        resolved_dir = current_dir.resolve()
        if resolved_dir in visited: return
        visited.add(resolved_dir)
        
        with os.scandir(current_dir) as iterator:
            for item in iterator:
                try:
                    if item.is_dir(follow_symlinks=False):
                        if _should_scan_directory(item, protected_cache):
                            _process_directory(Path(item.path), found, depth + 1, protected_cache, visited)
                    elif item.is_file(follow_symlinks=False):
                        stats = item.stat(follow_symlinks=False)
                        if _is_valid_junk_entry(item, stats) and os.access(item.path, os.R_OK):
                            found.append(JunkFile(Path(item.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime)))
                except (OSError, PermissionError, ValueError): continue
    except (OSError, PermissionError, RuntimeError, ValueError): pass

def scan_for_junk(directories: Optional[Sequence[str | Path]] = None) -> List[JunkFile]:
    """Inicia la detección de archivos basura en los directorios configurados."""
    found: List[JunkFile] = []
    protected_cache: set[str] = set()
    visited: set[Path] = set()
    scan_list: Sequence[str | Path] = directories or DEFAULT_SCAN_DIRS
    for d in scan_list:
        try:
            if not d: continue
            p = Path(d).expanduser()
            if p.exists() and p.is_dir() and not _is_unc_path(p) and is_safe_to_modify(p) and os.access(p, os.R_OK):
                _process_directory(p, found, 0, protected_cache, visited)
        except (OSError, RuntimeError, ValueError): continue
    return found

def sort_junk(files: Sequence[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena el listado de archivos basura según el criterio definido en el registro."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Valida y mueve los archivos candidatos a un directorio de cuarentena temporal.
    
    Asegura que cada operación pase por el chequeo de seguridad `ensure_safe_to_modify`.
    """
    if not files: return None
    try:
        dest_base = Path(review_dir).expanduser()
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        dest_res = dest_base.resolve()
        ensure_safe_to_modify(dest_res)
    except (OSError, RuntimeError, PermissionError): return None
    
    for junk_file in files:
        if not junk_file or not junk_file.path.exists(): continue
        if not is_safe_to_modify(junk_file.path): continue
        if not _is_safe_for_disk_op(junk_file.path, dest_res): continue
        target_path = _can_move_file(junk_file, dest_res)
        if target_path:
            try:
                ensure_safe_to_modify(junk_file.path)
                shutil.move(str(junk_file.path), str(target_path))
            except (OSError, shutil.Error, PermissionError) as e:
                logger.error(f"Error moviendo {junk_file.path}: {e}")
                continue
    return dest_res

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Verifica disponibilidad de espacio en la unidad destino y genera una ruta única."""
    try:
        anchor = dest_base.anchor
        if not anchor: return None
        usage = shutil.disk_usage(anchor)
        if usage.free < (junk_file.size_bytes + MIN_FREE_SPACE_BYTES): return None
    except (OSError, AttributeError, ValueError): return None
    safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
    return _generate_unique_target(dest_base / safe_name)

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Ejecuta la eliminación permanente de archivos en el directorio de revisión.
    
    Requiere que cada archivo pase por el filtro de seguridad `ensure_safe_to_modify`.
    """
    try:
        dest = Path(review_dir).expanduser().resolve()
        if not dest.is_dir() or not is_safe_to_modify(dest): return 0
        count = 0
        for item in dest.iterdir():
            if item.is_file():
                try:
                    ensure_safe_to_modify(item)
                    item.unlink()
                    count += 1
                except (OSError, PermissionError): continue
        return count
    except (OSError, PermissionError, RuntimeError): return 0
