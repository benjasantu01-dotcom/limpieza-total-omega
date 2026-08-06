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
from typing import Generator, Iterable, Dict, List, Tuple, Optional, Union

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
    """Un archivo individual con su ruta y tamaño en bytes."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño del archivo convertido a Megabytes (MB)."""
        return round(self.size_bytes / (1024 * 1024), 2) if self.size_bytes > 0 else 0.0


@dataclass
class ExtensionUsage:
    """Acumulado de espacio y cantidad para una extensión de archivo específica."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        """Calcula el espacio total ocupado por esta extensión en Megabytes (MB)."""
        return round(self.size_bytes / (1024 * 1024), 2) if self.size_bytes > 0 else 0.0


@dataclass
class FolderUsage:
    """Acumulado de espacio y conteo de archivos para una ruta de carpeta."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        """Calcula el espacio total de la carpeta en Megabytes (MB)."""
        return round(self.size_bytes / (1024 * 1024), 2) if self.size_bytes > 0 else 0.0


@dataclass
class DriveUsage:
    """Representación del estado de almacenamiento de una unidad lógica."""
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        """Porcentaje de espacio utilizado (0.0 a 100.0). Maneja división por cero."""
        if self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def is_almost_full(self) -> bool:
        """True si el espacio libre es menor al 10% del total de la unidad."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """
    Convierte un valor de bytes a una cadena legible con su unidad (B, KB, MB, GB, TB).
    """
    if num is None:
        return "0 B"
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


def drive_usage(mount: Union[str, os.PathLike, None]) -> Optional[DriveUsage]:
    """
    Consulta el estado de almacenamiento de una unidad montada localmente.
    """
    if not mount:
        return None
    try:
        path_str = os.fspath(mount)
        if path_str.startswith(("\\\\", "//")):
            return None
        p = Path(path_str).expanduser().resolve()
        if not p.exists() or not p.is_absolute() or is_protected_path(p):
            return None
        usage = shutil.disk_usage(path_str)
        return DriveUsage(mount=str(mount), total=usage.total, used=usage.used, free=usage.free)
    except (OSError, ValueError, TypeError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """
    Obtiene el reporte de uso para una lista de unidades o todas las detectadas.
    """
    if mounts is None:
        if os.name == "nt":
            import string
            mounts = [f"{letter}:\\" for letter in string.ascii_uppercase
                      if os.path.exists(f"{letter}:\\")]
        else:
            mounts = ["/"]
    results = []
    if mounts:
        for mount in mounts:
            if mount:
                usage = drive_usage(mount)
                if usage is not None:
                    results.append(usage)
    return results


def walk_files(directory: Union[str, os.PathLike], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Recorre recursivamente un directorio omitiendo enlaces simbólicos y rutas protegidas.
    """
    if not directory:
        return

    base_path = Path(directory).expanduser().resolve()
    if not base_path.exists() or not base_path.is_dir() or (skip_protected and is_protected_path(base_path)):
        return

    visited_directories: set[Path] = {base_path}

    def scan_level(current_path: Path) -> Generator[Tuple[Path, int], None, None]:
        try:
            with os.scandir(current_path) as iterator:
                for entry in iterator:
                    try:
                        # Usar entry directamente es más eficiente que convertir a Path
                        if entry.is_symlink():
                            continue
                        
                        if entry.is_dir():
                            full_path = Path(entry.path)
                            if full_path not in visited_directories:
                                if skip_protected and is_protected_path(full_path):
                                    continue
                                visited_directories.add(full_path)
                                yield from scan_level(full_path)
                        else:
                            yield Path(entry.path), entry.stat().st_size
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            return

    yield from scan_level(base_path)


def largest_files(directory: Union[str, os.PathLike], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Identifica los N archivos más grandes en la ruta dada usando un min-heap."""
    if not directory:
        return []
    return heapq.nlargest(
        max(0, limit), 
        (FileEntry(path=p, size_bytes=s) for p, s in walk_files(directory, skip_protected)),
        key=lambda e: e.size_bytes
    )


def usage_by_extension(directory: Union[str, os.PathLike], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """
    Calcula el uso de espacio total agrupado por extensión de archivo.
    """
    if not directory:
        return []
    sizes: Dict[str, int] = defaultdict(int)
    counts: Dict[str, int] = defaultdict(int)
    for path, size in walk_files(directory, skip_protected):
        ext = path.suffix.lower() or "(sin extensión)"
        sizes[ext] += size
        counts[ext] += 1
    
    usage_list = [ExtensionUsage(extension=ext, size_bytes=size, count=counts[ext])
                  for ext, size in sizes.items()]
    
    return heapq.nlargest(max(0, limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """
    Calcula qué subdirectorios de primer nivel ocupan más espacio total.
    """
    if not directory:
        return []
    try:
        base = Path(directory).expanduser().resolve()
        if not base.exists() or not base.is_dir():
            return []
        
        folder_map: Dict[Path, FolderUsage] = {}
        for path, size in walk_files(base, skip_protected):
            try:
                rel = path.relative_to(base)
                top_level = base / rel.parts[0]
                
                if top_level not in folder_map:
                    folder_map[top_level] = FolderUsage(path=top_level, size_bytes=size, file_count=1)
                else:
                    folder_map[top_level].size_bytes += size
                    folder_map[top_level].file_count += 1
            except (ValueError, IndexError):
                continue

        return heapq.nlargest(max(0, limit), folder_map.values(), key=lambda f: f.size_bytes)
    except (OSError, RuntimeError):
        return []


def total_size(directory: Union[str, os.PathLike], skip_protected: bool = True) -> Tuple[int, int]:
    """Calcula la suma total de bytes y cantidad de archivos en un directorio."""
    if not directory:
        return 0, 0
    total = 0
    count = 0
    for _, size in walk_files(directory, skip_protected):
        total += size
        count += 1
    return total, count


def summarize(directory: Union[str, os.PathLike], skip_protected: bool = True) -> List[str]:
    """
    Genera un reporte de texto con el resumen de uso de disco analizado.
    """
    if not directory:
        return ["Error: Ruta no proporcionada."]
        
    try:
        path_obj = Path(directory).expanduser().resolve()
        if not path_obj.exists() or not path_obj.is_dir():
            return [f"Error: La ruta '{directory}' no es un directorio válido."]
    except (OSError, RuntimeError):
        return ["Error: No se pudo acceder a la ruta especificada."]
        
    ext_sizes: Dict[str, int] = defaultdict(int)
    ext_counts: Dict[str, int] = defaultdict(int)
    top_heap: List[Tuple[int, str]] = []
    total_bytes = 0
    total_files = 0
    
    for path, size in walk_files(path_obj, skip_protected):
        total_bytes += size
        total_files += 1
        ext_name = path.suffix.lower() or "(sin extensión)"
        ext_sizes[ext_name] += size
        ext_counts[ext_name] += 1
        
        if len(top_heap) < 8:
            heapq.heappush(top_heap, (size, str(path)))
        elif size > top_heap[0][0]:
            heapq.heapreplace(top_heap, (size, str(path)))

    lines: List[str] = [
        f"Carpeta analizada: {path_obj}",
        f"Total: {format_size(total_bytes)} en {total_files} archivos",
        "",
        "Por tipo de archivo:",
    ]
    
    sorted_exts: List[Tuple[str, int]] = heapq.nlargest(8, ext_sizes.items(), key=lambda item: item[1])
    for ext, size in sorted_exts:
        lines.append(f"  {ext:<18} {format_size(size):>10}  ({ext_counts[ext]} archivos)")
        
    lines.append("")
    lines.append("Archivos más grandes:")
    for size, path in sorted(top_heap, key=lambda x: x[0], reverse=True):
        lines.append(f"  {format_size(size):>10}  {path}")
        
    return lines
