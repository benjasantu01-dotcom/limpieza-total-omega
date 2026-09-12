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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Final, Callable, Union, TypeAlias, NamedTuple, Dict, Sequence

from safety import is_safe_to_modify, ensure_safe_to_modify, is_protected_path

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger(__name__)

SortKey: TypeAlias = Union[int, datetime]

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
        """Retorna el tamaño del archivo en MB."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión del archivo es considerada basura."""
        return is_valid_junk_extension(self.path.name)

def is_valid_junk_extension(filename: str) -> bool:
    """Valida si la extensión del archivo está en JUNK_EXTENSIONS."""
    return os.path.splitext(filename)[1].lower() in JUNK_EXTENSIONS

def _get_win_attributes(path_or_entry: Union[os.DirEntry, Path]) -> int:
    """Obtiene atributos de archivo Win32 mediante syscalls."""
    try:
        if hasattr(path_or_entry, 'stat'):
            return path_or_entry.stat(follow_symlinks=False).st_file_attributes
        return Path(path_or_entry).stat().st_file_attributes
    except (OSError, AttributeError, ValueError):
        return 0

def _is_junction(entry: Union[os.DirEntry, Path]) -> bool:
    """Determina si una ruta es un punto de reparse (Junction/Symlink)."""
    if entry is None: return False
    is_sym = entry.is_symlink() if hasattr(entry, 'is_symlink') else Path(str(entry)).is_symlink()
    is_junction_attr = os.name == "nt" and bool(_get_win_attributes(entry) & 0x400)
    return is_sym or is_junction_attr

def _is_unc_path(path: Path) -> bool:
    """Verifica si una ruta es UNC (Universal Naming Convention)."""
    if not isinstance(path, Path): return True
    try:
        return str(path.absolute()).startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """Añade un sufijo numérico si el archivo ya existe en destino."""
    if target is None: return target
    base_target, counter = target, 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Verifica si el nombre de una carpeta no está bloqueado."""
    return bool(name) and name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _is_file_locked(path: Path) -> bool:
    """Verifica si un archivo está inaccesible o en uso sin abrirlo exclusivamente."""
    if path is None: return True
    try:
        if not path.exists() or _is_junction(path): return True
        if not _passes_system_checks(path): return True
        return not os.access(path, os.R_OK)
    except (OSError, PermissionError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """Valida que el destino no sea subdirectorio de la fuente."""
    try:
        s, d = src.resolve(), dest.resolve()
        return s == d or (d.exists() and d.is_relative_to(s))
    except (OSError, ValueError):
        return True

def _passes_system_checks(src: Path) -> bool:
    """Filtra archivos con atributos especiales (Sistema/Oculto)."""
    if os.name != "nt" or src is None: return True
    attrs = _get_win_attributes(src)
    return not (attrs & 0x06) if attrs != 0 else True

def _has_forbidden_chars(path: Path) -> bool:
    """Valida nombres reservados de Windows y caracteres prohibidos."""
    if path is None: return True
    path_str = str(path).lower()
    reserved = ["con", "prn", "aux", "nul", "com1", "lpt1"]
    return any(path_str.startswith(r) for r in reserved) or any(c in str(path) for c in ["<", ">", "|", "\0"])

def _validate_path_security(src: Path, dest: Path) -> bool:
    """
    Verifica que las rutas sean absolutas, no UNC, no contengan nombres 
    reservados, respeten límites de longitud MAX_PATH y no sean protegidas.
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
    Confirma que el archivo existe, es un archivo regular (no junction/link), 
    no tiene atributos de sistema y es accesible para lectura.
    """
    try:
        if src is None or not src.exists() or not src.is_file(): return False
        if _is_junction(src) or src.is_symlink() or not _passes_system_checks(src): return False
        return not _is_file_locked(src) and src.stat().st_size > 0
    except (OSError, PermissionError):
        return False

def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Realiza una auditoría completa de seguridad previa a la ejecución de I/O,
    asegurando integridad de rutas, prevención de recursividad y validación de archivos.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    if not _validate_path_security(src, dest): return False
    try:
        s_res = src.resolve()
        if not s_res.exists() or _is_recursive_violation(s_res, dest): return False
        target_dir = dest.parent if dest.is_file() else dest
        if not target_dir.exists() or s_res.drive != target_dir.resolve().drive: return False
        return _validate_file_attributes(s_res)
    except (OSError, RuntimeError, AttributeError):
        return False

def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """Wrapper para comprobar seguridad de JunkFile."""
    if not isinstance(junk_file, JunkFile) or dest is None: return False
    return junk_file.path is not None and junk_file.path.exists() and _is_safe_for_disk_op(junk_file.path, dest)

def _should_scan_directory(entry: os.DirEntry, protected_cache: set[str]) -> bool:
    """Filtro de directorios con caché de rutas protegidas."""
    if entry is None or not _is_allowed_directory(entry.name) or _is_junction(entry):
        return False
    return entry.path not in protected_cache

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int = 0, protected_cache: Optional[set[str]] = None) -> None:
    """Recorrido recursivo optimizado con caché de rutas bloqueadas."""
    if protected_cache is None: protected_cache = set()
    if depth > 50 or current_dir is None or not current_dir.exists(): return
    
    if is_protected_path(current_dir):
        protected_cache.add(str(current_dir))
        return

    try:
        with os.scandir(current_dir) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry, protected_cache):
                            _process_directory(Path(entry.path), found, depth + 1, protected_cache)
                    elif entry.is_file(follow_symlinks=False):
                        _evaluate_entry(entry, found)
                except (OSError, PermissionError): continue
    except (OSError, PermissionError, RuntimeError): pass

def scan_for_junk(directories: Optional[Sequence[str]] = None) -> List[JunkFile]:
    """Escanea directorios en busca de archivos temporales."""
    valid_dirs = [Path(d) for d in (directories or DEFAULT_SCAN_DIRS) if isinstance(d, str)]
    found: List[JunkFile] = []
    protected_cache = set()
    for d in valid_dirs:
        try:
            p = d.expanduser()
            if p.exists() and p.is_dir() and not _is_unc_path(p):
                _process_directory(p.resolve(), found, 0, protected_cache)
        except (OSError, RuntimeError, TypeError): continue
    return found

def _evaluate_entry(entry: os.DirEntry, found: List[JunkFile]) -> None:
    """Evalúa un archivo, valida si es basura y lo añade a la lista usando datos de scandir."""
    try:
        # Validación rápida sin llamadas extra a disco (scandir ya tiene info)
        if is_valid_junk_extension(entry.name) and len(entry.path) < 260:
            stat_info = entry.stat()
            if stat_info.st_size > 0 and not (_get_win_attributes(entry) & 0x06):
                # Verificar acceso antes de instanciar
                if os.access(entry.path, os.R_OK):
                    found.append(JunkFile(Path(entry.path), stat_info.st_size, datetime.fromtimestamp(stat_info.st_mtime)))
    except (OSError, PermissionError):
        pass

def sort_junk(files: Sequence[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena la lista de archivos según criterio."""
    if not isinstance(files, list): return []
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Verifica espacio y genera ruta destino única, asegurando que no escape al directorio base."""
    if not _is_safe_to_move(junk_file, dest_base): return None
    try:
        if shutil.disk_usage(dest_base.resolve().anchor).free < (junk_file.size_bytes + (50 * 1024 * 1024)):
            return None
        safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
        target = _generate_unique_target(dest_base.resolve() / safe_name)
        return target if target.resolve().is_relative_to(dest_base.resolve()) else None
    except (OSError, ValueError, AttributeError): return None

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """Traslada archivos validados a un área de cuarentena."""
    if not files or not isinstance(review_dir, str): return None
    try:
        dest_base = Path(review_dir).expanduser().resolve()
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        if not os.access(dest_base, os.W_OK) or not is_safe_to_modify(dest_base): return None
    except (OSError, RuntimeError, TypeError): return None
    for junk_file in files:
        try:
            target = _can_move_file(junk_file, dest_base)
            if target and is_safe_to_modify(junk_file.path) and is_safe_to_modify(target):
                ensure_safe_to_modify(junk_file.path)
                ensure_safe_to_modify(target)
                shutil.move(str(junk_file.path), str(target))
        except (OSError, PermissionError, shutil.Error, RuntimeError, TypeError) as e:
            logger.error(f"Error moviendo {junk_file.path}: {e}")
    return dest_base

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Elimina permanentemente archivos desde la carpeta de cuarentena."""
    if not isinstance(review_dir, str): return 0
    try:
        dest = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not os.access(dest, os.W_OK) or not is_safe_to_modify(dest): return 0
    except (OSError, RuntimeError, TypeError): return 0
    count = 0
    for item in dest.iterdir():
        try:
            if isinstance(item, Path) and item.is_file() and is_safe_to_modify(item.resolve()):
                ensure_safe_to_modify(item.resolve())
                item.unlink()
                count += 1
        except (PermissionError, OSError, ValueError, TypeError) as e:
            logger.error(f"Error eliminando {item}: {e}")
    return count
