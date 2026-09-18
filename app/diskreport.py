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
from typing import Generator, Iterable, Dict, List, Tuple, Optional, Union, NamedTuple, TypeAlias

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

MB_SIZE: int = 1024 * 1024
Inode: TypeAlias = Tuple[int, int]
SizeReport: TypeAlias = Tuple[int, int]


class ExtStats:
    """Estadísticas acumuladas por extensión de archivo (mutable para rendimiento)."""
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0


class SummaryData(NamedTuple):
    """
    Estructura de datos interna para consolidar reportes de un solo recorrido.
    - total_bytes: Suma total de bytes procesados.
    - total_files: Cantidad total de archivos procesados.
    - ext_stats: Mapeo de extensión a su objeto ExtStats (bytes y conteo).
    - top_files: Heap que almacena tuplas de (tamaño_bytes, ruta_archivo).
    """
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[Tuple[int, Path]]


def _bytes_to_mb(size_bytes: int | float) -> float:
    """Convierte bytes a Megabytes con precisión de dos decimales."""
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    return round(float(size_bytes) / MB_SIZE, 2)


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """
    Valida la ruta de entrada para operaciones de escaneo.
    Asegura que el directorio exista, sea accesible y no esté en la lista 
    de rutas protegidas por seguridad.
    """
    if directory is None:
        return None
    try:
        raw_path = Path(directory).resolve(strict=True)
        
        if not raw_path.is_dir():
            return None
            
        if is_protected_path(raw_path) or not os.access(raw_path, os.R_OK):
            return None
            
        return raw_path
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(entry: os.DirEntry) -> bool:
    """
    Filtro de exclusión para el escaneo de disco.
    Detecta enlaces simbólicos y puntos de reparse (Windows Junctions) para 
    evitar cruzar volúmenes o entrar en bucles de recursión.
    """
    REPARSE_POINT_ATTR = 0x400
    try:
        if entry.is_symlink():
            return True
        if os.name == 'nt':
            st = entry.stat(follow_symlinks=False)
            if hasattr(st, 'st_file_attributes') and (st.st_file_attributes & REPARSE_POINT_ATTR):
                return True
    except (OSError, PermissionError, AttributeError):
        return True
    return False


def _get_local_windows_drives() -> List[str]:
    """Detecta letras de unidad disponibles en Windows de forma segura."""
    import string
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            p = Path(drive).resolve(strict=False)
            if p.exists() and not is_protected_path(p):
                drives.append(drive)
        except (OSError, PermissionError, RuntimeError):
            continue
    return drives


@dataclass(frozen=True)
class FileEntry:
    """Representación inmutable de un archivo detectado y su tamaño."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño del archivo en Megabytes."""
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class ExtensionUsage:
    """Métrica inmutable de uso total acumulado para una extensión de archivo."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño acumulado de todos los archivos con esta extensión."""
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class FolderUsage:
    """Resumen inmutable de uso de una carpeta específica."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño total de los archivos contenidos en la carpeta en MB."""
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class DriveUsage:
    """Estado de capacidad de una unidad de almacenamiento."""
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        """Calcula el porcentaje de espacio utilizado (0.0 a 100.0)."""
        if not isinstance(self.total, (int, float)) or self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def is_almost_full(self) -> bool:
        """Determina si el espacio libre es inferior al umbral crítico del 10%."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """Convierte bytes a formato legible (KB, MB, GB, TB)."""
    if not isinstance(num, (int, float)) or num < 0:
        return "0 B"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            decimals = 0 if unit == "B" else 1
            return f"{value:.{decimals}f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def drive_usage(mount: Union[str, os.PathLike, None]) -> Optional[DriveUsage]:
    """Obtiene métricas de capacidad para la unidad especificada."""
    if mount is None:
        return None
    try:
        p = Path(mount).resolve(strict=True)
        if not is_protected_path(p):
            usage = shutil.disk_usage(p)
            return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        return None
    return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """Recopila el reporte de uso para todas las unidades detectadas."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Generador de archivos recursivo. Utiliza un stack y validación de inodos 
    para evitar redundancias (hard-links) y ciclos (puntos de reparse).
    """
    root_path = _validate_root(directory)
    if root_path is None: return

    visited_inodes: set[Inode] = set()
    stack: List[Path] = [root_path]
    
    while stack:
        current_dir = stack.pop()
            
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    if _is_excluded_path(entry): continue
                    
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            path = Path(entry.path)
                            if skip_protected and is_protected_path(path): continue
                            
                            st = entry.stat(follow_symlinks=False)
                            inode: Inode = (st.st_dev, st.st_ino)
                            if inode not in visited_inodes:
                                visited_inodes.add(inode)
                                stack.append(path)
                                
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            if st.st_size >= 0:
                                yield Path(entry.path), st.st_size
                            
                    except (PermissionError, OSError, FileNotFoundError):
                        continue
        except (PermissionError, OSError, FileNotFoundError):
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Devuelve los archivos más pesados encontrados en la ruta indicada."""
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=max(0, limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Agrupa el uso de espacio por extensión de archivo."""
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(max(0, limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Calcula el uso de disco de las subcarpetas inmediatas al directorio dado."""
    root = _validate_root(directory)
    if not root: return []
    folder_total_bytes: Dict[Path, int] = defaultdict(int)
    folder_file_counts: Dict[Path, int] = defaultdict(int)
    
    for path, size_bytes in walk_files(root, skip_protected):
        try:
            relative = path.relative_to(root)
            if not relative.parts: continue
            
            top_level_folder = root / relative.parts[0]
            folder_total_bytes[top_level_folder] += size_bytes
            folder_file_counts[top_level_folder] += 1
        except (ValueError, IndexError, OSError): 
            continue

    results = [FolderUsage(p, folder_total_bytes[p], folder_file_counts[p]) for p in folder_total_bytes]
    return heapq.nlargest(max(0, limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> SizeReport:
    """Calcula el tamaño total en bytes y la cantidad total de archivos."""
    root = _validate_root(directory)
    if not root: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Función interna de agregación que recorre el árbol y consolida métricas.
    """
    total_bytes: int = 0
    total_files: int = 0
    ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)
    top_heap: List[Tuple[int, Path]] = []
    
    for path, size_bytes in walk_files(directory, skip_protected):
        try:
            total_bytes += size_bytes
            total_files += 1
            
            ext_raw = path.suffix
            ext = ext_raw.lower() if ext_raw else "(sin extensión)"
            
            stat = ext_stats[ext]
            stat.total_bytes += size_bytes
            stat.count += 1
            
            if limit > 0 and size_bytes > 0:
                if len(top_heap) < limit:
                    heapq.heappush(top_heap, (size_bytes, path))
                elif size_bytes > top_heap[0][0]:
                    heapq.heapreplace(top_heap, (size_bytes, path))
        except Exception:
            continue
    
    return SummaryData(total_bytes, total_files, ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera una representación en texto del reporte de uso de disco."""
    root = _validate_root(directory)
    if not root: return ["Error: Ruta no válida."]
    
    data = _collect_summary_data(root, skip_protected, limit=20)
    
    if data is None or data.total_files == 0: 
        return ["Aviso: No hay archivos accesibles para analizar en la ruta indicada."]

    lines = [f"Carpeta: {root}", f"Total: {format_size(data.total_bytes)} en {data.total_files} archivos", "", "Por tipo:"]
    sorted_exts = heapq.nlargest(8, data.ext_stats.items(), key=lambda x: x[1].total_bytes)
    for ext, stats in sorted_exts:
        lines.append(f"  {ext:<18} {format_size(stats.total_bytes):>10}  ({stats.count} archivos)")
    
    if data.top_files:
        lines.extend(["", "Mayores archivos:"])
        lines.extend([f"  {format_size(s):>10}  {p}" for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)])
    
    return lines
