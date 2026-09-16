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
    ext = os.path.splitext(filename)[1].lower()
    return ext in JUNK_EXTENSIONS

def _get_win_attributes(path_or_entry: Union[os.DirEntry, Path, str]) -> int:
    """
    Extrae los bits de atributos del sistema de archivos (Win32).
    Se usa para detectar archivos ocultos o de sistema antes de interactuar con ellos.
    """
    try:
        if hasattr(path_or_entry, 'stat'):
            return path_or_entry.stat(follow_symlinks=False).st_file_attributes
        return Path(path_or_entry).stat().st_file_attributes
    except (OSError, AttributeError, ValueError):
        return 0

def _is_junction(entry: Union[os.DirEntry, Path]) -> bool:
    """
    Detecta si una ruta es un punto de reparse (Junction/Symlink) para
    prevenir recursión infinita y evitar salir del directorio de trabajo.
    """
    if entry is None: return False
    is_sym = entry.is_symlink() if hasattr(entry, 'is_symlink') else Path(str(entry)).is_symlink()
    is_junction_attr = os.name == "nt" and bool(_get_win_attributes(entry) & WIN_ATTR_JUNCTION)
    return is_sym or is_junction_attr

def _is_unc_path(path: Path) -> bool:
    """Valida si una ruta reside en un recurso de red mediante UNC (ej. \\servidor\recurso)."""
    if not isinstance(path, Path): return True
    try:
        path_abs = path.absolute()
        return str(path_abs).startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """Genera una ruta única ante colisiones, añadiendo un sufijo numérico hasta 999."""
    if target is None: return target
    base_target, counter = target, 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Verifica si el nombre de carpeta no figura en la lista negra de seguridad."""
    return bool(name) and name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _is_file_locked(path: Path) -> bool:
    """
    Verifica si un archivo está inaccesible mediante os.access.
    Considera bloqueado cualquier archivo en Junction o protegido por el sistema.
    """
    if path is None: return True
    try:
        if not path.exists() or _is_junction(path): return True
        if not _passes_system_checks(path): return True
        return not os.access(path, os.R_OK)
    except (OSError, PermissionError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """Previene que la operación de movimiento anide recursivamente la fuente en el destino."""
    try:
        s, d = src.resolve(), dest.resolve()
        return s == d or (d.exists() and d.is_relative_to(s))
    except (OSError, ValueError):
        return True

def _passes_system_checks(src: Path) -> bool:
    """Filtra archivos marcados como 'Sistema' (0x04) u 'Oculto' (0x02) en Win32."""
    if os.name != "nt" or src is None: return True
    attrs = _get_win_attributes(src)
    # Retorna True solo si no posee bits de sistema ni de oculto activados
    return not (attrs & WIN_ATTR_MASK) if attrs != 0 else True

def _has_forbidden_chars(path: Path) -> bool:
    """Detecta nombres reservados por Windows o caracteres prohibidos en el sistema de archivos."""
    if path is None: return True
    path_str = str(path).lower()
    reserved = ["con", "prn", "aux", "nul", "com1", "lpt1"]
    return any(path_str.startswith(r) for r in reserved) or any(c in str(path) for c in ["<", ">", "|", "\0"])

def _validate_path_security(src: Path, dest: Path) -> bool:
    """Realiza una auditoría de seguridad integral validando longitudes y rutas protegidas."""
    if src is None or dest is None: return False
    if _is_unc_path(src) or _is_unc_path(dest): return False
    if _has_forbidden_chars(src): return False
    if len(str(src)) > 260 or len(str(dest)) > 260: return False
    try:
        return not (is_protected_path(src.resolve()) or is_protected_path(dest.resolve()))
    except (OSError, RuntimeError):
        return False

def _validate_file_attributes(src: Path) -> bool:
    """Verifica integridad: existencia, flags de sistema y que el archivo no sea vacío."""
    try:
        if src is None or not src.is_file(): return False
        if _is_junction(src) or src.is_symlink(): return False
        if not _passes_system_checks(src) or src.stat().st_size == 0: return False
        return not _is_file_locked(src)
    except (OSError, PermissionError):
        return False

def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Validación centralizada que garantiza que la operación sea segura.
    
    Args:
        src: Ruta del archivo origen.
        dest: Ruta del destino o carpeta destino.
        
    Returns:
        True si la operación cumple todas las reglas de seguridad y restricciones de disco.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    if not _validate_path_security(src, dest): return False
    try:
        s_res = src.resolve()
        if not s_res.exists() or _is_recursive_violation(s_res, dest): return False
        
        parent = dest.parent if dest.is_file() else dest
        if not parent.exists(): return False
        if s_res.drive != parent.resolve().drive: return False
        
        return _validate_file_attributes(s_res)
    except (OSError, RuntimeError, AttributeError):
        return False

def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """Validador tipado para confirmar disponibilidad y seguridad del JunkFile antes de mover."""
    if not isinstance(junk_file, JunkFile) or dest is None: return False
    return junk_file.path is not None and junk_file.path.exists() and _is_safe_for_disk_op(junk_file.path, dest)

def _should_scan_directory(entry: os.DirEntry, protected_cache: set[str]) -> bool:
    """Determina si un directorio es elegible para escaneo evitando bloqueados y junctions."""
    if entry is None or not _is_allowed_directory(entry.name) or _is_junction(entry):
        return False
    return entry.path not in protected_cache and not is_protected_path(Path(entry.path))

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int = 0, protected_cache: Optional[set[str]] = None) -> None:
    """
    Realiza recorrido recursivo para detectar basura, limitando la profundidad a 50 niveles.
    
    Args:
        current_dir: Directorio actual a escanear.
        found: Lista acumulativa de JunkFiles hallados.
        depth: Profundidad de recursión actual.
    """
    if protected_cache is None: protected_cache = set()
    if depth > 50 or is_protected_path(current_dir): return
    
    try:
        with os.scandir(current_dir) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry, protected_cache):
                            _process_directory(Path(entry.path), found, depth + 1, protected_cache)
                    elif entry.is_file(follow_symlinks=False):
                        if is_valid_junk_extension(entry.name):
                            _evaluate_entry(entry, found)
                except (OSError, PermissionError): continue
    except (OSError, PermissionError, RuntimeError): pass

def scan_for_junk(directories: Optional[Sequence[str]] = None) -> List[JunkFile]:
    """Escanea las rutas proporcionadas (o por defecto) buscando archivos temporales."""
    valid_dirs: List[Path] = [Path(d) for d in (directories or DEFAULT_SCAN_DIRS) if isinstance(d, (str, Path))]
    found: List[JunkFile] = []
    protected_cache: set[str] = set()
    for d in valid_dirs:
        try:
            p = d.expanduser()
            if p.exists() and p.is_dir() and not _is_unc_path(p):
                _process_directory(p.resolve(), found, 0, protected_cache)
        except (OSError, RuntimeError, TypeError): continue
    return found

def _evaluate_entry(entry: os.DirEntry, found: List[JunkFile]) -> None:
    """Evalúa metadata de entrada para validar si debe incluirse en los resultados."""
    try:
        stat_info = entry.stat(follow_symlinks=False)
        if stat_info.st_size > 0 and not (_get_win_attributes(entry) & WIN_ATTR_MASK):
            found.append(JunkFile(Path(entry.path), stat_info.st_size, datetime.fromtimestamp(stat_info.st_mtime)))
    except (OSError, PermissionError):
        pass

def sort_junk(files: Sequence[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena el listado de archivos basura usando los criterios definidos en SORT_REGISTRY."""
    if not isinstance(files, list): return []
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Verifica espacio disponible y calcula una ruta de destino segura para evitar colisiones."""
    if not _is_safe_to_move(junk_file, dest_base): return None
    try:
        dest_res = dest_base.resolve()
        margin: int = 50 * 1024 * 1024
        usage = shutil.disk_usage(dest_res.anchor)
        if usage.free < (junk_file.size_bytes + margin):
            return None
        safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
        destination_path = _generate_unique_target(dest_res / safe_name)
        return destination_path if destination_path.resolve().is_relative_to(dest_res) else None
    except (OSError, ValueError, AttributeError): return None

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """Mueve archivos validados a la zona de cuarentena tras verificar permisos de escritura."""
    if not files or not isinstance(review_dir, str): return None
    try:
        dest_base = Path(review_dir).expanduser().resolve()
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        if not os.access(dest_base, os.W_OK) or not is_safe_to_modify(dest_base): return None
    except (OSError, RuntimeError, TypeError): return None
    
    for junk_file in files:
        if not isinstance(junk_file, JunkFile) or junk_file.path is None: continue
        try:
            target_path = _can_move_file(junk_file, dest_base)
            if target_path and is_safe_to_modify(junk_file.path) and not _is_file_locked(junk_file.path):
                ensure_safe_to_modify(junk_file.path)
                shutil.move(str(junk_file.path), str(target_path))
        except (OSError, PermissionError, shutil.Error, RuntimeError, TypeError) as e:
            logger.error(f"Error moviendo {junk_file.path}: {e}")
    return dest_base

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Ejecuta la eliminación permanente y segura de archivos dentro de la carpeta de revisión."""
    if not isinstance(review_dir, str): return 0
    try:
        dest = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not dest.is_dir() or not os.access(dest, os.W_OK) or not is_safe_to_modify(dest): return 0
    except (OSError, RuntimeError, TypeError): return 0
    
    count: int = 0
    for item in dest.iterdir():
        try:
            if item.is_file() and is_safe_to_modify(item):
                ensure_safe_to_modify(item)
                item.unlink()
                count += 1
        except (PermissionError, OSError, ValueError, TypeError) as e:
            logger.error(f"Error eliminando {item}: {e}")
    return count
