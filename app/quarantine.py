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
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Union, Dict, Any, TypeAlias, Set, Tuple, Optional, Callable, TypeVar, Iterator

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
_HASH_CACHE: Dict[Tuple[str, float, int], str] = {}
_WIN_ATTR_CACHE: Dict[str, int] = {}

WINDOWS_RESERVED_NAMES: Set[str] = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", 
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", 
    "LPT6", "LPT7", "LPT8", "LPT9"
}

def _get_read_chunk(file_obj: Any) -> Iterator[bytes]:
    """Generador para leer el archivo en bloques sin agotar la memoria."""
    while True:
        data = file_obj.read(CHUNK_SIZE)
        if not data:
            break
        yield data

def _check_for_reparse_points(path: Path) -> None:
    """
    Valida que la ruta no sea un Junction o Symlink.
    Los puntos de reparse son vectores de ataque que pueden romper el aislamiento.
    """
    if path is None: raise UnsafePathError("Ruta nula en chequeo de reparse.")
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
    """Wrapper semántico para verificar estructuras de directorios complejas."""
    _check_for_reparse_points(path)

def _get_win_attributes(path: Path) -> int:
    """Consulta atributos de archivo Win32 con caché para optimizar rendimiento."""
    if os.name != 'nt': return 0
    if not path.exists(): return -1
    
    p_str = str(path)
    if p_str in _WIN_ATTR_CACHE: return _WIN_ATTR_CACHE[p_str]
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(p_str)
        if attrs == -1: return -1
        _WIN_ATTR_CACHE[p_str] = attrs
        return attrs
    except (OSError, AttributeError, KeyError, WindowsError):
        return -1

def _win_has_system_attributes(path: Path) -> bool:
    """Verifica si el archivo posee atributos de sistema, oculto o temporal."""
    try:
        attrs = _get_win_attributes(path)
        if attrs == -1: return True
        return bool(attrs & 0x02 or attrs & 0x04 or attrs & 0x100)
    except Exception:
        return True

def _verify_source_fs_permissions(path: Path) -> None:
    """Valida la seguridad del origen prohibiendo rutas UNC y atributos críticos."""
    p_str: str = str(path)
    if p_str.startswith(r"\\") or p_str.startswith("//"):
        raise UnsafePathError("Operaciones en rutas UNC no permitidas.")
    _check_path_for_junctions(path)
    if _win_has_system_attributes(path):
        raise UnsafePathError("Archivo protegido por atributos de sistema o temporal.")

def _is_filesystem_read_only(path: Path) -> bool:
    """Confirma si el directorio permite operaciones de escritura mediante un test."""
    if path is None: return True
    if not path.exists(): return False
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
    Ejecuta operaciones I/O con estrategia de reintento exponencial.
    Necesario ante bloqueos transitorios por software de seguridad (Antivirus).
    """
    max_retries: int = 3
    for i in range(max_retries):
        try:
            return func(*args, **kwargs)
        except (OSError, IOError, PermissionError) as e:
            if i == max_retries - 1:
                raise e
            time.sleep(0.1 * (2 ** i))
    raise RuntimeError("Fallo de E/S tras múltiples intentos.")

def _is_file_exclusive(path: Path) -> bool:
    """
    Verifica si el archivo está libre de bloqueos exclusivos.
    Windows: Intenta abrir con acceso 0 (no exclusivo) sin share modes.
    POSIX: Usa fcntl (flock) para intentar adquirir bloqueo de escritura exclusivo.
    """
    if path is None or not path.exists():
        return False
    if os.name == 'nt':
        k32 = ctypes.windll.kernel32
        # OPEN_EXISTING = 3, FILE_ATTRIBUTE_NORMAL = 0x80
        handle: int = k32.CreateFileW(str(path), 0, 0, None, 3, 0x80, None)
        if handle == -1: return False
        try:
            k32.CloseHandle(handle)
        except OSError:
            pass
        return True
    
    try:
        fd: int = os.open(path, os.O_RDONLY) 
        try:
            # LOCK_EX: exclusivo, LOCK_NB: no bloqueante (retorna error si falla)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        finally:
            os.close(fd)
    except (OSError, IOError, PermissionError, AttributeError):
        return False

@dataclass
class QuarantineItem:
    """Entidad inmutable que encapsula metadatos para la auditoría de cuarentena."""
    item_id: str
    original_path: str
    stored_name: str
    size_bytes: int
    reason: str
    quarantined_at: str
    sha256: str = ""
    file_inode: Inode = 0

    def __post_init__(self) -> None:
        """Normaliza y valida los tipos de datos en la instancia."""
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
        """Retorna el tamaño del archivo en Megabytes para reportes."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la entidad a un formato compatible con JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Optional[QuarantineItem]:
        """Factory: Reconstruye un ítem desde un diccionario, validando rutas."""
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

    def _validate_integrity(self, stored_path: Path, st: Optional[os.stat_result] = None) -> bool:
        """Verificación interna de metadatos (inodo, tamaño, UID)."""
        if stored_path is None or not stored_path.exists(): return False
        try:
            _check_for_reparse_points(stored_path)
            file_stat = st if st is not None else stored_path.stat()
            if hasattr(os, 'getuid') and file_stat.st_uid != os.getuid():
                return False
            if self.file_inode != 0 and file_stat.st_ino != self.file_inode:
                return False
            if file_stat.st_nlink > 1:
                return False
            return (
                stored_path.is_file() and 
                file_stat.st_size == self.size_bytes and
                file_stat.st_size > 0
            )
        except (OSError, PermissionError):
            return False

    def verify_integrity(self, stored_path: Path) -> bool:
        """Verificación profunda: Metadatos + Coincidencia de HASH SHA256."""
        if stored_path is None or not stored_path.exists(): return False
        try:
            st = stored_path.stat()
            if not self._validate_integrity(stored_path, st):
                return False
            
            return bool(self.sha256 and _get_sha256(stored_path) == self.sha256)
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """Calcula hash SHA256 con streaming para minimizar presión de memoria."""
    if path is None or not path.exists(): return ""
    try:
        st = path.stat()
        cache_key = (str(path), st.st_mtime, st.st_size)
        if cache_key in _HASH_CACHE:
            return _HASH_CACHE[cache_key]
        
        sha256_hash = hashlib.sha256()
        with open(path, "rb", buffering=CHUNK_SIZE) as f:
            for chunk in _get_read_chunk(f):
                sha256_hash.update(chunk)
        res = sha256_hash.hexdigest()
        _HASH_CACHE[cache_key] = res
        return res
    except (OSError, IOError, MemoryError):
        return ""

def _is_file_in_use_by_system(path: Path) -> bool:
    """Detecta si el archivo está bajo control de procesos del sistema."""
    if path is None or not path.exists():
        return False
    
    try:
        st: os.stat_result = path.stat()
        if st.st_nlink > 1: return True
    except OSError:
        return True

    if _win_has_system_attributes(path):
        return True
            
    return not _is_file_exclusive(path)

def _validate_file_for_unlink(path: Path, expected_hash: Optional[str], expected_inode: Inode) -> None:
    """Validaciones de seguridad previas a la eliminación definitiva (Purge)."""
    if path is None or not path.exists():
        raise UnsafePathError("Ruta nula o inexistente para unlink.")
    
    # Prevenir borrado del manifiesto
    if path.name == MANIFEST_NAME:
        raise UnsafePathError("Protección de integridad: No se permite borrar el manifiesto.")
    
    ensure_safe_to_modify(path)
    if is_protected_path(path.parent):
        raise UnsafePathError("Protección de jerarquía: directorio padre no seguro.")
        
    _check_for_reparse_points(path)
    if is_protected_path(path) or ":" in path.name:
        raise UnsafePathError("Ruta protegida o ADS detectado.")
        
    st: os.stat_result = path.stat()
    if st.st_nlink > 1 or (expected_inode != 0 and st.st_ino != expected_inode):
        raise UnsafePathError("Inconsistencia en inodo o enlaces.")
    if hasattr(os, 'getuid') and st.st_uid != os.getuid():
        raise UnsafePathError("Error de propiedad de archivo.")
    
    if expected_hash and _get_sha256(path) != expected_hash:
        raise UnsafePathError("Falla de integridad: hash mismatch.")
    
    if _is_file_in_use_by_system(path):
        raise UnsafePathError("Archivo en uso por el sistema.")

def _safe_unlink(path: Path, expected_hash: Optional[str] = None, expected_inode: Inode = 0) -> bool:
    """Eliminación controlada con sincronización de fsync para integridad de disco."""
    if not path or not path.exists():
        return False
    try:
        _validate_file_for_unlink(path, expected_hash, expected_inode)
        parent: Path = path.parent
        _check_io_error_context(path.unlink)
        
        if parent.exists():
            try:
                dir_fd: int = os.open(str(parent), os.O_RDONLY)
                try: os.fsync(dir_fd)
                finally: os.close(dir_fd)
            except (OSError, PermissionError):
                pass
        return True
    except (OSError, PermissionError, UnsafePathError):
        return False

def _check_path_syntax_integrity(path: Path) -> None:
    """Mitigación contra inyecciones de ruta y longitudes excesivas."""
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
    """Limpia caracteres peligrosos en nombres para el entorno de cuarentena."""
    return "".join(c for c in filename if c.isalnum() or c in "._-").lstrip(".")

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """Crea nombres normalizados únicos dentro del sandbox."""
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
    """Verifica que el directorio del sandbox pertenezca al usuario actual."""
    if path is None or not path.exists(): raise UnsafePathError("Ruta nula o inexistente en ownership.")
    if hasattr(os, 'getuid'):
        try:
            if path.stat().st_uid != os.getuid():
                raise UnsafePathError("Propiedad de directorio no coincide con usuario.")
        except OSError:
            raise UnsafePathError("No se pudo verificar la propiedad del directorio.")

def quarantine_dir(base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Prepara y asegura el directorio de cuarentena (sandbox)."""
    if not base or not str(base).strip():
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path: Path = Path(str(base)).expanduser().resolve()
        
        # Validaciones de estructura de ruta
        if len(str(path)) > 240:
             raise UnsafePathError("Ruta de cuarentena excede límites de seguridad.")
        if len(path.parts) > 16:
            raise UnsafePathError("Profundidad de ruta de cuarentena excesiva.")
        if not path.name.strip() or path == path.parent:
            raise UnsafePathError("Ruta de cuarentena inválida: raíz o vacía.")
        
        _check_path_for_junctions(path)
        
        if is_protected_path(path):
            raise UnsafePathError("Directorio de cuarentena reside en ruta protegida.")
        
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
    """Retorna la ubicación absoluta del manifiesto JSON."""
    return (base_dir / MANIFEST_NAME)


def _is_within_quarantine_sandbox(path: Path, root: Path) -> bool:
    """Valida confinamiento estricto dentro de la raíz designada."""
    if path is None or root is None: return False
    return is_within_directory(path, root)

def _validate_quarantine_path(path: Path, base: Path) -> Path:
    """Post-validación de confinamiento."""
    if path is None or base is None: raise UnsafePathError("Ruta nula en sandbox.")
    if not is_within_directory(path, base):
        raise UnsafePathError("Acceso fuera del sandbox detectado.")
    return path

def _check_device_consistency(source: Path, target_directory: Path) -> None:
    """Impide transferencias entre diferentes volúmenes físicos."""
    try:
        if source.stat().st_dev != target_directory.stat().st_dev:
            raise UnsafePathError("Operación entre distintos volúmenes no permitida.")
    except OSError:
        raise UnsafePathError("No se pudo verificar la consistencia del dispositivo.")

def _verify_quarantine_preconditions(source_path: Path, target_directory: Path) -> None:
    """Validaciones de integridad previas al aislamiento."""
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
    """Verifica condiciones de seguridad para aislar el archivo."""
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
            raise UnsafePathError("Aislamiento denegado: propiedad incorrecta.")

    _check_device_consistency(resolved_source, resolved_dest_dir)
        
    _verify_quarantine_preconditions(resolved_source, resolved_dest_dir)
    ensure_safe_to_modify(resolved_source, allow_sensitive=True)
    if not _is_file_exclusive(resolved_source):
        raise IOError("Archivo origen bloqueado por otro proceso.")


def _validate_isolation_request(source_path: Path, target_directory: Path) -> None:
    """Orquestador de validaciones antes de iniciar el aislamiento."""
    if not target_directory.is_dir():
        raise UnsafePathError("Destino de cuarentena debe ser un directorio.")
    
    _check_path_syntax_integrity(source_path)
    _verify_source_fs_permissions(source_path)
    _check_for_reparse_points(source_path)
    
    if _win_has_system_attributes(source_path):
        raise UnsafePathError("Aislamiento denegado: archivo con atributos de sistema.")
    
    try:
        resolved_source: Path = source_path.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise UnsafePathError(f"Ruta origen inaccesible: {e}")
        
    if len(str(target_directory)) > 240:
        raise UnsafePathError("Ruta de cuarentena demasiado larga.")
    
    resolved_target = target_directory.resolve()
    if is_within_directory(resolved_source, resolved_target) or resolved_source == resolved_target:
        raise UnsafePathError("Archivo origen ya reside dentro del sandbox.")
    
    _ensure_disk_space(target_directory, resolved_source.stat().st_size)
    _check_isolation_safety(resolved_source, target_directory)


def load_manifest(base: PathLike = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Carga y cachea el manifiesto para evitar I/O redundante."""
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
        current_mtime, current_size = st.st_mtime, st.st_size
    except OSError:
        return []

    if not force_reload and base_dir in _MANIFEST_CACHE:
        cached_items, cached_mtime, cached_size = _MANIFEST_CACHE[base_dir]
        if cached_mtime == current_mtime and cached_size == current_size:
            return cached_items
        
    try:
        with open(m_path, "r", encoding="utf-8") as f:
            data: Any = json.load(f)
        
        if not isinstance(data, list):
            raise ValueError("Formato de manifiesto inválido: no es una lista.")
            
        items: List[QuarantineItem] = []
        for d in data:
            if isinstance(d, dict):
                try:
                    item = QuarantineItem.from_dict(d)
                    if item:
                        items.append(item)
                except (ValueError, TypeError, KeyError):
                    continue
                    
        _MANIFEST_CACHE[base_dir] = (items, current_mtime, current_size)
        return items
    except (OSError, json.JSONDecodeError, ValueError):
        _MANIFEST_CACHE[base_dir] = ([], 0.0, 0)
        return []

def save_manifest(items: List[QuarantineItem], base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persiste manifiesto de forma atómica mediante escritura a temporal y reemplazo."""
    try:
        base_path: Path = quarantine_dir(base)
    except (OSError, PermissionError) as e:
        raise OSError(f"Sandbox inaccesible para guardar manifiesto: {e}")
        
    target_path: Path = _manifest_path(base_path)
    
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista.")

    try:
        serializable_items: List[Dict[str, Any]] = [item.to_dict() for item in items]
        encoded_content: bytes = json.dumps(serializable_items, indent=2, ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError) as e:
        raise OSError(f"Error al serializar metadatos: {e}")

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
            try: os.fsync(dir_fd)
            finally: os.close(dir_fd)
        except OSError:
            pass
        
        st = target_path.stat()
        _MANIFEST_CACHE[base_path] = (items, st.st_mtime, st.st_size)
        return target_path
    except (OSError, IOError) as e:
        if temp_name and os.path.exists(temp_name):
            try: os.unlink(temp_name)
            except OSError: pass
        raise OSError(f"Falla al persistir manifiesto: {e}")


def _ensure_disk_space(dest_dir: Path, required_size: int) -> None:
    """Verifica disponibilidad de espacio con margen de seguridad del 5%."""
    if not isinstance(required_size, int) or required_size < 0:
        raise ValueError("Tamaño requerido inválido.")
    if dest_dir is None or not dest_dir.exists():
        raise FileNotFoundError(f"Directorio inexistente: {dest_dir}")
    if not dest_dir.is_dir() or not os.access(dest_dir, os.W_OK | os.R_OK):
        raise PermissionError(f"Directorio inaccesible: {dest_dir}")
    if not is_safe_to_modify(dest_dir) or _is_filesystem_read_only(dest_dir):
        raise PermissionError(f"Sin permisos de escritura seguros: {dest_dir}")
    
    usage = shutil.disk_usage(dest_dir)
    margin: int = max(int(required_size * 0.05), 5 * 1024 * 1024)
    if usage.free < (required_size + margin):
        raise OSError("Espacio insuficiente en disco.")


def _validate_file_transfer_preconditions(source: Path, destination: Path) -> None:
    """Verificaciones de seguridad antes de transferir datos."""
    if is_protected_path(destination):
        raise UnsafePathError("Destino en ruta protegida.")
    if not is_safe_to_modify(destination.parent):
        raise UnsafePathError("Directorio destino no seguro.")
    _check_device_consistency(source, destination.parent.resolve())
    _verify_source_fs_permissions(destination)
    if not source.exists() or not source.is_file():
        raise OSError("Archivo origen inaccesible.")
    
    if os.name == 'nt' and _win_has_system_attributes(source):
        raise PermissionError("Archivo origen marcado como solo lectura/sistema.")

    if destination.exists():
        if os.path.samefile(source, destination):
            raise UnsafePathError("Operación circular detectada.")
        raise FileExistsError(f"El destino ya existe: {destination}")


def _create_temp_file(source: Path, destination: Path) -> Path:
    """Crea un nombre temporal único con validación de existencia previa."""
    # Uso de uuid4 para aleatoriedad y flags de exclusividad para evitar TOCTOU
    parent = destination.parent
    for _ in range(5):
        temp_path = parent / f".tmp_{uuid.uuid4().hex}"
        try:
            fd = os.open(str(temp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return temp_path
        except (FileExistsError, OSError):
            continue
    raise RuntimeError("No se pudo generar un archivo temporal seguro.")


def _verify_copied_data(source_stat: os.stat_result, dest_path: Path, source_hash: str) -> None:
    """Valida integridad de bits post-copia mediante hash y tamaño."""
    if dest_path.stat().st_size != source_stat.st_size:
        raise OSError("Falla de integridad: tamaño mismatch.")
    final_hash: str = _get_sha256(dest_path)
    if not final_hash or final_hash != source_hash:
        raise OSError("Falla crítica: el hash del archivo copiado no coincide.")

def _perform_secure_copy(source: Path, temp_dest: Path, source_hash: str) -> None:
    """Copia protegida con streaming, restricción de acceso y sincronización a disco."""
    f_src = None
    f_dst = None
    try:
        st = source.stat()
        if not (st.st_mode & 0o100000):
            raise OSError("El origen no es un archivo regular.")
        
        _check_for_reparse_points(source)
        
        if st.st_nlink > 1:
            raise UnsafePathError("Archivo origen con enlaces múltiples.")
        
        if not is_safe_to_modify(temp_dest.parent):
            raise UnsafePathError("Directorio destino no seguro.")
        
        f_src = open(source, "rb", buffering=CHUNK_SIZE)
        f_dst = open(temp_dest, "wb", buffering=CHUNK_SIZE)
        shutil.copyfileobj(f_src, f_dst, length=CHUNK_SIZE)
        f_dst.flush()
        os.fsync(f_dst.fileno())
        
        # Restringir permisos a solo lectura/escritura del dueño (octal 0600)
        try:
            os.chmod(temp_dest, 0o600)
        except OSError:
            pass
                
        _verify_copied_data(st, temp_dest, source_hash)
    except (OSError, IOError) as e:
        if temp_dest.exists():
            try: _check_io_error_context(temp_dest.unlink)
            except OSError: pass
        raise OSError(f"Falla durante copia I/O: {e}")
    finally:
        if f_src: f_src.close()
        if f_dst: f_dst.close()


def _write_temp_to_final(source: Path, destination: Path) -> Tuple[str, Inode]:
    """Flujo atómico: Escritura temporal seguida de reemplazo final."""
    _check_path_syntax_integrity(destination)
    _validate_file_transfer_preconditions(source, destination)
    
    if not source.exists() or not source.is_file():
        raise FileNotFoundError("Archivo origen no encontrado.")
        
    source_hash: str = _get_sha256(source)
    temp_dest: Path = _create_temp_file(source, destination)
    
    try:
        _perform_secure_copy(source, temp_dest, source_hash)
        
        if destination.exists():
             raise FileExistsError("Colisión de ruta antes del reemplazo.")
             
        os.replace(temp_dest, destination)
        
        try:
            with os.fdopen(os.open(str(destination.parent), os.O_RDONLY), 'r') as dir_fd:
                os.fsync(dir_fd.fileno())
        except OSError:
            pass
        
        dest_stat = destination.stat()
        if dest_stat.st_nlink > 1:
             raise UnsafePathError("Integridad comprometida: destino tiene enlaces múltiples.")

        ensure_safe_to_modify(destination, allow_sensitive=True)
        return source_hash, dest_stat.st_ino
    except Exception as e:
        if temp_dest.exists():
            try: _check_io_error_context(temp_dest.unlink)
            except OSError: pass
        if destination.exists() and not os.path.samefile(source, destination):
            _safe_unlink(destination)
        raise RuntimeError(f"Error crítico en transferencia: {e}")


class IsolationManager:
    """Gestor de aislamiento crítico para mitigar condiciones de carrera (TOCTOU)."""
    
    @staticmethod
    def validate_atomicity(source: Path, original_stat: os.stat_result, dest: Path) -> None:
        """
        Verifica que el archivo origen mantenga su identidad y estado íntegro
        durante la ventana de tiempo de aislamiento.
        """
        if source is None or not source.exists():
            raise FileNotFoundError("Archivo origen eliminado antes del aislamiento.")
        
        current_stat = source.stat()
        if current_stat.st_ino != original_stat.st_ino:
            raise RuntimeError("Integridad comprometida: archivo reemplazado (TOCTOU).")
        if current_stat.st_nlink > 1:
            raise UnsafePathError("Aislamiento denegado: enlaces físicos múltiples.")
        
        dest_res: Path = dest.resolve()
        base_res: Path = dest.parent.resolve()
        if not is_within_directory(dest_res, base_res):
            raise UnsafePathError("Intento de escape del sandbox.")
        if not is_safe_to_modify(dest.parent):
            raise UnsafePathError("Sandbox destino inseguro.")

def _atomic_isolate_file(source: Path, destination: Path, original_size: int) -> Tuple[str, Inode]:
    """Aisla archivos aplicando validaciones de integridad estrictas contra TOCTOU."""
    _check_path_syntax_integrity(destination)
    _check_path_syntax_integrity(source)
    if not source.exists():
        raise FileNotFoundError("Archivo origen no existe.")
    
    if source.parent.resolve() == destination.parent.resolve():
        raise UnsafePathError("Origen y destino colisionan en la misma carpeta.")
    
    if _win_has_system_attributes(source) or not source.is_file():
        raise UnsafePathError("Aislamiento denegado: atributo de sistema o no regular.")
    
    with open(source, "rb") as f:
        stat_orig = os.fstat(f.fileno())
        if stat_orig.st_size != original_size or stat_orig.st_nlink > 1:
            raise RuntimeError("El archivo cambió o tiene enlaces inválidos (TOCTOU).")
        
        IsolationManager.validate_atomicity(source, stat_orig, destination)
        
        existing_items: List[QuarantineItem] = load_manifest(destination.parent.parent)
        if any(i.file_inode == stat_orig.st_ino for i in existing_items):
            raise RuntimeError("Colisión de inodo: archivo ya registrado.")

        if destination.exists():
            raise FileExistsError("Colisión de ruta: archivo destino ya presente.")
        
        destination.parent.mkdir(parents=True, exist_ok=True)
        h, i = _write_temp_to_final(source, destination)
        
        if destination.stat().st_ino != i or _get_sha256(destination) != h or destination.stat().st_nlink > 1:
            raise RuntimeError("Integridad comprometida: destino inconsistente.")
        return h, i


def _register_quarantine_item(
    destination: Path,
    source_path: Path,
    file_hash: str,
    file_inode: Inode,
    reason: str,
    original_size: int,
    base: PathLike
) -> QuarantineItem:
    """Registra un nuevo ítem en el manifiesto JSON."""
    try:
        if destination is None or not destination.exists():
            raise RuntimeError("Falla: intento de registrar archivo inexistente.")

        items_list: List[QuarantineItem] = load_manifest(base)
        
        dest_stat = destination.stat()
        if dest_stat.st_ino != file_inode or dest_stat.st_nlink > 1:
            raise RuntimeError("Inconsistencia crítica: mismatch de inodo o enlace.")

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
    except (OSError, IOError, ValueError, RuntimeError) as e:
        if destination and destination.exists():
            _safe_unlink(destination)
        raise RuntimeError(f"Falla al registrar ítem en manifiesto: {e}")


def _validate_input_path(source: PathLike) -> Path:
    """Resuelve y valida la ruta de origen desde la interfaz."""
    if source is None:
        raise ValueError("Ruta de origen nula.")
    
    path_val = str(source).strip()
    if not path_val:
        raise ValueError("Ruta de origen vacía.")
    
    p_source: Path = Path(path_val)
    if path_val.startswith(r"\\") or path_val.startswith("//"):
        raise UnsafePathError("Rutas UNC no permitidas.")
        
    if len(path_val) > 260:
        raise UnsafePathError("Ruta de entrada excede límites.")
        
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
    """Aísla archivos de forma atómica. Orquestador de alto nivel."""
    p_source: Path = _validate_input_path(source)
    
    if _is_filesystem_read_only(p_source.parent):
        raise OSError("El archivo origen reside en un volumen de solo lectura.")
        
    try:
        dest_dir: Path = quarantine_dir(base)
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Sandbox inaccesible: {e}")

    try:
        st_info: os.stat_result = _check_io_error_context(p_source.stat)
    except OSError as e:
        raise RuntimeError(f"No se pudo acceder al archivo original: {e}")
    
    if _is_file_in_use_by_system(p_source):
        raise IOError("Archivo origen bloqueado por el sistema: abortado.")
        
    if p_source.resolve().stat().st_dev != dest_dir.resolve().stat().st_dev:
        raise UnsafePathError("Origen y sandbox deben estar en la misma unidad.")
        
    _validate_isolation_request(p_source, dest_dir)
    destination: Path = dest_dir / _generate_safe_stored_name(p_source, uuid.uuid4().hex[:12])
    
    try:
        file_hash, file_inode = _atomic_isolate_file(p_source, destination, st_info.st_size)
        
        try:
            item: QuarantineItem = _register_quarantine_item(destination, p_source, file_hash, file_inode, reason, st_info.st_size, base)
        except Exception as e:
            if destination.exists(): _safe_unlink(destination)
            raise e
            
        if p_source.exists() and not os.path.samefile(p_source, destination):
            try:
                _check_io_error_context(p_source.unlink)
            except OSError as e:
                raise RuntimeError(f"Falla al remover original post-aislamiento: {e}")
                    
        return item
    except (Exception, SystemExit, KeyboardInterrupt) as e:
        if destination and destination.exists():
            _safe_unlink(destination)
        raise RuntimeError(f"Error durante proceso de cuarentena: {e}")

def list_items(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Recupera lista de ítems en cuarentena, ordenados cronológicamente."""
    base_dir = quarantine_dir(base)
    items = _MANIFEST_CACHE.get(base_dir, ([], 0.0, 0))[0]
    if not items:
        items = load_manifest(base)
    return sorted(items, key=lambda x: x.quarantined_at, reverse=True)


def restore_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un archivo aislado al destino original previa verificación de integridad."""
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID de ítem vacío.")
    
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
            raise RuntimeError(f"Archivo inexistente o corrupto: {item_id}")
            
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
        
        _check_io_error_context(os.replace, str(stored_file), str(destination))
        
        for p in [parent, base_path]:
            if p and p.exists():
                try:
                    dir_fd: int = os.open(str(p), os.O_RDONLY)
                    try: os.fsync(dir_fd)
                    finally: os.close(dir_fd)
                except (OSError, PermissionError):
                    pass
            
        items = load_manifest(base, force_reload=True)
        save_manifest([i for i in items if i.item_id != item_id], base)
        return destination
    except (OSError, PermissionError, IOError, KeyError) as e:
        raise RuntimeError(f"Error crítico en restauración: {e}")


def purge_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> bool:
    """Borrado permanente de un ítem tras verificar su integridad."""
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID de ítem vacío.")
    
    try:
        base_path: Path = quarantine_dir(base)
    except OSError:
        return False
    
    if is_protected_path(base_path):
        return False
    
    items: List[QuarantineItem] = load_manifest(base)
    quarantine_item: Optional[QuarantineItem] = next((i for i in items if i.item_id == item_id), None)
    if quarantine_item is None:
        return False
    
    stored_file: Path = base_path / quarantine_item.stored_name
    if not stored_file.exists():
        items = load_manifest(base, force_reload=True)
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
    
    if not quarantine_item.verify_integrity(stored_file):
        raise UnsafePathError(f"Integridad fallida: {item_id}.")
        
    if _safe_unlink(stored_file, expected_hash=quarantine_item.sha256, expected_inode=quarantine_item.file_inode):
        items = load_manifest(base, force_reload=True)
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
    return False


def _is_item_purgable(file_path: Optional[Path], item: Optional[QuarantineItem] = None) -> bool:
    """Valida estado de archivo para su eliminación definitiva."""
    if file_path is None or not file_path.exists() or not file_path.is_file():
        return False
    try:
        _check_for_reparse_points(file_path)
        if _is_file_in_use_by_system(file_path):
            return False
        
        if item is not None and item.verify_integrity(file_path):
            return _safe_unlink(file_path, expected_hash=item.sha256, expected_inode=item.file_inode)
        
        return False
    except (OSError, PermissionError, UnsafePathError):
        return False


def purge_all(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Elimina permanentemente todo el contenido del sandbox de forma segura."""
    try:
        root_path: Path = quarantine_dir(base).resolve()
        if _is_filesystem_read_only(root_path) or is_protected_path(root_path):
            return 0
            
        items: List[QuarantineItem] = load_manifest(base)
        # Diccionario para acceso eficiente por nombre de archivo
        item_map: Dict[str, QuarantineItem] = {i.stored_name: i for i in items}
        purgado_ids: Set[str] = set()
        
        for f in root_path.iterdir():
            if f.name == MANIFEST_NAME or f.is_dir() or not is_within_directory(f.resolve(), root_path):
                continue
            
            # Buscamos en el mapa en lugar de iterar la lista de objetos
            item = item_map.get(f.name)
            try:
                if item and _is_item_purgable(f, item):
                    purgado_ids.add(item.item_id)
                elif not item and f.suffix != ".tmp":
                    # Borrar archivos huérfanos que no están en el manifiesto
                    _safe_unlink(f)
            except (OSError, PermissionError, UnsafePathError):
                continue
        
        if purgado_ids:
            new_items: List[QuarantineItem] = [i for i in items if i.item_id not in purgado_ids]
            _check_io_error_context(save_manifest, new_items, base)
            
            try:
                with os.fdopen(os.open(str(root_path), os.O_RDONLY), 'r') as dir_fd:
                    os.fsync(dir_fd.fileno())
            except (OSError, PermissionError):
                pass
            
        return len(purgado_ids)
    except (OSError, PermissionError, UnsafePathError, RuntimeError):
        return 0


def total_quarantined_bytes(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el espacio total ocupado en el sandbox (bytes) mediante el manifiesto cacheado."""
    try:
        base_dir: Path = quarantine_dir(base)
        if base_dir in _MANIFEST_CACHE:
            return sum(item.size_bytes for item in _MANIFEST_CACHE[base_dir][0])
        return sum(item.size_bytes for item in load_manifest(base))
    except (OSError, UnsafePathError):
        return 0


def summarize(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera informe legible para la UI utilizando el manifiesto cacheado."""
    items: List[QuarantineItem] = load_manifest(base)
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
