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

    def __post_init__(self) -> None:
        """Asegura la coherencia de tipos tras la instanciación de la dataclass."""
        self.size_bytes = int(self.size_bytes)
        if not self.item_id:
            raise ValueError("ID de ítem vacío o inválido")

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño del archivo en MB para reporte de interfaz."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la instancia a diccionario para persistencia JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional[QuarantineItem]:
        """
        Valida y reconstruye una instancia desde un dict del manifiesto.
        Retorna None si la estructura de datos es incompatible o corrupta.
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
        Valida que el archivo en cuarentena no haya sido alterado.
        Compara tamaño y hash SHA-256 contra los metadatos del manifiesto.
        """
        if not stored_path or not stored_path.is_file():
            return False
        try:
            stats = stored_path.stat()
            if stats.st_size != self.size_bytes:
                return False
            if self.sha256 and _get_sha256(stored_path) != self.sha256:
                return False
            return True
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """Genera hash SHA-256 mediante lectura por bloques para evitar consumo alto de RAM."""
    sha256_hash = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                sha256_hash.update(chunk)
    except (OSError, PermissionError):
        return ""
    return sha256_hash.hexdigest()


def _is_file_locked(path: Path) -> bool:
    """Intenta abrir un archivo en modo lectura/escritura para detectar bloqueos exclusivos."""
    try:
        with open(path, "rb+") as f:
            return False
    except (OSError, PermissionError):
        return True


def _safe_unlink(path: Path) -> bool:
    """Borrado atómico de un solo archivo con manejo explícito de errores de I/O."""
    try:
        if path.is_file():
            path.unlink()
            return True
        return False
    except (OSError, PermissionError):
        return False


def quarantine_dir(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Resuelve, normaliza y asegura la existencia del directorio de cuarentena."""
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path = Path(base).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    except (OSError, RuntimeError) as e:
        raise OSError(f"No se pudo preparar el directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ruta absoluta del manifiesto dentro de una carpeta base dada."""
    return base_dir / MANIFEST_NAME


def _validate_isolation_request(source_path: Path, dest_dir: Path) -> None:
    """Valida que una ruta pueda ser movida a cuarentena sin violar políticas de seguridad."""
    if ".." in source_path.parts or "\0" in str(source_path) or any(c in str(source_path.name) for c in "<>:\"|?*"):
        raise UnsafePathError(f"Ruta con caracteres maliciosos o navegación prohibida: {source_path.name}")
    
    if source_path.is_symlink() or (hasattr(source_path, 'is_junction') and source_path.is_junction()):
        raise UnsafePathError(f"Operación denegada en punto de reparse: {source_path}")

    if not source_path.is_file():
        raise UnsafePathError(f"Solo se permiten archivos regulares: {source_path}")
        
    if is_protected_path(source_path):
        raise UnsafePathError(f"Operación prohibida en ruta del sistema: {source_path}")
        
    if is_protected_path(dest_dir):
        raise UnsafePathError(f"Directorio de cuarentena protegido o inválido: {dest_dir}")
    
    if is_within_directory(source_path, dest_dir):
        raise UnsafePathError(f"El archivo ya reside en la carpeta de cuarentena: {source_path}")
        
    if source_path.drive != dest_dir.drive:
        raise UnsafePathError(f"Operación denegada: el archivo está en otro dispositivo o partición.")

    if os.path.exists(dest_dir) and os.path.exists(source_path):
        if os.path.samefile(source_path, dest_dir):
            raise UnsafePathError(f"Ruta de origen y destino colisionan mediante alias: {source_path}")

    ensure_safe_to_modify(source_path, allow_sensitive=True)
    
    if _is_file_locked(source_path):
        raise IOError(f"El archivo está en uso por otro proceso: {source_path}")


def load_manifest(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Carga el manifiesto de cuarentena con caché de memoria (mtime)."""
    try:
        base_path = quarantine_dir(base)
        path = _manifest_path(base_path)
    except (OSError, ValueError):
        return []

    base_str = str(base_path)
    try:
        current_mtime = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        current_mtime = 0.0

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
            
        if not isinstance(raw_data, list):
            return []

        items = [item for entry in raw_data if (item := QuarantineItem.from_dict(entry))]
        _manifest_cache[base_str] = (current_mtime, items)
        return items
    except (json.JSONDecodeError, OSError, PermissionError, ValueError):
        return []


def save_manifest(items: List[QuarantineItem], base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Guarda el estado actual del manifiesto mediante escritura atómica (swap de archivo)."""
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
    except (OSError, PermissionError) as e:
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except OSError: pass
        raise RuntimeError(f"Error fatal al persistir manifiesto: {e}")
    return target_path


def quarantine_file(
    source: Union[str, Path],
    reason: str = "Marcado como sospechoso",
    base: Union[str, Path] = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """Realiza la migración segura de un archivo a la carpeta de cuarentena."""
    if not source:
        raise ValueError("La ruta de origen no puede estar vacía.")
    
    source_path = normalize(source).resolve()
    dest_dir = quarantine_dir(base)
    
    if not source_path.exists():
        raise FileNotFoundError(f"El archivo de origen no existe: {source_path}")
    
    _validate_isolation_request(source_path, dest_dir)
    
    try:
        file_size = source_path.stat().st_size
    except OSError as e:
        raise OSError(f"Error al acceder a metadatos de archivo: {e}")

    usage = shutil.disk_usage(dest_dir)
    if usage.free < (file_size * 1.05):
        raise RuntimeError("Espacio insuficiente en disco para mover a cuarentena.")
        
    item_id = uuid.uuid4().hex[:12]
    safe_name = "".join(c for c in source_path.name if c.isalnum() or c in "._-")
    stored_name = f"{item_id}__{safe_name}"[:250] 
    destination = dest_dir / stored_name

    if is_protected_path(destination):
        raise UnsafePathError(f"Ruta de cuarentena final insegura: {destination}")

    if destination.exists():
        raise FileExistsError(f"Colisión de nombre en destino: {destination}")

    temp_dest = destination.with_suffix(".tmp")
    try:
        shutil.copy2(source_path, temp_dest)
        if temp_dest.stat().st_size != file_size:
            raise RuntimeError("La copia de seguridad no coincide en tamaño.")
        os.replace(temp_dest, destination)
        _safe_unlink(source_path)
    except Exception as e:
        if temp_dest.exists():
            _safe_unlink(temp_dest)
        raise RuntimeError(f"Falla crítica al procesar archivo: {e}")

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
        items = load_manifest(base)
        items.append(item)
        save_manifest(items, base)
        return item
    except Exception as e:
        if destination.exists():
            shutil.move(str(destination), str(source_path))
        raise RuntimeError(f"Error irrecuperable procesando metadatos: {e}")


def list_items(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna ítems en cuarentena, ordenados del más reciente al más antiguo."""
    return sorted(load_manifest(base), key=lambda i: i.quarantined_at, reverse=True)


def restore_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un archivo de la cuarentena a su ruta original."""
    if not item_id or not isinstance(item_id, str):
        raise ValueError("ID de ítem vacío o tipo incorrecto.")
    
    items = load_manifest(base)
    item_map = {i.item_id: i for i in items}
    match = item_map.get(item_id)
    
    if not match:
        raise KeyError(f"No se encontró ítem con ID: {item_id}")

    try:
        base_path = quarantine_dir(base)
        stored_file = (base_path / match.stored_name).resolve()
    except (OSError, ValueError) as e:
        raise RuntimeError(f"No se pudo acceder al directorio de cuarentena: {e}")
    
    if not stored_file.exists():
        items.remove(match)
        save_manifest(items, base)
        raise FileNotFoundError(f"El archivo no existe en la carpeta de cuarentena: {stored_file}")

    if not match.verify_integrity(stored_file):
        raise RuntimeError("Integridad comprometida: el archivo en cuarentena fue alterado.")

    destination = normalize(match.original_path).resolve()
    
    if destination.is_symlink() or (hasattr(destination, 'is_junction') and destination.is_junction()):
        raise UnsafePathError(f"Restauración denegada: {destination} es un enlace simbólico o unión.")

    if is_protected_path(destination):
        raise UnsafePathError(f"Restauración denegada: la ruta está protegida.")

    if destination.exists():
        raise FileExistsError(f"El destino ya está ocupado: {destination}")
        
    ensure_safe_to_modify(destination, allow_sensitive=False)

    try:
        parent_dir = destination.parent
        if not parent_dir.exists():
            parent_dir.mkdir(parents=True, exist_ok=True)
            
        shutil.move(str(stored_file), str(destination))
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Fallo durante la operación de restauración: {e}")

    items.remove(match)
    save_manifest(items, base)
    return destination


def purge_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> bool:
    """Elimina permanentemente un archivo de la cuarentena previa validación de integridad."""
    if not item_id or not isinstance(item_id, str):
        return False
        
    items = load_manifest(base)
    item_map = {i.item_id: i for i in items}
    match = item_map.get(item_id)
    
    if not match:
        return False

    quarantine_root = quarantine_dir(base)
    stored_file = quarantine_root / match.stored_name
    
    if is_protected_path(stored_file) or not is_within_directory(stored_file, quarantine_root):
        raise UnsafePathError(f"Intento de borrado fuera de cuarentena: {stored_file}")

    if not stored_file.exists() or not match.verify_integrity(stored_file):
        raise UnsafePathError(f"Integridad comprometida: no se borra un archivo sospechoso modificado.")
    
    ensure_safe_to_modify(stored_file, allow_sensitive=False)

    success = _safe_unlink(stored_file)
    if success:
        items.remove(match)
        save_manifest(items, base)
    return success


def _should_purge_file(entry: Path, quarantine_root: Path, item_map_by_name: Dict[str, QuarantineItem]) -> bool:
    """Verifica si un archivo en cuarentena es seguro para ser eliminado."""
    try:
        abs_entry = entry.resolve()
        if entry.name == MANIFEST_NAME or not is_within_directory(abs_entry, quarantine_root):
            return False
        
        # Solo purgar si el archivo es un ítem registrado en el manifiesto
        if entry.name not in item_map_by_name:
            return False
            
        if not item_map_by_name[entry.name].verify_integrity(abs_entry):
            return False
        
        ensure_safe_to_modify(abs_entry, allow_sensitive=False)
        return True
    except (UnsafePathError, OSError):
        return False


def purge_all(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Vacía la cuarentena, borrando solo archivos cuya integridad se puede verificar."""
    try:
        quarantine_root = quarantine_dir(base)
    except OSError:
        return 0
        
    if is_protected_path(quarantine_root):
        raise UnsafePathError("Operación denegada en ruta protegida.")
        
    ensure_safe_to_modify(quarantine_root, allow_sensitive=False)
    
    items = load_manifest(base)
    item_map_by_name: Dict[str, QuarantineItem] = {item.stored_name: item for item in items}
    
    count = 0
    remaining_items: List[QuarantineItem] = []
    
    try:
        for entry in quarantine_root.iterdir():
            if _should_purge_file(entry, quarantine_root, item_map_by_name):
                if _safe_unlink(entry):
                    count += 1
                else:
                    remaining_items.append(item_map_by_name[entry.name])
            elif entry.name in item_map_by_name:
                remaining_items.append(item_map_by_name[entry.name])
                
    except (OSError, PermissionError):
        pass
            
    if count > 0:
        save_manifest(remaining_items, base)
    return count


def total_quarantined_bytes(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el espacio total ocupado por los archivos en el manifiesto."""
    return sum(item.size_bytes for item in load_manifest(base))


def summarize(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte legible de los ítems en cuarentena para la interfaz."""
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
