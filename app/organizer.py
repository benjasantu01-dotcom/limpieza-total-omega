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
from app.safety import ensure_safe_to_modify

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

# Carpetas de sistema críticas que nunca se recorren.
SYSTEM_FOLDER_BLOCKLIST = {"windows", "program files", "program files (x86)", "$recycle.bin", "system volume information"}


def list_available_drives() -> List[str]:
    """
    Devuelve las letras de unidad disponibles en Windows (ej. ['C:\\', 'D:\\']).
    """
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
    path: Path
    size_bytes: int
    modified: datetime

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Recorre jerárquicamente las rutas proporcionadas en busca de archivos basura.
    
    Aplica filtros de seguridad para ignorar carpetas críticas (blocklist)
    y verificar la integridad de la ruta mediante ensure_safe_to_modify.
    """
    if directories is not None and not isinstance(directories, list):
        logger.error("El parámetro directories debe ser una lista.")
        return []

    dirs = directories or DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    junk_set = {ext.lower() for ext in JUNK_EXTENSIONS}
    blocklist = {s.lower() for s in SYSTEM_FOLDER_BLOCKLIST}

    def _walk_dir(base_path: str):
        try:
            with os.scandir(base_path) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() not in blocklist:
                                _walk_dir(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            if Path(entry.name).suffix.lower() in junk_set:
                                full_path = Path(entry.path)
                                # Seguridad: Solo añadir si la ruta pasa el filtro de app/safety.py
                                if ensure_safe_to_modify(full_path):
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
            pass

    for d in dirs:
        if not isinstance(d, str):
            continue
        p = Path(d).expanduser()
        if p.exists() and p.is_dir():
            _walk_dir(str(p))
        else:
            logger.warning(f"Ruta de escaneo inválida: {p}")
            
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    if not isinstance(files, list):
        return []
        
    if by not in ("size", "date"):
        logger.warning("Criterio de ordenación desconocido '%s', usando 'size' por defecto.", by)
        by = "size"

    key_func = (lambda f: f.size_bytes) if by == "size" else (lambda f: f.modified)
    return sorted(files, key=key_func, reverse=not ascending)


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """
    Mueve los archivos detectados a una ubicación de cuarentena para su validación manual.
    
    Evita colisiones de nombres añadiendo un timestamp y un contador secuencial.
    """
    if not files or not isinstance(files, list):
        logger.warning("La lista de archivos a organizar está vacía o es inválida.")
        return Path(review_dir).expanduser()

    dest = Path(review_dir).expanduser().resolve()
    # Seguridad: Validar que el directorio de destino sea seguro para modificar
    if not ensure_safe_to_modify(dest):
        raise PermissionError(f"El directorio de destino no es seguro: {dest}")

    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("No se pudo preparar el directorio de revisión %s: %s", dest, e)
        raise

    for jf in files:
        if not isinstance(jf, JunkFile) or not jf.path:
            continue
            
        try:
            full_source_path = jf.path.resolve()
            
            # Verificación de robustez: existencia, tipo y permisos antes de mover
            if not full_source_path.exists() or not full_source_path.is_file():
                continue
            if not os.access(full_source_path, os.R_OK):
                continue
            
            # Impedir mover archivos fuera de los límites de seguridad definidos
            if not ensure_safe_to_modify(full_source_path):
                continue
                
            # Evitar bucles o recursión sobre la propia carpeta de destino
            if dest in full_source_path.parents or full_source_path.parent == dest:
                continue

            base_name = jf.path.stem
            ext = jf.path.suffix
            timestamp = int(jf.modified.timestamp())
            
            target = dest / f"{base_name}_{timestamp}{ext}"
            counter = 1
            while target.exists():
                target = dest / f"{base_name}_{timestamp}_{counter}{ext}"
                counter += 1

            shutil.move(str(full_source_path), str(target))
        except (PermissionError, OSError, shutil.Error) as e:
            logger.error("Error procesando archivo %s: %s", jf.path, e)
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    dest = Path(review_dir).expanduser()
    if not dest.exists():
        logger.info("Directorio de revisión no encontrado: %s", dest)
        return 0

    count = 0
    for f in dest.iterdir():
        try:
            if f.is_file():
                # Validación de seguridad antes de ejecutar el borrado real
                if ensure_safe_to_modify(f):
                    f.unlink()
                    count += 1
        except (PermissionError, OSError) as e:
            logger.error("No se pudo borrar permanentemente el archivo %s: %s", f, e)
            continue
    return count
