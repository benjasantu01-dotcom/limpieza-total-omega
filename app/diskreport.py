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
from typing import Generator, Iterable

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
        return round(self.size_bytes / (1024 * 1024), 2)


@dataclass
class ExtensionUsage:
    """Cuánto ocupa un tipo de archivo."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


@dataclass
class FolderUsage:
    """Cuánto ocupa una carpeta, contando su contenido."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


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
        usage = shutil.disk_usage(str(mount))
    except (OSError, ValueError):
        return None
    return DriveUsage(mount=str(mount), total=usage.total, used=usage.used, free=usage.free)


def all_drives_usage(mounts: Iterable[str] | None = None) -> list[DriveUsage]:
    """Espacio de todas las unidades disponibles.

    `mounts` se puede pasar explícitamente (útil para tests); si no, se
    detectan las unidades del sistema.
    """
    if mounts is None:
        if os.name == "nt":
            import string
            mounts = [f"{letter}:\\" for letter in string.ascii_uppercase
                      if os.path.exists(f"{letter}:\\")]
        else:
            mounts = ["/"]
    results = []
    for mount in mounts:
        usage = drive_usage(mount)
        if usage is not None:
            results.append(usage)
    return results


def walk_files(directory: str | os.PathLike, skip_protected: bool = True) -> Generator[tuple[Path, int], None, None]:
    """
    Genera pares (ruta, tamaño) de archivos bajo `directory`.
    
    Implementa una recursión controlada mediante os.walk con manejo de errores
    para archivos bloqueados, inexistentes o con permisos restringidos.
    """
    if not directory:
        return
    try:
        base = Path(directory).expanduser().resolve()
        if not base.exists() or not base.is_dir():
            return
    except (OSError, RuntimeError):
        return
        
    if base.is_symlink():
        return
    if skip_protected and is_protected_path(base):
        return
    
    for root, subdirs, files in os.walk(base, onerror=lambda _: None):
        root_path = Path(root)
            
        if skip_protected:
            subdirs[:] = [d for d in subdirs if not is_protected_path(root_path / d)]
            
        for name in files:
            path = root_path / name
            try:
                # Verificación de seguridad adicional por cada archivo hallado
                if path.is_symlink() or (skip_protected and is_protected_path(path)):
                    continue
                yield path, path.stat().st_size
            except (OSError, PermissionError, FileNotFoundError):
                continue


def largest_files(directory: str | os.PathLike, limit: int = 20, skip_protected: bool = True) -> list[FileEntry]:
    """Los archivos más grandes bajo una carpeta, de mayor a menor."""
    # Usamos nlargest para evitar ordenar toda la lista en memoria
    return heapq.nlargest(
        limit, 
        (FileEntry(path=p, size_bytes=s) for p, s in walk_files(directory, skip_protected)),
        key=lambda e: e.size_bytes
    )


def usage_by_extension(directory: str | os.PathLike, limit: int = 15, skip_protected: bool = True) -> list[ExtensionUsage]:
    """Espacio agrupado por extensión, de mayor a menor."""
    sizes: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    for path, size in walk_files(directory, skip_protected):
        ext = path.suffix.lower() or "(sin extensión)"
        sizes[ext] += size
        counts[ext] += 1
    
    usage_list = [ExtensionUsage(extension=ext, size_bytes=size, count=counts[ext])
                  for ext, size in sizes.items()]
    
    return heapq.nlargest(limit, usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: str | os.PathLike, limit: int = 10, skip_protected: bool = True) -> list[FolderUsage]:
    """
    Calcula el peso acumulado de las subcarpetas directas o archivos en el nivel actual.
    """
    if not directory:
        return []
    try:
        base = Path(directory).expanduser().resolve()
        if skip_protected and is_protected_path(base):
            return []
    except (OSError, RuntimeError):
        return []
        
    folder_map: dict[Path, FolderUsage] = {}
    
    for path, size in walk_files(base, skip_protected):
        try:
            relative_path = path.relative_to(base)
        except ValueError:
            continue

        # Manejo robusto: si relative_path está vacío (archivo en raíz), top_level es base
        if not relative_path.parts:
            top_level = base
        else:
            top_level = base / relative_path.parts[0]
        
        if top_level not in folder_map:
            folder_map[top_level] = FolderUsage(path=top_level, size_bytes=0, file_count=0)
            
        stats = folder_map[top_level]
        stats.size_bytes += size
        stats.file_count += 1

    return heapq.nlargest(limit, folder_map.values(), key=lambda f: f.size_bytes)


def total_size(directory: str | os.PathLike, skip_protected: bool = True) -> tuple[int, int]:
    """Devuelve (bytes totales, cantidad de archivos) bajo una carpeta."""
    total = 0
    count = 0
    for _, size in walk_files(directory, skip_protected):
        total += size
        count += 1
    return total, count


def summarize(directory: str | os.PathLike, skip_protected: bool = True) -> list[str]:
    """Resumen legible del uso de disco de una carpeta."""
    total, count = total_size(directory, skip_protected)
    lines = [
        f"Carpeta analizada: {directory}",
        f"Total: {format_size(total)} en {count} archivos",
        "",
        "Por tipo de archivo:",
    ]
    for usage in usage_by_extension(directory, limit=8, skip_protected=skip_protected):
        lines.append(f"  {usage.extension:<18} {format_size(usage.size_bytes):>10}  ({usage.count} archivos)")
    lines.append("")
    lines.append("Archivos más grandes:")
    for entry in largest_files(directory, limit=8, skip_protected=skip_protected):
        lines.append(f"  {format_size(entry.size_bytes):>10}  {entry.path}")
    return lines
