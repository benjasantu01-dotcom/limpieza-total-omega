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

# Constantes de atributos de Windows (Win32 API) utilizadas para filtrar archivos del sistema
WIN_ATTR_JUNCTION: Final[int] = 0x400  # Reparse Point (Junction)
WIN_ATTR_SYSTEM: Final[int] = 0x04     # FILE_ATTRIBUTE_SYSTEM
WIN_ATTR_HIDDEN: Final[int] = 0x02     # FILE_ATTRIBUTE_HIDDEN
WIN_ATTR_MASK: Final[int] = WIN_ATTR_SYSTEM | WIN_ATTR_HIDDEN

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
    Representa un archivo candidato a limpieza detectado en el sistema.
    
    Almacena metadatos esenciales para la toma de decisiones del usuario
    sobre la seguridad y utilidad de mover/eliminar el archivo.
    """
    path: Path
    size_bytes: int
    modified: datetime

    def __post_init__(self) -> None:
        try:
            self.path = self.path.resolve()
        except (OSError, RuntimeError):
            pass

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño del archivo en Megabytes para facilitar la lectura humana."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión del archivo es candidata según la heurística definida."""
        return is_valid_junk_extension(self.path.name)

def is_valid_junk_extension(filename: str) -> bool:
    """Verifica si el sufijo del archivo pertenece a la lista de JUNK_EXTENSIONS."""
    name_lower = filename.lower()
    return any(name_lower.endswith(ext) for ext in JUNK_EXTENSIONS)

def _get_win_attributes(entry: os.DirEntry) -> int:
    """
    Extrae los atributos de archivo nativos de Windows mediante la API `st_file_attributes`.
    Devuelve 0 si la llamada falla, evitando interrupciones en el flujo del escáner.
    """
    try:
        return entry.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        return 0

def _is_junction(entry: os.DirEntry) -> bool:
    """
    Detecta si una entrada es un 'Junction point' o enlace simbólico.
    Bloquear esto previene que el escáner entre en bucles recursivos o intente
    modificar archivos que existen fuera del árbol de directorios esperado.
    """
    return entry.is_symlink() or bool(_get_win_attributes(entry) & WIN_ATTR_JUNCTION)

def _is_unc_path(path: Path) -> bool:
    """
    Determina si la ruta es una ruta de red (UNC). Las rutas de red no son seguras
    de manipular mediante operaciones locales de I/O ya que escapan del control
    del sistema de archivos local.
    """
    try:
        return str(path.absolute()).startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """
    Gestiona colisiones de nombres de archivos en el directorio destino mediante
    la adición incremental de sufijos numéricos (1-999) para evitar sobrescrituras.
    """
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Valida que el nombre de un directorio no esté en la lista negra de protección del sistema."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _is_file_locked(path: Path) -> bool:
    """
    Verifica la accesibilidad del archivo intentando abrirlo en modo lectura binaria.
    Un error de permiso o acceso implica un bloqueo por otro proceso.
    """
    if not path.is_file():
        return True
    try:
        with open(path, 'rb') as f:
            return False
    except (PermissionError, OSError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Verifica que la ruta de destino no sea un subdirectorio del origen para 
    evitar el movimiento de archivos hacia sí mismos o ciclos infinitos.
    """
    try:
        s, d = str(src.resolve()), str(dest.resolve())
        return os.path.commonpath([s, d]) == s
    except (OSError, ValueError):
        return True

def _has_forbidden_chars(path: Path) -> bool:
    """Detecta caracteres que no son válidos en rutas de Windows, evitando errores en la manipulación."""
    path_str = str(path).lower()
    return any(c in path_str for c in ["<", ">", "|", "\0"])

def _validate_path_security(src: Path, dest: Path) -> bool:
    """
    Valida la integridad de las rutas involucradas: check de caracteres, 
    longitud máxima permitida (MAX_PATH) y protección de sistema (safety.py).
    """
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > 260 or len(str(dest)) > 260: return False
    return not (is_protected_path(src) or is_protected_path(dest))

def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Realiza una validación exhaustiva de pre-condiciones para mover archivos:
    verifica existencia, permisos de escritura, concurrencia (bloqueos), 
    y consistencia del sistema de archivos según `safety.py`.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    try:
        if not src.exists() or not src.is_file() or src.is_symlink(): return False
        if not is_safe_to_modify(src): return False
        if not _validate_path_security(src, dest): return False
        target_dir = dest.parent if dest.exists() else dest
        if not target_dir.is_dir() or not os.access(target_dir, os.W_OK): return False
        if is_protected_path(target_dir) or _is_unc_path(target_dir): return False
        if src.drive != target_dir.drive: return False
        if _is_recursive_violation(src, dest): return False
        if not os.access(src, os.R_OK): return False
        stats = src.stat()
        if not (0 <= stats.st_size < 100_000_000_000): return False
        return not _is_file_locked(src)
    except (OSError, RuntimeError, AttributeError):
        return False

def _should_scan_directory(entry: os.DirEntry, protected_cache: set[str]) -> bool:
    """Filtra directorios para el escaneo usando caché para no repetir verificaciones de seguridad."""
    if not _is_allowed_directory(entry.name) or _is_junction(entry): return False
    if entry.path in protected_cache: return False
    if is_protected_path(Path(entry.path)):
        protected_cache.add(entry.path)
        return False
    return True

def _is_valid_junk_entry(entry: os.DirEntry, stats: os.stat_result) -> bool:
    """Verifica si un archivo es candidato a limpieza basándose en atributos, extensión y tamaño."""
    return (0 <= stats.st_size < 100_000_000_000 and 
            not (_get_win_attributes(entry) & WIN_ATTR_MASK) and
            is_valid_junk_extension(entry.name))

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int, protected_cache: set[str], visited: set[Path]) -> None:
    """
    Recorre directorios de forma recursiva (máx 50 niveles).
    Utiliza `visited` como conjunto de control para prevenir ciclos de archivos.
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
                        if _is_valid_junk_entry(item, stats):
                            found.append(JunkFile(Path(item.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime)))
                except (OSError, PermissionError): continue
    except (OSError, PermissionError, RuntimeError): pass

def scan_for_junk(directories: Optional[Sequence[str | Path]] = None) -> List[JunkFile]:
    """Inicia el proceso de detección de archivos basura en las rutas configuradas."""
    found: List[JunkFile] = []
    protected_cache: set[str] = set()
    visited: set[Path] = set()
    scan_list: Sequence[str | Path] = directories or DEFAULT_SCAN_DIRS
    for d in scan_list:
        try:
            p = Path(d).expanduser()
            if p.exists() and p.is_dir() and not _is_unc_path(p):
                _process_directory(p, found, 0, protected_cache, visited)
        except (OSError, RuntimeError): continue
    return found

def sort_junk(files: Sequence[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena el listado de archivos basura basándose en los criterios registrados en SORT_REGISTRY."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Mueve los archivos candidatos a un directorio de revisión.
    Utiliza `ensure_safe_to_modify` para cumplir con las reglas de seguridad antes de cada movimiento.
    """
    if not files: return None
    try:
        dest_base = Path(review_dir).expanduser()
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        dest_res = dest_base.resolve()
        ensure_safe_to_modify(dest_res)
    except (OSError, RuntimeError, PermissionError): return None
    
    for junk_file in files:
        if not is_safe_to_modify(junk_file.path): continue
        if not _is_safe_for_disk_op(junk_file.path, dest_res): continue
        target_path = _can_move_file(junk_file, dest_res)
        if target_path:
            try:
                ensure_safe_to_modify(junk_file.path)
                shutil.move(str(junk_file.path), str(target_path))
            except (OSError, shutil.Error, PermissionError): continue
    return dest_res

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """
    Valida la disponibilidad de espacio en disco en la unidad de destino y 
    genera una ruta única para la operación de movimiento.
    """
    try:
        usage = shutil.disk_usage(dest_base.anchor)
        # Margen de seguridad de 50MB
        if usage.free < (junk_file.size_bytes + 52428800): return None
    except (OSError, FileNotFoundError, AttributeError): return None
    safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
    return _generate_unique_target(dest_base / safe_name)

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Ejecuta la eliminación permanente de archivos confirmados tras la revisión.
    Aplica `ensure_safe_to_modify` para garantizar que la operación respeta las políticas de seguridad.
    """
    try:
        dest = Path(review_dir).expanduser().resolve()
        if not dest.is_dir(): return 0
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
