"""
organizer.py
Este módulo implementa la lógica de detección y gestión de archivos temporales.

Su objetivo es identificar candidatos a limpieza y moverlos a un entorno de 
revisión aislado (_Para_Revisar). Toda operación crítica se apoya en 
`safety.py` para garantizar que no se manipulen rutas de sistema o archivos 
bloqueados por procesos críticos del SO.

Estrategia de seguridad:
1. Validaciones preventivas (`_is_safe_for_disk_op`) antes de cualquier IO.
2. Uso de `path.resolve()` para mitigar ataques de redirección de rutas.
3. Validación de permisos mediante `os.access` y chequeos de bloqueos.
"""

from __future__ import annotations
import os
import shutil
import string
import logging
import ctypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Final, Callable, Union, TypeAlias, NamedTuple, Dict, Sequence

from safety import is_safe_to_modify, ensure_safe_to_modify, is_protected_path

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger(__name__)

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
    """
    Representa un archivo candidato a limpieza.
    """
    path: Path
    size_bytes: int
    modified: datetime

    def __post_init__(self) -> None:
        if isinstance(self.path, Path):
            try:
                # Normaliza rutas para evitar inconsistencias por enlaces simbólicos
                self.path = self.path.resolve()
            except (OSError, RuntimeError):
                pass

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño del archivo en MB redondeado a dos decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión del archivo está en JUNK_EXTENSIONS."""
        return is_valid_junk_extension(self.path.name)


def is_valid_junk_extension(filename: str) -> bool:
    """Valida si un nombre de archivo corresponde a una extensión considerada basura."""
    return os.path.splitext(filename)[1].lower() in JUNK_EXTENSIONS


def _get_win_attributes(path_or_entry: Union[os.DirEntry, Path]) -> int:
    """
    Obtiene los atributos de archivo Win32 mediante syscalls.
    Retorna la máscara de bits de atributos Win32 o 0 si el acceso falla.
    """
    try:
        if hasattr(path_or_entry, 'stat'):
            return path_or_entry.stat(follow_symlinks=False).st_file_attributes
        return Path(path_or_entry).stat().st_file_attributes
    except (OSError, AttributeError, ValueError):
        return 0


def _is_junction(entry: Union[os.DirEntry, Path]) -> bool:
    """
    Determina si una ruta es un punto de reparse (Junction/Symlink).
    """
    if entry is None: return False
    is_sym = entry.is_symlink() if hasattr(entry, 'is_symlink') else Path(str(entry)).is_symlink()
    is_junction_attr = os.name == "nt" and bool(_get_win_attributes(entry) & 0x400)
    return is_sym or is_junction_attr


def _is_junk_path(path_str: str) -> bool:
    """Delegado para verificar extensión mediante `is_valid_junk_extension`."""
    if not isinstance(path_str, str):
        return False
    return is_valid_junk_extension(path_str)


def _is_unc_path(path: Path) -> bool:
    """
    Verifica si una ruta utiliza formato UNC (Universal Naming Convention).
    Las rutas UNC pueden tener comportamientos inesperados en operaciones locales.
    """
    if path is None: return True
    try:
        p_str: str = str(path.absolute())
        return p_str.startswith(("\\\\", "//"))
    except Exception:
        return True


def _generate_unique_target(target: Path) -> Path:
    """
    Genera una ruta única para evitar colisiones de nombres durante el movimiento.
    """
    if target is None:
        return target
        
    base_target = target
    counter: int = 1
    
    while target.exists() and counter <= 999:
        target = target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
        
    return target


def _is_allowed_directory(name: str) -> bool:
    """Verifica si el nombre de una carpeta está fuera de la lista de bloqueo del sistema."""
    return name is not None and name.lower() not in SYSTEM_FOLDER_BLOCKLIST


def _is_file_locked(path: Path) -> bool:
    """
    Verifica si un archivo está inaccesible debido a bloqueos de SO o permisos.
    
    El método evalúa:
    1. Existencia y tipo (excluye uniones/enlaces para evitar recursión circular).
    2. Atributos de sistema/oculto que impiden manipulación estándar.
    3. Capacidad de apertura exclusiva (intentando un acceso de solo lectura en modo binario).
    
    Retorna True si el archivo está protegido, bloqueado o inaccesible.
    """
    if path is None or not path.exists() or _is_junction(path): 
        return True
    
    if not _passes_system_checks(path):
        return True

    try:
        if not os.access(path, os.R_OK):
            return True
        with open(path, 'rb'):
            pass
        return False
    except (OSError, PermissionError, IOError, BlockingIOError):
        return True


def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Valida que el movimiento no sea circular (ej. intentar mover una carpeta dentro de sí misma).
    """
    if src is None or dest is None: return True
    try:
        s: Path = src.resolve()
        d: Path = dest.resolve()
        return s == d or (d.exists() and s.exists() and d.is_relative_to(s))
    except (OSError, ValueError):
        return True


def _passes_system_checks(src: Path) -> bool:
    """
    Filtra archivos con atributos especiales (Sistema, Oculto).
    """
    if os.name != "nt" or src is None: return True
    # 0x02: Hidden, 0x04: System
    mask: int = 0x06
    return not (_get_win_attributes(src) & mask)


def _has_forbidden_chars(path: Path) -> bool:
    """
    Valida nombres reservados de Windows (ej. CON, NUL) y caracteres prohibidos en rutas.
    """
    if path is None: return True
    try:
        path_str: str = str(path).lower()
        reserved: List[str] = ["con", "prn", "aux", "nul", "com1", "lpt1"]
        if any(path_str.startswith(r) for r in reserved): return True
        return any(c in str(path) for c in ["<", ">", "|", "\0"])
    except (ValueError, TypeError):
        return True


def _validate_path_security(src: Path, dest: Path) -> bool:
    """
    Valida la integridad de la estructura de las rutas.
    """
    if src is None or dest is None: return False
    if _is_unc_path(src) or _is_unc_path(dest): return False
    if _has_forbidden_chars(src): return False
    if len(str(src)) > 260 or len(str(dest)) > 260: return False
    try:
        return not (is_protected_path(src.resolve()) or is_protected_path(dest.resolve()))
    except (OSError, RuntimeError):
        return False


def _validate_file_attributes(src: Path) -> bool:
    """
    Valida integridad básica: es archivo, no está bloqueado por el SO y tiene tamaño positivo.
    """
    try:
        if src is None or not src.exists() or not src.is_file(): return False
        if _is_junction(src) or src.is_symlink(): return False
        if not _passes_system_checks(src) or _is_file_locked(src): return False
        return src.stat().st_size > 0
    except (OSError, PermissionError):
        return False


def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Orquestador principal de seguridad antes de cualquier operación de I/O.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    if not _validate_path_security(src, dest): return False
    
    try:
        s_res: Path = src.resolve()
        if not s_res.exists(): return False
        if dest.exists() and (_is_junction(dest) or dest.is_symlink()): return False
        if _is_recursive_violation(s_res, dest): return False
        target_dir: Path = dest.parent if dest.is_file() else dest
        if not target_dir.exists(): return False
        # Mover entre unidades físicas distintas puede romper permisos y atomicity
        if s_res.drive != target_dir.resolve().drive: return False
        return _validate_file_attributes(s_res)
    except (OSError, RuntimeError, AttributeError):
        return False


def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """Verifica si el objeto JunkFile es seguro para ser movido a una ruta destino."""
    if junk_file is None or not isinstance(junk_file, JunkFile) or dest is None: return False
    return junk_file.path is not None and junk_file.path.exists() and _is_safe_for_disk_op(junk_file.path, dest)


def _should_scan_directory(entry: os.DirEntry) -> bool:
    """Determina si una subcarpeta es apta para ser escaneada."""
    return entry is not None and _is_allowed_directory(entry.name) and not _is_junction(entry)


def _evaluate_entry(entry: os.DirEntry, found: List[JunkFile]) -> None:
    """Analiza una entrada individual y la añade a la lista si es basura válida."""
    try:
        _, ext = os.path.splitext(entry.name)
        if ext.lower() in JUNK_EXTENSIONS:
            info = entry.stat()
            if info.st_size > 0:
                # Se usa la ruta de DirEntry directamente para evitar instanciar Path innecesariamente
                path_obj = Path(entry.path)
                if not _is_file_locked(path_obj):
                    found.append(JunkFile(path_obj, info.st_size, datetime.fromtimestamp(info.st_mtime)))
    except (OSError, PermissionError):
        pass


def _process_directory(current_dir: Path, found: List[JunkFile], depth: int = 0) -> None:
    """
    Recorre recursivamente directorios buscando archivos temporales hasta una profundidad máxima.
    """
    if depth > 50 or current_dir is None or is_protected_path(current_dir):
        return

    try:
        with os.scandir(current_dir) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    if _should_scan_directory(entry):
                        _process_directory(Path(entry.path), found, depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    _evaluate_entry(entry, found)
    except (OSError, PermissionError, RuntimeError):
        pass


def scan_for_junk(directories: Optional[Sequence[str]] = None) -> List[JunkFile]:
    """
    Escanea las rutas indicadas buscando archivos temporales definidos en JUNK_EXTENSIONS.
    """
    if directories is not None and (not isinstance(directories, (list, tuple)) or not all(isinstance(d, str) for d in directories)):
        return []
    
    search_dirs: List[Path] = [Path(d) for d in directories] if directories else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    
    for d in search_dirs:
        try:
            path_obj: Path = d.expanduser()
            if path_obj.is_dir() and not _is_unc_path(path_obj):
                _process_directory(path_obj.resolve(), found)
        except (OSError, RuntimeError, TypeError):
            continue
    return found


def sort_junk(files: Sequence[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena la lista de archivos según el criterio configurado."""
    if not isinstance(files, list):
        return []
        
    key: str = by.lower() if isinstance(by, str) else "size"
    config: SortConfig = SORT_REGISTRY.get(key, SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))


def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """
    Valida espacio y permisos antes de autorizar el movimiento.
    """
    if not _is_safe_to_move(junk_file, dest_base): return None
    try:
        dest_base_res: Path = dest_base.resolve()
        try:
            if shutil.disk_usage(dest_base_res.anchor).free < (junk_file.size_bytes + (50 * 1024 * 1024)): 
                return None
        except (OSError, ValueError):
            pass 
            
        src_res: Path = junk_file.path.resolve()
        safe_name: str = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
        target: Path = _generate_unique_target(dest_base_res / safe_name)
        
        if target.exists() and os.path.samefile(src_res, target): return None
            
        return target
    except (OSError, ValueError, AttributeError):
        return None


def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Traslada archivos validados a un área de cuarentena para revisión posterior por el usuario.
    """
    if not files or not isinstance(review_dir, str): return None

    try:
        dest_base: Path = Path(review_dir).expanduser().resolve()
        if not is_safe_to_modify(dest_base): return None
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError, TypeError):
        return None

    for junk_file in files:
        try:
            target = _can_move_file(junk_file, dest_base)
            if target and is_safe_to_modify(junk_file.path) and is_safe_to_modify(target):
                ensure_safe_to_modify(junk_file.path)
                ensure_safe_to_modify(target)
                shutil.move(str(junk_file.path), str(target))
        except (FileNotFoundError, OSError, PermissionError, shutil.Error, RuntimeError, TypeError) as e:
            logger.error(f"Error moviendo {junk_file.path}: {e}")
            continue
    return dest_base


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina archivos desde la carpeta de cuarentena, habiendo validado previamente su seguridad.
    """
    if not isinstance(review_dir, str): return 0

    try:
        dest: Path = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not is_safe_to_modify(dest): 
            return 0
    except (OSError, RuntimeError, TypeError):
        return 0

    count: int = 0
    for item in dest.iterdir():
        try:
            if not isinstance(item, Path): continue
            resolved_item = item.resolve()
            if resolved_item.is_file() and is_safe_to_modify(resolved_item):
                ensure_safe_to_modify(resolved_item)
                resolved_item.unlink()
                count += 1
        except (PermissionError, OSError, ValueError, TypeError) as e:
            logger.error(f"Error eliminando {item}: {e}")
    return count
