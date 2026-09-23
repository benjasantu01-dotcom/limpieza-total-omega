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
    """Extrae la máscara de bits de atributos Win32 (System/Hidden/Junction) de una ruta."""
    try:
        if isinstance(path_or_entry, os.DirEntry):
            return path_or_entry.stat(follow_symlinks=False).st_file_attributes
        return Path(path_or_entry).stat().st_file_attributes
    except (OSError, AttributeError, ValueError):
        return 0

def _is_junction(entry: Union[os.DirEntry, Path]) -> bool:
    """Determina si la ruta es un punto de reparse (Junction/Symlink) para evitar recursión circular."""
    if isinstance(entry, os.DirEntry):
        return entry.is_symlink() or bool(_get_win_attributes(entry) & WIN_ATTR_JUNCTION)
    return Path(entry).is_symlink() or bool(_get_win_attributes(entry) & WIN_ATTR_JUNCTION)

def _is_unc_path(path: Path) -> bool:
    """Valida si la ruta es una ruta de red (UNC) que debe evitarse por latencia/bloqueos."""
    try:
        return str(path.absolute()).startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """Genera una ruta única para archivos en cuarentena añadiendo un prefijo numérico ante colisiones."""
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Compara el nombre de carpeta contra una lista de directorios prohibidos por seguridad."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _is_file_locked(path: Path) -> bool:
    """Verifica si el archivo está siendo usado por otro proceso intentando abrirlo en modo lectura."""
    try:
        return not os.access(path, os.R_OK)
    except (OSError, PermissionError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """Previene que una ruta destino sea un subdirectorio del origen para evitar pérdida de datos."""
    try:
        s, d = src.resolve(), dest.resolve()
        return s == d or d.is_relative_to(s)
    except (OSError, ValueError):
        return True

def _passes_system_checks(src: Path) -> bool:
    """Valida que el archivo no posea atributos de sistema o sea un archivo oculto del SO."""
    if os.name != "nt": return True
    return not (_get_win_attributes(src) & WIN_ATTR_MASK)

def _has_forbidden_chars(path: Path) -> bool:
    """Detecta caracteres nulos o caracteres de redirección ilegales en rutas de archivos."""
    path_str = str(path).lower()
    return any(c in path_str for c in ["<", ">", "|", "\0"])

def _validate_path_security(src: Path, dest: Path) -> bool:
    """Audita rutas origen y destino frente a bloqueos, rutas UNC y longitud MAX_PATH (260)."""
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > 260 or len(str(dest)) > 260: return False
    return not (is_protected_path(src) or is_protected_path(dest))

def _validate_file_attributes(src: Path) -> bool:
    """
    Realiza una auditoría profunda de atributos para verificar si un archivo es apto 
    para ser movido. Filtra archivos bloqueados, puntos de reparse, archivos vacíos 
    o de tamaño extremo (>100GB).
    """
    try:
        stats = src.stat()
        if not src.is_file() or _is_junction(src) or stats.st_size == 0 or stats.st_size > 100_000_000_000: 
            return False
        return _passes_system_checks(src) and not _is_file_locked(src)
    except (OSError, PermissionError):
        return False

def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Coordinador central de seguridad para operaciones de E/S.
    Verifica permisos del sistema, integridad de la jerarquía de rutas y 
    restricciones de lectura/escritura antes de autorizar cualquier movimiento.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path) or not src.exists(): 
        return False
    if not is_safe_to_modify(src): 
        return False
    if not _validate_path_security(src, dest): 
        return False
        
    try:
        target_parent = (dest.parent if not dest.exists() else dest.resolve().parent)
        if is_protected_path(target_parent) or _is_unc_path(target_parent) or src.drive != target_parent.drive: return False
        if _is_recursive_violation(src, dest): return False
        if not os.access(target_parent, os.W_OK) or not os.access(src, os.W_OK): return False
        return _validate_file_attributes(src)
    except (OSError, RuntimeError, AttributeError):
        return False

def _is_safe_to_move(junk_file: Optional[JunkFile], dest: Path) -> bool:
    """Valida la integridad del objeto JunkFile y la seguridad de la ruta destino para el movimiento."""
    if junk_file is None or junk_file.path is None: return False
    return junk_file.path.exists() and _is_safe_for_disk_op(junk_file.path, dest)

def _should_scan_directory(entry: os.DirEntry, protected_cache: set[str]) -> bool:
    """Filtra directorios no permitidos o protegidos durante la recursión del escáner."""
    if not _is_allowed_directory(entry.name) or _is_junction(entry): return False
    path_str = entry.path
    if path_str in protected_cache: return False
    if is_protected_path(Path(path_str)):
        protected_cache.add(path_str)
        return False
    return True

def _is_size_within_limits(size: int) -> bool:
    """Asegura que el tamaño del archivo esté en un rango operativo saludable (0 - 100GB)."""
    return 0 < size < 100_000_000_000

def _is_valid_junk_file(entry: os.DirEntry) -> bool:
    """
    Aplica filtros iniciales de metadatos (tamaño, atributos, extensión) para determinar 
    si una entrada de directorio es un candidato válido para el escaneo de basura.
    """
    try:
        stats = entry.stat(follow_symlinks=False)
        return (_is_size_within_limits(stats.st_size) and 
                not (_get_win_attributes(entry) & WIN_ATTR_MASK) and
                is_valid_junk_extension(entry.name))
    except (OSError, PermissionError):
        return False

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int, protected_cache: set[str], visited: set[Path]) -> None:
    """Realiza un recorrido recursivo del sistema de archivos con límite de profundidad 50."""
    if depth > 50: return
    try:
        resolved_dir = current_dir.resolve()
        if resolved_dir in visited: return
        visited.add(resolved_dir)
        
        with os.scandir(current_dir) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry, protected_cache):
                            _process_directory(Path(entry.path), found, depth + 1, protected_cache, visited)
                    elif entry.is_file(follow_symlinks=False) and _is_valid_junk_file(entry):
                        _evaluate_entry(entry, found)
                except (OSError, PermissionError): continue
    except (OSError, PermissionError, RuntimeError): pass

def scan_for_junk(directories: Optional[Sequence[str | Path]] = None) -> List[JunkFile]:
    """Escanea directorios en busca de basura, normalizando rutas y gestionando el estado de escaneo."""
    found: List[JunkFile] = []
    protected_cache: set[str] = set()
    visited: set[Path] = set()
    scan_list: Sequence[str | Path] = directories or DEFAULT_SCAN_DIRS
    for d in scan_list:
        p = Path(d).expanduser()
        if p.exists() and p.is_dir() and not _is_unc_path(p):
            _process_directory(p, found, 0, protected_cache, visited)
    return found

def _evaluate_entry(entry: os.DirEntry, found: List[JunkFile]) -> None:
    """
    Transforma un `os.DirEntry` válido en un objeto `JunkFile`. 
    Captura metadatos de sistema como tamaño y fecha de última modificación.
    """
    try:
        stats = entry.stat(follow_symlinks=False)
        found.append(JunkFile(Path(entry.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime)))
    except (OSError, PermissionError, ValueError):
        pass

def sort_junk(files: Sequence[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena los archivos encontrados mediante el registro de criterios configurados."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """Mueve archivos validados a cuarentena tras asegurar permisos de escritura y seguridad de destino."""
    if not files: return None
    try:
        dest_base = Path(review_dir).expanduser().resolve()
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        if _is_junction(dest_base) or not is_safe_to_modify(dest_base): return None
    except (OSError, RuntimeError, PermissionError): return None
    
    for junk_file in files:
        try:
            target_path = _can_move_file(junk_file, dest_base)
            if target_path and is_safe_to_modify(junk_file.path):
                ensure_safe_to_modify(junk_file.path)
                shutil.move(str(junk_file.path), str(target_path))
        except (OSError, shutil.Error, PermissionError): continue
    return dest_base

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Valida disponibilidad de espacio en disco y gestiona colisiones de nombres para el movimiento."""
    if not _is_safe_to_move(junk_file, dest_base): return None
    try:
        usage = shutil.disk_usage(dest_base.anchor)
        if usage.free < (junk_file.size_bytes + 52428800): return None
    except (OSError, FileNotFoundError, AttributeError): return None
    
    # Asegurar que el nombre del archivo sea limpio y prevenir escape de directorio
    safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
    candidate = dest_base / safe_name
    return _generate_unique_target(candidate)

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Elimina permanentemente los archivos en cuarentena tras realizar chequeos de seguridad obligatorios."""
    try:
        dest = Path(review_dir).expanduser().resolve()
        if not dest.is_dir() or not is_safe_to_modify(dest) or _is_junction(dest): return 0
        
        count = 0
        for item in dest.iterdir():
            if item.is_file():
                if is_safe_to_modify(item):
                    ensure_safe_to_modify(item)
                    item.unlink()
                    count += 1
        return count
    except (OSError, PermissionError, RuntimeError): return 0
