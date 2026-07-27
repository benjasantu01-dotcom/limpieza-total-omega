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
    """
    Representa un archivo aislado. 
    Mantiene la metainformación necesaria para revertir la acción de cuarentena.
    """
    item_id: str
    original_path: str
    stored_name: str
    size_bytes: int
    reason: str
    quarantined_at: str
    sha256: str = ""

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño del archivo en MB con dos decimales de precisión."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> dict:
        """Serializa la instancia a un diccionario compatible con JSON."""
        return asdict(self)


def _get_sha256(path: Path) -> str:
    """
    Calcula el hash SHA-256 de un archivo en bloques de 4KB para minimizar el uso de memoria.
    Lanza OSError si el archivo no es accesible.
    """
    sha256_hash = hashlib.sha256()
    with open(path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def _is_file_locked(path: Path) -> bool:
    """
    Determina si un archivo está bloqueado intentando renombrarlo sobre sí mismo.
    Retorna True si el sistema operativo deniega la operación (archivo en uso).
    """
    try:
        path.rename(path)
        return False
    except OSError:
        return True


def quarantine_dir(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Normaliza la ruta de cuarentena y asegura su existencia en el sistema de archivos.
    """
    path = Path(base).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Retorna la ubicación esperada del archivo JSON de manifiesto."""
    return quarantine_dir(base) / MANIFEST_NAME


def load_manifest(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """
    Obtiene la lista de ítems en cuarentena desde el manifiesto JSON.
    Usa un caché en memoria para evitar I/O redundante. Retorna lista vacía si el archivo no existe o está corrupto.
    """
    base_str = str(base)
    if not force_reload and base_str in _manifest_cache:
        return _manifest_cache[base_str]
        
    path = _manifest_path(base)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items: List[QuarantineItem] = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            items.append(QuarantineItem(**entry))
        except (TypeError, ValueError):
            continue
    _manifest_cache[base_str] = items
    return items


def save_manifest(items: List[QuarantineItem], base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Persiste la lista de objetos QuarantineItem en un archivo JSON en disco.
    """
    _manifest_cache[str(base)] = items
    path = _manifest_path(base)
    path.write_text(
        json.dumps([item.to_dict() for item in items], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def quarantine_file(
    source: Union[str, Path],
    reason: str = "Marcado como sospechoso",
    base: Union[str, Path] = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """
    Mueve un archivo a cuarentena tras validar seguridad y bloqueos.
    Lanza UnsafePathError (vía `ensure_safe_to_modify`), FileNotFoundError, 
    IOError si está bloqueado, o RuntimeError si falla el movimiento físico.
    """
    if not source:
        raise ValueError("La ruta de origen no puede estar vacía.")
    
    origin = normalize(source)
    if not origin.is_file():
        raise FileNotFoundError(f"El objeto no es un archivo válido: {origin}")
    
    if _is_file_locked(origin):
        raise IOError(f"El archivo está en uso por otro proceso: {origin}")

    ensure_safe_to_modify(origin, allow_sensitive=True)
    
    dest_dir = quarantine_dir(base)
    ensure_safe_to_modify(dest_dir, allow_sensitive=False)

    item_id = uuid.uuid4().hex[:12]
    safe_filename = Path(origin.name).name
    stored_name = f"{item_id}__{safe_filename}"
    destination = dest_dir / stored_name

    if destination.exists():
        raise FileExistsError(f"Colisión de nombre en cuarentena: {destination}")

    file_size = origin.stat().st_size
    
    try:
        shutil.move(str(origin), str(destination))
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
            items = list(_manifest_cache.get(str(base)) or load_manifest(base))
            items.append(item)
            save_manifest(items, base)
            return item
        except Exception as e:
            if destination.exists():
                shutil.move(str(destination), str(origin))
            raise RuntimeError(f"Error al actualizar manifiesto: {e}")
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Falla crítica al mover archivo a cuarentena: {e}")


def list_items(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna todos los ítems en cuarentena, ordenados de forma descendente por fecha."""
    items = list(load_manifest(base))
    items.sort(key=lambda i: i.quarantined_at, reverse=True)
    return items


def restore_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Restaura un archivo a su ubicación original validando su integridad mediante SHA-256.
    Lanza UnsafePathError si la ruta original es protegida, o RuntimeError si la integridad falló.
    """
    if not item_id or not isinstance(item_id, str):
        raise ValueError("El ID del elemento debe ser una cadena válida.")

    items = list(_manifest_cache.get(str(base)) or load_manifest(base))
    match = next((i for i in items if i.item_id == item_id), None)
    if match is None:
        raise KeyError(f"No hay ningún elemento en cuarentena con id '{item_id}'.")

    stored_file = quarantine_dir(base) / match.stored_name
    
    if not stored_file.is_file():
        raise FileNotFoundError(f"Archivo en cuarentena inexistente: {stored_file}")
        
    if match.sha256 and _get_sha256(stored_file) != match.sha256:
        raise RuntimeError("Integridad comprometida: el archivo en cuarentena fue alterado.")

    destination = normalize(match.original_path)
    
    if is_protected_path(destination):
        raise UnsafePathError(f"Restauración denegada: '{destination}' es una ruta protegida.")

    if destination.exists():
        raise FileExistsError(f"Restauración abortada: El archivo ya existe en '{destination}'")
        
    ensure_safe_to_modify(destination, allow_sensitive=False)

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stored_file), str(destination))
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Error durante la restauración del archivo: {e}")

    items.remove(match)
    save_manifest(items, base)
    return destination


def purge_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> bool:
    """
    Elimina físicamente un archivo de la cuarentena. 
    Lanza UnsafePathError si el archivo está fuera del directorio de cuarentena definido.
    """
    if not item_id or not isinstance(item_id, str):
        return False
    items = load_manifest(base)
    match = next((i for i in items if i.item_id == item_id), None)
    if match is None:
        return False

    quarantine_root = quarantine_dir(base)
    stored_file = normalize(quarantine_root / match.stored_name)
    
    if not is_within_directory(stored_file, quarantine_root):
        raise UnsafePathError(f"Borrado bloqueado: '{stored_file}' está fuera de la cuarentena.")

    if stored_file.is_file():
        stored_file.unlink()
    items.remove(match)
    save_manifest(items, base)
    return True


def purge_all(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """
    Vacía la carpeta de cuarentena y resetea el manifiesto. Retorna el número de archivos borrados.
    """
    quarantine_root = quarantine_dir(base)
    items = load_manifest(base)
    count = 0
    for item in items:
        stored_file = quarantine_root / item.stored_name
        if not is_within_directory(stored_file, quarantine_root):
            continue
        try:
            if stored_file.is_file():
                stored_file.unlink()
                count += 1
        except (OSError, PermissionError):
            continue
    save_manifest([], base)
    return count


def total_quarantined_bytes(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el uso total de espacio (en bytes) de la cuarentena."""
    return sum(item.size_bytes for item in load_manifest(base))


def summarize(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte textual descriptivo de los ítems en cuarentena para la interfaz."""
    items = list_items(base)
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
