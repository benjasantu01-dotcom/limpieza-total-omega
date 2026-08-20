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
    """
    Convierte una medida de bytes a Megabytes (MB).
    
    Args:
        size_bytes: Cantidad de bytes a convertir.
        
    Returns:
        Valor en MB redondeado a dos decimales. Retorna 0.0 para entradas inválidas.
    """
    if not isinstance(size_bytes, (int, float)):
        return 0.0
    val = float(size_bytes)
    if val <= 0:
        return 0.0
    return round(val / (1024 * 1024), 2)


@dataclass
class FileEntry:
    """Representa un archivo individual y su peso en bytes."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Retorna el peso del archivo convertido a MB."""
        return _bytes_to_mb(self.size_bytes)


@dataclass
class ExtensionUsage:
    """Estadística agregada para archivos de una misma extensión."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        """Retorna el peso total del grupo de archivos convertido a MB."""
        return _bytes_to_mb(self.size_bytes)


@dataclass
class FolderUsage:
    """Métrica de uso de espacio para un directorio específico."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        """Retorna el peso total del directorio convertido a MB."""
        return _bytes_to_mb(self.size_bytes)


@dataclass
class DriveUsage:
    """Estado de almacenamiento de una unidad lógica montada."""
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        """Calcula el porcentaje de ocupación de la unidad (0.0 a 100.0)."""
        if self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def is_almost_full(self) -> bool:
        """Retorna True si el espacio libre es menor al 10% de la capacidad total."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """
    Convierte una cantidad de bytes a una cadena legible (ej: '1.2 GB').
    
    Args:
        num: Valor numérico en bytes.
        
    Returns:
        String formateado con la unidad correspondiente. '0 B' si la entrada es inválida.
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
        mount: Ruta o letra de la unidad a analizar.
        
    Returns:
        Instancia de DriveUsage con estadísticas o None si la ruta es inaccesible.
    """
    if not mount:
        return None
    try:
        # Bloquear rutas UNC preventivamente
        str_mount = str(mount)
        if str_mount.startswith(("\\\\", "//")):
            return None
            
        p = Path(mount).resolve(strict=False)
        if not p.exists():
            return None
            
        usage = shutil.disk_usage(p)
        return DriveUsage(mount=str(mount), total=usage.total, used=usage.used, free=usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, FileNotFoundError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """
    Obtiene el uso de almacenamiento de todas las unidades detectadas en el sistema.
    
    Args:
        mounts: Opcional, lista de rutas de unidades a analizar. Si es None, detecta automáticas.
        
    Returns:
        Lista de objetos DriveUsage con la información de cada unidad.
    """
    if mounts is None:
        if os.name == "nt":
            import string
            mounts = [f"{letter}:\\" for letter in string.ascii_uppercase
                      if os.path.exists(f"{letter}:\\")]
        else:
            mounts = ["/"]
    
    results: List[DriveUsage] = []
    if mounts and isinstance(mounts, Iterable):
        for mount in mounts:
            if mount and isinstance(mount, (str, os.PathLike)):
                usage = drive_usage(mount)
                if usage:
                    results.append(usage)
    return results


def walk_files(directory: Union[str, os.PathLike], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Generador que recorre recursivamente el sistema de archivos de forma iterativa.
    
    Args:
        directory: Directorio base de inicio.
        skip_protected: Si es True, filtra rutas según `is_protected_path`.
        
    Yields:
        Tupla conteniendo el objeto Path y el tamaño en bytes del archivo.
    """
    if not directory:
        return

    try:
        p_path = Path(directory)
        base_path = p_path.resolve(strict=False)
        if not base_path.exists() or not base_path.is_dir():
            return
        if skip_protected and is_protected_path(base_path):
            return
    except (OSError, RuntimeError, TypeError, ValueError):
        return

    visited_inodes: set[Tuple[int, int]] = set()
    stack: List[str] = [str(base_path)]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        # Saltar symlinks/junctions para evitar bucles infinitos
                        if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
                            continue
                        
                        entry_path = Path(entry.path)
                        if skip_protected and is_protected_path(entry_path):
                            continue
                        
                        # stat puede fallar si el archivo fue borrado o denegado acceso
                        stat_data = entry.stat(follow_symlinks=False)
                        
                        if entry.is_dir():
                            inode_key = (stat_data.st_dev, stat_data.st_ino)
                            if inode_key not in visited_inodes:
                                visited_inodes.add(inode_key)
                                stack.append(entry.path)
                        elif entry.is_file():
                            yield entry_path, stat_data.st_size
                    except (OSError, PermissionError, FileNotFoundError):
                        continue
        except (OSError, PermissionError, FileNotFoundError):
            continue


def largest_files(directory: Union[str, os.PathLike], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """
    Identifica los archivos más grandes en un directorio utilizando un heap.
    
    Args:
        directory: Ruta base de búsqueda.
        limit: Cantidad máxima de archivos a retornar.
        skip_protected: Si se deben ignorar rutas protegidas.
        
    Returns:
        Lista de objetos FileEntry ordenados de mayor a menor peso.
    """
    if not directory or not isinstance(limit, int) or limit <= 0:
        return []
    
    return [FileEntry(path=p, size_bytes=s) for s, p in heapq.nlargest(
        limit, 
        ((s, p) for p, s in walk_files(directory, skip_protected)),
        key=lambda x: x[0]
    )]


def usage_by_extension(directory: Union[str, os.PathLike], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """
    Agrupa el uso de espacio total por extensión de archivo.
    
    Args:
        directory: Directorio base de búsqueda.
        limit: Límite de extensiones a mostrar.
        skip_protected: Si se deben ignorar rutas protegidas.
        
    Returns:
        Lista de objetos ExtensionUsage con el detalle de uso.
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
    
    Args:
        directory: Ruta base del directorio a analizar.
        limit: Cantidad máxima de carpetas a retornar.
        skip_protected: Si se deben ignorar rutas protegidas.
        
    Returns:
        Lista de objetos FolderUsage con el resumen de peso y conteo de archivos.
    """
    if not directory or not isinstance(limit, int) or limit <= 0:
        return []
    
    try:
        base = Path(directory).resolve(strict=False)
        sums: Dict[Path, int] = defaultdict(int)
        counts: Dict[Path, int] = defaultdict(int)
        
        for path, size in walk_files(base, skip_protected):
            try:
                # Validar que path pertenezca a base antes de invocar relative_to
                if base in path.parents or path == base:
                    relative = path.relative_to(base)
                    if len(relative.parts) > 0:
                        top_folder = base / relative.parts[0]
                        sums[top_folder] += size
                        counts[top_folder] += 1
            except (ValueError, IndexError, RuntimeError): 
                continue

        results: List[FolderUsage] = [FolderUsage(p, sums[p], counts[p]) for p in sums]
        return heapq.nlargest(limit, results, key=lambda f: f.size_bytes)
    except (OSError, RuntimeError, TypeError, ValueError):
        return []


def total_size(directory: Union[str, os.PathLike], skip_protected: bool = True) -> Tuple[int, int]:
    """
    Calcula el tamaño total en bytes y el conteo de archivos en un directorio.
    
    Args:
        directory: Ruta del directorio.
        skip_protected: Si se deben ignorar rutas protegidas.
        
    Returns:
        Tupla (total_bytes, total_archivos).
    """
    total_bytes, file_count = 0, 0
    for _, size in walk_files(directory, skip_protected):
        total_bytes += size
        file_count += 1
    return total_bytes, file_count


def _collect_summary_data(directory: Path, skip_protected: bool) -> Tuple[int, int, Dict[str, int], Dict[str, int], List[Tuple[int, Path]]]:
    """
    Realiza una pasada única para recolectar todas las métricas necesarias para el resumen.
    
    Args:
        directory: Path base del directorio a analizar.
        skip_protected: Booleano para filtrar rutas protegidas.
        
    Returns:
        Tupla con: bytes totales, conteo total, diccionario de tamaños por ext, 
        diccionario de conteos por ext, y heap con los 8 archivos más grandes.
    """
    total_bytes, total_files = 0, 0
    ext_sizes: Dict[str, int] = defaultdict(int)
    ext_counts: Dict[str, int] = defaultdict(int)
    top_files_heap: List[Tuple[int, Path]] = []
    
    for path, size in walk_files(directory, skip_protected):
        total_bytes += size
        total_files += 1
        
        ext = path.suffix.lower() or "(sin extensión)"
        ext_sizes[ext] += size
        ext_counts[ext] += 1
        
        if len(top_files_heap) < 8:
            heapq.heappush(top_files_heap, (size, path))
        elif size > top_files_heap[0][0]:
            heapq.heapreplace(top_files_heap, (size, path))
            
    return total_bytes, total_files, ext_sizes, ext_counts, top_files_heap


def summarize(directory: Union[str, os.PathLike], skip_protected: bool = True) -> List[str]:
    """
    Genera un informe textual unificado con los hallazgos del análisis de disco.
    
    Args:
        directory: Directorio a analizar.
        skip_protected: Si se deben ignorar rutas protegidas.
        
    Returns:
        Lista de strings conteniendo el informe formateado para visualización.
    """
    if not directory: 
        return ["Error: Ruta no proporcionada."]
    
    try:
        p_input = Path(directory).resolve(strict=False)
        if not p_input.exists():
            return [f"Error: Ruta no existente: {p_input}"]
        if not p_input.is_dir():
            return [f"Error: Ruta no es un directorio: {p_input}"]
        if skip_protected and is_protected_path(p_input):
            return [f"Error: Ruta protegida no permitida: {p_input}"]
            
        total_bytes, total_files, ext_sizes, ext_counts, top_files_heap = _collect_summary_data(p_input, skip_protected)
    except (OSError, PermissionError, ValueError, TypeError, RuntimeError):
        return ["Error: Acceso denegado o error durante el análisis del disco."]

    lines = [f"Carpeta analizada: {p_input}", f"Total: {format_size(total_bytes)} en {total_files} archivos", "", "Por tipo de archivo:"]
    sorted_exts = heapq.nlargest(8, ext_sizes.items(), key=lambda item: item[1])
    for ext, size in sorted_exts:
        lines.append(f"  {ext:<18} {format_size(size):>10}  ({ext_counts[ext]} archivos)")
        
    lines.extend(["", "Archivos más grandes:"])
    for size, path in sorted(top_files_heap, key=lambda x: x[0], reverse=True):
        lines.append(f"  {format_size(size):>10}  {path}")
    return lines
