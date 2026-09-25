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
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Union, Dict, Any, TypeAlias, Set, Tuple, Optional

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
    
    Esta clase actúa como el contrato de datos para el manifiesto de cuarentena,
    asegurando que cada ítem contenga la trazabilidad necesaria para una 
    restauración segura (ubicación original, hash de integridad y razón).
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
        """Calcula el tamaño en MB con precisión de dos decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la instancia a un diccionario plano para JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional[QuarantineItem]:
        """
        Instancia un QuarantineItem desde un diccionario, validando esquemas.

        Args:
            data: Diccionario con los datos persistidos del ítem.
            
        Returns:
            Una instancia válida o None si el esquema no es correcto.
        """
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
                sha256=str(data.get("sha256", ""))
            )
        except (ValueError, TypeError):
            return None

    def _validate_integrity(self, stored_path: Path) -> bool:
        """Verificación física: confirma que el archivo existe y es un archivo regular."""
        if not stored_path.exists(): return False
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
        Verificación profunda: contrasta el hash SHA-256 del archivo contra el registro.

        Args:
            stored_path: Ruta física del archivo en el sandbox.

        Returns:
            True si el hash coincide exactamente con la firma almacenada.
        """
        if not self._validate_integrity(stored_path):
            return False
        try:
            return bool(self.sha256 and _get_sha256(stored_path) == self.sha256)
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """
    Calcula el hash SHA-256 de un archivo mediante streaming para ahorrar memoria.

    Args:
        path: Objeto Path al archivo a procesar.

    Returns:
        Hash hexadecimal como string, o cadena vacía en caso de error.
    """
    if not path.is_file():
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


def _is_file_locked(path: Path) -> bool:
    """
    Determina si un archivo está siendo bloqueado por otro proceso.
    Utiliza un intento de apertura exclusivo para verificar disponibilidad.
    """
    if not path.exists():
        return True
    try:
        # Intenta abrir con acceso compartido para lectura/escritura (o solo lectura)
        # En sistemas POSIX esto suele ser siempre posible, en Windows falla si otro proceso tiene el lock
        fd = os.open(path, os.O_RDWR) if os.access(path, os.W_OK) else os.open(path, os.O_RDONLY)
        os.close(fd)
        return False
    except (OSError, IOError):
        return True

def _safe_unlink(path: Path, expected_hash: Optional[str] = None) -> bool:
    """
    Elimina un archivo tras validar seguridad y opcionalmente su integridad.
    Retorna True solo si la eliminación física fue exitosa.
    """
    try:
        if not path.exists():
            return False
        resolved = path.resolve()
        
        is_valid_target = (
            resolved.exists() and 
            resolved.is_file() and 
            not resolved.is_symlink() and 
            not is_protected_path(resolved) and
            is_safe_to_modify(resolved)
        )
        if not is_valid_target:
            return False
            
        if expected_hash and _get_sha256(resolved) != expected_hash:
            return False

        ensure_safe_to_modify(resolved)
        
        if not _is_file_locked(resolved):
            path.unlink()
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try: os.fsync(dir_fd)
                finally: os.close(dir_fd)
            except OSError:
                pass
            return True
        return False
    except (OSError, PermissionError, UnsafePathError):
        return False

def _check_path_syntax_integrity(path: Path) -> None:
    """
    Previene ataques de 'Path Traversal' validando profundidad y caracteres.
    Lanza UnsafePathError si la ruta es sospechosa o está fuera de límites.
    """
    path_str = str(path)
    if any(ord(c) < 32 for c in path_str) or "\0" in path_str:
        raise UnsafePathError("Ruta con caracteres de control.")
    if len(path.parts) > 32:
        raise UnsafePathError("Profundidad de ruta excesiva.")
    
    if ":" in path.name:
        raise UnsafePathError("Ruta con flujos de datos alternos (ADS) prohibidos.")
    
    try:
        resolved = path.resolve(strict=True)
        if resolved.is_symlink():
            raise UnsafePathError("Operación denegada: enlace simbólico.")
        if hasattr(resolved, 'is_junction') and resolved.is_junction():
            raise UnsafePathError("Operación denegada: punto de reparse.")
    except (OSError, RuntimeError):
        pass


def _sanitize_filename(filename: str) -> str:
    """Filtra caracteres no alfanuméricos básicos para nombres de archivo seguros."""
    return "".join(c for c in filename if c.isalnum() or c in "._-")

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """Genera un nombre para el archivo dentro del sandbox, neutralizando caracteres."""
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
    """Valida que la UID del directorio coincida con el usuario actual en POSIX."""
    if hasattr(os, 'getuid'):
        if path.stat().st_uid != os.getuid():
            raise UnsafePathError("Propiedad de directorio no coincide con usuario.")

def quarantine_dir(base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Resuelve y prepara el directorio de cuarentena (sandbox).
    Verifica seguridad mediante `ensure_safe_to_modify` y prepara la estructura.
    Lanza OSError o UnsafePathError ante cualquier falla de integridad.
    """
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path = Path(base).expanduser().resolve()
        if not path.name.strip():
            raise UnsafePathError("Ruta de cuarentena inválida.")
        if is_protected_path(path):
            raise UnsafePathError("Directorio de cuarentena reside en ruta protegida.")
        
        ensure_safe_to_modify(path)
        
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise OSError(f"No se pudo crear el directorio de cuarentena: {e}")
            
        _ensure_path_ownership(path)
        return path
    except (OSError, RuntimeError, UnsafePathError) as e:
        raise OSError(f"Error al preparar directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ubicación absoluta del archivo de manifiesto JSON."""
    return (base_dir / MANIFEST_NAME).resolve()


def _is_within_quarantine_sandbox(path: Path, root: Path) -> bool:
    """Confirma que la ruta resida físicamente dentro del directorio sandbox."""
    return is_within_directory(path, root)

def _validate_quarantine_path(path: Path, base: Path) -> Path:
    """Valida que la ruta sea una sub-ruta legítima del sandbox. Lanza UnsafePathError si falla."""
    if not is_within_directory(path.resolve(), base.resolve()):
        raise UnsafePathError("Acceso fuera del sandbox detectado.")
    return path.resolve()

def _check_windows_file_attributes(path_str: str) -> None:
    """Detecta atributos ocultos o de sistema en Windows para evitar aislar archivos críticos."""
    if os.name != 'nt':
        return
    path_obj = Path(path_str)
    if not path_obj.exists():
        return
    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path_obj))
    if attrs != -1:
        if attrs & 0x02 or attrs & 0x04:
            raise UnsafePathError("Archivo con atributos del sistema/oculto no permitido.")

def _check_device_consistency(source: Path, target_dir: Path) -> None:
    """Verifica que el origen y destino estén en el mismo volumen (dispositivo)."""
    if source.stat().st_dev != target_dir.stat().st_dev:
        raise UnsafePathError("Operación entre distintos volúmenes no permitida.")

def _check_isolation_safety(source_path: Path, dest_dir: Path) -> None:
    """
    Verifica condiciones de seguridad origen-destino previo a la operación.
    Lanza excepciones de seguridad si el origen es un enlace o el destino no tiene permisos.
    """
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

    if resolved_source.parent == resolved_dest_dir:
        raise UnsafePathError("Operación circular detectada.")
    if is_protected_path(resolved_source):
        raise UnsafePathError("Ruta origen protegida.")
    if is_protected_path(resolved_dest_dir) or is_protected_path(resolved_dest_dir.parent):
        raise UnsafePathError("Destino en ruta protegida.")
    if _is_within_quarantine_sandbox(resolved_source, resolved_dest_dir):
        raise UnsafePathError("Archivo ya se encuentra en sandbox.")
        
    ensure_safe_to_modify(resolved_source, allow_sensitive=True)
    if _is_file_locked(resolved_source):
        raise IOError("Archivo en uso.")


def _validate_isolation_request(source_path: Path, dest_dir: Path) -> None:
    """Orquesta las verificaciones de integridad antes del movimiento."""
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


def load_manifest(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Carga el manifiesto de cuarentena y filtra ítems malformados. Retorna lista vacía si hay error."""
    try:
        base_dir = quarantine_dir(base)
        m_path = _manifest_path(base_dir)
        if not m_path.exists() or m_path.stat().st_size == 0:
            return []
        
        with open(m_path, "r", encoding="utf-8") as f:
            data = json.load(f)
                
        if not isinstance(data, list):
            return []
            
        results: List[QuarantineItem] = []
        for d in data:
            if not isinstance(d, dict):
                continue
            item = QuarantineItem.from_dict(d)
            if item:
                results.append(item)
        return results
    except (json.JSONDecodeError, OSError, PermissionError, UnsafePathError, ValueError):
        return []


def save_manifest(items: List[QuarantineItem], base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persiste el manifiesto usando escritura atómica. Garantiza que el archivo JSON esté completo."""
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
        with tempfile.NamedTemporaryFile("wb", dir=base_path, delete=False) as tf:
            temp_path = Path(tf.name)
            tf.write(encoded_content)
            tf.flush()
            os.fsync(tf.fileno())
            
        if temp_path and temp_path.exists() and temp_path.stat().st_size == len(encoded_content):
            os.replace(temp_path, target_path)
        else:
            raise OSError("Integridad del archivo temporal fallida.")
        
        dir_fd = os.open(str(base_path), os.O_RDONLY)
        try: 
            os.fsync(dir_fd)
        finally: 
            os.close(dir_fd)
        
        return target_path
    except (OSError, IOError) as e:
        raise RuntimeError(f"Error crítico al persistir manifiesto: {e}")
    finally:
        if temp_path and temp_path.exists():
            try: os.remove(temp_path)
            except OSError: pass


def _ensure_disk_space(dest_dir: Path, required_size: int) -> None:
    """Verifica disponibilidad de espacio y capacidad de escritura mediante un test temporal."""
    if not dest_dir.exists():
        raise FileNotFoundError(f"Directorio inexistente: {dest_dir}")
    if not os.access(dest_dir, os.W_OK):
        raise PermissionError(f"Sin permisos de escritura: {dest_dir}")
    
    test_file = dest_dir / f".test_{uuid.uuid4().hex}"
    try:
        test_file.touch()
        test_file.unlink()
    except OSError:
        raise OSError("Sistema de archivos del destino marcado como solo lectura.")

    usage = shutil.disk_usage(dest_dir)
    margin = max(int(required_size * 0.05), 5 * 1024 * 1024)
    if usage.free < (required_size + margin):
        raise OSError("Espacio insuficiente en disco.")


def _validate_file_transfer_preconditions(source: Path, destination: Path) -> None:
    """Valida permisos y seguridad del origen y destino antes de la transferencia."""
    if is_protected_path(destination):
        raise UnsafePathError("Destino en ruta protegida.")
    
    ensure_safe_to_modify(destination.parent)
    _check_device_consistency(source, destination.parent.resolve())
    _check_windows_file_attributes(str(destination))

    if not source.is_file():
        raise OSError("Archivo origen inaccesible para copia.")
    
    if os.name == 'nt':
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(source))
        if attrs != -1 and (attrs & 0x01):
            raise PermissionError("Archivo origen marcado como solo lectura.")

    if destination.exists():
        raise FileExistsError(f"El destino ya existe: {destination}")


def _copy_with_verification(source: Path, temp_dest: Path, source_hash: str) -> None:
    """Ejecuta la copia binaria y verifica la integridad del archivo resultante."""
    with open(source, "rb") as f_src:
        stat_src = os.fstat(f_src.fileno())
        if not (stat_src.st_mode & 0o100000):
            raise OSError("El archivo origen no es un archivo regular.")

        with open(temp_dest, "wb") as f_dst:
            shutil.copyfileobj(f_src, f_dst)
            f_dst.flush()
            os.fsync(f_dst.fileno())
            
    if temp_dest.stat().st_size != stat_src.st_size:
        raise OSError("Falla de integridad: tamaño mismatch tras copia.")
        
    final_hash = _get_sha256(temp_dest)
    if not final_hash or final_hash != source_hash:
        raise OSError("Falla crítica: el hash del archivo copiado no coincide.")


def _write_temp_to_final(source: Path, destination: Path) -> str:
    """Copia física segura al sandbox mediante un archivo temporal y reemplazo atómico."""
    _check_path_syntax_integrity(destination)
    _validate_file_transfer_preconditions(source, destination)

    if not source.is_file():
        raise FileNotFoundError("Archivo origen no encontrado o no es un archivo.")

    if destination.exists():
        raise FileExistsError("Colisión de ruta: el archivo destino ya existe.")

    ensure_safe_to_modify(destination.parent)

    source_hash = _get_sha256(source)
    temp_dest = destination.with_suffix(".tmp")
    
    try:
        _copy_with_verification(source, temp_dest, source_hash)
            
        os.replace(temp_dest, destination)
        ensure_safe_to_modify(destination, allow_sensitive=True)
        return source_hash
            
    except Exception as e:
        if temp_dest.exists():
            try: temp_dest.unlink()
            except OSError: pass
        if destination.exists():
            _safe_unlink(destination)
        raise OSError(f"Error crítico en transferencia: {e}")


def _atomic_isolate_file(source: Path, destination: Path, original_size: int) -> str:
    """Coordina el aislamiento seguro del archivo hacia el sandbox."""
    if not source.is_file():
        raise FileNotFoundError("Archivo origen inexistente o inválido.")
    
    if source.resolve() == destination.resolve():
        raise UnsafePathError("El origen ya reside en el directorio destino.")
        
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
    """Registra el ítem en el manifiesto JSON tras la confirmación de escritura."""
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
        )
        items_list.append(quarantine_item)
        save_manifest(items_list, base)
        return quarantine_item
    except Exception as e:
        if destination.exists():
            _safe_unlink(destination)
        raise RuntimeError(f"Falla al registrar ítem en manifiesto: {e}")


def _validate_source_for_quarantine(source: Path) -> Path:
    """Valida la elegibilidad del archivo origen para ser aislado."""
    if source.is_dir():
        raise UnsafePathError("Aislamiento de directorios no permitido.")
    if source.is_symlink():
        raise UnsafePathError("No se permite aislar enlaces simbólicos.")
    if not source.is_file():
        raise FileNotFoundError("Archivo origen inexistente.")
    if _is_file_locked(source):
        raise IOError("Archivo origen bloqueado por el sistema.")
    return source

def quarantine_file(
    source: PathLike,
    reason: str = "Marcado como sospechoso",
    base: PathLike = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """
    Ejecuta el flujo completo de aislamiento, integrando validación y persistencia.

    Args:
        source: Ruta del archivo a poner en cuarentena.
        reason: Motivo por el cual se aísla el archivo.
        base: Directorio base de cuarentena.

    Returns:
        El objeto QuarantineItem creado. Lanza RuntimeError en caso de falla crítica.
    """
    if source is None:
        raise ValueError("Ruta de origen nula o vacía.")
    
    p_source = Path(source)
    if not p_source.is_absolute():
        try:
            p_source = p_source.resolve(strict=True)
        except (OSError, RuntimeError) as e:
            raise UnsafePathError(f"Ruta origen no válida: {e}")
    
    source_path = _validate_source_for_quarantine(p_source)
    if not source_path.exists():
        raise FileNotFoundError("El archivo origen desapareció antes del aislamiento.")
        
    original_size = source_path.stat().st_size
    dest_dir = quarantine_dir(base)
    
    if _is_within_quarantine_sandbox(source_path, dest_dir.resolve()):
        raise UnsafePathError("Archivo ya en el sandbox.")

    _validate_isolation_request(source_path, dest_dir)
    
    destination = dest_dir / _generate_safe_stored_name(source_path, uuid.uuid4().hex[:12])
    
    try:
        file_hash = _atomic_isolate_file(source_path, destination, original_size)
        
        if not source_path.exists():
            raise RuntimeError("El archivo origen ha desaparecido inesperadamente.")
        
        item = _register_quarantine_item(destination, source_path, file_hash, reason, original_size, base)
        if not item.verify_integrity(destination):
            raise RuntimeError("Integridad post-registro fallida.")
        
        try:
            source_path.unlink()
        except OSError as e:
            raise RuntimeError(f"Archivo aislado, pero falló el borrado del origen: {e}")
            
        return item
    except Exception as e:
        if destination.exists():
            _safe_unlink(destination)
        raise RuntimeError(f"Error durante aislamiento: {e}")

def list_items(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna ítems validados y purga registros sin contraparte física en disco."""
    try:
        base_path = quarantine_dir(base)
        items = load_manifest(base)
        
        # Optimización: Set de nombres para búsqueda O(1) en el bucle
        actual_files = {f.name for f in base_path.iterdir() if f.is_file()}
        
        valid_items: List[QuarantineItem] = []
        for i in items:
            if i.stored_name in actual_files:
                f_path = base_path / i.stored_name
                if i._validate_integrity(f_path):
                    valid_items.append(i)
            
        if len(valid_items) != len(items):
            save_manifest(valid_items, base)
            
        return sorted(valid_items, key=lambda x: x.quarantined_at, reverse=True)
    except (OSError, UnsafePathError, PermissionError):
        return []


def restore_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Restaura un ítem al origen tras validar integridad y permisos de destino.
    Verifica que la restauración no sobreescriba rutas protegidas.

    Args:
        item_id: Identificador único del ítem en el manifiesto.
        base: Directorio base de cuarentena.

    Returns:
        La ruta original restaurada. Lanza RuntimeError si la restauración falla.
    """
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID de ítem inválido.")
    
    try:
        base_path = quarantine_dir(base)
        items = load_manifest(base)
        items_map = {i.item_id: i for i in items}
        quarantine_item = items_map.get(item_id)
        
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
    """Elimina permanentemente un ítem específico del sandbox. Verifica hash antes de borrar."""
    if not isinstance(item_id, str) or not item_id.strip():
        return False
        
    base_path = quarantine_dir(base)
    items = load_manifest(base)
    items_map = {i.item_id: i for i in items}
    quarantine_item = items_map.get(item_id)
    
    if quarantine_item is None:
        return False
        
    stored_file = base_path / quarantine_item.stored_name
    
    if not stored_file.exists():
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
        
    if not quarantine_item.verify_integrity(stored_file):
        raise UnsafePathError(f"Integridad fallida para {item_id}.")
        
    if _safe_unlink(stored_file, expected_hash=quarantine_item.sha256):
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
    return False


def _is_item_purgable(file_path: Path, item: QuarantineItem, base_path: Path) -> bool:
    """Verifica requisitos de seguridad antes de purgar un ítem del sandbox."""
    if not file_path.exists() or not file_path.is_file() or file_path.is_symlink() or is_protected_path(file_path):
        return False
    if not is_safe_to_modify(file_path):
        return False
    return (
        is_within_directory(file_path, base_path) and
        item.verify_integrity(file_path) and
        _safe_unlink(file_path, expected_hash=item.sha256)
    )


def purge_all(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Limpia todos los ítems validados del sandbox. Retorna cantidad eliminada."""
    try:
        quarantine_root = quarantine_dir(base)
    except (OSError, RuntimeError, UnsafePathError):
        return 0
        
    items = load_manifest(base)
    # Mapeo por nombre de archivo para O(1) en el bucle principal
    item_map = {i.stored_name: i for i in items}
    purged_ids: Set[str] = set()
    
    try:
        for f in quarantine_root.iterdir():
            if f.name == MANIFEST_NAME or not f.is_file():
                continue
            item = item_map.get(f.name)
            if item and _is_item_purgable(f, item, quarantine_root):
                purged_ids.add(item.item_id)
                
        if purged_ids:
            remaining_items = [i for i in items if i.item_id not in purged_ids]
            save_manifest(remaining_items, base)
            
    except (OSError, PermissionError):
        pass
        
    return len(purged_ids)


def total_quarantined_bytes(base: PathLike = DEFAULT_QUARANTINE_DIR, items: Optional[List[QuarantineItem]] = None) -> int:
    """Calcula el uso total de espacio ocupado por ítems en cuarentena; acepta lista precargada."""
    if items is None:
        items = load_manifest(base)
    return sum(item.size_bytes for item in items)


def summarize(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte legible para el usuario sobre los ítems en cuarentena."""
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
