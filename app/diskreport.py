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
    """
    Convierte bytes a Megabytes.
    
    Args:
        size_bytes (int | float): Cantidad de bytes a convertir.
        
    Returns:
        float: Valor en MB con 2 decimales. Retorna 0.0 si el valor no es positivo.
    """
    if isinstance(size_bytes, (int, float)) and size_bytes > 0:
        return round(size_bytes / (1024 * 1024), 2)
    return 0.0


@dataclass
class FileEntry:
    """Representa un archivo individual, su ubicación y peso en bytes."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Peso del archivo en MB."""
        return _bytes_to_mb(self.size_bytes)


@dataclass
class ExtensionUsage:
    """Agregado estadístico del espacio ocupado por una extensión específica."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        """Peso total del grupo en MB."""
        return _bytes_to_mb(self.size_bytes)


@dataclass
class FolderUsage:
    """Métrica de uso de espacio para un directorio específico."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        """Peso total del directorio en MB."""
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
        """True si el espacio libre es inferior al 10% del total."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """
    Formatea bytes a una cadena legible (B, KB, MB, GB, TB).
    
    Args:
        num (Union[int, float, None]): Cantidad de bytes a formatear.
        
    Returns:
        str: Cadena formateada ej: '1.2 GB'. Retorna '0 B' ante errores.
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
    Obtiene métricas de uso de una unidad de disco mediante `shutil.disk_usage`.
    
    Args:
        mount (Union[str, os.PathLike, None]): Ruta de la unidad a analizar.
        
    Returns:
        Optional[DriveUsage]: Objeto DriveUsage o None si la ruta es inaccesible.
    """
    if not mount:
        return None
    try:
        p = Path(mount).expanduser().resolve()
        if str(p).startswith(("\\\\", "//")):
            return None
        if not p.exists() or is_protected_path(p):
            return None
            
        usage = shutil.disk_usage(p)
        return DriveUsage(mount=str(mount), total=usage.total, used=usage.used, free=usage.free)
    except (OSError, ValueError, TypeError, PermissionError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """
    Lista el uso de las unidades detectadas. Autodetecta unidades en Windows si mounts es None.

    Args:
        mounts (Optional[Iterable[str]]): Colección de rutas de montaje.
        
    Returns:
        List[DriveUsage]: Lista de objetos con el estado de uso de cada unidad.
    """
    if mounts is None:
        if os.name == "nt":
            import string
            mounts = [f"{letter}:\\" for letter in string.ascii_uppercase
                      if os.path.exists(f"{letter}:\\")]
        else:
            mounts = ["/"]
    results: List[DriveUsage] = []
    if mounts:
        for mount in mounts:
            if mount:
                usage = drive_usage(mount)
                if usage is not None:
                    results.append(usage)
    return results


def walk_files(directory: Union[str, os.PathLike], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Recorre el sistema de archivos de forma iterativa y segura.

    Args:
        directory (Union[str, os.PathLike]): Ruta base de inicio.
        skip_protected (bool): Si es True, ignora directorios del sistema.

    Yields:
        Tuple[Path, int]: Tupla con la ruta del archivo y su tamaño en bytes.
    """
    if not directory:
        return

    try:
        base_path = Path(directory).expanduser().resolve()
        if str(base_path).startswith(("\\\\", "//")):
            return
        if not base_path.exists() or not base_path.is_dir() or (skip_protected and is_protected_path(base_path)):
            return
    except (OSError, RuntimeError, TypeError, ValueError):
        return

    visited_inodes: set[Tuple[int, int]] = set()
    stack: List[Path] = [base_path]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        # Identificar enlaces y prevenir seguir junctions
                        if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
                            continue

                        if entry.is_dir():
                            st = entry.stat()
                            inode = (st.st_dev, st.st_ino)
                            if inode not in visited_inodes:
                                visited_inodes.add(inode)
                                if skip_protected and is_protected_path(Path(entry.path)):
                                    continue
                                stack.append(Path(entry.path))
                        else:
                            yield Path(entry.path), entry.stat().st_size
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue


def largest_files(directory: Union[str, os.PathLike], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """
    Encuentra los N archivos de mayor tamaño en la jerarquía dada.

    Args:
        directory (Union[str, os.PathLike]): Directorio base.
        limit (int): Cantidad máxima de archivos a retornar.
        skip_protected (bool): Filtro de seguridad para rutas protegidas.

    Returns:
        List[FileEntry]: Lista ordenada de los archivos más grandes.
    """
    if not directory or not isinstance(limit, int) or limit <= 0:
        return []
    try:
        return heapq.nlargest(
            limit, 
            (FileEntry(path=p, size_bytes=s) for p, s in walk_files(directory, skip_protected)),
            key=lambda e: e.size_bytes
        )
    except (OSError, PermissionError):
        return []


def usage_by_extension(directory: Union[str, os.PathLike], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """
    Agrupa y suma el espacio ocupado por cada extensión de archivo.
    
    Args:
        directory (Union[str, os.PathLike]): Directorio base.
        limit (int): Número de extensiones a mostrar.
        skip_protected (bool): Filtro de seguridad.

    Returns:
        List[ExtensionUsage]: Listado estadístico por extensión.
    """
    if not directory or not isinstance(limit, int) or limit <= 0:
        return []
    
    size_map: Dict[str, int] = defaultdict(int)
    count_map: Dict[str, int] = defaultdict(int)
    
    for path, size in walk_files(directory, skip_protected):
        ext = path.suffix.lower() if path.suffix else "(sin extensión)"
        size_map[ext] += size
        count_map[ext] += 1
    
    usage_list: List[ExtensionUsage] = [
        ExtensionUsage(extension=ext, size_bytes=size, count=count_map[ext])
        for ext, size in size_map.items()
    ]
    
    return heapq.nlargest(limit, usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """
    Calcula el peso total de las subcarpetas inmediatas del directorio padre.

    Args:
        directory (Union[str, os.PathLike]): Directorio base.
        limit (int): Número de carpetas a listar.
        skip_protected (bool): Filtro de seguridad.

    Returns:
        List[FolderUsage]: Lista de carpetas pesadas analizadas.
    """
    if not directory or not isinstance(limit, int) or limit <= 0:
        return []
    
    try:
        base = Path(directory).expanduser().resolve()
        if not base.exists() or not base.is_dir() or (skip_protected and is_protected_path(base)):
            return []
        
        sums: Dict[Path, int] = defaultdict(int)
        counts: Dict[Path, int] = defaultdict(int)
        
        for path, size in walk_files(base, skip_protected):
            try:
                rel = path.relative_to(base)
                if not rel.parts:
                    continue
                top_level = base / rel.parts[0]
                sums[top_level] += size
                counts[top_level] += 1
            except (ValueError, IndexError): 
                continue

        results: List[FolderUsage] = [FolderUsage(p, sums[p], counts[p]) for p in sums]
        return heapq.nlargest(limit, results, key=lambda f: f.size_bytes)
    except (OSError, RuntimeError, TypeError, ValueError):
        return []


def total_size(directory: Union[str, os.PathLike], skip_protected: bool = True) -> Tuple[int, int]:
    """
    Calcula espacio total y cantidad de archivos en un directorio.
    
    Returns:
        Tuple[int, int]: (bytes_totales, cantidad_archivos).
    """
    if not directory:
        return (0, 0)
    total_bytes, file_count = 0, 0
    for _, size in walk_files(directory, skip_protected):
        total_bytes += size
        file_count += 1
    return total_bytes, file_count


def summarize(directory: Union[str, os.PathLike], skip_protected: bool = True) -> List[str]:
    """
    Genera un informe textual unificado del uso de disco.
    
    Args:
        directory (Union[str, os.PathLike]): Directorio a analizar.
        skip_protected (bool): Filtro de seguridad.

    Returns:
        List[str]: Líneas del informe listo para imprimir.
    """
    if not directory: 
        return ["Error: Ruta no proporcionada."]
    
    try:
        p_input = Path(directory).expanduser().resolve()
        if str(p_input).startswith(("\\\\", "//")):
            return ["Error: No se permiten rutas de red (UNC)."]
        if not p_input.exists():
            return [f"Error: La ruta no existe: {p_input}"]
        if not p_input.is_dir(): 
            return [f"Error: No es un directorio: {p_input}"]
        if skip_protected and is_protected_path(p_input):
            return [f"Error: Ruta protegida no permitida: {p_input}"]
    except (OSError, TypeError, RuntimeError, ValueError):
        return ["Error: Ruta inválida o inaccesible."]
        
    ext_stats: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    top_files_heap: List[Tuple[int, str]] = []
    total_bytes, total_files = 0, 0
    
    for path, size in walk_files(p_input, skip_protected):
        total_bytes += size
        total_files += 1
        
        ext = path.suffix.lower() if path.suffix else "(sin extensión)"
        stats = ext_stats[ext]
        stats[0] += size
        stats[1] += 1
        
        if len(top_files_heap) < 8:
            heapq.heappush(top_files_heap, (size, str(path)))
        elif size > top_files_heap[0][0]:
            heapq.heapreplace(top_files_heap, (size, str(path)))

    lines = [f"Carpeta analizada: {p_input}", f"Total: {format_size(total_bytes)} en {total_files} archivos", "", "Por tipo de archivo:"]
    
    for ext, stats in heapq.nlargest(8, ext_stats.items(), key=lambda item: item[1][0]):
        lines.append(f"  {ext:<18} {format_size(stats[0]):>10}  ({stats[1]} archivos)")
        
    lines.extend(["", "Archivos más grandes:"])
    for size, path in sorted(top_files_heap, key=lambda x: x[0], reverse=True):
        lines.append(f"  {format_size(size):>10}  {path}")
        
    return lines
