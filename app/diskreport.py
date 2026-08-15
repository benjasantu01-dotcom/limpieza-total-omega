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
    Convierte una medida de bytes a Megabytes.
    
    Args:
        size_bytes: Cantidad numérica de bytes.
        
    Returns:
        Valor resultante en MB redondeado a dos decimales.
    """
    if isinstance(size_bytes, (int, float)) and size_bytes > 0:
        return round(size_bytes / (1024 * 1024), 2)
    return 0.0


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
    Convierte una cantidad de bytes a una cadena legible con sufijo de unidad.
    
    Args:
        num: Cantidad de bytes a formatear.
        
    Returns:
        Cadena formateada ej: '1.2 GB'. Retorna '0 B' en caso de valores nulos o inválidos.
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
    Consulta el estado de almacenamiento de una unidad mediante `shutil.disk_usage`.
    
    Args:
        mount: Ruta de la unidad o punto de montaje.
        
    Returns:
        Objeto DriveUsage si la ruta es válida y accesible, None en caso contrario.
    """
    if not mount:
        return None
    try:
        p = Path(mount).resolve()
        # Rechazar explícitamente rutas UNC para evitar inyección de rutas de red
        if p.parts[0].startswith(("\\\\", "//")):
            return None
        if not p.exists() or is_protected_path(p):
            return None
            
        usage = shutil.disk_usage(p)
        return DriveUsage(mount=str(mount), total=usage.total, used=usage.used, free=usage.free)
    except (OSError, PermissionError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """
    Obtiene el estado de uso de todas las unidades detectadas.
    
    Args:
        mounts: Opcional, lista de rutas a consultar. Si es None, autodetecta unidades (Windows).
        
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
    if mounts:
        for mount in mounts:
            if mount:
                usage = drive_usage(mount)
                if usage is not None:
                    results.append(usage)
    return results


def walk_files(directory: Union[str, os.PathLike], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Recorre recursivamente un directorio (sin seguir enlaces simbólicos) y genera
    tuplas con la ruta de cada archivo y su tamaño en bytes.

    Utiliza `os.scandir` para mejorar el rendimiento de I/O y detecta ciclos de
    directorios mediante la comparación de inodos (números de dispositivo e inodo).

    Args:
        directory: Ruta base donde iniciar el recorrido.
        skip_protected: Si es True, omite directorios marcados por `is_protected_path`.

    Yields:
        Tuplas (path: Path, size: int) para cada archivo encontrado.
    """
    if not directory:
        return

    try:
        base_path = Path(directory).resolve()
        # Rechazar rutas UNC (evita inyección en redes)
        if base_path.parts[0].startswith(("\\\\", "//")):
            return
        if not base_path.exists() or not base_path.is_dir():
            return
        if skip_protected and is_protected_path(base_path):
            return
    except (OSError, RuntimeError, TypeError, ValueError):
        return

    # Estructura de control: (dev, ino) para evitar ciclos (bucle infinito).
    visited_inodes: set[Tuple[int, int]] = set()
    stack: List[Path] = [base_path]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        # Ignorar enlaces simbólicos o junctions para garantizar seguridad
                        if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
                            continue
                            
                        # Validar protección antes de procesar la entrada
                        target = Path(entry.path).resolve()
                        if skip_protected and is_protected_path(target):
                            continue

                        if entry.is_dir():
                            stat_data = entry.stat()
                            inode_key = (stat_data.st_dev, stat_data.st_ino)
                            if inode_key not in visited_inodes:
                                visited_inodes.add(inode_key)
                                stack.append(target)
                        else:
                            yield target, entry.stat().st_size
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue


def largest_files(directory: Union[str, os.PathLike], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """
    Encuentra los N archivos de mayor tamaño en un directorio.

    Args:
        directory: Directorio base de búsqueda.
        limit: Cantidad máxima de archivos a devolver.
        skip_protected: Si es True, ignora rutas de sistema.

    Returns:
        Lista de `FileEntry` ordenados de mayor a menor tamaño.
    """
    if not directory or not isinstance(limit, int) or limit <= 0:
        return []
    try:
        return heapq.nlargest(
            limit, 
            (FileEntry(path=p, size_bytes=s) for p, s in walk_files(directory, skip_protected)),
            key=lambda e: e.size_bytes
        )
    except Exception:
        return []


def usage_by_extension(directory: Union[str, os.PathLike], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """
    Agrupa el uso de espacio por extensión de archivo dentro de un directorio.
    
    Args:
        directory: Directorio base de búsqueda.
        limit: Cantidad de grupos por extensión a retornar.
        skip_protected: Si es True, ignora rutas de sistema.

    Returns:
        Lista de `ExtensionUsage` calculados.
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
    Calcula y lista las subcarpetas inmediatas que ocupan más espacio en un directorio padre.

    Args:
        directory: Ruta del directorio a analizar.
        limit: Cantidad de carpetas a listar.
        skip_protected: Si es True, ignora rutas de sistema.

    Returns:
        Lista de `FolderUsage` de las subcarpetas más pesadas.
    """
    if not directory or not isinstance(limit, int) or limit <= 0:
        return []
    
    try:
        base = Path(directory).resolve()
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
    except Exception:
        return []


def total_size(directory: Union[str, os.PathLike], skip_protected: bool = True) -> Tuple[int, int]:
    """
    Calcula el tamaño total en bytes y la cantidad de archivos de un directorio.
    
    Returns:
        Tupla con formato (bytes_totales, cantidad_archivos).
    """
    if not directory:
        return (0, 0)
    total_bytes, file_count = 0, 0
    for _, size in walk_files(directory, skip_protected):
        total_bytes += size
        file_count += 1
    return total_bytes, file_count


def _collect_summary_data(directory: Path, skip_protected: bool) -> Tuple[int, int, Dict[str, int], Dict[str, int], List[Tuple[int, str]]]:
    """Recolecta métricas crudas para el resumen de disco en una única pasada."""
    total_bytes, total_files = 0, 0
    ext_sizes: Dict[str, int] = defaultdict(int)
    ext_counts: Dict[str, int] = defaultdict(int)
    top_files_heap: List[Tuple[int, str]] = []
    
    for path, size in walk_files(directory, skip_protected):
        try:
            total_bytes += size
            total_files += 1
            
            ext = path.suffix.lower() or "(sin extensión)"
            ext_sizes[ext] += size
            ext_counts[ext] += 1
            
            if len(top_files_heap) < 8:
                heapq.heappush(top_files_heap, (size, str(path)))
            elif size > top_files_heap[0][0]:
                heapq.heapreplace(top_files_heap, (size, str(path)))
        except (AttributeError, TypeError, OSError):
            continue
            
    return total_bytes, total_files, ext_sizes, ext_counts, top_files_heap


def summarize(directory: Union[str, os.PathLike], skip_protected: bool = True) -> List[str]:
    """
    Genera un informe textual resumen del análisis de uso de disco.
    
    Args:
        directory: Directorio a analizar.
        skip_protected: Si es True, ignora rutas de sistema.

    Returns:
        Lista de cadenas con el reporte formateado línea por línea.
    """
    if not directory: 
        return ["Error: Ruta no proporcionada."]
    
    try:
        p_input = Path(directory).resolve()
        # Rechazar rutas UNC
        if p_input.parts[0].startswith(("\\\\", "//")):
            return ["Error: No se permiten rutas de red (UNC)."]
        if not p_input.exists():
            return [f"Error: La ruta no existe: {p_input}"]
        if not p_input.is_dir(): 
            return [f"Error: No es un directorio: {p_input}"]
        if skip_protected and is_protected_path(p_input):
            return [f"Error: Ruta protegida no permitida: {p_input}"]
            
        total_bytes, total_files, ext_sizes, ext_counts, top_files_heap = _collect_summary_data(p_input, skip_protected)
    except (OSError, RuntimeError, PermissionError):
        return ["Error: Acceso denegado o error durante el análisis del disco."]
    except Exception:
        return ["Error: Fallo inesperado durante el análisis del disco."]

    lines = [f"Carpeta analizada: {p_input}", f"Total: {format_size(total_bytes)} en {total_files} archivos", "", "Por tipo de archivo:"]
    
    for ext, size in heapq.nlargest(8, ext_sizes.items(), key=lambda item: item[1]):
        lines.append(f"  {ext:<18} {format_size(size):>10}  ({ext_counts[ext]} archivos)")
        
    lines.extend(["", "Archivos más grandes:"])
    for size, path in sorted(top_files_heap, key=lambda x: x[0], reverse=True):
        lines.append(f"  {format_size(size):>10}  {path}")
        
    return lines
