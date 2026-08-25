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
`safety.is_protected_path`, así un análisis de "tecla unidad C:" no se
mete en Windows ni en Program Files.
"""

from __future__ import annotations
import os
import shutil
import heapq
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Iterable, Dict, List, Tuple, Optional, Union, NamedTuple

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


class SummaryData(NamedTuple):
    """Contenedor de resultados para el análisis unificado de directorios."""
    total_bytes: int
    total_files: int
    ext_sizes: Dict[str, int]
    ext_counts: Dict[str, int]
    top_files: List[Tuple[int, Path]]


def _bytes_to_mb(size_bytes: int | float) -> float:
    """
    Convierte una medida de bytes a Megabytes (MB).
    
    Args:
        size_bytes: Cantidad de bytes a convertir.
        
    Returns:
        float: Valor en MB redondeado a dos decimales. Retorna 0.0 para entradas inválidas.
    """
    if not isinstance(size_bytes, (int, float)):
        return 0.0
    val = float(size_bytes)
    if val <= 0:
        return 0.0
    return round(val / (1024 * 1024), 2)


@dataclass
class FileEntry:
    """
    Registro de un archivo detectado durante el escaneo.
    
    Attributes:
        path: Objeto Path con la ubicación absoluta del archivo.
        size_bytes: Tamaño del archivo en bytes (del sistema de archivos).
    """
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """float: Retorna el peso del archivo convertido a MB."""
        return _bytes_to_mb(self.size_bytes)


@dataclass
class ExtensionUsage:
    """
    Agrupación estadística de archivos por extensión.
    
    Attributes:
        extension: La extensión encontrada (ej: '.png') o '(sin extensión)'.
        size_bytes: Sumatoria del tamaño de todos los archivos con esta extensión.
        count: Cantidad total de archivos procesados para este grupo.
    """
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        """float: Retorna el peso total del grupo de archivos convertido a MB."""
        return _bytes_to_mb(self.size_bytes)


@dataclass
class FolderUsage:
    """
    Métrica de uso de espacio para un directorio específico (ej. subcarpetas directas).
    
    Attributes:
        path: Ruta del directorio analizado.
        size_bytes: Tamaño acumulado de todos los archivos en el árbol del directorio.
        file_count: Cantidad de archivos contenidos en este directorio y sus subcarpetas.
    """
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        """float: Retorna el peso total del directorio convertido a MB."""
        return _bytes_to_mb(self.size_bytes)


@dataclass
class DriveUsage:
    """
    Estado de almacenamiento de una unidad lógica montada.
    
    Attributes:
        mount: Cadena con la letra de unidad o punto de montaje.
        total: Capacidad total en bytes.
        used: Espacio ocupado en bytes.
        free: Espacio libre disponible en bytes.
    """
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        """float: Calcula el porcentaje de ocupación de la unidad (0.0 a 100.0)."""
        if self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def is_almost_full(self) -> bool:
        """bool: Retorna True si el espacio libre es menor al 10% de la capacidad total."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """
    Convierte una cantidad de bytes a una cadena legible (ej: '1.2 GB').
    
    Args:
        num: Valor numérico en bytes.
        
    Returns:
        str: String formateado con la unidad correspondiente. '0 B' si la entrada es inválida.
    """
    if num is None or not isinstance(num, (int, float)):
        return "0 B"
    
    value = float(num)
    if value < 0:
        return "0 B"
        
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            decimals = 0 if unit == "B" else 1
            return f"{value:.{decimals}f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def drive_usage(mount: Union[str, os.PathLike, None]) -> Optional[DriveUsage]:
    """
    Consulta el estado de almacenamiento de una unidad específica.
    
    Args:
        mount: Ruta de la unidad o directorio a consultar.
        
    Returns:
        Optional[DriveUsage]: Estado de la unidad, o None si la ruta es inaccesible 
        o bloqueada por motivos de seguridad.
    """
    if not mount or not isinstance(mount, (str, os.PathLike)):
        return None
        
    try:
        p = Path(os.fspath(mount)).resolve(strict=False)
        if is_protected_path(p) or not os.access(p, os.R_OK):
            return None
            
        str_mount = str(p)
        if str_mount.startswith(("\\\\", "//")):
            return None
            
        usage = shutil.disk_usage(p)
        if usage.total == 0:
            return None
            
        return DriveUsage(mount=str_mount, total=usage.total, used=usage.used, free=usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """
    Obtiene el uso de almacenamiento de todas las unidades detectadas.
    
    Args:
        mounts: Iterable opcional con rutas a consultar. Si es None, autodetecta unidades.
        
    Returns:
        List[DriveUsage]: Lista de estados por cada unidad detectada con éxito.
    """
    if mounts is None:
        if os.name == "nt":
            import string
            mounts = [f"{letter}:\\" for letter in string.ascii_uppercase
                      if os.path.exists(f"{letter}:\\")]
        else:
            mounts = ["/"]
    
    if not isinstance(mounts, Iterable):
        return []
    
    results: List[DriveUsage] = []
    for mount in mounts:
        if isinstance(mount, str) and mount:
            usage = drive_usage(mount)
            if usage:
                results.append(usage)
    return results


def _is_valid_traversal_entry(entry: os.DirEntry, skip_protected: bool) -> bool:
    """
    Verifica si una entrada del sistema de archivos debe ser ignorada por seguridad o tipo.
    """
    if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
        return False
    
    if skip_protected and is_protected_path(Path(entry.path)):
        return False
        
    return True


def walk_files(directory: Union[str, os.PathLike], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Generador iterativo que recorre directorios recursivamente mediante `os.scandir`.
    """
    if not directory:
        return

    try:
        base_path = Path(os.path.realpath(str(directory)))
        if not base_path.is_dir() or (skip_protected and is_protected_path(base_path)):
            return
    except (OSError, RuntimeError, TypeError, ValueError):
        return

    visited_inodes: set[Tuple[int, int]] = set()
    stack: List[str] = [str(base_path)]
    MAX_DEPTH = 100
    depths: Dict[str, int] = {str(base_path): 0}
    
    while stack:
        current_dir = stack.pop()
        depth = depths.get(current_dir, 0)
        
        if depth > MAX_DEPTH:
            continue
            
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if not _is_valid_traversal_entry(entry, skip_protected):
                            continue
                        
                        if entry.is_dir(follow_symlinks=False):
                            stat_data = entry.stat(follow_symlinks=False)
                            inode_key = (stat_data.st_dev, stat_data.st_ino)
                            if inode_key not in visited_inodes:
                                visited_inodes.add(inode_key)
                                path_str = entry.path
                                stack.append(path_str)
                                depths[path_str] = depth + 1
                        elif entry.is_file(follow_symlinks=False):
                            f_stat = entry.stat(follow_symlinks=False)
                            yield Path(entry.path), max(0, f_stat.st_size)
                    except (PermissionError, FileNotFoundError, OSError):
                        continue
        except (PermissionError, FileNotFoundError, OSError):
            continue


def largest_files(directory: Union[str, os.PathLike], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """
    Identifica los N archivos más grandes en un directorio.
    
    Args:
        directory: Ruta de análisis.
        limit: Cantidad de archivos a retornar.
        skip_protected: Flag de seguridad para ignorar rutas protegidas.
        
    Returns:
        List[FileEntry]: Lista de archivos ordenados de mayor a menor tamaño.
    """
    if not directory or not isinstance(limit, int) or limit <= 0:
        return []
    
    items = ((s, p) for p, s in walk_files(directory, skip_protected))
    return [FileEntry(path=p, size_bytes=s) for s, p in heapq.nlargest(limit, items, key=lambda x: x[0])]


def usage_by_extension(directory: Union[str, os.PathLike], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """
    Agrupa el uso de espacio total por extensión de archivo.
    
    Returns:
        List[ExtensionUsage]: Lista de extensiones ordenadas por uso total de bytes.
    """
    if not directory or not isinstance(limit, int) or limit <= 0:
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
    Identifica las subcarpetas de primer nivel que ocupan más espacio.
    
    Returns:
        List[FolderUsage]: Lista de carpetas de primer nivel ordenadas por peso.
    """
    if not directory or not isinstance(limit, int) or limit <= 0:
        return []
    
    try:
        p_base = Path(os.path.realpath(str(directory)))
        if not p_base.is_dir() or (skip_protected and is_protected_path(p_base)):
            return []
            
        sums: Dict[str, int] = defaultdict(int)
        counts: Dict[str, int] = defaultdict(int)
        
        for path, size in walk_files(p_base, skip_protected):
            try:
                relative = path.relative_to(p_base)
                if not relative.parts:
                    continue
                top_folder = p_base / relative.parts[0]
                str_path = str(top_folder)
                sums[str_path] += size
                counts[str_path] += 1
            except (ValueError, IndexError, AttributeError):
                continue

        results: List[FolderUsage] = [FolderUsage(Path(p), sums[p], counts[p]) for p in sums]
        return heapq.nlargest(limit, results, key=lambda f: f.size_bytes)
    except (OSError, RuntimeError, TypeError, ValueError):
        return []


def total_size(directory: Union[str, os.PathLike], skip_protected: bool = True) -> Tuple[int, int]:
    """
    Calcula el tamaño total en bytes y el conteo de archivos en un directorio.
    
    Returns:
        Tuple[int, int]: (Total en bytes, Cantidad de archivos).
    """
    total_bytes, file_count = 0, 0
    if not directory:
        return 0, 0
    for _, size in walk_files(directory, skip_protected):
        total_bytes += size
        file_count += 1
    return total_bytes, file_count


def _collect_summary_data(directory: Path, skip_protected: bool) -> SummaryData:
    """
    Recolección interna de métricas para optimizar múltiples lecturas de disco.
    """
    total_bytes, total_files = 0, 0
    ext_sizes: Dict[str, int] = defaultdict(int)
    ext_counts: Dict[str, int] = defaultdict(int)
    
    # Heap para mantener los 8 archivos más grandes durante la iteración
    top_files_heap: List[Tuple[int, Path]] = []
    
    for path, size in walk_files(directory, skip_protected):
        total_bytes += size
        total_files += 1
        ext = path.suffix.lower() or "(sin extensión)"
        ext_sizes[ext] += size
        ext_counts[ext] += 1
        
        # Mantenimiento del heap para el top-8
        if len(top_files_heap) < 8:
            heapq.heappush(top_files_heap, (size, path))
        elif size > top_files_heap[0][0]:
            heapq.heapreplace(top_files_heap, (size, path))
            
    sorted_top_files = sorted(top_files_heap, key=lambda x: x[0], reverse=True)
            
    return SummaryData(total_bytes, total_files, ext_sizes, ext_counts, sorted_top_files)


def summarize(directory: Union[str, os.PathLike], skip_protected: bool = True) -> List[str]:
    """
    Genera un informe textual unificado con los hallazgos del análisis.
    
    Returns:
        List[str]: Líneas del reporte formateado.
    """
    if not directory:
        return ["Error: Ruta no proporcionada."]

    try:
        p_input = Path(os.path.realpath(str(directory)))
        if not p_input.exists():
            return [f"Error: Ruta no existente: {p_input}"]
        if not p_input.is_dir():
            return [f"Error: Ruta no es un directorio: {p_input}"]
        
        if skip_protected and is_protected_path(p_input):
            return [f"Error: Ruta protegida no permitida: {p_input}"]
            
        data = _collect_summary_data(p_input, skip_protected)
    except (OSError, PermissionError, RuntimeError, TypeError, ValueError) as e:
        return [f"Error: No se pudo procesar la ruta: {str(e)}"]

    lines = [
        f"Carpeta analizada: {p_input}", 
        f"Total: {format_size(data.total_bytes)} en {data.total_files} archivos", 
        "", 
        "Por tipo de archivo:"
    ]
    sorted_exts = heapq.nlargest(8, data.ext_sizes.items(), key=lambda item: item[1])
    for ext, size in sorted_exts:
        lines.append(f"  {ext:<18} {format_size(size):>10}  ({data.ext_counts[ext]} archivos)")
        
    lines.extend(["", "Archivos más grandes:"])
    for size, path in data.top_files:
        lines.append(f"  {format_size(size):>10}  {path}")
    return lines
