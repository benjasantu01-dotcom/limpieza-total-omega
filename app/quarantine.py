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
        """Normaliza tipos de datos internos tras la creación de la instancia."""
        try:
            self.size_bytes = int(self.size_bytes)
        except (ValueError, TypeError):
            self.size_bytes = 0
        if not self.item_id:
            raise ValueError("ID de ítem vacío o inválido")

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño en Megabytes con precisión de dos decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Convierte los datos del ítem a un diccionario plano para persistencia JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional[QuarantineItem]:
        """
        Valida y reconstruye una instancia desde datos JSON.
        
        Args:
            data: Diccionario deserializado del manifiesto.
        Returns:
            Instancia si el esquema es válido, None en caso contrario.
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
        Verifica que el archivo físico en cuarentena coincida con sus metadatos.
        Confirma tamaño y hash SHA-256 (si este último está presente).
        """
        if not stored_path or not stored_path.is_file():
            return False
        try:
            stats = stored_path.stat()
            if stats.st_size != self.size_bytes:
                return False
            actual_hash = _get_sha256(stored_path)
            # Solo exigimos coincidencia de hash si el ítem tenía uno registrado
            if self.sha256 and actual_hash != self.sha256:
                return False
            return actual_hash != ""
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """Calcula el hash SHA-256 de un archivo en bloques para proteger la memoria RAM."""
    sha256_hash = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                sha256_hash.update(chunk)
    except (OSError, PermissionError):
        return ""
    return sha256_hash.hexdigest()


def _is_file_locked(path: Path) -> bool:
    """Determina si un archivo está en uso exclusivo intentando abrirlo en modo escritura."""
    try:
        with open(path, "rb+") as f:
            return False
    except (OSError, PermissionError):
        return True


def _safe_unlink(path: Path) -> bool:
    """Elimina de forma segura un archivo regular, evitando directorios."""
    try:
        if path.is_file():
            path.unlink()
            return True
        return False
    except (OSError, PermissionError):
        return False


def quarantine_dir(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Resuelve la ruta absoluta al directorio de cuarentena, creándolo si no existe."""
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path = Path(base).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    except (OSError, RuntimeError) as e:
        raise OSError(f"No se pudo preparar el directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Obtiene la ruta al archivo de manifiesto JSON dentro de un directorio base."""
    return (base_dir / MANIFEST_NAME).resolve()


def _is_valid_quarantine_path(path: Path, root: Path) -> TypeGuard[Path]:
    """Valida que la ruta pertenezca efectivamente al directorio de cuarentena."""
    return is_within_directory(path, root)


def _validate_isolation_request(source_path: Path, dest_dir: Path) -> None:
    """
    Ejecuta todas las comprobaciones de seguridad antes de mover archivos al sandbox.
    
    Verifica: profundidad de ruta, caracteres peligrosos, enlaces simbólicos,
    atributos de sistema (Windows), protección de rutas y consistencia de unidades.
    """
    # 1. Comprobación de longitud y estructura
    if len(source_path.parts) > 32:
        raise UnsafePathError("Profundidad de ruta excesiva: riesgo de desbordamiento.")
    if ":" in source_path.name.replace(source_path.drive, ""):
        raise UnsafePathError("Ruta con flujos de datos alternos no permitida.")
    if ".." in source_path.parts or "\0" in str(source_path) or any(c in str(source_path.name) for c in "<>\"|?*"):
        raise UnsafePathError("Ruta con caracteres prohibidos o navegación no permitida.")
    
    # 2. Comprobación de tipos de archivo peligrosos
    if source_path.is_symlink() or (hasattr(source_path, 'is_junction') and source_path.is_junction()):
        raise UnsafePathError("Operación denegada en enlace o punto de reparse.")

    # 3. Comprobación de atributos en Windows (Atributos de sistema/ocultos/solo lectura)
    try:
        if os.name == 'nt':
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(source_path))
            if attrs != -1 and (attrs & 0x02 or attrs & 0x04): 
                raise UnsafePathError("No se permite procesar archivos con atributos de sistema/ocultos.")
            if attrs != -1 and (attrs & 0x01):
                raise UnsafePathError("Archivo protegido contra escritura (solo lectura).")
    except (OSError, AttributeError):
        pass

    # 4. Validaciones de estado lógico
    if not source_path.is_file():
        raise UnsafePathError("Solo se aceptan archivos regulares.")
    if is_protected_path(source_path):
        raise UnsafePathError("Operación prohibida: la ruta está protegida por el sistema.")
    if is_protected_path(dest_dir):
        raise UnsafePathError("Destino inválido: directorio de cuarentena protegido.")
    if _is_valid_quarantine_path(source_path, dest_dir):
        raise UnsafePathError("El archivo ya reside en el sandbox de cuarentena.")
    
    # 5. Comprobaciones de entorno de disco
    try:
        if source_path.drive != dest_dir.drive:
            raise UnsafePathError("Operación prohibida entre dispositivos distintos.")
    except (OSError, AttributeError):
        pass

    try:
        if os.path.exists(dest_dir) and os.path.exists(source_path):
            if os.path.samefile(source_path, dest_dir):
                raise UnsafePathError("Colisión de rutas mediante alias del sistema.")
    except OSError:
        pass

    # 6. Verificaciones finales de permisos y bloqueo
    ensure_safe_to_modify(source_path, allow_sensitive=True)
    if _is_file_locked(source_path):
        raise IOError("El archivo está en uso por otro proceso y no puede moverse.")


def load_manifest(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Carga el manifiesto de cuarentena, usando una caché interna basada en mtime."""
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
            _manifest_cache[base_str] = (current_mtime, [])
            return []

        items = [item for entry in raw_data if (item := QuarantineItem.from_dict(entry))]
        _manifest_cache[base_str] = (current_mtime, items)
        return items
    except (json.JSONDecodeError, OSError, PermissionError, ValueError):
        _manifest_cache[base_str] = (current_mtime, [])
        return []


def save_manifest(items: List[QuarantineItem], base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Guarda el manifiesto utilizando una escritura atómica mediante un archivo temporal."""
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
        raise RuntimeError(f"Error fatal al persistir manifiesto de cuarentena: {e}")
    return target_path


def quarantine_file(
    source: Union[str, Path],
    reason: str = "Marcado como sospechoso",
    base: Union[str, Path] = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """
    Aísla un archivo en cuarentena tras validar integridad y permisos.
    
    El proceso es: validación -> copiado a .tmp -> hash -> reemplazo -> borrado original.
    """
    if not source:
        raise ValueError("La ruta de origen no puede estar vacía.")
    
    source_path = Path(source).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Archivo no encontrado o inválido: {source_path}")
        
    ensure_safe_to_modify(source_path, allow_sensitive=True)
    
    if str(source_path).startswith(("\\\\", "//")):
        raise UnsafePathError("No se permite cuarentena en recursos compartidos de red.")
        
    dest_dir = quarantine_dir(base)
    _validate_isolation_request(source_path, dest_dir)
    
    if not os.access(dest_dir, os.W_OK):
        raise PermissionError("Sin permisos de escritura en la carpeta de cuarentena.")
    
    file_size = source_path.stat().st_size
    usage = shutil.disk_usage(dest_dir)
    if usage.free < (file_size * 1.05):
        raise RuntimeError("Espacio insuficiente en disco para realizar la cuarentena.")
        
    item_id = uuid.uuid4().hex[:12]
    safe_name = "".join(c for c in source_path.name if c.isalnum() or c in "._-")
    
    # Prevenir nombres reservados en Windows
    if safe_name.upper().split('.')[0] in ("CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"):
        safe_name = f"q_{safe_name}"

    stored_name = f"{item_id}__{safe_name}"[:250] 
    destination = dest_dir / stored_name

    if destination.exists():
        raise UnsafePathError("Colisión de nombres: el destino ya existe.")
    
    temp_dest = dest_dir / f"{item_id}.tmp"
    try:
        shutil.copy2(source_path, temp_dest)
        file_hash = _get_sha256(temp_dest)
        if not file_hash or temp_dest.stat().st_size != file_size:
            raise RuntimeError("Falla de integridad: el archivo en sandbox no coincide con el original.")
        os.replace(temp_dest, destination)
        os.remove(source_path)
    except (OSError, PermissionError) as e:
        if temp_dest.exists(): _safe_unlink(temp_dest)
        if destination.exists(): _safe_unlink(destination)
        raise RuntimeError(f"Error crítico durante el aislamiento: {e}")
    finally:
        if temp_dest.exists(): _safe_unlink(temp_dest)

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
        raise RuntimeError(f"Error al procesar el manifiesto: {e}")


def list_items(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna los ítems en cuarentena, ordenados del más reciente al más antiguo."""
    return sorted(load_manifest(base), key=lambda i: i.quarantined_at, reverse=True)


def restore_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un archivo al sistema de archivos original tras verificar su integridad."""
    if not item_id or not isinstance(item_id, str):
        raise ValueError("ID de ítem inválido.")
    
    items = load_manifest(base)
    item_map = {i.item_id: i for i in items}
    match = item_map.get(item_id)
    
    if not match:
        raise KeyError(f"No se encontró el ítem: {item_id}")

    try:
        base_path = quarantine_dir(base)
        stored_file = (base_path / match.stored_name).resolve()
    except (OSError, ValueError) as e:
        raise RuntimeError(f"Falla de acceso al sandbox: {e}")
    
    if not stored_file.exists():
        items.remove(match)
        save_manifest(items, base)
        raise FileNotFoundError("Archivo no encontrado en cuarentena.")

    if not match.verify_integrity(stored_file):
        raise RuntimeError("Integridad comprometida: el archivo en cuarentena fue modificado.")

    destination = Path(match.original_path).resolve()
    if destination.is_symlink() or (hasattr(destination, 'is_junction') and destination.is_junction()):
        raise UnsafePathError("Restauración denegada: destino es un punto de reparse.")
    if is_protected_path(destination):
        raise UnsafePathError("Restauración denegada: destino protegido.")
    if destination.exists():
        raise FileExistsError(f"Error: el destino {destination} ya existe.")
        
    ensure_safe_to_modify(destination, allow_sensitive=False)

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(stored_file), str(destination))
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Fallo durante la restauración: {e}")

    items.remove(match)
    save_manifest(items, base)
    return destination


def purge_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> bool:
    """Elimina permanentemente un archivo del sandbox, verificando su integridad previa."""
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
        raise UnsafePathError("Borrado de seguridad fallido: ruta fuera de sandbox.")

    if not stored_file.exists() or not match.verify_integrity(stored_file):
        raise UnsafePathError("Integridad comprometida: no se borra un archivo sospechoso inestable.")
    
    ensure_safe_to_modify(stored_file, allow_sensitive=False)

    if _safe_unlink(stored_file):
        items.remove(match)
        save_manifest(items, base)
        return True
    return False


def purge_all(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Vacía el sandbox de cuarentena, eliminando únicamente archivos registrados y validados."""
    try:
        quarantine_root = quarantine_dir(base)
    except (OSError, ValueError):
        return 0
    
    items = load_manifest(base)
    item_map = {item.stored_name: item for item in items}
    
    purged_count = 0
    items_to_keep: List[QuarantineItem] = []
    
    try:
        for entry in quarantine_root.iterdir():
            if entry.name == MANIFEST_NAME or not entry.is_file():
                continue
            
            if entry.name in item_map:
                item = item_map[entry.name]
                if entry.exists() and item.verify_integrity(entry):
                    if _safe_unlink(entry):
                        purged_count += 1
                        continue
                items_to_keep.append(item)
                
        if purged_count > 0:
            save_manifest(items_to_keep, base)
    except (OSError, PermissionError):
        pass
        
    return purged_count


def total_quarantined_bytes(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el total de bytes ocupados por los archivos actualmente en cuarentena."""
    return sum(item.size_bytes for item in load_manifest(base))


def summarize(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera un reporte legible de los ítems en cuarentena para visualización UI."""
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
