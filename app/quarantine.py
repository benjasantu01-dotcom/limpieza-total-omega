"""
quarantine.py — cuarentena reversible para archivos sospechosos.

ES LA PIEZA QUE UNE LIMPIADOR Y ANTIVIRUS
-----------------------------------------
Cuando `scanner.py` marca algo como sospechoso, la respuesta correcta NO es
borrarlo: un falso positivo borrado es un daño irreversible. Acá el archivo
se mueve a una carpeta aislada y se anota en un manifiesto con su ruta
original, tamaño, fecha y motivo. Después se puede **restaurar exactamente
donde estaba**, o vaciar la cuarentena cuando el usuario ya revisó.

Garantías de seguridad que este módulo respeta siempre:
  - Nada se borra al poner en cuarentena; solo se mueve.
  - No se puede poner en cuarentena algo de una ruta protegida del sistema.
  - Al restaurar, el destino se valida para que un manifiesto manipulado no
  - pueda escribir en una ruta de sistema.
  - Vaciar la cuarentena solo borra dentro de la carpeta de cuarentena.
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

# Tipos definidos para claridad en firmas de funciones
PathLike: TypeAlias = Union[str, Path]
ManifestData: TypeAlias = List[Dict[str, Any]]
Inode: TypeAlias = int
T = TypeVar("T")

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

DEFAULT_QUARANTINE_DIR: str = "~/LimpiezaTotalOmega/_Cuarentena"
MANIFEST_NAME: str = "manifest.json"
CHUNK_SIZE: int = 131072  # 128KB para procesamiento de I/O
_MANIFEST_CACHE: Dict[Path, Tuple[List[QuarantineItem], float]] = {}

WINDOWS_RESERVED_NAMES: Set[str] = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", 
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", 
    "LPT6", "LPT7", "LPT8", "LPT9"
}

def _check_path_for_junctions(path: Path) -> None:
    """Verifica si la ruta es un punto de reparse (junction) mediante la API de Windows."""
    if os.name != 'nt':
        return
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs != -1 and (attrs & 0x400):
            raise UnsafePathError("Ruta detectada como punto de reparse/junction.")
    except (OSError, AttributeError):
        pass

def _verify_source_fs_permissions(path: Path) -> None:
    """Valida atributos de sistema o rutas prohibidas (UNC) en archivos origen."""
    p_str = str(path)
    if p_str.startswith(r"\\") or p_str.startswith("//"):
        raise UnsafePathError("Operaciones en rutas UNC no permitidas.")
    if os.name == 'nt':
        try:
            attrs = ctypes.windll.kernel32.GetFileAttributesW(p_str)
            if attrs != -1 and (attrs & 0x02 or attrs & 0x04 or attrs & 0x100):
                raise UnsafePathError("Archivo protegido por atributos de sistema o temporal.")
        except (OSError, AttributeError):
            pass

def _is_filesystem_read_only(path: Path) -> bool:
    """Determina si un directorio es de solo lectura mediante una operación de prueba."""
    if not path.exists(): return True
    try:
        test_file = path / f".test_{uuid.uuid4().hex}"
        test_file.touch()
        test_file.unlink()
        return False
    except (OSError, PermissionError):
        return True

def _check_io_error_context(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Ejecuta una función de I/O con una estrategia de reintento exponencial."""
    max_retries = 3
    for i in range(max_retries):
        try:
            return func(*args, **kwargs)
        except (OSError, IOError, PermissionError) as e:
            if i == max_retries - 1:
                raise e
            time.sleep(0.1 * (2 ** i))
    raise RuntimeError("Fallo inesperado en reintentos de I/O.")

def _win_has_system_attributes(path: Path) -> bool:
    """Consulta la API de Windows para verificar atributos de sistema/oculto."""
    if os.name != 'nt': return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & 0x02 or attrs & 0x04 or attrs & 0x100)
    except (OSError, AttributeError):
        return True

def _is_file_exclusive(path: Path) -> bool:
    """Verifica si el archivo permite acceso exclusivo usando flags de sistema o flock."""
    if os.name == 'nt':
        k32 = ctypes.windll.kernel32
        handle = k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x00000080, None)
        if handle == -1: return False
        k32.CloseHandle(handle)
        return True
    
    try:
        fd = os.open(path, os.O_RDONLY) 
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        finally:
            os.close(fd)
    except (OSError, IOError, AttributeError):
        return False

@dataclass
class QuarantineItem:
    """Representación de un archivo en cuarentena con metadatos de integridad."""
    item_id: str
    original_path: str
    stored_name: str
    size_bytes: int
    reason: str
    quarantined_at: str
    sha256: str = ""
    file_inode: Inode = 0

    def __post_init__(self) -> None:
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
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Optional[QuarantineItem]:
        """Crea una instancia desde un diccionario validando esquema y rutas."""
        if not isinstance(data, dict):
            return None
        
        required = ("item_id", "original_path", "stored_name", "size_bytes", "reason", "quarantined_at")
        if not all(k in data and data[k] is not None for k in required):
            return None
            
        try:
            orig_p = Path(str(data["original_path"]))
            if not orig_p.is_absolute():
                return None
                
            return cls(
                item_id=str(data["item_id"]),
                original_path=str(orig_p),
                stored_name=str(data["stored_name"]),
                size_bytes=int(data["size_bytes"]),
                reason=str(data["reason"]),
                quarantined_at=str(data["quarantined_at"]),
                sha256=str(data.get("sha256", "")),
                file_inode=int(data.get("file_inode", 0))
            )
        except (ValueError, TypeError):
            return None

    def _validate_integrity(self, stored_path: Path) -> bool:
        """Realiza chequeos estructurales básicos para prevenir manipulación."""
        if not stored_path.exists(): return False
        try:
            _check_path_for_junctions(stored_path)
            if stored_path.is_symlink():
                return False
            
            st = stored_path.stat()
            if hasattr(os, 'getuid') and st.st_uid != os.getuid():
                return False

            if self.file_inode != 0 and st.st_ino != self.file_inode:
                return False
            
            if st.st_nlink > 1:
                return False
            
            return (
                stored_path.is_file() and 
                st.st_size == self.size_bytes and
                st.st_size > 0
            )
        except (OSError, PermissionError):
            return False

    def verify_integrity(self, stored_path: Path) -> bool:
        """Verifica la integridad avanzada comparando el hash SHA256 actual del archivo."""
        if not self._validate_integrity(stored_path):
            return False
        try:
            current_hash = _get_sha256(stored_path)
            return bool(self.sha256 and current_hash == self.sha256)
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """Calcula el hash SHA256 del archivo mediante streaming para optimizar uso de memoria."""
    if not path.exists() or not path.is_file():
        return ""
    
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
        
    sha256_hash = hashlib.sha256()
    fd = os.open(str(path), flags)
    try:
        with os.fdopen(fd, "rb") as handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                sha256_hash.update(chunk)
    except (OSError, PermissionError, IOError):
        return ""
    return sha256_hash.hexdigest()


def _is_file_in_use_by_system(path: Path) -> bool:
    """Determina si un archivo está siendo utilizado o bloqueado por el sistema."""
    if not path.exists():
        return False
    
    try:
        st = path.stat()
        if st.st_nlink > 1: return True
    except OSError:
        return True

    if _win_has_system_attributes(path):
        return True
            
    return not _is_file_exclusive(path)

def _validate_file_for_unlink(path: Path, expected_hash: Optional[str], expected_inode: Inode) -> bool:
    """Pre-valida condiciones de seguridad antes de permitir el borrado de un archivo."""
    try:
        if not is_safe_to_modify(path) or is_protected_path(path) or ":" in path.name:
            return False
            
        st = path.stat()
        if st.st_nlink > 1 or (expected_inode != 0 and st.st_ino != expected_inode):
            return False
        if hasattr(os, 'getuid') and st.st_uid != os.getuid():
            return False
        
        if expected_hash and _get_sha256(path) != expected_hash:
            return False
        
        return not _is_file_in_use_by_system(path)
    except (OSError, PermissionError):
        return False

def _safe_unlink(path: Path, expected_hash: Optional[str] = None, expected_inode: Inode = 0) -> bool:
    """Ejecuta un borrado controlado garantizando la sincronización del sistema de archivos."""
    if not _validate_file_for_unlink(path, expected_hash, expected_inode):
        return False
        
    try:
        parent = path.parent
        _check_io_error_context(path.unlink)
        
        if parent.exists():
            dir_fd = os.open(str(parent), os.O_RDONLY)
            try: os.fsync(dir_fd)
            finally: os.close(dir_fd)
        return True
    except (OSError, PermissionError):
        return False

def _check_path_syntax_integrity(path: Path) -> None:
    """Realiza validaciones sintácticas estrictas para prevenir inyecciones o errores de ruta."""
    if path is None:
        raise UnsafePathError("Ruta nula recibida.")
        
    path_str = str(path)
    if len(path_str) > 260:
        raise UnsafePathError("Ruta de sistema demasiado larga.")
    if any(ord(c) < 32 for c in path_str) or "\0" in path_str:
        raise UnsafePathError("Ruta con caracteres de control.")
    if len(path.parts) > 32:
        raise UnsafePathError("Profundidad de ruta excesiva.")
    if ":" in path.name:
        raise UnsafePathError("Ruta con flujos de datos alternos (ADS) prohibidos.")
    
    if path.name.upper() in WINDOWS_RESERVED_NAMES:
        raise UnsafePathError("Nombre de archivo reservado por el sistema.")

    _check_path_for_junctions(path)
    try:
        resolved = path.resolve(strict=True)
        if resolved.is_symlink():
            raise UnsafePathError("Operación denegada: enlace simbólico.")
    except (OSError, RuntimeError):
        pass


def _sanitize_filename(filename: str) -> str:
    """Elimina caracteres peligrosos para asegurar la portabilidad del nombre del archivo."""
    sanitized = "".join(c for c in filename if c.isalnum() or c in "._-")
    return sanitized.lstrip(".")

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """Genera un nombre normalizado único para los archivos aislados."""
    if not item_id:
        raise ValueError("ID de ítem requerido para generar nombre seguro.")
    
    sanitized = _sanitize_filename(original_path.name)
    if not sanitized:
        sanitized = "unknown_file"
    
    parts = sanitized.split('.')
    name_base = parts[0] if parts[0] else "q_file"
    
    if name_base.upper() in WINDOWS_RESERVED_NAMES:
        name_base = f"q_{name_base}"
        
    extension = f".{parts[-1]}" if len(parts) > 1 else ""
    candidate = f"{item_id}__{name_base[:64]}{extension}".replace(":", "_")[:128]
    
    if candidate.split('.')[0].upper() in WINDOWS_RESERVED_NAMES:
        candidate = f"safe_{candidate}"
        
    return candidate

def _ensure_path_ownership(path: Path) -> None:
    """Valida que el directorio sea propiedad del usuario actual en POSIX."""
    if hasattr(os, 'getuid'):
        try:
            if path.stat().st_uid != os.getuid():
                raise UnsafePathError("Propiedad de directorio no coincide con usuario.")
        except OSError:
            raise UnsafePathError("No se pudo verificar la propiedad del directorio.")

def quarantine_dir(base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Retorna y valida el directorio de cuarentena, asegurando su existencia y seguridad."""
    if not base or not str(base).strip():
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path = Path(str(base)).expanduser().resolve()
        
        if len(str(path)) > 240:
             raise UnsafePathError("Ruta de cuarentena excede límites de seguridad.")
        
        if not path.name.strip() or path == path.parent:
            raise UnsafePathError("Ruta de cuarentena inválida: raíz o vacía.")
        
        _check_path_for_junctions(path)
        
        if is_protected_path(path):
            raise UnsafePathError("Directorio de cuarentena reside en ruta protegida.")
        
        if path.is_symlink():
             raise UnsafePathError("Ruta de cuarentena no puede ser un enlace simbólico.")
        
        if path.exists():
            if not path.is_dir():
                 raise UnsafePathError("Ruta de cuarentena apunta a un archivo existente.")
            if not os.access(path, os.W_OK | os.R_OK):
                 raise PermissionError("Directorio inaccesible para operaciones de I/O.")
            if not is_safe_to_modify(path):
                raise UnsafePathError("Ruta de cuarentena marcada como insegura.")
            if _is_filesystem_read_only(path):
                raise PermissionError("Permisos insuficientes en directorio.")
        else:
            _check_io_error_context(path.mkdir, parents=True, exist_ok=True)
            if not path.exists() or not path.is_dir():
                raise OSError("Fallo al asegurar directorio de cuarentena.")
            
        _ensure_path_ownership(path)
        return path
    except (OSError, RuntimeError, UnsafePathError, PermissionError) as e:
        raise OSError(f"Error al preparar directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ubicación del manifiesto dentro de un directorio dado."""
    return (base_dir / MANIFEST_NAME)


def _is_within_quarantine_sandbox(path: Path, root: Path) -> bool:
    """Verifica si la ruta está contenida dentro del sandbox de cuarentena."""
    return is_within_directory(path, root)

def _validate_quarantine_path(path: Path, base: Path) -> Path:
    """Valida contra escapes de sandbox al acceder a rutas en cuarentena."""
    if not is_within_directory(path, base):
        raise UnsafePathError("Acceso fuera del sandbox detectado.")
    return path

def _check_windows_file_attributes(path: Path) -> None:
    """Valida la ausencia de atributos de sistema/oculto (Windows)."""
    if _win_has_system_attributes(path):
        raise UnsafePathError("Archivo con atributos del sistema/oculto no permitido.")

def _check_device_consistency(source: Path, target_directory: Path) -> None:
    """Asegura que el origen y destino estén en el mismo volumen (atomicidad)."""
    try:
        if source.stat().st_dev != target_directory.stat().st_dev:
            raise UnsafePathError("Operación entre distintos volúmenes no permitida.")
    except OSError:
        raise UnsafePathError("No se pudo verificar la consistencia del dispositivo.")

def _verify_quarantine_preconditions(source_path: Path, target_directory: Path) -> None:
    """Valida restricciones de seguridad para el aislamiento inicial."""
    resolved_source = source_path.resolve(strict=True)
    resolved_dest_dir = target_directory.resolve()
    
    if is_protected_path(resolved_source):
        raise UnsafePathError("Ruta origen protegida.")
    if is_protected_path(resolved_dest_dir) or is_protected_path(resolved_dest_dir.parent):
        raise UnsafePathError("Destino en ruta protegida.")
    if _is_within_quarantine_sandbox(resolved_source, resolved_dest_dir):
        raise UnsafePathError("Archivo ya se encuentra en sandbox.")
    if is_within_directory(resolved_dest_dir, resolved_source):
        raise UnsafePathError("Operación recursiva prohibida: destino dentro de origen.")
    if resolved_source.parent == resolved_dest_dir:
        raise UnsafePathError("Operación circular detectada.")

def _check_isolation_safety(source_path: Path, target_directory: Path) -> None:
    """Chequeos finales de permisos y disponibilidad para aislar."""
    resolved_source = source_path.resolve(strict=True)
    resolved_dest_dir = target_directory.resolve()
    if not resolved_source.is_file():
        raise UnsafePathError("Solo se permiten archivos regulares.")
    if resolved_source.is_symlink():
        raise UnsafePathError("Aislamiento de enlaces simbólicos prohibido.")
    if resolved_source.stat().st_size == 0:
        raise UnsafePathError("Archivos vacíos prohibidos.")
    if _is_filesystem_read_only(target_directory):
        raise PermissionError("Directorio de cuarentena sin permisos de escritura.")
    
    if hasattr(os, 'getuid'):
        if resolved_source.stat().st_uid != os.getuid():
            raise UnsafePathError("Aislamiento denegado: el archivo no pertenece al usuario.")

    _check_device_consistency(resolved_source, resolved_dest_dir)
    if os.path.exists(resolved_dest_dir) and os.path.samefile(resolved_source, resolved_dest_dir):
        raise UnsafePathError("Operación circular detectada.")
        
    _verify_quarantine_preconditions(resolved_source, resolved_dest_dir)
    ensure_safe_to_modify(resolved_source, allow_sensitive=True)
    if not _is_file_exclusive(resolved_source):
        raise IOError("Archivo origen bloqueado por otro proceso.")


def _validate_isolation_request(source_path: Path, target_directory: Path) -> None:
    """Coordina las validaciones previas al aislamiento de un archivo."""
    if not target_directory.is_dir():
        raise UnsafePathError("Destino de cuarentena debe ser un directorio.")
    if target_directory.is_file():
        raise UnsafePathError("Destino bloqueado por archivo existente.")
    _check_path_syntax_integrity(source_path)
    _check_windows_file_attributes(source_path)
    if source_path.is_symlink():
        raise UnsafePathError("Aislamiento de enlaces simbólicos denegado.")
    try:
        resolved_source = source_path.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise UnsafePathError(f"Ruta origen inaccesible: {e}")
    if len(str(target_directory)) > 240:
        raise UnsafePathError("Ruta de cuarentena demasiado larga.")
    if is_within_directory(resolved_source, target_directory.resolve()):
        raise UnsafePathError("Archivo origen ya reside dentro del sandbox.")
    _ensure_disk_space(target_directory, resolved_source.stat().st_size)
    _check_isolation_safety(resolved_source, target_directory)


def load_manifest(base: PathLike = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Carga y cachea el manifiesto de cuarentena, usando tiempo de modificación para optimizar."""
    try:
        base_dir = quarantine_dir(base)
    except (OSError, UnsafePathError):
        return []
        
    m_path = _manifest_path(base_dir)
    if not m_path.exists():
        _MANIFEST_CACHE[base_dir] = ([], 0.0)
        return []

    try:
        current_mtime = m_path.stat().st_mtime
    except OSError:
        return []

    if not force_reload and base_dir in _MANIFEST_CACHE:
        cached_items, cached_mtime = _MANIFEST_CACHE[base_dir]
        if cached_mtime == current_mtime:
            return cached_items
        
    try:
        with open(m_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        if not isinstance(data, list):
            raise ValueError("Formato de manifiesto inválido: no es una lista.")
            
        items = [i for i in (QuarantineItem.from_dict(d) for d in data) if i]
                    
        _MANIFEST_CACHE[base_dir] = (items, current_mtime)
        return items
    except (OSError, json.JSONDecodeError, ValueError):
        _MANIFEST_CACHE[base_dir] = ([], 0.0)
        return []


def save_manifest(items: List[QuarantineItem], base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persiste el manifiesto usando escritura atómica y fsync."""
    base_path = quarantine_dir(base)
    target_path = _manifest_path(base_path)
    
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista de objetos.")

    try:
        serializable_items = [item.to_dict() for item in items]
        encoded_content = json.dumps(serializable_items, indent=2, ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError) as e:
        raise RuntimeError(f"Error al serializar metadatos de cuarentena: {e}")

    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=base_path, delete=False) as tf:
            temp_name = tf.name
            tf.write(encoded_content)
            tf.flush()
            os.fsync(tf.fileno())
            
        os.replace(temp_name, target_path)
        dir_fd = os.open(str(base_path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        
        _MANIFEST_CACHE[base_path] = (items, target_path.stat().st_mtime)
        return target_path
    except (OSError, IOError) as e:
        if temp_name and os.path.exists(temp_name):
            try: os.unlink(temp_name)
            except OSError: pass
        raise RuntimeError(f"Falla al persistir manifiesto en disco: {e}")


def _ensure_disk_space(dest_dir: Path, required_size: int) -> None:
    """Verifica disponibilidad de espacio con margen de seguridad del 5%."""
    if not isinstance(required_size, int) or required_size < 0:
        raise ValueError("Tamaño requerido inválido.")
    if not dest_dir.exists():
        raise FileNotFoundError(f"Directorio inexistente: {dest_dir}")
    if not is_safe_to_modify(dest_dir) or _is_filesystem_read_only(dest_dir):
        raise PermissionError(f"Sin permisos de escritura seguros: {dest_dir}")
    
    usage = shutil.disk_usage(dest_dir)
    margin = max(int(required_size * 0.05), 5 * 1024 * 1024)
    if usage.free < (required_size + margin):
        raise OSError("Espacio insuficiente en disco.")


def _validate_file_transfer_preconditions(source: Path, destination: Path) -> None:
    """Verificación de seguridad previa a la transferencia de datos."""
    if is_protected_path(destination):
        raise UnsafePathError("Destino en ruta protegida.")
    if not is_safe_to_modify(destination.parent):
        raise UnsafePathError("Directorio destino no seguro.")
    _check_device_consistency(source, destination.parent.resolve())
    _check_windows_file_attributes(destination)
    if not source.is_file():
        raise OSError("Archivo origen inaccesible.")
    
    if os.name == 'nt' and _win_has_system_attributes(source):
        raise PermissionError("Archivo origen marcado como solo lectura/sistema.")

    if destination.exists():
        if os.path.samefile(source, destination):
            raise UnsafePathError("Operación circular detectada.")
        raise FileExistsError(f"El destino ya existe: {destination}")


def _create_temp_file(source: Path, destination: Path) -> Path:
    """Crea una ruta temporal para la transferencia atómica."""
    return destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"


def _verify_copied_data(source_stat: os.stat_result, dest_path: Path, source_hash: str) -> None:
    """Verifica integridad del archivo tras la copia."""
    if dest_path.stat().st_size != source_stat.st_size:
        raise OSError("Falla de integridad: tamaño mismatch tras copia.")
    final_hash = _get_sha256(dest_path)
    if not final_hash or final_hash != source_hash:
        raise OSError("Falla crítica: el hash del archivo copiado no coincide.")

def _perform_secure_copy(source: Path, temp_dest: Path, source_hash: str) -> None:
    """Copia archivo de forma streaming con fsync."""
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
        
    source_fd = os.open(str(source), flags)
    try:
        with os.fdopen(source_fd, "rb") as source_handle:
            stat_src = os.fstat(source_handle.fileno())
            if not (stat_src.st_mode & 0o100000):
                raise OSError("El archivo origen no es un archivo regular.")
            
            if os.name == 'nt':
                _check_path_for_junctions(source)
            
            if stat_src.st_nlink > 1:
                raise UnsafePathError("Archivo origen con enlaces físicos múltiples.")
            
            if not is_safe_to_modify(temp_dest.parent):
                raise UnsafePathError("Directorio de destino no seguro.")
            
            if not is_safe_to_modify(temp_dest):
                raise UnsafePathError("Ruta destino insegura detectada.")
            
            dest_fd = os.open(str(temp_dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(dest_fd, "wb") as dest_handle:
                    shutil.copyfileobj(source_handle, dest_handle)
                    dest_handle.flush()
                    os.fsync(dest_fd)
            except Exception:
                os.close(dest_fd)
                if temp_dest.exists():
                    _check_io_error_context(temp_dest.unlink)
                raise
        _verify_copied_data(stat_src, temp_dest, source_hash)
    except (OSError, IOError) as e:
        raise OSError(f"Falla durante operación I/O de copia: {e}")


def _write_temp_to_final(source: Path, destination: Path) -> Tuple[str, Inode]:
    """Transfiere archivo al destino final con reemplazo atómico."""
    _check_path_syntax_integrity(destination)
    _validate_file_transfer_preconditions(source, destination)
    
    if not source.is_file():
        raise FileNotFoundError("Archivo origen no encontrado.")
        
    source_hash = _get_sha256(source)
    temp_dest = _create_temp_file(source, destination)
    
    try:
        _perform_secure_copy(source, temp_dest, source_hash)
        os.replace(temp_dest, destination)
        
        dir_fd = os.open(str(destination.parent), os.O_RDONLY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
        
        if destination.stat().st_nlink > 1:
             raise UnsafePathError("Integridad comprometida: el archivo destino tiene enlaces múltiples.")

        ensure_safe_to_modify(destination, allow_sensitive=True)
        return source_hash, destination.stat().st_ino
    except Exception as e:
        if temp_dest.exists():
            try: _check_io_error_context(temp_dest.unlink)
            except OSError: pass
        if destination.exists() and not os.path.samefile(source, destination):
            _safe_unlink(destination)
        raise OSError(f"Error crítico en transferencia: {e}")


class IsolationManager:
    """Gestiona validaciones para asegurar atomicidad de aislamiento."""
    
    @staticmethod
    def validate_atomicity(source: Path, original_stat: os.stat_result, dest: Path) -> None:
        """Verifica que el archivo no haya cambiado (TOCTOU) durante el proceso."""
        if not source.exists():
            raise FileNotFoundError("Archivo origen eliminado antes del aislamiento.")
        if source.stat().st_ino != original_stat.st_ino:
            raise RuntimeError("Integridad comprometida: archivo reemplazado (TOCTOU).")
        if source.stat().st_nlink > 1:
            raise UnsafePathError("Aislamiento denegado: múltiples enlaces físicos.")
        
        dest_res = dest.resolve()
        base_res = dest.parent.resolve()
        if not is_within_directory(dest_res, base_res):
            raise UnsafePathError("Intento de escape del sandbox destino.")
        if not is_safe_to_modify(dest.parent):
            raise UnsafePathError("Sandbox destino no es una ruta segura.")

def _validate_integrity_before_move(source: Path, original_size: int, original_inode: Inode) -> None:
    """Verifica mediante inodo y tamaño que el archivo original no fue sustituido."""
    try:
        current_stat = source.stat()
        if current_stat.st_size != original_size or current_stat.st_ino != original_inode:
            raise RuntimeError("Integridad comprometida: el archivo fue modificado (TOCTOU).")
    except OSError as e:
        raise RuntimeError(f"Falla al verificar integridad del origen antes de mover: {e}")

def _atomic_isolate_file(source: Path, destination: Path, original_size: int) -> Tuple[str, Inode]:
    """Ejecuta aislamiento atómico coordinando estados de validación."""
    _check_path_syntax_integrity(destination)
    if not source.exists():
        raise FileNotFoundError("Archivo origen no existe.")
    
    try:
        pre_stat = source.stat()
    except OSError:
        raise RuntimeError("No se pudo obtener estado de archivo.")
        
    stat_orig = _check_io_error_context(source.stat)
    if stat_orig.st_size != original_size or stat_orig.st_ino != pre_stat.st_ino:
        raise RuntimeError("El archivo cambió durante la validación inicial (TOCTOU).")
    
    IsolationManager.validate_atomicity(source, stat_orig, destination)
    
    if len(str(destination)) >= 250:
        raise OSError("Ruta destino demasiado larga.")
        
    existing_items = load_manifest(destination.parent.parent)
    if any(i.file_inode == stat_orig.st_ino for i in existing_items):
        raise RuntimeError("Colisión de inodo: el archivo parece estar ya registrado.")

    _validate_integrity_before_move(source, original_size, stat_orig.st_ino)

    try:
        if destination.exists():
            raise FileExistsError("Colisión de ruta: archivo destino ya presente.")
        return _write_temp_to_final(source, destination)
    except (OSError, IOError, RuntimeError) as e:
        if destination.exists() and not os.path.samefile(source, destination):
            _safe_unlink(destination)
        raise RuntimeError(f"Error durante aislamiento: {e}")


def _register_quarantine_item(
    destination: Path,
    source_path: Path,
    file_hash: str,
    file_inode: Inode,
    reason: str,
    original_size: int,
    base: PathLike
) -> QuarantineItem:
    """Registra metadatos en el manifiesto JSON tras validar el archivo en sandbox."""
    try:
        items_list = load_manifest(base)
        quarantine_item = QuarantineItem(
            item_id=uuid.uuid4().hex[:12],
            original_path=str(source_path),
            stored_name=destination.name,
            size_bytes=original_size,
            reason=reason if reason else "Sin motivo",
            quarantined_at=datetime.now().isoformat(timespec="seconds"),
            sha256=file_hash,
            file_inode=file_inode,
        )
        items_list.append(quarantine_item)
        save_manifest(items_list, base)
        return quarantine_item
    except (OSError, IOError, ValueError) as e:
        if destination.exists():
            _safe_unlink(destination)
        raise RuntimeError(f"Falla al registrar ítem en manifiesto: {e}")


def _validate_source_for_quarantine(source: Path) -> Path:
    """Valida si el archivo cumple prerrequisitos para entrar en cuarentena."""
    if source.is_symlink():
        raise UnsafePathError("Aislamiento de enlaces simbólicos no permitido.")
    _check_path_for_junctions(source)
    if source.is_dir():
        raise UnsafePathError("Aislamiento de directorios no permitido.")
    if not source.is_file():
        raise FileNotFoundError("Archivo origen inexistente.")
    
    if is_protected_path(source):
        raise UnsafePathError("La ruta origen está en una zona protegida.")
    
    if hasattr(os, 'getuid') and source.stat().st_uid != os.getuid():
        raise UnsafePathError("Aislamiento denegado: el archivo origen no pertenece al usuario.")

    _verify_source_fs_permissions(source)
    return source

def _cleanup_orphaned_destination(destination: Path) -> None:
    """Elimina residuos si un proceso de aislamiento falla."""
    if destination.exists():
        _safe_unlink(destination)

def _verify_transaction_integrity(item: QuarantineItem, destination: Path) -> None:
    """Verifica la integridad de la transacción post-registro."""
    if not item.verify_integrity(destination):
        raise RuntimeError("Integridad post-registro fallida.")

def _validate_input_path(source: PathLike) -> Path:
    """Valida existencia y acceso de la ruta origen previniendo inyecciones."""
    if source is None:
        raise ValueError("Ruta de origen nula.")
    
    p_source = Path(str(source))
    if not str(p_source).strip():
        raise ValueError("Ruta de origen vacía.")
    
    if str(p_source).startswith(r"\\") or str(p_source).startswith("//"):
        raise UnsafePathError("Rutas UNC no permitidas.")
        
    if len(str(p_source)) > 260:
        raise UnsafePathError("Ruta de entrada excede límites del sistema.")
        
    if not p_source.is_absolute():
        try:
            p_source = p_source.resolve(strict=True)
        except (OSError, RuntimeError) as e:
            raise UnsafePathError(f"Ruta origen no válida: {e}")
            
    if p_source == p_source.anchor:
        raise UnsafePathError("No se permite aislar la raíz de una unidad.")

    if not p_source.exists():
        raise FileNotFoundError("Archivo origen no encontrado.")
    
    _check_path_for_junctions(p_source)
    
    if not p_source.is_file():
        raise ValueError("El origen debe ser un archivo regular.")
    if not os.access(p_source, os.R_OK):
        raise PermissionError("Archivo origen sin permisos de lectura.")
    return p_source

def quarantine_file(
    source: PathLike,
    reason: str = "Marcado como sospechoso",
    base: PathLike = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """
    Aísla un archivo sospechoso en el sandbox de cuarentena, moviéndolo de forma atómica.
    
    :param source: Ruta del archivo a aislar.
    :param reason: Motivo del aislamiento.
    :param base: Directorio base de cuarentena.
    :return: Objeto QuarantineItem con los metadatos registrados.
    """
    p_source = _validate_input_path(source)
    st_info = _check_io_error_context(p_source.stat)
    source_path = _validate_source_for_quarantine(p_source)
    
    if _is_file_in_use_by_system(source_path):
        raise IOError("Archivo origen bloqueado por el sistema: operación abortada.")
        
    dest_dir = quarantine_dir(base)
    if _is_within_quarantine_sandbox(source_path, dest_dir.resolve()):
        raise UnsafePathError("Archivo ya en el sandbox.")
    
    _validate_isolation_request(source_path, dest_dir)
    destination = dest_dir / _generate_safe_stored_name(source_path, uuid.uuid4().hex[:12])
    
    file_hash, file_inode = _atomic_isolate_file(source_path, destination, st_info.st_size)
    
    if not destination.exists() or destination.stat().st_ino != file_inode or _get_sha256(destination) != file_hash:
        raise RuntimeError("Falla crítica: el destino no es coherente tras la copia.")
            
    item = _register_quarantine_item(destination, source_path, file_hash, file_inode, reason, st_info.st_size, base)
        
    if source_path.exists() and not os.path.samefile(source_path, destination):
        try:
            _check_io_error_context(source_path.unlink)
        except OSError as e:
            raise RuntimeError(f"Aislamiento exitoso, falla al remover original: {e}")
                
    _verify_transaction_integrity(item, destination)
    return item

def list_items(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna una lista de ítems en cuarentena, ordenados descendentemente por fecha."""
    return sorted(load_manifest(base), key=lambda x: x.quarantined_at, reverse=True)


def restore_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un ítem de cuarentena a su ubicación original validando integridad."""
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID de ítem vacío o inválido.")
    
    try:
        base_path = quarantine_dir(base)
        manifest = load_manifest(base)
        quarantine_item = next((i for i in manifest if i.item_id == item_id), None)
        
        if not quarantine_item:
            raise KeyError(f"Ítem no encontrado: {item_id}")
        
        stored_file = base_path / quarantine_item.stored_name
        
        if not stored_file.exists() or not quarantine_item.verify_integrity(stored_file):
            items = load_manifest(base, force_reload=True)
            save_manifest([i for i in items if i.item_id != item_id], base)
            raise RuntimeError(f"Archivo en cuarentena inexistente o corrupto: {item_id}")
            
        destination = Path(quarantine_item.original_path).resolve()
        _check_path_syntax_integrity(destination)
        
        if destination.exists():
            raise FileExistsError(f"El destino ya existe: {destination}")
            
        if is_protected_path(destination):
            raise UnsafePathError("Restauración denegada: destino protegido.")
            
        parent = destination.parent.resolve()
        if not is_safe_to_modify(parent):
            raise UnsafePathError("Directorio padre no seguro.")
        
        _ensure_disk_space(parent, quarantine_item.size_bytes)
        if not parent.exists():
            _check_io_error_context(parent.mkdir, parents=True, exist_ok=True)
        
        os.replace(str(stored_file), str(destination))
        
        for p in [parent, base_path]:
            if p.exists():
                dir_fd = os.open(str(p), os.O_RDONLY)
                try: os.fsync(dir_fd)
                finally: os.close(dir_fd)
            
        items = load_manifest(base, force_reload=True)
        save_manifest([i for i in items if i.item_id != item_id], base)
        return destination
    except (OSError, PermissionError, IOError, KeyError) as e:
        raise RuntimeError(f"Error crítico en restauración: {e}")


def purge_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> bool:
    """Elimina permanentemente un ítem del sistema de cuarentena."""
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID de ítem vacío o inválido.")
    base_path = quarantine_dir(base)
    
    items = load_manifest(base)
    quarantine_item = next((i for i in items if i.item_id == item_id), None)
    if quarantine_item is None:
        return False
        
    stored_file = _validate_quarantine_path(base_path / quarantine_item.stored_name, base_path)
    if not stored_file.exists():
        items = load_manifest(base, force_reload=True)
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
    
    if not quarantine_item.verify_integrity(stored_file):
        raise UnsafePathError(f"Integridad fallida para {item_id}.")
        
    if _safe_unlink(stored_file, expected_hash=quarantine_item.sha256, expected_inode=quarantine_item.file_inode):
        items = load_manifest(base, force_reload=True)
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
    return False


def _is_item_purgable(file_path: Path, item: QuarantineItem) -> bool:
    """Verifica condiciones de purgado según integridad."""
    return (
        file_path.exists() and 
        file_path.is_file() and 
        not file_path.is_symlink() and
        item.verify_integrity(file_path) and
        _safe_unlink(file_path, expected_hash=item.sha256, expected_inode=item.file_inode)
    )


def purge_all(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Vacía toda la cuarentena y sincroniza el manifiesto."""
    try:
        quarantine_root = quarantine_dir(base).resolve()
        if _is_filesystem_read_only(quarantine_root):
            return 0
            
        items = load_manifest(base)
        item_map = {i.stored_name: i for i in items}
        purgado_ids: Set[str] = set()
        
        for f in quarantine_root.iterdir():
            if f.name == MANIFEST_NAME or is_protected_path(f):
                continue
            
            if f.name in item_map:
                if _is_item_purgable(f, item_map[f.name]):
                    purgado_ids.add(item_map[f.name].item_id)
            elif is_safe_to_modify(f) and _safe_unlink(f):
                continue
        
        if purgado_ids:
            new_items = [i for i in items if i.item_id not in purgado_ids]
            save_manifest(new_items, base)
            
            if quarantine_root.exists():
                dir_fd = os.open(str(quarantine_root), os.O_RDONLY)
                try: os.fsync(dir_fd)
                finally: os.close(dir_fd)
            
        return len(purgado_ids)
    except (OSError, PermissionError, UnsafePathError):
        return 0


def total_quarantined_bytes(base: PathLike = DEFAULT_QUARANTINE_DIR, items: Optional[List[QuarantineItem]] = None) -> int:
    """Calcula el peso total ocupado por ítems en cuarentena."""
    return sum(item.size_bytes for item in (items or load_manifest(base)))


def summarize(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera una descripción legible para el usuario final sobre el estado del sandbox."""
    items = list_items(base)
    if not items:
        return ["La cuarentena está vacía."]
    
    total_mb = sum(i.size_mb for i in items)
    lines = [f"{len(items)} archivo(s) en cuarentena — {total_mb:.2f} MB", ""]
    for item in items:
        lines.extend([
            f"  [{item.item_id}] {Path(item.original_path).name} — {item.size_mb} MB",
            f"      Motivo: {item.reason}",
            f"      Origen: {item.original_path}",
            f"      Aislado: {item.quarantined_at}"
        ])
    lines.extend(["", "Nada de esto se borró: se puede restaurar a su ubicación original."])
    return lines
