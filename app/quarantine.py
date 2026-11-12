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
from typing import List, Union, Dict, Any, TypeAlias, Set, Tuple, Optional, Callable, TypeVar

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
T = TypeVar("T")

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
CHUNK_SIZE: int = 262144  # 256KB para streaming eficiente de archivos grandes
_MANIFEST_CACHE: Dict[Path, Tuple[List[QuarantineItem], float, int]] = {}
_HASH_CACHE: Dict[Tuple[int, int, float, float], Tuple[str, float]] = {}
_WIN_ATTR_CACHE: Dict[str, Tuple[int, float]] = {}
_INODE_USE_CACHE: Dict[Tuple[int, int, float], Tuple[bool, float]] = {}

WINDOWS_RESERVED_NAMES: Set[str] = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", 
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", 
    "LPT6", "LPT7", "LPT8", "LPT9"
}

def _sync_directory(directory: Path) -> None:
    """
    Realiza una operación de fsync sobre el directorio padre para asegurar que
    los metadatos (entradas de directorio, renombrados) se escriban físicamente
    en el medio de almacenamiento, evitando pérdidas de datos ante fallos.
    """
    if not directory.is_dir():
        return
    fd = -1
    try:
        dir_abs = _is_path_canonical(directory)
        fd = os.open(str(dir_abs), os.O_RDONLY)
        os.fsync(fd)
    except (OSError, PermissionError):
        pass
    finally:
        if fd != -1:
            os.close(fd)

def _is_path_canonical(path: Path) -> Path:
    """
    Resuelve rutas relativas y elimina componentes simbólicos para obtener 
    la ruta absoluta final. Se usa strict=False para permitir la validación 
    de rutas que aún no existen (ej. destinos de cuarentena).
    """
    try:
        if not path.is_absolute():
            path = path.resolve()
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path

def _check_for_reparse_points(path: Path) -> None:
    """
    Verifica que la ruta no sea un punto de reanálisis (Junction, Symlink, Reparse Point).
    Impide que el sistema de archivos siga enlaces que escapen del control de la app.
    """
    if path is None: raise UnsafePathError("Ruta nula en chequeo de reparse.")
    path = _is_path_canonical(path)
    if os.name == 'nt':
        try:
            attrs: int = _get_win_attributes(path)
            if attrs != -1 and (attrs & 0x400):
                raise UnsafePathError("Ruta detectada como punto de reparse.")
        except (OSError, AttributeError):
            pass
    if path.is_symlink():
        raise UnsafePathError("Ruta detectada como enlace simbólico.")

def _check_path_for_junctions(path: Path) -> None:
    """Wrapper de validación estructural para jerarquías de directorios."""
    _check_for_reparse_points(path)

def _get_win_attributes(path: Path) -> int:
    """
    Obtiene atributos de archivo en Windows mediante llamadas a la API nativa.
    Implementa una caché de 1s para optimizar múltiples accesos en un solo ciclo.
    """
    if os.name != 'nt': return 0
    p_str: str = str(path)
    now: float = time.monotonic()
    
    if p_str in _WIN_ATTR_CACHE:
        attrs, ts = _WIN_ATTR_CACHE[p_str]
        if now - ts < 1.0:
            return attrs
            
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(p_str)
        _WIN_ATTR_CACHE[p_str] = (attrs, now)
        return attrs
    except (OSError, AttributeError):
        return -1

def _win_has_system_attributes(path: Path) -> bool:
    """
    Detecta si un archivo tiene flags de sistema, oculto o temporal.
    Evita la manipulación de archivos core necesarios para la estabilidad del SO.
    """
    attrs: int = _get_win_attributes(path)
    if attrs == -1: return True
    return bool(attrs & 0x01 or attrs & 0x02 or attrs & 0x04 or attrs & 0x100)

def _verify_source_fs_permissions(path: Path) -> None:
    """Valida restricciones de red (UNC) y atributos especiales en el origen."""
    p_str: str = str(path)
    if p_str.startswith(r"\\") or p_str.startswith("//"):
        raise UnsafePathError("Operaciones en rutas UNC no permitidas.")
    _check_path_for_junctions(path)
    if _win_has_system_attributes(path):
        raise UnsafePathError("Archivo protegido por atributos de sistema o temporal.")

def _is_filesystem_read_only(path: Path) -> bool:
    """Verifica accesibilidad de escritura mediante bit de permiso del S.O."""
    if path is None: return True
    if not path.exists(): return False
    return not os.access(path, os.W_OK)

def _check_io_error_context(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """
    Wrapper de ejecución para operaciones de E/S. Implementa reintento exponencial
    ante errores transitorios, comunes cuando antivirus o indexadores bloquean archivos.
    """
    max_retries: int = 3
    for i in range(max_retries):
        try:
            return func(*args, **kwargs)
        except (OSError, IOError, PermissionError) as e:
            if i == max_retries - 1:
                raise e
            time.sleep(0.05 * (2 ** i))
    raise RuntimeError("Fallo de E/S tras múltiples intentos.")

def _is_file_exclusive(path: Path) -> bool:
    """
    Verifica si el archivo está libre de bloqueos de terceros.
    En Windows usa CreateFile con FILE_SHARE_NONE; en POSIX usa flock.
    """
    if path is None or not path.exists():
        return False
    if os.name == 'nt':
        try:
            INVALID_HANDLE_VALUE = -1
            handle = ctypes.windll.kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
            if handle == INVALID_HANDLE_VALUE: return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except (OSError, AttributeError, PermissionError):
            return False
    
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except (OSError, IOError, PermissionError, AttributeError):
        return False
    finally:
        if fd != -1:
            os.close(fd)

@dataclass
class QuarantineItem:
    """
    Registro persistente de un archivo en el sandbox.
    Almacena inodo, dispositivo y hash para garantizar que el archivo restaurado
    o purgado sea exactamente el mismo que se aisló (protección TOCTOU).
    """
    item_id: str
    original_path: str
    stored_name: str
    size_bytes: int
    reason: str
    quarantined_at: str
    sha256: str = ""
    file_inode: Inode = 0
    device_id: int = 0
    mtime_snapshot: float = 0.0

    def __post_init__(self) -> None:
        """Normalización de tipos y validación post-inicialización del registro."""
        try:
            self.size_bytes = int(self.size_bytes)
        except (ValueError, TypeError):
            self.size_bytes = 0
        if not isinstance(self.item_id, str) or not self.item_id.strip():
            raise ValueError("ID de ítem inválido")
        if not isinstance(self.reason, str) or not self.reason.strip():
            self.reason = "Sin motivo especificado"
        if not isinstance(self.original_path, str) or not self.original_path.strip():
            raise ValueError("Ruta original inválida")

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño del ítem en MB con redondeo para reportes."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la instancia a diccionario para persistencia JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Optional[QuarantineItem]:
        """Reconstruye un ítem desde JSON validando rutas contra políticas de seguridad."""
        if not isinstance(data, dict): return None
        required = ("item_id", "original_path", "stored_name", "size_bytes", "reason", "quarantined_at")
        if not all(k in data and data[k] is not None for k in required): return None
        try:
            orig_p: Path = _is_path_canonical(Path(str(data["original_path"])))
            if not orig_p.is_absolute() or is_protected_path(orig_p): return None
            return cls(
                item_id=str(data["item_id"]), original_path=str(orig_p), stored_name=str(data["stored_name"]),
                size_bytes=int(data["size_bytes"]), reason=str(data["reason"]), quarantined_at=str(data["quarantined_at"]),
                sha256=str(data.get("sha256", "")), file_inode=int(data.get("file_inode", 0)),
                device_id=int(data.get("device_id", 0)), mtime_snapshot=float(data.get("mtime_snapshot", 0.0))
            )
        except (ValueError, TypeError): return None

    def _validate_integrity(self, stored_path: Path, st_stat: os.stat_result) -> bool:
        """
        Validación de metadatos de bajo nivel para detectar sustitución de archivos.
        Compara inodo, dispositivo y enlaces duros contra el registro original.
        """
        try:
            if hasattr(os, 'getuid') and st_stat.st_uid != os.getuid(): return False
            if self.file_inode != 0 and st_stat.st_ino != self.file_inode: return False
            if self.device_id != 0 and st_stat.st_dev != self.device_id: return False
            if st_stat.st_nlink > 1: return False
            if self.mtime_snapshot != 0.0 and abs(st_stat.st_mtime - self.mtime_snapshot) > 1.0: return False
            return (stored_path.is_file() and st_stat.st_size == self.size_bytes and st_stat.st_size > 0)
        except (OSError, PermissionError): return False

    def verify_integrity(self, stored_path: Path) -> bool:
        """Combina verificación de metadatos con validación de hash SHA256."""
        if stored_path is None or not stored_path.exists(): return False
        try:
            st = stored_path.stat()
            _check_for_reparse_points(stored_path)
            if not self._validate_integrity(stored_path, st): return False
            return bool(self.sha256 and _get_sha256(stored_path, st) == self.sha256)
        except (OSError, PermissionError): return False


def _get_sha256(path: Path, st_stat: Optional[os.stat_result] = None) -> str:
    """
    Calcula el hash del archivo utilizando streaming para memoria constante.
    Usa una caché basada en inodo y mtime para acelerar escaneos repetidos.
    """
    if path is None: return ""
    try:
        st_res = st_stat if st_stat else path.stat()
        key = (st_res.st_dev, st_res.st_ino, st_res.st_mtime, getattr(st_res, 'st_ctime', 0.0))
        now = time.monotonic()
        
        if key in _HASH_CACHE and (now - _HASH_CACHE[key][1] < 60.0):
            return _HASH_CACHE[key][0]
        
        with open(path, "rb") as f:
            if hasattr(hashlib, "file_digest"):
                res = hashlib.file_digest(f, "sha256").hexdigest()
            else:
                sha256_hash = hashlib.sha256()
                while chunk := f.read(CHUNK_SIZE):
                    sha256_hash.update(chunk)
                res = sha256_hash.hexdigest()
        
        _HASH_CACHE[key] = (res, now)
        return res
    except (OSError, IOError, MemoryError, PermissionError): 
        return ""

def _is_file_in_use_by_system(path: Path) -> bool:
    """Verifica uso mediante inodos y atributos. Usa caché local para rapidez."""
    if path is None or not path.exists(): return False
    try:
        st = path.stat()
        key = (st.st_dev, st.st_ino, st.st_mtime)
        now = time.monotonic()
        
        if key in _INODE_USE_CACHE and (now - _INODE_USE_CACHE[key][1] < 0.5):
            return _INODE_USE_CACHE[key][0]
        
        in_use = (st.st_nlink > 1 or _win_has_system_attributes(path) or not _is_file_exclusive(path))
            
        _INODE_USE_CACHE[key] = (in_use, now)
        return in_use
    except (OSError, PermissionError):
        return True

def _validate_file_for_unlink(path: Path, expected_hash: Optional[str], expected_inode: Inode, expected_device: int = 0) -> None:
    """
    Auditoría estricta pre-borrado. Valida que el archivo en disco no haya sido
    intercambiado (TOCTOU) y verifica que cumpla con los hashes registrados.
    """
    if path is None or not path.exists(): raise UnsafePathError("Ruta nula o inexistente para unlink.")
    if path.name == MANIFEST_NAME: raise UnsafePathError("Protección de integridad: No se permite borrar el manifiesto.")
    
    ensure_safe_to_modify(path)
    
    if is_protected_path(path.parent): raise UnsafePathError("Protección de jerarquía: directorio padre no seguro.")
    _check_for_reparse_points(path)
    if is_protected_path(path) or len(str(path)) > 260 or ":" in path.name: 
        raise UnsafePathError("Ruta protegida, ADS o longitud excedida.")
    
    st: os.stat_result = path.stat()
    if st.st_nlink > 1 or (expected_inode != 0 and st.st_ino != expected_inode): raise UnsafePathError("Inconsistencia en inodo o enlaces.")
    if expected_device != 0 and st.st_dev != expected_device: raise UnsafePathError("Falla de consistencia de dispositivo.")
    if hasattr(os, 'getuid') and st.st_uid != os.getuid(): raise UnsafePathError("Error de propiedad de archivo.")
    if expected_hash and _get_sha256(path, st) != expected_hash: raise UnsafePathError("Falla de integridad: hash mismatch.")
    if _is_file_in_use_by_system(path): raise UnsafePathError("Archivo en uso por el sistema.")

def _safe_unlink(path: Path, expected_hash: Optional[str] = None, expected_inode: Inode = 0, expected_device: int = 0) -> bool:
    """Elimina el archivo validando integridad y forzando sincronización del FS."""
    if not path or not path.exists() or not path.is_file(): return True
    try:
        _validate_file_for_unlink(path, expected_hash, expected_inode, expected_device)
        parent: Path = path.parent
        _check_io_error_context(path.unlink)
        if parent.exists():
            _sync_directory(parent)
        
        time.sleep(0.01)
        return not path.exists()
    except (UnsafePathError, OSError, PermissionError, RuntimeError):
        return not path.exists()

def _check_path_syntax_integrity(path: Path) -> None:
    """
    Previene inyecciones mediante validación de caracteres, profundidad y longitud.
    Valida normalización Unicode para evitar evasión de filtros por nombre.
    """
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
    if path.exists():
        try:
            resolved: Path = path.resolve(strict=True)
            _check_for_reparse_points(resolved)
        except (OSError, RuntimeError): pass

def _sanitize_filename(filename: str) -> str:
    """Limpia cadenas para crear nombres de archivos compatibles multiplataforma."""
    return "".join(c for c in filename if c.isalnum() or c in "._-").lstrip(".")

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """Genera un nombre de archivo único y seguro para el almacenamiento interno."""
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
    """Verifica que el usuario actual posea el directorio raíz del sandbox."""
    if path is None or not path.exists(): raise UnsafePathError("Ruta nula o inexistente en ownership.")
    if hasattr(os, 'getuid'):
        try:
            if path.stat().st_uid != os.getuid(): raise UnsafePathError("Propiedad de directorio no coincide con usuario.")
        except OSError: raise UnsafePathError("No se pudo verificar la propiedad del directorio.")

def quarantine_dir(base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Prepara y valida el directorio de cuarentena, asegurando que sea seguro."""
    if not base or not str(base).strip(): raise ValueError("El directorio base no puede estar vacío.")
    try:
        path: Path = _is_path_canonical(Path(str(base)).expanduser())
        if ":" in path.name or any(part.upper() in WINDOWS_RESERVED_NAMES for part in path.parts):
            raise UnsafePathError("Ruta de cuarentena contiene elementos reservados o ADS.")
        if len(str(path)) > 240: raise UnsafePathError("Ruta de cuarentena excede límites de seguridad.")
        if len(path.parts) > 16: raise UnsafePathError("Profundidad de ruta de cuarentena excesiva.")
        if not path.name.strip() or path == path.parent: raise UnsafePathError("Ruta de cuarentena inválida: raíz o vacía.")
        _check_path_for_junctions(path)
        if is_protected_path(path): raise UnsafePathError("Directorio de cuarentena reside en ruta protegida.")
        
        if path.exists() and not path.is_dir():
            raise UnsafePathError("Ruta de cuarentena bloqueada por un archivo existente.")
            
        if path.exists():
            if not is_safe_to_modify(path): raise UnsafePathError("Ruta de cuarentena marcada como insegura.")
            if not os.access(path, os.W_OK | os.R_OK): raise PermissionError("Directorio inaccesible para operaciones de I/O.")
            if _is_filesystem_read_only(path): raise PermissionError("Permisos insuficientes en directorio.")
        else:
            _check_io_error_context(path.mkdir, parents=True, exist_ok=True)
            if not path.exists() or not path.is_dir(): raise OSError("Fallo al asegurar directorio de cuarentena.")
        
        try:
            with tempfile.NamedTemporaryFile(dir=path, delete=True):
                pass
        except (PermissionError, OSError) as e:
            raise PermissionError(f"Directorio de cuarentena no permite creación de temporales: {e}")
        
        _ensure_path_ownership(path)
        return path
    except (OSError, RuntimeError, UnsafePathError, PermissionError) as e:
        raise OSError(f"Error al preparar directorio de cuarentena: {e}")

def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ruta completa al archivo de manifiesto JSON."""
    return (base_dir / MANIFEST_NAME)

def _is_within_quarantine_sandbox(path: Path, root: Path) -> bool:
    """Verifica confinamiento de ruta contra la raíz del sandbox."""
    if path is None or root is None: return False
    return is_within_directory(_is_path_canonical(path), _is_path_canonical(root))

def _validate_quarantine_path(path: Path, base: Path) -> Path:
    """Asegura confinamiento de ruta contra la base del sandbox."""
    if path is None or base is None: raise UnsafePathError("Ruta nula en sandbox.")
    if not is_within_directory(_is_path_canonical(path), _is_path_canonical(base)): raise UnsafePathError("Acceso fuera del sandbox detectado.")
    return path

def _check_device_consistency(source: Path, target_directory: Path) -> None:
    """Impide operaciones entre volúmenes distintos para garantizar atomicidad."""
    if not isinstance(source, Path) or not isinstance(target_directory, Path):
        raise TypeError("Las rutas deben ser instancias de pathlib.Path.")
    try:
        if _is_path_canonical(source).stat().st_dev != _is_path_canonical(target_directory).stat().st_dev:
            raise UnsafePathError("Operación entre distintos volúmenes no permitida.")
    except OSError as e:
        raise UnsafePathError(f"No se pudo verificar la consistencia del dispositivo: {e}")

def _verify_quarantine_preconditions(source_path: Path, target_directory: Path) -> None:
    """Valida precondiciones contra bucles o escapes en el sistema de archivos."""
    resolved_source: Path = _is_path_canonical(source_path)
    resolved_dest_dir: Path = _is_path_canonical(target_directory)
    if is_protected_path(resolved_source): raise UnsafePathError("Ruta origen protegida.")
    if is_protected_path(resolved_dest_dir) or is_protected_path(resolved_dest_dir.parent): raise UnsafePathError("Destino en ruta protegida.")
    if _is_within_quarantine_sandbox(resolved_source, resolved_dest_dir): raise UnsafePathError("Archivo ya se encuentra en sandbox.")
    if is_within_directory(resolved_dest_dir, resolved_source): raise UnsafePathError("Operación recursiva prohibida: destino dentro de origen.")
    if resolved_source.parent == resolved_dest_dir: raise UnsafePathError("Operación circular detectada.")

def _check_isolation_safety(source_path: Path, target_directory: Path) -> None:
    """Validaciones de integridad y restricciones previas al aislamiento."""
    source_abs: Path = _is_path_canonical(source_path)
    target_abs: Path = _is_path_canonical(target_directory)
    
    _check_for_reparse_points(source_abs)
    if not source_abs.is_file(): raise UnsafePathError("Solo se permiten archivos regulares.")
    if source_abs.stat().st_size == 0: raise UnsafePathError("Archivos vacíos prohibidos.")
    if _is_filesystem_read_only(target_abs): raise PermissionError("Directorio de cuarentena sin permisos de escritura.")
    
    if hasattr(os, 'getuid'):
        if source_abs.stat().st_uid != os.getuid(): raise UnsafePathError("Aislamiento denegado: propiedad incorrecta.")
        
    _check_device_consistency(source_abs, target_abs)
    _verify_quarantine_preconditions(source_abs, target_abs)
    ensure_safe_to_modify(source_abs, allow_sensitive=True)
    
    if not _is_file_exclusive(source_abs): raise IOError("Archivo origen bloqueado por otro proceso.")

def _validate_isolation_request(source_path: Path, target_directory: Path) -> None:
    """Coordina las validaciones críticas previas al aislamiento."""
    if not target_directory.is_dir(): 
        raise UnsafePathError("Destino de cuarentena debe ser un directorio.")
    
    source_path = _is_path_canonical(source_path)
    if not source_path.exists() or not source_path.is_file():
        raise UnsafePathError("El origen debe ser un archivo existente.")

    _check_path_syntax_integrity(source_path)
    _verify_source_fs_permissions(source_path)
    _check_for_reparse_points(source_path)
    
    if _win_has_system_attributes(source_path): 
        raise UnsafePathError("Aislamiento denegado: archivo con atributos de sistema o temporal.")
    
    if len(str(target_directory)) > 240: 
        raise UnsafePathError("Ruta de cuarentena demasiado larga.")
        
    resolved_target = _is_path_canonical(target_directory)
    if is_within_directory(source_path, resolved_target) or source_path == resolved_target:
        raise UnsafePathError("Archivo origen ya reside dentro del sandbox.")
        
    _ensure_disk_space(target_directory, source_path.stat().st_size)
    _check_isolation_safety(source_path, target_directory)

def load_manifest(base: PathLike = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Carga y valida el manifiesto contra el estado actual del disco, usando caché."""
    try: 
        base_dir: Path = quarantine_dir(base)
    except (OSError, UnsafePathError): 
        return []
    
    m_path: Path = _manifest_path(base_dir)
    if not m_path.exists():
        _MANIFEST_CACHE[base_dir] = ([], 0.0, 0)
        return []
        
    try:
        st = m_path.stat()
        # Validación de caché usando metadatos de archivo (sin leer el JSON)
        if not force_reload and base_dir in _MANIFEST_CACHE:
            cached_items, cached_mtime, cached_size = _MANIFEST_CACHE[base_dir]
            if cached_mtime == st.st_mtime and cached_size == st.st_size: 
                return cached_items
        
        with open(m_path, "r", encoding="utf-8") as f: 
            data = json.load(f)
            
        if not isinstance(data, list): 
            return []
            
        valid_items: List[QuarantineItem] = []
        needs_cleanup = False
        for entry in data:
            item = QuarantineItem.from_dict(entry)
            # Solo verificar integridad si el ítem es válido según el schema
            if item and item.verify_integrity(base_dir / item.stored_name):
                valid_items.append(item)
            else:
                needs_cleanup = True
        
        if needs_cleanup:
            save_manifest(valid_items, base)
        
        _MANIFEST_CACHE[base_dir] = (valid_items, st.st_mtime, st.st_size)
        return valid_items
        
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return []

def save_manifest(items: List[QuarantineItem], base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Persiste el manifiesto usando escritura atómica: escribe en un temporal
    y realiza un os.replace, garantizando que el archivo nunca quede corrupto.
    """
    base_path: Path = quarantine_dir(base)
    target_path: Path = _manifest_path(base_path)
    
    try:
        serializable = [item.to_dict() for item in items if isinstance(item, QuarantineItem)]
        encoded = json.dumps(serializable, indent=2, ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError) as e:
        raise ValueError(f"Error de serialización: {e}")
        
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=base_path, delete=False) as tf:
            temp_name = tf.name
            tf.write(encoded)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(temp_name, target_path)
        _sync_directory(base_path)
        st = target_path.stat()
        _MANIFEST_CACHE[base_path] = (items, st.st_mtime, st.st_size)
        return target_path
    except (OSError, IOError) as e:
        if temp_name and os.path.exists(temp_name):
            try: os.remove(temp_name)
            except OSError: pass
        raise OSError(f"Error crítico al guardar manifiesto: {e}")

def _ensure_disk_space(dest_dir: Path, required_size: int) -> None:
    """Valida disponibilidad de espacio dejando un margen de seguridad (5%)."""
    if not isinstance(required_size, int) or required_size < 0: raise ValueError("Tamaño requerido inválido.")
    dest_dir = _is_path_canonical(dest_dir)
    if not dest_dir.exists(): raise FileNotFoundError(f"Directorio inexistente: {dest_dir}")
    if not dest_dir.is_dir() or not os.access(dest_dir, os.W_OK | os.R_OK): raise PermissionError(f"Directorio inaccesible: {dest_dir}")
    if not is_safe_to_modify(dest_dir) or _is_filesystem_read_only(dest_dir): raise PermissionError(f"Sin permisos de escritura seguros: {dest_dir}")
    usage = shutil.disk_usage(dest_dir)
    margin: int = max(int(required_size * 0.05), 5 * 1024 * 1024)
    if usage.free < (required_size + margin): raise OSError("Espacio insuficiente en disco.")

def _validate_file_transfer_preconditions(source: Path, destination: Path) -> None:
    """Valida que la transferencia sea posible bajo las restricciones de seguridad."""
    if is_protected_path(destination): raise UnsafePathError("Destino en ruta protegida.")
    ensure_safe_to_modify(destination.parent)
    _check_device_consistency(source, destination.parent)
    _verify_source_fs_permissions(destination)
    if not source.exists() or not source.is_file(): raise OSError("Archivo origen inaccesible.")
    if os.name == 'nt' and _win_has_system_attributes(source): raise PermissionError("Archivo origen marcado como solo lectura/sistema.")
    if destination.exists():
        if os.path.samefile(source, destination): raise UnsafePathError("Operación circular detectada.")
        raise FileExistsError(f"El destino ya existe: {destination}")

def _create_temp_file(source: Path, destination: Path) -> Path:
    """Crea un archivo temporal único para realizar la copia atómica."""
    parent = destination.parent
    ensure_safe_to_modify(parent)
    for _ in range(5):
        temp_path = parent / f".tmp_{uuid.uuid4().hex}"
        try:
            fd = os.open(str(temp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                # Verificación de integridad tras creación exclusiva
                st = os.fstat(fd)
                if st.st_nlink != 1:
                    os.close(fd)
                    continue
                os.fsync(fd)
            finally: os.close(fd)
            return temp_path
        except (FileExistsError, OSError): continue
    raise RuntimeError("No se pudo generar un archivo temporal seguro.")

def _verify_copied_data(source_stat: os.stat_result, dest_path: Path, source_hash: str) -> None:
    """Verifica integridad binaria mediante tamaño y hash SHA256 tras la copia."""
    if dest_path.stat().st_size != source_stat.st_size: raise OSError("Falla de integridad: tamaño mismatch.")
    final_hash: str = _get_sha256(dest_path)
    if not final_hash or final_hash != source_hash: raise OSError("Falla crítica: el hash del archivo copiado no coincide.")

def _perform_secure_copy(source: Path, temp_dest: Path, source_hash: str) -> None:
    """Realiza la copia de bytes y sincroniza físicamente al disco."""
    try:
        st = source.stat()
        if not (st.st_mode & 0o100000): raise OSError("El origen no es un archivo regular.")
        _check_for_reparse_points(source)
        if st.st_nlink > 1: raise UnsafePathError("Archivo origen con enlaces múltiples.")
        
        _ensure_disk_space(temp_dest.parent, st.st_size)
        
        ensure_safe_to_modify(temp_dest.parent)
        
        def _copy():
            with open(source, "rb") as f_src:
                with open(temp_dest, "wb") as f_dst:
                    # Validar nuevamente que el temporal sea un archivo simple antes de escribir
                    if os.path.islink(temp_dest): raise UnsafePathError("Inyección de enlace durante copia.")
                    shutil.copyfileobj(f_src, f_dst)
                    f_dst.flush()
                    os.fsync(f_dst.fileno()) 
        
        _check_io_error_context(_copy)
        
        try: os.chmod(temp_dest, 0o600)
        except OSError: pass
        if not temp_dest.is_file(): raise OSError("El archivo copiado fue reemplazado.")
        _verify_copied_data(st, temp_dest, source_hash)
    except (OSError, IOError) as e:
        if temp_dest.exists():
            try: _check_io_error_context(temp_dest.unlink)
            except OSError: pass
        raise OSError(f"Falla durante copia I/O: {e}")

def _write_temp_to_final(source_path: Path, dest_path: Path) -> Tuple[str, Inode, int, float]:
    """Coordina el movimiento atómico del temporal al destino final."""
    _check_path_syntax_integrity(dest_path)
    _validate_file_transfer_preconditions(source_path, dest_path)
    
    source_hash: str = _get_sha256(source_path)
    temp_dest: Path = _create_temp_file(source_path, dest_path)
    
    try:
        _perform_secure_copy(source_path, temp_dest, source_hash)
        if dest_path.exists(): raise FileExistsError("Colisión de ruta antes del reemplazo.")
        os.replace(temp_dest, dest_path)
        _sync_directory(dest_path.parent)
        
        dest_stat = dest_path.stat()
        if dest_stat.st_nlink > 1: raise UnsafePathError("Integridad comprometida: destino tiene enlaces múltiples.")
        ensure_safe_to_modify(dest_path, allow_sensitive=True)
        return source_hash, dest_stat.st_ino, dest_stat.st_dev, dest_stat.st_mtime
    except (OSError, IOError, UnsafePathError, FileExistsError) as e:
        if temp_dest.exists():
            try: _check_io_error_context(temp_dest.unlink)
            except OSError: pass
        if dest_path.exists() and not os.path.samefile(source_path, dest_path): _safe_unlink(dest_path)
        raise RuntimeError(f"Error crítico en transferencia: {e}")

class IsolationManager:
    """Gestiona validaciones para asegurar la atomicidad de aislamiento."""
    @staticmethod
    def validate_atomicity(source: Path, original_stat: os.stat_result, dest: Path) -> None:
        """
        Verificación TOCTOU: compara el inodo y dispositivo original antes y
        durante el proceso, abortando si el archivo fue reemplazado.
        """
        if source is None or not source.exists(): raise FileNotFoundError("Archivo origen eliminado antes del aislamiento.")
        current_stat = source.stat()
        if current_stat.st_ino != original_stat.st_ino or current_stat.st_dev != original_stat.st_dev: 
            raise RuntimeError("Integridad comprometida: archivo reemplazado o movido (TOCTOU).")
        if current_stat.st_nlink > 1: raise UnsafePathError("Aislamiento denegado: enlaces físicos múltiples.")
        dest_res: Path = _is_path_canonical(dest)
        base_res: Path = _is_path_canonical(dest.parent)
        if not is_within_directory(dest_res, base_res): raise UnsafePathError("Intento de escape del sandbox.")
        ensure_safe_to_modify(dest.parent)

def _verify_post_isolation_integrity(dest_path: Path, expected_hash: str, expected_inode: Inode, expected_device: int) -> None:
    """Valida la consistencia tras mover el archivo al sandbox."""
    dest_stat = dest_path.stat()
    if dest_stat.st_ino != expected_inode or dest_stat.st_dev != expected_device or dest_stat.st_nlink > 1:
        raise RuntimeError("Integridad comprometida: destino inconsistente.")
    if _get_sha256(dest_path) != expected_hash:
        raise RuntimeError("Integridad comprometida: hash mismatch.")
    if _win_has_system_attributes(dest_path):
        raise UnsafePathError("Integridad comprometida: el archivo en sandbox adquirió atributos de sistema.")

def _atomic_isolate_file(source: Path, destination: Path, original_size: int) -> Tuple[str, Inode, int, float]:
    """Aísla archivos mediante un proceso atómico de varias etapas."""
    _check_path_syntax_integrity(destination)
    _check_path_syntax_integrity(source)
    if not source.exists(): raise FileNotFoundError("Archivo origen no existe.")
    _check_for_reparse_points(source)
    
    resolved_source = _is_path_canonical(source)
    resolved_dest = _is_path_canonical(destination)
    
    if is_within_directory(resolved_source, resolved_dest.parent):
        raise UnsafePathError("Colisión de rutas detectada en el sandbox.")
    
    if _win_has_system_attributes(source) or not source.is_file(): raise UnsafePathError("Aislamiento denegado: atributo de sistema o no regular.")
    ensure_safe_to_modify(destination.parent)
    
    if not _is_file_exclusive(source): raise IOError("El archivo origen está bloqueado por otro proceso.")
    
    with open(source, "rb") as f:
        stat_orig = os.fstat(f.fileno())
        if stat_orig.st_size != original_size or stat_orig.st_nlink > 1: raise RuntimeError("El archivo cambió o tiene enlaces inválidos (TOCTOU).")
        existing_items: List[QuarantineItem] = load_manifest(destination.parent.parent)
        if any(i.file_inode == stat_orig.st_ino and i.device_id == stat_orig.st_dev for i in existing_items): raise RuntimeError("Colisión de inodo: archivo ya registrado.")
        IsolationManager.validate_atomicity(source, stat_orig, destination)
        if destination.exists(): raise FileExistsError("Colisión de ruta: archivo destino ya presente.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            h, i, d, mtime = _write_temp_to_final(source, destination)
        except (OSError, IOError, UnsafePathError, FileExistsError) as e:
            raise RuntimeError(f"Error durante la escritura del archivo aislado: {e}")
            
        ensure_safe_to_modify(destination, allow_sensitive=True)
        _verify_post_isolation_integrity(destination, h, i, d)
        return h, i, d, mtime

def _register_quarantine_item(destination: Path, source_path: Path, file_hash: str, file_inode: Inode, device_id: int, mtime: float, reason: str, original_size: int, base: PathLike) -> QuarantineItem:
    """Registra el nuevo ítem en el manifiesto JSON."""
    if destination is None or not destination.exists(): raise RuntimeError("Falla: intento de registrar archivo inexistente.")
    try:
        items_list: List[QuarantineItem] = load_manifest(base)
        dest_stat = destination.stat()
        if dest_stat.st_ino != file_inode or dest_stat.st_dev != device_id or dest_stat.st_nlink > 1: raise RuntimeError("Inconsistencia crítica: mismatch de inodo, dispositivo o enlace.")
        quarantine_item: QuarantineItem = QuarantineItem(
            item_id=uuid.uuid4().hex[:12], original_path=str(_is_path_canonical(source_path)), stored_name=destination.name,
            size_bytes=original_size, reason=reason if reason else "Sin motivo", quarantined_at=datetime.now().isoformat(timespec="seconds"),
            sha256=file_hash, file_inode=file_inode, device_id=device_id, mtime_snapshot=mtime
        )
        items_list.append(quarantine_item)
        save_manifest(items_list, base)
        return quarantine_item
    except (OSError, IOError, ValueError, RuntimeError) as e:
        if destination.exists(): _safe_unlink(destination)
        raise RuntimeError(f"Falla al registrar ítem en manifiesto: {e}")

def _validate_input_path(source: PathLike) -> Path:
    """Valida la entrada del archivo origen contra inyección o ataques de trayectoria."""
    if source is None: raise ValueError("Ruta de origen nula.")
    path_val = str(source).strip()
    if not path_val: raise ValueError("Ruta de origen vacía.")
    p_source: Path = _is_path_canonical(Path(path_val))
    if path_val.startswith(r"\\") or path_val.startswith("//"): raise UnsafePathError("Rutas UNC no permitidas.")
    if len(path_val) > 260: raise UnsafePathError("Ruta de entrada excede límites.")
    if len(p_source.parts) > 32: raise UnsafePathError("Profundidad de ruta de entrada excesiva.")
    if p_source == p_source.anchor: raise UnsafePathError("No se permite aislar la raíz de una unidad.")
    if not p_source.exists(): raise FileNotFoundError("Archivo origen no encontrado.")
    _check_for_reparse_points(p_source)
    if not p_source.is_file(): raise ValueError("El origen debe ser un archivo regular.")
    if not os.access(p_source, os.R_OK): raise PermissionError("Archivo origen sin permisos de lectura.")
    return p_source

def quarantine_file(source: PathLike, reason: str = "Marcado como sospechoso", base: PathLike = DEFAULT_QUARANTINE_DIR) -> QuarantineItem:
    """Punto de entrada: aísla un archivo en cuarentena tras validar seguridad."""
    p_source: Path = _validate_input_path(source)
    
    try:
        if not _is_file_exclusive(p_source):
            raise IOError("El archivo origen está bloqueado por otro proceso.")
        initial_stat = p_source.stat()
    except (OSError, IOError, PermissionError) as e:
        raise RuntimeError(f"Error de acceso durante validación de origen: {e}")

    if _is_filesystem_read_only(p_source.parent): raise OSError("El archivo origen reside en un volumen de solo lectura.")
    try: dest_dir: Path = quarantine_dir(base)
    except (OSError, PermissionError) as e: raise RuntimeError(f"Sandbox inaccesible: {e}")
    
    ensure_safe_to_modify(dest_dir)
    
    if _win_has_system_attributes(p_source): raise PermissionError("Archivo origen restringido por atributos de sistema.")
    if _is_path_canonical(p_source).stat().st_dev != dest_dir.stat().st_dev: raise UnsafePathError("Origen y sandbox deben estar en la misma unidad.")
    
    _validate_isolation_request(p_source, dest_dir)
    destination: Path = dest_dir / _generate_safe_stored_name(p_source, uuid.uuid4().hex[:12])
    
    if destination.exists():
        if os.path.samefile(p_source, destination):
            raise UnsafePathError("Operación circular detectada: origen y destino iguales.")
        raise FileExistsError(f"Colisión de destino en sandbox: {destination}")
    
    try:
        file_hash, file_inode, dev_id, mtime = _atomic_isolate_file(p_source, destination, initial_stat.st_size)
        item: QuarantineItem = _register_quarantine_item(destination, p_source, file_hash, file_inode, dev_id, mtime, reason, initial_stat.st_size, base)
        if p_source.exists() and not os.path.samefile(p_source, destination):
            try: _check_io_error_context(p_source.unlink)
            except (OSError, PermissionError) as e: raise RuntimeError(f"Falla al remover original post-aislamiento: {e}")
        return item
    except (OSError, IOError, ValueError, RuntimeError, UnsafePathError) as e:
        if destination.exists(): _safe_unlink(destination)
        raise RuntimeError(f"Error durante proceso de cuarentena: {e}")

def list_items(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna ítems ordenados por fecha de aislamiento."""
    try: quarantine_dir(base)
    except (OSError, UnsafePathError): return []
    items = load_manifest(base)
    return sorted(items, key=lambda x: x.quarantined_at, reverse=True)

def restore_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura archivo del sandbox validando integridad del manifiesto."""
    if not isinstance(item_id, str) or not item_id.strip(): raise ValueError("ID de ítem vacío.")
    try:
        base_path: Path = quarantine_dir(base)
        manifest: List[QuarantineItem] = load_manifest(base)
        quarantine_item: Optional[QuarantineItem] = next((i for i in manifest if i.item_id == item_id), None)
        if not quarantine_item: raise KeyError(f"Ítem no encontrado: {item_id}")
        stored_file: Path = base_path / quarantine_item.stored_name
        if not stored_file.exists() or not quarantine_item.verify_integrity(stored_file):
            items: List[QuarantineItem] = load_manifest(base, force_reload=True)
            save_manifest([i for i in items if i.item_id != item_id], base)
            raise RuntimeError(f"Archivo inexistente o corrupto: {item_id}")
        destination: Path = _is_path_canonical(Path(quarantine_item.original_path))
        _check_path_syntax_integrity(destination)
        if destination.exists(): raise FileExistsError(f"El destino ya existe: {destination}")
        if is_protected_path(destination): raise UnsafePathError("Restauración denegada: destino protegido.")
        parent: Path = destination.parent
        ensure_safe_to_modify(parent)
        _ensure_disk_space(parent, quarantine_item.size_bytes)
        if not parent.exists(): _check_io_error_context(parent.mkdir, parents=True, exist_ok=True)
        
        ensure_safe_to_modify(destination)
        
        _check_io_error_context(os.replace, str(stored_file), str(destination))
        for p in [parent, base_path]:
            if p and p.exists():
                _sync_directory(p)
        items = load_manifest(base, force_reload=True)
        save_manifest([i for i in items if i.item_id != item_id], base)
        return destination
    except (OSError, PermissionError, IOError, KeyError) as e: raise RuntimeError(f"Error crítico en restauración: {e}")

def purge_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> bool:
    """Elimina permanentemente un ítem del sandbox tras validar su integridad."""
    if not isinstance(item_id, str) or not item_id.strip(): raise ValueError("ID de ítem vacío.")
    try: base_path: Path = quarantine_dir(base)
    except OSError: return False
    if is_protected_path(base_path): return False
    items: List[QuarantineItem] = load_manifest(base)
    quarantine_item: Optional[QuarantineItem] = next((i for i in items if i.item_id == item_id), None)
    if quarantine_item is None: return False
    stored_file: Path = base_path / quarantine_item.stored_name
    if not stored_file.exists():
        items = load_manifest(base, force_reload=True)
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
    if not quarantine_item.verify_integrity(stored_file):
        raise UnsafePathError(f"Integridad fallida: {item_id}.")
    
    if _safe_unlink(stored_file, expected_hash=quarantine_item.sha256, expected_inode=quarantine_item.file_inode, expected_device=quarantine_item.device_id):
        items = load_manifest(base, force_reload=True)
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
    return False

def _is_item_purgable(file_path: Optional[Path], item: Optional[QuarantineItem] = None) -> bool:
    """Verifica si un ítem cumple los criterios de seguridad para purgado."""
    if file_path is None or not file_path.exists() or not file_path.is_file(): return False
    try:
        _check_for_reparse_points(file_path)
        if _is_file_in_use_by_system(file_path): return False
        if item is not None and item.verify_integrity(file_path):
            return _safe_unlink(file_path, expected_hash=item.sha256, expected_inode=item.file_inode, expected_device=item.device_id)
        return False
    except (OSError, PermissionError, UnsafePathError): return False

def purge_all(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Purga masiva de todos los ítems registrados en el sandbox."""
    try:
        root_path: Path = quarantine_dir(base)
        if _is_filesystem_read_only(root_path) or is_protected_path(root_path): return 0
        items = load_manifest(base)
        item_map: Dict[str, QuarantineItem] = {i.stored_name: i for i in items}
        purgado_ids: Set[str] = set()
        
        with os.scandir(root_path) as it:
            for entry in it:
                if entry.name == MANIFEST_NAME or not entry.is_file():
                    continue
                
                entry_path = Path(entry.path)
                if not is_within_directory(_is_path_canonical(entry_path), _is_path_canonical(root_path)):
                    continue

                item = item_map.get(entry.name)
                if not item:
                    try:
                        _safe_unlink(entry_path)
                    except (OSError, PermissionError, UnsafePathError): continue
                    continue
                
                try:
                    if _is_item_purgable(entry_path, item):
                        purgado_ids.add(item.item_id)
                except (OSError, PermissionError, UnsafePathError): continue
            
        if purgado_ids:
            new_items = [i for i in items if i.item_id not in purgado_ids]
            save_manifest(new_items, base)
            _sync_directory(root_path)
        
        return len(purgado_ids)
    except (OSError, PermissionError, UnsafePathError, RuntimeError): return 0

def total_quarantined_bytes(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el peso total ocupado por archivos en cuarentena."""
    items = load_manifest(base)
    return sum(item.size_bytes for item in items)

def summarize(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera descripción legible del estado actual de la cuarentena."""
    items: List[QuarantineItem] = load_manifest(base)
    if not items: return ["La cuarentena está vacía."]
    total_mb: float = sum(i.size_mb for i in items)
    lines: List[str] = [f"{len(items)} archivo(s) en cuarentena — {total_mb:.2f} MB", ""]
    for item in items:
        lines.append(f"  [{item.item_id}] {Path(item.original_path).name} — {item.size_mb} MB")
        lines.append(f"      Motivo: {item.reason}")
        lines.append(f"      Origen: {item.original_path}")
        lines.append(f"      Aislado: {item.quarantined_at}")
    lines.extend(["", "Nada de esto se borró: se puede restaurar a su ubicación original."])
    return lines
