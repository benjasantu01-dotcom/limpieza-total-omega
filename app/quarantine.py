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
CHUNK_SIZE: int = 262144  # 256KB para mejorar throughput de I/O
_MANIFEST_CACHE: Dict[Path, Tuple[List[QuarantineItem], float]] = {}
_HASH_CACHE: Dict[Tuple[str, float, int], str] = {}

WINDOWS_RESERVED_NAMES: Set[str] = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", 
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", 
    "LPT6", "LPT7", "LPT8", "LPT9"
}

def _check_for_reparse_points(path: Path) -> None:
    """Valida que la ruta no sea un punto de reparse (Junction/Symlink)."""
    if os.name == 'nt':
        try:
            attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs != -1 and (attrs & 0x400):
                raise UnsafePathError("Ruta detectada como punto de reparse.")
        except (OSError, AttributeError):
            pass
    if path.is_symlink():
        raise UnsafePathError("Ruta detectada como enlace simbólico.")

def _check_path_for_junctions(path: Path) -> None:
    """
    Verifica si la ruta es un punto de reparse (junction) mediante la API de Windows.
    Previene el seguimiento de enlaces que podrían ocultar estructuras de sistema.
    """
    _check_for_reparse_points(path)

def _win_has_system_attributes(path: Path) -> bool:
    """Consulta atributos críticos (OCULTO/SISTEMA) en Windows vía Win32 API."""
    if os.name != 'nt': return False
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        # 0x02: HIDDEN, 0x04: SYSTEM, 0x100: TEMPORARY
        return attrs != -1 and bool(attrs & 0x02 or attrs & 0x04 or attrs & 0x100)
    except (OSError, AttributeError):
        return True

def _verify_source_fs_permissions(path: Path) -> None:
    """
    Valida que la ruta origen sea un archivo estándar local y no esté en una ruta UNC.
    """
    p_str: str = str(path)
    if p_str.startswith(r"\\") or p_str.startswith("//"):
        raise UnsafePathError("Operaciones en rutas UNC no permitidas.")
    _check_path_for_junctions(path)
    if _win_has_system_attributes(path):
        raise UnsafePathError("Archivo protegido por atributos de sistema o temporal.")

def _is_filesystem_read_only(path: Path) -> bool:
    """
    Determina si un directorio es de solo lectura mediante una prueba de creación.
    Este método es más confiable que 'os.access' en sistemas de archivos virtuales.
    """
    if not path.exists(): return True
    if not os.access(path, os.W_OK):
        return True
    try:
        test_file: Path = path / f".test_{uuid.uuid4().hex}"
        test_file.touch()
        test_file.unlink()
        return False
    except (OSError, PermissionError):
        return True

def _check_io_error_context(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """
    Ejecuta una función de I/O con reintento exponencial ante bloqueos transitorios.
    Mitiga bloqueos temporales típicos durante escaneos de otros antivirus.
    """
    max_retries: int = 3
    for i in range(max_retries):
        try:
            return func(*args, **kwargs)
        except (OSError, IOError, PermissionError) as e:
            if i == max_retries - 1:
                raise e
            time.sleep(0.1 * (2 ** i))
    raise RuntimeError("Fallo inesperado en reintentos de I/O.")

def _is_file_exclusive(path: Path) -> bool:
    """
    Comprueba si el archivo está libre para escritura. 
    En Windows, intenta abrir el archivo con acceso nulo exclusivo (CreateFileW).
    """
    if not path.exists():
        return False
    if os.name == 'nt':
        k32 = ctypes.windll.kernel32
        handle: int = k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x00000080, None)
        if handle == -1: return False
        k32.CloseHandle(handle)
        return True
    
    try:
        fd: int = os.open(path, os.O_RDONLY) 
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        finally:
            os.close(fd)
    except (OSError, IOError, PermissionError, AttributeError):
        return False

@dataclass
class QuarantineItem:
    """Representación persistida de un archivo en cuarentena junto a sus metadatos de integridad."""
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
        """Retorna el tamaño del archivo en MB para informes al usuario."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Convierte la instancia en diccionario para la serialización a JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Optional[QuarantineItem]:
        """Deserializa y valida el esquema de un ítem desde el manifiesto JSON."""
        if not isinstance(data, dict):
            return None
        
        required = ("item_id", "original_path", "stored_name", "size_bytes", "reason", "quarantined_at")
        if not all(k in data and data[k] is not None for k in required):
            return None
            
        try:
            orig_p: Path = Path(str(data["original_path"]))
            if not orig_p.is_absolute() or is_protected_path(orig_p):
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
        Verifica integridad estructural: el archivo físico existe, no es un link,
        y el inodo/tamaño coinciden con los metadatos registrados.
        """
        if not stored_path.exists(): return False
        try:
            _check_for_reparse_points(stored_path)
            
            st: os.stat_result = stored_path.stat()
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
        """Verificación completa mediante metadatos y cálculo de hash SHA256 de contenido."""
        if not self._validate_integrity(stored_path):
            return False
        try:
            current_hash: str = _get_sha256(stored_path)
            if stored_path.stat().st_ino != self.file_inode:
                return False
            return bool(self.sha256 and current_hash == self.sha256)
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """Calcula hash SHA256 mediante streaming para optimizar uso de memoria."""
    try:
        st = path.stat()
        cache_key = (str(path), st.st_mtime, st.st_size)
        if cache_key in _HASH_CACHE:
            return _HASH_CACHE[cache_key]
        
        sha256_hash = hashlib.sha256()
        with open(path, "rb", buffering=CHUNK_SIZE) as f:
            for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                sha256_hash.update(chunk)
        res = sha256_hash.hexdigest()
        _HASH_CACHE[cache_key] = res
        return res
    except (OSError, IOError, MemoryError):
        return ""


def _is_file_in_use_by_system(path: Path) -> bool:
    """Verifica si el archivo está siendo bloqueado por procesos o atributos críticos."""
    if not path.exists():
        return False
    
    try:
        st: os.stat_result = path.stat()
        if st.st_nlink > 1: return True
    except OSError:
        return True

    if _win_has_system_attributes(path):
        return True
            
    return not _is_file_exclusive(path)

def _validate_file_for_unlink(path: Path, expected_hash: Optional[str], expected_inode: Inode) -> bool:
    """
    Verificación estricta antes de eliminar un archivo en cuarentena. 
    Garantiza que no ha sido alterado ni suplantado desde su aislamiento.
    """
    try:
        ensure_safe_to_modify(path)
        _check_for_reparse_points(path)
        if is_protected_path(path) or ":" in path.name:
            return False
            
        st: os.stat_result = path.stat()
        if st.st_nlink > 1 or (expected_inode != 0 and st.st_ino != expected_inode):
            return False
        if hasattr(os, 'getuid') and st.st_uid != os.getuid():
            return False
        
        if expected_hash and _get_sha256(path) != expected_hash:
            return False
        
        return not _is_file_in_use_by_system(path)
    except (OSError, PermissionError, UnsafePathError):
        return False

def _safe_unlink(path: Path, expected_hash: Optional[str] = None, expected_inode: Inode = 0) -> bool:
    """Borrado controlado: garantiza sincronización mediante fsync del directorio contenedor."""
    if not path.exists():
        return False
        
    if not _validate_file_for_unlink(path, expected_hash, expected_inode):
        return False
        
    try:
        parent: Path = path.parent
        _check_io_error_context(path.unlink)
        
        if parent.exists():
            try:
                dir_fd: int = os.open(str(parent), os.O_RDONLY)
                try: os.fsync(dir_fd)
                finally: os.close(dir_fd)
            except OSError:
                pass
        return True
    except (OSError, PermissionError):
        return False

def _check_path_syntax_integrity(path: Path) -> None:
    """
    Valida sintaxis para prevenir ataques de inyección de rutas y flujos de datos alternos (ADS).
    """
    if path is None:
        raise UnsafePathError("Ruta nula recibida.")
        
    path_str: str = os.fspath(path)
    if len(path_str) > 260:
        raise UnsafePathError("Ruta de sistema demasiado larga.")
    if any(ord(c) < 32 for c in path_str) or "\0" in path_str:
        raise UnsafePathError("Ruta con caracteres de control.")
    if "~" in path_str[1:]:
        raise UnsafePathError("Inyección de tilde detectada.")
    if len(path.parts) > 32:
        raise UnsafePathError("Profundidad de ruta excesiva.")
    if ":" in path.name:
        raise UnsafePathError("Ruta con flujos de datos alternos (ADS) prohibidos.")
    
    if path.name.upper() in WINDOWS_RESERVED_NAMES:
        raise UnsafePathError("Nombre de archivo reservado por el sistema.")

    _check_path_for_junctions(path)
    try:
        resolved: Path = path.resolve(strict=True)
        _check_for_reparse_points(resolved)
    except (OSError, RuntimeError):
        pass


def _sanitize_filename(filename: str) -> str:
    """Normaliza nombres de archivos para garantizar portabilidad en el sandbox."""
    return "".join(c for c in filename if c.isalnum() or c in "._-").lstrip(".")

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """Genera un identificador único para el archivo dentro de la cuarentena."""
    if not item_id:
        raise ValueError("ID de ítem requerido.")
    
    sanitized: str = _sanitize_filename(original_path.name)
    if not sanitized:
        sanitized = "unknown_file"
    
    parts: List[str] = sanitized.split('.')
    name_base: str = parts[0] if parts[0] else "q_file"
    
    if name_base.upper() in WINDOWS_RESERVED_NAMES:
        name_base = f"q_{name_base}"
        
    extension: str = f".{parts[-1]}" if len(parts) > 1 else ""
    candidate: str = f"{item_id}__{name_base[:64]}{extension}".replace(":", "_")[:128]
    
    if candidate.split('.')[0].upper() in WINDOWS_RESERVED_NAMES:
        candidate = f"safe_{candidate}"
        
    return candidate

def _ensure_path_ownership(path: Path) -> None:
    """Seguridad: valida que el directorio sea propiedad del usuario actual (en POSIX)."""
    if hasattr(os, 'getuid'):
        try:
            if path.stat().st_uid != os.getuid():
                raise UnsafePathError("Propiedad de directorio no coincide con usuario.")
        except OSError:
            raise UnsafePathError("No se pudo verificar la propiedad del directorio.")

def quarantine_dir(base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Prepara y valida el entorno sandbox para asegurar que sea seguro.
    """
    if not base or not str(base).strip():
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path: Path = Path(str(base)).expanduser().resolve()
        
        if len(str(path)) > 240:
             raise UnsafePathError("Ruta de cuarentena excede límites de seguridad.")
        
        if not path.name.strip() or path == path.parent:
            raise UnsafePathError("Ruta de cuarentena inválida: raíz o vacía.")
        
        _check_path_for_junctions(path)
        
        if is_protected_path(path):
            raise UnsafePathError("Directorio de cuarentena reside en ruta protegida.")
        
        _check_for_reparse_points(path)
        
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
    """Retorna la ruta al archivo manifiesto dentro del sandbox."""
    return (base_dir / MANIFEST_NAME)


def _is_within_quarantine_sandbox(path: Path, root: Path) -> bool:
    """Verifica que una ruta esté confinada dentro del sandbox."""
    return is_within_directory(path, root)

def _validate_quarantine_path(path: Path, base: Path) -> Path:
    """Asegura que ninguna operación escape del directorio sandbox."""
    if not is_within_directory(path, base):
        raise UnsafePathError("Acceso fuera del sandbox detectado.")
    return path

def _check_device_consistency(source: Path, target_directory: Path) -> None:
    """Verifica que el origen y destino residan en el mismo volumen (Atomicidad)."""
    try:
        if source.stat().st_dev != target_directory.stat().st_dev:
            raise UnsafePathError("Operación entre distintos volúmenes no permitida.")
    except OSError:
        raise UnsafePathError("No se pudo verificar la consistencia del dispositivo.")

def _verify_quarantine_preconditions(source_path: Path, target_directory: Path) -> None:
    """Validaciones de seguridad iniciales para el aislamiento."""
    resolved_source: Path = source_path.resolve(strict=True)
    resolved_dest_dir: Path = target_directory.resolve()
    
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
    """Validaciones de integridad y permisos antes de mover el archivo."""
    resolved_source: Path = source_path.resolve(strict=True)
    resolved_dest_dir: Path = target_directory.resolve()
    _check_for_reparse_points(resolved_source)
    if not resolved_source.is_file():
        raise UnsafePathError("Solo se permiten archivos regulares.")
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
    """
    Coordina todas las validaciones de seguridad previas a iniciar el proceso
    de transferencia al sandbox de cuarentena.
    """
    if not target_directory.is_dir():
        raise UnsafePathError("Destino de cuarentena debe ser un directorio.")
    
    _check_path_syntax_integrity(source_path)
    _verify_source_fs_permissions(source_path)
    _check_for_reparse_points(source_path)
    
    try:
        resolved_source: Path = source_path.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise UnsafePathError(f"Ruta origen inaccesible: {e}")
        
    if len(str(target_directory)) > 240:
        raise UnsafePathError("Ruta de cuarentena demasiado larga.")
    if is_within_directory(resolved_source, target_directory.resolve()):
        raise UnsafePathError("Archivo origen ya reside dentro del sandbox.")
    
    _ensure_disk_space(target_directory, resolved_source.stat().st_size)
    _check_isolation_safety(resolved_source, target_directory)


def load_manifest(base: PathLike = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Carga el manifiesto de cuarentena con caché por mtime."""
    try:
        base_dir: Path = quarantine_dir(base)
    except (OSError, UnsafePathError):
        return []
        
    m_path: Path = _manifest_path(base_dir)
    if not m_path.exists():
        _MANIFEST_CACHE[base_dir] = ([], 0.0)
        return []

    try:
        current_mtime: float = m_path.stat().st_mtime
    except OSError:
        return []

    if not force_reload and base_dir in _MANIFEST_CACHE:
        cached_items, cached_mtime = _MANIFEST_CACHE[base_dir]
        if cached_mtime == current_mtime:
            return cached_items
        
    try:
        with open(m_path, "r", encoding="utf-8") as f:
            data: Any = json.load(f)
        
        if not isinstance(data, list):
            raise ValueError("Formato de manifiesto inválido: no es una lista.")
            
        items: List[QuarantineItem] = []
        for d in data:
            item = QuarantineItem.from_dict(d)
            if item:
                items.append(item)
                    
        _MANIFEST_CACHE[base_dir] = (items, current_mtime)
        return items
    except (OSError, json.JSONDecodeError, ValueError):
        _MANIFEST_CACHE[base_dir] = ([], 0.0)
        return []


def save_manifest(items: List[QuarantineItem], base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persiste el manifiesto usando una técnica de escritura atómica para prevenir corrupción."""
    base_path: Path = quarantine_dir(base)
    target_path: Path = _manifest_path(base_path)
    
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista de objetos.")

    try:
        serializable_items: List[Dict[str, Any]] = [item.to_dict() for item in items]
        encoded_content: bytes = json.dumps(serializable_items, indent=2, ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError) as e:
        raise RuntimeError(f"Error al serializar metadatos de cuarentena: {e}")

    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=base_path, delete=False) as tf:
            temp_name = tf.name
            tf.write(encoded_content)
            tf.flush()
            os.fsync(tf.fileno())
            
        os.replace(temp_name, target_path)
        try:
            dir_fd: int = os.open(str(base_path), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        
        _MANIFEST_CACHE[base_path] = (items, target_path.stat().st_mtime)
        return target_path
    except (OSError, IOError) as e:
        if temp_name and os.path.exists(temp_name):
            try: os.unlink(temp_name)
            except OSError: pass
        raise RuntimeError(f"Falla al persistir manifiesto en disco: {e}")


def _ensure_disk_space(dest_dir: Path, required_size: int) -> None:
    """Valida disponibilidad de espacio antes de realizar operaciones de transferencia."""
    if not isinstance(required_size, int) or required_size < 0:
        raise ValueError("Tamaño requerido inválido.")
    if not dest_dir.exists():
        raise FileNotFoundError(f"Directorio inexistente: {dest_dir}")
    if not is_safe_to_modify(dest_dir) or _is_filesystem_read_only(dest_dir):
        raise PermissionError(f"Sin permisos de escritura seguros: {dest_dir}")
    
    usage: shutil.Usage = shutil.disk_usage(dest_dir)
    margin: int = max(int(required_size * 0.05), 5 * 1024 * 1024)
    if usage.free < (required_size + margin):
        raise OSError("Espacio insuficiente en disco.")


def _validate_file_transfer_preconditions(source: Path, destination: Path) -> None:
    """Validaciones de seguridad previas a la transferencia física del archivo."""
    if is_protected_path(destination):
        raise UnsafePathError("Destino en ruta protegida.")
    if not is_safe_to_modify(destination.parent):
        raise UnsafePathError("Directorio destino no seguro.")
    _check_device_consistency(source, destination.parent.resolve())
    _verify_source_fs_permissions(destination)
    if not source.is_file():
        raise OSError("Archivo origen inaccesible.")
    
    if os.name == 'nt' and _win_has_system_attributes(source):
        raise PermissionError("Archivo origen marcado como solo lectura/sistema.")

    if destination.exists():
        if os.path.samefile(source, destination):
            raise UnsafePathError("Operación circular detectada.")
        raise FileExistsError(f"El destino ya existe: {destination}")


def _create_temp_file(source: Path, destination: Path) -> Path:
    """Genera un archivo temporal único para la transferencia segura."""
    return destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"


def _verify_copied_data(source_stat: os.stat_result, dest_path: Path, source_hash: str) -> None:
    """Verifica que los datos copiados coincidan en tamaño y hash con el origen."""
    if dest_path.stat().st_size != source_stat.st_size:
        raise OSError("Falla de integridad: tamaño mismatch tras copia.")
    final_hash: str = _get_sha256(dest_path)
    if not final_hash or final_hash != source_hash:
        raise OSError("Falla crítica: el hash del archivo copiado no coincide.")

def _perform_secure_copy(source: Path, temp_dest: Path, source_hash: str) -> None:
    """Streaming de archivos con fsync para asegurar integridad en disco físico."""
    try:
        st = source.stat()
        if not (st.st_mode & 0o100000):
            raise OSError("El archivo origen no es un archivo regular.")
        
        _check_for_reparse_points(source)
        
        if st.st_nlink > 1:
            raise UnsafePathError("Archivo origen con enlaces físicos múltiples.")
        
        if not is_safe_to_modify(temp_dest.parent):
            raise UnsafePathError("Directorio de destino no seguro.")
        
        with open(source, "rb", buffering=CHUNK_SIZE) as f_src:
            with open(temp_dest, "wb", buffering=CHUNK_SIZE) as f_dst:
                shutil.copyfileobj(f_src, f_dst, length=CHUNK_SIZE)
                f_dst.flush()
                os.fsync(f_dst.fileno())
                
        _verify_copied_data(st, temp_dest, source_hash)
    except (OSError, IOError) as e:
        if temp_dest.exists():
            try: _check_io_error_context(temp_dest.unlink)
            except OSError: pass
        raise OSError(f"Falla durante operación I/O de copia: {e}")


def _write_temp_to_final(source: Path, destination: Path) -> Tuple[str, Inode]:
    """
    Gestiona la escritura atómica del archivo de origen al destino final mediante
    copia a un archivo temporal intermedio seguido de un reemplazo seguro.
    """
    _check_path_syntax_integrity(destination)
    _validate_file_transfer_preconditions(source, destination)
    
    if not source.is_file():
        raise FileNotFoundError("Archivo origen no encontrado.")
        
    source_hash: str = _get_sha256(source)
    temp_dest: Path = _create_temp_file(source, destination)
    
    try:
        _perform_secure_copy(source, temp_dest, source_hash)
        
        # Doble chequeo de existencia antes de reemplazar
        if destination.exists():
             raise FileExistsError("Colisión de ruta detectada antes del reemplazo.")
             
        os.replace(temp_dest, destination)
        
        try:
            dir_fd: int = os.open(str(destination.parent), os.O_RDONLY)
            try: os.fsync(dir_fd)
            finally: os.close(dir_fd)
        except OSError:
            pass
        
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
    """Gestor de validaciones para evitar condiciones de carrera (TOCTOU) durante aislamiento."""
    
    @staticmethod
    def validate_atomicity(source: Path, original_stat: os.stat_result, dest: Path) -> None:
        """
        Verifica que el archivo no haya sido suplantado durante la ejecución.
        """
        if not source.exists():
            raise FileNotFoundError("Archivo origen eliminado antes del aislamiento.")
        if source.stat().st_ino != original_stat.st_ino:
            raise RuntimeError("Integridad comprometida: archivo reemplazado (TOCTOU).")
        if source.stat().st_nlink > 1:
            raise UnsafePathError("Aislamiento denegado: múltiples enlaces físicos.")
        
        dest_res: Path = dest.resolve()
        base_res: Path = dest.parent.resolve()
        if not is_within_directory(dest_res, base_res):
            raise UnsafePathError("Intento de escape del sandbox destino.")
        if not is_safe_to_modify(dest.parent):
            raise UnsafePathError("Sandbox destino no es una ruta segura.")

def _validate_integrity_before_move(source: Path, original_size: int, original_inode: Inode) -> None:
    """Previene condiciones TOCTOU verificando el inodo y tamaño previo a la operación."""
    try:
        current_stat: os.stat_result = source.stat()
        if current_stat.st_size != original_size or current_stat.st_ino != original_inode:
            raise RuntimeError("Integridad comprometida: el archivo fue modificado (TOCTOU).")
    except OSError as e:
        raise RuntimeError(f"Falla al verificar integridad del origen antes de mover: {e}")

def _atomic_isolate_file(source: Path, destination: Path, original_size: int) -> Tuple[str, Inode]:
    """Coordina el flujo completo de aislamiento atómico con chequeos redundantes."""
    _check_path_syntax_integrity(destination)
    if not source.exists():
        raise FileNotFoundError("Archivo origen no existe.")
    
    if _win_has_system_attributes(source):
        raise UnsafePathError("Aislamiento bloqueado: archivo de sistema detectado.")
    
    try:
        if not destination.parent.exists():
            raise OSError("Directorio de cuarentena inaccesible.")
        
        stat_orig: os.stat_result = _check_io_error_context(source.stat)
        if stat_orig.st_size != original_size or stat_orig.st_nlink > 1:
            raise RuntimeError("El archivo cambió o tiene enlaces inválidos (TOCTOU).")
    except OSError as e:
        raise RuntimeError(f"No se pudo obtener estado de archivo: {e}")
    
    IsolationManager.validate_atomicity(source, stat_orig, destination)
    
    if len(str(destination)) >= 250:
        raise OSError("Ruta destino demasiado larga.")
        
    existing_items: List[QuarantineItem] = load_manifest(destination.parent.parent)
    if any(i.file_inode == stat_orig.st_ino for i in existing_items):
        raise RuntimeError("Colisión de inodo: el archivo parece estar ya registrado.")

    _validate_integrity_before_move(source, original_size, stat_orig.st_ino)

    final_check: os.stat_result = source.stat()
    if final_check.st_ino != stat_orig.st_ino or final_check.st_size != original_size or final_check.st_nlink > 1:
        raise RuntimeError("Integridad comprometida: el archivo fue modificado (TOCTOU).")

    temp_dest: Optional[Path] = None
    try:
        if destination.exists():
            raise FileExistsError("Colisión de ruta: archivo destino ya presente.")
        
        h, i = _write_temp_to_final(source, destination)
        if destination.stat().st_ino != i:
            raise RuntimeError("Integridad comprometida: el inodo destino no coincide tras la operación.")
        return h, i
    except Exception as e:
        if destination.exists() and not os.path.samefile(source, destination):
            _safe_unlink(destination)
        if temp_dest and temp_dest.exists():
            try: _check_io_error_context(temp_dest.unlink)
            except OSError: pass
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
    """Registra los metadatos finales en el manifiesto JSON."""
    try:
        if not destination.exists():
            raise RuntimeError("Falla: intento de registrar archivo inexistente en manifiesto.")

        items_list: List[QuarantineItem] = load_manifest(base)
        
        if destination.stat().st_ino != file_inode or destination.stat().st_nlink > 1:
            raise RuntimeError("Inconsistencia crítica: mismatch de inodo o enlace físico detectado.")

        quarantine_item: QuarantineItem = QuarantineItem(
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
    """Valida la seguridad de la ruta origen antes de cualquier proceso."""
    _check_for_reparse_points(source)
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

def _verify_transaction_integrity(item: QuarantineItem, destination: Path) -> None:
    """Verifica que el archivo en cuarentena mantenga la integridad post-transacción."""
    if not item.verify_integrity(destination):
        raise RuntimeError("Integridad post-registro fallida.")

def _validate_input_path(source: PathLike) -> Path:
    """Valida y resuelve la ruta de entrada para el aislamiento."""
    if source is None:
        raise ValueError("Ruta de origen nula.")
    
    p_source: Path = Path(str(source))
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
    
    _check_for_reparse_points(p_source)
    
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
    Aísla un archivo sospechoso mediante un proceso atómico y seguro.
    """
    p_source: Path = _validate_input_path(source)
    st_info: os.stat_result = _check_io_error_context(p_source.stat)
    
    if _is_file_in_use_by_system(p_source):
        raise IOError("Archivo origen bloqueado por el sistema: operación abortada.")
        
    dest_dir: Path = quarantine_dir(base)
    if _is_within_quarantine_sandbox(p_source.resolve(), dest_dir.resolve()):
        raise UnsafePathError("Archivo ya en el sandbox.")
    
    _validate_isolation_request(p_source, dest_dir)
    destination: Path = dest_dir / _generate_safe_stored_name(p_source, uuid.uuid4().hex[:12])
    
    file_hash, file_inode = _atomic_isolate_file(p_source, destination, st_info.st_size)
    
    if not destination.exists() or destination.stat().st_ino != file_inode or _get_sha256(destination) != file_hash:
        raise RuntimeError("Falla crítica: el destino no es coherente tras la copia.")
            
    item: QuarantineItem = _register_quarantine_item(destination, p_source, file_hash, file_inode, reason, st_info.st_size, base)
        
    if p_source.exists() and not os.path.samefile(p_source, destination):
        try:
            _check_io_error_context(p_source.unlink)
        except OSError as e:
            raise RuntimeError(f"Aislamiento exitoso, falla al remover original: {e}")
                
    _verify_transaction_integrity(item, destination)
    return item

def list_items(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna ítems de cuarentena ordenados por fecha de aislamiento."""
    try:
        return sorted(load_manifest(base), key=lambda x: x.quarantined_at, reverse=True)
    except (TypeError, AttributeError):
        return []


def restore_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un ítem de cuarentena a su ruta original tras verificar integridad."""
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID de ítem vacío o inválido.")
    
    try:
        base_path: Path = quarantine_dir(base)
        manifest: List[QuarantineItem] = load_manifest(base)
        quarantine_item: Optional[QuarantineItem] = next((i for i in manifest if i.item_id == item_id), None)
        
        if not quarantine_item:
            raise KeyError(f"Ítem no encontrado: {item_id}")
        
        stored_file: Path = base_path / quarantine_item.stored_name
        
        if not stored_file.exists() or not quarantine_item.verify_integrity(stored_file):
            items: List[QuarantineItem] = load_manifest(base, force_reload=True)
            save_manifest([i for i in items if i.item_id != item_id], base)
            raise RuntimeError(f"Archivo en cuarentena inexistente o corrupto: {item_id}")
            
        destination: Path = Path(quarantine_item.original_path).resolve()
        _check_path_syntax_integrity(destination)
        
        if destination.exists():
            raise FileExistsError(f"El destino ya existe: {destination}")
            
        if is_protected_path(destination):
            raise UnsafePathError("Restauración denegada: destino protegido.")
            
        parent: Path = destination.parent.resolve()
        if not is_safe_to_modify(parent):
            raise UnsafePathError("Directorio padre no seguro.")
        
        _ensure_disk_space(parent, quarantine_item.size_bytes)
        if not parent.exists():
            _check_io_error_context(parent.mkdir, parents=True, exist_ok=True)
        
        os.replace(str(stored_file), str(destination))
        
        for p in [parent, base_path]:
            if p.exists():
                try:
                    dir_fd: int = os.open(str(p), os.O_RDONLY)
                    try: os.fsync(dir_fd)
                    finally: os.close(dir_fd)
                except OSError:
                    pass
            
        items = load_manifest(base, force_reload=True)
        save_manifest([i for i in items if i.item_id != item_id], base)
        return destination
    except (OSError, PermissionError, IOError, KeyError) as e:
        raise RuntimeError(f"Error crítico en restauración: {e}")


def purge_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> bool:
    """Borrado permanente de un ítem de la cuarentena."""
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID de ítem vacío o inválido.")
    base_path: Path = quarantine_dir(base)
    
    if is_protected_path(base_path):
        return False
    
    items: List[QuarantineItem] = load_manifest(base)
    quarantine_item: Optional[QuarantineItem] = next((i for i in items if i.item_id == item_id), None)
    if quarantine_item is None:
        return False
        
    stored_file: Path = _validate_quarantine_path(base_path / quarantine_item.stored_name, base_path)
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


def _is_item_purgable(file_path: Path, item: Optional[QuarantineItem] = None) -> bool:
    """Verifica si un ítem puede ser purgado tras validar integridad."""
    try:
        if not (file_path.exists() and file_path.is_file()):
            return False
        _check_for_reparse_points(file_path)
        if _is_file_in_use_by_system(file_path):
            return False
        
        if item:
            return item.verify_integrity(file_path) and _safe_unlink(file_path, expected_hash=item.sha256, expected_inode=item.file_inode)
        
        return False
    except (OSError, PermissionError, UnsafePathError):
        return False


def purge_all(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Vacía el directorio de cuarentena, sincronizando el manifiesto final."""
    try:
        quarantine_root: Path = quarantine_dir(base).resolve()
        if _is_filesystem_read_only(quarantine_root) or is_protected_path(quarantine_root):
            return 0
            
        items: List[QuarantineItem] = load_manifest(base)
        item_map: Dict[str, QuarantineItem] = {i.stored_name: i for i in items}
        purgado_ids: Set[str] = set()
        
        for f in quarantine_root.iterdir():
            try:
                if f.name == MANIFEST_NAME or is_protected_path(f):
                    continue
                
                # Solo procesar si el archivo es un ítem registrado o huérfano legítimo
                item = item_map.get(f.name)
                if _is_item_purgable(f, item):
                    if item:
                        purgado_ids.add(item.item_id)
                    else:
                        # Limpieza de archivos sin metadatos conocidos
                        _safe_unlink(f)
            except (OSError, PermissionError, UnsafePathError):
                continue
        
        if purgado_ids:
            new_items: List[QuarantineItem] = [i for i in items if i.item_id not in purgado_ids]
            save_manifest(new_items, base)
            
            try:
                dir_fd: int = os.open(str(quarantine_root), os.O_RDONLY)
                try: os.fsync(dir_fd)
                finally: os.close(dir_fd)
            except OSError:
                pass
            
        return len(purgado_ids)
    except (OSError, PermissionError, UnsafePathError):
        return 0


def total_quarantined_bytes(base: PathLike = DEFAULT_QUARANTINE_DIR, items: Optional[List[QuarantineItem]] = None) -> int:
    """Calcula el espacio ocupado por ítems en cuarentena, permitiendo inyección de lista pre-cargada."""
    manifest_items = items if items is not None else load_manifest(base)
    return sum(item.size_bytes for item in manifest_items)


def summarize(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte legible del estado actual de la cuarentena."""
    items: List[QuarantineItem] = list_items(base)
    if not items:
        return ["La cuarentena está vacía."]
    
    total_mb: float = sum(i.size_mb for i in items)
    lines: List[str] = [f"{len(items)} archivo(s) en cuarentena — {total_mb:.2f} MB", ""]
    for item in items:
        lines.append(f"  [{item.item_id}] {Path(item.original_path).name} — {item.size_mb} MB")
        lines.append(f"      Motivo: {item.reason}")
        lines.append(f"      Origen: {item.original_path}")
        lines.append(f"      Aislado: {item.quarantined_at}")
    lines.extend(["", "Nada de esto se borró: se puede restaurar a su ubicación original."])
    return lines
