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
        """Asegura la coherencia de los tipos de datos tras la instanciación."""
        try:
            self.size_bytes = int(self.size_bytes)
        except (ValueError, TypeError):
            self.size_bytes = 0
        if not self.item_id or not isinstance(self.item_id, str):
            raise ValueError("ID de ítem vacío o inválido")
        if not self.reason or not isinstance(self.reason, str):
            self.reason = "Sin motivo especificado"

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño del archivo en MB para su visualización en UI."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Convierte la instancia en un diccionario para su serialización."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional[QuarantineItem]:
        """Crea una instancia desde un diccionario validando campos obligatorios."""
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
        Previene la ejecución o restauración de archivos que hayan sido alterados mientras 
        estaban en cuarentena, invalidando la operación si el hash o tamaño no coinciden.
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
    """Calcula SHA256 mediante streaming para manejar archivos grandes eficientemente."""
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
    Intenta abrir un archivo para determinar si está bloqueado por otro proceso.
    Crucial antes de mover archivos para evitar dejar el sistema en un estado 
    inconsistente o perder datos durante la copia.
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
    Elimina un archivo tras validar seguridad.
    Esta función es el único punto de salida destructivo, asegurando que solo se 
    borre el archivo si este reside realmente en el sandbox autorizado.
    """
    try:
        if path.is_file() and not path.is_symlink() and is_safe_to_modify(path):
            path.unlink()
            return True
        return False
    except (OSError, PermissionError):
        return False

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """Genera un nombre de archivo seguro evitando colisiones y nombres reservados."""
    safe_chars = "".join(c for c in original_path.name if c.isalnum() or c in "._-")
    parts = safe_chars.split('.')
    name_base = parts[0] if parts[0] else "q_file"
    if name_base.upper() in WINDOWS_RESERVED_NAMES:
        name_base = f"q_{name_base}"
    safe_name = f"{name_base}.{parts[-1]}" if len(parts) > 1 else name_base
    return f"{item_id}__{safe_name}"[:250]


def quarantine_dir(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Retorna la ruta absoluta al directorio de cuarentena, creándolo si es necesario.
    Implementa validación de seguridad contra rutas protegidas del sistema antes de 
    cualquier operación de disco.
    """
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path = Path(base).expanduser().resolve()
        if path.name.strip() == "":
            raise UnsafePathError("Ruta de cuarentena inválida o vacía.")
        if is_protected_path(path):
            raise UnsafePathError("Directorio de cuarentena reside en ruta protegida.")
        path.mkdir(parents=True, exist_ok=True)
        return path
    except (OSError, RuntimeError) as e:
        raise OSError(f"No se pudo preparar el directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ruta completa al archivo de manifiesto JSON."""
    return (base_dir / MANIFEST_NAME).resolve()


def _is_valid_quarantine_path(path: Path, root: Path) -> TypeGuard[Path]:
    """Valida que la ruta esté estrictamente dentro del directorio raíz de cuarentena."""
    return is_within_directory(path, root)


def _check_windows_file_attributes(path_str: str) -> None:
    """
    Verifica atributos de archivo (sistema, oculto, solo lectura) en Windows.
    Previene manipular archivos críticos marcados como 'sistema' que, de ser 
    movidos, causarían inestabilidad inmediata en el OS.
    """
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
    Valida la sintaxis de la ruta para prevenir ataques de inyección o navegación 
    maliciosa de directorios (Directory Traversal).
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
    """Verifica pre-requisitos de seguridad antes de mover un archivo."""
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
    """Carga interna: lee el JSON de manifiesto para aprovechar la caché."""
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
                item = QuarantineItem.from_dict(entry)
                if item:
                    valid_items.append(item)
        return valid_items
    except (json.JSONDecodeError, OSError, PermissionError):
        return []

def load_manifest(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Carga la lista de ítems en cuarentena, permitiendo forzar el recargado."""
    base_path = quarantine_dir(base)
    base_str = str(base_path)
    if force_reload:
        _load_manifest_internal.cache_clear()
    return _load_manifest_internal(base_str)


def save_manifest(items: List[QuarantineItem], base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Persiste el manifiesto usando un archivo temporal para asegurar atomicidad.
    Garantiza que, ante un fallo eléctrico o de proceso, el manifiesto antiguo no 
    se corrompa, manteniendo la integridad de la base de datos de cuarentena.
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
    Copia un archivo al sandbox de forma atómica validando su hash y existencia.
    Asegura que el archivo en cuarentena sea una copia exacta antes de permitir 
    el borrado del original, mitigando pérdida de datos.
    """
    if source.is_symlink() or ":" in str(source):
        raise UnsafePathError("Operación denegada: origen no es archivo regular.")
    if destination.exists():
        raise RuntimeError("Conflicto de seguridad: el destino ya existe en el sandbox.")
    temp_dest = destination.parent / f".tmp_{uuid.uuid4().hex}"
    try:
        shutil.copy2(source, temp_dest)
        if temp_dest.stat().st_size != file_size:
            raise RuntimeError("Corrupción durante copia: tamaño mismatch.")
        file_hash = _get_sha256(temp_dest)
        if not file_hash:
            raise RuntimeError("Falla de integridad: no se pudo calcular hash.")
        os.replace(temp_dest, destination)
        return file_hash
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Error crítico durante el aislamiento: {e}")
    finally:
        if temp_dest.exists():
            _safe_unlink(temp_dest)


def quarantine_file(
    source: Union[str, Path],
    reason: str = "Marcado como sospechoso",
    base: Union[str, Path] = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """Orquesta el aislamiento de un archivo: valida, copia y actualiza el manifiesto."""
    if not source:
        raise ValueError("La ruta de origen no puede estar vacía.")
    source_path = Path(source).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"El archivo origen no existe: {source_path}")
    if not source_path.is_file():
        raise UnsafePathError("Solo se aceptan archivos regulares.")
    if source_path.is_symlink():
        raise UnsafePathError("No se permite cuarentena de enlaces simbólicos.")
    ensure_safe_to_modify(source_path, allow_sensitive=True)
    if str(source_path).startswith(("\\\\", "//")):
        raise UnsafePathError("No se permite cuarentena en recursos compartidos de red.")
    dest_dir = quarantine_dir(base)
    _validate_isolation_request(source_path, dest_dir)
    try:
        file_size = source_path.stat().st_size
    except OSError as e:
        raise RuntimeError(f"No se pudo determinar el tamaño del archivo origen: {e}")
    
    # Doble chequeo crítico ante condiciones de carrera
    if not source_path.exists():
        raise RuntimeError("El archivo origen desapareció antes de ser aislado.")

    usage = shutil.disk_usage(dest_dir)
    if usage.free < (file_size * 1.05):
        raise RuntimeError("Espacio insuficiente en disco.")
    item_id = uuid.uuid4().hex[:12]
    stored_name = _generate_safe_stored_name(source_path, item_id)
    destination = dest_dir / stored_name
    if destination.exists():
        raise UnsafePathError("Colisión de nombres detectada en el almacenamiento de cuarentena.")
    file_hash = _atomic_isolate_file(source_path, destination, file_size)
    try:
        if not destination.exists():
            raise RuntimeError("Fallo en la confirmación de aislamiento.")
        # Validación extra: confirmar que el original sigue ahí antes de borrar
        if source_path.exists():
            try:
                os.remove(source_path)
            except OSError as e:
                raise RuntimeError(f"Archivo copiado a cuarentena pero error al borrar original: {e}")
        
        try:
            items = load_manifest(base)
            item = QuarantineItem(
                item_id=item_id,
                original_path=str(source_path),
                stored_name=stored_name,
                size_bytes=file_size,
                reason=str(reason) if reason else "Sin motivo",
                quarantined_at=datetime.now().isoformat(timespec="seconds"),
                sha256=file_hash,
            )
            items.append(item)
            save_manifest(items, base)
            return item
        except (Exception, OSError) as e:
            raise RuntimeError(f"Aislamiento físico exitoso, pero fallo al actualizar manifiesto: {e}")
            
    except (Exception, OSError, ValueError) as e:
        if destination.exists() and not destination.exists() == False:
            _safe_unlink(destination)
        raise RuntimeError(f"Error al finalizar el aislamiento: {e}")


def list_items(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna los ítems en cuarentena, ordenados cronológicamente."""
    return sorted(load_manifest(base), key=lambda i: i.quarantined_at, reverse=True)


def restore_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Restaura un ítem desde el sandbox a su ubicación original tras verificar integridad.
    Implementa validaciones estrictas para asegurar que el usuario no pueda restaurar 
    un archivo a una ubicación protegida o sobrescribir archivos existentes peligrosamente.
    """
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ID de ítem inválido o vacío.")
    items = load_manifest(base)
    match = next((i for i in items if i.item_id == item_id), None)
    if not match:
        raise KeyError(f"No se encontró el ítem: {item_id}")
    base_path = quarantine_dir(base)
    stored_file = (base_path / match.stored_name).resolve()
    if not _is_valid_quarantine_path(stored_file, base_path):
        raise UnsafePathError("Acceso fuera del sandbox de cuarentena detectado.")
    if not stored_file.exists() or not stored_file.is_file():
        items.remove(match)
        save_manifest(items, base)
        raise FileNotFoundError("Archivo en cuarentena no localizado en disco.")
    if not match.verify_integrity(stored_file):
        raise RuntimeError("Integridad comprometida.")
    destination = Path(match.original_path).resolve()
    if is_protected_path(destination):
        raise UnsafePathError("Restauración denegada: destino protegido por sistema.")
    if destination.exists():
        raise FileExistsError(f"Error: el destino {destination} ya existe.")
    try:
        ensure_safe_to_modify(destination.parent, allow_sensitive=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(stored_file), str(destination))
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Fallo crítico durante la restauración: {e}")
    items.remove(match)
    save_manifest(items, base)
    return destination


def purge_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> bool:
    """
    Borra un archivo de la cuarentena permanentemente tras verificar su integridad.
    La verificación previa asegura que no se están borrando archivos en mal estado o 
    modificados fuera del flujo de control de la app.
    """
    if not isinstance(item_id, str) or not item_id.strip():
        return False
    items = load_manifest(base)
    match = next((i for i in items if i.item_id == item_id), None)
    if not match:
        return False
    quarantine_root = quarantine_dir(base)
    stored_file = (quarantine_root / match.stored_name).resolve()
    if not stored_file.exists():
        items.remove(match)
        save_manifest(items, base)
        return False
    if not match.verify_integrity(stored_file):
        raise UnsafePathError("Integridad comprometida: no se puede procesar el archivo.")
    if not _is_valid_quarantine_path(stored_file, quarantine_root):
        raise UnsafePathError("Intento de borrado fuera del sandbox.")
    if _safe_unlink(stored_file):
        items.remove(match)
        save_manifest(items, base)
        return True
    return False


def _is_item_purgable(entry: Path, item: QuarantineItem, root: Path) -> bool:
    """Helper interno para validar si un archivo en el sandbox es elegible para borrado."""
    return (
        _is_valid_quarantine_path(entry.resolve(), root) and
        entry.is_file() and
        item.verify_integrity(entry) and
        not _is_file_locked(entry)
    )


def purge_all(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Limpia el sandbox eliminando únicamente archivos verificables en el manifiesto."""
    quarantine_root = quarantine_dir(base)
    items = load_manifest(base)
    if not items:
        return 0
    item_map: Dict[str, QuarantineItem] = {i.stored_name: i for i in items}
    purged_ids = set()
    
    for entry in quarantine_root.iterdir():
        if entry.name in item_map:
            item = item_map[entry.name]
            if entry.exists() and _is_item_purgable(entry, item, quarantine_root):
                if _safe_unlink(entry):
                    purged_ids.add(item.item_id)
            
    if purged_ids:
        remaining_items = [i for i in load_manifest(base, force_reload=True) if i.item_id not in purged_ids]
        save_manifest(remaining_items, base)
        
    return len(purged_ids)


def total_quarantined_bytes(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el tamaño total en bytes de los archivos en cuarentena aprovechando el manifiesto cargado."""
    return sum(item.size_bytes for item in load_manifest(base))


def summarize(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte legible sobre el estado actual de la cuarentena."""
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
