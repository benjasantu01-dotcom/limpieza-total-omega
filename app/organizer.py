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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Final, Callable, Union, TypeAlias, NamedTuple, Dict

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
    Se utiliza una dataclass para centralizar el acceso a propiedades calculadas
    como tamaño en MB, evitando lógica dispersa en el módulo.
    """
    path: Path
    size_bytes: int
    modified: datetime

    def __post_init__(self) -> None:
        if isinstance(self.path, Path):
            try:
                # Normaliza rutas para evitar inconsistencias por enlaces simbólicos
                self.path = self.path.resolve()
            except OSError:
                pass

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño del archivo en MB redondeado a dos decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión del archivo está en JUNK_EXTENSIONS."""
        return _is_junk_path(self.path)


def _is_junction(entry: os.DirEntry | Path) -> bool:
    """
    Verifica si la entrada es un punto de reparse (Junction/Symlink).
    Utiliza atributos de archivo de Windows (0x400) para identificar puntos de montaje.
    """
    try:
        if isinstance(entry, os.DirEntry):
            is_sym = entry.is_symlink()
            is_junction_attr = os.name == "nt" and bool(entry.stat().st_file_attributes & 0x400)
            return is_sym or is_junction_attr
        
        path_p = Path(entry)
        is_sym = path_p.is_symlink()
        is_junction_attr = os.name == "nt" and bool(path_p.lstat().st_file_attributes & 0x400)
        return is_sym or is_junction_attr
    except (OSError, AttributeError):
        return True # Asumir inseguro ante errores de lectura


def _is_junk_path(path: Path) -> bool:
    """Comprueba si la extensión del archivo coincide con las extensiones de basura definidas."""
    return path.suffix.lower() in JUNK_EXTENSIONS


def _is_unc_path(path: Path) -> bool:
    """Detecta si una ruta corresponde a una ruta UNC de red (formato \\servidor\recurso)."""
    if path is None: return True
    try:
        p_str: str = str(path.absolute())
        return p_str.startswith(("\\\\", "//"))
    except Exception:
        return True


def _generate_unique_target(target: Path) -> Path:
    """
    Genera una ruta única para evitar sobreescritura en el directorio de revisión.
    Si el destino ya existe, aplica un sufijo numérico incremental (_1, _2...).
    """
    if target is None or not target.exists():
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
    return name is not None and name.lower() not in SYSTEM_FOLDER_BLOCKLIST


def _is_file_locked(path: Path) -> bool:
    """
    Verifica si un archivo está en uso exclusivo mediante intento de apertura.
    Se utiliza como medida defensiva para evitar errores de E/S al mover archivos activos.
    """
    if path is None: return True
    try:
        # Intentamos abrir en modo lectura exclusiva
        with open(path, "rb") as f:
            return False
    except (PermissionError, OSError, IOError):
        return True # Asumir bloqueado/inseguro ante cualquier error de acceso


def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """
    Previene que el destino contenga a la fuente, protegiendo la integridad 
    del sistema de archivos al evitar movimientos recursivos auto-referenciados.
    """
    if src is None or dest is None: return True
    try:
        s: Path = src.resolve()
        d: Path = dest.resolve()
        return d.is_relative_to(s) or os.path.samefile(s, d)
    except (OSError, ValueError):
        return True


def _passes_system_checks(src: Path) -> bool:
    """
    Verifica atributos de bajo nivel de Windows (System, Hidden, ReadOnly).
    Los archivos con estos atributos se consideran protegidos y no deben ser movidos.
    """
    if os.name != "nt" or src is None: return True
    try:
        stat: os.stat_result = src.stat()
        # Máscara binaria: 0x4 (SYSTEM), 0x2 (HIDDEN), 0x1 (READONLY)
        return not (stat.st_file_attributes & 0x407)
    except OSError:
        return False # Asumir protegido/inseguro si no se pueden leer atributos


def _has_forbidden_chars(path: Path) -> bool:
    """Valida la ausencia de caracteres o nombres reservados en el sistema de archivos de Windows."""
    if path is None: return True
    path_str: str = str(path).lower()
    reserved: List[str] = ["con", "prn", "aux", "nul", "com1", "lpt1"]
    if any(path_str.startswith(r) for r in reserved): return True
    return any(c in str(path) for c in ["<", ">", "|", "\0"])


def _validate_path_security(src: Path, dest: Path) -> bool:
    """Valida que ni fuente ni destino violen reglas de seguridad, UNC o nombres reservados."""
    if src is None or dest is None: return False
    if _is_unc_path(src) or _is_unc_path(dest): return False
    if _has_forbidden_chars(src): return False
    if is_protected_path(src.resolve()) or is_protected_path(dest.resolve()): return False
    return True


def _validate_file_attributes(src: Path) -> bool:
    """Verifica que el archivo sea un archivo real, no esté bloqueado y tenga atributos seguros."""
    try:
        if src is None or not src.exists() or not src.is_file(): return False
        if _is_junction(src) or src.is_symlink(): return False
        if not _passes_system_checks(src) or _is_file_locked(src): return False
        return src.stat().st_size > 0
    except (OSError, PermissionError):
        return False


def _is_safe_for_disk_op(src: Path, dest: Path) -> bool:
    """
    Realiza una validación exhaustiva (fail-fast) antes de operaciones de disco.
    Divide la validación en capas: formato de ruta, seguridad de sistema y estado del archivo.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    
    # Longitud de ruta máxima para evitar excepciones de sistema en Windows
    if len(str(src)) >= 255 or len(str(dest)) >= 255: return False
    
    if not _validate_path_security(src, dest): return False
    
    try:
        s_res: Path = src.resolve()
        
        if dest.exists() and (_is_junction(dest) or dest.is_symlink()): return False
        if _is_recursive_violation(s_res, dest): return False
        
        target_dir: Path = dest.parent if dest.is_file() else dest
        if not (os.access(s_res, os.W_OK) and os.access(target_dir, os.W_OK)): return False
        
        return _validate_file_attributes(s_res)
    except (OSError, RuntimeError, AttributeError):
        return False


def _is_safe_to_move(junk_file: JunkFile, dest: Path) -> bool:
    """Verifica si el archivo es un objeto válido y seguro para ser movido."""
    if junk_file is None or not isinstance(junk_file, JunkFile): return False
    return junk_file.path is not None and junk_file.path.exists() and _is_safe_for_disk_op(junk_file.path, dest)


def _should_scan_directory(entry: os.DirEntry) -> bool:
    """Determina si una subcarpeta es apta para ser escaneada basándose en su nombre."""
    return entry is not None and _is_allowed_directory(entry.name) and not _is_junction(entry)


def _process_directory(current_dir: Path, found: List[JunkFile], depth: int = 0) -> None:
    """
    Recorre recursivamente un directorio buscando archivos basura.
    """
    if not isinstance(current_dir, Path) or not current_dir.exists() or depth > 50:
        return
    try:
        with os.scandir(current_dir) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry):
                            _process_directory(Path(entry.path), found, depth + 1)
                    elif entry.is_file(follow_symlinks=False):
                        if os.path.splitext(entry.name)[1].lower() in JUNK_EXTENSIONS:
                            stats: os.stat_result = entry.stat()
                            if stats.st_size > 0:
                                found.append(JunkFile(Path(entry.path), stats.st_size, datetime.fromtimestamp(stats.st_mtime)))
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError, RuntimeError):
        pass


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Escanea directorios en busca de archivos basura. Valida que cada entrada sea una lista de strings.
    """
    if directories is not None and (not isinstance(directories, list) or not all(isinstance(d, str) for d in directories)):
        return []
    
    search_dirs: List[Path] = [Path(d) for d in directories] if directories else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    
    for d in search_dirs:
        try:
            if not isinstance(d, Path): continue
            path_obj: Path = d.expanduser()
            if path_obj.exists() and path_obj.is_dir() and not _is_unc_path(path_obj):
                resolved: Path = path_obj.resolve()
                if not is_protected_path(resolved):
                    _process_directory(resolved, found)
        except (OSError, RuntimeError, TypeError):
            continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena una lista de archivos basura según un criterio registrado."""
    if not isinstance(files, list) or not all(isinstance(f, JunkFile) for f in files):
        return []
        
    key: str = by.lower() if isinstance(by, str) else "size"
    config: SortConfig = SORT_REGISTRY.get(key, SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))


def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Valida espacio en disco y seguridad antes de proponer una ruta de movimiento."""
    if junk_file is None or dest_base is None: return None
    if not isinstance(junk_file, JunkFile) or junk_file.path is None or not isinstance(dest_base, Path): return None
    if _is_unc_path(dest_base) or is_protected_path(dest_base): return None
    try:
        if not dest_base.exists() or not dest_base.is_dir(): return None
        
        # Validar espacio disponible + margen de seguridad (50MB)
        if shutil.disk_usage(dest_base.anchor).free < (junk_file.size_bytes + (50 * 1024 * 1024)): 
            return None
            
        if not _is_safe_to_move(junk_file, dest_base): return None
        
        src_res: Path = junk_file.path.resolve()
        dest_base_res: Path = dest_base.resolve()
        if src_res.is_relative_to(dest_base_res): return None
        
        safe_name: str = f"{junk_file.path.stem}_{int(junk_file.modified.timestamp())}{junk_file.path.suffix}"
        target: Path = (_generate_unique_target(dest_base / safe_name)).resolve()
        return target if target.is_relative_to(dest_base_res) else None
    except (OSError, ValueError, AttributeError):
        return None


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """Traslada archivos basura a un área de cuarentena para revisión humana."""
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
            if target and is_safe_to_modify(src) and is_safe_to_modify(target) and not is_protected_path(target):
                ensure_safe_to_modify(src)
                ensure_safe_to_modify(target)
                shutil.move(str(src), str(target))
        except (OSError, PermissionError, shutil.Error, RuntimeError) as e:
            logger.error(f"Error moviendo {junk_file.path}: {e}")
    return dest_base


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Elimina permanentemente archivos en el directorio de revisión con validación estricta de tipo."""
    if not isinstance(review_dir, str): return 0

    try:
        dest: Path = Path(review_dir).expanduser().resolve()
        if not dest.exists() or _is_unc_path(dest) or not is_safe_to_modify(dest) or is_protected_path(dest): 
            return 0
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
