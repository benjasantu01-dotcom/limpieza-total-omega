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
    return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]

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
    name_lower = filename.lower()
    return any(name_lower.endswith(ext) for ext in JUNK_EXTENSIONS)

def _get_win_attributes(entry: os.DirEntry) -> int:
    """Extrae la máscara de bits de atributos Win32 (System/Hidden/Junction) usando stat directo."""
    try:
        return entry.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        return 0

def _is_junction(entry: os.DirEntry) -> bool:
    """Determina si la ruta es un punto de reparse (Junction/Symlink) para evitar bucles o recursión infinita."""
    return entry.is_symlink() or bool(_get_win_attributes(entry) & WIN_ATTR_JUNCTION)

def _is_unc_path(path: Path) -> bool:
    """Valida si la ruta es de red (UNC), evitando bloqueos por latencia o permisos de red inaccesibles."""
    try:
        return str(path.absolute()).startswith(("\\\\", "//"))
    except (OSError, RuntimeError):
        return True

def _generate_unique_target(target: Path) -> Path:
    """Gestiona colisiones de nombre añadiendo un índice numérico para evitar sobrescrituras accidentales."""
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Verifica si el nombre de carpeta no figura en la lista negra de directorios protegidos."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _is_file_locked(path: Path) -> bool:
    """Intenta validar acceso exclusivo mediante lectura. Retorna True si está bloqueado."""
    try:
        if not path.exists(): return True
        if path.stat().st_size == 0: return False
        with open(path, 'rb') as f:
            f.read(1)
        return False
    except (OSError, PermissionError, IOError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """Previene que el movimiento resulte en una jerarquía circular donde el destino contenga al origen."""
    try:
        s, d = str(src.resolve()), str(dest.resolve())
        return os.path.commonpath([s, d]) == s
    except (OSError, ValueError):
        return True

def _has_forbidden_chars(path: Path) -> bool:
    """Detecta caracteres que podrían inyectar comandos o corromper rutas en shell."""
    path_str = str(path).lower()
    return any(c in path_str for c in ["<", ">", "|", "\0"])

def _validate_path_security(src: Path, dest: Path) -> bool:
    """
    Realiza una auditoría estática de rutas. 
    Verifica límites MAX_PATH de Windows (260 chars) y previene rutas UNC o caracteres maliciosos.
    """
    if _is_unc_path(src) or _is_unc_path(dest) or _has_forbidden_chars(src): return False
    if len(str(src)) > 260 or len(str(dest)) > 260: return False
    return not (is_protected_path(src) or is_protected_path(dest))

def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Coordinador de seguridad para E/S: verifica integridad, exclusividad de archivo,
    restricciones de unidad y que no haya rutas circulares antes de cualquier mutación.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    try:
        if not src.exists() or not src.is_file() or not is_safe_to_modify(src): return False
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
    """Determina si es seguro descender por un directorio, evitando junctions y rutas protegidas."""
    if not _is_allowed_directory(entry.name) or _is_junction(entry): return False
    if entry.path in protected_cache: return False
    if is_protected_path(Path(entry.path)):
        protected_cache.add(entry.path)
        return False
    return True

def _is_valid_junk_entry(entry: os.DirEntry, stats: os.stat_result) -> bool:
    """Filtra archivos por tamaño, atributos de sistema/oculto y extensión basura."""
    return (0 <= stats.st_size < 100_000_000_000 and 
            not (_get_win_attributes(entry) & WIN_ATTR_MASK) and
            is_valid_junk_extension(entry.name))

def _process_directory(current_dir: Path, found: List[JunkFile], depth: int, protected_cache: set[str], visited: set[Path]) -> None:
    """Recorrido recursivo limitado por profundidad y caché para evitar ciclos de enlaces."""
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
    """Escanea los directorios definidos en busca de basura, aplicando exclusiones de seguridad."""
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
    """Ordena los archivos encontrados mediante el registro de criterios configurados."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """Mueve los archivos candidatos a un directorio de cuarentena tras validar integridad."""
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
    """Valida disponibilidad de espacio en disco y genera un nombre seguro para la transferencia."""
    try:
        usage = shutil.disk_usage(dest_base.anchor)
        if usage.free < (junk_file.size_bytes + 52428800): return None
    except (OSError, FileNotFoundError, AttributeError): return None
    safe_name = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
    return _generate_unique_target(dest_base / safe_name)

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Elimina archivos en cuarentena tras validar que es seguro modificar cada elemento."""
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
