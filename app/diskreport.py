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
    """Representa un archivo individual, su ubicación y peso en bytes."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño del archivo convertido a Megabytes (MB)."""
        return round(self.size_bytes / (1024 * 1024), 2) if self.size_bytes > 0 else 0.0


@dataclass
class ExtensionUsage:
    """Agregado estadístico del espacio ocupado por una extensión específica."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        """Calcula el espacio total ocupado por esta extensión en Megabytes (MB)."""
        return round(self.size_bytes / (1024 * 1024), 2) if self.size_bytes > 0 else 0.0


@dataclass
class FolderUsage:
    """Métrica de uso de espacio para un directorio específico."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        """Calcula el espacio total de la carpeta en Megabytes (MB)."""
        return round(self.size_bytes / (1024 * 1024), 2) if self.size_bytes > 0 else 0.0


@dataclass
class DriveUsage:
    """Estado de almacenamiento de una unidad lógica."""
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        """Porcentaje de espacio utilizado (0.0 a 100.0)."""
        if self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def is_almost_full(self) -> bool:
        """Indica si el espacio libre es inferior al 10% del total."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """
    Convierte bytes a una cadena legible (B, KB, MB, GB, TB).
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
    Obtiene el estado de una unidad. Retorna None si la ruta es inválida, 
    protegida o inaccesible (incluyendo rutas UNC por política de seguridad).
    """
    if not mount:
        return None
    try:
        path_str = os.fspath(mount)
        if path_str.startswith(("\\\\", "//")):
            return None
        p = Path(path_str).expanduser().resolve()
        
        # Filtro de seguridad: no analizar rutas protegidas o inexistentes
        if not p.exists() or not p.is_absolute() or is_protected_path(p):
            return None
            
        usage = shutil.disk_usage(path_str)
        return DriveUsage(mount=str(mount), total=usage.total, used=usage.used, free=usage.free)
    except (OSError, ValueError, TypeError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """
    Lista el uso de unidades. En Windows detecta letras de unidad, en Unix la raíz.
    """
    if mounts is None:
        if os.name == "nt":
            import string
            mounts = [f"{letter}:\\" for letter in string.ascii_uppercase
                      if os.path.exists(f"{letter}:\\")]
        else:
            mounts = ["/"]
    results: List[DriveUsage] = []
    for mount in mounts:
        if mount:
            usage = drive_usage(mount)
            if usage is not None:
                results.append(usage)
    return results


def walk_files(directory: Union[str, os.PathLike], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Generador recursivo de archivos. 
    Seguridad: Implementa detección de puntos de reparse (junctions/symlinks) y 
    evita ciclos mediante `visited_directories`.
    """
    if not directory:
        return

    try:
        base_path = Path(directory).expanduser().resolve()
        if not base_path.exists() or not base_path.is_dir() or (skip_protected and is_protected_path(base_path)):
            return
    except (OSError, RuntimeError):
        return

    visited_directories: set[Path] = {base_path}

    def scan_level(current_path: Path) -> Generator[Tuple[Path, int], None, None]:
        try:
            with os.scandir(current_path) as iterator:
                for entry in iterator:
                    try:
                        # Evitar seguir enlaces simbólicos y puntos de reparse de Windows
                        if entry.is_symlink():
                            continue
                        if os.name == 'nt':
                            # 0x400 es FILE_ATTRIBUTE_REPARSE_POINT
                            if entry.stat().st_file_attributes & 0x400:
                                continue
                        
                        if entry.is_dir():
                            full_path = Path(entry.path).resolve()
                            if full_path not in visited_directories:
                                if skip_protected and is_protected_path(full_path):
                                    continue
                                visited_directories.add(full_path)
                                yield from scan_level(full_path)
                        else:
                            # Leer tamaño directamente sin seguir links para evitar errores de permisos
                            yield Path(entry.path), entry.stat(follow_symlinks=False).st_size
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            return

    yield from scan_level(base_path)


def largest_files(directory: Union[str, os.PathLike], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Retorna los archivos más grandes mediante un min-heap para eficiencia O(n log k)."""
    if not directory or limit <= 0:
        return []
    return heapq.nlargest(
        limit, 
        (FileEntry(path=p, size_bytes=s) for p, s in walk_files(directory, skip_protected)),
        key=lambda e: e.size_bytes
    )


def usage_by_extension(directory: Union[str, os.PathLike], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Calcula la ocupación total por extensión de archivo."""
    if not directory or limit <= 0:
        return []
    
    size_map: Dict[str, int] = defaultdict(int)
    count_map: Dict[str, int] = defaultdict(int)
    
    for path, size in walk_files(directory, skip_protected):
        ext = path.suffix.lower() or "(sin extensión)"
        size_map[ext] += size
        count_map[ext] += 1
    
    usage_list = [ExtensionUsage(extension=ext, size_bytes=size, count=count_map[ext])
                  for ext, size in size_map.items()]
    
    return heapq.nlargest(limit, usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Identifica las subcarpetas de primer nivel que consumen más espacio."""
    if not directory or limit <= 0:
        return []
    try:
        base = Path(directory).expanduser().resolve()
        if not base.exists() or not base.is_dir() or (skip_protected and is_protected_path(base)):
            return []
        
        folder_map: Dict[Path, FolderUsage] = {}
        for path, size in walk_files(base, skip_protected):
            try:
                rel = path.relative_to(base)
                if not rel.parts:
                    continue
                top_level = base / rel.parts[0]
                
                if top_level not in folder_map:
                    folder_map[top_level] = FolderUsage(path=top_level, size_bytes=size, file_count=1)
                else:
                    folder_map[top_level].size_bytes += size
                    folder_map[top_level].file_count += 1
            except (ValueError, IndexError, OSError):
                continue

        return heapq.nlargest(limit, folder_map.values(), key=lambda f: f.size_bytes)
    except (OSError, RuntimeError):
        return []


def total_size(directory: Union[str, os.PathLike], skip_protected: bool = True) -> Tuple[int, int]:
    """Retorna (bytes_totales, cantidad_archivos) de un directorio."""
    if not directory:
        return 0, 0
    total_bytes = 0
    file_count = 0
    for _, size in walk_files(directory, skip_protected):
        total_bytes += size
        file_count += 1
    return total_bytes, file_count


def summarize(directory: Union[str, os.PathLike], skip_protected: bool = True) -> List[str]:
    """Genera un resumen en texto del análisis de uso de disco."""
    if not directory:
        return ["Error: Ruta no proporcionada."]
        
    try:
        path_obj = Path(directory).expanduser().resolve()
        if not path_obj.exists() or not path_obj.is_dir():
            return [f"Error: La ruta '{directory}' no es un directorio válido."]
    except (OSError, RuntimeError):
        return ["Error: No se pudo acceder a la ruta especificada."]
        
    ext_size_map: Dict[str, int] = defaultdict(int)
    ext_count_map: Dict[str, int] = defaultdict(int)
    top_files_heap: List[Tuple[int, str]] = []
    
    total_bytes = 0
    total_files = 0
    
    for path, size in walk_files(path_obj, skip_protected):
        total_bytes += size
        total_files += 1
        
        ext_name = path.suffix.lower() or "(sin extensión)"
        ext_size_map[ext_name] += size
        ext_count_map[ext_name] += 1
        
        if len(top_files_heap) < 8:
            heapq.heappush(top_files_heap, (size, str(path)))
        elif size > top_files_heap[0][0]:
            heapq.heapreplace(top_files_heap, (size, str(path)))

    lines = [
        f"Carpeta analizada: {path_obj}",
        f"Total: {format_size(total_bytes)} en {total_files} archivos",
        "",
        "Por tipo de archivo:",
    ]
    
    for ext, size in heapq.nlargest(8, ext_size_map.items(), key=lambda item: item[1]):
        lines.append(f"  {ext:<18} {format_size(size):>10}  ({ext_count_map[ext]} archivos)")
        
    lines.append("")
    lines.append("Archivos más grandes:")
    for size, path in sorted(top_files_heap, key=lambda x: x[0], reverse=True):
        lines.append(f"  {format_size(size):>10}  {path}")
        
    return lines
