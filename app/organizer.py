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

# Constantes de atributos de Windows (Win32 API)
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
    """Detecta unidades montadas en Windows mediante el barrido de letras de unidad."""
    if os.name != "nt":
        return []
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive: str = f"{letter}:\\"
        if os.path.exists(drive):
            drives.append(drive)
    return drives

@dataclass
class JunkFile:
    """Representa un archivo candidato a limpieza detectado en el sistema."""
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
        """Calcula y retorna el tamaño del archivo convertido a Megabytes."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión del archivo está presente en JUNK_EXTENSIONS."""
        return is_valid_junk_extension(self.path.name)

def is_valid_junk_extension(filename: str) -> bool:
    """Comprueba si el sufijo de un nombre de archivo coincide con una extensión basura."""
    return os.path.splitext(filename)[1].lower() in JUNK_EXTENSIONS

def _get_win_attributes(path_or_entry: Union[os.DirEntry, Path, str]) -> int:
    """Extrae los bits de atributos del sistema de archivos usando la API de Windows."""
    try:
        if isinstance(path_or_entry, os.DirEntry):
            return path_or_entry.stat(follow_symlinks=False).st_file_attributes
        return Path(path_or_entry).stat().st_file_attributes
    except (OSError, AttributeError, ValueError):
        return 0

def _is_junction(entry: Union[os.DirEntry, Path]) -> bool:
    """Verifica si la ruta es un punto de reparse (junction o symlink) que no debe seguirse."""
    if isinstance(entry, os.DirEntry):
        return entry.is_symlink() or bool(_get_win_attributes(entry) & WIN_ATTR_JUNCTION)
    return Path(entry).is_symlink() or bool(_get_win_attributes(entry) & WIN_ATTR_JUNCTION)

def _is_unc_path(path: Path) -> bool:
    """Determina si la ruta es una ruta de red (UNC) para evitar operaciones de I/O bloqueantes."""
    try:
        return str(path.absolute()).startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """Resuelve colisiones de nombres añadiendo un índice numérico al nombre del archivo."""
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Valida si el directorio no es un componente crítico del sistema según la blocklist."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _is_file_locked(path: Path) -> bool:
    """
    Comprueba si el archivo está en uso verificando el acceso de lectura (R_OK).
    Devuelve True si el archivo no puede leerse, asumiendo que está bloqueado.
    """
    try:
        return not os.access(path, os.R_OK)
    except (OSError, PermissionError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Verifica si el destino está contenido dentro del origen para evitar 
    que el proceso de limpieza intente borrar su propio directorio de trabajo.
    """
    try:
        s, d = src.resolve(), dest.resolve()
        return s == d or d.is_relative_to(s)
    except (OSError, ValueError):
        return True

def _passes_system_checks(src: Path) -> bool:
    """Verifica si el archivo tiene atributos de sistema u oculto que impidan su limpieza."""
    if os.name != "nt": return True
    return not (_get_win_attributes(src) & WIN_ATTR_MASK)

def _has_forbidden_chars(path: Path) -> bool:
    """Filtra rutas que contienen caracteres prohibidos por el sistema operativo."""
    path_str = str(path).lower()
    return any(c in path_str for c in ["<", ">", "|", "\0"])

def _validate_path_security(src: Path, dest: Path) -> bool:
    """
    Auditoría previa al movimiento: verifica rutas UNC, límites de longitud (MAX_PATH)
    y consulta el filtro de seguridad global de la aplicación.
    """
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > 260 or len(str(dest)) > 260: return False
    try:
        return not (is_protected_path(src.resolve()) or is_protected_path(dest.resolve()))
    except (OSError, RuntimeError):
        return False

def _validate_file_attributes(src: Path) -> bool:
    """
    Valida si el archivo es candidato a procesamiento: verifica que sea un archivo, 
    no sea un junction, tenga tamaño válido (evita bloqueos de IO) y no esté bloqueado.
    """
    try:
        st = src.stat()
        if not src.is_file() or _is_junction(src) or st.st_size == 0 or st.st_size > 100_000_000_000: return False
        return _passes_system_checks(src) and not _is_file_locked(src)
    except (OSError, PermissionError):
        return False

def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Validador booleano centralizado para asegurar la integridad de I/O.
    Verifica seguridad de rutas, permisos y atomicidad de unidad física.
    """
    if not _validate_path_security(src, dest): 
        return False
    if not is_safe_to_modify(src): 
        return False
        
    try:
        s_res = src.resolve()
        # Verificamos existencia real post-resolución
        if not s_res.exists(): return False
        
        parent = (dest.parent if not dest.exists() else dest.resolve().parent)
        
        if _is_recursive_violation(s_res, dest): 
            return False
        if not os.access(parent, os.W_OK): 
            return False
            
        return s_res.drive == parent.drive and _validate_file_attributes(s_res)
    except (OSError, RuntimeError, AttributeError):
        return False

def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """Wrapper de seguridad que confirma la existencia del origen antes de validar el movimiento."""
    return junk_file.path.exists() and _is_safe_for_disk_op(junk_file.path, dest)

def _should_scan_directory(entry: os.DirEntry, protected_cache: set[str]) -> bool:
    """
    Decide si un directorio debe ser explorado. Usa caché para evitar consultas redundantes 
    a `is_protected_path` y omite puntos de reparse.
    """
    if not _is_allowed_directory(entry.name) or _is_junction(entry): return False
    if entry.path in protected_cache: return False
    # Verificación en línea optimizada
    if is_protected_path(Path(entry.path)):
        protected_cache.add(entry.path)
        return False
    return True

def _is_valid_junk_file(entry: os.DirEntry) -> bool:
    """Determina si una entrada es un candidato legítimo basándose en tamaño y extensión."""
    try:
        st = entry.stat(follow_symlinks=False)
        return (0 < st.st_size < 100_000_000_000 and 
                not (_get_win_attributes(entry) & WIN_ATTR_MASK) and
                is_valid_junk_extension(entry.name))
    except (OSError, PermissionError):
        return False

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int, protected_cache: set[str]) -> None:
    """
    Recorrido recursivo del sistema de archivos limitado a 50 niveles para prevenir desbordamiento.
    """
    if depth > 50: return
    try:
        with os.scandir(current_dir) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry, protected_cache):
                            _process_directory(Path(entry.path), found, depth + 1, protected_cache)
                    elif entry.is_file(follow_symlinks=False) and _is_valid_junk_file(entry):
                        _evaluate_entry(entry, found)
                except (OSError, PermissionError): continue
    except (OSError, PermissionError, RuntimeError): pass

def scan_for_junk(directories: Optional[Sequence[str]] = None) -> List[JunkFile]:
    """Inicia el escaneo de directorios configurados y retorna una lista de objetos JunkFile."""
    found: List[JunkFile] = []
    protected_cache: set[str] = set()
    for d in (directories or DEFAULT_SCAN_DIRS):
        p = Path(d).expanduser()
        if p.exists() and p.is_dir() and not _is_unc_path(p):
            # Resolvemos al inicio de cada raíz de escaneo una única vez
            _process_directory(p.resolve(), found, 0, protected_cache)
    return found

def _evaluate_entry(entry: os.DirEntry, found: List[JunkFile]) -> None:
    """Crea una instancia JunkFile capturando metadatos de acceso y tamaño."""
    try:
        stat_info = entry.stat(follow_symlinks=False)
        found.append(JunkFile(Path(entry.path), stat_info.st_size, datetime.fromtimestamp(stat_info.st_mtime)))
    except (OSError, PermissionError, ValueError):
        pass

def sort_junk(files: Sequence[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena los archivos encontrados utilizando el registro de configuraciones de ordenamiento."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Mueve los archivos candidatos a una carpeta de cuarentena tras asegurar la ruta destino.
    Usa `ensure_safe_to_modify` para cumplir con las políticas de integridad.
    """
    if not files: return None
    try:
        dest_base = Path(review_dir).expanduser().resolve()
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        ensure_safe_to_modify(dest_base)
    except (OSError, RuntimeError): return None
    
    for junk_file in files:
        try:
            target_path = _can_move_file(junk_file, dest_base)
            if target_path and is_safe_to_modify(junk_file.path):
                ensure_safe_to_modify(junk_file.path)
                shutil.move(str(junk_file.path), str(target_path))
        except (OSError, shutil.Error): continue
    return dest_base

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Calcula disponibilidad de espacio y resuelve colisiones de nombre antes de mover."""
    if not _is_safe_to_move(junk_file, dest_base): return None
    try:
        anchor = dest_base.anchor
        usage = shutil.disk_usage(anchor)
        if usage.free < (junk_file.size_bytes + 52428800): return None
    except (OSError, FileNotFoundError, AttributeError): return None
    safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
    return _generate_unique_target(dest_base / safe_name)

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Borra permanentemente los archivos en cuarentena tras validar que es seguro hacerlo."""
    try:
        dest = Path(review_dir).expanduser().resolve()
        if not dest.is_dir() or not is_safe_to_modify(dest): return 0
        
        count = 0
        for item in dest.iterdir():
            if item.is_file():
                if is_safe_to_modify(item):
                    ensure_safe_to_modify(item)
                    item.unlink()
                    count += 1
        return count
    except (OSError, PermissionError): return 0
