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
  - No se puede poner en cuarentena algo de una ruta protegida del sistema
    (se valida con `safety.ensure_safe_to_modify`).
  - Al restaurar, el destino se valida para que un manifiesto manipulado no
    pueda escribir en una ruta de sistema.
  - Vaciar la cuarentena solo borra dentro de la carpeta de cuarentena, y
    se verifica con `safety.is_within_directory` antes de cada borrado.
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
from typing import List, Union, Dict, Tuple, Optional, Any

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

# Cache de memoria: { str(path_carpeta_base): (mtime_del_archivo_manifest, lista_de_items) }
_manifest_cache: Dict[str, Tuple[float, List[QuarantineItem]]] = {}

@dataclass
class QuarantineItem:
    """Representa un archivo aislado con metadatos para asegurar reversibilidad."""
    item_id: str
    original_path: str
    stored_name: str
    size_bytes: int
    reason: str
    quarantined_at: str
    sha256: str = ""

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño del archivo en MB con precisión de dos decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la instancia a un diccionario compatible con el esquema JSON."""
        return asdict(self)

    def verify_integrity(self, stored_path: Path) -> bool:
        """
        Valida que el archivo en cuarentena coincida con los metadatos registrados.
        
        Args:
            stored_path: Ruta del archivo dentro de la carpeta de cuarentena.
            
        Returns:
            True si el tamaño y hash coinciden con los guardados en el manifiesto.
        """
        if not stored_path.exists():
            return False
        if stored_path.stat().st_size != self.size_bytes:
            return False
        if self.sha256 and _get_sha256(stored_path) != self.sha256:
            return False
        return True


def _get_sha256(path: Path) -> str:
    """Calcula el hash SHA-256 de un archivo en bloques de 4KB para minimizar el uso de RAM."""
    sha256_hash = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
    except (OSError, IOError) as e:
        raise OSError(f"Falla crítica al leer archivo para hash: {e}")
    return sha256_hash.hexdigest()


def _is_file_locked(path: Path) -> bool:
    """Verifica si un archivo está bloqueado intentando abrirlo en modo append (escritura exclusiva)."""
    try:
        with open(path, "a+b") as f:
            return False
    except (OSError, PermissionError):
        return True


def quarantine_dir(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Normaliza y asegura la existencia del directorio de cuarentena expandiendo el '~'."""
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    path = Path(base).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ubicación esperada del archivo manifest.json dentro del directorio base."""
    return base_dir / MANIFEST_NAME


def _validate_and_convert_item(entry: Any) -> Optional[QuarantineItem]:
    """Valida un diccionario de datos bruto y lo convierte en un objeto QuarantineItem."""
    required = {"item_id", "original_path", "stored_name", "size_bytes", "reason", "quarantined_at"}
    if not isinstance(entry, dict) or not required.issubset(entry.keys()):
        return None
    try:
        return QuarantineItem(
            item_id=str(entry["item_id"]),
            original_path=str(entry["original_path"]),
            stored_name=str(entry["stored_name"]),
            size_bytes=int(entry["size_bytes"]),
            reason=str(entry["reason"]),
            quarantined_at=str(entry["quarantined_at"]),
            sha256=str(entry.get("sha256", ""))
        )
    except (ValueError, TypeError):
        return None


def load_manifest(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """
    Carga el manifiesto desde disco, utilizando caché por mtime para optimizar I/O.
    
    Args:
        base: Directorio donde buscar el manifiesto.
        force_reload: Si es True, ignora la caché existente.
        
    Returns:
        Lista de objetos QuarantineItem deserializados; retorna lista vacía en caso de corrupción.
    """
    base_path = quarantine_dir(base)
    path = _manifest_path(base_path)
    base_str = str(base_path)
    
    current_mtime = path.stat().st_mtime if path.exists() else 0.0
    if not force_reload and base_str in _manifest_cache:
        cached_mtime, cached_data = _manifest_cache[base_str]
        if cached_mtime == current_mtime:
            return cached_data
        
    if not path.exists():
        _manifest_cache[base_str] = (0.0, [])
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    
    if not isinstance(raw_data, list):
        return []

    items = [item for entry in raw_data if (item := _validate_and_convert_item(entry))]
    _manifest_cache[base_str] = (current_mtime, items)
    return items


def save_manifest(items: List[QuarantineItem], base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Persiste la lista de ítems de forma atómica usando un archivo temporal.
    
    Args:
        items: Lista de objetos QuarantineItem a persistir.
        base: Directorio de trabajo.
        
    Raises:
        ValueError: Si items no es una lista.
        RuntimeError: En caso de fallo de escritura atómica.
    """
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista de ítems.")
        
    base_path = quarantine_dir(base)
    target_path = _manifest_path(base_path)
    
    fd, temp_path = tempfile.mkstemp(dir=base_path, text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump([item.to_dict() for item in items], f, indent=2, ensure_ascii=False)
        os.replace(temp_path, target_path)
        _manifest_cache[str(base_path)] = (target_path.stat().st_mtime, items)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise RuntimeError(f"Error fatal al persistir manifiesto: {e}")
    return target_path


def quarantine_file(
    source: Union[str, Path],
    reason: str = "Marcado como sospechoso",
    base: Union[str, Path] = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """
    Mueve un archivo a cuarentena tras validar que es seguro operarlo.
    """
    if not source:
        raise ValueError("La ruta de origen no puede estar vacía.")
    
    source_path = normalize(source)
    
    if not source_path.exists():
        raise FileNotFoundError(f"El archivo de origen no existe: {source_path}")
    
    if not source_path.is_file() or source_path.is_symlink():
        raise UnsafePathError(f"Solo se permiten archivos regulares: {source_path}")
        
    if is_protected_path(source_path):
        raise UnsafePathError(f"Operación prohibida en ruta del sistema: {source_path}")
        
    dest_dir = quarantine_dir(base)
    if is_protected_path(dest_dir):
        raise UnsafePathError(f"Directorio de cuarentena protegido o inválido: {dest_dir}")
    
    if dest_dir.is_symlink() or (hasattr(dest_dir, 'is_junction') and dest_dir.is_junction()):
        raise UnsafePathError("La ruta de cuarentena es un punto de reparse (prohibido).")

    if is_within_directory(source_path, dest_dir):
        raise UnsafePathError(f"El archivo ya reside en la carpeta de cuarentena: {source_path}")

    ensure_safe_to_modify(source_path, allow_sensitive=True)
    ensure_safe_to_modify(dest_dir, allow_sensitive=False)
    
    if not os.access(dest_dir, os.W_OK):
        raise PermissionError(f"El directorio de cuarentena no tiene permisos de escritura: {dest_dir}")
    
    if _is_file_locked(source_path):
        raise IOError(f"El archivo está en uso por otro proceso: {source_path}")
    
    try:
        file_size = source_path.stat().st_size
    except OSError as e:
        raise OSError(f"Error al acceder a metadatos de archivo: {e}")
        
    try:
        usage = shutil.disk_usage(dest_dir)
        if usage.free < file_size:
            raise OSError(f"Espacio insuficiente en disco para mover: {dest_dir}")
    except OSError as e:
        raise OSError(f"Error al verificar espacio en disco: {e}")

    item_id = uuid.uuid4().hex[:12]
    safe_name = "".join(c for c in source_path.name if c.isalnum() or c in "._-")
    stored_name = f"{item_id}__{safe_name}"[:250] 
    destination = dest_dir / stored_name

    if is_protected_path(destination):
        raise UnsafePathError(f"Ruta de destino en cuarentena considerada insegura: {destination}")

    if destination.exists():
        raise FileExistsError(f"Colisión de nombre en destino: {destination}")

    try:
        shutil.move(str(source_path), str(destination))
    except OSError as e:
        if e.errno in (28, 39): 
             raise OSError(f"Falla de escritura (disco lleno o error de sistema): {e}")
        raise RuntimeError(f"Falla crítica al mover archivo: {e}")

    if not destination.exists():
        raise RuntimeError("Integridad comprometida: el archivo no apareció en el destino tras el movimiento.")
    
    if destination.stat().st_size != file_size:
        raise RuntimeError("Integridad comprometida: el archivo cambió de tamaño tras el movimiento.")

    try:
        file_hash = _get_sha256(destination)
        item = QuarantineItem(
            item_id=item_id,
            original_path=str(source_path),
            stored_name=stored_name,
            size_bytes=file_size,
            reason=reason,
            quarantined_at=datetime.now().isoformat(timespec="seconds"),
            sha256=file_hash,
        )
        items = load_manifest(base).copy()
        items.append(item)
        save_manifest(items, base)
        return item
    except Exception as e:
        if destination.exists():
            try:
                shutil.move(str(destination), str(source_path))
            except OSError:
                pass
        raise RuntimeError(f"Error irrecuperable procesando metadatos (archivo revertido): {e}")


def list_items(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna los ítems en cuarentena, ordenados cronológicamente (más nuevo primero)."""
    return sorted(load_manifest(base), key=lambda i: i.quarantined_at, reverse=True)


def restore_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un archivo a su ruta original tras verificar su integridad."""
    items = load_manifest(base)
    match = next((i for i in items if i.item_id == item_id), None)
    
    if not match:
        raise KeyError(f"No se encontró ítem con ID: {item_id}")

    base_path = quarantine_dir(base)
    stored_file = base_path / match.stored_name
    
    if not stored_file.exists():
        raise FileNotFoundError(f"El archivo no existe en la carpeta de cuarentena: {stored_file}")

    if not match.verify_integrity(stored_file):
        raise RuntimeError("Integridad comprometida: el archivo en cuarentena fue alterado.")

    destination = normalize(match.original_path)
    
    if destination.is_symlink():
        raise UnsafePathError(f"Restauración denegada: {destination} es un enlace simbólico.")

    if is_protected_path(destination):
        raise UnsafePathError(f"Restauración denegada: la ruta está protegida.")

    if destination.exists():
        raise FileExistsError(f"El destino ya está ocupado: {destination}")
        
    ensure_safe_to_modify(destination, allow_sensitive=False)

    try:
        parent_dir = destination.parent
        if parent_dir.exists() and not parent_dir.is_dir():
            raise NotADirectoryError(f"La ruta padre no es un directorio: {parent_dir}")
        
        if not parent_dir.exists():
            parent_dir.mkdir(parents=True, exist_ok=True)
            
        shutil.move(str(stored_file), str(destination))
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Fallo durante la operación de restauración: {e}")

    items.remove(match)
    save_manifest(items, base)
    return destination


def purge_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> bool:
    """Elimina físicamente un ítem tras validar su integridad. Retorna True si tuvo éxito."""
    items = load_manifest(base)
    match = next((i for i in items if i.item_id == item_id), None)
    
    if not match:
        return False

    quarantine_root = quarantine_dir(base)
    stored_file = quarantine_root / match.stored_name
    
    if is_protected_path(stored_file) or not is_within_directory(stored_file, quarantine_root):
        raise UnsafePathError(f"Intento de borrado fuera de cuarentena: {stored_file}")

    if not stored_file.exists() or not match.verify_integrity(stored_file):
        raise UnsafePathError(f"Integridad comprometida: no se borra un archivo sospechoso modificado.")

    try:
        stored_file.unlink()
    except (OSError, PermissionError):
        return False
    
    items.remove(match)
    save_manifest(items, base)
    return True


def purge_all(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Limpia el almacén completo, borrando solo archivos cuya integridad es verificable."""
    quarantine_root = quarantine_dir(base)
    if is_protected_path(quarantine_root):
        raise UnsafePathError("Operación denegada en ruta protegida.")
        
    ensure_safe_to_modify(quarantine_root, allow_sensitive=False)
    
    items = load_manifest(base)
    item_map = {item.stored_name: item for item in items}
    stored_names = set(item_map.keys())
    count = 0
    
    for entry in quarantine_root.iterdir():
        if entry.is_file() and entry.name in stored_names:
            item = item_map[entry.name]
            if is_within_directory(entry, quarantine_root) and item.verify_integrity(entry):
                try:
                    entry.unlink()
                    count += 1
                except (OSError, PermissionError):
                    continue
        elif entry.is_file() and entry.name != MANIFEST_NAME:
            # Caso de archivo basura en carpeta de cuarentena, no listado en manifiesto
            try:
                entry.unlink()
            except (OSError, PermissionError):
                pass
            
    if count > 0:
        remaining_items = [i for i in items if (quarantine_root / i.stored_name).exists()]
        save_manifest(remaining_items, base)
    return count


def total_quarantined_bytes(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el peso total en bytes de los archivos bajo cuarentena usando la caché."""
    return sum(item.size_bytes for item in load_manifest(base))


def summarize(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte legible para el usuario detallando el estado de la cuarentena."""
    items = load_manifest(base)
    if not items:
        return ["La cuarentena está vacía."]
    
    total_mb = sum(i.size_mb for i in items)
    
    lines = [f"{len(items)} archivo(s) en cuarentena — {round(total_mb, 2)} MB", ""]
    for item in items:
        lines.append(f"  [{item.item_id}] {Path(item.original_path).name} — {item.size_mb} MB")
        lines.append(f"      Motivo: {item.reason}")
        lines.append(f"      Origen: {item.original_path}")
        lines.append(f"      Aislado: {item.quarantined_at}")
    lines.append("")
    lines.append("Nada de esto se borró: se puede restaurar a su ubicación original.")
    return lines
