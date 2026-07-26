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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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


def list_available_drives() -> list[str]:
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
    path: Path
    size_bytes: int
    modified: datetime

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


# Carpetas de sistema críticas que nunca se recorren, incluso si el
# usuario elige escanear una unidad completa (ej. "C:\"). Evita que un
# escaneo de disco completo se meta en Windows/Program Files.
SYSTEM_FOLDER_BLOCKLIST = {"windows", "program files", "program files (x86)", "$recycle.bin", "system volume information"}


def scan_for_junk(directories: list[str] | None = None) -> list[JunkFile]:
    """Recorre las carpetas indicadas y devuelve candidatos a basura.
    No modifica nada en el disco. Si una de las 'directories' es la raíz
    de una unidad (ej. 'D:\\'), se recorre completa salvo las carpetas
    de sistema críticas listadas en SYSTEM_FOLDER_BLOCKLIST."""
    dirs = directories or DEFAULT_SCAN_DIRS
    found: list[JunkFile] = []

    for d in dirs:
        p = Path(d)
        if not p.exists():
            continue
        for root, subdirs, files in os.walk(p):
            subdirs[:] = [sd for sd in subdirs if sd.lower() not in SYSTEM_FOLDER_BLOCKLIST]
            for name in files:
                fp = Path(root) / name
                try:
                    stat = fp.stat()
                except (PermissionError, FileNotFoundError):
                    continue
                if fp.suffix.lower() in JUNK_EXTENSIONS:
                    found.append(
                        JunkFile(
                            path=fp,
                            size_bytes=stat.st_size,
                            modified=datetime.fromtimestamp(stat.st_mtime),
                        )
                    )
    return found


def sort_junk(files: list[JunkFile], by: str = "size", ascending: bool = True) -> list[JunkFile]:
    """Ordena por 'size' o 'date'."""
    key = (lambda f: f.size_bytes) if by == "size" else (lambda f: f.modified)
    return sorted(files, key=key, reverse=not ascending)


def stage_for_review(files: list[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """Mueve (no borra) los archivos encontrados a una carpeta de revisión,
    preservando el nombre original + timestamp para evitar colisiones."""
    dest = Path(os.path.expanduser(review_dir))
    dest.mkdir(parents=True, exist_ok=True)

    for jf in files:
        target = dest / f"{jf.path.stem}_{int(jf.modified.timestamp())}{jf.path.suffix}"
        try:
            shutil.move(str(jf.path), str(target))
        except (PermissionError, FileNotFoundError, shutil.Error):
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """Borra DEFINITIVAMENTE lo que quedó en la carpeta de revisión.
    Acción explícita: el usuario debe llamarla a propósito (ej. botón
    'Vaciar revisados' en la UI), nunca se dispara automáticamente."""
    dest = Path(os.path.expanduser(review_dir))
    if not dest.exists():
        return 0
    count = 0
    for f in dest.iterdir():
        if f.is_file():
            f.unlink()
            count += 1
    return count
