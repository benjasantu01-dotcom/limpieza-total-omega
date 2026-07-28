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
    Genera tuplas (ruta_absoluta, tamaño_en_bytes) para cada archivo encontrado.
    """
    if not directory:
        return
    try:
        base = Path(directory).expanduser().resolve(strict=False)
        if not base.exists() or not base.is_dir():
            return
    except (OSError, RuntimeError):
        return
    
    for root, subdirs, files in os.walk(base, onerror=lambda _: None):
        try:
            root_path = Path(root)
            
            # Seguridad: no permitir escape del directorio base
            if not str(root_path).startswith(str(base)):
                subdirs.clear()
                continue

            # Seguridad: evitar seguir enlaces simbólicos y puntos de reparse
            if root_path.is_symlink() or (os.name == 'nt' and root_path.stat().st_reparse_tag != 0):
                subdirs.clear()
                continue

            # Seguridad: evitar entrar en carpetas protegidas
            if skip_protected and is_protected_path(root_path):
                subdirs.clear()
                continue
                
            if skip_protected:
                subdirs[:] = [d for d in subdirs if not is_protected_path(root_path / d)]
                
            for name in files:
                try:
                    path = root_path / name
                    st = path.lstat()
                    # Seguridad: evitar archivos en carpetas protegidas detectadas tarde
                    if path.is_symlink() or (os.name == 'nt' and getattr(st, 'st_reparse_tag', 0) != 0):
                        continue
                    if skip_protected and is_protected_path(path):
                        continue
                    yield path, st.st_size
                except (OSError, PermissionError, FileNotFoundError, AttributeError, ValueError):
                    continue
        except (OSError, PermissionError):
            continue


def largest_files(directory: str | os.PathLike, limit: int = 20, skip_protected: bool = True) -> list[FileEntry]:
    """Los archivos más grandes bajo una carpeta, de mayor a menor."""
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
    """Calcula el peso total de los elementos contenidos en cada carpeta de primer nivel."""
    if not directory:
        return []
    try:
        base = Path(directory).expanduser().resolve(strict=False)
        if not base.exists() or not base.is_dir():
            return []
    except (OSError, RuntimeError):
        return []
        
    folder_map: dict[Path, FolderUsage] = {}
    
    for path, size in walk_files(base, skip_protected):
        try:
            # Seguridad adicional: validar que path esté bajo base antes de calcular relativo
            if not str(path).startswith(str(base)):
                continue
            rel = path.relative_to(base)
            if not rel.parts:
                continue
            top_level = base / rel.parts[0]
        except (ValueError, IndexError):
            continue
            
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
    """Genera un informe textual del uso de disco en el directorio especificado."""
    if not directory:
        return ["Error: Ruta vacía."]
        
    try:
        path_obj = Path(directory).expanduser().resolve(strict=False)
        if not path_obj.exists() or not path_obj.is_dir():
            return [f"Error: La carpeta '{directory}' no es válida."]
    except (OSError, RuntimeError):
        return ["Error: No se pudo acceder a la ruta."]
        
    total_bytes: int = 0
    total_files: int = 0
    extension_map: dict[str, ExtensionUsage] = {}
    
    # Mantenemos un heap de tamaño fijo para evitar cargar todos los archivos en RAM
    top_8_files = []

    for path, size in walk_files(path_obj, skip_protected):
        total_bytes += size
        total_files += 1
            
        ext_name = path.suffix.lower() or "(sin extensión)"
        if ext_name not in extension_map:
            extension_map[ext_name] = ExtensionUsage(ext_name, 0, 0)
        ext_data = extension_map[ext_name]
        ext_data.size_bytes += size
        ext_data.count += 1
        
        # Mantenemos solo los 8 más grandes en el heap
        heapq.heappush(top_8_files, (size, path))
        if len(top_8_files) > 8:
            heapq.heappop(top_8_files)

    lines = [
        f"Carpeta analizada: {path_obj}",
        f"Total: {format_size(total_bytes)} en {total_files} archivos",
        "",
        "Por tipo de archivo:",
    ]
    
    sorted_exts = heapq.nlargest(8, extension_map.values(), key=lambda x: x.size_bytes)
    for ext_data in sorted_exts:
        lines.append(f"  {ext_data.extension:<18} {format_size(ext_data.size_bytes):>10}  ({ext_data.count} archivos)")
        
    lines.append("")
    lines.append("Archivos más grandes:")
    for size, path in sorted(top_8_files, key=lambda x: x[0], reverse=True):
        lines.append(f"  {format_size(size):>10}  {path}")
        
    return lines
