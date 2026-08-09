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


def _bytes_to_mb(size_bytes: int | float) -> float:
    """Utilidad interna para convertir bytes a Megabytes con precisión de 2 decimales."""
    return round(size_bytes / (1024 * 1024), 2) if isinstance(size_bytes, (int, float)) and size_bytes > 0 else 0.0


@dataclass
class FileEntry:
    """Representa un archivo individual, su ubicación y peso en bytes."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass
class ExtensionUsage:
    """Agregado estadístico del espacio ocupado por una extensión específica."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass
class FolderUsage:
    """Métrica de uso de espacio para un directorio específico."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


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
    Maneja entradas inválidas retornando "0 B".
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
        
        if not p.exists() or not p.is_absolute() or is_protected_path(p):
            return None
        
        if not os.access(p, os.R_OK):
            return None
            
        usage = shutil.disk_usage(p)
        return DriveUsage(mount=str(mount), total=usage.total, used=usage.used, free=usage.free)
    except (OSError, ValueError, TypeError, PermissionError):
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
    Generador iterativo que recorre archivos bajo `directory`.
    Evita seguir enlaces simbólicos y carpetas protegidas para seguridad.
    """
    if not directory:
        return

    try:
        path_input = os.fspath(directory)
        root = Path(path_input).expanduser().resolve()
        if not root.exists() or not root.is_dir() or (skip_protected and is_protected_path(root)):
            return
    except (OSError, RuntimeError, TypeError):
        return

    visited_inodes: set[Tuple[int, int]] = set()
    stack: List[Path] = [root]

    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink():
                            continue
                        
                        path_obj = Path(entry.path).resolve()
                        
                        # Seguridad: validar que la ruta resuelta permanezca bajo el directorio raíz
                        if root not in path_obj.parents and path_obj != root:
                            continue

                        if skip_protected and is_protected_path(path_obj):
                            continue

                        if os.name == 'nt':
                            attrs = entry.stat(follow_symlinks=False).st_file_attributes
                            if (attrs & 0x400) or (attrs & 0x2):
                                continue

                        if entry.is_dir():
                            st = entry.stat()
                            inode = (st.st_dev, st.st_ino)
                            if inode not in visited_inodes:
                                visited_inodes.add(inode)
                                stack.append(path_obj)
                        else:
                            yield path_obj, entry.stat().st_size
                    except (OSError, PermissionError, ValueError, FileNotFoundError):
                        continue
        except (OSError, PermissionError, FileNotFoundError):
            continue


def largest_files(directory: Union[str, os.PathLike], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """
    Retorna los archivos más grandes usando un min-heap para eficiencia O(n log k).
    Ignora archivos inaccesibles durante la iteración.
    """
    if not directory or limit <= 0:
        return []
    return heapq.nlargest(
        limit, 
        (FileEntry(path=p, size_bytes=s) for p, s in walk_files(directory, skip_protected)),
        key=lambda e: e.size_bytes
    )


def usage_by_extension(directory: Union[str, os.PathLike], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """
    Calcula el peso total ocupado por cada extensión de archivo encontrada.
    """
    if not directory or limit <= 0:
        return []
    
    size_map: Dict[str, int] = defaultdict(int)
    count_map: Dict[str, int] = defaultdict(int)
    
    for path, size in walk_files(directory, skip_protected):
        ext = path.suffix.lower() or "(sin extensión)"
        size_map[ext] += size
        count_map[ext] += 1
    
    usage_list: List[ExtensionUsage] = [
        ExtensionUsage(extension=ext, size_bytes=size, count=count_map[ext])
        for ext, size in size_map.items()
    ]
    
    return heapq.nlargest(limit, usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """
    Agrupa recursivamente el peso de carpetas de primer nivel bajo el directorio base.
    """
    if not directory or limit <= 0:
        return []
    
    try:
        base = Path(os.fspath(directory)).expanduser().resolve()
        if not base.exists() or not base.is_dir() or (skip_protected and is_protected_path(base)):
            return []
        
        sums: Dict[Path, int] = defaultdict(int)
        counts: Dict[Path, int] = defaultdict(int)
        
        for path, size in walk_files(base, skip_protected):
            try:
                relative = path.relative_to(base)
                if not relative.parts: continue
                top_level = base / relative.parts[0]
                if skip_protected and is_protected_path(top_level): continue
                sums[top_level] += size
                counts[top_level] += 1
            except (OSError, ValueError, RuntimeError): continue

        results: List[FolderUsage] = [FolderUsage(p, sums[p], counts[p]) for p in sums]
        return heapq.nlargest(limit, results, key=lambda f: f.size_bytes)
    except (OSError, RuntimeError, TypeError):
        return []


def total_size(directory: Union[str, os.PathLike], skip_protected: bool = True) -> Tuple[int, int]:
    """Retorna el tamaño total en bytes y la cantidad total de archivos accesibles."""
    total_bytes, file_count = 0, 0
    for _, size in walk_files(directory, skip_protected):
        total_bytes += size
        file_count += 1
    return total_bytes, file_count


def summarize(directory: Union[str, os.PathLike], skip_protected: bool = True) -> List[str]:
    """
    Genera un informe textual unificado del uso de disco con las estadísticas principales.
    """
    if not directory: return ["Error: Ruta no proporcionada."]
    try:
        path_obj = Path(os.fspath(directory)).expanduser().resolve()
        if not path_obj.exists(): return [f"Error: Ruta no encontrada: {path_obj}"]
    except (OSError, RuntimeError):
        return ["Error: Ruta inválida o inaccesible."]
        
    ext_size: Dict[str, int] = defaultdict(int)
    ext_count: Dict[str, int] = defaultdict(int)
    top_files_heap: List[Tuple[int, str]] = []
    total_bytes, total_files = 0, 0
    
    for path, size in walk_files(path_obj, skip_protected):
        total_bytes += size
        total_files += 1
        
        ext = path.suffix.lower() or "(sin extensión)"
        ext_size[ext] += size
        ext_count[ext] += 1
        
        if len(top_files_heap) >= 8:
            heapq.heappushpop(top_files_heap, (size, str(path)))
        else:
            heapq.heappush(top_files_heap, (size, str(path)))

    lines = [f"Carpeta analizada: {path_obj}", f"Total: {format_size(total_bytes)} en {total_files} archivos", "", "Por tipo de archivo:"]
    
    sorted_exts = heapq.nlargest(8, ext_size.items(), key=lambda item: item[1])
    for ext, size in sorted_exts:
        lines.append(f"  {ext:<18} {format_size(size):>10}  ({ext_count[ext]} archivos)")
        
    lines.extend(["", "Archivos más grandes:"])
    for size, path in sorted(top_files_heap, key=lambda x: x[0], reverse=True):
        lines.append(f"  {format_size(size):>10}  {path}")
        
    return lines
