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
    pueda escribir en una ruta de sistema.
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
from typing import List, Union, Dict, Tuple, Optional, Any, TypeGuard

from safety import (
    UnsafePathError,
    ensure_safe_to_modify,
    is_protected_path,
    is_within_directory,
    normalize,
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

DEFAULT_QUARANTINE_DIR = "~/LimpiezaTotalOmega/_Cuarentena"
MANIFEST_NAME = "manifest.json"

# Nombres reservados en sistemas Windows que podrían causar colisiones o fallos de acceso
WINDOWS_RESERVED_NAMES = {
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
        try:
            self.size_bytes = int(self.size_bytes)
        except (ValueError, TypeError):
            self.size_bytes = 0
        if not self.item_id:
            raise ValueError("ID de ítem vacío o inválido")

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional[QuarantineItem]:
        """
        Crea una instancia de QuarantineItem desde un diccionario, validando la presencia 
        de campos obligatorios. Retorna None si el formato es inválido.
        """
        required = {"item_id", "original_path", "stored_name", "size_bytes", "reason", "quarantined_at"}
        if not isinstance(data, dict) or not required.issubset(data.keys()):
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

    def verify_integrity(self, stored_path: Path) -> bool:
        """
        Verifica que el archivo actual en disco coincida con el hash y tamaño registrados.
        Requiere que 'stored_path' sea una ruta absoluta y existente.
        """
        if not stored_path or not stored_path.is_file() or stored_path.is_symlink():
            return False
        try:
            stats = stored_path.stat()
            if stats.st_size != self.size_bytes:
                return False
            actual_hash = _get_sha256(stored_path)
            if self.sha256 and actual_hash != self.sha256:
                return False
            return actual_hash != ""
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """Calcula el hash SHA256 de un archivo en bloques de 64KB para optimizar memoria."""
    sha256_hash = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                sha256_hash.update(chunk)
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Error al leer archivo para hash: {e}")
    return sha256_hash.hexdigest()


def _is_file_locked(path: Path) -> bool:
    """
    Intenta abrir el archivo en modo lectura/escritura (rb+) para detectar bloqueos.
    Retorna True si el archivo está en uso por otro proceso (Windows).
    """
    try:
        with open(path, "rb+") as f:
            return False
    except (OSError, PermissionError):
        return True


def _safe_unlink(path: Path) -> bool:
    """
    Elimina un archivo tras verificar que es regular y no un enlace simbólico.
    Es una operación destructiva; solo debe llamarse desde funciones de purga.
    """
    try:
        if path.is_file() and not path.is_symlink():
            path.unlink()
            return True
        return False
    except (OSError, PermissionError):
        return False

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """Crea un nombre de archivo seguro eliminando caracteres no alfanuméricos y evitando nombres reservados de sistema."""
    safe_chars = "".join(c for c in original_path.name if c.isalnum() or c in "._-")
    parts = safe_chars.split('.')
    name_base = parts[0] if parts[0] else "q_file"
    if name_base.upper() in WINDOWS_RESERVED_NAMES:
        name_base = f"q_{name_base}"
    safe_name = f"{name_base}.{parts[-1]}" if len(parts) > 1 else name_base
    return f"{item_id}__{safe_name}"[:250]


def quarantine_dir(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Resuelve la ruta absoluta al directorio de cuarentena asegurando que exista."""
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path = Path(base).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    except (OSError, RuntimeError) as e:
        raise OSError(f"No se pudo preparar el directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ruta absoluta del manifiesto dentro de un directorio de cuarentena."""
    return (base_dir / MANIFEST_NAME).resolve()


def _is_valid_quarantine_path(path: Path, root: Path) -> TypeGuard[Path]:
    """Valida que una ruta resida estrictamente dentro de la carpeta de cuarentena."""
    return is_within_directory(path, root)


def _check_windows_file_attributes(path_str: str) -> None:
    """Bloquea archivos con atributos de sistema o solo lectura para prevenir manipulación indeseada."""
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
    """Valida la sintaxis de la ruta para prevenir ataques de path traversal o uso de flujos alternos."""
    path_str = str(path)
    if any(ord(c) < 32 for c in path_str):
        raise UnsafePathError("Ruta con caracteres de control prohibida.")
    if len(path.parts) > 32:
        raise UnsafePathError("Profundidad de ruta excesiva: riesgo de desbordamiento.")
    if ":" in path.name.replace(path.drive, ""):
        raise UnsafePathError("Ruta con flujos de datos alternos no permitida.")
    if ".." in path.parts or any(c in str(path.name) for c in "<>\"|?*"):
        raise UnsafePathError("Ruta con caracteres prohibidos o navegación no permitida.")
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise UnsafePathError("Operación denegada en enlace o punto de reparse.")


def _validate_isolation_request(source_path: Path, dest_dir: Path) -> None:
    """
    Ejecuta chequeos de seguridad antes de mover un archivo al sandbox.
    Verifica sintaxis, atributos de archivo y protege contra colisiones/traversal.
    """
    _check_path_syntax_integrity(source_path)
    _check_windows_file_attributes(str(source_path))

    resolved_source = source_path.resolve()
    if not resolved_source.is_file():
        raise UnsafePathError("Solo se aceptan archivos regulares.")
    
    if is_protected_path(resolved_source):
        raise UnsafePathError("Operación prohibida: la ruta está protegida por el sistema.")
    if is_protected_path(dest_dir):
        raise UnsafePathError("Destino inválido: directorio de cuarentena protegido.")
    if _is_valid_quarantine_path(resolved_source, dest_dir):
        raise UnsafePathError("El archivo ya reside en el sandbox de cuarentena.")
    
    try:
        if resolved_source.drive and dest_dir.drive and resolved_source.drive.lower() != dest_dir.drive.lower():
            raise UnsafePathError("Operación prohibida entre dispositivos distintos.")
    except OSError:
        pass

    ensure_safe_to_modify(resolved_source, allow_sensitive=True)
    if _is_file_locked(resolved_source):
        raise IOError("El archivo está en uso por otro proceso y no puede moverse.")

@lru_cache(maxsize=1)
def _load_manifest_internal(base_str: str) -> List[QuarantineItem]:
    """Carga el manifiesto desde el disco, validando estructura y tipos."""
    base_path = Path(base_str)
    path = _manifest_path(base_path)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        if not isinstance(raw_data, list):
            return []
        valid_items = []
        for entry in raw_data:
            if isinstance(entry, dict):
                item = QuarantineItem.from_dict(entry)
                if item:
                    valid_items.append(item)
        return valid_items
    except (json.JSONDecodeError, OSError, PermissionError):
        return []

def load_manifest(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Interfaz pública para cargar el manifiesto de cuarentena."""
    base_path = quarantine_dir(base)
    base_str = str(base_path)
    if force_reload:
        _load_manifest_internal.cache_clear()
    return _load_manifest_internal(base_str)


def save_manifest(items: List[QuarantineItem], base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persiste la lista de ítems a un archivo temporal y reemplaza el manifiesto atómicamente."""
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista de ítems.")
    base_path = quarantine_dir(base)
    target_path = _manifest_path(base_path)
    temp_fd, temp_path = tempfile.mkstemp(dir=base_path, text=True)
    try:
        with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
            json.dump([item.to_dict() for item in items], f, indent=2, ensure_ascii=False)
        os.replace(temp_path, target_path)
        _load_manifest_internal.cache_clear()
    except (OSError, PermissionError, TypeError, IOError) as e:
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except OSError: pass
        raise RuntimeError(f"Error fatal al persistir manifiesto de cuarentena: {e}")
    return target_path


def quarantine_file(
    source: Union[str, Path],
    reason: str = "Marcado como sospechoso",
    base: Union[str, Path] = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """
    Aísla un archivo en la zona de cuarentena. 
    1. Valida el origen. 2. Realiza copia atómica. 3. Borra el origen tras verificar integridad.
    """
    if not source:
        raise ValueError("La ruta de origen no puede estar vacía.")
    
    source_path = Path(source).expanduser().resolve()
    
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"Archivo no encontrado: {source_path}")
    
    if _is_file_locked(source_path):
        raise IOError("El archivo está en uso y no puede ser movido a cuarentena.")
        
    ensure_safe_to_modify(source_path, allow_sensitive=True)
    if str(source_path).startswith(("\\\\", "//")):
        raise UnsafePathError("No se permite cuarentena en recursos compartidos de red.")
    
    dest_dir = quarantine_dir(base)
    if not dest_dir.exists():
        raise RuntimeError("Directorio de cuarentena inaccesible o no creado.")
    
    # Prevenir que el origen y destino sean la misma carpeta o anidados
    try:
        if os.path.commonpath([str(source_path.parent), str(dest_dir)]) == str(dest_dir):
            raise UnsafePathError("Operación denegada: el archivo ya está en el destino o bajo una subcarpeta del mismo.")
    except ValueError:
        pass
        
    _validate_isolation_request(source_path, dest_dir)
    if not os.access(dest_dir, os.W_OK):
        raise PermissionError("Sin permisos de escritura en la carpeta de cuarentena.")
    
    file_size = source_path.stat().st_size
    usage = shutil.disk_usage(dest_dir)
    if usage.free < (file_size * 1.05):
        raise RuntimeError("Espacio insuficiente en disco para realizar la cuarentena.")
    
    item_id = uuid.uuid4().hex[:12]
    stored_name = _generate_safe_stored_name(source_path, item_id)
    destination = dest_dir / stored_name
    
    if destination.exists():
        raise UnsafePathError("Colisión de nombres: el destino ya existe.")
    
    temp_dest = dest_dir / f"{item_id}_{os.getpid()}.tmp"
    try:
        shutil.copy2(source_path, temp_dest)
        if temp_dest.stat().st_size != file_size:
            raise RuntimeError("Corrupción durante copia: tamaño mismatch.")
        file_hash = _get_sha256(temp_dest)
        if not file_hash:
            raise RuntimeError("Falla de integridad: no se pudo calcular hash.")
        os.replace(temp_dest, destination)
        os.remove(source_path)
    except (OSError, PermissionError) as e:
        if temp_dest.exists(): _safe_unlink(temp_dest)
        raise RuntimeError(f"Error crítico durante el aislamiento: {e}")
    finally:
        if 'temp_dest' in locals() and temp_dest.exists(): _safe_unlink(temp_dest)
    
    try:
        item = QuarantineItem(
            item_id=item_id,
            original_path=str(source_path),
            stored_name=stored_name,
            size_bytes=file_size,
            reason=reason,
            quarantined_at=datetime.now().isoformat(timespec="seconds"),
            sha256=file_hash,
        )
        items = load_manifest(base)
        items.append(item)
        save_manifest(items, base)
        return item
    except Exception as e:
        if destination.exists():
            try: shutil.move(str(destination), str(source_path))
            except Exception: pass
        raise RuntimeError(f"Error al procesar el manifiesto: {e}")


def list_items(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Lista los elementos en cuarentena, ordenados cronológicamente."""
    return sorted(load_manifest(base), key=lambda i: i.quarantined_at, reverse=True)


def restore_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Restaura un archivo desde la cuarentena a su ruta original.
    Valida: 1. Integridad SHA256. 2. Seguridad del destino. 3. Existencia en manifiesto.
    """
    if not item_id or not isinstance(item_id, str):
        raise ValueError("ID de ítem inválido.")
    items = load_manifest(base)
    match = next((i for i in items if i.item_id == item_id), None)
    if not match:
        raise KeyError(f"No se encontró el ítem: {item_id}")
    try:
        base_path = quarantine_dir(base)
        stored_file = (base_path / match.stored_name).resolve()
    except (OSError, ValueError) as e:
        raise RuntimeError(f"Falla de acceso al sandbox: {e}")
    
    if not stored_file.exists() or not stored_file.is_file():
        items.remove(match)
        save_manifest(items, base)
        raise FileNotFoundError("Archivo en cuarentena no localizado o inaccesible.")
    
    if not match.verify_integrity(stored_file):
        raise RuntimeError("Integridad comprometida: el archivo en cuarentena fue modificado.")
    
    destination = Path(match.original_path).resolve()
    if destination.is_symlink() or (hasattr(destination, 'is_junction') and destination.is_junction()):
        raise UnsafePathError("Restauración denegada: destino es un punto de reparse.")
    if is_protected_path(destination):
        raise UnsafePathError("Restauración denegada: destino protegido.")
    if destination.exists():
        raise FileExistsError(f"Error: el destino {destination} ya existe.")
    
    try:
        ensure_safe_to_modify(destination.parent, allow_sensitive=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(stored_file), str(destination))
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Fallo durante la restauración: {e}")
    
    items.remove(match)
    save_manifest(items, base)
    return destination


def purge_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> bool:
    """Borra un ítem específico de la cuarentena tras validar su integridad y sandbox."""
    if not item_id or not isinstance(item_id, str):
        return False
    items = load_manifest(base)
    match = next((i for i in items if i.item_id == item_id), None)
    if not match:
        return False
    quarantine_root = quarantine_dir(base)
    stored_file = (quarantine_root / match.stored_name).resolve()
    
    if not _is_valid_quarantine_path(stored_file, quarantine_root):
        raise UnsafePathError("Borrado de seguridad fallido: ruta fuera de sandbox.")
    
    if not stored_file.exists() or not match.verify_integrity(stored_file):
        raise UnsafePathError("Integridad comprometida: no se puede procesar el archivo.")
    
    ensure_safe_to_modify(stored_file, allow_sensitive=False)
    if _safe_unlink(stored_file):
        try:
            items.remove(match)
            save_manifest(items, base)
            return True
        except (OSError, PermissionError, ValueError):
            return False
    return False


def purge_all(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Vacía la cuarentena eliminando archivos, manteniendo la integridad del manifiesto."""
    try:
        quarantine_root = quarantine_dir(base)
    except (OSError, ValueError):
        return 0
    
    items = load_manifest(base)
    item_map = {i.stored_name: i for i in items}
    purged_count = 0
    items_to_remove = []
    
    for entry in quarantine_root.iterdir():
        # Validar que sea archivo, no sea manifiesto y esté en el sandbox real
        if entry.name == MANIFEST_NAME or not entry.is_file():
            continue
            
        real_entry = entry.resolve()
        # Seguridad: verificar estrictamente que el real_entry esté bajo la raíz
        try:
            if os.path.commonpath([str(real_entry), str(quarantine_root)]) != str(quarantine_root):
                continue
        except (ValueError, OSError):
            continue
            
        # SEGURIDAD: Solo purgar si el archivo figura en el manifiesto
        item = item_map.get(entry.name)
        if not item:
            continue
            
        try:
            if _is_file_locked(entry):
                continue
            
            if item.verify_integrity(real_entry):
                ensure_safe_to_modify(real_entry, allow_sensitive=False)
                if _safe_unlink(real_entry):
                    items_to_remove.append(item)
                    purged_count += 1
        except (OSError, PermissionError, UnsafePathError):
            continue
    
    if purged_count > 0:
        new_items = [i for i in items if i not in items_to_remove]
        try:
            save_manifest(new_items, base)
        except RuntimeError:
            pass 
    return purged_count


def total_quarantined_bytes(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el espacio total ocupado por los archivos en cuarentena."""
    return sum(item.size_bytes for item in load_manifest(base))


def summarize(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte legible de los ítems en cuarentena."""
    items = load_manifest(base)
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
