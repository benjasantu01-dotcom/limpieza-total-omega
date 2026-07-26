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
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Union

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


@dataclass
class QuarantineItem:
    """Un archivo aislado, con todo lo necesario para devolverlo a su lugar."""
    item_id: str
    original_path: str
    stored_name: str
    size_bytes: int
    reason: str
    quarantined_at: str

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> dict:
        return asdict(self)


def quarantine_dir(base: str | Path = DEFAULT_QUARANTINE_DIR) -> Path:
    """Devuelve la carpeta de cuarentena, creándola si no existe."""
    path = Path(base).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path(base: str | Path = DEFAULT_QUARANTINE_DIR) -> Path:
    return quarantine_dir(base) / MANIFEST_NAME


def load_manifest(base: str | Path = DEFAULT_QUARANTINE_DIR) -> list[QuarantineItem]:
    """Lee el manifiesto. Devuelve lista vacía si no existe o está corrupto.

    Un manifiesto ilegible no debe impedir usar la app: se prefiere perder
    el índice (los archivos siguen ahí) antes que romper la interfaz.
    """
    path = _manifest_path(base)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items: list[QuarantineItem] = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            items.append(QuarantineItem(**entry))
        except TypeError:
            continue  # entrada con formato viejo o inválido: se ignora
    return items


def save_manifest(items: List[QuarantineItem], base: str | Path = DEFAULT_QUARANTINE_DIR) -> Path:
    """Serializa la lista de items a JSON y guarda el archivo de manifiesto en disco."""
    path = _manifest_path(base)
    path.write_text(
        json.dumps([item.to_dict() for item in items], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def quarantine_file(
    source: str | Path,
    reason: str = "Marcado como sospechoso",
    base: str | Path = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """Mueve un archivo a la cuarentena y lo anota en el manifiesto.

    Lanza UnsafePathError si el origen está en una ruta protegida, y
    FileNotFoundError si no existe. Realiza un test de escritura para 
    asegurar que el archivo no está bloqueado por otro proceso.
    """
    origin = normalize(source)
    if not origin.is_file():
        raise FileNotFoundError(f"No existe el archivo a poner en cuarentena: {origin}")
    
    # Verificación de exclusividad: intentar abrir el archivo en modo exclusivo
    try:
        with open(origin, "ab"):
            pass
    except IOError:
        raise IOError(f"El archivo está en uso por otro proceso: {origin}")

    ensure_safe_to_modify(origin, allow_sensitive=True)

    destination_dir = quarantine_dir(base)
    item_id = uuid.uuid4().hex[:12]
    stored_name = f"{item_id}__{origin.name}"
    destination = destination_dir / stored_name

    size = origin.stat().st_size
    shutil.move(str(origin), str(destination))

    item = QuarantineItem(
        item_id=item_id,
        original_path=str(origin),
        stored_name=stored_name,
        size_bytes=size,
        reason=reason,
        quarantined_at=datetime.now().isoformat(timespec="seconds"),
    )
    items = load_manifest(base)
    items.append(item)
    save_manifest(items, base)
    return item


def list_items(base: str | Path = DEFAULT_QUARANTINE_DIR) -> list[QuarantineItem]:
    """Archivos actualmente en cuarentena, del más reciente al más viejo."""
    items = load_manifest(base)
    items.sort(key=lambda i: i.quarantined_at, reverse=True)
    return items


def restore_item(item_id: str, base: str | Path = DEFAULT_QUARANTINE_DIR) -> Path:
    """Devuelve un archivo a su ubicación original y lo saca del manifiesto.

    Valida el destino antes de escribir: si el manifiesto fue manipulado
    para apuntar a una carpeta de sistema, la restauración se bloquea.
    """
    items = load_manifest(base)
    match = next((i for i in items if i.item_id == item_id), None)
    if match is None:
        raise KeyError(f"No hay ningún elemento en cuarentena con id '{item_id}'.")

    stored = quarantine_dir(base) / match.stored_name
    if not stored.exists():
        raise FileNotFoundError(f"El archivo en cuarentena ya no está: {stored}")

    destination = normalize(match.original_path)
    if destination.exists():
        raise FileExistsError(f"Restauración abortada: El archivo ya existe en '{destination}'")
        
    if is_protected_path(destination.parent):
        raise UnsafePathError(
            f"Restauración bloqueada: '{destination.parent}' es una ruta de sistema protegida."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(stored), str(destination))

    save_manifest([i for i in items if i.item_id != item_id], base)
    return destination


def purge_item(item_id: str, base: str | Path = DEFAULT_QUARANTINE_DIR) -> bool:
    """Borra definitivamente UN elemento de la cuarentena.

    Verifica la integridad de la ruta para impedir desbordamientos fuera de 
    la carpeta de cuarentena, incluso si el manifiesto fue corrompido.
    """
    items = load_manifest(base)
    match = next((i for i in items if i.item_id == item_id), None)
    if match is None:
        return False

    root = quarantine_dir(base)
    stored = normalize(root / match.stored_name)
    if not is_within_directory(stored, root):
        raise UnsafePathError(f"Borrado bloqueado: '{stored}' está fuera de la cuarentena.")

    if stored.is_file():
        stored.unlink()
    save_manifest([i for i in items if i.item_id != item_id], base)
    return True


def purge_all(base: str | Path = DEFAULT_QUARANTINE_DIR) -> int:
    """Vacía la cuarentena eliminando todos los archivos registrados.

    Itera sobre el manifiesto y borra solo si la ruta confirmada pertenece 
    a la carpeta de cuarentena. Devuelve el contador de eliminaciones.
    """
    root = quarantine_dir(base)
    items = load_manifest(base)
    borrados = 0
    for item in items:
        stored = normalize(root / item.stored_name)
        if not is_within_directory(stored, root):
            continue  # nunca borrar fuera de la cuarentena
        try:
            if stored.is_file():
                stored.unlink()
                borrados += 1
        except (OSError, PermissionError):
            continue
    save_manifest([], base)
    return borrados


def total_quarantined_bytes(base: str | Path = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el peso total en bytes de todos los archivos aislados."""
    return sum(item.size_bytes for item in load_manifest(base))


def summarize(base: str | Path = DEFAULT_QUARANTINE_DIR) -> list[str]:
    """Genera una lista de cadenas con el reporte legible de la cuarentena."""
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
