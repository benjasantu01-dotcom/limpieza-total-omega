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
_MANIFEST_CACHE: Dict[str, List[QuarantineItem]] = {}

WINDOWS_RESERVED_NAMES: Set[str] = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", 
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", 
    "LPT6", "LPT7", "LPT8", "LPT9"
}

def _check_io_error_context(func: Callable, *args, **kwargs) -> Any:
    """Implementa reintento con espera (backoff exponencial) para I/O bloqueado."""
    max_retries = 3
    for i in range(max_retries):
        try:
            return func(*args, **kwargs)
        except (OSError, IOError, PermissionError) as e:
            if i == max_retries - 1:
                raise e
            time.sleep(0.1 * (2 ** i))

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
    file_inode: int = 0  # Identificador de inodo para validación TOCTOU

    def __post_init__(self) -> None:
        try:
            self.size_bytes = int(self.size_bytes)
        except (ValueError, TypeError):
            self.size_bytes = 0
        if not isinstance(self.item_id, str) or not self.item_id:
            raise ValueError("ID de ítem vacío o inválido")
        if not isinstance(self.reason, str) or not self.reason:
            self.reason = "Sin motivo especificado"

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
        required: Tuple[str, ...] = ("item_id", "original_path", "stored_name", "size_bytes", "reason", "quarantined_at")
        if not all(key in data and data[key] is not None for key in required):
            return None
        try:
            orig_p = str(data["original_path"])
            if not Path(orig_p).is_absolute():
                return None
            return cls(
                item_id=str(data["item_id"]),
                original_path=orig_p,
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
        """Verifica existencia, inodo, tamaño y tipo del archivo en el sandbox."""
        if not stored_path.exists(): return False
        try:
            if stored_path.is_symlink() or (hasattr(stored_path, 'is_junction') and stored_path.is_junction()):
                return False
            
            st = stored_path.stat()
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
        """Realiza comprobación de hash SHA-256 contra el registro original."""
        if not self._validate_integrity(stored_path):
            return False
        try:
            current_hash = _get_sha256(stored_path)
            return bool(self.sha256 and current_hash == self.sha256)
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """Calcula hash SHA-256 usando búferes para evitar saturación de memoria."""
    if not path.exists() or not path.is_file():
        return ""
    sha256_hash = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                sha256_hash.update(chunk)
    except (OSError, PermissionError, IOError):
        return ""
    return sha256_hash.hexdigest()


def _is_file_in_use_by_system(path: Path) -> bool:
    """Verifica si el archivo está bloqueado por el sistema o tiene enlaces múltiples."""
    if not path.exists():
        return False
    
    try:
        if path.stat().st_nlink > 1:
            return True
    except OSError:
        return True

    if os.name != 'nt':
        return False
        
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs != -1 and (attrs & 0x02 or attrs & 0x04): return True
    except (OSError, AttributeError, ValueError):
        return True
            
    try:
        import msvcrt
        fd = os.open(path, os.O_RDONLY | os.O_BINARY)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
        return False
    except (OSError, IOError, ImportError, AttributeError):
        return True

def _is_file_locked(path: Path) -> bool:
    """Wrapper para chequeo de bloqueos de archivos en el sistema operativo."""
    return _is_file_in_use_by_system(path)

def _safe_unlink(path: Path, expected_hash: Optional[str] = None, expected_inode: int = 0) -> bool:
    """Eliminación controlada tras validación de metadatos y hash."""
    if not path.is_absolute() or not path.exists() or not path.is_file():
        return False
    if is_protected_path(path) or path.is_symlink():
        return False
    
    try:
        st = path.stat()
        if expected_inode != 0 and st.st_ino != expected_inode:
            return False
            
        resolved = path.resolve()
        if not is_safe_to_modify(resolved) or is_protected_path(resolved):
            return False
        
        if expected_hash and _get_sha256(resolved) != expected_hash:
            return False
        if _is_file_locked(resolved):
            return False
            
        _check_io_error_context(resolved.unlink)
        return True
    except (OSError, PermissionError, UnsafePathError):
        return False

def _check_path_syntax_integrity(path: Path) -> None:
    """Valida caracteres prohibidos, profundidad y flujos ADS (Alternate Data Streams)."""
    path_str = str(path)
    if any(ord(c) < 32 for c in path_str) or "\0" in path_str:
        raise UnsafePathError("Ruta con caracteres de control.")
    if len(path.parts) > 32:
        raise UnsafePathError("Profundidad de ruta excesiva.")
    if ":" in path.name:
        raise UnsafePathError("Ruta con flujos de datos alternos (ADS) prohibidos.")
    
    if path.name.upper() in WINDOWS_RESERVED_NAMES:
        raise UnsafePathError("Nombre de archivo reservado por el sistema.")

    try:
        resolved = path.resolve(strict=True)
        if resolved.is_symlink():
            raise UnsafePathError("Operación denegada: enlace simbólico.")
        if hasattr(resolved, 'is_junction') and resolved.is_junction():
            raise UnsafePathError("Operación denegada: punto de reparse.")
    except (OSError, RuntimeError):
        pass


def _sanitize_filename(filename: str) -> str:
    """Elimina caracteres inválidos para el sistema de archivos."""
    return "".join(c for c in filename if c.isalnum() or c in "._-")

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """Crea un nombre de archivo único, seguro y libre de colisiones."""
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
    """Verifica que el directorio pertenezca al usuario en sistemas POSIX."""
    if hasattr(os, 'getuid'):
        if path.stat().st_uid != os.getuid():
            raise UnsafePathError("Propiedad de directorio no coincide con usuario.")

def quarantine_dir(base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Normaliza, valida y asegura la existencia del directorio de cuarentena."""
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path = Path(base).expanduser().resolve()
        if not path.name.strip() or path == path.parent:
            raise UnsafePathError("Ruta de cuarentena inválida o es raíz.")
        if is_protected_path(path):
            raise UnsafePathError("Directorio de cuarentena reside en ruta protegida.")
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
             raise UnsafePathError("Ruta de cuarentena no puede ser un punto de reparse.")
        ensure_safe_to_modify(path)
        if not path.exists():
            _check_io_error_context(path.mkdir, parents=True, exist_ok=True)
        if not os.access(path, os.W_OK | os.R_OK):
            raise PermissionError("Permisos insuficientes en directorio.")
        _ensure_path_ownership(path)
        return path
    except (OSError, RuntimeError, UnsafePathError, PermissionError) as e:
        raise OSError(f"Error al preparar directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ubicación absoluta esperada para el manifiesto."""
    return (base_dir / MANIFEST_NAME).resolve()


def _is_within_quarantine_sandbox(path: Path, root: Path) -> bool:
    """Verifica si una ruta está confinada al sandbox."""
    return is_within_directory(path, root)

def _validate_quarantine_path(path: Path, base: Path) -> Path:
    """Asegura que el acceso al archivo no rompa el confinamiento del sandbox."""
    resolved_path = path.resolve()
    resolved_base = base.resolve()
    if not is_within_directory(resolved_path, resolved_base):
        raise UnsafePathError("Acceso fuera del sandbox detectado.")
    return resolved_path

def _check_windows_file_attributes(path_str: str) -> None:
    """Filtra archivos con atributos especiales de sistema en Windows."""
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
    """Valida que origen y destino estén en el mismo volumen (evita cruce)."""
    if source.stat().st_dev != target_dir.stat().st_dev:
        raise UnsafePathError("Operación entre distintos volúmenes no permitida.")

def _validate_isolation_constraints(source_path: Path, dest_dir: Path) -> None:
    """Valida jerarquías de seguridad y evitar recursividad en el movimiento."""
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
    """Ejecuta chequeos finales pre-aislamiento."""
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
    
    try:
        _check_device_consistency(resolved_source, resolved_dest_dir)
        if os.path.samefile(resolved_source, resolved_dest_dir):
            raise UnsafePathError("Operación circular detectada.")
    except OSError:
        pass
        
    _validate_isolation_constraints(resolved_source, resolved_dest_dir)
    ensure_safe_to_modify(resolved_source, allow_sensitive=True)
    if _is_file_locked(resolved_source):
        raise IOError("Archivo en uso.")


def _validate_isolation_request(source_path: Path, dest_dir: Path) -> None:
    """Valida precondiciones completas antes de aislar."""
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
    """Deserializa el manifiesto, usando caché de sesión para rendimiento."""
    base_dir = quarantine_dir(base)
    base_key = str(base_dir)
    if base_key in _MANIFEST_CACHE and not force_reload:
        return _MANIFEST_CACHE[base_key]
        
    try:
        m_path = _manifest_path(base_dir)
        if not m_path.exists() or m_path.stat().st_size == 0:
            return []
        
        with open(m_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        if not isinstance(data, list):
            return []
            
        items = [i for d in data if (i := QuarantineItem.from_dict(d))]
        _MANIFEST_CACHE[base_key] = items
        return items
    except (OSError, PermissionError, UnsafePathError, json.JSONDecodeError):
        return []


def save_manifest(items: List[QuarantineItem], base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persiste el manifiesto usando escritura atómica y validación de integridad."""
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista.")
    if not all(isinstance(i, QuarantineItem) for i in items):
        raise TypeError("Ítems no compatibles.")
    
    base_path = quarantine_dir(base)
    target_path = _manifest_path(base_path)
    
    try:
        serializable_items = [item.to_dict() for item in items]
        encoded_content = json.dumps(serializable_items, indent=2, ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError) as e:
        raise RuntimeError(f"Error serializando manifiesto: {e}")
    
    temp_path: Optional[Path] = None
    try:
        if not is_safe_to_modify(base_path):
            raise UnsafePathError("Directorio destino no seguro para persistir.")
            
        with tempfile.NamedTemporaryFile("wb", dir=base_path, delete=False) as tf:
            temp_path = Path(tf.name)
            tf.write(encoded_content)
            tf.flush()
            os.fsync(tf.fileno())
        
        if temp_path and temp_path.exists() and temp_path.stat().st_size == len(encoded_content):
            os.replace(temp_path, target_path)
            _MANIFEST_CACHE[str(base_path)] = items
            try:
                with open(base_path, "rb") as d:
                    os.fsync(d.fileno())
            except (OSError, AttributeError):
                pass
        else:
            raise OSError("Integridad del archivo temporal fallida.")
        return target_path
    except (OSError, IOError) as e:
        raise RuntimeError(f"Error crítico al persistir manifiesto: {e}")
    finally:
        if temp_path and temp_path.exists():
            try: _check_io_error_context(os.remove, temp_path)
            except OSError: pass


def _ensure_disk_space(dest_dir: Path, required_size: int) -> None:
    """Verifica disponibilidad real de espacio en el destino."""
    if not dest_dir.exists():
        raise FileNotFoundError(f"Directorio inexistente: {dest_dir}")
    if not is_safe_to_modify(dest_dir) or not os.access(dest_dir, os.W_OK):
        raise PermissionError(f"Sin permisos de escritura seguros: {dest_dir}")
    test_file = dest_dir / f".test_{uuid.uuid4().hex}"
    try:
        test_file.touch()
        _check_io_error_context(test_file.unlink)
    except OSError:
        raise OSError("Sistema de archivos del destino marcado como solo lectura.")
    usage = shutil.disk_usage(dest_dir)
    margin = max(int(required_size * 0.05), 5 * 1024 * 1024)
    if usage.free < (required_size + margin):
        raise OSError("Espacio insuficiente en disco.")


def _validate_file_transfer_preconditions(source: Path, destination: Path) -> None:
    """Verifica seguridad del destino antes de la transferencia."""
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
    """Genera ruta temporal única para el proceso de transferencia."""
    return destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"


def _copy_with_verification(source: Path, temp_dest: Path, source_hash: str) -> None:
    """Realiza copia byte a byte verificando integridad final."""
    try:
        fd_src = os.open(str(source), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise OSError(f"No se pudo abrir el origen de forma segura: {e}")
    try:
        with os.fdopen(fd_src, "rb") as f_src:
            stat_src = os.fstat(f_src.fileno())
            if not (stat_src.st_mode & 0o100000):
                raise OSError("El archivo origen no es un archivo regular.")
            if not is_safe_to_modify(temp_dest.parent):
                raise UnsafePathError("Directorio de destino no seguro.")
            dst_fd = os.open(str(temp_dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(dst_fd, "wb") as f_dst:
                    shutil.copyfileobj(f_src, f_dst)
                    f_dst.flush()
                    os.fsync(dst_fd)
            except Exception:
                os.close(dst_fd)
                raise
        if temp_dest.stat().st_size != stat_src.st_size:
            raise OSError("Falla de integridad: tamaño mismatch tras copia.")
        final_hash = _get_sha256(temp_dest)
        if not final_hash or final_hash != source_hash:
            raise OSError("Falla crítica: el hash del archivo copiado no coincide.")
    except (OSError, IOError) as e:
        if temp_dest.exists():
            try: _check_io_error_context(temp_dest.unlink)
            except OSError: pass
        raise OSError(f"Falla durante operación I/O de copia: {e}")


def _write_temp_to_final(source: Path, destination: Path) -> Tuple[str, int]:
    """Gestiona el flujo completo: validación, copia verificada y reemplazo."""
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
        _copy_with_verification(source, temp_dest, source_hash)
        os.replace(temp_dest, destination)
        ensure_safe_to_modify(destination, allow_sensitive=True)
        return source_hash, destination.stat().st_ino
    except Exception as e:
        if temp_dest.exists():
            try: _check_io_error_context(temp_dest.unlink)
            except OSError: pass
        if destination.exists():
            _safe_unlink(destination)
        raise OSError(f"Error crítico en transferencia: {e}")


def _atomic_isolate_file(source: Path, destination: Path, original_size: int) -> Tuple[str, int]:
    """Aislamiento atómico de un archivo sospechoso validando TOCTOU."""
    if not source.exists():
        raise FileNotFoundError("Archivo origen no existe.")
    stat_orig = _check_io_error_context(source.stat)
    if stat_orig.st_size != original_size:
        raise RuntimeError("El archivo cambió durante la validación inicial (TOCTOU).")
    
    if source.resolve() == destination.resolve():
        raise UnsafePathError("El origen ya reside en el directorio destino.")
    _validate_quarantine_path(destination, destination.parent)
    if len(str(destination)) >= 250:
        raise OSError("Ruta destino demasiado larga.")
    try:
        return _write_temp_to_final(source, destination)
    except Exception as e:
        if destination.exists():
            _safe_unlink(destination)
        raise RuntimeError(f"Error durante aislamiento: {e}")


def _register_quarantine_item(
    destination: Path,
    source_path: Path,
    file_hash: str,
    file_inode: int,
    reason: str,
    original_size: int,
    base: PathLike
) -> QuarantineItem:
    """Registra metadatos en el manifiesto JSON tras el aislamiento exitoso."""
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
    except Exception as e:
        if destination.exists():
            _safe_unlink(destination)
        raise RuntimeError(f"Falla al registrar ítem en manifiesto: {e}")


def _validate_source_for_quarantine(source: Path) -> Path:
    """Valida integridad de la fuente pre-aislamiento."""
    if source.is_symlink():
        raise UnsafePathError("Aislamiento de enlaces simbólicos no permitido.")
    try:
        resolved = source.resolve(strict=True)
        if hasattr(resolved, 'is_junction') and resolved.is_junction():
            raise UnsafePathError("Aislamiento de puntos de reparse (Junctions) no permitido.")
    except (OSError, RuntimeError):
        pass
    if source.is_dir():
        raise UnsafePathError("Aislamiento de directorios no permitido.")
    if not source.is_file():
        raise FileNotFoundError("Archivo origen inexistente.")
    if _is_file_locked(source):
        raise IOError("Archivo origen bloqueado por el sistema.")
    return source

def _cleanup_orphaned_destination(destination: Path) -> None:
    """Elimina remanentes de operaciones fallidas."""
    if destination.exists():
        _safe_unlink(destination)

def _verify_transaction_integrity(item: QuarantineItem, destination: Path) -> None:
    """Comprueba coherencia post-registro."""
    if not item.verify_integrity(destination):
        raise RuntimeError("Integridad post-registro fallida.")

def _validate_input_path(source: PathLike) -> Path:
    """Valida los parámetros de entrada para operaciones públicas."""
    if not source:
        raise ValueError("Ruta de origen nula o vacía.")
    p_source = Path(source)
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
    """
    Aísla un archivo de forma segura, respetando todas las garantías de integridad.
    """
    p_source = _validate_input_path(source)
    source_path = _validate_source_for_quarantine(p_source)
    
    original_size = source_path.stat().st_size
    dest_dir = quarantine_dir(base)
    
    if _is_within_quarantine_sandbox(source_path, dest_dir.resolve()):
        raise UnsafePathError("Archivo ya en el sandbox.")
    
    _validate_isolation_request(source_path, dest_dir)
    destination = dest_dir / _generate_safe_stored_name(source_path, uuid.uuid4().hex[:12])
    
    if destination.exists():
        raise FileExistsError("Colisión: el nombre de destino ya existe en la cuarentena.")

    try:
        file_hash, file_inode = _atomic_isolate_file(source_path, destination, original_size)
        
        if not destination.exists() or _get_sha256(destination) != file_hash:
            raise RuntimeError("Falla crítica: el destino no es coherente tras la copia.")
            
        if source_path.exists():
            try:
                _check_io_error_context(source_path.unlink)
            except OSError as e:
                raise RuntimeError(f"Aislamiento exitoso, pero falla al remover origen: {e}")
                
        item = _register_quarantine_item(destination, source_path, file_hash, file_inode, reason, original_size, base)
        _verify_transaction_integrity(item, destination)
        return item
    except Exception as e:
        _cleanup_orphaned_destination(destination)
        raise RuntimeError(f"Error durante aislamiento: {e}")

def list_items(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Lista elementos en cuarentena, ordenados por fecha de aislamiento."""
    try:
        quarantine_dir(base)
        return sorted(load_manifest(base), key=lambda x: x.quarantined_at, reverse=True)
    except (OSError, UnsafePathError, PermissionError):
        return []


def restore_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un archivo validando integridad, permisos y destino seguro."""
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID de ítem vacío o inválido.")
    try:
        base_path = quarantine_dir(base)
        items = load_manifest(base)
        item_map = {i.item_id: i for i in items}
        
        quarantine_item = item_map.get(item_id)
        if quarantine_item is None:
            raise KeyError(f"Ítem no encontrado: {item_id}")
        
        stored_file = _validate_quarantine_path(base_path / quarantine_item.stored_name, base_path)
        if not stored_file.exists() or not stored_file.is_file():
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
            try:
                _check_io_error_context(parent.mkdir, parents=True, exist_ok=True)
            except OSError as e:
                raise RuntimeError(f"Falla al crear destino: {e}")
        
        if not is_safe_to_modify(destination):
            raise UnsafePathError("Destino no seguro.")
        
        if not stored_file.exists() or not quarantine_item.verify_integrity(stored_file):
            raise RuntimeError("Falla de integridad post-validación.")
            
        os.replace(str(stored_file), str(destination))
        save_manifest([i for i in items if i.item_id != item_id], base)
        return destination
    except (OSError, PermissionError, IOError) as e:
        raise RuntimeError(f"Error crítico en restauración: {e}")


def purge_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> bool:
    """Elimina permanentemente un ítem específico de la cuarentena."""
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID de ítem vacío o inválido.")
    base_path = quarantine_dir(base)
    items = load_manifest(base)
    item_map = {i.item_id: i for i in items}
    
    quarantine_item = item_map.get(item_id)
    if quarantine_item is None:
        return False
        
    stored_file = _validate_quarantine_path(base_path / quarantine_item.stored_name, base_path)
    if not stored_file.exists():
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
    if not quarantine_item.verify_integrity(stored_file):
        raise UnsafePathError(f"Integridad fallida para {item_id}.")
    if _safe_unlink(stored_file, expected_hash=quarantine_item.sha256, expected_inode=quarantine_item.file_inode):
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
    return False


def _is_item_purgable(file_path: Path, item: QuarantineItem, base_path: Path) -> bool:
    """Valida los requisitos de seguridad antes de eliminar un archivo de la cuarentena."""
    if not file_path.is_file() or file_path.is_symlink():
        return False
    if not is_within_directory(file_path.resolve(), base_path.resolve()):
        return False
    if hasattr(file_path, 'is_junction') and file_path.is_junction():
        return False
    if not is_safe_to_modify(file_path):
        return False
    
    return (
        item.verify_integrity(file_path) and
        _safe_unlink(file_path, expected_hash=item.sha256, expected_inode=item.file_inode)
    )


def purge_all(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Elimina todos los archivos verificados de la cuarentena."""
    try:
        quarantine_root = quarantine_dir(base)
        items = load_manifest(base)
        # Diccionario para acceso O(1) al buscar por nombre de archivo almacenado
        item_map = {i.stored_name: i for i in items}
        
        purged_ids: Set[str] = set()
        for f in quarantine_root.iterdir():
            if f.name == MANIFEST_NAME or not f.exists() or not f.is_file():
                continue
            if not is_safe_to_modify(f):
                continue
            item = item_map.get(f.name)
            if item and _is_item_purgable(f, item, quarantine_root):
                purged_ids.add(item.item_id)
        
        if purged_ids:
            save_manifest([i for i in items if i.item_id not in purged_ids], base)
            
        return len(purged_ids)
    except (OSError, PermissionError, UnsafePathError):
        return 0


def total_quarantined_bytes(base: PathLike = DEFAULT_QUARANTINE_DIR, items: Optional[List[QuarantineItem]] = None) -> int:
    """Calcula el espacio total ocupado por los archivos en cuarentena."""
    if items is None:
        items = load_manifest(base)
    return sum(item.size_bytes for item in items)


def summarize(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte legible del estado actual de la cuarentena."""
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
