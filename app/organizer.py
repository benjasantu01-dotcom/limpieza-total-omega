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
from typing import List, Optional, Final, Callable, Union
from safety import is_safe_to_modify

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Extensiones típicas de archivos "basura" / temporales en Windows
JUNK_EXTENSIONS: Final = {
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
}
# Pre-calculado para eficiencia en loops; evita conversiones repetidas a minúsculas
_LOWER_JUNK_EXTS: Final = {ext.lower() for ext in JUNK_EXTENSIONS}

# Carpetas típicas donde se acumula basura en Windows 11
DEFAULT_SCAN_DIRS: Final = [
    os.path.expandvars(r"%TEMP%"),
    os.path.expandvars(r"%LOCALAPPDATA%\Temp"),
    os.path.expanduser("~/Downloads"),
]

# Carpetas de sistema críticas que nunca se recorren para prevenir daños al SO
SYSTEM_FOLDER_BLOCKLIST: Final = frozenset({"windows", "program files", "program files (x86)", "$recycle.bin", "system volume information"})


def list_available_drives() -> List[str]:
    """
    Lista las unidades de disco montadas en el sistema mediante iteración de letras de unidad.

    Returns:
        List[str]: Lista de rutas raíz (ej. ['C:\\', 'D:\\']). Retorna vacía en entornos no-NT.
    """
    if os.name != "nt":
        return []
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if os.path.exists(drive):
            drives.append(drive)
    return drives


@dataclass
class JunkFile:
    """Representa un archivo candidato a limpieza con sus metadatos básicos para auditoría y ordenación."""
    path: Path
    size_bytes: int
    modified: datetime

    @property
    def size_mb(self) -> float:
        """Convierte bytes a Megabytes (formato float) para legibilidad en la interfaz."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Verifica si la extensión del archivo coincide con los criterios de basura predefinidos."""
        return self.path.suffix.lower() in _LOWER_JUNK_EXTS


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Realiza un escaneo recursivo en busca de archivos candidatos a limpieza.
    
    El proceso utiliza 'is_safe_to_modify' como filtro de seguridad en cada entrada 
    para garantizar que solo se cataloguen archivos fuera de rutas críticas. 
    Se ignoran explícitamente enlaces simbólicos para prevenir escapes del árbol de directorios.
    """
    if directories is not None and not isinstance(directories, list):
        logger.error("El parámetro directories debe ser una lista.")
        return []

    dirs = directories or DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    blocklist = SYSTEM_FOLDER_BLOCKLIST

    def _walk_dir(base_path: str) -> None:
        try:
            with os.scandir(base_path) as it:
                for entry in it:
                    try:
                        # Ignorar enlaces simbólicos para evitar bucles infinitos y exposición a rutas fuera de control
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() not in blocklist:
                                _walk_dir(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            if entry.path.lower().endswith(tuple(_LOWER_JUNK_EXTS)):
                                full_path = Path(entry.path)
                                # Validación centralizada: evitar tocar archivos protegidos por la lógica de seguridad
                                if is_safe_to_modify(full_path):
                                    stat = entry.stat()
                                    found.append(
                                        JunkFile(
                                            path=full_path,
                                            size_bytes=stat.st_size,
                                            modified=datetime.fromtimestamp(stat.st_mtime),
                                        )
                                    )
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            return

    for d in dirs:
        if isinstance(d, str):
            p = Path(d).expanduser()
            if p.exists() and p.is_dir():
                _walk_dir(str(p.resolve()))
            
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """
    Ordena la lista de JunkFile según el criterio especificado.
    
    Args:
        files: Lista de objetos JunkFile a ordenar.
        by: Atributo de ordenamiento ('size' para bytes, 'date' para fecha de modificación).
        ascending: Dirección del ordenamiento.
    """
    if not isinstance(files, list):
        return []
        
    if by not in ("size", "date"):
        logger.warning("Criterio de ordenación desconocido '%s', usando 'size' por defecto.", by)
        by = "size"

    key_func: Callable[[JunkFile], Union[int, datetime]] = (
        (lambda f: f.size_bytes) if by == "size" else (lambda f: f.modified)
    )
    return sorted(files, key=key_func, reverse=not ascending)


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """
    Mueve archivos candidatos a una carpeta de revisión ("staging").

    Seguridad: 
    1. Verifica que cada origen sea seguro mediante 'is_safe_to_modify'.
    2. Aplica comprobación de colisiones (evitar mover recursivamente).
    3. Confirma que el destino sea seguro antes de la operación de escritura.
    """
    if not isinstance(files, list) or not isinstance(review_dir, str):
        logger.warning("Entrada inválida en stage_for_review.")
        return Path(review_dir).expanduser().resolve()

    dest = Path(review_dir).expanduser().resolve()
    
    try:
        dest.mkdir(parents=True, exist_ok=True)
        if not dest.is_dir():
            raise NotADirectoryError(f"El destino {dest} no es un directorio válido.")
    except (OSError, NotADirectoryError) as e:
        logger.error("No se pudo preparar el directorio de revisión %s: %s", dest, e)
        raise

    for jf in files:
        if not isinstance(jf, JunkFile) or not hasattr(jf, 'path') or jf.path is None:
            continue
            
        try:
            full_source_path = jf.path.resolve()
            
            # Verificaciones pre-movimiento: existencia y seguridad de escritura
            if not full_source_path.exists() or not full_source_path.is_file():
                continue
            
            if not is_safe_to_modify(full_source_path) or not is_safe_to_modify(dest):
                continue

            # Prevenir colisiones lógicas: el destino no puede ser antepasado del origen (evitar recursión maliciosa)
            # Tampoco permitir mover el archivo hacia sí mismo
            if dest == full_source_path or dest in full_source_path.parents or full_source_path.parent == dest:
                continue
                
            # Verificar acceso exclusivo antes de mover
            try:
                with open(full_source_path, 'rb'):
                    pass
            except (PermissionError, OSError):
                logger.warning("Archivo bloqueado o en uso: %s", full_source_path)
                continue

            usage = shutil.disk_usage(dest.anchor)
            if usage.free < (jf.size_bytes + 10 * 1024 * 1024):
                logger.error("Espacio insuficiente en destino para mover %s", full_source_path)
                continue

            base_name = jf.path.stem
            ext = jf.path.suffix
            timestamp = int(jf.modified.timestamp())
            
            # Construir ruta final validando que permanezca confinada al directorio de staging
            target = (dest / f"{base_name}_{timestamp}{ext}").resolve()
            if not str(target).startswith(str(dest)):
                continue
                
            counter = 1
            while target.exists():
                target = dest / f"{base_name}_{timestamp}_{counter}{ext}"
                counter += 1

            shutil.move(str(full_source_path), str(target))
        except (PermissionError, OSError, shutil.Error) as e:
            logger.error("Error crítico moviendo archivo %s: %s", jf.path, e)
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina permanentemente archivos en el directorio de revisión tras confirmación externa.
    
    Seguridad: Implementa doble chequeo. Solo opera archivos que pasan 'is_safe_to_modify'
    y cuyo Path resuelto es estrictamente hijo del directorio de revisión ('startswith'),
    previniendo borrados accidentales en directorios padre fuera de control.
    """
    if not isinstance(review_dir, str):
        return 0
        
    dest = Path(review_dir).expanduser().resolve()
    if not dest.exists() or not dest.is_dir():
        logger.info("Directorio de revisión no encontrado o inválido: %s", dest)
        return 0

    count = 0
    for f in dest.iterdir():
        try:
            if f.is_file():
                # Doble validación: seguridad del módulo y confinamiento del Path
                if is_safe_to_modify(f) and str(f.resolve()).startswith(str(dest)):
                    f.unlink()
                    count += 1
                else:
                    logger.info("No se borró por seguridad: %s", f)
        except (PermissionError, OSError) as e:
            logger.error("No se pudo borrar permanentemente el archivo %s: %s", f, e)
            continue
    return count
