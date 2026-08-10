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
    Es vital para evitar bucles infinitos en el sistema de archivos y no seguir 
    enlaces que podrían apuntar fuera de los directorios permitidos.
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
    Previene la pérdida de datos o sobrescritura accidental durante el proceso
    de cuarentena/revisión si ya existe un archivo con el mismo nombre.
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
    """
    Filtro de seguridad para directorios. Compara contra la lista de bloqueo 
    para asegurar que no descendamos en carpetas críticas del sistema operativo.
    """
    return name.lower() not in SYSTEM_FOLDER_BLOCKLIST


def _is_file_accessible(path: Path) -> bool:
    """
    Intenta abrir el archivo en modo lectura binaria para verificar permisos.
    Esto permite filtrar archivos bloqueados por el SO o usados exclusivamente por otros procesos
    sin necesidad de intentar moverlos (evitando errores en runtime).
    """
    try:
        with open(path, "rb") as f:
            return True
    except (OSError, PermissionError):
        return False


def _is_file_locked(path: Path) -> bool:
    """Verifica si un archivo está bloqueado por otro proceso intentando abrirlo en modo append."""
    try:
        with open(path, "a+b"):
            return False
    except (OSError, PermissionError):
        return True


def _is_safe_for_move(path: Path) -> bool:
    """
    Validación de seguridad compuesta: verifica que la ruta esté permitida por los 
    guards de seguridad globales y que el archivo sea efectivamente legible antes 
    de cualquier operación de movimiento.
    """
    return is_safe_to_modify(path) and _is_file_accessible(path)


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Realiza un escaneo recursivo en directorios buscando archivos temporales.
    Usa una función interna recursiva para gestionar el recorrido evitando entrar 
    en junctions o rutas protegidas.
    """
    dirs: List[str] = directories if directories is not None else DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []

    def _walk_dir(base_path: str) -> None:
        """Recorrido en profundidad que respeta los bloqueos de directorios y symlinks."""
        try:
            with os.scandir(base_path) as it:
                for entry in it:
                    if _is_junction(entry):
                        continue
                    
                    if entry.is_dir():
                        if _is_allowed_directory(entry.name):
                            _walk_dir(entry.path)
                    elif _is_junk_path(Path(entry.name)):
                        try:
                            stat = entry.stat()
                            path_obj = Path(entry.path)
                            if _is_safe_for_move(path_obj):
                                found.append(JunkFile(
                                    path=path_obj,
                                    size_bytes=stat.st_size,
                                    modified=datetime.fromtimestamp(stat.st_mtime)
                                ))
                        except (PermissionError, OSError):
                            continue
        except (PermissionError, OSError):
            pass

    for d in dirs:
        try:
            p = Path(d).expanduser().resolve()
            if p.exists() and p.is_dir() and is_safe_to_modify(p):
                _walk_dir(str(p))
        except (RuntimeError, OSError, ValueError):
            continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """Ordena los archivos encontrados por tamaño o fecha mediante funciones lambda de extracción."""
    if not isinstance(files, list) or not files:
        return []
        
    configs: Dict[str, Callable[[JunkFile], SortKey]] = {
        "size": lambda f: f.size_bytes,
        "date": lambda f: f.modified
    }
        
    criterio = by.lower() if isinstance(by, str) else "size"
    key_func = configs.get(criterio, configs["size"])
        
    return sorted(files, key=key_func, reverse=not bool(ascending))


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """
    Mueve archivos candidatos a una carpeta de revisión segura.
    Valida cada archivo contra los permisos de seguridad antes de cada movimiento 
    (ensure_safe_to_modify), garantizando que nada protegido sea manipulado accidentalmente.
    """
    if not files:
        raise ValueError("La lista de archivos a procesar no puede estar vacía.")

    dest: Path = Path(review_dir).expanduser().resolve()
    ensure_safe_to_modify(dest)
    dest.mkdir(parents=True, exist_ok=True)

    for jf in files:
        try:
            if not isinstance(jf, JunkFile) or not jf.path:
                continue
            current_abs: Path = jf.path.resolve()
            
            if not current_abs.exists() or not current_abs.is_file():
                continue
            
            # Verificación de que no estamos moviendo la carpeta raíz de revisión a sí misma
            if dest == current_abs or dest in current_abs.parents:
                continue
            
            # Verificación defensiva contra rutas fuera del ámbito de seguridad
            ensure_safe_to_modify(current_abs)
            
            if _is_file_locked(current_abs):
                continue

            target: Path = _generate_unique_target(dest / f"{current_abs.stem}_{int(jf.modified.timestamp())}{current_abs.suffix}")
            shutil.move(str(current_abs), str(target))
        except (PermissionError, OSError, shutil.Error, RuntimeError):
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina archivos de la carpeta de revisión.
    Realiza una comprobación de seguridad doble para asegurar que la carpeta destino 
    esté dentro del ámbito permitido y que el archivo pertenezca a la estructura 
    de revisión para prevenir borrados accidentales fuera del área controlada.
    """
    if not isinstance(review_dir, str) or not review_dir.strip():
        return 0

    dest: Path = Path(review_dir).expanduser().resolve()
    # ensure_safe_to_modify verifica recursivamente la validez de la ruta de revisión
    ensure_safe_to_modify(dest)

    count: int = 0
    try:
        with os.scandir(dest) as it:
            for entry in it:
                try:
                    if entry.is_file() and not _is_junction(entry):
                        path_to_delete = Path(entry.path).resolve()
                        # Validación defensiva: asegurar que el archivo a borrar esté contenido estrictamente en dest
                        if dest == path_to_delete.parent:
                            os.remove(path_to_delete)
                            count += 1
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        pass
    return count
