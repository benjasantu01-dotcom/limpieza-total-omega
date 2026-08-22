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
        """Valida y normaliza los tipos de datos tras la inicialización del objeto."""
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
        """Retorna el tamaño del archivo en MB para su visualización en UI."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la instancia a un formato de diccionario plano."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional[QuarantineItem]:
        """Reconstruye una instancia desde un diccionario validando campos obligatorios."""
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
        Verifica que el archivo físico en el sandbox sea bit-a-bit idéntico al original.
        Utiliza el hash SHA256 almacenado en el manifiesto como fuente de verdad.
        """
        if not stored_path or not stored_path.is_file() or stored_path.is_symlink():
            return False
        try:
            stats = stored_path.stat()
            if stats.st_size != self.size_bytes:
                return False
            actual_hash = _get_sha256(stored_path)
            return bool(self.sha256 and actual_hash == self.sha256)
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """Calcula el hash SHA256 de un archivo mediante streaming para optimizar memoria."""
    sha256_hash = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(131072):
                sha256_hash.update(chunk)
    except (OSError, PermissionError):
        return ""
    return sha256_hash.hexdigest()


def _is_file_locked(path: Path) -> bool:
    """
    Determina si un archivo está bloqueado intentando abrirlo en modo lectura/escritura.
    Si el sistema operativo deniega el acceso, asumimos que está en uso.
    """
    if not isinstance(path, Path) or not path.exists():
        return False
    try:
        with open(path, "rb+") as f:
            return False
    except (IOError, OSError, PermissionError):
        return True


def _safe_unlink(path: Path) -> bool:
    """
    Ejecuta el borrado de un archivo tras confirmar que la ruta es segura y 
    no es un enlace simbólico, previniendo la eliminación accidental de datos fuera del sandbox.
    """
    try:
        if path.is_file() and not path.is_symlink() and is_safe_to_modify(path):
            path.unlink()
            return True
        return False
    except (OSError, PermissionError):
        return False

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """
    Genera un nombre de archivo único y seguro para el sandbox, sanitizando caracteres
    peligrosos y evitando nombres reservados de Windows.
    """
    safe_chars = "".join(c for c in original_path.name if c.isalnum() or c in "._-")
    parts = safe_chars.split('.')
    name_base = parts[0] if parts[0] else "q_file"
    if name_base.upper() in WINDOWS_RESERVED_NAMES:
        name_base = f"q_{name_base}"
    safe_name = f"{name_base}.{parts[-1]}" if len(parts) > 1 else name_base
    return f"{item_id}__{safe_name}"[:250]


def quarantine_dir(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Resuelve la ruta al directorio de cuarentena. Valida que no caiga en rutas de sistema 
    protegidas y crea el directorio si es necesario.
    """
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path = Path(base).expanduser().resolve()
        if not path.name.strip():
            raise UnsafePathError("Ruta de cuarentena inválida o vacía.")
        if is_protected_path(path):
            raise UnsafePathError("Directorio de cuarentena reside en ruta protegida.")
        path.mkdir(parents=True, exist_ok=True)
        return path
    except (OSError, RuntimeError) as e:
        raise OSError(f"No se pudo preparar el directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Obtiene la ruta absoluta al archivo JSON de manifiesto."""
    return (base_dir / MANIFEST_NAME).resolve()


def _is_valid_quarantine_path(path: Path, root: Path) -> TypeGuard[Path]:
    """Valida que una ruta resida estrictamente bajo el directorio raíz de cuarentena."""
    return is_within_directory(path, root)


def _check_windows_file_attributes(path_str: str) -> None:
    """Verifica atributos de SO en Windows (oculto, sistema) para evitar errores de acceso."""
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
    """
    Valida la sintaxis de la ruta para prevenir Directory Traversal, inyección
    o profundidad excesiva que podría causar fallos en las APIs de sistema.
    """
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
    """
    Filtro de seguridad exhaustivo previo a mover un archivo. Comprueba la integridad 
    de la ruta, permisos y que no se trate de una operación entre discos diferentes.
    """
    _check_path_syntax_integrity(source_path)
    _check_windows_file_attributes(str(source_path))
    resolved_source = source_path.resolve()
    if not resolved_source.is_file():
        raise UnsafePathError("Solo se aceptan archivos regulares.")
    if is_protected_path(resolved_source):
        raise UnsafePathError("Operación prohibida: la ruta origen está protegida por el sistema.")
    if is_protected_path(dest_dir) or is_protected_path(dest_dir.parent):
        raise UnsafePathError("Destino inválido: directorio de cuarentena en ruta protegida.")
    if _is_valid_quarantine_path(resolved_source, dest_dir):
        raise UnsafePathError("El archivo ya reside en el sandbox de cuarentena.")
    if resolved_source.drive.lower() != dest_dir.drive.lower():
        raise UnsafePathError("Operación prohibida: origen y destino en dispositivos diferentes.")
    ensure_safe_to_modify(resolved_source, allow_sensitive=True)
    if _is_file_locked(resolved_source):
        raise IOError("El archivo está en uso por otro proceso y no puede moverse.")

@lru_cache(maxsize=4)
def _load_manifest_internal(base_str: str) -> List[QuarantineItem]:
    """
    Carga interna: lee y deserializa el manifiesto JSON. El resultado se mantiene en 
    caché LRU para mejorar el rendimiento sin sacrificar la coherencia con el disco.
    """
    base_path = Path(base_str)
    path = _manifest_path(base_path)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        if not isinstance(raw_data, list):
            return []
        valid_items: List[QuarantineItem] = []
        for entry in raw_data:
            if isinstance(entry, dict):
                quarantine_item = QuarantineItem.from_dict(entry)
                if quarantine_item:
                    valid_items.append(quarantine_item)
        return valid_items
    except (json.JSONDecodeError, OSError, PermissionError):
        return []

def load_manifest(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Carga la lista de ítems en cuarentena, permitiendo refrescar el caché mediante force_reload."""
    base_path = quarantine_dir(base)
    base_str = str(base_path)
    if force_reload:
        _load_manifest_internal.cache_clear()
    return _load_manifest_internal(base_str)


def save_manifest(items: List[QuarantineItem], base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Persiste el manifiesto usando escritura atómica: escribe en un archivo temporal 
    y luego reemplaza el real, evitando corrupciones si ocurre un error durante el volcado.
    """
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista de ítems.")
    base_path = quarantine_dir(base)
    target_path = _manifest_path(base_path)
    temp_fd, temp_path = tempfile.mkstemp(dir=base_path, text=True)
    try:
        with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
            json.dump([item.to_dict() for item in items], f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target_path)
        _load_manifest_internal.cache_clear()
    except (OSError, PermissionError, TypeError, IOError) as e:
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except OSError: pass
        raise RuntimeError(f"Error fatal al persistir manifiesto de cuarentena: {e}")
    return target_path


def _atomic_isolate_file(source: Path, destination: Path, file_size: int) -> str:
    """
    Realiza la copia física al sandbox mediante un archivo temporal, verificando integridad 
    por hash y tamaño antes de confirmar la operación.
    """
    if source.is_symlink() or ":" in str(source):
        raise UnsafePathError("Origen no es archivo regular.")
    if destination.exists():
        raise FileExistsError(f"Conflicto: {destination.name} ya existe.")
    
    temp_dest = destination.parent / f".tmp_{uuid.uuid4().hex}"
    try:
        shutil.copy2(source, temp_dest)
        if temp_dest.stat().st_size != file_size:
            raise OSError("Corrupción durante copia: mismatch de tamaño.")
            
        file_hash = _get_sha256(temp_dest)
        if not file_hash:
            raise OSError("Falla de integridad: no se pudo calcular hash.")
            
        os.replace(temp_dest, destination)
        return file_hash
    except Exception:
        if temp_dest.exists():
            _safe_unlink(temp_dest)
        raise
    finally:
        if temp_dest.exists():
            _safe_unlink(temp_dest)


def quarantine_file(
    source: Union[str, Path],
    reason: str = "Marcado como sospechoso",
    base: Union[str, Path] = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """Orquesta el aislamiento de un archivo: valida, copia y registra en el manifiesto."""
    if not source:
        raise ValueError("La ruta de origen no puede estar vacía.")
    
    source_path = Path(source).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"El archivo origen no existe: {source_path}")
    if not source_path.is_file():
        raise UnsafePathError("Solo se aceptan archivos regulares, no directorios.")
    
    ensure_safe_to_modify(source_path, allow_sensitive=True)
    
    dest_dir = quarantine_dir(base)
    _validate_isolation_request(source_path, dest_dir)
    
    try:
        file_stats = source_path.stat()
        file_size = file_stats.st_size
    except OSError as e:
        raise OSError(f"No se pudo determinar el tamaño del archivo origen: {e}")
    
    usage = shutil.disk_usage(dest_dir)
    if usage.free < (file_size * 1.05):
        raise OSError("Espacio insuficiente en disco para el aislamiento.")
        
    item_id = uuid.uuid4().hex[:12]
    stored_name = _generate_safe_stored_name(source_path, item_id)
    destination = dest_dir / stored_name
    
    file_hash = _atomic_isolate_file(source_path, destination, file_size)
    
    try:
        items = load_manifest(base)
        quarantine_item = QuarantineItem(
            item_id=item_id,
            original_path=str(source_path),
            stored_name=stored_name,
            size_bytes=file_size,
            reason=str(reason) if reason else "Sin motivo",
            quarantined_at=datetime.now().isoformat(timespec="seconds"),
            sha256=file_hash,
        )
        items.append(quarantine_item)
        save_manifest(items, base)
        
        try:
            if source_path.exists():
                os.remove(source_path)
        except OSError:
            pass
            
        return quarantine_item
    except Exception as e:
        if destination.exists():
            _safe_unlink(destination)
        raise RuntimeError(f"Error al finalizar el aislamiento y persistir registro: {e}")


def list_items(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna los ítems en cuarentena, ordenados cronológicamente."""
    return sorted(load_manifest(base), key=lambda item: item.quarantined_at, reverse=True)


def restore_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un ítem desde el sandbox a su ubicación original previa validación de integridad."""
    if not item_id or not isinstance(item_id, str):
        raise ValueError("ID de ítem inválido o vacío.")
        
    items = load_manifest(base)
    quarantine_item = next((item for item in items if item.item_id == item_id), None)
    if not quarantine_item:
        raise KeyError(f"No se encontró el ítem: {item_id}")
    
    base_path = quarantine_dir(base)
    stored_file = (base_path / quarantine_item.stored_name).resolve()
    
    if not stored_file.exists():
        raise FileNotFoundError("Archivo en cuarentena no localizado en disco.")
        
    if not quarantine_item.verify_integrity(stored_file):
        raise RuntimeError("Integridad comprometida: el hash no coincide.")
    
    destination = Path(quarantine_item.original_path).absolute()
    _check_path_syntax_integrity(destination)
    
    if is_protected_path(destination):
        raise UnsafePathError("Restauración denegada: destino protegido por sistema.")
    if destination.exists():
        raise FileExistsError(f"Error: el destino {destination} ya existe.")
    
    try:
        if not destination.parent.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            
        os.replace(str(stored_file), str(destination))
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Fallo crítico durante la restauración: {e}")
        
    items.remove(quarantine_item)
    save_manifest(items, base)
    return destination


def purge_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> bool:
    """Elimina permanentemente un ítem de la cuarentena tras verificar su integridad."""
    if not isinstance(item_id, str) or not item_id.strip():
        return False
    items = load_manifest(base)
    quarantine_item = next((item for item in items if item.item_id == item_id), None)
    if not quarantine_item:
        return False
    quarantine_root = quarantine_dir(base)
    stored_file = (quarantine_root / quarantine_item.stored_name).resolve()
    if not stored_file.exists():
        items.remove(quarantine_item)
        save_manifest(items, base)
        return False
    if not quarantine_item.verify_integrity(stored_file):
        raise UnsafePathError("Integridad comprometida: no se puede procesar el archivo.")
    if not _is_valid_quarantine_path(stored_file, quarantine_root):
        raise UnsafePathError("Intento de borrado fuera del sandbox.")
    if _safe_unlink(stored_file):
        items.remove(quarantine_item)
        save_manifest(items, base)
        return True
    return False


def _is_item_purgable(file_path: Path, item: QuarantineItem, root: Path) -> bool:
    """Verifica si un archivo en el sandbox cumple requisitos para ser borrado."""
    return (
        _is_valid_quarantine_path(file_path.resolve(), root) and
        file_path.is_file() and
        item.verify_integrity(file_path) and
        not _is_file_locked(file_path)
    )


def purge_all(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Elimina todos los archivos verificables del sandbox y actualiza el manifiesto."""
    quarantine_root = quarantine_dir(base)
    items = load_manifest(base)
    if not items:
        return 0
    
    item_map = {item.stored_name: item for item in items}
    purged_ids = set()
    
    for file_path in quarantine_root.iterdir():
        if file_path.name in item_map:
            item = item_map[file_path.name]
            if _is_item_purgable(file_path, item, quarantine_root):
                if _safe_unlink(file_path):
                    purged_ids.add(item.item_id)
            
    if purged_ids:
        remaining_items = [i for i in items if i.item_id not in purged_ids]
        save_manifest(remaining_items, base)
        
    return len(purged_ids)


def total_quarantined_bytes(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el uso total de almacenamiento de la cuarentena."""
    return sum(item.size_bytes for item in load_manifest(base))


def summarize(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte descriptivo legible sobre el estado de la cuarentena."""
    items = load_manifest(base)
    if not items:
        return ["La cuarentena está vacía."]
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
