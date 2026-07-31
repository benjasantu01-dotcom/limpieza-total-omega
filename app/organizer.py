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
from safety import is_safe_to_modify, ensure_safe_to_modify

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Extensiones típicas de archivos "basura" / temporales en Windows
JUNK_EXTENSIONS: Final = {
    ".tmp", ".temp", ".log", ".bak", ".old", ".dmp", ".chk", ".cache",
}
# Pre-calculado para eficiencia en loops
_LOWER_JUNK_EXTS: Final = {ext.lower() for ext in JUNK_EXTENSIONS}
_JUNK_EXTS_TUPLE: Final = tuple(_LOWER_JUNK_EXTS)

# Carpetas típicas donde se acumula basura
DEFAULT_SCAN_DIRS: Final = [
    os.path.expandvars(r"%TEMP%"),
    os.path.expandvars(r"%LOCALAPPDATA%\Temp"),
    os.path.expanduser("~/Downloads"),
]

# Carpetas de sistema críticas que nunca se recorren para prevenir daños al SO
SYSTEM_FOLDER_BLOCKLIST: Final = frozenset({"windows", "program files", "program files (x86)", "$recycle.bin", "system volume information"})


def list_available_drives() -> List[str]:
    """
    Detecta unidades montadas en sistemas Windows.

    Returns:
        List[str]: Lista de rutas raíz (ej. ['C:\\', 'D:\\']). Retorna lista vacía si no es Windows.
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
    """
    Representa un archivo candidato a limpieza.
    """
    path: Path
    size_bytes: int
    modified: datetime

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño del archivo en Megabytes (redondeado a 2 decimales)."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def is_junk_extension(self) -> bool:
        """Valida si la extensión está en la lista de permitidas."""
        return self.path.suffix.lower() in _LOWER_JUNK_EXTS


def _generate_unique_target(target: Path) -> Path:
    """
    Genera una ruta única iterando un contador si el archivo destino ya existe.
    """
    if not target.exists():
        return target
        
    parent, stem, suffix = target.parent, target.stem, target.suffix
    counter = 1
    
    while (candidate := parent / f"{stem}_{counter}{suffix}").exists():
        counter += 1
    return candidate


def _is_junk_file(entry: os.DirEntry[str]) -> bool:
    """
    Valida si un archivo es basura y si es seguro realizar operaciones sobre él.
    """
    if not entry.is_file(follow_symlinks=False):
        return False
    if not entry.name.lower().endswith(_JUNK_EXTS_TUPLE):
        return False
    return is_safe_to_modify(Path(entry.path))


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Escanea directorios en busca de archivos temporales mediante recursión segura.
    """
    dirs = directories or DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    blocklist = SYSTEM_FOLDER_BLOCKLIST

    def _walk_dir(base_path: str) -> None:
        try:
            with os.scandir(base_path) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() not in blocklist:
                                _walk_dir(entry.path)
                        elif _is_junk_file(entry):
                            stat = entry.stat()
                            found.append(JunkFile(
                                path=Path(entry.path),
                                size_bytes=stat.st_size,
                                modified=datetime.fromtimestamp(stat.st_mtime)
                            ))
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            pass

    for d in dirs:
        p = Path(d).expanduser()
        if p.exists() and p.is_dir():
            _walk_dir(str(p))
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """
    Ordena una lista de objetos JunkFile según tamaño o fecha.
    """
    if by not in ("size", "date"):
        by = "size"

    key_func: Callable[[JunkFile], Union[int, datetime]] = (
        (lambda f: f.size_bytes) if by == "size" else (lambda f: f.modified)
    )
    return sorted(files, key=key_func, reverse=not ascending)


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """
    Mueve archivos candidatos a una carpeta de cuarentena para revisión humana.
    """
    dest = Path(review_dir).expanduser().resolve()
    ensure_safe_to_modify(dest)
    dest.mkdir(parents=True, exist_ok=True)

    for jf in files:
        try:
            full_source_path = jf.path.resolve()
            
            if not full_source_path.is_file() or not is_safe_to_modify(full_source_path):
                continue
            
            # Evitar bucles de movimiento o mover a sí mismo
            if dest in full_source_path.parents or full_source_path.parent == dest:
                continue

            target = _generate_unique_target(dest / f"{jf.path.stem}_{int(jf.modified.timestamp())}{jf.path.suffix}")
            
            ensure_safe_to_modify(full_source_path)
            ensure_safe_to_modify(target)
            shutil.move(str(full_source_path), str(target))
        except (PermissionError, OSError, shutil.Error):
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina permanentemente archivos desde la carpeta de revisión tras validación.
    """
    dest = Path(review_dir).expanduser().resolve()
    if not dest.exists() or not dest.is_dir():
        return 0

    count = 0
    for f in dest.iterdir():
        try:
            if f.is_file() and is_safe_to_modify(f):
                f.unlink()
                count += 1
        except (PermissionError, OSError):
            continue
    return count
