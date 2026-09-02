"""
organizer.py
Organiza archivos "basura" (temporales, cache, descargas viejas, etc.)
en carpetas ordenadas por tamaño o fecha, sin borrar nada automáticamente.

Filosofía de seguridad: este módulo NUNCA borra archivos por sí solo.
Solo mueve candidatos a una carpeta de revisión ("_Para_Revisar") para
que el usuario descida qué borrar. Borrar es una acción explícita y
separada (ver delete_reviewed()).
"""

from __future__ import annotations
import os
import shutil
import string
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Final, Callable, Union, TypeAlias, NamedTuple, Dict

from safety import is_safe_to_modify, ensure_safe_to_modify, is_protected_path

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Definiciones de tipo para claridad en firmas complejas
SortKey: TypeAlias = Union[int, datetime]

class SortConfig(NamedTuple):
    """Define los criterios permitidos para el ordenamiento de archivos."""
    field: str
    key_func: Callable[[JunkFile], SortKey]

# Mapeo centralizado de criterios de ordenamiento para facilitar la extensión
SORT_REGISTRY: Final[Dict[str, SortConfig]] = {
    "size": SortConfig("size", lambda f: f.size_bytes),
    "date": SortConfig("date", lambda f: f.modified)
}

# Extensiones típicas de archivos "basura" / temporales
JUNK_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
})

# Carpetas típicas donde se acumula basura
DEFAULT_SCAN_DIRS: Final[List[Path]] = [
    Path(os.environ.get("TEMP", "C:\\Temp")),
    Path(os.environ.get("LOCALAPPDATA", "C:\\")) / "Temp",
    Path.home() / "Downloads",
]

# Carpetas de sistema críticas que nunca se recorren para prevenir daños al SO
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
    """Representa un archivo candidato a limpieza con metadatos asociados."""
    path: Path
    size_bytes: int
    modified: datetime

    def __post_init__(self) -> None:
        if isinstance(self.path, Path):
            self.path = self.path.resolve()

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño del archivo en MB redondeado a dos decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión del archivo está en JUNK_EXTENSIONS."""
        return _is_junk_path(self.path)


def _is_junction(entry: os.DirEntry | Path) -> bool:
    """Verifica si la entrada es un punto de reparse (Junction/Symlink) para evitar bucles infinitos."""
    try:
        if isinstance(entry, os.DirEntry):
            return entry.is_symlink() or (os.name == "nt" and bool(entry.stat().st_file_attributes & 0x400))
        return entry.is_symlink() or (os.name == "nt" and bool(entry.lstat().st_file_attributes & 0x400))
    except (OSError, AttributeError):
        return False


def _is_junk_path(path: Path) -> bool:
    """Comprueba si la extensión del archivo coincide con las extensiones de basura definidas."""
    return path.suffix.lower() in JUNK_EXTENSIONS


def _is_unc_path(path: Path) -> bool:
    """Detecta si una ruta corresponde a una ruta UNC de red (formato \\servidor\recurso)."""
    try:
        return str(path.absolute()).startswith(("\\\\", "//"))
    except Exception:
        return True


def _generate_unique_target(target: Path) -> Path:
    """Genera una ruta única para evitar sobreescritura, añadiendo sufijo numérico si el archivo existe."""
    if not target.exists():
        return target
        
    parent: Path = target.parent
    stem: str = target.stem
    suffix: str = target.suffix
    counter: int = 1
    
    while (candidate := parent / f"{stem}_{counter}{suffix}").exists():
        counter += 1
        if counter > 999: break 
    return candidate


def _is_allowed_directory(name: str) -> bool:
    """Valida si el nombre de una carpeta no está en la lista de carpetas críticas del sistema."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST


def _is_file_locked(path: Path) -> bool:
    """Intenta abrir el archivo en modo lectura binaria; si falla, el archivo está bloqueado por el SO."""
    try:
        with open(path, "rb"):
            return False
    except (OSError, PermissionError, IOError):
        return True


def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """Previene que el destino sea una ruta que contenga a la fuente, evitando ciclos destructivos."""
    try:
        s: Path = src.resolve()
        d: Path = dest.resolve()
        if s == d: return True
        try:
            return d.is_relative_to(s) or os.path.samefile(s, d)
        except OSError:
            return False
    except (OSError, RuntimeError, ValueError):
        return True


def _passes_system_checks(src: Path) -> bool:
    """Verifica atributos de sistema/oculto/solo lectura que impiden manipulación estándar."""
    if os.name != "nt": return True
    try:
        stat = src.stat()
        # Máscara: 0x4 (SYSTEM), 0x2 (HIDDEN), 0x1 (READONLY)
        return not (stat.st_file_attributes & 0x407)
    except OSError:
        return False


def _has_forbidden_chars(path: Path) -> bool:
    """Valida la ausencia de caracteres reservados de Windows en la ruta."""
    path_str = str(path).lower()
    reserved = {"con", "prn", "aux", "nul", "com1", "lpt1"}
    if any(path_str.startswith(r) for r in reserved): return True
    return any(c in str(path) for c in ["<", ">", "|", "\0"])


def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Realiza una validación exhaustiva de seguridad antes de mover o borrar un archivo.
    Verifica: integridad de rutas, permisos de escritura, estado de bloqueo, 
    ausencia de caracteres prohibidos y cumplimiento de reglas en safety.py.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    
    try:
        s_res = src.resolve()
        if not s_res.exists() or not s_res.is_file() or s_res.parent == s_res: return False
        
        # Chequeos de integridad estructural y seguridad
        if _is_junction(s_res) or s_res.is_symlink(): return False
        if dest.exists() and (_is_junction(dest) or dest.is_symlink()): return False
        
        if is_protected_path(s_res) or is_protected_path(dest.resolve()): return False
        if not is_safe_to_modify(s_res) or not is_safe_to_modify(dest): return False
        if _is_recursive_violation(s_res, dest): return False
        
        # Verificación de permisos de escritura y estado de bloqueo
        target_dir = dest.parent if dest.is_file() else dest
        if not (os.access(s_res, os.W_OK) and os.access(target_dir, os.W_OK)): return False
        
        stat = s_res.stat()
        return stat.st_size > 0 and _passes_system_checks(s_res) and not _is_file_locked(s_res)
    except (OSError, RuntimeError, AttributeError):
        return False


def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """Verifica si un objeto JunkFile es seguro para la operación de movimiento."""
    return isinstance(junk_file, JunkFile) and junk_file.path.exists() and _is_safe_for_disk_op(junk_file.path, dest)


def _should_scan_directory(entry: os.DirEntry) -> bool:
    """Determina si una subcarpeta es apta para ser escaneada basándose en su nombre y naturaleza técnica."""
    return _is_allowed_directory(entry.name) and not _is_junction(entry)


def _process_directory(current_dir: Path, found: List[JunkFile]) -> None:
    """Recorre recursivamente un directorio buscando archivos basura usando cache de DirEntry."""
    if not isinstance(current_dir, Path) or not current_dir.exists():
        return
    try:
        with os.scandir(current_dir) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry):
                            _process_directory(Path(entry.path), found)
                    elif entry.is_file(follow_symlinks=False):
                        if _is_junk_path(Path(entry.name)):
                            stats = entry.stat()
                            if stats.st_size > 0:
                                found.append(JunkFile(Path(entry.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime)))
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError, RuntimeError):
        pass


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """Escanea los directorios especificados en busca de archivos basura."""
    if directories is not None and not isinstance(directories, list):
        return []
    
    search_dirs: List[Path] = [Path(d) for d in directories] if directories else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    
    for d in search_dirs:
        try:
            if not isinstance(d, Path): continue
            path_obj = d.expanduser()
            if path_obj.exists() and path_obj.is_dir() and not _is_unc_path(path_obj):
                resolved = path_obj.resolve()
                if not is_protected_path(resolved):
                    _process_directory(resolved, found)
        except (OSError, RuntimeError, TypeError):
            continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena una lista de JunkFiles según un criterio registrado en SORT_REGISTRY."""
    if not isinstance(files, list) or not all(isinstance(f, JunkFile) for f in files):
        return []
        
    key: str = by.lower() if isinstance(by, str) else "size"
    config: SortConfig = SORT_REGISTRY.get(key, SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))


def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Valida espacio en disco y seguridad de ruta antes de proponer una ruta de movimiento."""
    if not isinstance(junk_file, JunkFile) or not isinstance(dest_base, Path): return None
    if _is_unc_path(dest_base) or is_protected_path(dest_base): return None
    try:
        if not dest_base.exists() or not dest_base.is_dir(): return None
        
        # Validar espacio (al menos 50MB libres extra)
        if shutil.disk_usage(dest_base.anchor).free < (junk_file.size_bytes + (50 * 1024 * 1024)): 
            return None
            
        if not _is_safe_to_move(junk_file, dest_base): return None
        if junk_file.path.resolve().is_relative_to(dest_base.resolve()): return None
        
        safe_name: str = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
        target: Path = (_generate_unique_target(dest_base / safe_name)).resolve()
        return target if target.is_relative_to(dest_base.resolve()) else None
    except (OSError, ValueError, AttributeError):
        return None


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Traslada archivos basura a un área de cuarentena para revisión humana.
    Realiza chequeos preventivos antes de cada operación individual de 'shutil.move' 
    utilizando 'ensure_safe_to_modify' para garantizar la integridad.
    """
    if not files or not isinstance(review_dir, str): return None

    try:
        dest_base: Path = Path(review_dir).expanduser().resolve()
        if _is_unc_path(dest_base) or is_protected_path(dest_base): return None
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        if not is_safe_to_modify(dest_base): return None
    except (OSError, RuntimeError):
        return None

    for junk_file in files:
        try:
            if not isinstance(junk_file, JunkFile) or junk_file.path is None: continue
            src: Path = junk_file.path.resolve()
            if not src.exists() or src.is_relative_to(dest_base): continue
            
            target: Optional[Path] = _can_move_file(junk_file, dest_base)
            if target and is_safe_to_modify(src) and is_safe_to_modify(target):
                ensure_safe_to_modify(src)
                ensure_safe_to_modify(target)
                shutil.move(str(src), str(target))
        except (OSError, PermissionError, shutil.Error, RuntimeError) as e:
            logger.error(f"Error moviendo {junk_file.path}: {e}")
    return dest_base


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Elimina permanentemente archivos en el directorio de revisión tras validación de seguridad."""
    if not isinstance(review_dir, str): return 0

    try:
        dest: Path = Path(review_dir).expanduser().resolve()
        if _is_unc_path(dest) or not dest.exists() or not is_safe_to_modify(dest) or is_protected_path(dest): return 0
    except (OSError, RuntimeError):
        return 0

    count: int = 0
    for item in dest.iterdir():
        try:
            if item.is_file() and item.exists() and is_safe_to_modify(item) and not is_protected_path(item):
                if _passes_system_checks(item) and not _is_file_locked(item):
                    ensure_safe_to_modify(item)
                    item.unlink()
                    count += 1
        except (PermissionError, OSError, ValueError) as e:
            logger.error(f"Error eliminando {item}: {e}")
    return count
