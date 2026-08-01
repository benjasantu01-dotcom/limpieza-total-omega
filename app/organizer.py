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
from typing import List, Optional, Final, Callable, Union, TypeAlias
from safety import is_safe_to_modify, ensure_safe_to_modify

# Configuración de log para seguimiento de errores no críticos
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Definiciones de tipo para claridad en firmas complejas
SortKey: TypeAlias = Union[int, datetime]

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
SYSTEM_FOLDER_BLOCKLIST: Final = {
    "windows", "program files", "program files (x86)", "$recycle.bin", "system volume information"
}


def list_available_drives() -> List[str]:
    """
    Detecta unidades montadas en sistemas Windows.

    Returns:
        List[str]: Lista de rutas raíz (ej. ['C:\\', 'D:\\']). 
        Retorna lista vacía si el SO no es Windows o no se detectan unidades.
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
        """Valida si la extensión del archivo coincide con las permitidas."""
        return self.path.suffix.lower() in _LOWER_JUNK_EXTS


def _generate_unique_target(target: Path) -> Path:
    """
    Genera una ruta única para el movimiento de archivos. Si el destino ya existe,
    se anexa un contador numérico para evitar colisiones sin sobrescribir.
    """
    if not target.exists():
        return target
        
    parent, stem, suffix = target.parent, target.stem, target.suffix
    counter = 1
    
    while (candidate := parent / f"{stem}_{counter}{suffix}").exists():
        counter += 1
    return candidate


def _is_valid_junk(entry: os.DirEntry[str]) -> bool:
    """
    Valida mediante heurística si un archivo es basura y si es seguro tocarlo.
    Se ignora cualquier archivo que sea un enlace simbólico o reparse point para
    evitar problemas de recursión infinita o manipulación fuera de límites.
    """
    if entry.is_symlink():
        return False
    if not entry.is_file():
        return False
    if not entry.name.lower().endswith(_JUNK_EXTS_TUPLE):
        return False
    return is_safe_to_modify(Path(entry.path))


def scan_for_junk(directories: Optional[List[str]] = None) -> List[JunkFile]:
    """
    Realiza un escaneo recursivo buscando archivos temporales candidatos.
    Omite directorios bloqueados y utiliza 'is_safe_to_modify' para garantizar
    que solo se consideren archivos cuya modificación no ponga en riesgo la integridad.
    """
    dirs = directories or DEFAULT_SCAN_DIRS
    found: List[JunkFile] = []
    blocklist = SYSTEM_FOLDER_BLOCKLIST

    def _walk_dir(base_path: str) -> None:
        try:
            with os.scandir(base_path) as it:
                for entry in it:
                    try:
                        # Exclusión estricta de symlinks para prevenir fugas de directorio
                        if entry.is_symlink():
                            continue
                        if entry.is_dir():
                            if entry.name.lower() not in blocklist:
                                _walk_dir(entry.path)
                        elif entry.name.lower().endswith(_JUNK_EXTS_TUPLE):
                            if is_safe_to_modify(Path(entry.path)):
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
        if d:
            p = Path(d).expanduser()
            if p.exists() and p.is_dir():
                _walk_dir(str(p))
    return found


def sort_junk(files: List[JunkFile], by: str = "size", ascending: bool = True) -> List[JunkFile]:
    """
    Ordena una lista de objetos JunkFile según tamaño o fecha.
    """
    if not files:
        return []
        
    by_normalized = by.lower() if by else "size"
    if by_normalized not in ("size", "date"):
        by_normalized = "size"

    key_func: Callable[[JunkFile], SortKey] = (
        (lambda f: f.size_bytes) if by_normalized == "size" else (lambda f: f.modified)
    )
    return sorted(files, key=key_func, reverse=not ascending)


def stage_for_review(files: List[JunkFile], review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> Path:
    """
    Mueve archivos candidatos a una carpeta de revisión segura.
    Realiza verificaciones de exclusión de symlinks y comprueba mediante 
    'ensure_safe_to_modify' que la operación no vulnere las reglas del sistema.
    """
    if not review_dir:
        raise ValueError("La ruta de revisión no puede estar vacía")

    dest = Path(review_dir).expanduser().resolve()
    if dest.is_symlink():
        raise PermissionError("Ruta de destino inválida: symlink detectado.")
        
    ensure_safe_to_modify(dest)
    dest.mkdir(parents=True, exist_ok=True)

    for jf in files:
        if not isinstance(jf, JunkFile) or not jf.path:
            continue
        try:
            # Validar existencia física y que no sea un enlace externo
            if not jf.path.exists() or jf.path.is_symlink():
                continue
            
            full_source_path = jf.path.resolve()
            
            if not is_safe_to_modify(full_source_path):
                continue
            if dest in full_source_path.parents or full_source_path.parent == dest:
                continue
            
            # Comprobación de disponibilidad (evitar mover archivos en uso activo)
            try:
                with open(full_source_path, 'rb+'):
                    pass
            except (IOError, OSError):
                continue

            target_base = dest / f"{jf.path.stem}_{int(jf.modified.timestamp())}{jf.path.suffix}"
            target = _generate_unique_target(target_base)
            
            if dest not in target.parents:
                continue
            
            ensure_safe_to_modify(full_source_path)
            ensure_safe_to_modify(target)
            shutil.move(str(full_source_path), str(target))
        except (PermissionError, OSError, shutil.Error):
            continue
    return dest


def delete_reviewed(review_dir: str = "~/LimpiezaTotalOmega/_Para_Revisar") -> int:
    """
    Elimina permanentemente archivos desde la carpeta de revisión.
    Utiliza 'is_safe_to_modify' para asegurar que el archivo no haya sido
    reemplazado por un enlace simbólico o un archivo del sistema durante el proceso.
    
    Returns:
        int: Número de archivos eliminados con éxito.
    """
    if not review_dir:
        return 0

    try:
        dest = Path(review_dir).expanduser().resolve()
    except (RuntimeError, OSError):
        return 0
        
    if not dest.exists() or not dest.is_dir() or dest.is_symlink():
        return 0

    count = 0
    for f in dest.iterdir():
        try:
            # El borrado se limita a archivos simples y validados
            if f.is_file() and not f.is_symlink() and is_safe_to_modify(f):
                f.unlink()
                count += 1
        except (PermissionError, OSError):
            continue
    return count
