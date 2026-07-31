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
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Union, Dict, Tuple, Optional

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
    except (OSError, IOError) as e:
        raise OSError(f"Falla crítica al leer archivo para hash: {e}")
    return sha256_hash.hexdigest()


def _is_file_locked(path: Path) -> bool:
    """
    Determina si un archivo está bloqueado intentando abrirlo en modo append.
    Si falla, el sistema operativo lo tiene reservado para otro proceso.
    """
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
    """Obtiene la ruta completa del archivo de manifiesto JSON."""
    return base_dir / MANIFEST_NAME


def load_manifest(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """
    Carga el manifiesto desde disco. Utiliza caché por mtime para optimizar rendimiento.
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
        raw_data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    
    if not isinstance(raw_data, list):
        return []

    items: List[QuarantineItem] = []
    required_keys = {"item_id", "original_path", "stored_name", "size_bytes", "reason", "quarantined_at"}
    
    for entry in raw_data:
        if not isinstance(entry, dict) or not required_keys.issubset(entry.keys()):
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
        except (ValueError, TypeError):
            continue
    _manifest_cache[base_str] = (current_mtime, items)
    return items


def save_manifest(items: List[QuarantineItem], base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persiste la lista de ítems al archivo de manifiesto y actualiza el caché."""
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
    Mueve un archivo a cuarentena tras validar que es seguro operarlo.
    
    Aplica restricciones de seguridad estrictas:
    1. Verifica que la ruta no sea protegida del sistema.
    2. Impide mover symlinks para evitar recorridos infinitos o saltos fuera del entorno.
    3. Asegura espacio en disco antes de la operación física.
    """
    if not source:
        raise ValueError("La ruta de origen no puede estar vacía.")
    
    source_path = normalize(source)
    
    if not source_path.exists():
        raise FileNotFoundError(f"El archivo de origen no existe: {source_path}")
    
    if is_protected_path(source_path):
        raise UnsafePathError(f"Operación prohibida en ruta del sistema: {source_path}")
        
    dest_dir = quarantine_dir(base)
    if is_protected_path(dest_dir):
        raise UnsafePathError(f"Directorio de cuarentena protegido o inválido: {dest_dir}")

    if not source_path.is_file():
        raise ValueError(f"El objeto origen no es un archivo válido: {source_path}")
    
    if source_path.is_symlink():
        raise UnsafePathError(f"Operación denegada: {source_path} es un enlace simbólico.")
    
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
        usage = shutil.disk_usage(dest_dir)
        if usage.free < file_size:
            raise OSError(f"Espacio insuficiente en disco para mover: {dest_dir}")
    except OSError as e:
        raise OSError(f"Error al verificar metadatos de archivo/disco: {e}")

    item_id = uuid.uuid4().hex[:12]
    safe_name = "".join(c for c in source_path.name if c.isalnum() or c in "._-")
    stored_name = f"{item_id}__{safe_name}"[:250] 
    destination = dest_dir / stored_name

    if destination.exists():
        raise FileExistsError(f"Colisión de nombre en destino: {destination}")

    try:
        shutil.move(str(source_path), str(destination))
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Falla crítica al mover archivo: {e}")

    if not destination.exists():
        raise RuntimeError("El archivo no pudo localizarse en el destino tras el movimiento.")

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
        items = load_manifest(base, force_reload=True)
        items.append(item)
        save_manifest(items, base)
        return item
    except Exception as e:
        try:
            shutil.move(str(destination), str(source_path))
        except OSError:
            pass
        raise RuntimeError(f"Error irrecuperable procesando metadatos (archivo revertido): {e}")


def list_items(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna los ítems en cuarentena, ordenados cronológicamente (más recientes primero)."""
    return sorted(load_manifest(base), key=lambda i: i.quarantined_at, reverse=True)


def restore_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Restaura un archivo a su ruta original tras verificar su hash SHA-256.
    
    Validaciones:
    - Asegura que el archivo no ha sido alterado mientras estuvo aislado.
    - Evita restaurar sobre rutas protegidas.
    - Recrea recursivamente el árbol de carpetas destino si es necesario.
    """
    if not item_id or not isinstance(item_id, str):
        raise ValueError("El ID debe ser una cadena válida.")

    items = load_manifest(base)
    match: Optional[QuarantineItem] = next((i for i in items if i.item_id == item_id), None)
    
    if match is None:
        raise KeyError(f"No se encontró ítem con ID: {item_id}")

    base_path = quarantine_dir(base)
    stored_file = base_path / match.stored_name
    
    if not stored_file.exists():
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
    """Elimina físicamente un ítem tras validar que sigue en cuarentena y es auténtico."""
    if not item_id or not isinstance(item_id, str):
        return False
    
    items = load_manifest(base)
    match: Optional[QuarantineItem] = next((i for i in items if i.item_id == item_id), None)
    
    if match is None:
        return False

    quarantine_root = quarantine_dir(base)
    stored_file = quarantine_root / match.stored_name
    
    if is_protected_path(stored_file) or not stored_file.exists() or not is_within_directory(stored_file, quarantine_root):
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
    """Limpia el almacén completo, borrando solo archivos cuya integridad es verificable."""
    quarantine_root = quarantine_dir(base)
    if is_protected_path(quarantine_root):
        raise UnsafePathError("Operación denegada en ruta protegida.")
        
    ensure_safe_to_modify(quarantine_root, allow_sensitive=False)
    
    items = load_manifest(base)
    count = 0
    
    def _is_safe_to_purge(path: Path, item: QuarantineItem) -> bool:
        """Verifica restricciones de seguridad para borrar un ítem específico."""
        return (
            is_within_directory(path, quarantine_root) and
            not is_protected_path(path) and
            (not item.sha256 or _get_sha256(path) == item.sha256)
        )

    for item in items:
        stored_file = quarantine_root / item.stored_name
        if stored_file.exists() and _is_safe_to_purge(stored_file, item):
            try:
                stored_file.unlink()
                count += 1
            except (OSError, PermissionError):
                continue
    save_manifest([], base)
    return count


def total_quarantined_bytes(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el peso total en bytes de los archivos bajo cuarentena."""
    return sum(item.size_bytes for item in load_manifest(base))


def summarize(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte legible para el usuario detallando el estado de la cuarentena."""
    items = load_manifest(base)
    if not items:
        return ["La cuarentena está vacía."]
    
    total_bytes = sum(i.size_bytes for i in items)
    total_mb = round(total_bytes / (1024 * 1024), 2)
    
    lines = [f"{len(items)} archivo(s) en cuarentena — {total_mb} MB", ""]
    for item in items:
        lines.append(f"  [{item.item_id}] {Path(item.original_path).name} — {item.size_mb} MB")
        lines.append(f"      Motivo: {item.reason}")
        lines.append(f"      Origen: {item.original_path}")
        lines.append(f"      Aislado: {item.quarantined_at}")
    lines.append("")
    lines.append("Nada de esto se borró: se puede restaurar a su ubicación original.")
    return lines
