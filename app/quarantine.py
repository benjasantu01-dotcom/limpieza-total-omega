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
_HASH_CACHE: Dict[Tuple[int, int, float, int], Tuple[str, float]] = {}
_WIN_ATTR_CACHE: Dict[str, Tuple[int, float]] = {}
_INODE_USE_CACHE: Dict[Tuple[int, int, float], Tuple[bool, float]] = {}

WINDOWS_RESERVED_NAMES: Set[str] = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", 
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", 
    "LPT6", "LPT7", "LPT8", "LPT9"
}

def _sync_directory(directory: Path) -> None:
    """Asegura la persistencia física de los cambios en el directorio llamando a fsync."""
    try:
        dir_abs = _is_path_canonical(directory)
        fd = os.open(str(dir_abs), os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    except (OSError, PermissionError):
        pass

def _is_path_canonical(path: Path) -> Path:
    """Resuelve la ruta absoluta y normaliza enlaces para auditoría de seguridad."""
    try:
        if not path.is_absolute():
            path = path.resolve()
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path

def _check_for_reparse_points(path: Path) -> None:
    """Valida la ausencia de puntos de reanálisis NTFS para evitar escapes del sandbox."""
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
    """Consulta atributos Win32 (GetFileAttributesW) usando caché con TTL de 1s."""
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
    """Verifica si el archivo posee atributos críticos de sistema (Oculto, Sistema, etc)."""
    attrs: int = _get_win_attributes(path)
    if attrs == -1: return True
    return bool(attrs & 0x01 or attrs & 0x02 or attrs & 0x04 or attrs & 0x100)

def _verify_source_fs_permissions(path: Path) -> None:
    """Valida que la ruta no sea UNC y carezca de atributos restringidos de sistema."""
    p_str: str = str(path)
    if p_str.startswith(r"\\") or p_str.startswith("//"):
        raise UnsafePathError("Operaciones en rutas UNC no permitidas.")
    _check_path_for_junctions(path)
    if _win_has_system_attributes(path):
        raise UnsafePathError("Archivo protegido por atributos de sistema o temporal.")

def _is_filesystem_read_only(path: Path) -> bool:
    """Determina si la ruta reside en un sistema de archivos sin permisos de escritura."""
    if path is None: return True
    if not path.exists(): return False
    return not os.access(path, os.W_OK)

def _check_io_error_context(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Wrapper con reintento exponencial para operaciones de E/S propensas a bloqueos."""
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
    """Verifica si el archivo puede abrirse con acceso exclusivo y sin compartir."""
    if path is None or not path.exists():
        return False
    if os.name == 'nt':
        try:
            # CreateFile con dwShareMode=0 garantiza exclusividad total
            # dwDesiredAccess: 0, dwShareMode: 0, dwCreationDisposition: OPEN_EXISTING
            handle = ctypes.windll.kernel32.CreateFileW(str(path), 0, 0, None, 3, 0x80, None)
            if handle == -1 or handle == 0: return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except (OSError, AttributeError, PermissionError):
            return False
    
    try:
        # Intento de lock exclusivo no bloqueante
        fd = os.open(path, os.O_RDONLY)
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
    """Representa un registro de archivo en el manifiesto de cuarentena con validación TOCTOU."""
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
        """Asegura la integridad y tipos básicos de los datos del ítem."""
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
        """Calcula el tamaño del ítem en MB."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el ítem para almacenamiento en formato JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Optional[QuarantineItem]:
        """Reconstruye un ítem desde JSON, validando que sea una ruta absoluta segura."""
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

    def _validate_integrity(self, stored_path: Path, st: os.stat_result) -> bool:
        """Compara metadatos de sistema para prevenir ataques de reemplazo TOCTOU."""
        try:
            if hasattr(os, 'getuid') and st.st_uid != os.getuid(): return False
            if self.file_inode != 0 and st.st_ino != self.file_inode: return False
            if self.device_id != 0 and st.st_dev != self.device_id: return False
            if st.st_nlink > 1: return False
            if self.mtime_snapshot != 0.0 and abs(st.st_mtime - self.mtime_snapshot) > 1.0: return False
            return (stored_path.is_file() and st.st_size == self.size_bytes and st.st_size > 0)
        except (OSError, PermissionError): return False

    def verify_integrity(self, stored_path: Path) -> bool:
        """Verifica la integridad física combinando metadatos y hash SHA256."""
        if stored_path is None or not stored_path.exists(): return False
        try:
            st = stored_path.stat()
            _check_for_reparse_points(stored_path)
            if not self._validate_integrity(stored_path, st): return False
            return bool(self.sha256 and _get_sha256(stored_path, st) == self.sha256)
        except (OSError, PermissionError): return False


def _get_sha256(path: Path, st: Optional[os.stat_result] = None) -> str:
    """Calcula hash SHA256 optimizado con caché por metadatos (dev, ino, mtime)."""
    if path is None: return ""
    try:
        stat_res = st if st else path.stat()
        cache_key = (stat_res.st_dev, stat_res.st_ino, stat_res.st_mtime, stat_res.st_size)
        now = time.monotonic()
        
        if cache_key in _HASH_CACHE:
            res, ts = _HASH_CACHE[cache_key]
            if now - ts < 60.0: return res
        
        with open(path, "rb") as f:
            if hasattr(hashlib, "file_digest"):
                res = hashlib.file_digest(f, "sha256").hexdigest()
            else:
                sha256_hash = hashlib.sha256()
                for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                    sha256_hash.update(chunk)
                res = sha256_hash.hexdigest()
        
        _HASH_CACHE[cache_key] = (res, now)
        return res
    except (OSError, IOError, MemoryError): return ""

def _is_file_in_use_by_system(path: Path) -> bool:
    """Valida si el archivo está siendo usado o bloqueado por el sistema (caching 0.5s)."""
    if path is None or not path.exists(): return False
    try:
        st = path.stat()
        cache_key = (st.st_dev, st.st_ino, st.st_mtime)
        now = time.monotonic()
        
        if cache_key in _INODE_USE_CACHE:
            res, ts = _INODE_USE_CACHE[cache_key]
            if now - ts < 0.5: return res
        
        in_use = (st.st_nlink > 1 or _win_has_system_attributes(path) or not _is_file_exclusive(path))
            
        _INODE_USE_CACHE[cache_key] = (in_use, now)
        return in_use
    except (OSError, PermissionError):
        return True

def _validate_file_for_unlink(path: Path, expected_hash: Optional[str], expected_inode: Inode, expected_device: int = 0) -> None:
    """Auditoría de seguridad previa a una operación de borrado físico (unlink)."""
    if path is None or not path.exists(): raise UnsafePathError("Ruta nula o inexistente para unlink.")
    if path.name == MANIFEST_NAME: raise UnsafePathError("Protección de integridad: No se permite borrar el manifiesto.")
    
    ensure_safe_to_modify(path)
    
    if is_protected_path(path.parent): raise UnsafePathError("Protección de jerarquía: directorio padre no seguro.")
    _check_for_reparse_points(path)
    if is_protected_path(path) or ":" in path.name: raise UnsafePathError("Ruta protegida o ADS detectado.")
    
    st: os.stat_result = path.stat()
    if st.st_nlink > 1 or (expected_inode != 0 and st.st_ino != expected_inode): raise UnsafePathError("Inconsistencia en inodo o enlaces.")
    if expected_device != 0 and st.st_dev != expected_device: raise UnsafePathError("Falla de consistencia de dispositivo.")
    if hasattr(os, 'getuid') and st.st_uid != os.getuid(): raise UnsafePathError("Error de propiedad de archivo.")
    if expected_hash and _get_sha256(path, st) != expected_hash: raise UnsafePathError("Falla de integridad: hash mismatch.")
    if _is_file_in_use_by_system(path): raise UnsafePathError("Archivo en uso por el sistema.")

def _safe_unlink(path: Path, expected_hash: Optional[str] = None, expected_inode: Inode = 0, expected_device: int = 0) -> bool:
    """Borra el archivo confirmando su integridad y forzando la sincronización de metadatos."""
    if not path or not path.exists(): return True
    try:
        _validate_file_for_unlink(path, expected_hash, expected_inode, expected_device)
        parent: Path = path.parent
        _check_io_error_context(path.unlink)
        if parent.exists():
            _sync_directory(parent)
        return True
    except (UnsafePathError, OSError, PermissionError, RuntimeError):
        return False

def _check_path_syntax_integrity(path: Path) -> None:
    """Valida la sintaxis de la ruta para prevenir inyecciones o desbordamientos."""
    if path is None: raise UnsafePathError("Ruta nula recibida.")
    path = _is_path_canonical(path)
    path_str: str = os.fspath(path)
    if len(path_str) > 260: raise UnsafePathError("Ruta de sistema demasiado larga.")
    if any(ord(c) < 32 for c in path_str) or "\0" in path_str: raise UnsafePathError("Ruta con caracteres de control.")
    if "~" in path_str[1:]: raise UnsafePathError("Inyección de tilde detectada.")
    if len(path.parts) > 32: raise UnsafePathError("Profundidad de ruta excesiva.")
    if ":" in path.name: raise UnsafePathError("Ruta con flujos de datos alternos (ADS) prohibidos.")
    if path.name.upper() in WINDOWS_RESERVED_NAMES: raise UnsafePathError("Nombre de archivo reservado por el sistema.")
    _check_path_for_junctions(path)
    if path.exists():
        try:
            resolved: Path = path.resolve(strict=True)
            _check_for_reparse_points(resolved)
        except (OSError, RuntimeError): pass

def _sanitize_filename(filename: str) -> str:
    """Limpia nombres de archivos para garantizar compatibilidad multiplataforma."""
    return "".join(c for c in filename if c.isalnum() or c in "._-").lstrip(".")

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """Genera un nombre seguro en el sandbox para evitar colisiones."""
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
    """Valida la propiedad del directorio raíz del sandbox."""
    if path is None or not path.exists(): raise UnsafePathError("Ruta nula o inexistente en ownership.")
    if hasattr(os, 'getuid'):
        try:
            if path.stat().st_uid != os.getuid(): raise UnsafePathError("Propiedad de directorio no coincide con usuario.")
        except OSError: raise UnsafePathError("No se pudo verificar la propiedad del directorio.")

def quarantine_dir(base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Prepara el directorio raíz de cuarentena validando permisos y seguridad."""
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
        
        path_resolved = path.resolve()
        if path_resolved.exists():
            if not path_resolved.is_dir(): raise UnsafePathError("Ruta de cuarentena apunta a un archivo existente.")
            if not is_safe_to_modify(path_resolved): raise UnsafePathError("Ruta de cuarentena marcada como insegura.")
            if not os.access(path_resolved, os.W_OK | os.R_OK): raise PermissionError("Directorio inaccesible para operaciones de I/O.")
            if _is_filesystem_read_only(path_resolved): raise PermissionError("Permisos insuficientes en directorio.")
        else:
            _check_io_error_context(path_resolved.mkdir, parents=True, exist_ok=True)
            if not path_resolved.exists() or not path_resolved.is_dir(): raise OSError("Fallo al asegurar directorio de cuarentena.")
        
        # Validar capacidad de escritura real creando un temporal efímero
        try:
            tmp_check = tempfile.NamedTemporaryFile(dir=path_resolved, delete=True)
            tmp_check.close()
        except (PermissionError, OSError) as e:
            raise PermissionError(f"Directorio de cuarentena no permite creación de temporales: {e}")
        
        _ensure_path_ownership(path_resolved)
        return path_resolved
    except (OSError, RuntimeError, UnsafePathError, PermissionError) as e:
        raise OSError(f"Error al preparar directorio de cuarentena: {e}")

def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ruta completa al archivo de manifiesto JSON."""
    return (base_dir / MANIFEST_NAME)

def _is_within_quarantine_sandbox(path: Path, root: Path) -> bool:
    """Verifica si una ruta está confinada estrictamente al sandbox."""
    if path is None or root is None: return False
    return is_within_directory(_is_path_canonical(path), _is_path_canonical(root))

def _validate_quarantine_path(path: Path, base: Path) -> Path:
    """Asegura confinamiento de ruta contra el sandbox base."""
    if path is None or base is None: raise UnsafePathError("Ruta nula en sandbox.")
    if not is_within_directory(_is_path_canonical(path), _is_path_canonical(base)): raise UnsafePathError("Acceso fuera del sandbox detectado.")
    return path

def _check_device_consistency(source: Path, target_directory: Path) -> None:
    """Impide operaciones cross-volume requeridas para garantizar atomicidad."""
    if not isinstance(source, Path) or not isinstance(target_directory, Path):
        raise TypeError("Las rutas deben ser instancias de pathlib.Path.")
    try:
        if _is_path_canonical(source).stat().st_dev != _is_path_canonical(target_directory).stat().st_dev:
            raise UnsafePathError("Operación entre distintos volúmenes no permitida.")
    except OSError as e:
        raise UnsafePathError(f"No se pudo verificar la consistencia del dispositivo: {e}")

def _verify_quarantine_preconditions(source_path: Path, target_directory: Path) -> None:
    """Valida precondiciones lógicas para evitar bucles de carpetas."""
    resolved_source: Path = _is_path_canonical(source_path)
    resolved_dest_dir: Path = _is_path_canonical(target_directory)
    if is_protected_path(resolved_source): raise UnsafePathError("Ruta origen protegida.")
    if is_protected_path(resolved_dest_dir) or is_protected_path(resolved_dest_dir.parent): raise UnsafePathError("Destino en ruta protegida.")
    if _is_within_quarantine_sandbox(resolved_source, resolved_dest_dir): raise UnsafePathError("Archivo ya se encuentra en sandbox.")
    if is_within_directory(resolved_dest_dir, resolved_source): raise UnsafePathError("Operación recursiva prohibida: destino dentro de origen.")
    if resolved_source.parent == resolved_dest_dir: raise UnsafePathError("Operación circular detectada.")

def _check_isolation_safety(source_path: Path, target_directory: Path) -> None:
    """Valida estado físico del archivo previo a iniciar el aislamiento."""
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
    """Coordina las validaciones previas al aislamiento (Sandbox)."""
    if not target_directory.is_dir(): 
        raise UnsafePathError("Destino de cuarentena debe ser un directorio.")
    
    source_path = _is_path_canonical(source_path)
    _check_path_syntax_integrity(source_path)
    _verify_source_fs_permissions(source_path)
    _check_for_reparse_points(source_path)
    
    if _win_has_system_attributes(source_path): 
        raise UnsafePathError("Aislamiento denegado: archivo con atributos de sistema o solo lectura.")
    
    if len(str(target_directory)) > 240: 
        raise UnsafePathError("Ruta de cuarentena demasiado larga.")
        
    resolved_target = _is_path_canonical(target_directory)
    if is_within_directory(source_path, resolved_target) or source_path == resolved_target:
        raise UnsafePathError("Archivo origen ya reside dentro del sandbox.")
        
    _ensure_disk_space(target_directory, source_path.stat().st_size)
    _check_isolation_safety(source_path, target_directory)

def load_manifest(base: PathLike = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Carga y filtra ítems del manifiesto, removiendo registros de archivos inexistentes."""
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
        if not force_reload and base_dir in _MANIFEST_CACHE:
            cached_items, cached_mtime, cached_size = _MANIFEST_CACHE[base_dir]
            if cached_mtime == st.st_mtime and cached_size == st.st_size: 
                return cached_items
        
        # Optimización: Set para búsqueda O(1) de archivos en disco
        existing_names = {entry.name for entry in os.scandir(base_dir) if entry.is_file()}
        
        with open(m_path, "r", encoding="utf-8") as f: 
            data = json.load(f)
            
        if not isinstance(data, list): 
            raise ValueError("El manifiesto no es una lista válida.")
            
        valid_items: List[QuarantineItem] = []
        for entry in data:
            if not isinstance(entry, dict): continue
            item = QuarantineItem.from_dict(entry)
            # Solo validamos si el archivo almacenado físicamente existe
            if item and item.stored_name in existing_names:
                valid_items.append(item)
        
        if len(valid_items) < len(data):
            save_manifest(valid_items, base)
            
        st = m_path.stat()
        _MANIFEST_CACHE[base_dir] = (valid_items, st.st_mtime, st.st_size)
        return valid_items
        
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        _MANIFEST_CACHE[base_dir] = ([], 0.0, 0)
        return []

def save_manifest(items: List[QuarantineItem], base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persiste el manifiesto usando archivos temporales y sincronización de disco (fsync)."""
    base_path: Path = quarantine_dir(base)
    target_path: Path = _manifest_path(base_path)
    if not isinstance(items, list): raise TypeError("El manifiesto debe ser una lista.")
    try:
        encoded_content: bytes = json.dumps([item.to_dict() for item in items], indent=2, ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError) as e: raise ValueError(f"Error al serializar metadatos: {e}")
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=base_path, delete=False) as tf:
            temp_name = tf.name
            tf.write(encoded_content)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(temp_name, target_path)
        _sync_directory(base_path)
        st = target_path.stat()
        _MANIFEST_CACHE[base_path] = (items, st.st_mtime, st.st_size)
        return target_path
    except (OSError, IOError) as e:
        if temp_name and os.path.exists(temp_name):
            try: os.unlink(temp_name)
            except OSError: pass
        raise OSError(f"Falla al persistir manifiesto: {e}")

def _ensure_disk_space(dest_dir: Path, required_size: int) -> None:
    """Verifica disponibilidad de espacio con margen crítico del 5%."""
    if not isinstance(required_size, int) or required_size < 0: raise ValueError("Tamaño requerido inválido.")
    dest_dir = _is_path_canonical(dest_dir)
    if not dest_dir.exists(): raise FileNotFoundError(f"Directorio inexistente: {dest_dir}")
    if not dest_dir.is_dir() or not os.access(dest_dir, os.W_OK | os.R_OK): raise PermissionError(f"Directorio inaccesible: {dest_dir}")
    if not is_safe_to_modify(dest_dir) or _is_filesystem_read_only(dest_dir): raise PermissionError(f"Sin permisos de escritura seguros: {dest_dir}")
    usage = shutil.disk_usage(dest_dir)
    margin: int = max(int(required_size * 0.05), 5 * 1024 * 1024)
    if usage.free < (required_size + margin): raise OSError("Espacio insuficiente en disco.")

def _validate_file_transfer_preconditions(source: Path, destination: Path) -> None:
    """Valida precondiciones para transferencias origen -> sandbox."""
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
    """Genera archivo temporal único con persistencia de metadatos (fsync)."""
    parent = destination.parent
    ensure_safe_to_modify(parent)
    for _ in range(5):
        temp_path = parent / f".tmp_{uuid.uuid4().hex}"
        try:
            fd = os.open(str(temp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
            return temp_path
        except (FileExistsError, OSError): continue
    raise RuntimeError("No se pudo generar un archivo temporal seguro.")

def _verify_copied_data(source_stat: os.stat_result, dest_path: Path, source_hash: str) -> None:
    """Verifica integridad binaria (tamaño y hash) tras la transferencia."""
    if dest_path.stat().st_size != source_stat.st_size: raise OSError("Falla de integridad: tamaño mismatch.")
    final_hash: str = _get_sha256(dest_path)
    if not final_hash or final_hash != source_hash: raise OSError("Falla crítica: el hash del archivo copiado no coincide.")

def _perform_secure_copy(source: Path, temp_dest: Path, source_hash: str) -> None:
    """Copia los bytes del archivo verificando inodos y hash final."""
    try:
        st = source.stat()
        if not (st.st_mode & 0o100000): raise OSError("El origen no es un archivo regular.")
        _check_for_reparse_points(source)
        if st.st_nlink > 1: raise UnsafePathError("Archivo origen con enlaces múltiples.")
        
        # Validar espacio crítico antes de iniciar la copia larga
        _ensure_disk_space(temp_dest.parent, st.st_size)
        
        ensure_safe_to_modify(temp_dest.parent)
        
        def _copy():
            with open(source, "rb") as f_src:
                with open(temp_dest, "wb") as f_dst:
                    shutil.copyfileobj(f_src, f_dst)
                    f_dst.flush()
                    os.fsync(f_dst.fileno())
        
        _check_io_error_context(_copy)
        
        try: os.chmod(temp_dest, 0o600)
        except OSError: pass
        _verify_copied_data(st, temp_dest, source_hash)
    except (OSError, IOError) as e:
        if temp_dest.exists():
            try: _check_io_error_context(temp_dest.unlink)
            except OSError: pass
        raise OSError(f"Falla durante copia I/O: {e}")

def _write_temp_to_final(source_path: Path, dest_path: Path) -> Tuple[str, Inode, int, float]:
    """Finaliza el movimiento atómico tras validar integridad en destino."""
    _check_path_syntax_integrity(dest_path)
    _validate_file_transfer_preconditions(source_path, dest_path)
    
    source_hash: str = _get_sha256(source_path)
    temp_dest: Path = _create_temp_file(source_path, dest_path)
    
    _check_for_reparse_points(temp_dest)
    
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
    """Gestor de validación para atomicidad en el proceso de aislamiento."""
    @staticmethod
    def validate_atomicity(source: Path, original_stat: os.stat_result, dest: Path) -> None:
        """Verificación TOCTOU para asegurar que el archivo origen no haya cambiado."""
        if source is None or not source.exists(): raise FileNotFoundError("Archivo origen eliminado antes del aislamiento.")
        current_stat = source.stat()
        if current_stat.st_ino != original_stat.st_ino or current_stat.st_dev != original_stat.st_dev: 
            raise RuntimeError("Integridad comprometida: archivo reemplazado o movido (TOCTOU).")
        if current_stat.st_nlink > 1: raise UnsafePathError("Aislamiento denegado: enlaces físicos múltiples.")
        dest_res: Path = _is_path_canonical(dest)
        base_res: Path = _is_path_canonical(dest.parent)
        if not is_within_directory(dest_res, base_res): raise UnsafePathError("Intento de escape del sandbox.")
        ensure_safe_to_modify(dest.parent)

def _atomic_isolate_file(source: Path, destination: Path, original_size: int) -> Tuple[str, Inode, int, float]:
    """Aísla archivos validando condiciones de carrera y exclusividad."""
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
        if destination.stat().st_ino != i or destination.stat().st_dev != d or _get_sha256(destination) != h or destination.stat().st_nlink > 1:
            raise RuntimeError("Integridad comprometida: destino inconsistente.")
        return h, i, d, mtime

def _register_quarantine_item(destination: Path, source_path: Path, file_hash: str, file_inode: Inode, device_id: int, mtime: float, reason: str, original_size: int, base: PathLike) -> QuarantineItem:
    """Registra un nuevo ítem en el manifiesto tras validar su persistencia física."""
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
    """Valida la ruta de entrada para prevenir ataques comunes."""
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
    """
    Aísla un archivo en el sandbox de cuarentena.

    Args:
        source: Ruta del archivo sospechoso.
        reason: Motivo del aislamiento.
        base: Directorio del sandbox de cuarentena.

    Returns:
        Objeto QuarantineItem registrado.

    Raises:
        RuntimeError: En caso de fallo crítico en E/S o seguridad.
    """
    p_source: Path = _validate_input_path(source)
    
    try:
        if not _is_file_exclusive(p_source):
            raise IOError("El archivo origen está bloqueado por otro proceso.")
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Error de acceso durante pre-check de exclusividad: {e}")

    try:
        initial_stat = p_source.stat()
    except OSError as e:
        raise RuntimeError(f"Error al acceder a metadatos de origen: {e}")
    
    if _is_filesystem_read_only(p_source.parent): raise OSError("El archivo origen reside en un volumen de solo lectura.")
    try: dest_dir: Path = quarantine_dir(base)
    except (OSError, PermissionError) as e: raise RuntimeError(f"Sandbox inaccesible: {e}")
    
    ensure_safe_to_modify(dest_dir)
    
    if _win_has_system_attributes(p_source): raise PermissionError("Archivo origen restringido por atributos de sistema.")
    if _is_path_canonical(p_source).stat().st_dev != dest_dir.stat().st_dev: raise UnsafePathError("Origen y sandbox deben estar en la misma unidad.")
    
    _validate_isolation_request(p_source, dest_dir)
    destination: Path = dest_dir / _generate_safe_stored_name(p_source, uuid.uuid4().hex[:12])
    
    try:
        file_hash, file_inode, dev_id, mtime = _atomic_isolate_file(p_source, destination, initial_stat.st_size)
        item: QuarantineItem = _register_quarantine_item(destination, p_source, file_hash, file_inode, dev_id, mtime, reason, initial_stat.st_size, base)
        if p_source.exists() and not os.path.samefile(p_source, destination):
            try: _check_io_error_context(p_source.unlink)
            except OSError as e: raise RuntimeError(f"Falla al remover original post-aislamiento: {e}")
        return item
    except (OSError, IOError, ValueError, RuntimeError, UnsafePathError) as e:
        if destination and destination.exists(): _safe_unlink(destination)
        raise RuntimeError(f"Error durante proceso de cuarentena: {e}")

def list_items(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna los ítems en cuarentena ordenados cronológicamente."""
    try: base_dir = quarantine_dir(base)
    except (OSError, UnsafePathError): return []
    items = load_manifest(base)
    return sorted(items, key=lambda x: x.quarantined_at, reverse=True)

def restore_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un archivo verificando su integridad contra el manifiesto."""
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
        _check_io_error_context(os.replace, str(stored_file), str(destination))
        for p in [parent, base_path]:
            if p and p.exists():
                _sync_directory(p)
        items = load_manifest(base, force_reload=True)
        save_manifest([i for i in items if i.item_id != item_id], base)
        return destination
    except (OSError, PermissionError, IOError, KeyError) as e: raise RuntimeError(f"Error crítico en restauración: {e}")

def purge_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> bool:
    """Elimina permanentemente un ítem del sandbox y limpia el manifiesto."""
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
    """Determina si un ítem puede ser eliminado físicamente de forma segura."""
    if file_path is None or not file_path.exists() or not file_path.is_file(): return False
    try:
        _check_for_reparse_points(file_path)
        if _is_file_in_use_by_system(file_path): return False
        if item is not None and item.verify_integrity(file_path):
            return _safe_unlink(file_path, expected_hash=item.sha256, expected_inode=item.file_inode, expected_device=item.device_id)
        return False
    except (OSError, PermissionError, UnsafePathError): return False

def purge_all(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Vacía el directorio de cuarentena purgando todos los ítems registrados."""
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
                item = item_map.get(entry.name)
                if not item:
                    try:
                        _safe_unlink(Path(entry.path))
                    except (OSError, PermissionError, UnsafePathError): continue
                    continue
                
                try:
                    f = Path(entry.path)
                    if _is_item_purgable(f, item):
                        purgado_ids.add(item.item_id)
                except (OSError, PermissionError, UnsafePathError): continue
            
        if purgado_ids:
            new_items = [i for i in items if i.item_id not in purgado_ids]
            save_manifest(new_items, base)
            _sync_directory(root_path)
        return len(purgado_ids)
    except (OSError, PermissionError, UnsafePathError, RuntimeError): return 0

def total_quarantined_bytes(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Retorna el peso total ocupado por los archivos en cuarentena sin recrear instancias de objetos."""
    try:
        base_dir = quarantine_dir(base)
        m_path = _manifest_path(base_dir)
        if not m_path.exists(): return 0
        with open(m_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return sum(item.get("size_bytes", 0) for item in data if isinstance(item, dict))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0

def summarize(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera reporte legible de la cuarentena para la interfaz de usuario."""
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
