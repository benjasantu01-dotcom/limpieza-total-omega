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
from typing import List, Union, Dict, Any, TypeAlias, Set, Tuple, Optional, Callable

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
    """Verifica si una ruta es un punto de reparse o junction usando la API de Windows."""
    if os.name != 'nt':
        return
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs != -1 and (attrs & 0x400):  # FILE_ATTRIBUTE_REPARSE_POINT
            raise UnsafePathError("Ruta detectada como punto de reparse/junction.")
    except (OSError, AttributeError):
        pass

def _is_filesystem_read_only(path: Path) -> bool:
    """Detecta si un sistema de archivos está montado en modo solo lectura."""
    try:
        test_file = path / f".test_{uuid.uuid4().hex}"
        test_file.touch()
        test_file.unlink()
        return False
    except OSError:
        return True

def _check_io_error_context(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """
    Ejecuta una función de E/S con reintentos exponenciales para manejar
    bloqueos transitorios causados por indexadores de archivos o antivirus.
    """
    max_retries = 3
    for i in range(max_retries):
        try:
            return func(*args, **kwargs)
        except (OSError, IOError, PermissionError) as e:
            if i == max_retries - 1:
                raise e
            time.sleep(0.1 * (2 ** i))

def _is_file_exclusive(path: Path) -> bool:
    """
    Determina si el archivo puede abrirse en modo exclusivo sin ser bloqueado 
    por otro proceso, usando APIs nativas del SO.
    """
    if os.name == 'nt':
        try:
            k32 = ctypes.windll.kernel32
            handle = k32.CreateFileW(str(path), 0x80000000 | 0x40000000, 0, None, 3, 0x00000080, None)
            if handle == -1: return False
            overlapped = ctypes.create_string_buffer(20)
            locked = k32.LockFileEx(handle, 2, 0, 1, 0, overlapped)
            if locked: k32.UnlockFileEx(handle, 0, 1, 0, overlapped)
            k32.CloseHandle(handle)
            return bool(locked)
        except (OSError, AttributeError, ValueError): return False
    
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
    """Modelo de datos inmutable para un registro de archivo en cuarentena."""
    item_id: str
    original_path: str
    stored_name: str
    size_bytes: int
    reason: str
    quarantined_at: str
    sha256: str = ""
    file_inode: Inode = 0  # Identificador de inodo para validación TOCTOU

    def __post_init__(self) -> None:
        try:
            self.size_bytes = int(self.size_bytes)
        except (ValueError, TypeError):
            self.size_bytes = 0
        if not isinstance(self.item_id, str) or not self.item_id:
            raise ValueError("ID de ítem vacío o inválido")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("Motivo no especificado")

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño en MB para reportes de usuario."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el ítem a un diccionario plano."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Optional[QuarantineItem]:
        """Reconstruye un QuarantineItem validando tipos y existencia de campos."""
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
        """
        Verifica atributos físicos de seguridad: propiedad, inodo, tamaño y
        ausencia de enlaces simbólicos o compartidos.
        """
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
        """Realiza comprobación completa: validación de estructura + hash SHA-256."""
        if not self._validate_integrity(stored_path):
            return False
        try:
            current_hash = _get_sha256(stored_path)
            return bool(self.sha256 and current_hash == self.sha256)
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """
    Calcula el hash SHA-256 de un archivo mediante streaming (bloques de 128KB)
    para evitar el consumo excesivo de RAM en archivos grandes.
    """
    if not path.exists() or not path.is_file():
        return ""
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
        
    try:
        sha256_hash = hashlib.sha256()
        fd = os.open(str(path), flags)
        try:
            with os.fdopen(fd, "rb") as handle:
                while True:
                    chunk = handle.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    sha256_hash.update(chunk)
        except (OSError, IOError):
            os.close(fd)
            raise
    except (OSError, PermissionError, IOError):
        return ""
    return sha256_hash.hexdigest()


def _is_file_in_use_by_system(path: Path) -> bool:
    """Verifica si el archivo está en uso exclusivo por el sistema o posee múltiples enlaces."""
    if not path.exists():
        return False
    
    try:
        if path.stat().st_nlink > 1:
            return True
    except OSError:
        return True

    if os.name == 'nt':
        try:
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs != -1 and (attrs & 0x02 or attrs & 0x04): return True
        except (OSError, AttributeError, ValueError):
            return True
            
    return not _is_file_exclusive(path)

def _is_file_locked(path: Path) -> bool:
    """Wrapper para chequeo de bloqueos de archivos en el sistema operativo."""
    return _is_file_in_use_by_system(path)

def _safe_unlink(path: Path, expected_hash: Optional[str] = None, expected_inode: Inode = 0) -> bool:
    """
    Eliminación segura: requiere validación de inodo, hash y ausencia de 
    bloqueos/enlaces antes de proceder con el unlink.
    """
    if is_protected_path(path):
        return False
        
    if not path.exists() or not path.is_file():
        return False
    if path.is_symlink():
        return False
    
    try:
        st = path.stat()
        if st.st_nlink > 1:
            return False
        if expected_inode != 0 and st.st_ino != expected_inode:
            return False
        if hasattr(os, 'getuid') and st.st_uid != os.getuid():
            return False
            
        if expected_hash and _get_sha256(path) != expected_hash:
            return False
            
        if _is_file_locked(path):
            return False
            
        parent = path.parent
        _check_io_error_context(path.unlink)
        
        dir_fd = os.open(str(parent), os.O_RDONLY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
        
        return True
    except (OSError, PermissionError):
        return False

def _check_path_syntax_integrity(path: Path) -> None:
    """Valida que la ruta no contenga caracteres de control, ADS, o profundidad excesiva."""
    path_str = str(path)
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
    """Filtra caracteres no alfanuméricos para generar nombres de archivo seguros."""
    return "".join(c for c in filename if c.isalnum() or c in "._-")

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """Genera un nombre para el archivo en cuarentena basado en su ID y nombre base."""
    if not item_id:
        raise ValueError("ID de ítem requerido para generar nombre seguro.")
    
    sanitized = _sanitize_filename(original_path.name)
    if not sanitized or sanitized in (".", ".."):
        sanitized = "unknown_file"
    parts = sanitized.split('.')
    name_base = parts[0] if parts[0] else "q_file"
    
    if name_base.upper() in WINDOWS_RESERVED_NAMES:
        name_base = f"q_{name_base}"
        
    name_base = "".join(c for c in name_base if c.isprintable() and c not in '<>:"/\\|?*')
    extension = f".{parts[-1]}" if len(parts) > 1 else ""
    
    candidate = f"{item_id}__{name_base[:64]}{extension}".replace(":", "_")[:128]
    return candidate

def _ensure_path_ownership(path: Path) -> None:
    """Verifica la propiedad del directorio en sistemas POSIX para prevenir ataques de symlink."""
    if hasattr(os, 'getuid'):
        try:
            if path.stat().st_uid != os.getuid():
                raise UnsafePathError("Propiedad de directorio no coincide con usuario.")
        except OSError:
            raise UnsafePathError("No se pudo verificar la propiedad del directorio.")

def quarantine_dir(base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Normaliza y valida el directorio de cuarentena, asegurando que sea un destino seguro."""
    if not base or not str(base).strip():
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path = Path(str(base)).expanduser().resolve()
        _check_path_for_junctions(path)
        if not path.name.strip() or path == path.parent:
            raise UnsafePathError("Ruta de cuarentena inválida o es raíz.")
        
        if is_protected_path(path):
            raise UnsafePathError("Directorio de cuarentena reside en ruta protegida.")
        
        if path.is_symlink():
             raise UnsafePathError("Ruta de cuarentena no puede ser un enlace simbólico.")
        
        if not is_safe_to_modify(path):
            raise UnsafePathError("Ruta de cuarentena marcada como insegura.")
            
        if not path.exists():
            _check_io_error_context(path.mkdir, parents=True, exist_ok=True)
            
        if not os.access(path, os.W_OK | os.R_OK):
            raise PermissionError("Permisos insuficientes en directorio.")
        _ensure_path_ownership(path)
        return path
    except (OSError, RuntimeError, UnsafePathError, PermissionError) as e:
        raise OSError(f"Error al preparar directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ubicación absoluta del manifiesto JSON."""
    return (base_dir / MANIFEST_NAME).resolve()


def _is_within_quarantine_sandbox(path: Path, root: Path) -> bool:
    """Verifica si una ruta está confinada estrictamente al sandbox (quarantine_dir)."""
    return is_within_directory(path, root)

def _validate_quarantine_path(path: Path, base: Path) -> Path:
    """Valida la integridad de la ruta dentro del sandbox tras la resolución."""
    resolved_path = path.resolve()
    resolved_base = base.resolve()
    if not is_within_directory(resolved_path, resolved_base):
        raise UnsafePathError("Acceso fuera del sandbox detectado.")
    if resolved_path.name != path.name:
        raise UnsafePathError("Intento de manipulación de ruta detectado.")
    return resolved_path

def _check_windows_file_attributes(path_str: str) -> None:
    """Filtra archivos con atributos especiales (oculto/sistema) en Windows."""
    if os.name != 'nt':
        return
    path_obj = Path(path_str)
    if not path_obj.exists():
        return
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path_obj))
        if attrs != -1:
            if attrs & 0x02 or attrs & 0x04:
                raise UnsafePathError("Archivo con atributos del sistema/oculto no permitido.")
    except (OSError, AttributeError):
        pass

def _check_device_consistency(source: Path, target_dir: Path) -> None:
    """Valida que la operación de transferencia ocurra dentro del mismo dispositivo/volumen."""
    try:
        if source.stat().st_dev != target_dir.stat().st_dev:
            raise UnsafePathError("Operación entre distintos volúmenes no permitida.")
    except OSError:
        raise UnsafePathError("No se pudo verificar la consistencia del dispositivo.")

def _verify_quarantine_preconditions(source_path: Path, dest_dir: Path) -> None:
    """Validaciones jerárquicas de seguridad previas al aislamiento."""
    resolved_source = source_path.resolve(strict=True)
    resolved_dest_dir = dest_dir.resolve()
    
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

def _check_isolation_safety(source_path: Path, dest_dir: Path) -> None:
    """Chequeos finales de seguridad antes de mover datos al sandbox."""
    resolved_source = source_path.resolve(strict=True)
    resolved_dest_dir = dest_dir.resolve()
    if not resolved_source.is_file():
        raise UnsafePathError("Solo se permiten archivos regulares.")
    if resolved_source.is_symlink():
        raise UnsafePathError("Aislamiento de enlaces simbólicos prohibido.")
    if resolved_source.stat().st_size == 0:
        raise UnsafePathError("Archivos vacíos prohibidos.")
    if not os.access(dest_dir, os.W_OK):
        raise PermissionError("Directorio de cuarentena sin permisos de escritura.")
    
    _check_device_consistency(resolved_source, resolved_dest_dir)
    if os.path.samefile(resolved_source, resolved_dest_dir):
        raise UnsafePathError("Operación circular detectada.")
        
    _verify_quarantine_preconditions(resolved_source, resolved_dest_dir)
    ensure_safe_to_modify(resolved_source, allow_sensitive=True)
    if not _is_file_exclusive(resolved_source):
        raise IOError("Archivo origen bloqueado por otro proceso.")


def _validate_isolation_request(source_path: Path, dest_dir: Path) -> None:
    """Valida precondiciones completas, incluyendo sintaxis y permisos, para la solicitud de aislamiento."""
    if not dest_dir.is_dir():
        raise UnsafePathError("Destino de cuarentena debe ser un directorio.")
    _check_path_syntax_integrity(source_path)
    _check_windows_file_attributes(str(source_path))
    if source_path.is_symlink():
        raise UnsafePathError("Aislamiento de enlaces simbólicos denegado.")
    try:
        resolved_source = source_path.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise UnsafePathError(f"Ruta origen inaccesible: {e}")
    if len(str(dest_dir)) > 240:
        raise UnsafePathError("Ruta de cuarentena demasiado larga.")
    _ensure_disk_space(dest_dir, resolved_source.stat().st_size)
    _check_isolation_safety(resolved_source, dest_dir)


def load_manifest(base: PathLike = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Deserializa el manifiesto, implementando caché perezosa basada en el mtime del archivo."""
    try:
        base_dir = quarantine_dir(base)
    except (OSError, UnsafePathError):
        return []
        
    m_path = _manifest_path(base_dir)
    if not m_path.exists():
        _MANIFEST_CACHE[base_dir] = ([], 0.0)
        return []

    current_mtime = m_path.stat().st_mtime
    if not force_reload and base_dir in _MANIFEST_CACHE:
        cached_items, cached_mtime = _MANIFEST_CACHE[base_dir]
        if cached_mtime == current_mtime:
            return cached_items
        
    try:
        with open(m_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        if not isinstance(data, list):
            raise ValueError("Formato de manifiesto inválido.")
            
        items = [i for d in data if (i := QuarantineItem.from_dict(d))]
        _MANIFEST_CACHE[base_dir] = (items, current_mtime)
        return items
    except (OSError, json.JSONDecodeError, ValueError):
        _MANIFEST_CACHE[base_dir] = ([], current_mtime)
        return []


def save_manifest(items: List[QuarantineItem], base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persiste el manifiesto usando escritura atómica y actualiza la caché de estado."""
    base_path = quarantine_dir(base)
    target_path = _manifest_path(base_path)
    
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista de ítems.")

    try:
        serializable_items = [item.to_dict() for item in items]
        encoded_content = json.dumps(serializable_items, indent=2, ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError) as e:
        raise RuntimeError(f"Falla de serialización: {e}")

    try:
        if target_path.exists():
            if not target_path.is_file():
                raise PermissionError("Manifiesto no es un archivo regular.")
            if not os.access(target_path, os.W_OK):
                raise PermissionError("Manifiesto existente no es escribible.")
            if os.name == 'nt':
                attrs = ctypes.windll.kernel32.GetFileAttributesW(str(target_path))
                if attrs != -1 and (attrs & 0x02 or attrs & 0x04):
                    raise PermissionError("Manifiesto con atributos restringidos.")

        with tempfile.NamedTemporaryFile("wb", dir=base_path, delete=False) as tf:
            tf.write(encoded_content)
            tf.flush()
            os.fsync(tf.fileno())
            temp_name = tf.name
            
        os.replace(temp_name, target_path)
        
        dir_fd = os.open(str(base_path), os.O_RDONLY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
        
        _MANIFEST_CACHE[base_path] = (items, target_path.stat().st_mtime)
        return target_path
    except (OSError, IOError) as e:
        raise RuntimeError(f"Error crítico al persistir manifiesto: {e}")


def _ensure_disk_space(dest_dir: Path, required_size: int) -> None:
    """Verifica disponibilidad real de espacio en el volumen de destino."""
    if not isinstance(required_size, int) or required_size < 0:
        raise ValueError("Tamaño requerido inválido.")
    if not dest_dir.exists():
        raise FileNotFoundError(f"Directorio inexistente: {dest_dir}")
    if not is_safe_to_modify(dest_dir) or not os.access(dest_dir, os.W_OK):
        raise PermissionError(f"Sin permisos de escritura seguros: {dest_dir}")
    
    if _is_filesystem_read_only(dest_dir):
        raise OSError("Sistema de archivos del destino marcado como solo lectura.")
        
    usage = shutil.disk_usage(dest_dir)
    margin = max(int(required_size * 0.05), 5 * 1024 * 1024)
    if usage.free < (required_size + margin):
        raise OSError("Espacio insuficiente en disco.")


def _validate_file_transfer_preconditions(source: Path, destination: Path) -> None:
    """Verifica seguridad del destino antes de iniciar la transferencia de archivos."""
    if is_protected_path(destination):
        raise UnsafePathError("Destino en ruta protegida.")
    if not is_safe_to_modify(destination.parent):
        raise UnsafePathError("Directorio destino no seguro.")
    _check_device_consistency(source, destination.parent.resolve())
    _check_windows_file_attributes(str(destination))
    if not source.is_file():
        raise OSError("Archivo origen inaccesible para copia.")
    if os.name == 'nt':
        try:
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(source))
            if attrs != -1 and (attrs & 0x01):
                raise PermissionError("Archivo origen marcado como solo lectura.")
        except (OSError, AttributeError):
            pass
    if destination.exists():
        raise FileExistsError(f"El destino ya existe: {destination}")


def _create_temp_file(source: Path, destination: Path) -> Path:
    """Genera ruta temporal única para el proceso de transferencia de aislamiento."""
    return destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"


def _verify_copied_data(source_stat: os.stat_result, dest_path: Path, source_hash: str) -> None:
    """Valida la integridad de la copia mediante comparación de metadatos y hash."""
    if dest_path.stat().st_size != source_stat.st_size:
        raise OSError("Falla de integridad: tamaño mismatch tras copia.")
    final_hash = _get_sha256(dest_path)
    if not final_hash or final_hash != source_hash:
        raise OSError("Falla crítica: el hash del archivo copiado no coincide.")

def _perform_secure_copy(source: Path, temp_dest: Path, source_hash: str) -> None:
    """Realiza la copia física de datos usando streaming seguro y validación de atributos."""
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
        
    try:
        source_fd = os.open(str(source), flags)
    except OSError as e:
        raise OSError(f"No se pudo abrir el origen de forma segura: {e}")
    try:
        with os.fdopen(source_fd, "rb") as source_handle:
            stat_src = os.fstat(source_handle.fileno())
            if not (stat_src.st_mode & 0o100000):
                raise OSError("El archivo origen no es un archivo regular.")
            
            if stat_src.st_nlink > 1:
                raise UnsafePathError("Archivo origen con enlaces físicos múltiples.")
            
            if not is_safe_to_modify(temp_dest.parent):
                raise UnsafePathError("Directorio de destino no seguro.")
            
            dest_fd = os.open(str(temp_dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(dest_fd, "wb") as dest_handle:
                    shutil.copyfileobj(source_handle, dest_handle)
                    dest_handle.flush()
                    os.fsync(dest_fd)
            except Exception:
                os.close(dest_fd)
                raise
        _verify_copied_data(stat_src, temp_dest, source_hash)
    except (OSError, IOError) as e:
        if temp_dest.exists():
            try: _check_io_error_context(temp_dest.unlink)
            except OSError: pass
        raise OSError(f"Falla durante operación I/O de copia: {e}")


def _write_temp_to_final(source: Path, destination: Path) -> Tuple[str, Inode]:
    """Gestiona la secuencia de aislamiento: copia, valida y reemplaza archivo original."""
    _check_path_syntax_integrity(destination)
    _validate_file_transfer_preconditions(source, destination)
    
    if not source.is_file():
        raise FileNotFoundError("Archivo origen no encontrado o no es un archivo.")
    if destination.exists():
        raise FileExistsError("Colisión de ruta: el archivo destino ya existe.")
    if not is_safe_to_modify(destination.parent):
        raise UnsafePathError("Operación denegada en ruta no segura.")
        
    source_hash = _get_sha256(source)
    temp_dest = _create_temp_file(source, destination)
    
    try:
        _perform_secure_copy(source, temp_dest, source_hash)
        os.replace(temp_dest, destination)
        
        dir_fd = os.open(str(destination.parent), os.O_RDONLY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
        
        ensure_safe_to_modify(destination, allow_sensitive=True)
        return source_hash, destination.stat().st_ino
    except Exception as e:
        if temp_dest.exists():
            try: _check_io_error_context(temp_dest.unlink)
            except OSError: pass
        if destination.exists():
            _safe_unlink(destination)
        raise OSError(f"Error crítico en transferencia: {e}")


def _atomic_isolate_file(source: Path, destination: Path, original_size: int) -> Tuple[str, Inode]:
    """Ejecuta el aislamiento atómico asegurando integridad contra ataques TOCTOU."""
    if not source.exists():
        raise FileNotFoundError("Archivo origen no existe.")
    
    stat_orig = _check_io_error_context(source.stat)
    if stat_orig.st_size != original_size:
        raise RuntimeError("El archivo cambió durante la validación inicial (TOCTOU).")
    
    if stat_orig.st_nlink > 1:
        raise UnsafePathError("Aislamiento denegado: el archivo tiene enlaces físicos múltiples.")
    
    dest_resolved = destination.resolve()
    base_resolved = destination.parent.resolve()
    
    if base_resolved.is_symlink() or (os.name == 'nt' and _check_path_for_junctions(base_resolved)):
        raise UnsafePathError("Sandbox destino inválido: no es una ruta física directa.")

    if not is_within_directory(dest_resolved, base_resolved):
        raise UnsafePathError("Intento de escape del sandbox detectado.")
        
    if source.resolve() == dest_resolved:
        raise UnsafePathError("El origen ya reside en el directorio destino.")
    _validate_quarantine_path(destination, destination.parent)
    
    if len(str(destination)) >= 250:
        raise OSError("Ruta destino demasiado larga.")
        
    if not is_safe_to_modify(destination.parent):
        raise UnsafePathError("El sandbox destino ha sido invalidado.")

    if source.stat().st_ino != stat_orig.st_ino:
        raise RuntimeError("Integridad comprometida: el archivo fue reemplazado (TOCTOU).")

    existing_items = load_manifest(destination.parent.parent)
    if any(i.file_inode == stat_orig.st_ino for i in existing_items):
        raise RuntimeError("Colisión de inodo: el archivo parece estar ya registrado.")

    try:
        if destination.exists():
            raise FileExistsError("Colisión de ruta: archivo destino ya presente.")
        return _write_temp_to_final(source, destination)
    except (OSError, IOError) as e:
        if destination.exists():
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
    """Registra los metadatos del archivo aislado en el manifiesto JSON."""
    try:
        items_list = load_manifest(base)
        quarantine_item = QuarantineItem(
            item_id=uuid.uuid4().hex[:12],
            original_path=str(source_path),
            stored_name=destination.name,
            size_bytes=original_size,
            reason=str(reason) if reason else "Sin motivo",
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
    """Valida la integridad y seguridad del archivo origen antes de procesarlo."""
    if source.is_symlink():
        raise UnsafePathError("Aislamiento de enlaces simbólicos no permitido.")
    _check_path_for_junctions(source)
    if source.is_dir():
        raise UnsafePathError("Aislamiento de directorios no permitido.")
    if not source.is_file():
        raise FileNotFoundError("Archivo origen inexistente.")
    
    if is_protected_path(source):
        raise UnsafePathError("La ruta origen está en una zona protegida.")
    
    return source

def _cleanup_orphaned_destination(destination: Path) -> None:
    """Elimina remanentes de archivos aislados fallidamente."""
    if destination.exists():
        _safe_unlink(destination)

def _verify_transaction_integrity(item: QuarantineItem, destination: Path) -> None:
    """Valida la coherencia del estado post-registro en el manifiesto."""
    if not item.verify_integrity(destination):
        raise RuntimeError("Integridad post-registro fallida.")

def _validate_input_path(source: PathLike) -> Path:
    """Valida los parámetros de entrada del usuario para operaciones públicas del módulo."""
    if not source or not str(source).strip():
        raise ValueError("Ruta de origen nula o vacía.")
    p_source = Path(str(source))
    if not p_source.is_absolute():
        try:
            p_source = p_source.resolve(strict=True)
        except (OSError, RuntimeError) as e:
            raise UnsafePathError(f"Ruta origen no válida: {e}")
    if not p_source.exists():
        raise FileNotFoundError("Archivo origen no encontrado.")
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
    """Aísla un archivo de forma segura, respetando todas las garantías de integridad."""
    p_source = _validate_input_path(source)
    
    try:
        st_info = _check_io_error_context(p_source.stat)
    except OSError as e:
        raise RuntimeError(f"Falla al verificar estado del archivo origen: {e}")
        
    source_path = _validate_source_for_quarantine(p_source)
    
    if _is_file_in_use_by_system(source_path):
        raise IOError("Archivo origen bloqueado por el sistema: operación abortada por seguridad.")
        
    dest_dir = quarantine_dir(base)
    
    if _is_filesystem_read_only(dest_dir):
        raise OSError("El directorio de cuarentena no permite operaciones de escritura.")
    
    if _is_within_quarantine_sandbox(source_path, dest_dir.resolve()):
        raise UnsafePathError("Archivo ya en el sandbox.")
    
    _validate_isolation_request(source_path, dest_dir)
    destination = dest_dir / _generate_safe_stored_name(source_path, uuid.uuid4().hex[:12])
    
    if destination.exists():
        raise FileExistsError("Colisión: el nombre de destino ya existe en la cuarentena.")

    try:
        file_hash, file_inode = _atomic_isolate_file(source_path, destination, st_info.st_size)
        
        if not destination.exists() or destination.stat().st_ino != file_inode or _get_sha256(destination) != file_hash:
            raise RuntimeError("Falla crítica: el destino no es coherente tras la copia.")
            
        if source_path.exists():
            try:
                _check_io_error_context(source_path.unlink)
            except OSError as e:
                raise RuntimeError(f"Aislamiento exitoso, pero falla al remover origen: {e}")
                
        item = _register_quarantine_item(destination, source_path, file_hash, file_inode, reason, st_info.st_size, base)
        _verify_transaction_integrity(item, destination)
        return item
    except (OSError, IOError, RuntimeError) as e:
        _cleanup_orphaned_destination(destination)
        raise RuntimeError(f"Error durante aislamiento: {e}")

def list_items(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Lista elementos en cuarentena, ordenados cronológicamente."""
    try:
        items = load_manifest(base)
        return sorted(items, key=lambda x: x.quarantined_at, reverse=True)
    except (OSError, UnsafePathError, PermissionError):
        return []


def restore_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un archivo validando integridad, permisos y seguridad de la ruta destino."""
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID de ítem vacío o inválido.")
    try:
        base_path = quarantine_dir(base)
        manifest = load_manifest(base)
            
        quarantine_item = next((i for i in manifest if i.item_id == item_id), None)
        if not quarantine_item:
            raise KeyError(f"Ítem no encontrado: {item_id}")
        
        stored_file = _validate_quarantine_path(base_path / quarantine_item.stored_name, base_path)
        if not stored_file.exists() or not stored_file.is_file():
            items = load_manifest(base, force_reload=True)
            save_manifest([i for i in items if i.item_id != item_id], base)
            raise RuntimeError("Archivo en cuarentena inexistente.")
            
        if not quarantine_item.verify_integrity(stored_file):
            raise RuntimeError("Integridad comprometida.")
        
        destination = Path(quarantine_item.original_path).resolve()
        _check_path_syntax_integrity(destination)
        if is_protected_path(destination):
            raise UnsafePathError("Restauración denegada: destino protegido.")
        if destination.exists():
            raise FileExistsError("El destino ya existe.")
        
        _check_device_consistency(stored_file, destination.parent.resolve())
        parent = destination.parent
        if not is_safe_to_modify(parent):
            raise UnsafePathError("Directorio padre no seguro.")
        _ensure_disk_space(parent, quarantine_item.size_bytes)
        
        if not parent.exists():
            _check_io_error_context(parent.mkdir, parents=True, exist_ok=True)
        
        if not is_safe_to_modify(destination):
            raise UnsafePathError("Destino no seguro.")
            
        os.replace(str(stored_file), str(destination))
        
        for p in [parent, base_path]:
            dir_fd = os.open(str(p), os.O_RDONLY)
            try: os.fsync(dir_fd)
            finally: os.close(dir_fd)
            
        items = load_manifest(base, force_reload=True)
        save_manifest([i for i in items if i.item_id != item_id], base)
        return destination
    except (OSError, PermissionError, IOError) as e:
        raise RuntimeError(f"Error crítico en restauración: {e}")


def purge_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> bool:
    """Elimina permanentemente un archivo específico de la cuarentena."""
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


def _is_item_purgable(file_path: Path, item: QuarantineItem, base_dir: Path) -> bool:
    """Valida los requisitos de seguridad antes de proceder con el borrado."""
    if not is_within_directory(file_path, base_dir):
        return False
    if _is_file_in_use_by_system(file_path):
        return False
    if not file_path.exists():
        return True
    return (
        file_path.is_file() and 
        not file_path.is_symlink() and
        item.verify_integrity(file_path) and
        _safe_unlink(file_path, expected_hash=item.sha256, expected_inode=item.file_inode)
    )


def purge_all(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Limpia todos los archivos verificados de la cuarentena eficientemente."""
    try:
        quarantine_root = quarantine_dir(base)
        if _is_filesystem_read_only(quarantine_root):
            return 0
            
        items = load_manifest(base)
        if not items:
            return 0
        
        item_map = {i.stored_name: i for i in items}
        purged_ids: Set[str] = set()
        
        for f in quarantine_root.iterdir():
            if f.name == MANIFEST_NAME or not f.is_file():
                continue
            item = item_map.get(f.name)
            if item:
                if not f.exists() or _is_item_purgable(f, item, quarantine_root):
                    purged_ids.add(item.item_id)
        
        if purged_ids:
            remaining = [i for i in items if i.item_id not in purged_ids]
            save_manifest(remaining, base)
            
        return len(purged_ids)
    except (OSError, PermissionError, UnsafePathError):
        return 0


def total_quarantined_bytes(base: PathLike = DEFAULT_QUARANTINE_DIR, items: Optional[List[QuarantineItem]] = None) -> int:
    """Calcula el espacio total consumido por los ítems en cuarentena."""
    if items is None:
        items = load_manifest(base)
    return sum(item.size_bytes for item in items)


def summarize(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera una vista legible y amigable de la cuarentena actual."""
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
