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
from typing import List, Union, Dict, Optional

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

_manifest_cache: Dict[str, List[QuarantineItem]] = {}

@dataclass
class QuarantineItem:
    """Representa un archivo aislado con metadatos para reversibilidad."""
    item_id: str
    original_path: str
    stored_name: str
    size_bytes: int
    reason: str
    quarantined_at: str
    sha256: str = ""

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño del archivo en MB con dos decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> dict:
        """Serializa la instancia a un diccionario compatible con JSON."""
        return asdict(self)


def _get_sha256(path: Path) -> str:
    """Calcula el hash SHA-256 de un archivo en bloques de 4KB."""
    sha256_hash = hashlib.sha256()
    with open(path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def _is_file_locked(path: Path) -> bool:
    """Verifica si un archivo está en uso exclusivo intentando un rename."""
    try:
        path.rename(path)
        return False
    except OSError:
        return True


def quarantine_dir(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Normaliza y asegura la existencia del directorio de cuarentena."""
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    path = Path(base).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Retorna la ruta absoluta al archivo de manifiesto."""
    return quarantine_dir(base) / MANIFEST_NAME


def load_manifest(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """
    Carga y parsea el manifiesto desde el disco. Implementa una caché en memoria
    para evitar I/O redundante. Valida tipos de datos contra el esquema esperado.
    """
    base_path = quarantine_dir(base)
    base_str = str(base_path)
    if not force_reload and base_str in _manifest_cache:
        return _manifest_cache[base_str]
        
    path = _manifest_path(base_path)
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
    _manifest_cache[base_str] = items
    return items


def save_manifest(items: List[QuarantineItem], base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Persiste la lista de ítems en el manifiesto JSON.
    La integridad del archivo depende de que `items` contenga solo objetos válidos.
    """
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista de ítems.")
        
    base_path = quarantine_dir(base)
    _manifest_cache[str(base_path)] = items
    path = _manifest_path(base_path)
    try:
        path.write_text(
            json.dumps([item.to_dict() for item in items], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        raise RuntimeError(f"Error al escribir el manifiesto: {e}")
    return path


def quarantine_file(
    source: Union[str, Path],
    reason: str = "Marcado como sospechoso",
    base: Union[str, Path] = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """Mueve un archivo a cuarentena tras validar seguridad y bloqueos."""
    if not source:
        raise ValueError("La ruta de origen no puede estar vacía.")
    
    origin = normalize(source)
    dest_dir = quarantine_dir(base)

    if not origin.is_file():
        raise FileNotFoundError(f"El objeto no es un archivo válido: {origin}")
    
    if origin.is_symlink():
        raise UnsafePathError(f"Operación denegada: {origin} es un enlace simbólico.")
    
    if is_within_directory(origin, dest_dir):
        raise UnsafePathError(f"El archivo ya reside en la carpeta de cuarentena: {origin}")

    if _is_file_locked(origin):
        raise IOError(f"El archivo está en uso por otro proceso: {origin}")

    ensure_safe_to_modify(origin, allow_sensitive=True)
    ensure_safe_to_modify(dest_dir, allow_sensitive=False)
    
    if not origin.exists():
        raise FileNotFoundError(f"El archivo desapareció antes de ser procesado: {origin}")

    file_size = origin.stat().st_size
    usage = shutil.disk_usage(dest_dir)
    if usage.free < file_size:
        raise OSError(f"No hay espacio suficiente en: {dest_dir}")

    item_id = uuid.uuid4().hex[:12]
    stored_name = f"{item_id}__{origin.name}"
    destination = dest_dir / stored_name

    if destination.exists():
        raise FileExistsError(f"Colisión de nombre en cuarentena: {destination}")

    try:
        shutil.move(str(origin), str(destination))
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Falla al mover archivo a cuarentena: {e}")

    try:
        file_hash = _get_sha256(destination)
        item = QuarantineItem(
            item_id=item_id,
            original_path=str(origin),
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
            try:
                destination.unlink()
            except OSError:
                pass
        raise RuntimeError(f"Error al procesar manifiesto tras mover el archivo: {e}")


def list_items(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna los ítems en cuarentena ordenados por fecha descendente."""
    return sorted(load_manifest(base), key=lambda i: i.quarantined_at, reverse=True)


def restore_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un archivo a su ubicación original validando integridad."""
    if not item_id or not isinstance(item_id, str):
        raise ValueError("El ID debe ser una cadena válida.")

    items = load_manifest(base)
    match = next((i for i in items if i.item_id == item_id), None)
    
    if match is None:
        raise KeyError(f"No se encontró el ítem: {item_id}")

    base_path = quarantine_dir(base)
    stored_file = base_path / match.stored_name
    
    if not stored_file.is_file():
        raise FileNotFoundError(f"Archivo inexistente en cuarentena: {stored_file}")
        
    if match.sha256 and _get_sha256(stored_file) != match.sha256:
        raise RuntimeError("Integridad comprometida (SHA256 mismatch).")

    destination = normalize(match.original_path)
    
    if is_protected_path(destination):
        raise UnsafePathError(f"Restauración denegada: {destination} está protegida.")

    if destination.exists():
        raise FileExistsError(f"El destino ya está ocupado: {destination}")
        
    ensure_safe_to_modify(destination, allow_sensitive=False)

    try:
        if not destination.parent.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stored_file), str(destination))
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Fallo durante la restauración: {e}")

    items.remove(match)
    save_manifest(items, base)
    return destination


def purge_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> bool:
    """Elimina físicamente un ítem de la cuarentena."""
    if not item_id or not isinstance(item_id, str):
        return False
    
    items = load_manifest(base)
    match = next((i for i in items if i.item_id == item_id), None)
    
    if match is None:
        return False

    quarantine_root = quarantine_dir(base)
    stored_file = quarantine_root / match.stored_name
    
    if not stored_file.is_file() or not is_within_directory(stored_file, quarantine_root):
        raise UnsafePathError(f"Intento de borrado fuera de cuarentena: {stored_file}")

    try:
        stored_file.unlink()
    except (OSError, PermissionError):
        return False
    
    items.remove(match)
    save_manifest(items, base)
    return True


def purge_all(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Limpia el directorio y resetea el manifiesto."""
    quarantine_root = quarantine_dir(base)
    items = load_manifest(base)
    count = 0
    for item in items:
        stored_file = quarantine_root / item.stored_name
        if stored_file.is_file() and is_within_directory(stored_file, quarantine_root):
            try:
                stored_file.unlink()
                count += 1
            except (OSError, PermissionError):
                continue
    save_manifest([], base)
    return count


def total_quarantined_bytes(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el total de bytes usados por ítems en cuarentena."""
    base_path = quarantine_dir(base)
    items = _manifest_cache.get(str(base_path), load_manifest(base))
    return sum(item.size_bytes for item in items)


def summarize(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera una lista de cadenas para reporte legible en UI."""
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
