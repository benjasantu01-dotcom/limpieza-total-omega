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
from typing import List, Union, Dict, Any, Optional

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
    """
    Representa un registro de archivo en cuarentena.
    
    Esta clase actúa como un contrato de datos entre el archivo en disco
    y el manifiesto JSON. Toda modificación de atributos debe mantener
    la consistencia con `verify_integrity`.
    """
    item_id: str
    original_path: str
    stored_name: str
    size_bytes: int
    reason: str
    quarantined_at: str
    sha256: str = ""

    def __post_init__(self) -> None:
        """Valida que los tipos y valores del modelo sean íntegros."""
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
        """Calcula el tamaño en Megabytes con precisión de dos decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa la instancia a diccionario para persistencia JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional[QuarantineItem]:
        """
        Reconstruye un ítem desde un diccionario.
        
        Args:
            data: Diccionario con la estructura esperada de un ítem.
        Returns:
            Instancia de QuarantineItem o None si la estructura es inválida.
        """
        if not isinstance(data, dict):
            return None
        required = {"item_id", "original_path", "stored_name", "size_bytes", "reason", "quarantined_at"}
        if not required.issubset(data.keys()):
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

    def _validate_integrity(self, stored_path: Path) -> bool:
        """
        Verificación de nivel 1: Comprueba que el archivo físico exista,
        no sea un link y que su tamaño coincida con el registro.
        """
        try:
            return stored_path.is_file() and not stored_path.is_symlink() and stored_path.stat().st_size == self.size_bytes
        except (OSError, PermissionError):
            return False

    def verify_integrity(self, stored_path: Path) -> bool:
        """
        Verificación de nivel 2: Valida el contenido mediante hash SHA-256.
        
        Precondición: El archivo debe haber pasado previamente `_validate_integrity`.
        """
        if not stored_path or not self._validate_integrity(stored_path):
            return False
        try:
            return bool(self.sha256 and _get_sha256(stored_path) == self.sha256)
        except (OSError, PermissionError):
            return False


def _get_sha256(path: Path) -> str:
    """Calcula hash SHA-256 usando buffers para evitar saturación de memoria."""
    sha256_hash = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(131072):
                sha256_hash.update(chunk)
    except (OSError, PermissionError, IOError):
        return ""
    return sha256_hash.hexdigest()


def _is_file_locked(path: Path) -> bool:
    """Comprueba si el archivo está siendo bloqueado por otro proceso."""
    if not isinstance(path, Path) or path is None:
        return True
    if not path.exists() or not path.is_file():
        return False
    try:
        with open(path, "rb") as f:
            return False
    except (PermissionError, IOError):
        return True
    except OSError:
        return False


def _safe_unlink(path: Path) -> bool:
    """
    Borra un archivo previa validación de seguridad y bloqueo.
    Solo borra si el archivo no está en uso, es propiedad del usuario y reside en una zona permitida.
    """
    if not isinstance(path, Path) or path is None:
        return False
    try:
        # Re-chequeo atómico antes de unlink
        if not path.exists() or not path.is_file() or path.is_symlink():
            return False
        if hasattr(os, 'getuid') and path.stat().st_uid != os.getuid():
            return False
            
        if is_safe_to_modify(path) and not _is_file_locked(path):
            path.unlink()
            return True
        return False
    except (OSError, PermissionError):
        return False

def _generate_safe_stored_name(original_path: Path, item_id: str) -> str:
    """
    Normaliza el nombre del archivo para la cuarentena.
    Evita caracteres prohibidos y nombres reservados de Windows.
    """
    sanitized = "".join(c for c in original_path.name if c.isalnum() or c in "._-")
    if not sanitized or sanitized in (".", ".."):
        sanitized = "unknown_file"
        
    parts = sanitized.split('.')
    name_base = parts[0] if parts[0] else "q_file"
    if name_base.upper() in WINDOWS_RESERVED_NAMES:
        name_base = f"q_{name_base}"
    
    extension = f".{parts[-1]}" if len(parts) > 1 else ""
    return f"{item_id}__{name_base[:64]}{extension}"[:128]


def quarantine_dir(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """
    Resuelve el directorio base de cuarentena.
    Asegura que la ruta no sea parte de una jerarquía protegida.
    """
    if not base:
        raise ValueError("El directorio base no puede estar vacío.")
    try:
        path = Path(base).expanduser().resolve()
        if not path.name.strip():
            raise UnsafePathError("Ruta de cuarentena inválida o vacía.")
        if is_protected_path(path):
            raise UnsafePathError("Directorio de cuarentena reside en ruta protegida.")
        if not is_safe_to_modify(path):
            raise UnsafePathError("Directorio de cuarentena no cumple políticas de seguridad.")
        path.mkdir(parents=True, exist_ok=True)
        return path
    except (OSError, RuntimeError) as e:
        raise OSError(f"No se pudo preparar el directorio de cuarentena: {e}")


def _manifest_path(base_dir: Path) -> Path:
    """Retorna la ubicación del manifiesto."""
    return (base_dir / MANIFEST_NAME).resolve()


def _is_within_quarantine_sandbox(path: Path, root: Path) -> bool:
    """Valida si una ruta reside dentro de la jerarquía de cuarentena."""
    return is_within_directory(path, root)


def _check_windows_file_attributes(path_str: str) -> None:
    """Validación específica para el sistema de archivos de Windows."""
    if os.name != 'nt':
        return
    path_obj = Path(path_str)
    if not path_obj.exists():
        return
    if len(path_obj.parts) > 64:
        raise UnsafePathError("Profundidad de ruta excesiva en sistema Windows.")
    import ctypes
    attrs = ctypes.windll.kernel32.GetFileAttributesW(path_str)
    if attrs != -1:
        if attrs & 0x02 or attrs & 0x04:
            raise UnsafePathError("No se permite procesar archivos con atributos de sistema/ocultos.")
        if attrs & 0x01:
            raise UnsafePathError("Archivo protegido contra escritura (solo lectura).")


def _check_path_syntax_integrity(path: Path) -> None:
    """Filtro de seguridad contra ataques de path traversal."""
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


def _check_isolation_safety(source_path: Path, dest_dir: Path) -> None:
    """Validación de seguridad antes de aislar (coherencia de rutas)."""
    resolved_source = source_path.resolve(strict=True)
    resolved_dest_dir = dest_dir.resolve()
    
    if not resolved_source.is_file():
        raise UnsafePathError("Solo se aceptan archivos regulares para aislamiento.")
    if resolved_source.stat().st_size == 0:
        raise UnsafePathError("Operación denegada: archivos vacíos no permitidos.")
    if resolved_source.parent == resolved_dest_dir:
        raise UnsafePathError("Operación circular: origen y destino en la misma carpeta.")
    if is_protected_path(resolved_source):
        raise UnsafePathError("Operación prohibida: la ruta origen está protegida.")
    if is_protected_path(resolved_dest_dir) or is_protected_path(resolved_dest_dir.parent):
        raise UnsafePathError("Destino inválido: directorio de cuarentena en ruta protegida.")
    if _is_within_quarantine_sandbox(resolved_source, resolved_dest_dir):
        raise UnsafePathError("El archivo ya reside en el sandbox de cuarentena.")
    if resolved_source.stat().st_dev != resolved_dest_dir.stat().st_dev:
        raise UnsafePathError("Operación prohibida: origen y destino en dispositivos diferentes.")
    
    ensure_safe_to_modify(resolved_source, allow_sensitive=True)
    if _is_file_locked(resolved_source):
        raise IOError("El archivo está en uso por otro proceso y no puede moverse.")


def _validate_isolation_request(source_path: Path, dest_dir: Path) -> None:
    """
    Ejecuta el protocolo de pre-validación de seguridad.
    
    Verifica la sintaxis, atributos de sistema, disponibilidad de espacio 
    en disco y la seguridad lógica (evitar bucles o rutas protegidas) 
    antes de iniciar cualquier operación de I/O.
    """
    _check_path_syntax_integrity(source_path)
    _check_windows_file_attributes(str(source_path))
    
    if source_path.is_symlink():
        raise UnsafePathError("No se permite aislar enlaces simbólicos o puntos de reparse.")

    try:
        resolved_source = source_path.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise UnsafePathError(f"Ruta origen inaccesible: {e}")
    
    _ensure_disk_space(dest_dir, resolved_source.stat().st_size)
    _check_isolation_safety(resolved_source, dest_dir)


@lru_cache(maxsize=4)
def _load_manifest_raw(base_str: str, _mtime: float = 0.0) -> List[Dict[str, Any]]:
    """Carga de bajo nivel (cacheada) del manifiesto."""
    path = _manifest_path(Path(base_str))
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError, PermissionError):
        return []

def load_manifest(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR, force_reload: bool = False) -> List[QuarantineItem]:
    """Carga y sincroniza el manifiesto frente a posibles eliminaciones externas."""
    base_path = quarantine_dir(base)
    m_path = _manifest_path(base_path)
    mtime = m_path.stat().st_mtime if m_path.exists() else 0.0
    
    if force_reload:
        _load_manifest_raw.cache_clear()
        
    raw_data = _load_manifest_raw(str(base_path), mtime)
    
    validated = []
    dirty = False
    for d in raw_data:
        item = QuarantineItem.from_dict(d)
        if item:
            if (base_path / item.stored_name).exists():
                validated.append(item)
            else:
                dirty = True
    
    if dirty:
        save_manifest(validated, base_path)
        
    return validated


def save_manifest(items: List[QuarantineItem], base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Persistencia atómica (JSON) para evitar corrupción por fallos durante la escritura."""
    if not isinstance(items, list):
        raise ValueError("El manifiesto debe ser una lista de ítems.")
    
    if not all(isinstance(i, QuarantineItem) for i in items):
        raise TypeError("El manifiesto contiene objetos no compatibles con QuarantineItem.")

    base_path = quarantine_dir(base)
    target_path = _manifest_path(base_path)
    temp_path: Optional[Path] = None
    
    try:
        # Serializar antes de abrir el temporal para validar estructura
        content = json.dumps([item.to_dict() for item in items], indent=2, ensure_ascii=False)
        
        with tempfile.NamedTemporaryFile("w", dir=base_path, encoding="utf-8", delete=False) as tf:
            temp_path = Path(tf.name)
            tf.write(content)
            tf.flush()
            os.fsync(tf.fileno())
            
        # Validación post-escritura: verificar que el archivo temporal no esté corrupto
        if temp_path.stat().st_size != len(content.encode('utf-8')):
             raise OSError("Integridad del archivo temporal fallida.")

        os.replace(temp_path, target_path)
        
        dir_fd = os.open(str(base_path), os.O_RDONLY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
        
        _load_manifest_raw.cache_clear()
        return target_path
    except (OSError, TypeError, IOError) as e:
        if temp_path and temp_path.exists():
            try: os.remove(temp_path)
            except OSError: pass
        raise RuntimeError(f"Fallo crítico al persistir manifiesto: {e}")


def _ensure_disk_space(dest_dir: Path, required_size: int) -> None:
    """Verifica que el disco destino tenga espacio suficiente (margen de seguridad)."""
    if not dest_dir.exists():
        raise FileNotFoundError(f"Directorio inexistente: {dest_dir}")
    if not os.access(dest_dir, os.W_OK):
        raise PermissionError(f"Directorio sin permisos de escritura: {dest_dir}")
    usage = shutil.disk_usage(dest_dir)
    margin = max(int(required_size * 0.05), 5 * 1024 * 1024)
    if usage.free < (required_size + margin):
        raise OSError("Espacio insuficiente en disco para aislamiento seguro.")


def _atomic_isolate_file(source: Path, destination: Path, original_size: int) -> str:
    """
    Realiza la copia física de un archivo al sandbox mediante un buffer temporal.
    
    Utiliza `tempfile` para asegurar que el archivo no aparezca en destino 
    hasta estar completamente escrito y sincronizado al disco (fsync). 
    Incluye validación final de tamaño y hash SHA-256 para garantizar 
    la integridad bit a bit.
    """
    if not source.exists():
        raise FileNotFoundError("El archivo de origen no existe.")

    if not _is_within_quarantine_sandbox(destination.resolve(), destination.parent.resolve()):
        raise UnsafePathError("Operación denegada: intento de escritura fuera del sandbox.")
    
    if len(str(destination)) >= 250:
        raise OSError("Ruta de destino demasiado larga.")

    temp_path: Optional[Path] = None
    try:
        fd, temp_file_path = tempfile.mkstemp(dir=destination.parent, prefix=".tmp_q_")
        temp_path = Path(temp_file_path)
        try:
            with os.fdopen(fd, 'wb') as tmp:
                with open(source, 'rb') as src:
                    shutil.copyfileobj(src, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
        except (OSError, IOError) as e:
            os.close(fd)
            raise e

        # Verificación de post-copia: integridad física
        if temp_path.stat().st_size != original_size:
            raise OSError("Error de integridad: el tamaño del archivo copiado no coincide.")
            
        if not is_safe_to_modify(temp_path):
            raise UnsafePathError("Integridad comprometida: el archivo en área temporal no es seguro.")
        
        _check_windows_file_attributes(str(temp_path))

        os.replace(temp_path, destination)
        
        dir_fd = os.open(str(destination.parent), os.O_RDONLY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
        
        # Última validación post-reemplazo
        if destination.stat().st_size != original_size:
            raise OSError("Error crítico: el archivo se corrompió tras el reemplazo atómico.")
            
        file_hash = _get_sha256(destination)
        if not file_hash:
            raise OSError("Falla de integridad: no se pudo verificar el hash.")
        return file_hash
    except Exception as e:
        if temp_path and temp_path.exists():
            try: os.remove(temp_path)
            except OSError: pass
        raise RuntimeError(f"Error de sistema durante aislamiento: {e}")


def quarantine_file(
    source: Union[str, Path],
    reason: str = "Marcado como sospechoso",
    base: Union[str, Path] = DEFAULT_QUARANTINE_DIR,
) -> QuarantineItem:
    """Ciclo completo: valida, aísla y registra el archivo en cuarentena."""
    if not source:
        raise ValueError("La ruta de origen no puede estar vacía.")
    
    source_path = Path(source).expanduser().resolve(strict=True)
    if source_path.is_dir():
        raise UnsafePathError("Solo se pueden aislar archivos individuales, no directorios.")
        
    original_size = source_path.stat().st_size
    
    dest_dir = quarantine_dir(base)
    
    if _is_within_quarantine_sandbox(source_path, dest_dir.resolve()):
        raise UnsafePathError("Operación denegada: el archivo ya está en el sandbox.")

    _validate_isolation_request(source_path, dest_dir)
    
    item_id = uuid.uuid4().hex[:12]
    destination = dest_dir / _generate_safe_stored_name(source_path, item_id)
    
    if destination.exists():
        raise FileExistsError(f"Colisión de nombre en el sandbox: {destination.name}")
        
    file_hash = _atomic_isolate_file(source_path, destination, original_size)
    
    try:
        base_path = quarantine_dir(base)
        m_path = _manifest_path(base_path)
        mtime = m_path.stat().st_mtime if m_path.exists() else 0.0
        raw_items = _load_manifest_raw(str(base_path), mtime)
        
        quarantine_item = QuarantineItem(
            item_id=item_id,
            original_path=str(source_path),
            stored_name=destination.name,
            size_bytes=original_size,
            reason=str(reason) if reason else "Sin motivo",
            quarantined_at=datetime.now().isoformat(timespec="seconds"),
            sha256=file_hash,
        )
        
        items_list = [QuarantineItem.from_dict(d) for d in raw_items if d.get("item_id")]
        items_list = [i for i in items_list if i]
        items_list.append(quarantine_item)
        save_manifest(items_list, base)
        
        if destination.exists() and quarantine_item.verify_integrity(destination):
            source_path.unlink()
            return quarantine_item
        else:
            raise RuntimeError("La integridad falló tras persistir el manifiesto.")
            
    except Exception as e:
        if destination.exists():
            _safe_unlink(destination)
        raise RuntimeError(f"Fallo en la operación de aislamiento: {e}")


def list_items(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[QuarantineItem]:
    """Retorna ítems ordenados por fecha de aislamiento (recientes primero)."""
    return sorted(load_manifest(base), key=lambda item: item.quarantined_at, reverse=True)


def restore_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> Path:
    """Restaura un ítem de la cuarentena previa validación de seguridad."""
    if not item_id or not isinstance(item_id, str):
        raise ValueError("ID de ítem inválido o vacío.")
    base_path = quarantine_dir(base)
    items = load_manifest(base)
    quarantine_item = next((i for i in items if i.item_id == item_id), None)
    if not quarantine_item:
        raise KeyError(f"No se encontró el ítem: {item_id}")
        
    stored_file = (base_path / quarantine_item.stored_name).resolve()
    if not stored_file.exists() or not quarantine_item.verify_integrity(stored_file):
        raise RuntimeError("Integridad comprometida: archivo no hallado o hash inválido.")
    
    destination = Path(quarantine_item.original_path).absolute()
    _check_path_syntax_integrity(destination)
    if is_protected_path(destination):
        raise UnsafePathError("Restauración denegada: destino protegido.")
    if destination.exists():
        raise FileExistsError(f"Error: el destino {destination} ya existe.")
    
    if stored_file.stat().st_dev != destination.parent.resolve().stat().st_dev:
        raise UnsafePathError("Restauración denegada: destino en dispositivo diferente al original.")
    
    parent = destination.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    if not is_safe_to_modify(parent) or not is_safe_to_modify(destination):
        raise UnsafePathError("Restauración denegada: destino no seguro.")
        
    try:
        os.replace(str(stored_file), str(destination))
        save_manifest([i for i in items if i.item_id != item_id], base)
    except (OSError, PermissionError) as e:
        raise RuntimeError(f"Fallo crítico durante restauración: {e}")
        
    return destination


def purge_item(item_id: str, base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> bool:
    """Borra permanentemente un ítem del sandbox tras verificar su estado."""
    if not isinstance(item_id, str) or not item_id.strip():
        return False
    base_path = quarantine_dir(base)
    items = load_manifest(base)
    quarantine_item = next((i for i in items if i.item_id == item_id), None)
    if not quarantine_item:
        return False
        
    stored_file = (base_path / quarantine_item.stored_name).resolve()
    if not stored_file.exists():
        save_manifest([i for i in items if i.item_id != item_id], base)
        return False
        
    if not quarantine_item.verify_integrity(stored_file):
        raise UnsafePathError(f"Integridad física fallida para ítem {item_id}: el hash no coincide.")
        
    if _safe_unlink(stored_file):
        save_manifest([i for i in items if i.item_id != item_id], base)
        return True
    return False


def _is_item_purgable(file_path: Path, item: QuarantineItem, base_path: Path) -> bool:
    """Helper interno para validar si un ítem cumple los requisitos para borrado seguro."""
    try:
        if not file_path.is_file() or not _is_within_quarantine_sandbox(file_path, base_path):
            return False
        return (
            item.verify_integrity(file_path) and
            not _is_file_locked(file_path) and
            is_safe_to_modify(file_path)
        )
    except (OSError, PermissionError):
        return False


def purge_all(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Limpia el sandbox de archivos verificables."""
    quarantine_root = quarantine_dir(base)
    items = load_manifest(base)
    
    item_map = {item.stored_name: item for item in items}
    purged_count = 0
    kept_items = []
    
    for stored_path in quarantine_root.iterdir():
        # Saltamos manifiesto o directorios
        if stored_path.name == MANIFEST_NAME or stored_path.is_dir():
            continue
            
        if stored_path.name in item_map:
            item = item_map[stored_path.name]
            if _is_item_purgable(stored_path, item, quarantine_root):
                if _safe_unlink(stored_path):
                    purged_count += 1
                    continue
            kept_items.append(item)
        else:
            # Archivo extraño no en manifiesto, omitir por seguridad
            continue
                
    if purged_count > 0:
        save_manifest(kept_items, base)
    return purged_count


def total_quarantined_bytes(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> int:
    """Calcula el espacio total ocupado en el sandbox (vía objetos cargados)."""
    return sum(item.size_bytes for item in load_manifest(base))


def summarize(base: Union[str, Path] = DEFAULT_QUARANTINE_DIR) -> List[str]:
    """Genera reporte resumen del estado del sandbox."""
    items = list_items(base)
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
