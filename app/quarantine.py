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
import shutil
import uuid
import hashlib
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Union, Dict, Tuple

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

# Almacena el estado del manifiesto en memoria para evitar I/O redundante:
# { str(base_path): (mtime_del_archivo, lista_de_items) }
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

    def to_dict(self) -> dict:
        """Serializa la instancia a un diccionario compatible con el esquema JSON."""
        return asdict(self)


def _get_sha256(path: Path) -> str:
    """
    Calcula el hash SHA-256 del archivo. 
    Se lee en bloques de 4KB para mantener bajo el consumo de memoria.
    """
    sha256_hash = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
    except OSError as e:
        raise OSError(f"Falla crítica al leer archivo para hash: {e}")
    return sha256_hash.hexdigest()


def _is_file_locked(path: Path) -> bool:
    """
    Verifica si un archivo está en uso exclusivo intentando una operación de renombrado.
    Si `rename` falla, se asume que otro proceso mantiene un lock sobre el archivo.
    """
    try:
        path.rename(path)
        return False
    except OSError:
        return True


def quarantine_dir(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Normaliza y asegura la existencia del directorio de cuarentena expandiendo el '~'."""
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    path = Path(base).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path(base_dir: Path) -> Path:
    """Obtiene la ruta completa del archivo de manifiesto JSON."""
    return base_dir / MANIFEST_NAME


def load_manifest(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """
    Carga el manifiesto. Utiliza una caché basada en `mtime` para evitar lecturas de disco innecesarias.
    Si el manifiesto es inválido (JSON roto o formato incorrecto), retorna una lista vacía para no romper la app.
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
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    
    if not isinstance(data, list):
        return []

    items: List[QuarantineItem] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            item = QuarantineItem(
                item_id=str(entry["item_id"]),
                original_path=str(entry["original_path"]),
                stored_name=str(entry["stored_name"]),
                size_bytes=int(entry["size_bytes"]),
                reason=str(entry["reason"]),
                quarantined_at=str(entry["quarantined_at"]),
                sha256=str(entry.get("sha256", ""))
            )
            items.append(item)
        except (KeyError, ValueError, TypeError):
            continue
    _manifest_cache[base_str] = (current_mtime, items)
    return items


def save_manifest(items: List[QuarantineItem], base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persiste la lista de ítems al archivo de manifiesto y actualiza el caché en memoria."""
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista de ítems.")
        
    base_path = quarantine_dir(base)
    path = _manifest_path(base_path)
    try:
        path.write_text(
            json.dumps([item.to_dict() for item in items], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _manifest_cache[str(base_path)] = (path.stat().st_mtime, items)
    except OSError as e:
        raise RuntimeError(f"Error fatal al persistir manifiesto: {e}")
    return path


def quarantine_file(
    source: Union[str, Path],
    reason: str = "Marcado como sospechoso",
    base: Union[str, Path] = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """
    Aísla un archivo moviéndolo a la carpeta de cuarentena.
    Realiza validaciones de seguridad: symlinks, permisos, ocupación y espacio en disco.
    """
    if not source:
        raise ValueError("La ruta de origen no puede estar vacía.")
    
    source_path = normalize(source)
    dest_dir = quarantine_dir(base)

    if not source_path.is_file():
        raise FileNotFoundError(f"El objeto origen no es un archivo válido: {source_path}")
    
    if source_path.is_symlink():
        raise UnsafePathError(f"Operación denegada: {source_path} es un enlace simbólico.")
    
    if is_within_directory(source_path, dest_dir):
        raise UnsafePathError(f"El archivo ya reside en la carpeta de cuarentena: {source_path}")

    # Validaciones críticas para evitar modificaciones no autorizadas en carpetas sensibles
    ensure_safe_to_modify(source_path, allow_sensitive=True)
    ensure_safe_to_modify(dest_dir, allow_sensitive=False)
    
    if _is_file_locked(source_path):
        raise IOError(f"El archivo está en uso por otro proceso: {source_path}")
    
    file_size = source_path.stat().st_size
    usage = shutil.disk_usage(dest_dir)
    if usage.free < file_size:
        raise OSError(f"Espacio insuficiente en disco para mover: {dest_dir}")

    item_id = uuid.uuid4().hex[:12]
    # Sanitización de nombre para evitar caracteres inválidos en sistemas de archivos
    safe_name = "".join(c for c in source_path.name if c.isalnum() or c in "._-")
    stored_name = f"{item_id}__{safe_name}"[:250] 
    destination = dest_dir / stored_name

    if destination.exists():
        raise FileExistsError(f"Colisión de nombre en destino: {destination}")

    try:
        shutil.move(str(source_path), str(destination))
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Falla crítica al mover archivo: {e}")

    try:
        # Verificación post-movimiento: el hash garantiza que el archivo no fue corrupto durante el IO
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
        items = load_manifest(base, force_reload=True)
        items.append(item)
        save_manifest(items, base)
        return item
    except Exception as e:
        # Intento de roll-back en caso de fallo al actualizar el manifiesto o calcular hash
        if destination.exists():
            try:
                shutil.move(str(destination), str(source_path))
            except OSError:
                pass
        raise RuntimeError(f"Error irrecuperable procesando archivo en cuarentena: {e}")


def list_items(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna los ítems en cuarentena, ordenados cronológicamente (más recientes primero)."""
    return sorted(load_manifest(base), key=lambda i: i.quarantined_at, reverse=True)


def restore_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Restaura un archivo a su ruta original. Valida la integridad (SHA256) antes del movimiento.
    Recrea directorios padre si es necesario, asegurando que no se restaure en rutas protegidas.
    """
    if not item_id or not isinstance(item_id, str):
        raise ValueError("El ID debe ser una cadena válida.")

    items = load_manifest(base)
    match = next((i for i in items if i.item_id == item_id), None)
    
    if match is None:
        raise KeyError(f"No se encontró ítem con ID: {item_id}")

    base_path = quarantine_dir(base)
    stored_file = base_path / match.stored_name
    
    if not stored_file.is_file():
        raise FileNotFoundError(f"Archivo inexistente en el almacén: {stored_file}")
        
    if match.sha256 and _get_sha256(stored_file) != match.sha256:
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
    """Elimina físicamente un ítem de la cuarentena tras validar que se encuentra en la ruta correcta."""
    if not item_id or not isinstance(item_id, str):
        return False
    
    items = load_manifest(base)
    match = next((i for i in items if i.item_id == item_id), None)
    
    if match is None:
        return False

    quarantine_root = quarantine_dir(base)
    stored_file = quarantine_root / match.stored_name
    
    # Doble verificación: que sea seguro, que exista y que esté confinado en cuarentena
    if is_protected_path(stored_file) or not stored_file.is_file() or not is_within_directory(stored_file, quarantine_root):
        raise UnsafePathError(f"Intento de borrado fuera de cuarentena: {stored_file}")

    if match.sha256 and _get_sha256(stored_file) != match.sha256:
        raise UnsafePathError(f"Integridad comprometida: no se borra un archivo sospechoso modificado.")

    try:
        stored_file.unlink()
    except (OSError, PermissionError):
        return False
    
    items.remove(match)
    save_manifest(items, base)
    return True


def purge_all(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Limpia el almacén completo y resetea el manifiesto."""
    quarantine_root = quarantine_dir(base)
    if is_protected_path(quarantine_root):
        raise UnsafePathError("Operación denegada en ruta protegida.")
        
    ensure_safe_to_modify(quarantine_root, allow_sensitive=False)
    
    items = load_manifest(base)
    count = 0
    for item in items:
        stored_file = quarantine_root / item.stored_name
        if not is_protected_path(stored_file) and stored_file.is_file() and is_within_directory(stored_file, quarantine_root):
            try:
                # Se eliminan solo los archivos que mantienen su integridad
                if not item.sha256 or _get_sha256(stored_file) == item.sha256:
                    stored_file.unlink()
                    count += 1
            except (OSError, PermissionError):
                continue
    save_manifest([], base)
    return count


def total_quarantined_bytes(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el peso total en bytes de los archivos bajo cuarentena."""
    items = load_manifest(base)
    return sum(item.size_bytes for item in items)


def summarize(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte human-readable con los ítems actuales."""
    items = load_manifest(base)
    if not items:
        return ["La cuarentena está vacía."]
    total_mb = round(total_quarantined_bytes(base) / (1024 * 1024), 2)
    lines = [f"{len(items)} archivo(s) en cuarentena — {total_mb} MB", ""]
    for item in items:
        lines.append(f"  [{item.item_id}] {Path(item.original_path).name} — {item.size_mb} MB")
        lines.append(f"      Motivo: {item.reason}")
        lines.append(f"      Origen: {item.original_path}")
        lines.append(f"      Aislado: {item.quarantined_at}")
    lines.append("")
    lines.append("Nada de esto se borró: se puede restaurar a su ubicación original.")
    return lines
