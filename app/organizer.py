"""
organizer.py

Este módulo implementa la lógica de detección, validación y gestión de archivos 
temporales (junk files) detectados en el sistema.

Su responsabilidad es identificar archivos candidatos a limpieza basados en 
extensiones y antigüedad, moviéndolos a un entorno de revisión (`_Para_Revisar`) 
antes de cualquier borrado, garantizando siempre la integridad mediante el 
módulo `safety.py`. Toda operación de E/S está protegida contra race conditions 
y violaciones de rutas de sistema.
"""

from __future__ import annotations
import os
import string
import logging
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Final, Callable, Union, TypeAlias, NamedTuple, Dict, Sequence, Literal, Set, Iterable

from safety import is_safe_to_modify, ensure_safe_to_modify, is_protected_path

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger(__name__)

# Tipos alias para claridad en firmas de funciones
SortKey: TypeAlias = Union[int, datetime]
SortField: TypeAlias = Literal["size", "date"]
PathLike: TypeAlias = Union[str, Path]

@dataclass(frozen=True)
class Win32Attributes:
    """Mapeo de constantes de atributos de archivo Win32 obtenidas de kernel32.h."""
    JUNCTION: Final[int] = 0x400      # Identificador de puntos de reanálisis (Junctions)
    SYSTEM: Final[int] = 0x04         # Flag de archivo de sistema de Windows
    HIDDEN: Final[int] = 0x02         # Flag de archivo oculto
    READONLY: Final[int] = 0x01       # Flag de solo lectura
    MASK_PROTECTED: Final[int] = 0x04 | 0x02  # Combinación para archivos ocultos o de sistema
    REPARSE_POINT: Final[int] = 0x400  # Reparse point que suele indicar un enlace simbólico

@dataclass(frozen=True)
class SecurityLimits:
    """Umbrales de seguridad para operaciones de disco, evitando desbordamientos o accesos indebidos."""
    MAX_PATH_LENGTH: Final[int] = 260               # Límite clásico de Windows MAX_PATH
    MAX_FILE_SIZE_BYTES: Final[int] = 100_000_000_000 # 100GB: Límite superior para evitar escaneos de unidades masivas
    MIN_FREE_SPACE_BYTES: Final[int] = 52_428_800    # 50MB: Espacio de reserva mínimo para evitar saturación del sistema
    MAX_SCAN_DEPTH: Final[int] = 50                  # Límite de recursión para prevenir stack overflow
    ATTRS: Final[Win32Attributes] = Win32Attributes()

LIMITS: Final[SecurityLimits] = SecurityLimits()

SYSTEM_CRITICAL_NAMES: Final[frozenset[str]] = frozenset({"pagefile.sys", "hiberfil.sys", "swapfile.sys"})

JUNK_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
})

# Caché pre-procesada para operaciones de alta frecuencia en bucles de escaneo
JUNK_EXTENSIONS_TUPLE: Final[tuple[str, ...]] = tuple(JUNK_EXTENSIONS)

@dataclass(frozen=True)
class ScanSettings:
    """Configuración inmutable para el proceso de escaneo de archivos."""
    max_depth: int = LIMITS.MAX_SCAN_DEPTH
    max_size: int = LIMITS.MAX_FILE_SIZE_BYTES
    extensions: frozenset[str] = JUNK_EXTENSIONS

class SortConfig(NamedTuple):
    """Define cómo extraer un valor de ordenamiento desde un objeto JunkFile."""
    field: str
    key_func: Callable[[JunkFile], SortKey]

SORT_REGISTRY: Final[Dict[str, SortConfig]] = {
    "size": SortConfig("size", lambda f: f.size_bytes),
    "date": SortConfig("date", lambda f: f.modified)
}

DEFAULT_SCAN_DIRS: Final[List[Path]] = [
    Path(os.environ.get("TEMP", "C:\\Temp")),
    Path(os.environ.get("LOCALAPPDATA", "C:\\")) / "Temp",
    Path.home() / "Downloads",
]

SYSTEM_FOLDER_BLOCKLIST: Final[frozenset[str]] = frozenset({
    "windows", "program files", "program files (x86)", "$recycle.bin", "system volume information"
})

def list_available_drives() -> List[str]:
    """Identifica letras de unidad montadas en el sistema para permitir el escaneo multi-unidad."""
    if os.name != "nt":
        return []
    try:
        return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
    except OSError:
        return []

@dataclass
class JunkFile:
    """
    Representa un archivo detectado que califica como candidato a limpieza.
    Utiliza inodos (_ino) y dev (_dev) para garantizar que el archivo analizado 
    es idéntico al que será movido, protegiendo contra la sustitución por enlaces simbólicos.
    """
    path: Path
    size_bytes: int
    modified: datetime
    _ino: Optional[int] = None
    _dev: Optional[int] = None

    def __post_init__(self) -> None:
        """Normaliza la ruta y captura el estado del archivo inmediatamente tras su instanciación."""
        if not isinstance(self.path, Path):
            self.path = Path(self.path)
        try:
            stat_result = self.path.lstat()
            abs_path = self.path.absolute()
            self.path = abs_path
            if self._ino is None: self._ino = stat_result.st_ino
            if self._dev is None: self._dev = stat_result.st_dev
        except (OSError, PermissionError, FileNotFoundError):
            pass

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño en MB para propósitos de reporte UI."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Determina si la extensión del archivo es candidata a limpieza."""
        return self.path.name.lower().endswith(JUNK_EXTENSIONS_TUPLE)

def is_valid_junk_extension(filename: str) -> bool:
    """Valida la extensión de un nombre de archivo contra el catálogo de basura."""
    if not isinstance(filename, str) or not filename: return False
    return filename.lower().endswith(JUNK_EXTENSIONS_TUPLE)

def _get_win_attributes(entry: Union[os.DirEntry, Path]) -> int:
    """
    Extrae los atributos de archivo de Windows (st_file_attributes) mediante 
    la API de bajo nivel. Utiliza 'follow_symlinks=False' para no seguir enlaces peligrosos.
    """
    try:
        if isinstance(entry, os.DirEntry):
            return entry.stat(follow_symlinks=False).st_file_attributes
        return entry.lstat().st_file_attributes
    except (OSError, AttributeError, ValueError, PermissionError):
        return 0

def _is_file_read_only(path: Path) -> bool:
    """Verifica si el atributo READONLY está activo en el archivo especificado."""
    return bool(_get_win_attributes(path) & LIMITS.ATTRS.READONLY)

def _is_junction(entry: Union[os.DirEntry, Path]) -> bool:
    """
    Detecta si el archivo es un punto de reanálisis (reparse point).
    Crucial para evitar que el escáner se escape de las carpetas de usuario hacia 
    recursos del sistema vinculados.
    """
    try:
        if isinstance(entry, Path) and entry.is_symlink(): return True
        return bool(_get_win_attributes(entry) & LIMITS.ATTRS.REPARSE_POINT)
    except (OSError, AttributeError):
        return True

def _is_unc_path(path: Path) -> bool:
    """Verifica si la ruta es una ruta de red (Universal Naming Convention)."""
    if not isinstance(path, Path): return True
    path_str = str(path)
    return path_str.startswith(("\\\\", "//"))

def _generate_unique_target(target: Path) -> Path:
    """
    Genera un nombre de archivo único para mover elementos a la cuarentena,
    añadiendo un sufijo numérico para evitar colisiones con archivos ya existentes.
    """
    if not isinstance(target, Path): return target
    base_target = target
    counter = 1
    while target.exists() and counter <= 999:
        target = base_target.with_name(f"{base_target.stem}_{counter}{base_target.suffix}")
        counter += 1
    return target

def _is_allowed_directory(name: str) -> bool:
    """Valida que el nombre de carpeta no coincida con directorios críticos protegidos."""
    if not isinstance(name, str) or not name: return False
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST

def _try_open_win_file(path: Path) -> bool:
    """
    Intenta obtener un handle de archivo compartido en Windows para verificar si está bloqueado.
    0x80000000: GENERIC_READ, 0x00000003: FILE_SHARE_READ|WRITE.
    """
    if not isinstance(path, Path) or not path.exists(): return False
    path_str = str(path)
    if len(path_str) >= LIMITS.MAX_PATH_LENGTH: return False
    try:
        handle = ctypes.windll.kernel32.CreateFileW(
            path_str, 0x80000000, 0x00000003, None, 3, 0x00000080, None
        )
        if handle == wintypes.HANDLE(-1).value:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except (AttributeError, TypeError, OSError, ValueError, ctypes.WinError):
        return False

def _is_file_locked(path: Path) -> bool:
    """Verifica si un archivo está siendo utilizado exclusivamente por otro proceso."""
    if not isinstance(path, Path): return True
    try:
        if not path.exists(): return True
        if os.name == 'nt':
            return not _try_open_win_file(path)
        with open(path, "rb"): return False
    except (OSError, PermissionError, FileNotFoundError):
        return True

def _is_recursive_violation(src: Path, dest: Path) -> bool:
    """Detecta si la operación intenta mover un archivo dentro de sí mismo o a sus padres."""
    try:
        src_resolved = src.resolve(strict=False)
        dest_resolved = dest.resolve(strict=False)
        return src_resolved == dest_resolved or dest_resolved in src_resolved.parents
    except (OSError, ValueError, RuntimeError):
        return True

def _has_forbidden_chars(path: Path) -> bool:
    """Previene el uso de caracteres que causan errores en el sistema de archivos NTFS."""
    if not isinstance(path, Path): return True
    try:
        path_str = str(path).lower()
        return any(c in path_str for c in ["<", ">", "|", "\0"])
    except (ValueError, TypeError):
        return True

def _validate_path_security(src: Path, dest: Path, root: Optional[Path] = None) -> bool:
    """
    Realiza una validación exhaustiva de seguridad antes de cualquier operación de movimiento.
    Verifica: integridad, rutas prohibidas, rutas de red y límites de profundidad.
    """
    if not isinstance(src, Path) or not isinstance(dest, Path): return False
    if _is_unc_path(src) or _is_unc_path(dest): return False
    if _has_forbidden_chars(src) or _has_forbidden_chars(dest): return False
    if len(str(src)) > LIMITS.MAX_PATH_LENGTH or len(str(dest)) > LIMITS.MAX_PATH_LENGTH: return False
    
    if root and isinstance(root, Path):
        try:
            dest_res = dest.resolve(strict=False)
            if not str(dest_res).startswith(str(root.resolve(strict=False))):
                return False
        except (OSError, RuntimeError):
            return False
        
    return is_safe_to_modify(dest) and not (is_protected_path(src) or is_protected_path(dest))

def _is_system_hidden(entry: Union[os.DirEntry, Path]) -> bool:
    """Comprueba si un archivo tiene atributos de sistema u ocultos activados."""
    attrs = _get_win_attributes(entry)
    return bool(attrs & LIMITS.ATTRS.MASK_PROTECTED)

def _validate_file_identity(junk_file: JunkFile, source_path: Path) -> bool:
    """
    Compara inodos y dispositivos entre el objeto guardado y el archivo actual.
    Evita la vulnerabilidad TOCTOU (Time-of-check to time-of-use).
    """
    if not isinstance(source_path, Path): return False
    try:
        stat_result = source_path.lstat()
        return (junk_file._ino is None or stat_result.st_ino == junk_file._ino) and \
               (junk_file._dev is None or stat_result.st_dev == junk_file._dev)
    except (OSError, AttributeError):
        return False

def _can_write_to_destination(dest_real: Path, size: int) -> bool:
    """
    Verifica mediante APIs nativas que exista suficiente espacio libre en el disco de destino
    antes de iniciar una transferencia.
    """
    if not isinstance(dest_real, Path) or size < 0: return False
    try:
        target_dir = dest_real if dest_real.is_dir() else dest_real.parent
        if not target_dir.exists():
            return False
        if not os.access(target_dir, os.W_OK):
            return False
        if os.name == 'nt':
            free_bytes = ctypes.c_ulonglong(0)
            if ctypes.windll.kernel32.GetDiskFreeSpaceExW(str(target_dir), None, None, ctypes.byref(free_bytes)):
                return free_bytes.value >= (size + LIMITS.MIN_FREE_SPACE_BYTES)
            return False
        else:
            stat = os.statvfs(target_dir)
            return (stat.f_bavail * stat.f_frsize) >= (size + LIMITS.MIN_FREE_SPACE_BYTES)
    except (OSError, AttributeError, TypeError, OverflowError):
        return False

def _is_safe_for_disk_op(junk_file: JunkFile, dest: Path) -> bool:
    """
    Validación de seguridad final para el movimiento. Agrupa chequeos de permisos,
    estado de bloqueo, identidad y espacio en disco.
    """
    if not isinstance(junk_file, JunkFile) or not isinstance(dest, Path): return False
    
    try:
        source_path = junk_file.path.resolve(strict=False)
        dest_real = dest.resolve(strict=False)
        
        # Validar si el destino está en la misma unidad si es posible
        if junk_file._dev is not None:
            if dest_real.exists():
                if dest_real.stat().st_dev != junk_file._dev: return False
        
        if len(str(junk_file.path)) >= LIMITS.MAX_PATH_LENGTH or len(str(dest)) >= LIMITS.MAX_PATH_LENGTH: return False
        if not (is_safe_to_modify(source_path) and not is_protected_path(source_path)): return False
        if not (dest_real.exists() and dest_real.is_dir() and not is_protected_path(dest_real)): return False
        if (_is_recursive_violation(source_path, dest_real) or source_path.name.lower() in SYSTEM_CRITICAL_NAMES): return False
        if not os.access(dest_real, os.W_OK): return False
        
        if not _validate_file_identity(junk_file, junk_file.path) or _is_file_locked(junk_file.path): return False
        if not _can_write_to_destination(dest_real, junk_file.size_bytes): return False
        
        return True
    except (OSError, AttributeError, ValueError, PermissionError):
        return False

def _should_scan_directory(entry: os.DirEntry) -> bool:
    """Filtra directorios no elegibles para escaneo basándose en atributos de sistema y junctions."""
    try:
        if not _is_allowed_directory(entry.name) or _is_junction(entry): return False
        if _is_system_hidden(entry): return False
        return True
    except (OSError, PermissionError):
        return False

def _is_candidate_junk(entry: os.DirEntry, stats: os.stat_result) -> bool:
    """Determina si un archivo cumple los criterios para ser clasificado como basura."""
    return (
        entry.name.lower().endswith(JUNK_EXTENSIONS_TUPLE) and
        not _is_system_hidden(entry) and
        stats.st_size < LIMITS.MAX_FILE_SIZE_BYTES
    )

def _process_directory(current_path: str, junk_candidates: List[JunkFile], depth: int, visited: Set[tuple[int, int]]) -> None:
    """
    Exploración recursiva protegida contra ciclos y exceso de profundidad.
    Recopila archivos basura cumpliendo con los filtros de seguridad de `safety.py`.
    Usa un conjunto de inodos/dispositivos para evitar ciclos o re-procesamiento.
    """
    if depth > LIMITS.MAX_SCAN_DEPTH: return
    
    try:
        st = os.stat(current_path)
        dir_id = (st.st_ino, st.st_dev)
        if dir_id in visited: return
        visited.add(dir_id)
    except (OSError, PermissionError): return

    try:
        with os.scandir(current_path) as iterator:
            for entry in iterator:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_scan_directory(entry):
                            if not is_protected_path(Path(entry.path)):
                                _process_directory(entry.path, junk_candidates, depth + 1, visited)
                    else:
                        stats = entry.stat(follow_symlinks=False)
                        if _is_candidate_junk(entry, stats):
                            path_obj = Path(entry.path)
                            if is_safe_to_modify(path_obj):
                                junk_candidates.append(JunkFile(
                                    path_obj, stats.st_size, 
                                    datetime.fromtimestamp(stats.st_mtime), 
                                    stats.st_ino, stats.st_dev
                                ))
                except (PermissionError, OSError): continue
    except (PermissionError, OSError, RuntimeError): pass

def scan_for_junk(directories: Optional[Iterable[PathLike]] = None) -> List[JunkFile]:
    """Inicia el escaneo recursivo en una lista de directorios permitidos."""
    junk_candidates: List[JunkFile] = []
    visited: Set[tuple[int, int]] = set()
    scan_list = directories if directories is not None else DEFAULT_SCAN_DIRS
    if not isinstance(scan_list, (list, tuple, set)): return []
    for d in scan_list:
        if not d: continue
        try:
            p = Path(d).expanduser().resolve()
            if p.exists() and p.is_dir():
                if not _is_unc_path(p) and is_safe_to_modify(p) and not is_protected_path(p):
                    _process_directory(str(p), junk_candidates, 0, visited)
        except (OSError, RuntimeError): continue
    return junk_candidates

def sort_junk(files: Sequence[JunkFile], by: SortField = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena los archivos basura detectados según criterios de tamaño o fecha."""
    config = SORT_REGISTRY.get(by.lower(), SORT_REGISTRY["size"])
    return sorted(files, key=config.key_func, reverse=not bool(ascending))

def stage_for_review(files: Sequence[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Optional[Path]:
    """
    Traslada archivos detectados a un directorio de revisión. 
    Asegura integridad mediante el uso de `ensure_safe_to_modify` antes de cada operación atómica.
    """
    if not files or not isinstance(files, (list, tuple)) or not isinstance(review_dir, str): return None
    try:
        dest_base = Path(review_dir).expanduser().resolve()
        if not dest_base.exists(): dest_base.mkdir(parents=True, exist_ok=True)
        if not is_safe_to_modify(dest_base) or is_protected_path(dest_base) or _is_junction(dest_base): return None
        if not dest_base.is_dir() or not os.access(dest_base, os.W_OK): return None
        
        for junk_file in files:
            if not isinstance(junk_file, JunkFile) or not junk_file.path.exists(): continue
            try:
                if not _is_safe_for_disk_op(junk_file, dest_base): continue
                target_path = _can_move_file(junk_file, dest_base)
                if target_path and not target_path.exists(): 
                    ensure_safe_to_modify(junk_file.path)
                    if _validate_file_identity(junk_file, junk_file.path):
                        os.replace(str(junk_file.path), str(target_path))
            except (OSError, PermissionError, TypeError, ValueError) as e:
                logger.warning(f"Error procesando {junk_file.path.name}: {e}")
                continue
        return dest_base
    except (OSError, RuntimeError): return None

def _is_same_file_identity(path: Path, expected_ino: int, expected_dev: int) -> bool:
    """Verificación profunda de identidad de archivo mediante metadatos de sistema (inodo/dispositivo)."""
    try: 
        s = path.lstat()
        return s.st_ino == expected_ino and s.st_dev == expected_dev
    except (OSError, AttributeError): return False

def _can_move_file(junk_file: JunkFile, dest_base: Path) -> Optional[Path]:
    """Calcula un destino seguro y único para evitar sobreescritura accidental."""
    if not isinstance(junk_file.modified, datetime) or not isinstance(dest_base, Path): return None
    try:
        timestamp = int(junk_file.modified.timestamp())
        safe_name = f"{junk_file.path.stem[:128]}_{timestamp}{junk_file.path.suffix}"
        target = dest_base / safe_name
        if _has_forbidden_chars(target) or len(str(target)) >= LIMITS.MAX_PATH_LENGTH: return None
        return _generate_unique_target(target)
    except (OSError, AttributeError, ValueError, OverflowError): return None

def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina permanentemente archivos tras la validación manual del usuario.
    Verifica la integridad inodo/dispositivo antes de realizar el unlink para evitar 
    errores destructivos ante cambios de sistema durante la revisión.
    """
    if not isinstance(review_dir, str) or not review_dir: return 0
    try:
        dest = Path(review_dir).expanduser().resolve()
        if not dest.exists() or not dest.is_dir() or not is_safe_to_modify(dest) or is_protected_path(dest): return 0
        count = 0
        for item in dest.iterdir():
            if not isinstance(item, Path) or not item.is_file(): continue
            if len(str(item)) >= LIMITS.MAX_PATH_LENGTH or not str(item.resolve()).startswith(str(dest)): continue
            if item.is_symlink() or _is_junction(item) or is_protected_path(item): continue
            if not item.name.lower().endswith(JUNK_EXTENSIONS_TUPLE) or _is_file_locked(item): continue
            if not is_safe_to_modify(item): continue
            try:
                stat_before = item.lstat()
                ensure_safe_to_modify(item)
                if item.exists() and _is_same_file_identity(item, stat_before.st_ino, stat_before.st_dev):
                    item.unlink()
                    count += 1
            except (OSError, PermissionError): continue
        return count
    except (OSError, RuntimeError): return 0
