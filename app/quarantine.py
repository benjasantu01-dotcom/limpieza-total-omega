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
from typing import List, Union, Dict, Any, TypeGuard, Optional

from safety import (
    UnsafePathError,
    ensure_safe_to_modify,
    is_safe_to_modify,
    is_protected_path,
    is_within_directory,
)

__all__ = [
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
]

DEFAULT_QUARANTINE_DIR: str = "~/LimpiezaTotalOmega/_Cuarentena"
MANIFEST_NAME: str = "manifest.json"

WINDOWS_RESERVED_NAMES: set[str] = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", 
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", 
    "LPT6", "LPT7", "LPT8", "LPT9"
}

@dataclass
class QuarantineItem:
    """Representa un archivo aislado con metadatos para asegurar la trazabilidad y reversibilidad."""
    item_id: str
    original_path: str
    stored_name: str
    size_bytes: int
    reason: str
    quarantined_at: str
    sha256: str = ""

    def __post_init__(self) -> None:
        """Normaliza tipos y asegura integridad mínima de los metadatos."""
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
        """Retorna el peso del ítem en MB redondeado para visualización."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la instancia a diccionario plano para persistencia JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional[QuarantineItem]:
        """Crea instancia a partir de datos serializados, validando integridad del esquema."""
        if not isinstance(data, dict):
            return None
        required = {"item_id", "original_path", "stored_name", "size_bytes", "reason", "quarantined_at"}
        if not required.issubset(data.keys()):
            return None
        try:
            return cls(
                item_id=str(data["item_id"]),
                original_path=str(data["original_path"]),
                stored_name=str(data["stored_name"]),
                size_bytes=int(data["size_bytes"]),
                reason=str(data["reason"]),
                quarantined_at=str(data["quarantined_at"]),
                sha256=str(data.get("sha256", ""))
            )
        except (ValueError, TypeError):
            return None

    def _validate_integrity(self, stored_path: Path) -> bool:
        """Verifica existencia, naturaleza de archivo y consistencia de tamaño en disco."""
        try:
            if not stored_path.is_file() or stored_path.is_symlink():
                return False
            return stored_path.stat().st_size == self.size_bytes
        except (OSError, PermissionError):
            return False

    def verify_integrity(self, stored_path: Path) -> bool:
        """Verifica que el hash del archivo actual coincida con el registrado durante la cuarentena."""
        if not stored_path or not self._validate_integrity(stored_path):
            return False
        try:
            actual_hash = _get_sha256(stored_path)
            return bool(self.sha256 and actual_hash == self.sha256)
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """Calcula hash SHA-256 mediante streaming de bloques para optimizar el uso de memoria."""
    sha256_hash = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(131072):
                sha256_hash.update(chunk)
    except (OSError, PermissionError):
        return ""
    return sha256_hash.hexdigest()


def _is_file_locked(path: Path) -> bool:
    """Verifica exclusividad de acceso al archivo intentando abrirlo en modo lectura."""
    if not isinstance(path, Path) or not path.exists():
        return False
    try:
        with open(path, "rb") as f:
            return False
    except (PermissionError, IOError):
        return True
    except OSError:
        return False


def _safe_unlink(path: Path) -> bool:
    """Elimina archivo de forma segura, validando previamente que no se trate de una ruta protegida."""
    try:
        if path.exists() and path.is_file() and not path.is_symlink() and is_safe_to_modify(path):
            path.unlink()
            return True
        return False
    except (OSError, PermissionError):
        return False

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """Genera alias seguro para el sandbox evitando caracteres especiales, nombres reservados y colisiones."""
    safe_name_chars = "".join(c for c in original_path.name if c.isalnum() or c in "._-")
    parts = safe_name_chars.split('.')
    name_base = parts[0] if parts[0] else "q_file"
    if name_base.upper() in WINDOWS_RESERVED_NAMES:
        name_base = f"q_{name_base}"
    extension = f".{parts[-1]}" if len(parts) > 1 else ""
    return f"{item_id}__{name_base[:64]}{extension}"[:128]


def quarantine_dir(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Obtiene y prepara el directorio de cuarentena, validando contra políticas de seguridad."""
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path = Path(base).expanduser().resolve()
        if not path.name.strip():
            raise UnsafePathError("Ruta de cuarentena inválida o vacía.")
        if is_protected_path(path):
            raise UnsafePathError("Directorio de cuarentena reside en ruta protegida.")
        if not is_safe_to_modify(path):
            raise UnsafePathError("Directorio de cuarentena no cumple políticas de seguridad.")
        path.mkdir(parents=True, exist_ok=True)
        return path
    except (OSError, RuntimeError) as e:
        raise OSError(f"No se pudo preparar el directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ubicación absoluta del archivo de manifiesto en el sandbox."""
    return (base_dir / MANIFEST_NAME).resolve()


def _is_within_quarantine_sandbox(path: Path, root: Path) -> bool:
    """Verifica que la ruta esté contenida estrictamente bajo el sandbox de cuarentena."""
    return is_within_directory(path, root)


def _check_windows_file_attributes(path_str: str) -> None:
    """Detecta banderas de sistema, oculto o solo lectura mediante API de Windows."""
    if os.name != 'nt':
        return
    import ctypes
    attrs = ctypes.windll.kernel32.GetFileAttributesW(path_str)
    if attrs != -1:
        if attrs & 0x02 or attrs & 0x04:
            raise UnsafePathError("No se permite procesar archivos con atributos de sistema/ocultos.")
        if attrs & 0x01:
            raise UnsafePathError("Archivo protegido contra escritura (solo lectura).")


def _check_path_syntax_integrity(path: Path) -> None:
    """Validación sintáctica profunda de rutas para prevenir ataques de path-traversal."""
    path_str = str(path)
    if any(ord(c) < 32 for c in path_str) or "\0" in path_str:
        raise UnsafePathError("Ruta con caracteres de control prohibida.")
    if len(path.parts) > 32:
        raise UnsafePathError("Profundidad de ruta excesiva.")
    if ":" in path.name.replace(path.drive, "") or ":" in str(path.parent):
        raise UnsafePathError("Ruta con flujos de datos alternos no permitida.")
    if ".." in path.parts or any(c in str(path.name) for c in "<>\"|?*"):
        raise UnsafePathError("Ruta con caracteres prohibidos o navegación no permitida.")
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise UnsafePathError("Operación denegada en enlace o punto de reparse.")


def _validate_isolation_request(source_path: Path, dest_dir: Path) -> None:
    """Valida todos los requisitos de seguridad antes de mover un archivo al sandbox."""
    _check_path_syntax_integrity(source_path)
    _check_windows_file_attributes(str(source_path))
    resolved_source = source_path.resolve()
    if not resolved_source.is_file():
        raise UnsafePathError("Solo se aceptan archivos regulares para aislamiento.")
    if resolved_source.parent == dest_dir.resolve():
        raise UnsafePathError("Operación circular: origen y destino en la misma carpeta.")
    if is_protected_path(resolved_source):
        raise UnsafePathError("Operación prohibida: la ruta origen está protegida por el sistema.")
    if is_protected_path(dest_dir) or is_protected_path(dest_dir.parent):
        raise UnsafePathError("Destino inválido: directorio de cuarentena en ruta protegida.")
    if _is_within_quarantine_sandbox(resolved_source, dest_dir):
        raise UnsafePathError("El archivo ya reside en el sandbox de cuarentena.")
    if resolved_source.drive.lower() != dest_dir.drive.lower():
        raise UnsafePathError("Operación prohibida: origen y destino en dispositivos diferentes.")
    ensure_safe_to_modify(resolved_source, allow_sensitive=True)
    if _is_file_locked(resolved_source):
        raise IOError("El archivo está en uso por otro proceso y no puede moverse.")

@lru_cache(maxsize=2)
def _load_manifest_internal(base_str: str) -> Dict[str, QuarantineItem]:
    """Carga y deserealiza el manifiesto desde el disco, utilizando caché LRU."""
    path = _manifest_path(Path(base_str))
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list):
                return {}
            
            items: Dict[str, QuarantineItem] = {}
            for entry in data:
                if isinstance(entry, dict):
                    item = QuarantineItem.from_dict(entry)
                    if item:
                        items[item.item_id] = item
            return items
    except (json.JSONDecodeError, OSError, PermissionError):
        return {}

def load_manifest(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Interfaz pública para obtener la lista de ítems en cuarentena."""
    base_path = quarantine_dir(base)
    if force_reload:
        _load_manifest_internal.cache_clear()
    
    # Optimización: Check de existencia rápido antes de procesar caché o disco
    if not _manifest_path(base_path).exists():
        return []
        
    return list(_load_manifest_internal(str(base_path)).values())


def save_manifest(items: List[QuarantineItem], base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Escribe el manifiesto de forma atómica mediante escritura a archivo temporal."""
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista de ítems.")
    base_path = quarantine_dir(base)
    target_path = _manifest_path(base_path)
    try:
        with tempfile.NamedTemporaryFile("w", dir=base_path, encoding="utf-8", delete=False) as tf:
            temp_name = tf.name
            json.dump([item.to_dict() for item in items], tf, indent=2, ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(temp_name, target_path)
        _load_manifest_internal.cache_clear()
        return target_path
    except (OSError, TypeError, IOError) as e:
        if 'temp_name' in locals() and os.path.exists(temp_name):
            try: os.remove(temp_name)
            except OSError: pass
        raise RuntimeError(f"Fallo crítico al persistir manifiesto: {e}")


def _atomic_isolate_file(source: Path, destination: Path, file_size: int) -> str:
    """Copia archivo al sandbox garantizando integridad con hash post-copia."""
    resolved_source = source.resolve()
    dest_dir = destination.parent.resolve()
    try:
        usage = shutil.disk_usage(dest_dir)
        if usage.free < (file_size + (1024 * 1024)):
            raise OSError("Espacio insuficiente en disco para aislamiento seguro.")
    except OSError as e:
        raise OSError(f"No se pudo determinar el espacio libre en disco: {e}")

    temp_file_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=dest_dir, prefix=".tmp_q_", delete=False) as tf:
            temp_file_path = Path(tf.name)
        shutil.copy2(resolved_source, temp_file_path)
        
        # Validación de integridad post-escritura antes de confirmar
        if not temp_file_path.exists() or temp_file_path.stat().st_size != file_size:
            raise OSError("Falla de integridad: archivo copiado no coincide en tamaño.")
            
        os.replace(temp_file_path, destination)
        file_hash = _get_sha256(destination)
        if not file_hash:
            raise OSError("Falla de integridad: no se pudo verificar el hash en destino.")
        return file_hash
    except Exception:
        if temp_file_path and temp_file_path.exists():
            _safe_unlink(temp_file_path)
        raise


def quarantine_file(
    source: Union[str, Path],
    reason: str = "Marcado como sospechoso",
    base: Union[str, Path] = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """Realiza el proceso completo de aislamiento y registro de un archivo."""
    if not source:
        raise ValueError("La ruta de origen no puede estar vacía.")
    
    try:
        source_path = Path(source).expanduser().resolve()
        source_stat = source_path.stat()
    except (OSError, AttributeError) as e:
        raise FileNotFoundError(f"Archivo origen inaccesible: {e}")

    if not source_stat.st_mode & 0o100000: # S_ISREG
        raise UnsafePathError("Solo se aceptan archivos regulares para aislamiento.")
    
    ensure_safe_to_modify(source_path, allow_sensitive=True)
    dest_dir = quarantine_dir(base)
    _validate_isolation_request(source_path, dest_dir)
    
    file_size = source_stat.st_size
    item_id = uuid.uuid4().hex[:12]
    destination = dest_dir / _generate_safe_stored_name(source_path, item_id)
    
    if destination.exists():
        raise FileExistsError(f"Colisión de nombre en el sandbox: {destination.name}")
        
    file_hash = _atomic_isolate_file(source_path, destination, file_size)
    
    try:
        items_dict = _load_manifest_internal(str(dest_dir))
        quarantine_item = QuarantineItem(
            item_id=item_id,
            original_path=str(source_path),
            stored_name=destination.name,
            size_bytes=file_size,
            reason=str(reason) if reason else "Sin motivo",
            quarantined_at=datetime.now().isoformat(timespec="seconds"),
            sha256=file_hash,
        )
        items_dict[item_id] = quarantine_item
        save_manifest(list(items_dict.values()), base)
        
        try:
            source_path.unlink()
        except OSError as e:
            raise RuntimeError(f"Aislamiento exitoso, pero el origen no pudo eliminarse: {e}")
        return quarantine_item
    except Exception:
        if destination.exists():
            _safe_unlink(destination)
        raise


def list_items(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna lista de archivos en cuarentena ordenados cronológicamente (más recientes primero)."""
    return sorted(load_manifest(base), key=lambda item: item.quarantined_at, reverse=True)


def restore_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un archivo desde la cuarentena a su ubicación original validando seguridad."""
    if not item_id or not isinstance(item_id, str):
        raise ValueError("ID de ítem inválido o vacío.")
    base_path = quarantine_dir(base)
    items_dict = _load_manifest_internal(str(base_path))
    quarantine_item = items_dict.get(item_id)
    if not quarantine_item:
        raise KeyError(f"No se encontró el ítem: {item_id}")
    stored_file = (base_path / quarantine_item.stored_name).resolve()
    if not stored_file.exists():
        raise FileNotFoundError(f"Archivo en cuarentena {quarantine_item.stored_name} no hallado.")
    if not quarantine_item.verify_integrity(stored_file):
        raise RuntimeError("Integridad comprometida: el hash no coincide con el registro.")
    if _is_file_locked(stored_file):
        raise IOError("El archivo en cuarentena está en uso y no puede restaurarse.")
    
    destination = Path(quarantine_item.original_path).absolute()
    _check_path_syntax_integrity(destination)
    if is_protected_path(destination):
        raise UnsafePathError("Restauración denegada: destino protegido por sistema.")
    if destination.exists():
        raise FileExistsError(f"Error: el destino {destination} ya existe en el sistema.")
    
    parent = destination.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    if not is_safe_to_modify(parent) or not is_safe_to_modify(destination):
        raise UnsafePathError("Restauración denegada: destino restringido o protegido.")
        
    try:
        os.replace(str(stored_file), str(destination))
        if destination.is_symlink():
            destination.unlink()
            raise UnsafePathError("Restauración denegada: el destino es un enlace simbólico.")
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Fallo crítico durante la restauración: {e}")
        
    del items_dict[item_id]
    save_manifest(list(items_dict.values()), base)
    return destination


def purge_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> bool:
    """Elimina permanentemente un archivo específico del sandbox."""
    if not isinstance(item_id, str) or not item_id.strip():
        return False
    base_path = quarantine_dir(base)
    items_dict = _load_manifest_internal(str(base_path))
    quarantine_item = items_dict.get(item_id)
    if not quarantine_item:
        return False
    stored_file = (base_path / quarantine_item.stored_name).resolve()
    if not stored_file.exists():
        items_dict.pop(item_id, None)
        save_manifest(list(items_dict.values()), base)
        return False
    if not quarantine_item.verify_integrity(stored_file):
        raise UnsafePathError(f"Integridad comprometida para ítem {item_id}: no se puede purgar.")
    if not _is_within_quarantine_sandbox(stored_file, base_path):
        raise UnsafePathError("Intento de borrado fuera del sandbox.")
    if _safe_unlink(stored_file):
        items_dict.pop(item_id, None)
        save_manifest(list(items_dict.values()), base)
        return True
    return False


def _is_item_purgable(file_path: Path, item: QuarantineItem, base_path: Path) -> bool:
    """Comprueba si un ítem puede ser purgado con seguridad."""
    try:
        return (
            file_path.exists() and
            file_path.is_file() and
            _is_within_quarantine_sandbox(file_path, base_path) and
            item.verify_integrity(file_path) and
            not _is_file_locked(file_path)
        )
    except (OSError, PermissionError):
        return False


def purge_all(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Limpia todo el sandbox de archivos purgables de una sola vez."""
    quarantine_root = quarantine_dir(base)
    items_dict = _load_manifest_internal(str(quarantine_root))
    if not items_dict:
        return 0
    
    purged_count = 0
    remaining_keys = list(items_dict.keys())
    for item_id in remaining_keys:
        item = items_dict[item_id]
        try:
            stored_path = (quarantine_root / item.stored_name).resolve()
            if not stored_path.exists():
                del items_dict[item_id]
                continue
            
            if _is_item_purgable(stored_path, item, quarantine_root):
                if _safe_unlink(stored_path):
                    del items_dict[item_id]
                    purged_count += 1
        except (OSError, PermissionError):
            continue
            
    if purged_count > 0:
        save_manifest(list(items_dict.values()), base)
    return purged_count


def total_quarantined_bytes(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el peso total ocupado por los archivos en cuarentena."""
    manifest = load_manifest(base)
    return sum(item.size_bytes for item in manifest)


def summarize(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte legible de los ítems en cuarentena para la interfaz de usuario."""
    items = load_manifest(base)
    if not items:
        return ["La cuarentena está vacía."]
    
    items = sorted(items, key=lambda i: i.quarantined_at, reverse=True)
    total_mb = sum(item.size_mb for item in items)
    
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
