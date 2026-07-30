"""
diskreport.py — análisis de uso de disco.

SOLO LECTURA: mide y reporta, nunca borra ni mueve. Sirve para responder
"¿en qué se me fue el espacio?" antes de decidir qué limpiar.

Incluye:
  - Espacio libre/usado por unidad.
  - Los archivos más grandes.
  - Uso agrupado por extensión (qué tipo de archivo ocupa más).
  - Las subcarpetas más pesadas.

Todas las funciones que recorren disco saltean carpetas de sistema usando
`safety.is_protected_path`, así un análisis de "toda la unidad C:" no se
mete en Windows ni en Program Files.
"""

from __future__ import annotations
import os
import shutil
import heapq
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Iterable, Dict, List, Tuple

from safety import is_protected_path

__all__ = [
    "FileEntry",
    "ExtensionUsage",
    "FolderUsage",
    "DriveUsage",
    "format_size",
    "drive_usage",
    "all_drives_usage",
    "walk_files",
    "largest_files",
    "usage_by_extension",
    "largest_folders",
    "total_size",
    "summarize",
]


@dataclass
class FileEntry:
    """Un archivo con su tamaño."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2) if self.size_bytes > 0 else 0.0


@dataclass
class ExtensionUsage:
    """Cuánto ocupa un tipo de archivo."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2) if self.size_bytes > 0 else 0.0


@dataclass
class FolderUsage:
    """Cuánto ocupa una carpeta, contando su contenido."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2) if self.size_bytes > 0 else 0.0


@dataclass
class DriveUsage:
    """Espacio de una unidad."""
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        if self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def is_almost_full(self) -> bool:
        """True si queda menos del 10%: Windows empieza a sufrir ahí."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: int | float) -> str:
    """Formatea bytes en la unidad más legible."""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "0 B"
    if value < 0:
        value = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            decimals = 0 if unit == "B" else 1
            return f"{value:.{decimals}f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def drive_usage(mount: str | os.PathLike) -> DriveUsage | None:
    """Espacio de una unidad concreta. None si no se puede consultar."""
    if not mount:
        return None
    try:
        path_str = os.fspath(mount)
        if not os.path.exists(path_str):
            return None
        usage = shutil.disk_usage(path_str)
    except (OSError, ValueError, TypeError):
        return None
    return DriveUsage(mount=str(mount), total=usage.total, used=usage.used, free=usage.free)


def all_drives_usage(mounts: Iterable[str] | None = None) -> list[DriveUsage]:
    """Espacio de todas las unidades disponibles."""
    if mounts is None:
        if os.name == "nt":
            import string
            # Validamos existencia básica antes de consultar para evitar bloqueos por unidades de red
            mounts = [f"{letter}:\\" for letter in string.ascii_uppercase
                      if os.path.isdir(f"{letter}:\\")]
        else:
            mounts = ["/"]
    results = []
    for mount in mounts:
        if mount:
            usage = drive_usage(mount)
            if usage is not None:
                results.append(usage)
    return results


def walk_files(directory: str | os.PathLike, skip_protected: bool = True) -> Generator[tuple[Path, int], None, None]:
    """
    Genera tuplas (ruta, tamaño) para cada archivo encontrado.
    """
    if not directory:
        return
    try:
        base_path = Path(directory).expanduser().resolve(strict=True)
        # Validación defensiva extra: impedir seguir enlaces simbólicos desde la raíz
        if base_path.is_symlink():
            return
        if not base_path.is_dir() or (skip_protected and is_protected_path(base_path)):
            return
    except (OSError, RuntimeError):
        return

    def should_ignore_entry(entry: os.DirEntry) -> bool:
        """Verifica restricciones de seguridad para una entrada del sistema de archivos."""
        try:
            if entry.is_symlink():
                return True
            if os.name == 'nt':
                if entry.stat(follow_symlinks=False).st_reparse_tag != 0:
                    return True
            path_entry = Path(entry.path).resolve()
            if not path_entry.is_relative_to(base_path):
                return True
            if skip_protected and is_protected_path(path_entry):
                return True
        except (OSError, ValueError):
            return True
        return False

    def recursive_scan(root_path: str) -> Generator[tuple[Path, int], None, None]:
        """Recorre directorios ignorando errores de permisos o cambios de estado."""
        try:
            with os.scandir(root_path) as iterator:
                for entry in iterator:
                    try:
                        if should_ignore_entry(entry):
                            continue
                        if entry.is_dir():
                            yield from recursive_scan(entry.path)
                        else:
                            yield Path(entry.path), entry.stat().st_size
                    except (OSError, PermissionError, FileNotFoundError):
                        continue
        except (OSError, PermissionError, FileNotFoundError):
            return

    yield from recursive_scan(str(base_path))


def largest_files(directory: str | os.PathLike, limit: int = 20, skip_protected: bool = True) -> list[FileEntry]:
    """Los archivos más grandes bajo una carpeta, de mayor a menor."""
    if not directory:
        return []
    return heapq.nlargest(
        max(0, limit), 
        (FileEntry(path=p, size_bytes=s) for p, s in walk_files(directory, skip_protected)),
        key=lambda e: e.size_bytes
    )


def usage_by_extension(directory: str | os.PathLike, limit: int = 15, skip_protected: bool = True) -> list[ExtensionUsage]:
    """Espacio agrupado por extensión, de mayor a menor."""
    if not directory:
        return []
    sizes: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    for path, size in walk_files(directory, skip_protected):
        ext = path.suffix.lower() or "(sin extensión)"
        sizes[ext] += size
        counts[ext] += 1
    
    usage_list = [ExtensionUsage(extension=ext, size_bytes=size, count=counts[ext])
                  for ext, size in sizes.items()]
    
    return heapq.nlargest(max(0, limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: str | os.PathLike, limit: int = 10, skip_protected: bool = True) -> list[FolderUsage]:
    """Calcula el peso total de los elementos contenidos en cada carpeta de primer nivel."""
    if not directory:
        return []
    try:
        base = Path(directory).expanduser().resolve(strict=True)
        if not base.is_dir():
            return []
    except (OSError, RuntimeError):
        return []
        
    folder_map: dict[Path, FolderUsage] = {}
    
    for path, size in walk_files(base, skip_protected):
        try:
            rel = path.resolve().relative_to(base)
            if not rel.parts:
                continue
            top_level = base / rel.parts[0]
            if top_level not in folder_map:
                folder_map[top_level] = FolderUsage(path=top_level, size_bytes=0, file_count=0)
            stats = folder_map[top_level]
            stats.size_bytes += size
            stats.file_count += 1
        except (ValueError, IndexError, OSError, FileNotFoundError):
            continue

    return heapq.nlargest(max(0, limit), folder_map.values(), key=lambda f: f.size_bytes)


def total_size(directory: str | os.PathLike, skip_protected: bool = True) -> tuple[int, int]:
    """Devuelve (bytes totales, cantidad de archivos) bajo una carpeta."""
    if not directory:
        return 0, 0
    total = 0
    count = 0
    # walk_files ya maneja internamente la validación de la existencia de la ruta
    for _, size in walk_files(directory, skip_protected):
        total += size
        count += 1
    return total, count


def summarize(directory: str | os.PathLike, skip_protected: bool = True) -> list[str]:
    """
    Genera un informe textual resumen del uso de disco en el directorio especificado.
    """
    if not directory:
        return ["Error: Ruta no especificada."]
        
    try:
        path_obj = Path(directory).expanduser().resolve(strict=True)
        if not path_obj.is_dir():
            return [f"Error: La ruta '{directory}' no es un directorio válido."]
    except (OSError, RuntimeError):
        return ["Error: No se pudo acceder a la ruta especificada."]
        
    total_bytes: int = 0
    total_files: int = 0
    ext_data_map: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    top_heap: List[Tuple[int, Path]] = []

    for path, size in walk_files(path_obj, skip_protected):
        total_bytes += size
        total_files += 1
            
        ext_name = path.suffix.lower() or "(sin extensión)"
        record = ext_data_map[ext_name]
        record[0] += size
        record[1] += 1
        
        if len(top_heap) < 8:
            heapq.heappush(top_heap, (size, path))
        elif size > top_heap[0][0]:
            heapq.heapreplace(top_heap, (size, path))

    lines = [
        f"Carpeta analizada: {path_obj}",
        f"Total: {format_size(total_bytes)} en {total_files} archivos",
        "",
        "Por tipo de archivo:",
    ]
    
    sorted_exts = sorted(
        ext_data_map.items(),
        key=lambda item: item[1][0], 
        reverse=True
    )[:8]
    
    for ext, data in sorted_exts:
        lines.append(f"  {ext:<18} {format_size(data[0]):>10}  ({data[1]} archivos)")
        
    lines.append("")
    lines.append("Archivos más grandes:")
    
    for size, path in sorted(top_heap, key=lambda x: x[0], reverse=True):
        lines.append(f"  {format_size(size):>10}  {path}")
        
    return lines
