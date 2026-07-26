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
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Extensiones típicas de archivos "basura" / temporales en Windows
JUNK_EXTENSIONS = {
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
}

# Carpetas típicas donde se acumula basura en Windows 11
DEFAULT_SCAN_DIRS = [
    os.path.expandvars(r"%TEMP%"),
    os.path.expandvars(r"%LOCALAPPDATA%\Temp"),
    os.path.expanduser("~/Downloads"),
]


@dataclass
class JunkFile:
    """Representa un archivo candidato a limpieza con sus metadatos básicos."""
    path: Path
    size_bytes: int
    modified: datetime

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño del archivo en Megabytes redondeado a 2 decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)


def scan_for_junk(directories: list[str] | None = None) -> list[JunkFile]:
    """
    Recorre las carpetas indicadas y devuelve una lista de candidatos a basura.
    Omite directorios o archivos sin permisos de lectura sin interrumpir el proceso.
    """
    dirs = directories or DEFAULT_SCAN_DIRS
    found: list[JunkFile] = []

    for d in dirs:
        p = Path(d).expanduser()
        if not p.exists():
            logger.warning(f"Ruta de escaneo no encontrada: {p}")
            continue
        if not p.is_dir():
            logger.warning(f"La ruta indicada no es un directorio: {p}")
            continue
            
        for root, _, files in os.walk(p):
            for name in files:
                fp = Path(root) / name
                try:
                    if fp.suffix.lower() in JUNK_EXTENSIONS:
                        stat = fp.stat()
                        found.append(
                            JunkFile(
                                path=fp,
                                size_bytes=stat.st_size,
                                modified=datetime.fromtimestamp(stat.st_mtime),
                            )
                        )
                except (PermissionError, FileNotFoundError, OSError) as e:
                    logger.debug(f"Acceso denegado o archivo no encontrado al inspeccionar {fp}: {e}")
                    continue
    return found


def sort_junk(files: list[JunkFile], by: str = "size", ascending: bool = True) -> list[JunkFile]:
    """
    Ordena la lista de JunkFile por tamaño ('size') o fecha de modificación ('date').
    Retorna una nueva lista ordenada sin modificar la original.
    """
    if by not in ("size", "date"):
        logger.warning("Criterio de ordenación desconocido '%s', usando 'size' por defecto.", by)
        by = "size"
        
    key_func = (lambda f: f.size_bytes) if by == "size" else (lambda f: f.modified)
    return sorted(files, key=key_func, reverse=not ascending)


def stage_for_review(files: list[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """
    Mueve los archivos encontrados a una carpeta de revisión.
    Preserva el nombre original agregando un timestamp para evitar colisiones.
    """
    dest = Path(review_dir).expanduser()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("No se pudo crear el directorio de revisión %s: %s", dest, e)
        raise

    for jf in files:
        if not jf.path.exists():
            continue
            
        target = dest / f"{jf.path.stem}_{int(jf.modified.timestamp())}{jf.path.suffix}"
        try:
            shutil.move(str(jf.path), str(target))
        except (PermissionError, FileNotFoundError, shutil.Error, OSError) as e:
            logger.error("Error moviendo archivo %s hacia %s: %s", jf.path, target, e)
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Borra definitivamente los archivos contenidos en la carpeta de revisión.
    Esta acción es destructiva y debe ser invocada explícitamente.
    """
    dest = Path(review_dir).expanduser()
    if not dest.exists():
        logger.info("Directorio de revisión no encontrado: %s", dest)
        return 0
        
    count = 0
    for f in dest.iterdir():
        try:
            if f.is_file():
                f.unlink()
                count += 1
        except (PermissionError, OSError) as e:
            logger.error("No se pudo borrar permanentemente el archivo %s: %s", f, e)
            continue
    return count
