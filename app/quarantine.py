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
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from functools import lru_cache
from typing import List, Union, Dict, Any, Optional, TypeAlias, Set, Tuple

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

WINDOWS_RESERVED_NAMES: Set[str] = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", 
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", 
    "LPT6", "LPT7", "LPT8", "LPT9"
}

@dataclass
class QuarantineItem:
    """
    Modelo de datos para un archivo en cuarentena.
    
    Gestiona la persistencia de metadatos de seguridad y validación de 
    integridad (SHA-256) entre el almacenamiento en disco y el manifiesto.
    """
    item_id: str
    original_path: str
    stored_name: str
    size_bytes: int
    reason: str
    quarantined_at: str
    sha256: str = ""

    def __post_init__(self) -> None:
        """Normaliza tipos y valida la integridad de los datos de la instancia."""
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
        """Retorna el tamaño en MB con redondeo a dos decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Convierte la instancia a diccionario para su serialización."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional[QuarantineItem]:
        """
        Crea instancia desde diccionario tras validar presencia de claves.
        
        Args:
            data: Diccionario con la estructura del ítem.
        Returns:
            Instancia de QuarantineItem o None si el diccionario es inválido.
        """
        if not isinstance(data, dict):
            return None
        required: Tuple[str, ...] = ("item_id", "original_path", "stored_name", "size_bytes", "reason", "quarantined_at")
        if not all(key in data for key in required):
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
                sha256=str(data.get("sha256", ""))
            )
        except (ValueError, TypeError):
            return None

    def _validate_integrity(self, stored_path: Path) -> bool:
        """
        Verificación de superficie: comprueba existencia, tipo y tamaño.
        
        Args:
            stored_path: Ruta del archivo dentro del sandbox.
        """
        if not stored_path or not stored_path.exists(): return False
        try:
            st = stored_path.stat()
            return (
                stored_path.is_file() and 
                not stored_path.is_symlink() and 
                st.st_size == self.size_bytes and
                st.st_size > 0
            )
        except (OSError, PermissionError):
            return False

    def verify_integrity(self, stored_path: Path) -> bool:
        """
        Verificación profunda: compara el hash SHA-256 del disco contra el manifiesto.
        """
        if not stored_path or not self._validate_integrity(stored_path):
            return False
        try:
            return bool(self.sha256 and _get_sha256(stored_path) == self.sha256)
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """Calcula hash SHA-256 usando buffers para manejar archivos grandes."""
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


def _is_file_locked(path: Path) -> bool:
    """
    Comprueba si un archivo está bloqueado intentando abrirlo en r+b.
    
    Returns:
        True si el archivo está en uso exclusivo o inaccesible.
    """
    if not isinstance(path, Path) or not path.exists():
        return False
    try:
        # Intentar apertura exclusiva simulada mediante bloqueo de descriptor
        with open(path, "a+b") as f:
            f.flush()
            os.fsync(f.fileno())
            return False
    except (PermissionError, IOError, OSError):
        return True


def _safe_unlink(path: Path) -> bool:
    """
    Realiza una eliminación segura verificando protección y estado de bloqueo.
    """
    if not path.is_file() or path.is_symlink() or is_protected_path(path):
        return False
        
    try:
        if is_safe_to_modify(path) and not _is_file_locked(path):
            path.unlink()
            return True
        return False
    except (OSError, PermissionError):
        return False

def _is_item_unreachable(path: Path) -> bool:
    """Valida que la ruta no contenga ADS o técnicas de ofuscación."""
    if ":" in path.name.replace(path.drive, ""): return True
    if any(c in str(path) for c in ("\0", "\x00")): return True
    return False

def _sanitize_filename(filename: str) -> str:
    """Filtra caracteres para evitar inyección de rutas."""
    return "".join(c for c in filename if c.isalnum() or c in "._-")

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """Genera nombre seguro para almacenamiento evitando colisiones."""
    sanitized = _sanitize_filename(original_path.name)
    if not sanitized or sanitized in (".", ".."):
        sanitized = "unknown_file"
        
    parts = sanitized.split('.')
    name_base = parts[0] if parts[0] else "q_file"
    if name_base.upper() in WINDOWS_RESERVED_NAMES:
        name_base = f"q_{name_base}"
    
    extension = f".{parts[-1]}" if len(parts) > 1 else ""
    return f"{item_id}__{name_base[:64]}{extension}"[:128]

def _ensure_path_ownership(path: Path) -> None:
    """Valida propiedad del directorio (POSIX)."""
    if hasattr(os, 'getuid'):
        if path.stat().st_uid != os.getuid():
            raise UnsafePathError("Propiedad de directorio no coincide con usuario.")

def quarantine_dir(base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Resuelve y asegura la carpeta de cuarentena."""
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path = Path(base).expanduser().resolve()
        if not path.name.strip():
            raise UnsafePathError("Ruta de cuarentena inválida.")
        if is_protected_path(path):
            raise UnsafePathError("Directorio de cuarentena reside en ruta protegida.")
        if not is_safe_to_modify(path):
            raise UnsafePathError("Directorio no cumple políticas de seguridad.")
        
        path.mkdir(parents=True, exist_ok=True)
        _ensure_path_ownership(path)
        return path
    except (OSError, RuntimeError) as e:
        raise OSError(f"Error al preparar directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ubicación del manifiesto."""
    return (base_dir / MANIFEST_NAME).resolve()


def _is_within_quarantine_sandbox(path: Path, root: Path) -> bool:
    """Valida la contención física del archivo dentro de la carpeta sandbox."""
    return is_within_directory(path, root)

def _validate_quarantine_path(path: Path, base: Path) -> Path:
    """Valida que la ruta esté canónicamente contenida en el sandbox."""
    resolved_path = path.resolve()
    resolved_base = base.resolve()
    if not is_within_directory(resolved_path, resolved_base):
        raise UnsafePathError("Acceso fuera del sandbox detectado.")
    return resolved_path

def _check_windows_file_attributes(path_str: str) -> None:
    """Valida atributos de sistema en Windows para detectar ofuscación."""
    if os.name != 'nt':
        return
    path_obj = Path(path_str)
    if not path_obj.exists():
        return
    import ctypes
    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path_obj))
    if attrs != -1:
        # Bloquear archivos de sistema (0x04) y ocultos (0x02) por seguridad
        if attrs & 0x02 or attrs & 0x04:
            raise UnsafePathError("Archivo con atributos del sistema/oculto no permitido.")


def _check_path_syntax_integrity(path: Path) -> None:
    """Valida sintaxis de ruta para prevenir Path Traversal y ADS."""
    if not path:
        raise UnsafePathError("Ruta vacía.")
    
    path_str = str(path)
    if any(ord(c) < 32 for c in path_str) or "\0" in path_str:
        raise UnsafePathError("Ruta con caracteres de control.")
    if len(path.parts) > 32:
        raise UnsafePathError("Profundidad de ruta excesiva.")
    if _is_item_unreachable(path):
        raise UnsafePathError("Ruta con flujos de datos alternos (ADS) o caracteres prohibidos.")
    
    try:
        resolved = path.resolve(strict=True)
        if resolved.is_symlink():
            raise UnsafePathError("Operación denegada: enlace simbólico.")
        if hasattr(resolved, 'is_junction') and resolved.is_junction():
            raise UnsafePathError("Operación denegada: punto de reparse.")
    except (OSError, RuntimeError):
        pass


def _check_isolation_safety(source_path: Path, dest_dir: Path) -> None:
    """
    Verifica condiciones de seguridad específicas sobre el origen y destino.
    """
    resolved_source = source_path.resolve(strict=True)
    resolved_dest_dir = dest_dir.resolve()
    
    if not resolved_source.is_file():
        raise UnsafePathError("Solo se permiten archivos regulares.")
    if resolved_source.is_symlink():
        raise UnsafePathError("Aislamiento de enlaces simbólicos prohibido.")
    if resolved_source.stat().st_size == 0:
        raise UnsafePathError("Archivos vacíos prohibidos.")
    
    try:
        if os.path.samefile(resolved_source, resolved_dest_dir):
            raise UnsafePathError("Operación circular detectada.")
    except OSError:
        pass

    if resolved_source.parent == resolved_dest_dir:
        raise UnsafePathError("Operación circular detectada.")
    if is_protected_path(resolved_source):
        raise UnsafePathError("Ruta origen protegida.")
    if is_protected_path(resolved_dest_dir) or is_protected_path(resolved_dest_dir.parent):
        raise UnsafePathError("Destino en ruta protegida.")
    if _is_within_quarantine_sandbox(resolved_source, resolved_dest_dir):
        raise UnsafePathError("Archivo ya se encuentra en sandbox.")
        
    if resolved_source.stat().st_dev != resolved_dest_dir.stat().st_dev:
        raise UnsafePathError("Dispositivos incompatibles.")
    
    ensure_safe_to_modify(resolved_source, allow_sensitive=True)
    if _is_file_locked(resolved_source):
        raise IOError("Archivo en uso.")


def _validate_isolation_request(source_path: Path, dest_dir: Path) -> None:
    """
    Ejecuta el protocolo completo de pre-validación de seguridad.
    """
    _check_path_syntax_integrity(source_path)
    _check_windows_file_attributes(str(source_path))
    
    if source_path.is_symlink():
        raise UnsafePathError("Aislamiento de enlaces simbólicos denegado.")

    try:
        resolved_source = source_path.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise UnsafePathError(f"Ruta origen inaccesible: {e}")
    
    _ensure_disk_space(dest_dir, resolved_source.stat().st_size)
    _check_isolation_safety(resolved_source, dest_dir)


@lru_cache(maxsize=8)
def _load_manifest_raw(base_str: str, content_hash: str) -> List[QuarantineItem]:
    """Carga y normaliza el manifiesto desde JSON."""
    path = _manifest_path(Path(base_str))
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list): 
                return []
            items = []
            for d in data:
                if not isinstance(d, dict):
                    continue
                try:
                    item = QuarantineItem.from_dict(d)
                    if item:
                        items.append(item)
                except (ValueError, KeyError, TypeError):
                    continue
            return items
    except (json.JSONDecodeError, OSError, PermissionError):
        return []

def load_manifest(base: PathLike = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Carga y sincroniza la lista de ítems en cuarentena."""
    try:
        base_path = quarantine_dir(base)
        m_path = _manifest_path(base_path)
        
        current_hash = "none"
        if m_path.exists():
            try:
                with open(m_path, "rb") as f:
                    current_hash = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                pass
        
        if force_reload:
            _load_manifest_raw.cache_clear()
        
        return list(_load_manifest_raw(str(base_path), current_hash))
    except (OSError, UnsafePathError):
        return []


def save_manifest(items: List[QuarantineItem], base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persiste el manifiesto usando escritura atómica."""
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista.")
    
    if not all(isinstance(i, QuarantineItem) for i in items):
        raise TypeError("Ítems no compatibles.")

    base_path = quarantine_dir(base)
    target_path = _manifest_path(base_path)
    temp_path: Optional[Path] = None
    
    try:
        if not items and target_path.exists() and target_path.stat().st_size > 1024:
             raise RuntimeError("Prevención de corrupción: intento de persistir vacío.")

        content = json.dumps([item.to_dict() for item in items], indent=2, ensure_ascii=False)
        
        with tempfile.NamedTemporaryFile("w", dir=base_path, encoding="utf-8", delete=False) as tf:
            temp_path = Path(tf.name)
            tf.write(content)
            tf.flush()
            os.fsync(tf.fileno())
            
        if temp_path.stat().st_size != len(content.encode('utf-8')):
             raise OSError("Integridad del archivo temporal fallida.")

        os.replace(temp_path, target_path)
        
        dir_fd = os.open(str(base_path), os.O_RDONLY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
        
        _load_manifest_raw.cache_clear()
        return target_path
    except (OSError, TypeError, IOError) as e:
        if temp_path and isinstance(temp_path, Path) and temp_path.exists():
            try: os.remove(temp_path)
            except OSError: pass
        raise RuntimeError(f"Error crítico al persistir manifiesto: {e}")


def _ensure_disk_space(dest_dir: Path, required_size: int) -> None:
    """Comprueba capacidad de disco con un margen de seguridad."""
    if not dest_dir.exists():
        raise FileNotFoundError(f"Directorio inexistente: {dest_dir}")
    if not os.access(dest_dir, os.W_OK):
        raise PermissionError(f"Sin permisos de escritura: {dest_dir}")
    usage = shutil.disk_usage(dest_dir)
    margin = max(int(required_size * 0.05), 5 * 1024 * 1024)
    if usage.free < (required_size + margin):
        raise OSError("Espacio insuficiente en disco.")


def _write_temp_to_final(source: Path, destination: Path) -> str:
    """
    Copia al sandbox y valida integridad contra condiciones TOCTOU (Time-of-check to time-of-use).
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(destination), flags, 0o600)
    
    try:
        src_stat = source.stat()
        src_ino = src_stat.st_ino
        src_dev = src_stat.st_dev
        
        with os.fdopen(fd, 'wb') as tmp:
            with open(source, 'rb') as src:
                shutil.copyfileobj(src, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        
        final_src_stat = source.stat()
        if final_src_stat.st_ino != src_ino or final_src_stat.st_dev != src_dev:
             raise OSError("Alerta de seguridad: origen reemplazado durante copia.")
        
        if destination.stat().st_size != src_stat.st_size or destination.stat().st_size == 0:
            raise OSError("Error de integridad post-escritura.")
            
        _check_windows_file_attributes(str(destination))
        ensure_safe_to_modify(destination, allow_sensitive=True)
        
        dir_fd = os.open(str(destination.parent), os.O_RDONLY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
        
        file_hash = _get_sha256(destination)
        if not file_hash:
            raise OSError("Falla de integridad: hash no generado.")
        return file_hash
    except Exception as e:
        if destination.exists():
            try: os.remove(destination)
            except OSError: pass
        raise e


def _atomic_isolate_file(source: Path, destination: Path, original_size: int) -> str:
    """
    Realiza el aislamiento atómico de un archivo dentro del sandbox.
    """
    if not source.exists():
        raise FileNotFoundError("Archivo origen inexistente.")
    
    _validate_quarantine_path(destination, destination.parent)
    
    if len(str(destination)) >= 250:
        raise OSError("Ruta destino demasiado larga.")

    try:
        return _write_temp_to_final(source, destination)
    except Exception as e:
        raise RuntimeError(f"Error durante aislamiento: {e}")


def _register_quarantine_item(
    destination: Path,
    source_path: Path,
    file_hash: str,
    reason: str,
    original_size: int,
    base: PathLike
) -> QuarantineItem:
    """Registra y persiste el ítem en el manifiesto tras aislamiento exitoso."""
    items_list = load_manifest(base)
    quarantine_item = QuarantineItem(
        item_id=uuid.uuid4().hex[:12],
        original_path=str(source_path),
        stored_name=destination.name,
        size_bytes=original_size,
        reason=str(reason) if reason else "Sin motivo",
        quarantined_at=datetime.now().isoformat(timespec="seconds"),
        sha256=file_hash,
    )
    items_list.append(quarantine_item)
    save_manifest(items_list, base)
    return quarantine_item


def quarantine_file(
    source: PathLike,
    reason: str = "Marcado como sospechoso",
    base: PathLike = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """
    Ejecuta el ciclo de vida completo de aislamiento y registro de un archivo.
    """
    if not source:
        raise ValueError("Ruta de origen vacía.")
    
    p_source = Path(source)
    if not p_source.is_absolute():
        try:
            p_source = p_source.resolve(strict=True)
        except (OSError, RuntimeError) as e:
            raise UnsafePathError(f"Ruta origen no válida: {e}")
    
    source_path = p_source
    if source_path.is_dir():
        raise UnsafePathError("Aislamiento de directorios no permitido.")
    if source_path.is_symlink():
        raise UnsafePathError("No se permite aislar enlaces simbólicos.")
    
    if not source_path.is_file():
        raise FileNotFoundError("Archivo origen inexistente.")
        
    original_size = source_path.stat().st_size
    dest_dir = quarantine_dir(base)
    
    if _is_within_quarantine_sandbox(source_path, dest_dir.resolve()):
        raise UnsafePathError("Archivo ya en el sandbox.")

    _validate_isolation_request(source_path, dest_dir)
    
    source_hash = _get_sha256(source_path)
    if not source_hash:
        raise RuntimeError("No se pudo calcular firma digital.")
    
    destination = dest_dir / _generate_safe_stored_name(source_path, uuid.uuid4().hex[:12])
    
    try:
        file_hash = _atomic_isolate_file(source_path, destination, original_size)
        item = _register_quarantine_item(destination, source_path, file_hash, reason, original_size, base)
        
        if item.verify_integrity(destination):
            try:
                source_path.unlink()
            except OSError as e:
                _safe_unlink(destination)
                raise RuntimeError(f"Falla al eliminar original: {e}")
            return item
        else:
            raise RuntimeError("Fallo de integridad post-persistencia.")
    except Exception:
        if destination.exists():
            _safe_unlink(destination)
        raise

def list_items(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna ítems validados presentes en el sandbox y purga huérfanos."""
    try:
        base_path = quarantine_dir(base)
        items = load_manifest(base)
        # Eficiencia O(1) con set
        existing_files = {f.name for f in base_path.iterdir() if f.is_file()}
        
        valid_items = [i for i in items if i.stored_name in existing_files]
        if len(valid_items) != len(items):
            save_manifest(valid_items, base)
            
        return sorted(valid_items, key=lambda x: x.quarantined_at, reverse=True)
    except (OSError, UnsafePathError):
        return []


def restore_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un archivo al destino original tras verificaciones."""
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID de ítem inválido.")
    
    try:
        base_path = quarantine_dir(base)
        items = load_manifest(base)
        quarantine_item = next((i for i in items if i.item_id == item_id), None)
        
        if quarantine_item is None:
            raise KeyError(f"Ítem no encontrado: {item_id}")
            
        stored_file = _validate_quarantine_path(base_path / quarantine_item.stored_name, base_path)
        
        if not stored_file.exists() or not stored_file.is_file():
            raise RuntimeError("Archivo en cuarentena inexistente.")
            
        if not quarantine_item.verify_integrity(stored_file):
            raise RuntimeError("Integridad comprometida.")
        
        destination = Path(quarantine_item.original_path).resolve()
        _check_path_syntax_integrity(destination)
        if is_protected_path(destination):
            raise UnsafePathError("Restauración denegada: destino protegido.")
        if destination.exists():
            raise FileExistsError("El destino ya existe.")
        
        if stored_file.stat().st_dev != destination.parent.resolve().stat().st_dev:
            raise UnsafePathError("Dispositivos incompatibles.")
        
        parent = destination.parent
        if not is_safe_to_modify(parent):
            raise UnsafePathError("Directorio padre no seguro.")
            
        _ensure_disk_space(parent, quarantine_item.size_bytes)
        
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise RuntimeError(f"Falla al crear destino: {e}")
                
        if not is_safe_to_modify(destination):
            raise UnsafePathError("Destino no seguro.")
            
        os.replace(str(stored_file), str(destination))
        save_manifest([i for i in items if i.item_id != item_id], base)
        return destination
    except (OSError, PermissionError, IOError) as e:
        raise RuntimeError(f"Error crítico en restauración: {e}")


def purge_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> bool:
    """Elimina permanentemente un ítem específico."""
    if not isinstance(item_id, str) or not item_id.strip():
        return False
        
    base_path = quarantine_dir(base)
    items = load_manifest(base)
    quarantine_item = next((i for i in items if i.item_id == item_id), None)
    
    if quarantine_item is None:
        return False
        
    stored_file = _validate_quarantine_path(base_path / quarantine_item.stored_name, base_path)
    if not stored_file.exists():
        save_manifest([i for i in items if i.item_id != item_id], base)
        return False
        
    if not quarantine_item.verify_integrity(stored_file):
        raise UnsafePathError(f"Integridad fallida para {item_id}.")
        
    if _safe_unlink(stored_file):
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
    return False


def _is_item_purgable(file_path: Path, item: QuarantineItem, base_path: Path) -> bool:
    """
    Verifica si un ítem cumple los requisitos para ser purgado del sandbox.
    """
    return (
        file_path.exists() and
        is_within_directory(file_path, base_path) and
        item.verify_integrity(file_path) and
        _safe_unlink(file_path)
    )


def purge_all(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Limpia todos los archivos validados de la cuarentena."""
    try:
        quarantine_root = quarantine_dir(base)
    except (OSError, RuntimeError, UnsafePathError):
        return 0
        
    items = load_manifest(base)
    item_map = {item.stored_name: item for item in items}
    purged_ids: Set[str] = set()
    
    try:
        for stored_path in quarantine_root.iterdir():
            if stored_path.name == MANIFEST_NAME or stored_path.is_dir():
                continue
            item = item_map.get(stored_path.name)
            if item and _is_item_purgable(stored_path, item, quarantine_root):
                purged_ids.add(item.item_id)
                
        if purged_ids:
            remaining_items = [i for i in items if i.item_id not in purged_ids]
            save_manifest(remaining_items, base)
            
    except (OSError, PermissionError):
        pass
        
    return len(purged_ids)


def total_quarantined_bytes(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Retorna el espacio total ocupado en bytes."""
    return sum(item.size_bytes for item in load_manifest(base))


def summarize(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte legible de los ítems en cuarentena."""
    items = list_items(base)
    if not items:
        return ["La cuarentena está vacía."]
    
    total_mb = sum(i.size_mb for i in items)
    lines = [f"{len(items)} archivo(s) en cuarentena — {round(total_mb, 2)} MB", ""]
    for item in items:
        lines.extend([
            f"  [{item.item_id}] {Path(item.original_path).name} — {item.size_mb} MB",
            f"      Motivo: {item.reason}",
            f"      Origen: {item.original_path}",
            f"      Aislado: {item.quarantined_at}"
        ])
    lines.extend(["", "Nada de esto se borró: se puede restaurar a su ubicación original."])
    return lines
