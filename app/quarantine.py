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

# Cache de memoria: { str(path_carpeta_base): (mtime_del_archivo_manifest, lista_de_items) }
_manifest_cache: Dict[str, Tuple[float, List[QuarantineItem]]] = {}

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
        """Normaliza los tipos de datos tras la inicialización de la dataclass."""
        try:
            self.size_bytes = int(self.size_bytes)
        except (ValueError, TypeError):
            self.size_bytes = 0
        if not self.item_id:
            raise ValueError("ID de ítem vacío o inválido")

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño del archivo en Megabytes (MB) con precisión de dos decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Convierte la instancia en un diccionario plano listo para ser serializado como JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional[QuarantineItem]:
        """
        Intenta reconstruir un objeto QuarantineItem desde un diccionario.
        
        Args:
            data: Diccionario con las claves esperadas del esquema de manifiesto.
        Returns:
            Instancia válida si los datos son íntegros, None si hay corrupción o campos faltantes.
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
        Realiza una validación de seguridad contra el archivo físico en el sandbox.
        Compara tamaño y hash SHA-256 almacenado contra el archivo actual en disco.
        """
        if not stored_path or not stored_path.is_file():
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
    """Calcula el hash SHA-256 de un archivo mediante lectura por bloques (64KB) para eficiencia de memoria."""
    sha256_hash = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                sha256_hash.update(chunk)
    except (OSError, PermissionError):
        return ""
    return sha256_hash.hexdigest()


def _is_file_locked(path: Path) -> bool:
    """Verifica si el archivo está siendo usado por otro proceso mediante un intento de acceso exclusivo."""
    try:
        with open(path, "rb+") as f:
            return False
    except (OSError, PermissionError):
        return True


def _safe_unlink(path: Path) -> bool:
    """Elimina un archivo asegurando que la operación solo afecte a archivos y no a directorios."""
    try:
        if path.is_file():
            path.unlink()
            return True
        return False
    except (OSError, PermissionError):
        return False


def quarantine_dir(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Resuelve la ruta de cuarentena y garantiza su creación.
    Raises:
        OSError: Si no se puede acceder o crear el directorio.
        ValueError: Si la ruta base está vacía.
    """
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path = Path(base).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    except (OSError, RuntimeError) as e:
        raise OSError(f"No se pudo preparar el directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Genera la ruta absoluta del archivo manifest.json dentro del directorio dado."""
    return (base_dir / MANIFEST_NAME).resolve()


def _is_valid_quarantine_path(path: Path, root: Path) -> TypeGuard[Path]:
    """Valida mediante TypeGuard que una ruta se mantenga estrictamente dentro del sandbox asignado."""
    return is_within_directory(path, root)


def _validate_isolation_request(source_path: Path, dest_dir: Path) -> None:
    """
    Ejecuta una serie de chequeos preventivos antes de mover un archivo a cuarentena.
    Raises:
        UnsafePathError: Si la ruta no cumple los requisitos de seguridad.
        IOError: Si el archivo está bloqueado por otro proceso.
    """
    if len(source_path.parts) > 32:
        raise UnsafePathError("Profundidad de ruta excesiva: riesgo de desbordamiento.")

    if ":" in source_path.name.replace(source_path.drive, ""):
        raise UnsafePathError(f"Ruta con flujos de datos alternos no permitida: {source_path}")

    if ".." in source_path.parts or "\0" in str(source_path) or any(c in str(source_path.name) for c in "<>\"|?*"):
        raise UnsafePathError(f"Ruta con caracteres maliciosos o navegación prohibida: {source_path.name}")
    
    if source_path.is_symlink() or (hasattr(source_path, 'is_junction') and source_path.is_junction()):
        raise UnsafePathError(f"Operación denegada en punto de reparse: {source_path}")

    try:
        if os.name == 'nt':
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(source_path))
            if attrs != -1 and (attrs & 0x02 or attrs & 0x04): 
                raise UnsafePathError("Prohibido procesar archivos con atributos de sistema/ocultos.")
    except (OSError, AttributeError):
        pass

    if not source_path.is_file():
        raise UnsafePathError(f"Solo se permiten archivos regulares: {source_path}")
        
    if is_protected_path(source_path):
        raise UnsafePathError(f"Operación prohibida en ruta del sistema: {source_path}")
        
    if is_protected_path(dest_dir):
        raise UnsafePathError(f"Directorio de cuarentena protegido o inválido: {dest_dir}")
    
    if _is_valid_quarantine_path(source_path, dest_dir):
        raise UnsafePathError(f"El archivo ya reside en la carpeta de cuarentena: {source_path}")
    
    try:
        if source_path.drive != dest_dir.drive:
            raise UnsafePathError(f"Operación denegada: el archivo está en otro dispositivo o partición.")
    except (OSError, AttributeError):
        pass

    try:
        if os.path.exists(dest_dir) and os.path.exists(source_path):
            if os.path.samefile(source_path, dest_dir):
                raise UnsafePathError(f"Ruta de origen y destino colisionan mediante alias: {source_path}")
    except OSError:
        pass

    ensure_safe_to_modify(source_path, allow_sensitive=True)
    
    if _is_file_locked(source_path):
        raise IOError(f"El archivo está en uso por otro proceso: {source_path}")


def load_manifest(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Carga el manifiesto desde JSON, implementando una caché basada en mtime para reducir E/S."""
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
    """
    Persiste la lista de ítems mediante escritura atómica.
    Raises:
        RuntimeError: Si la escritura o la persistencia fallan.
    """
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista de ítems.")
        
    base_path = quarantine_dir(base)
    target_path = _manifest_path(base_path)
    
    temp_fd, temp_path = tempfile.mkstemp(dir=base_path, text=True)
    try:
        with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
            json.dump([item.to_dict() for item in items], f, indent=2, ensure_ascii=False)
        os.replace(temp_path, target_path)
        _manifest_cache[str(base_path)] = (target_path.stat().st_mtime, items)
    except (OSError, PermissionError, TypeError, IOError) as e:
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
    """
    Mueve un archivo a la carpeta de cuarentena y registra sus metadatos.
    Raises:
        FileNotFoundError: Si el archivo origen no existe.
        UnsafePathError: Si la ruta infringe políticas de seguridad.
        RuntimeError: Si fallan las operaciones de disco o integridad.
    """
    if not source:
        raise ValueError("La ruta de origen no puede estar vacía.")
    
    source_path = Path(source).resolve()
    
    ensure_safe_to_modify(source_path, allow_sensitive=True)
    
    if str(source_path).startswith(("\\\\", "//")):
        raise UnsafePathError("No se permite cuarentena en rutas de red (UNC).")
        
    dest_dir = quarantine_dir(base)
    
    if not source_path.exists():
        raise FileNotFoundError(f"El archivo de origen no existe: {source_path}")
    
    _validate_isolation_request(source_path, dest_dir)
    
    if not os.access(dest_dir, os.W_OK):
        raise PermissionError(f"Directorio de cuarentena sin permisos de escritura: {dest_dir}")
    
    try:
        file_size = source_path.stat().st_size
    except OSError as e:
        raise RuntimeError(f"Error al acceder a metadatos de origen: {e}")

    try:
        usage = shutil.disk_usage(dest_dir)
        if usage.free < (file_size * 1.05):
            raise RuntimeError("Espacio insuficiente en disco para cuarentena.")
    except OSError as e:
        raise RuntimeError(f"Falla al verificar estado de disco: {e}")
        
    item_id = uuid.uuid4().hex[:12]
    safe_name = "".join(c for c in source_path.name if c.isalnum() or c in "._-")
    
    name_no_ext = Path(safe_name).stem.upper()
    if name_no_ext in ("CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"):
        safe_name = f"q_{safe_name}"

    stored_name = f"{item_id}__{safe_name}"[:250] 
    destination = dest_dir / stored_name

    if destination.exists():
        raise UnsafePathError(f"Colisión de nombre en destino: {destination}")
    
    temp_dest = dest_dir / f"{item_id}.tmp"
    try:
        shutil.copy2(source_path, temp_dest)
        
        # Validación de integridad post-copia antes de eliminar el original
        file_hash = _get_sha256(temp_dest)
        if not file_hash or temp_dest.stat().st_size != file_size:
            raise RuntimeError("Integridad fallida: el archivo en sandbox no coincide con el origen.")
            
        os.replace(temp_dest, destination)
        os.remove(source_path)
    except (OSError, PermissionError) as e:
        if temp_dest.exists():
            _safe_unlink(temp_dest)
        if destination.exists():
            _safe_unlink(destination)
        raise RuntimeError(f"Falla crítica al mover archivo: {e}")
    finally:
        if temp_dest.exists():
            _safe_unlink(temp_dest)

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
        raise RuntimeError(f"Error irrecuperable procesando metadatos: {e}")


def list_items(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna ítems en cuarentena, ordenados cronológicamente descendente."""
    return sorted(load_manifest(base), key=lambda i: i.quarantined_at, reverse=True)


def restore_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Restaura un archivo hacia su ubicación original tras validar su integridad.
    Raises:
        KeyError: Si no existe el ítem.
        FileNotFoundError: Si no se localiza el archivo físico.
        RuntimeError: Si fallan los chequeos de integridad o restauración.
        UnsafePathError: Si la ruta de destino no es segura.
    """
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

    destination = Path(match.original_path).resolve()
    
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
        os.replace(str(stored_file), str(destination))
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Fallo durante la operación de restauración: {e}")

    items.remove(match)
    save_manifest(items, base)
    return destination


def purge_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> bool:
    """
    Elimina permanentemente un archivo de la cuarentena previa validación de seguridad.
    Raises:
        UnsafePathError: Si la integridad o las rutas de seguridad fallan.
    """
    if not item_id or not isinstance(item_id, str):
        return False
        
    items = load_manifest(base)
    item_map = {i.item_id: i for i in items}
    match = item_map.get(item_id)
    
    if not match:
        return False

    quarantine_root = quarantine_dir(base)
    stored_file = (quarantine_root / match.stored_name).resolve()
    
    if not _is_valid_quarantine_path(stored_file, quarantine_root):
        raise UnsafePathError(f"Intento de borrado fuera de cuarentena: {stored_file}")

    if not stored_file.exists() or not match.verify_integrity(stored_file):
        raise UnsafePathError(f"Integridad comprometida: no se borra un archivo sospechoso modificado.")
    
    ensure_safe_to_modify(stored_file, allow_sensitive=False)

    success = _safe_unlink(stored_file)
    if success:
        items.remove(match)
        save_manifest(items, base)
    return success


def purge_all(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Vacía la carpeta de cuarentena procesando solo los archivos validados por manifiesto."""
    try:
        quarantine_root = quarantine_dir(base)
    except (OSError, ValueError):
        return 0
    
    items = load_manifest(base)
    item_map: Dict[str, QuarantineItem] = {item.stored_name: item for item in items}
    
    purged_count = 0
    items_to_keep: List[QuarantineItem] = []
    
    try:
        for entry in quarantine_root.iterdir():
            if entry.name == MANIFEST_NAME or not entry.is_file():
                continue
            
            # Solo procesamos si el archivo está en el manifiesto
            if entry.name in item_map:
                item = item_map[entry.name]
                if item.verify_integrity(entry):
                    if _safe_unlink(entry):
                        purged_count += 1
                        continue # Ya fue purgado
                items_to_keep.append(item)
            else:
                # Archivo huérfano (no en manifiesto), no debe ser borrado automáticamente
                pass
                
        if purged_count > 0:
            save_manifest(items_to_keep, base)
    except (OSError, PermissionError):
        pass
        
    return purged_count


def total_quarantined_bytes(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el espacio total ocupado por los archivos registrados en el manifiesto."""
    return sum(item.size_bytes for item in load_manifest(base))


def summarize(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte legible de los ítems en cuarentena para la interfaz gráfica."""
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
