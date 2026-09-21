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
        """Verificación básica de existencia, tipo y tamaño en disco."""
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
        Verificación profunda mediante comparación de hash SHA-256.

        Args:
            stored_path: Ruta física del archivo en el sandbox.

        Returns:
            True si el hash coincide con el registrado originalmente.
        """
        if not self._validate_integrity(stored_path):
            return False
        try:
            return bool(self.sha256 and _get_sha256(stored_path) == self.sha256)
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """
    Calcula el hash SHA-256 de un archivo mediante streaming para optimizar RAM.
    
    Args:
        path: Ruta del archivo a procesar.
    Returns:
        String con el hash hexadecimal o cadena vacía si falla la lectura.
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
    Verifica si un archivo está bloqueado por el S.O.
    Intenta abrir el archivo para lectura exclusiva evitando seguir symlinks.
    """
    if not path.exists():
        return False
    try:
        # Usamos flags de bajo nivel para asegurar comportamiento predecible
        flags = os.O_RDONLY | os.O_EXCL
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        os.close(fd)
        return False
    except (OSError, PermissionError):
        return True

def _safe_unlink(path: Path) -> bool:
    """
    Elimina un archivo tras validar políticas de seguridad y ausencia de bloqueos.
    Utiliza `is_safe_to_modify` para asegurar que el path no sea de sistema.
    """
    try:
        resolved = path.resolve()
        if not resolved.exists() or not resolved.is_file() or resolved.is_symlink() or is_protected_path(resolved):
            return False
            
        if is_safe_to_modify(resolved) and not _is_file_locked(resolved):
            path.unlink()
            return True
        return False
    except (OSError, PermissionError):
        return False

def _check_path_syntax_integrity(path: Path) -> None:
    """
    Valida sintaxis, profundidad y naturaleza del objeto para prevenir Path Traversal.
    Impide acceso a flujos de datos alternos (ADS) o paths excesivamente profundos.
    """
    path_str = str(path)
    if any(ord(c) < 32 for c in path_str) or "\0" in path_str:
        raise UnsafePathError("Ruta con caracteres de control.")
    if len(path.parts) > 32:
        raise UnsafePathError("Profundidad de ruta excesiva.")
    
    if ":" in path.name.replace(path.drive, "") or any(c in path_str for c in ("\0", "\x00")):
        raise UnsafePathError("Ruta con flujos de datos alternos (ADS) o caracteres prohibidos.")
    
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
    """Crea un nombre de archivo único prefijado para aislar el ítem en el sandbox."""
    sanitized = _sanitize_filename(original_path.name)
    if not sanitized or sanitized in (".", ".."):
        sanitized = "unknown_file"
        
    parts = sanitized.split('.')
    name_base = parts[0] if parts[0] else "q_file"
    if name_base.upper() in WINDOWS_RESERVED_NAMES:
        name_base = f"q_{name_base}"
    
    name_base = "".join(c for c in name_base if ord(c) >= 32)
    
    extension = f".{parts[-1]}" if len(parts) > 1 else ""
    candidate = f"{item_id}__{name_base[:64]}{extension}"[:128]
    return candidate

def _ensure_path_ownership(path: Path) -> None:
    """Valida que la UID del directorio coincida con el usuario actual en POSIX."""
    if hasattr(os, 'getuid'):
        if path.stat().st_uid != os.getuid():
            raise UnsafePathError("Propiedad de directorio no coincide con usuario.")

def quarantine_dir(base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Resuelve la ruta absoluta del sandbox y asegura las políticas de seguridad mínimas.
    
    Args:
        base: Ruta base donde se alojará el directorio de cuarentena.
    Returns:
        Objeto Path absoluto y validado del sandbox.
    """
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
        
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise OSError(f"No se pudo crear el directorio de cuarentena: {e}")
            
        _ensure_path_ownership(path)
        return path
    except (OSError, RuntimeError) as e:
        raise OSError(f"Error al preparar directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ubicación absoluta del archivo de manifiesto JSON."""
    return (base_dir / MANIFEST_NAME).resolve()


def _is_within_quarantine_sandbox(path: Path, root: Path) -> bool:
    """Confirma que la ruta resida físicamente dentro del directorio sandbox."""
    return is_within_directory(path, root)

def _validate_quarantine_path(path: Path, base: Path) -> Path:
    """Valida que la ruta sea una sub-ruta legítima del sandbox."""
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


def _check_isolation_safety(source_path: Path, dest_dir: Path) -> None:
    """
    Verifica condiciones de seguridad origen-destino previo a la operación.
    Asegura que el origen y destino sean válidos y que el destino permita escritura.
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
        # Prevenir movimientos entre diferentes sistemas de archivos (Device ID check)
        if resolved_source.stat().st_dev != resolved_dest_dir.stat().st_dev:
            raise UnsafePathError("Operación entre dispositivos no permitida.")
            
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
    
    # Validar longitud de ruta destino (previene errores de path demasiado largo)
    if len(str(dest_dir)) > 240:
        raise UnsafePathError("Ruta de cuarentena demasiado larga.")

    _ensure_disk_space(dest_dir, resolved_source.stat().st_size)
    _check_isolation_safety(resolved_source, dest_dir)


def load_manifest(base: PathLike = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Carga y deserializa el manifiesto, filtrando ítems malformados."""
    try:
        m_path = _manifest_path(quarantine_dir(base))
        if not m_path.is_file():
            return []
        
        # Lectura con manejo de errores para archivos vacíos o bloqueados
        try:
            with open(m_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
                
        if not isinstance(data, list):
            return []
            
        results: List[QuarantineItem] = []
        for d in data:
            if isinstance(d, dict):
                item = QuarantineItem.from_dict(d)
                if item:
                    results.append(item)
        return results
    except (OSError, PermissionError, UnsafePathError):
        return []


def save_manifest(items: List[QuarantineItem], base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persiste el manifiesto usando escritura atómica y validaciones de integridad."""
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
        
        # Sincronizar directorio para garantizar persistencia en metadata del FS
        dir_fd = os.open(str(base_path), os.O_RDONLY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
        
        return target_path
    except (OSError, IOError) as e:
        if temp_path and temp_path.exists():
            try: os.remove(temp_path)
            except OSError: pass
        raise RuntimeError(f"Error crítico al persistir manifiesto: {e}")


def _ensure_disk_space(dest_dir: Path, required_size: int) -> None:
    """Verifica disponibilidad de espacio en disco con un margen de seguridad."""
    if not dest_dir.exists():
        raise FileNotFoundError(f"Directorio inexistente: {dest_dir}")
    if not os.access(dest_dir, os.W_OK):
        raise PermissionError(f"Sin permisos de escritura: {dest_dir}")
    usage = shutil.disk_usage(dest_dir)
    margin = max(int(required_size * 0.05), 5 * 1024 * 1024)
    if usage.free < (required_size + margin):
        raise OSError("Espacio insuficiente en disco.")


def _validate_file_transfer_preconditions(source: Path, destination: Path) -> None:
    """Valida los permisos y seguridad de los paths antes de la transferencia física."""
    if is_protected_path(destination):
        raise UnsafePathError("Destino en ruta protegida.")
    if not is_safe_to_modify(destination.parent):
        raise UnsafePathError("Directorio destino no es seguro para escritura.")
    
    # Validar que origen y destino estén en el mismo dispositivo físico
    if source.stat().st_dev != destination.parent.resolve().stat().st_dev:
        raise UnsafePathError("Operación entre dispositivos no permitida.")
    
    ensure_safe_to_modify(destination.parent, allow_sensitive=True)
    _check_windows_file_attributes(str(destination))

    if not source.is_file():
        raise OSError("Archivo origen inaccesible para copia.")
    
    # Bloqueo adicional: detectar si el origen es de solo lectura (Windows)
    if os.name == 'nt':
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(source))
        if attrs != -1 and (attrs & 0x01):
            raise PermissionError("Archivo origen marcado como solo lectura.")

    if destination.exists():
        raise FileExistsError(f"El destino ya existe: {destination}")


def _write_temp_to_final(source: Path, destination: Path) -> str:
    """
    Realiza una copia física segura del archivo origen al sandbox usando 
    descriptores de archivo para evitar condiciones de carrera o bloqueos.
    """
    _check_path_syntax_integrity(destination)
    _validate_file_transfer_preconditions(source, destination)

    # Capturar estado inicial para detectar cambios en vuelo
    try:
        st_initial = source.stat()
    except OSError:
        raise OSError("No se pudo obtener estado del archivo origen.")

    source_hash = _get_sha256(source)

    fd_src: int = os.open(str(source), os.O_RDONLY)
    try:
        stat_src = os.fstat(fd_src)
        # Redundancia: asegurar que no cambió desde la última validación
        if stat_src.st_size != st_initial.st_size or stat_src.st_mtime != st_initial.st_mtime:
            raise OSError("El archivo cambió durante el proceso de aislamiento.")

        if not (stat_src.st_mode & 0o100000): 
            raise OSError("El archivo origen no es un archivo regular.")
            
        # TOCTOU Protection: abrir con flags O_EXCL para asegurar creación nueva,
        # luego verificar de nuevo que el destino no es un enlace simbólico creado maliciosamente.
        flags: int = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        mode: int = 0o600
        fd_dest: int = os.open(str(destination), flags, mode)
        try:
            # Validar nuevamente que no es un symlink (TOCTOU protection)
            if destination.is_symlink():
                raise UnsafePathError("Intento de ataque symlink detectado durante la escritura.")
                
            with os.fdopen(fd_src, 'rb') as src_file, os.fdopen(fd_dest, 'wb') as dst_file:
                shutil.copyfileobj(src_file, dst_file)
                dst_file.flush()
                os.fsync(dst_file.fileno())
            
            # Post-escritura: asegurar que el tamaño en disco es consistente
            if destination.stat().st_size != stat_src.st_size:
                raise OSError("Error de integridad post-escritura (tamaño mismatch).")
        except Exception as e:
            if destination.exists():
                try: os.remove(destination)
                except OSError: pass
            raise e
            
    except Exception as e:
        # fd_src ya está gestionado por el try/except, el descriptor de fd_dest es cerrado por os.fdopen/bloque try
        if 'fd_src' in locals(): os.close(fd_src)
        raise e
        
    final_hash = _get_sha256(destination)
    if not final_hash or final_hash != source_hash:
        if destination.exists():
            _safe_unlink(destination)
        raise OSError("Falla crítica: el hash del archivo copiado no coincide con el original.")
    
    # Finalizar: asegurar que el directorio padre reconoce la creación
    ensure_safe_to_modify(destination, allow_sensitive=True)
    dir_fd: int = os.open(str(destination.parent), os.O_RDONLY)
    try: os.fsync(dir_fd)
    finally: os.close(dir_fd)
    
    return final_hash


def _atomic_isolate_file(source: Path, destination: Path, original_size: int) -> str:
    """
    Coordina el aislamiento seguro del archivo hacia el sandbox mediante 
    validaciones de ruta previas a la escritura final.
    """
    if not source.exists():
        raise FileNotFoundError("Archivo origen inexistente.")
    
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
    # Validar bloqueo antes de proceder
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
        
        try:
            existing = {f.name for f in base_path.iterdir() if f.is_file()}
        except OSError:
            existing = set()
        
        valid_items: List[QuarantineItem] = []
        needs_save = False
        
        for i in items:
            if i.stored_name in existing and i._validate_integrity(base_path / i.stored_name):
                valid_items.append(i)
            else:
                needs_save = True
            
        if needs_save:
            save_manifest(valid_items, base)
            
        return sorted(valid_items, key=lambda x: x.quarantined_at, reverse=True)
    except (OSError, UnsafePathError, PermissionError):
        return []


def restore_item(item_id: str, base: PathLike = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Restaura un ítem al origen tras validar integridad y permisos de destino.
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
        
        # Validación de dispositivos para evitar cruce de FS
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
    """Elimina permanentemente un ítem específico del sandbox."""
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
        
    if _safe_unlink(stored_file):
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
    return False


def _is_item_purgable(file_path: Path, item: QuarantineItem, base_path: Path) -> bool:
    """
    Verifica requisitos de seguridad antes de purgar un ítem del sandbox.
    """
    if not file_path.exists() or not file_path.is_file() or file_path.is_symlink() or is_protected_path(file_path):
        return False
    return (
        is_within_directory(file_path, base_path) and
        item.verify_integrity(file_path) and
        _safe_unlink(file_path)
    )


def purge_all(base: PathLike = DEFAULT_QUARANTINE_DIR) -> int:
    """Limpia todos los ítems validados del sandbox de forma masiva."""
    try:
        quarantine_root = quarantine_dir(base)
    except (OSError, RuntimeError, UnsafePathError):
        return 0
        
    items = load_manifest(base)
    # Indexamos por nombre de archivo para evitar búsquedas O(N) en el loop
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
    """Calcula el uso total de espacio ocupado por ítems en cuarentena."""
    return sum(item.size_bytes for item in load_manifest(base))


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
