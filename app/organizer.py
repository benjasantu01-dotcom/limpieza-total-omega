"""
organizer.py
Organiza archivos "basura" (temporales, cache, descargas viejas, etc.)
en carpetas ordenadas por tamaño o fecha, sin borrar nada automáticamente.

Filosofía de seguridad: este módulo NUNCA borra archivos por sí solo.
Solo mueve candidatos a una carpeta de revisión ("_Para_Revisar") para
que el usuario decida qué borrar. Borrar es una acción explícita y
separada (ver delete_reviewed()).
"""

from __future__ import annotations
import os
import shutil
import string
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Final, Callable, Union, TypeAlias, NamedTuple, Dict

from safety import is_safe_to_modify, ensure_safe_to_modify

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Definiciones de tipo para claridad en firmas complejas
SortKey: TypeAlias = Union[int, datetime]

class SortConfig(NamedTuple):
    """Define los criterios permitidos para el ordenamiento de archivos."""
    field: str
    key_func: Callable[[JunkFile], SortKey]

# Extensiones típicas de archivos "basura" / temporales en Windows
JUNK_EXTENSIONS: Final[set[str]] = {
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
}
# Pre-calculado para eficiencia en loops
_LOWER_JUNK_EXTS: Final[set[str]] = {ext.lower() for ext in JUNK_EXTENSIONS}

# Carpetas típicas donde se acumula basura
DEFAULT_SCAN_DIRS: Final[List[str]] = [
    os.path.expandvars(r"%TEMP%"),
    os.path.expandvars(r"%LOCALAPPDATA%\Temp"),
    os.path.expanduser("~/Downloads"),
]

# Carpetas de sistema críticas que nunca se recorren para prevenir daños al SO
SYSTEM_FOLDER_BLOCKLIST: Final[set[str]] = {
    "windows", "program files", "program files (x86)", "$recycle.bin", "system volume information"
}


def list_available_drives() -> List[str]:
    """Detecta unidades montadas en sistemas Windows devolviendo una lista de rutas raíz."""
    if os.name != "nt":
        return []
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive: str = f"{letter}:\\"
        if os.path.exists(drive):
            drives.append(drive)
    return drives


@dataclass
class JunkFile:
    """Representa un archivo candidato a limpieza con metadatos asociados."""
    path: Path
    size_bytes: int
    modified: datetime

    def __post_init__(self) -> None:
        """Asegura la normalización de la ruta tras la instanciación."""
        if not isinstance(self.path, Path):
            self.path = Path(self.path)

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño del archivo en Megabytes (redondeado a 2 decimales)."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión está dentro de la lista de candidatos JUNK_EXTENSIONS."""
        return _is_junk_path(self.path)


def _is_junction(entry: os.DirEntry) -> bool:
    """
    Detecta si una entrada de directorio es un punto de reparse (junction o symlink).
    Evita que el escáner entre en bucles infinitos o salga de las carpetas de usuario.
    """
    try:
        return entry.is_symlink() or (os.name == "nt" and "reparse" in os.stat(entry.path).st_file_attributes)
    except (OSError, AttributeError):
        return False


def _is_junk_path(path: Path) -> bool:
    """Valida si el archivo posee una extensión categorizada como 'basura'."""
    return path.suffix.lower() in _LOWER_JUNK_EXTS


def _generate_unique_target(target: Path) -> Path:
    """
    Resuelve colisiones de nombres mediante sufijos numéricos incrementales.
    Previene la sobreescritura accidental al consolidar archivos de distintas rutas.
    """
    if not target.exists():
        return target
        
    parent: Path = target.parent
    stem: str = target.stem
    suffix: str = target.suffix
    counter: int = 1
    
    while (candidate := parent / f"{stem}_{counter}{suffix}").exists():
        counter += 1
    return candidate


def _is_allowed_directory(name: str) -> bool:
    """Verifica si el nombre de carpeta no pertenece a SYSTEM_FOLDER_BLOCKLIST."""
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST


def _is_file_accessible(path: Path) -> bool:
    """Verifica si el archivo es accesible intentando abrirlo en lectura binaria."""
    try:
        with open(path, "rb") as f:
            return True
    except (OSError, PermissionError):
        return False


def _is_file_locked(path: Path) -> bool:
    """
    Verifica si el archivo está bloqueado por otro proceso.
    Intenta abrirlo en modo append (A+) para testear permisos de escritura exclusivos.
    """
    try:
        with open(path, "a+b"):
            return False
    except (OSError, PermissionError):
        return True

def _is_safe_to_move(jf: JunkFile, dest: Path) -> bool:
    """
    Valida si un archivo es candidato seguro para ser movido.
    Verifica: existencia, accesibilidad, no ser recursivo y que ambos puntos residan 
    en el mismo volumen (evita errores de I/O por mover entre discos).
    """
    current_abs = jf.path.resolve()
    if not current_abs.exists() or not current_abs.is_file():
        return False
    # Impedir movimiento recursivo o sobre sí mismo
    if dest == current_abs or dest in current_abs.parents:
        return False
    # Verificar bloqueos activos y restricción de mover entre volúmenes distintos
    if _is_file_locked(current_abs) or current_abs.anchor != dest.anchor:
        return False
    return True


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Realiza un escaneo recursivo en los directorios especificados para identificar archivos basura.
    Utiliza medidas de seguridad para evitar seguir enlaces simbólicos y carpetas del sistema.
    """
    raw_dirs = directories if directories is not None else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    
    # Pre-normalizar rutas únicas para evitar redundancia
    unique_dirs: set[Path] = set()
    for d in raw_dirs:
        if d and isinstance(d, str):
            try:
                p = Path(d).expanduser().resolve()
                if p.exists() and p.is_dir() and is_safe_to_modify(p):
                    unique_dirs.add(p)
            except (RuntimeError, OSError, ValueError):
                continue

    def _walk_dir(base_path: Path) -> None:
        """Recorrido profundo seguro: ignora reparse points y archivos protegidos."""
        try:
            with os.scandir(base_path) as it:
                for entry in it:
                    if _is_junction(entry):
                        continue
                    
                    if entry.is_dir():
                        if _is_allowed_directory(entry.name):
                            _walk_dir(Path(entry.path))
                    elif entry.is_file():
                        path_obj = Path(entry.path)
                        if _is_junk_path(path_obj):
                            if is_safe_to_modify(path_obj) and _is_file_accessible(path_obj):
                                try:
                                    stat = entry.stat()
                                    found.append(JunkFile(
                                        path=path_obj,
                                        size_bytes=stat.st_size,
                                        modified=datetime.fromtimestamp(stat.st_mtime)
                                    ))
                                except OSError:
                                    continue
        except (PermissionError, OSError):
            pass

    for d in unique_dirs:
        _walk_dir(d)
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena los archivos encontrados basándose en tamaño o fecha de modificación."""
    if not isinstance(files, list):
        return []
        
    registry: Dict[str, SortConfig] = {
        "size": SortConfig("size", lambda f: f.size_bytes),
        "date": SortConfig("date", lambda f: f.modified)
    }
        
    criterio = by.lower() if isinstance(by, str) else "size"
    config = registry.get(criterio, registry["size"])
        
    return sorted(files, key=config.key_func, reverse=not bool(ascending))


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """
    Mueve los archivos basura identificados a un directorio de cuarentena/revisión.
    Valida la disponibilidad de espacio y permisos antes de procesar cada archivo.
    """
    if not files:
        raise ValueError("La lista de archivos a procesar no puede estar vacía.")

    dest: Path = Path(review_dir).expanduser().resolve()
    ensure_safe_to_modify(dest)
    dest.mkdir(parents=True, exist_ok=True)

    for jf in files:
        try:
            # Validar que el archivo fuente aún sea seguro antes de cada operación
            ensure_safe_to_modify(jf.path)
            
            if _is_safe_to_move(jf, dest):
                if os.access(dest, os.W_OK) and shutil.disk_usage(dest).free > jf.size_bytes:
                    target = _generate_unique_target(dest / f"{jf.path.stem}_{int(jf.modified.timestamp())}{jf.path.suffix}")
                    shutil.move(str(jf.path), str(target))
        except (PermissionError, OSError, shutil.Error, RuntimeError):
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina archivos de forma permanente de la carpeta de revisión tras verificación.
    """
    if not isinstance(review_dir, str) or not review_dir.strip():
        return 0

    dest: Path = Path(review_dir).expanduser().resolve()
    ensure_safe_to_modify(dest)

    count: int = 0
    try:
        with os.scandir(dest) as it:
            for entry in it:
                try:
                    if entry.is_file() and not _is_junction(entry):
                        path_to_delete = Path(entry.path).resolve()
                        # Verificar que el archivo a borrar esté estrictamente en la carpeta autorizada
                        if dest == path_to_delete.parent:
                            ensure_safe_to_modify(path_to_delete)
                            os.remove(path_to_delete)
                            count += 1
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        pass
    return count
