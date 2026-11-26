"""
quarantine.py — cuarentena reversible para archivos sospechosos.

ES LA PIEZA QUE UNE LIMPIADOR Y ANTIVIRUS
-----------------------------------------
Este módulo implementa un sandbox seguro para aislar archivos sospechosos detectados
por `scanner.py`. El flujo garantiza que ningún archivo se elimine permanentemente
sin intervención explícita. El mecanismo utiliza un manifiesto JSON protegido y
validaciones de integridad mediante hash SHA256 para prevenir manipulaciones.

Seguridad:
  - Todo movimiento es atómico (uso de archivos temporales + fsync).
  - Se detectan condiciones de carrera (TOCTOU) mediante comparación de inodos.
  - Se prohíbe el acceso a rutas del sistema mediante `safety.py`.
"""

from __future__ import annotations
import json
import os
import shutil
import uuid
import hashlib
import tempfile
import ctypes
import time
import fcntl
import unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Union, Dict, Any, TypeAlias, Set, Tuple, Optional, Callable, TypeVar, functools

from safety import (
    UnsafePathError,
    ensure_safe_to_modify,
    is_safe_to_modify,
    is_protected_path,
    is_within_directory,
)

# --- Tipos y Alias ---
PathLike: TypeAlias = Union[str, Path]
ManifestData: TypeAlias = List[Dict[str, Any]]
Inode: TypeAlias = int
DeviceId: TypeAlias = int
T = TypeVar("T")

# --- Decoradores de Robustez ---
def retry_io(func: Callable[..., T]) -> Callable[..., T]:
    """
    Decorador para reintentar operaciones de I/O ante fallos transitorios.
    Aplica una estrategia de retroceso exponencial para evitar colisiones.
    """
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        last_ex: Optional[Exception] = None
        for i in range(3):
            try:
                return func(*args, **kwargs)
            except (OSError, IOError, PermissionError) as e:
                last_ex = e
                time.sleep(0.05 * (2 ** i))
        raise last_ex if last_ex else IOError("Error de I/O persistente.")
    return wrapper

# --- Exportación ---
__all__: Tuple[str, ...] = (
    "QuarantineItem",
    "DEFAULT_QUARANTINE_DIR",
    "MANIFEST_NAME",
    "quarantine_dir",
    "load_manifest",
    "save_manifest",
    "quarantine_file",
    "list_items",
    "restore_item",
    "purge_item",
    "purge_all",
    "total_quarantined_bytes",
    "summarize",
)

# --- Constantes de Configuración ---
DEFAULT_QUARANTINE_DIR: str = "~/LimpiezaTotalOmega/_Cuarentena"
MANIFEST_NAME: str = "manifest.json"
CHUNK_SIZE: int = 262144
CACHE_TTL: float = 30.0
TEMP_FILE_TTL: float = 3600.0

FILE_ATTRIBUTE_READONLY: int = 0x01
FILE_ATTRIBUTE_HIDDEN: int = 0x02
FILE_ATTRIBUTE_SYSTEM: int = 0x04
FILE_ATTRIBUTE_TEMPORARY: int = 0x100
FILE_ATTRIBUTE_REPARSE_POINT: int = 0x400

_MANIFEST_CACHE: Dict[Path, Tuple[List[QuarantineItem], float, int]] = {}
_HASH_CACHE: Dict[Tuple[int, int, float], Tuple[str, float]] = {}
_WIN_ATTR_CACHE: Dict[str, Tuple[int, float]] = {}
_INODE_USE_CACHE: Dict[Tuple[int, int], Tuple[bool, float]] = {}

WINDOWS_RESERVED_NAMES: Set[str] = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", 
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", 
    "LPT6", "LPT7", "LPT8", "LPT9"
}

def _get_cache_key(st: os.stat_result) -> Tuple[int, int, float]:
    """Genera una clave única de caché basada en metadatos de sistema de archivos."""
    return (st.st_dev, st.st_ino, st.st_mtime)

def _is_file_locked(path: Path) -> bool:
    """Verifica si el archivo es escribible por el usuario actual."""
    try:
        return not os.access(path, os.W_OK)
    except OSError:
        return True

def _is_file_accessible_for_copy(path: Path) -> bool:
    """Valida si el archivo es accesible y no tiene hardlinks sospechosos."""
    try:
        st = path.stat()
        if st.st_nlink > 1: return False
        fd = os.open(path, os.O_RDONLY)
        try:
            if os.fstat(fd).st_ino != st.st_ino: return False
        finally:
            os.close(fd)
        return True
    except (OSError, PermissionError):
        return False

@retry_io
def _sync_directory(directory: Path) -> None:
    """Fuerza la persistencia de cambios en el directorio destino al disco."""
    if not directory.is_dir() or not is_safe_to_modify(directory):
        return
    
    if os.name == 'nt':
        handle = ctypes.windll.kernel32.CreateFileW(
            str(directory), 0x0001, 0x00000003, None, 3, 0x02000000, None
        )
        if handle != -1:
            try:
                ctypes.windll.kernel32.FlushFileBuffers(handle)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
    else:
        fd = -1
        try:
            fd = os.open(str(directory), os.O_RDONLY)
            os.fsync(fd)
        except OSError:
            pass
        finally:
            if fd != -1:
                os.close(fd)

def _is_path_canonical(path: Path) -> Path:
    """Resuelve la ruta absoluta normalizada evitando errores de resolución."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path

def _check_for_reparse_points(path: Path) -> None:
    """Previene seguir puntos de reparse (junctions/symlinks) de Windows."""
    if path is None: raise UnsafePathError("Ruta nula en chequeo de reparse.")
    path = _is_path_canonical(path)
    if os.name == 'nt':
        attrs = _get_win_attributes(path)
        if attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT):
            raise UnsafePathError("Ruta detectada como punto de reparse.")
    if path.is_symlink():
        raise UnsafePathError("Ruta detectada como enlace simbólico.")

def _check_path_for_junctions(path: Path) -> None:
    """Recorre la jerarquía de directorios buscando puntos de reparse."""
    curr = path
    for _ in range(16):
        _check_for_reparse_points(curr)
        if curr.parent == curr: break
        curr = curr.parent

def _get_win_attributes(path: Path) -> int:
    """Obtiene atributos de archivo en Windows con caché de corta duración."""
    if os.name != 'nt': return 0
    p_str: str = str(path)
    
    cached = _WIN_ATTR_CACHE.get(p_str)
    if cached and (time.monotonic() - cached[1] < CACHE_TTL):
        return cached[0]
            
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(p_str)
        _WIN_ATTR_CACHE[p_str] = (attrs, time.monotonic())
        return attrs
    except (OSError, AttributeError):
        return -1

def _win_has_system_attributes(path: Path) -> bool:
    """Verifica si el archivo tiene atributos de sistema o temporales activos."""
    attrs = _get_win_attributes(path)
    if attrs == -1: return True
    
    system_flags = (
        FILE_ATTRIBUTE_READONLY | 
        FILE_ATTRIBUTE_HIDDEN | 
        FILE_ATTRIBUTE_SYSTEM | 
        FILE_ATTRIBUTE_TEMPORARY
    )
    return bool(attrs & system_flags)

def _verify_source_fs_permissions(path: Path) -> None:
    """Valida que la ruta origen no sea una ruta UNC o punto de unión."""
    p_str: str = str(path)
    if p_str.startswith(r"\\") or p_str.startswith("//"):
        raise UnsafePathError("Operaciones en rutas UNC no permitidas.")
    _check_path_for_junctions(path)
    if _win_has_system_attributes(path):
        raise UnsafePathError("Archivo protegido por atributos de sistema o temporal.")

def _is_filesystem_read_only(path: Path) -> bool:
    """Verifica si el sistema de archivos deniega la escritura."""
    if path is None: return True
    if not path.exists(): return False
    return not os.access(path, os.W_OK)

@retry_io
def _is_file_exclusive(path: Path) -> bool:
    """Intenta abrir el archivo en modo exclusivo para detectar uso por terceros."""
    if path is None or not path.exists():
        return False
    
    try:
        if os.name == 'nt':
            handle = ctypes.windll.kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
            if handle == -1: return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        else:
            fd = os.open(path, os.O_RDONLY | os.O_EXCL)
            os.close(fd)
            return True
    except (OSError, PermissionError):
        return False

@dataclass
class QuarantineItem:
    item_id: str
    original_path: str
    stored_name: str
    size_bytes: int
    reason: str
    quarantined_at: str
    sha256: str = ""
    file_inode: Inode = 0
    device_id: DeviceId = 0
    mtime_snapshot: float = 0.0

    def __post_init__(self) -> None:
        try:
            self.size_bytes = int(self.size_bytes)
        except (ValueError, TypeError):
            self.size_bytes = 0

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Optional[QuarantineItem]:
        """Deserializa y valida un ítem del manifiesto JSON."""
        if not isinstance(data, dict): return None
        try:
            required = ("item_id", "original_path", "stored_name", "size_bytes", "reason", "quarantined_at")
            if not all(k in data for k in required): return None
            size = int(data["size_bytes"])
            if size < 0: return None
            raw_orig = str(data["original_path"])
            orig_p: Path = _is_path_canonical(Path(raw_orig))
            if not orig_p.is_absolute() or is_protected_path(orig_p): return None
            return cls(
                item_id=str(data["item_id"]), 
                original_path=str(orig_p), 
                stored_name=str(data["stored_name"]),
                size_bytes=size, 
                reason=str(data["reason"]), 
                quarantined_at=str(data["quarantined_at"]),
                sha256=str(data.get("sha256", "")), 
                file_inode=int(data.get("file_inode", 0)),
                device_id=int(data.get("device_id", 0)), 
                mtime_snapshot=float(data.get("mtime_snapshot", 0.0))
            )
        except (ValueError, TypeError, KeyError): 
            return None

    def _validate_integrity(self, stored_path: Path, st_stat: os.stat_result) -> bool:
        """Compara los metadatos actuales del archivo con la instantánea del manifiesto."""
        try:
            if hasattr(os, 'getuid') and st_stat.st_uid != os.getuid(): return False
            if self.file_inode != 0 and st_stat.st_ino != self.file_inode: return False
            if self.device_id != 0 and st_stat.st_dev != self.device_id: return False
            if st_stat.st_nlink > 1: return False
            if self.mtime_snapshot != 0.0 and abs(st_stat.st_mtime - self.mtime_snapshot) > 1.0: return False
            return (stored_path.is_file() and st_stat.st_size == self.size_bytes and st_stat.st_size > 0)
        except (OSError, PermissionError): return False

    def verify_integrity(self, stored_path: Path) -> bool:
        """Realiza la verificación completa de integridad (hash + metadatos)."""
        if stored_path is None or not stored_path.exists(): return False
        try:
            st = stored_path.stat()
            _check_for_reparse_points(stored_path)
            if not self._validate_integrity(stored_path, st): return False
            return bool(self.sha256 and _get_sha256(stored_path, st) == self.sha256)
        except (OSError, PermissionError): return False

def _get_sha256(path: Path, st_stat: Optional[os.stat_result] = None) -> str:
    """Calcula SHA256 usando caché para evitar re-cálculos innecesarios."""
    try:
        st = st_stat or path.stat()
        key = _get_cache_key(st)
        now = time.monotonic()
        if key in _HASH_CACHE and (now - _HASH_CACHE[key][1] < CACHE_TTL):
            return _HASH_CACHE[key][0]
        res = _compute_file_hash(path)
        _HASH_CACHE[key] = (res, now)
        return res
    except (OSError, IOError, MemoryError, PermissionError): 
        return ""

def _compute_file_hash(path: Path) -> str:
    """Lógica bruta de hashing con soporte de file_digest."""
    try:
        if path.stat().st_nlink > 1: raise OSError("Archivo modificado/concurrente.")
    except OSError: return ""
    hasher = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            if hasattr(hashlib, "file_digest"):
                try:
                    return hashlib.file_digest(f, "sha256").hexdigest()
                except (OSError, ValueError):
                    pass
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk: break
                hasher.update(chunk)
        return hasher.hexdigest()
    except (OSError, IOError, PermissionError):
        return ""

def _is_file_in_use_by_system(path: Path) -> bool:
    """Determina si un archivo está siendo ocupado por el SO o procesos con caché por inodo."""
    if path is None or not path.exists() or not path.is_file(): return False
    try:
        st = path.stat()
        key = (st.st_dev, st.st_ino)
        now = time.monotonic()
        cached = _INODE_USE_CACHE.get(key)
        if cached and (now - cached[1] < 1.0):
            return cached[0]
        
        in_use = (st.st_nlink > 1 or _win_has_system_attributes(path) or not _is_file_exclusive(path) or _is_file_locked(path))
        _INODE_USE_CACHE[key] = (in_use, now)
        return in_use
    except (OSError, PermissionError):
        return True

def _validate_unlink_constraints(path: Path, expected_hash: Optional[str], expected_inode: Inode) -> None:
    """
    Realiza chequeos críticos de seguridad previo a cualquier eliminación.
    """
    if path.name == MANIFEST_NAME: raise UnsafePathError("Protección de integridad: No se permite borrar el manifiesto.")
    ensure_safe_to_modify(path)
    if is_protected_path(path): raise UnsafePathError("Protección de seguridad: ruta protegida bloqueada.")
    if is_protected_path(path.parent): raise UnsafePathError("Protección de jerarquía: directorio padre no seguro.")
    _check_path_for_junctions(path)
    if len(str(path)) > 260 or ":" in path.name: 
        raise UnsafePathError("Ruta protegida, ADS o longitud excedida.")
    try:
        st: os.stat_result = path.stat()
    except OSError: raise UnsafePathError("Inaccesibilidad durante validación de unlink.")
    if st.st_nlink > 1 or (expected_inode != 0 and st.st_ino != expected_inode): raise UnsafePathError("Inconsistencia en inodo o enlaces.")
    if hasattr(os, 'getuid') and st.st_uid != os.getuid(): raise UnsafePathError("Error de propiedad de archivo.")
    if expected_hash and _get_sha256(path, st) != expected_hash: raise UnsafePathError("Falla de integridad: hash mismatch.")
    if _is_file_in_use_by_system(path): raise UnsafePathError("Archivo en uso por el sistema.")

def _validate_file_for_unlink(path: Path, expected_hash: Optional[str], expected_inode: Inode, expected_device: DeviceId = 0) -> None:
    """Wrapper de validación antes de unlink."""
    if path is None or not path.exists(): raise UnsafePathError("Ruta nula o inexistente para unlink.")
    _validate_unlink_constraints(path, expected_hash, expected_inode)
    try:
        if expected_device != 0 and path.stat().st_dev != expected_device: raise UnsafePathError("Falla de consistencia de dispositivo.")
    except OSError: raise UnsafePathError("Falla al consultar dispositivo.")

@retry_io
def _safe_unlink(path: Path, expected_hash: Optional[str] = None, expected_inode: Inode = 0, expected_device: DeviceId = 0) -> bool:
    """Eliminación segura con validación previa de metadatos e integridad."""
    if not path or not path.exists() or not path.is_file(): return True
    _validate_file_for_unlink(path, expected_hash, expected_inode, expected_device)
    parent: Path = path.parent
    path.unlink()
    if parent.exists():
        _sync_directory(parent)
    return not path.exists()

def _check_path_syntax_integrity(path: Path) -> None:
    """Valida la sintaxis de la ruta contra inyecciones y caracteres prohibidos."""
    if path is None: raise UnsafePathError("Ruta nula recibida.")
    path = _is_path_canonical(path)
    path_str: str = os.fspath(path)
    if len(path_str) > 260: raise UnsafePathError("Ruta de sistema demasiado larga.")
    if any(ord(c) < 32 for c in path_str) or "\0" in path_str: raise UnsafePathError("Ruta con caracteres de control.")
    if "~" in path_str[1:]: raise UnsafePathError("Inyección de tilde detectada.")
    if len(path.parts) > 32: raise UnsafePathError("Profundidad de ruta excesiva.")
    if ":" in path.name: raise UnsafePathError("Ruta con flujos de datos alternos (ADS) prohibidos.")
    if path.name.upper() in WINDOWS_RESERVED_NAMES: raise UnsafePathError("Nombre de archivo reservado por el sistema.")
    if path.name != unicodedata.normalize("NFC", path.name): raise UnsafePathError("Normalización Unicode inválida.")
    if path.name.endswith(" ") or path.name.endswith("."): raise UnsafePathError("Ruta con terminación peligrosa.")
    _check_path_for_junctions(path)

def _sanitize_filename(filename: str) -> str:
    """Limpia nombres de archivo para evitar caracteres maliciosos."""
    return "".join(c for c in filename if c.isalnum() or c in "._-").lstrip(".")

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """Genera un nombre de archivo seguro para el sandbox de cuarentena."""
    if not item_id: raise ValueError("ID de ítem requerido.")
    sanitized: str = _sanitize_filename(original_path.name)
    if not sanitized: sanitized = "unknown_file"
    parts: List[str] = sanitized.split('.')
    name_base: str = parts[0] if parts[0] else "q_file"
    if name_base.upper() in WINDOWS_RESERVED_NAMES: name_base = f"q_{name_base}"
    extension: str = f".{parts[-1]}" if len(parts) > 1 else ""
    candidate: str = f"{item_id}__{name_base[:64]}{extension}".replace(":", "_")[:128]
    if candidate.split('.')[0].upper() in WINDOWS_RESERVED_NAMES: candidate = f"safe_{candidate}"
    return candidate

def _ensure_path_ownership(path: Path) -> None:
    """Verifica que el usuario actual es dueño del directorio de cuarentena."""
    if path is None or not path.exists(): raise UnsafePathError("Ruta nula o inexistente en ownership.")
    if hasattr(os, 'getuid'):
        try:
            if path.stat().st_uid != os.getuid(): raise UnsafePathError("Propiedad de directorio no coincide con usuario.")
        except OSError: raise UnsafePathError("No se pudo verificar la propiedad del directorio.")

def quarantine_dir(base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Retorna y prepara el directorio base de cuarentena, validando su seguridad."""
    if not base or not str(base).strip(): raise ValueError("El directorio base no puede estar vacío.")
    try:
        path: Path = _is_path_canonical(Path(str(base)).expanduser())
    except Exception as e:
        raise UnsafePathError(f"Error resolviendo ruta: {e}")
    if len(path.parts) <= 1 or path.parent == path:
        raise UnsafePathError("No se permite que el sandbox sea una raíz de disco.")
    if ":" in path.name or any(part.upper() in WINDOWS_RESERVED_NAMES for part in path.parts):
        raise UnsafePathError("Ruta de cuarentena contiene elementos reservados o ADS.")
    if len(str(path)) > 240: raise UnsafePathError("Ruta de cuarentena excede límites de seguridad.")
    if len(path.parts) > 16: raise UnsafePathError("Profundidad de ruta de cuarentena excesiva.")
    _check_path_for_junctions(path)
    if is_protected_path(path): raise UnsafePathError("Directorio de cuarentena reside en ruta protegida.")
    if path.exists() and not path.is_dir():
        raise UnsafePathError("Ruta de cuarentena bloqueada por un archivo existente.")
    if path.exists():
        if not is_safe_to_modify(path): raise UnsafePathError("Ruta de cuarentena marcada como insegura.")
        if not os.access(path, os.W_OK | os.R_OK): raise PermissionError("Directorio inaccesible.")
        if _is_filesystem_read_only(path): raise PermissionError("Permisos insuficientes en directorio.")
    else:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"Fallo crítico al crear directorio: {e}")
        if not path.exists() or not path.is_dir(): raise OSError("Fallo al asegurar directorio de cuarentena.")
    try:
        now = time.time()
        for f in path.glob(".tmp_*"):
            if now - f.stat().st_mtime > TEMP_FILE_TTL:
                f.unlink()
    except OSError: pass
    _ensure_path_ownership(path)
    return path

def _manifest_path(base_dir: Path) -> Path:
    """Construye la ruta absoluta al manifiesto."""
    return (base_dir / MANIFEST_NAME)

def _is_within_quarantine_sandbox(path: Path, root: Path) -> bool:
    """Verifica si una ruta está estrictamente contenida dentro del sandbox."""
    if path is None or root is None: return False
    return is_within_directory(_is_path_canonical(path), _is_path_canonical(root))

def _check_device_consistency(source: Path, target_directory: Path) -> None:
    """Asegura que el origen y destino estén en el mismo volumen físico."""
    if not isinstance(source, Path) or not isinstance(target_directory, Path):
        raise TypeError("Las rutas deben ser instancias de pathlib.Path.")
    if _is_path_canonical(source).stat().st_dev != _is_path_canonical(target_directory).stat().st_dev:
        raise UnsafePathError("Operación entre distintos volúmenes no permitida.")

def _check_isolation_safety_strict(source_abs: Path, target_abs: Path) -> None:
    """Valida condiciones estrictas de seguridad antes de aislar un archivo."""
    _check_for_reparse_points(source_abs)
    if not source_abs.is_file(): raise UnsafePathError("Solo se permiten archivos regulares.")
    if source_abs.stat().st_size == 0: raise UnsafePathError("Archivos vacíos prohibidos.")
    if _is_filesystem_read_only(target_abs): raise PermissionError("Directorio de cuarentena sin permisos de escritura.")
    if not _is_file_accessible_for_copy(source_abs) or not _is_file_exclusive(source_abs):
        raise IOError("Archivo origen bloqueado o en uso exclusivo.")
    if hasattr(os, 'getuid'):
        if source_abs.stat().st_uid != os.getuid(): raise UnsafePathError("Aislamiento denegado: propiedad incorrecta.")
    _check_device_consistency(source_abs, target_abs)

def _validate_isolation_request(source_path: Path, target_directory: Path) -> None:
    """Valida los prerrequisitos antes de mover un archivo a cuarentena."""
    _check_path_for_junctions(target_directory)
    if not target_directory.is_dir() or not is_safe_to_modify(target_directory): 
        raise UnsafePathError("Destino de cuarentena debe ser un directorio seguro.")
    source_path = _is_path_canonical(source_path)
    if not source_path.exists() or not source_path.is_file():
        raise UnsafePathError("El origen debe ser un archivo existente.")
    if source_path.stat().st_size == 0:
        raise UnsafePathError("Aislamiento de archivos vacíos prohibido.")
    if ":" in source_path.name or source_path.name.endswith(" ") or source_path.name.endswith("."):
        raise UnsafePathError("Nombre de archivo contiene caracteres prohibidos o ADS.")
    _check_path_syntax_integrity(source_path)
    _verify_source_fs_permissions(source_path)
    _check_for_reparse_points(source_path)
    if _win_has_system_attributes(source_path): 
        raise UnsafePathError("Aislamiento denegado: archivo con atributos de sistema.")
    if len(str(target_directory)) > 240: 
        raise UnsafePathError("Ruta de cuarentena demasiado larga.")
    resolved_target = _is_path_canonical(target_directory)
    if is_within_directory(source_path, resolved_target) or source_path == resolved_target:
        raise UnsafePathError("Archivo origen ya reside dentro del sandbox.")
    _ensure_disk_space(target_directory, source_path.stat().st_size)
    _check_isolation_safety_strict(source_path, resolved_target)

def load_manifest(base: PathLike = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Carga y valida el manifiesto de archivos aislados con bloqueo de archivos."""
    try: 
        base_dir: Path = quarantine_dir(base)
    except (OSError, UnsafePathError): 
        return []
    m_path: Path = _manifest_path(base_dir)
    if not m_path.exists():
        _MANIFEST_CACHE[base_dir] = ([], 0.0, 0)
        return []
    
    try:
        with open(m_path, "r+", encoding="utf-8") as f:
            if hasattr(fcntl, 'flock'):
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            
            st = os.fstat(f.fileno())
            if not force_reload and base_dir in _MANIFEST_CACHE:
                cached_items, cached_mtime, cached_size = _MANIFEST_CACHE[base_dir]
                if cached_mtime == st.st_mtime and cached_size == st.st_size:
                    return cached_items
            
            raw_data = json.load(f)
            if not isinstance(raw_data, list): raise TypeError("Formato de manifiesto inválido.")
            valid_items: List[QuarantineItem] = []
            for entry in raw_data:
                item = QuarantineItem.from_dict(entry)
                if item: valid_items.append(item)
            _MANIFEST_CACHE[base_dir] = (valid_items, st.st_mtime, st.st_size)
            return valid_items
    except (OSError, FileNotFoundError, json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return []

@retry_io
def save_manifest(items: List[QuarantineItem], base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persiste el estado del manifiesto de forma atómica y bloqueada."""
    base_path: Path = quarantine_dir(base)
    if not is_safe_to_modify(base_path): raise UnsafePathError("Directorio de manifiesto no seguro.")
    target_path: Path = _manifest_path(base_path)
    serializable = [item.to_dict() for item in items if isinstance(item, QuarantineItem)]
    
    temp_fd, temp_name = tempfile.mkstemp(dir=base_path, text=False)
    try:
        with os.fdopen(temp_fd, 'wb') as f:
            if hasattr(fcntl, 'flock'):
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(serializable, indent=2, ensure_ascii=False).encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, target_path)
    except (OSError, IOError) as e:
        if os.path.exists(temp_name): os.remove(temp_name)
        raise RuntimeError(f"Falla crítica al persistir manifiesto: {e}")
        
    _sync_directory(base_path)
    st = target_path.stat()
    _MANIFEST_CACHE[base_path] = (items, st.st_mtime, st.st_size)
    return target_path

def _ensure_disk_space(dest_dir: Path, required_size: int) -> None:
    """Verifica la disponibilidad de espacio en disco antes de mover datos."""
    if not isinstance(required_size, int) or required_size < 0: raise ValueError("Tamaño requerido inválido.")
    dest_dir = _is_path_canonical(dest_dir)
    if not dest_dir.exists(): raise FileNotFoundError(f"Directorio inexistente: {dest_dir}")
    if not dest_dir.is_dir() or not is_safe_to_modify(dest_dir) or not os.access(dest_dir, os.W_OK | os.R_OK): 
        raise PermissionError(f"Directorio inaccesible: {dest_dir}")
    usage = shutil.disk_usage(dest_dir)
    if usage.free < 0: raise OSError("Error de lectura de espacio en disco.")
    margin: int = max(int(required_size * 0.05), 5 * 1024 * 1024)
    if usage.free < (required_size + margin): raise OSError("Espacio insuficiente en disco.")

def _validate_file_transfer_preconditions(source: Path, destination: Path) -> None:
    """Valida prerrequisitos de seguridad para el archivo destino."""
    if is_protected_path(destination): raise UnsafePathError("Destino en ruta protegida.")
    if not is_safe_to_modify(destination.parent): raise UnsafePathError("Directorio destino no seguro.")
    _check_device_consistency(source, destination.parent)
    _verify_source_fs_permissions(destination)
    if not source.exists() or not source.is_file(): raise OSError("Archivo origen inaccesible.")
    if os.name == 'nt' and _win_has_system_attributes(source): raise PermissionError("Archivo origen restringido.")
    if destination.exists():
        if os.path.samefile(source, destination): raise UnsafePathError("Operación circular detectada.")
        raise FileExistsError(f"El destino ya existe: {destination}")

@retry_io
def _create_temp_file(source: Path, destination: Path) -> Path:
    """Crea un archivo temporal seguro para la transferencia atómica."""
    parent = destination.parent
    ensure_safe_to_modify(parent)
    fd, temp_path_str = tempfile.mkstemp(dir=parent, prefix=".tmp_", text=False)
    temp_path = Path(temp_path_str)
    try:
        if temp_path.is_symlink():
            raise UnsafePathError("Ataque de enlace detectado en archivo temporal.")
        os.close(fd)
        os.chmod(temp_path, 0o600)
    except Exception:
        if os.path.exists(temp_path_str): os.remove(temp_path_str)
        raise
    return temp_path

def _verify_copied_data(source_stat: os.stat_result, dest_path: Path, source_hash: str) -> None:
    """Verifica la integridad del archivo tras la copia."""
    if dest_path.stat().st_size != source_stat.st_size: raise OSError("Falla de integridad: tamaño mismatch.")
    final_hash: str = _get_sha256(dest_path)
    if not final_hash or final_hash != source_hash: raise OSError("Falla crítica: el hash del archivo copiado no coincide.")

@retry_io
def _perform_secure_copy(source: Path, temporary_isolation_path: Path, source_hash: str) -> None:
    """Copia segura bloque a bloque hacia el archivo temporal."""
    st = source.stat()
    if not (st.st_mode & 0o100000): raise OSError("El origen no es un archivo regular.")
    _check_for_reparse_points(source)
    if st.st_nlink > 1: raise UnsafePathError("Archivo origen con enlaces múltiples.")
    _ensure_disk_space(temporary_isolation_path.parent, st.st_size)
    ensure_safe_to_modify(temporary_isolation_path.parent)
    with open(source, "rb") as f_src:
        with open(temporary_isolation_path, "wb") as f_dst:
            if os.path.islink(temporary_isolation_path) or _get_win_attributes(temporary_isolation_path) & FILE_ATTRIBUTE_REPARSE_POINT:
                raise UnsafePathError("Inyección de enlace durante copia.")
            shutil.copyfileobj(f_src, f_dst)
            f_dst.flush()
            os.fsync(f_dst.fileno()) 
    post_st = temporary_isolation_path.stat()
    if post_st.st_nlink != 1: raise UnsafePathError("Integridad comprometida: temporal con enlaces adicionales.")
    try: os.chmod(temporary_isolation_path, 0o600)
    except OSError: pass
    _verify_copied_data(st, temporary_isolation_path, source_hash)

def _write_temp_to_final(source_path: Path, dest_path: Path) -> Tuple[str, Inode, DeviceId, float]:
    """Gestiona la lógica de transferencia temporal a definitiva."""
    _check_path_syntax_integrity(dest_path)
    _validate_file_transfer_preconditions(source_path, dest_path)
    if not source_path.is_file(): raise UnsafePathError("El origen ha dejado de ser un archivo.")
    source_hash: str = _get_sha256(source_path)
    temporary_isolation_path: Path = _create_temp_file(source_path, dest_path)
    try:
        _perform_secure_copy(source_path, temporary_isolation_path, source_hash)
        if dest_path.exists(): raise FileExistsError("Colisión de ruta durante transferencia.")
        if not is_safe_to_modify(dest_path.parent): raise UnsafePathError("Destino final en ruta insegura.")
        if temporary_isolation_path.stat().st_nlink != 1: raise UnsafePathError("Integridad comprometida en temporal.")
        os.replace(temporary_isolation_path, dest_path)
        _sync_directory(dest_path.parent)
        dest_stat = dest_path.stat()
        if dest_stat.st_nlink > 1: raise UnsafePathError("Integridad comprometida: destino tiene enlaces físicos inesperados.")
        return source_hash, dest_stat.st_ino, dest_stat.st_dev, dest_stat.st_mtime
    except (OSError, IOError, UnsafePathError, FileExistsError) as e:
        if temporary_isolation_path.exists():
            try: os.remove(temporary_isolation_path)
            except OSError: pass
        if dest_path.exists() and not os.path.samefile(source_path, dest_path): _safe_unlink(dest_path)
        raise RuntimeError(f"Error crítico en transferencia: {e}")

class IsolationManager:
    """Gestor de aislamiento con validaciones atómicas de estado."""
    @staticmethod
    def validate_atomicity(source: Path, original_stat: os.stat_result, dest: Path) -> None:
        """Verifica que el estado del archivo origen no cambió durante la operación (TOCTOU)."""
        if source is None or not source.exists(): 
            raise FileNotFoundError("Archivo origen eliminado antes del aislamiento.")
        current_stat = source.stat()
        if current_stat.st_ino != original_stat.st_ino or current_stat.st_dev != original_stat.st_dev: 
            raise RuntimeError("Integridad comprometida: archivo reemplazado o movido.")
        if current_stat.st_mtime != original_stat.st_mtime:
            raise RuntimeError("Integridad comprometida: el archivo ha sido modificado (TOCTOU).")
        if current_stat.st_nlink > 1: 
            raise UnsafePathError("Aislamiento denegado: enlaces físicos detectados.")
        dest_res: Path = _is_path_canonical(dest)
        base_res: Path = _is_path_canonical(dest.parent)
        if not is_within_directory(dest_res, base_res): 
            raise UnsafePathError("Intento de escape del sandbox.")
        ensure_safe_to_modify(dest.parent)

def _verify_post_isolation_integrity(dest_path: Path, expected_hash: str, expected_inode: Inode, expected_device: DeviceId) -> None:
    """Verificación de seguridad tras completar el aislamiento."""
    dest_stat = dest_path.stat()
    if dest_stat.st_ino != expected_inode or dest_stat.st_dev != expected_device or dest_stat.st_nlink > 1:
        raise RuntimeError("Integridad comprometida: destino inconsistente.")
    if _get_sha256(dest_path) != expected_hash:
        raise RuntimeError("Integridad comprometida: hash mismatch.")
    if _win_has_system_attributes(dest_path):
        raise UnsafePathError("Integridad comprometida: atributos de sistema no permitidos en sandbox.")

@retry_io
def _check_file_metadata_consistency(source: Path, original_size: int, base: Path) -> os.stat_result:
    """
    Verifica consistencia de metadatos para prevenir ataques TOCTOU.
    """
    try:
        stat_orig = source.stat()
    except OSError as e:
        raise RuntimeError(f"Falla al obtener metadatos: {e}")
    if stat_orig.st_size != original_size or stat_orig.st_nlink > 1: 
        raise RuntimeError("Integridad comprometida (TOCTOU).")
    existing_items = load_manifest(base)
    if any(i.file_inode == stat_orig.st_ino and i.device_id == stat_orig.st_dev for i in existing_items): 
        raise RuntimeError("Colisión de inodo: el archivo ya existe en cuarentena.")
    return stat_orig

def _atomic_isolate_file(source: Path, destination: Path, original_size: int) -> Tuple[str, Inode, DeviceId, float]:
    """Ejecuta el flujo completo de aislamiento con garantías de atomicidad."""
    if not isinstance(source, Path) or not isinstance(destination, Path):
        raise UnsafePathError("Entradas de ruta inválidas en aislamiento.")
    _check_path_syntax_integrity(destination)
    _check_path_syntax_integrity(source)
    if not source.exists(): raise FileNotFoundError("Archivo origen no existe.")
    if original_size <= 0: raise UnsafePathError("Archivo origen vacío.")
    if source.is_symlink(): raise UnsafePathError("Aislamiento denegado: enlace simbólico.")
    if _win_has_system_attributes(source) or not source.is_file(): 
        raise UnsafePathError("Atributos inválidos o no es archivo regular.")
    resolved_source = _is_path_canonical(source)
    resolved_dest = _is_path_canonical(destination)
    if is_within_directory(resolved_source, resolved_dest.parent) or is_within_directory(source, destination.parent):
        raise UnsafePathError("Colisión de rutas.")
    ensure_safe_to_modify(destination.parent)
    try:
        stat_orig = _check_file_metadata_consistency(source, original_size, destination.parent.parent)
        IsolationManager.validate_atomicity(source, stat_orig, destination)
        if destination.exists(): raise FileExistsError("Destino ya presente.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        h, i, d, mtime = _write_temp_to_final(source, destination)
        ensure_safe_to_modify(destination, allow_sensitive=True)
        _verify_post_isolation_integrity(destination, h, i, d)
        return h, i, d, mtime
    except (OSError, UnsafePathError, RuntimeError) as e:
        raise RuntimeError(f"Falla durante la operación atómica: {e}")

def _register_quarantine_item(destination: Path, source_path: Path, file_hash: str, file_inode: Inode, device_id: DeviceId, mtime: float, reason: str, original_size: int, base: PathLike) -> QuarantineItem:
    """Registra el archivo aislado en el manifiesto de la aplicación."""
    if destination is None or not destination.exists(): raise RuntimeError("Falla: archivo inexistente.")
    items_list: List[QuarantineItem] = load_manifest(base)
    dest_stat = destination.stat()
    if dest_stat.st_ino != file_inode or dest_stat.st_dev != device_id or dest_stat.st_nlink > 1: raise RuntimeError("Inconsistencia: mismatch inodo/dispositivo.")
    quarantine_item: QuarantineItem = QuarantineItem(
        item_id=uuid.uuid4().hex[:12], original_path=str(_is_path_canonical(source_path)), stored_name=destination.name,
        size_bytes=original_size, reason=reason if reason else "Sin motivo", quarantined_at=datetime.now().isoformat(timespec="seconds"),
        sha256=file_hash, file_inode=file_inode, device_id=device_id, mtime_snapshot=mtime
    )
    items_list.append(quarantine_item)
    save_manifest(items_list, base)
    return quarantine_item

def _validate_input_path(source: PathLike) -> Path:
    """Auditoría de seguridad básica sobre la ruta origen antes de procesar."""
    if source is None: raise ValueError("Ruta de origen nula.")
    path_val = str(source).strip()
    if not path_val: raise ValueError("Ruta de origen vacía.")
    p_source: Path = _is_path_canonical(Path(path_val))
    if is_protected_path(p_source): raise UnsafePathError("Acceso prohibido.")
    if path_val.startswith(r"\\") or path_val.startswith("//"): raise UnsafePathError("Rutas UNC no permitidas.")
    if len(path_val) > 260: raise UnsafePathError("Ruta de entrada excede límites.")
    if len(p_source.parts) > 32: raise UnsafePathError("Profundidad de ruta excesiva.")
    if p_source == p_source.anchor: raise UnsafePathError("No se permite aislar raíz.")
    if not p_source.exists(): raise FileNotFoundError("Archivo origen no encontrado.")
    _check_for_reparse_points(p_source)
    if not p_source.is_file(): raise ValueError("El origen debe ser un archivo regular.")
    try:
        if p_source.stat().st_size <= 0: raise ValueError("Archivo origen vacío.")
    except OSError: raise ValueError("No se pudo auditar tamaño.")
    if not os.access(p_source, os.R_OK): raise PermissionError("Sin permisos de lectura.")
    return p_source

def quarantine_file(source: PathLike, reason: str = "Marcado como sospechoso", base: PathLike = DEFAULT_QUARANTINE_DIR) -> QuarantineItem:
    """
    Punto de entrada para aislar un archivo en cuarentena.
    Realiza auditoría de origen, validaciones de espacio y aislamiento atómico.
    """
    p_source: Path = _validate_input_path(source)
    dest_dir: Path = quarantine_dir(base)
    
    # Validaciones preventivas de estado antes de operar
    if not dest_dir.exists(): 
        raise RuntimeError("Directorio de cuarentena no disponible.")
    if not os.access(dest_dir, os.W_OK):
        raise PermissionError("Directorio de cuarentena sin escritura.")
    
    try:
        initial_stat = p_source.stat()
    except OSError as e:
        raise RuntimeError(f"Falla al auditar archivo origen: {e}")
        
    _ensure_disk_space(dest_dir, initial_stat.st_size)
    
    if _is_filesystem_read_only(p_source.parent): 
        raise OSError("Volumen origen read-only.")
    if _win_has_system_attributes(p_source): 
        raise PermissionError("Archivo restringido.")
    if _is_path_canonical(p_source).stat().st_dev != dest_dir.stat().st_dev: 
        raise UnsafePathError("Unidades distintas no soportadas.")
        
    _validate_isolation_request(p_source, dest_dir)
    
    destination: Path = dest_dir / _generate_safe_stored_name(p_source, uuid.uuid4().hex[:12])
    if destination.exists():
        if destination.is_dir(): raise UnsafePathError(f"Colisión con directorio: {destination}")
        if os.path.samefile(p_source, destination): raise UnsafePathError("Origen y destino idénticos.")
        raise FileExistsError(f"Colisión de destino: {destination}")
        
    try:
        file_hash, file_inode, dev_id, mtime = _atomic_isolate_file(p_source, destination, initial_stat.st_size)
        item: QuarantineItem = _register_quarantine_item(destination, p_source, file_hash, file_inode, dev_id, mtime, reason, initial_stat.st_size, base)
    except Exception as e:
        if destination.exists():
            try: os.remove(destination)
            except OSError: pass
        raise RuntimeError(f"Falla crítica en aislamiento: {e}")
    if p_source.exists() and not os.path.samefile(p_source, destination):
        try: p_source.unlink()
        except (OSError, PermissionError) as e: raise RuntimeError(f"Falla al remover original: {e}")
    return item

def list_items(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Lista los ítems en cuarentena, ordenados por fecha descendente."""
    items = load_manifest(base)
    return sorted(items, key=lambda x: x.quarantined_at, reverse=True)

def restore_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Restaura un archivo aislado a su ubicación original.
    Verifica integridad post-almacenamiento antes de mover el archivo.
    """
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID de ítem inválido.")
    base_path: Path = quarantine_dir(base)
    manifest: List[QuarantineItem] = load_manifest(base)
    quarantine_item: Optional[QuarantineItem] = next((i for i in manifest if i.item_id == item_id), None)
    if not quarantine_item: 
        raise ValueError(f"Ítem no encontrado en manifiesto: {item_id}")
    stored_file: Path = base_path / quarantine_item.stored_name
    if not stored_file.exists() or not quarantine_item.verify_integrity(stored_file):
        items: List[QuarantineItem] = load_manifest(base, force_reload=True)
        save_manifest([i for i in items if i.item_id != item_id], base)
        raise RuntimeError(f"Archivo corrupto o integridad fallida: {item_id}")
    destination: Path = _is_path_canonical(Path(quarantine_item.original_path))
    _check_path_syntax_integrity(destination)
    if destination.exists(): raise FileExistsError(f"Destino ya existe: {destination}")
    if is_protected_path(destination): raise UnsafePathError("Destino protegido.")
    parent: Path = destination.parent
    if not parent.exists():
        try: parent.mkdir(parents=True, exist_ok=True)
        except OSError as e: raise RuntimeError(f"Falla recreando carpetas: {e}")
    elif not parent.is_dir():
        raise RuntimeError(f"Destino no es carpeta: {parent}")
    ensure_safe_to_modify(parent)
    _ensure_disk_space(parent, quarantine_item.size_bytes)
    ensure_safe_to_modify(destination)
    try:
        os.replace(str(stored_file), str(destination))
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Falla crítica al mover el archivo desde cuarentena: {e}")
    for p in [parent, base_path]:
        if p and p.exists(): _sync_directory(p)
    items = load_manifest(base, force_reload=True)
    save_manifest([i for i in items if i.item_id != item_id], base)
    return destination

def purge_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> bool:
    """Elimina permanentemente un archivo de la cuarentena."""
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID inválido.")
    try: 
        base_path: Path = quarantine_dir(base)
    except (OSError, UnsafePathError): return False
    items: List[QuarantineItem] = load_manifest(base)
    quarantine_item: Optional[QuarantineItem] = next((i for i in items if i.item_id == item_id), None)
    if quarantine_item is None: return False
    stored_file: Path = base_path / quarantine_item.stored_name
    if not stored_file.exists():
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
    if not quarantine_item.verify_integrity(stored_file):
        raise UnsafePathError(f"Integridad fallida: {item_id}.")
    if _safe_unlink(stored_file, expected_hash=quarantine_item.sha256, expected_inode=quarantine_item.file_inode, expected_device=quarantine_item.device_id):
        save_manifest([i for i in load_manifest(base, force_reload=True) if i.item_id != item_id], base)
        return True
    return False

def _is_item_purgable(entry: os.DirEntry, item: QuarantineItem) -> bool:
    """Helper para verificar si un ítem es apto para purga."""
    try:
        p = Path(entry.path)
        _validate_file_for_unlink(p, item.sha256, item.file_inode, item.device_id)
        return _safe_unlink(p, expected_hash=item.sha256, expected_inode=item.file_inode, expected_device=item.device_id)
    except (OSError, PermissionError, UnsafePathError): return False

def purge_all(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Limpia todos los ítems de cuarentena expirados o purgados."""
    try:
        root_path: Path = quarantine_dir(base)
        items = load_manifest(base)
        
        valid_items_map = {item.stored_name: item for item in items}
        purgado_ids: Set[str] = set()
        now = time.time()
        
        with os.scandir(root_path) as it:
            for entry in it:
                if entry.name in valid_items_map:
                    if _is_item_purgable(entry, valid_items_map[entry.name]):
                        purgado_ids.add(valid_items_map[entry.name].item_id)
                elif entry.name.startswith(".tmp_"):
                    if now - entry.stat().st_mtime > TEMP_FILE_TTL:
                        try:
                            tmp_path = Path(entry.path)
                            # Seguridad: Solo borrar si la ruta es local, no es directorio, es segura y pertenece al sandbox.
                            if (is_safe_to_modify(tmp_path) and 
                                not is_protected_path(tmp_path) and 
                                not tmp_path.is_dir() and 
                                not _is_file_in_use_by_system(tmp_path) and 
                                is_within_directory(tmp_path, root_path)):
                                os.unlink(entry.path)
                        except (OSError, PermissionError, UnsafePathError): pass
        
        if purgado_ids:
            new_items = [i for i in items if i.item_id not in purgado_ids]
            save_manifest(new_items, base)
            _sync_directory(root_path)
        return len(purgado_ids)
    except (OSError, PermissionError, UnsafePathError, RuntimeError): return 0

def total_quarantined_bytes(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el espacio total ocupado por los archivos en cuarentena."""
    return sum(item.size_bytes for item in load_manifest(base))

def summarize(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte legible de los archivos actuales en cuarentena."""
    items: List[QuarantineItem] = load_manifest(base)
    if not items: return ["La cuarentena está vacía."]
    total_mb: float = sum(i.size_mb for i in items)
    lines: List[str] = [f"{len(items)} archivo(s) en cuarentena — {total_mb:.2f} MB", ""]
    for item in items:
        lines.append(f"  [{item.item_id}] {Path(item.original_path).name} — {item.size_mb} MB")
        lines.append(f"      Motivo: {item.reason}")
        lines.append(f"      Origen: {item.original_path}")
        lines.append(f"      Aislado: {item.quarantined_at}")
    lines.extend(["", "Los archivos se pueden restaurar a su ubicación original."])
    return lines
