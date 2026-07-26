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
from typing import List, Optional

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

# Carpetas de sistema críticas que nunca se recorren, incluso si el
# usuario elige escanear una unidad completa (ej. "C:\"). Evita que un
# escaneo de disco completo se meta en Windows/Program Files.
SYSTEM_FOLDER_BLOCKLIST = {"windows", "program files", "program files (x86)", "$recycle.bin", "system volume information"}


def list_available_drives() -> List[str]:
    """Devuelve las letras de unidad disponibles en Windows (ej. ['C:\\\\', 'D:\\\\']),
    para que el usuario pueda elegir en qué disco buscar además de las
    carpetas por defecto. No falla en sistemas no-Windows: devuelve lista vacía."""
    if os.name != "nt":
        return []
    drives = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if os.path.exists(drive):
            drives.append(drive)
    return drives


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


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Recorre las carpetas indicadas y devuelve candidatos a basura.
    """
    if directories is not None and not isinstance(directories, list):
        logger.error("El parámetro directories debe ser una lista.")
        return []

    dirs = directories or DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []

    for d in dirs:
        if not isinstance(d, str):
            continue
        p = Path(d).expanduser()
        if not p.exists() or not p.is_dir():
            logger.warning(f"Ruta de escaneo inválida: {p}")
            continue

        for root, subdirs, files in os.walk(p):
            subdirs[:] = [sd for sd in subdirs if sd.lower() not in SYSTEM_FOLDER_BLOCKLIST]
            for name in files:
                if Path(name).suffix.lower() in JUNK_EXTENSIONS:
                    fp = Path(root) / name
                    try:
                        stat = fp.stat()
                        found.append(
                            JunkFile(
                                path=fp.resolve(),
                                size_bytes=stat.st_size,
                                modified=datetime.fromtimestamp(stat.st_mtime),
                            )
                        )
                    except (PermissionError, FileNotFoundError, OSError) as e:
                        logger.debug(f"Acceso denegado o archivo no encontrado al inspeccionar {fp}: {e}")
                        continue
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """
    Ordena la lista de JunkFile por tamaño ('size') o fecha de modificación ('date').
    """
    if not isinstance(files, list):
        return []
        
    if by not in ("size", "date"):
        logger.warning("Criterio de ordenación desconocido '%s', usando 'size' por defecto.", by)
        by = "size"

    key_func = (lambda f: f.size_bytes) if by == "size" else (lambda f: f.modified)
    return sorted(files, key=key_func, reverse=not ascending)


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """
    Mueve archivos candidatos a una carpeta de cuarentena/revisión.
    """
    if not files or not isinstance(files, list):
        logger.warning("La lista de archivos a organizar está vacía o es inválida.")
        return Path(review_dir).expanduser()

    dest = Path(review_dir).expanduser().resolve()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("No se pudo preparar el directorio de revisión %s: %s", dest, e)
        raise

    for jf in files:
        if not isinstance(jf, JunkFile) or not jf.path or not jf.path.exists():
            continue

        try:
            full_source_path = jf.path.resolve()
        except OSError:
            continue

        if any(part.lower() in SYSTEM_FOLDER_BLOCKLIST for part in full_source_path.parts):
            logger.warning("Intento de mover archivo en ruta protegida: %s", full_source_path)
            continue

        base_name = jf.path.stem
        ext = jf.path.suffix
        timestamp = int(jf.modified.timestamp())
        
        target = dest / f"{base_name}_{timestamp}{ext}"
        
        counter = 1
        while target.exists():
            target = dest / f"{base_name}_{timestamp}_{counter}{ext}"
            counter += 1

        try:
            shutil.move(str(full_source_path), str(target))
        except (PermissionError, FileNotFoundError, shutil.Error, OSError) as e:
            logger.error("Error moviendo archivo %s hacia %s: %s", jf.path, target, e)
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Borra definitivamente los archivos contenidos en la carpeta de revisión.
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
