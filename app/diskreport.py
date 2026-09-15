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


class ExtStats(NamedTuple):
    """Estadísticas acumuladas por extensión de archivo."""
    total_bytes: int
    count: int


class SummaryData(NamedTuple):
    """Estructura de datos interna para consolidar reportes de un solo recorrido."""
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
    """Valida que el directorio sea una ruta absoluta, existente, segura y accesible."""
    if directory is None:
        return None
    try:
        raw_path = Path(directory).resolve()
        
        if not raw_path.exists() or not raw_path.is_dir():
            return None
            
        if is_protected_path(raw_path) or not os.access(raw_path, os.R_OK):
            return None
            
        return raw_path
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(entry: os.DirEntry) -> bool:
    """
    Verifica si una entrada debe ser ignorada. 
    Detecta enlaces simbólicos y puntos de reparse en Windows para evitar 
    recorridos infinitos o fuera de los límites del volumen.
    """
    REPARSE_POINT_ATTR = 0x400
    try:
        if entry.is_symlink():
            return True
        if os.name == 'nt':
            try:
                st = entry.stat(follow_symlinks=False)
                if hasattr(st, 'st_file_attributes'):
                    return bool(st.st_file_attributes & REPARSE_POINT_ATTR)
            except OSError:
                return True
    except (OSError, PermissionError, AttributeError):
        return True
    return False


def _get_local_windows_drives() -> List[str]:
    """Detecta letras de unidad disponibles en entorno Windows."""
    import string
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            if os.path.exists(drive) and not is_protected_path(Path(drive)):
                drives.append(drive)
        except (OSError, PermissionError):
            continue
    return drives


@dataclass(frozen=True)
class FileEntry:
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class ExtensionUsage:
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class FolderUsage:
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class DriveUsage:
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        if not isinstance(self.total, (int, float)) or self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def is_almost_full(self) -> bool:
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """Convierte bytes a una cadena legible con unidades escaladas (B, KB, MB, GB, TB)."""
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
    """Obtiene el estado de uso de una unidad de almacenamiento específica."""
    if mount is None:
        return None
    try:
        p = Path(mount).resolve()
        if p.exists() and not is_protected_path(p):
            usage = shutil.disk_usage(p)
            return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        pass
    return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """Reporta el uso de todas las unidades montadas disponibles."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Generador recursivo que recorre el árbol de archivos.
    Utiliza una estructura de pila (stack) para evitar la recursión profunda y 
    un conjunto de Inodes visitados para detectar ciclos en el sistema de archivos.
    """
    root_path = _validate_root(directory)
    if root_path is None:
        return

    visited_inodes: set[Inode] = set()
    stack: List[Path] = [root_path]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if _is_excluded_path(entry): continue
                        
                        if entry.is_dir(follow_symlinks=False):
                            entry_path = Path(entry.path)
                            if skip_protected and is_protected_path(entry_path): continue
                            try:
                                st = entry.stat(follow_symlinks=False)
                                inode: Inode = (getattr(st, 'st_dev', 0), getattr(st, 'st_ino', 0))
                                if inode[0] != 0 and inode not in visited_inodes:
                                    visited_inodes.add(inode)
                                    stack.append(entry_path)
                            except (OSError, PermissionError): continue
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                st = entry.stat()
                                size = getattr(st, 'st_size', 0)
                                if isinstance(size, (int, float)) and size >= 0:
                                    yield Path(entry.path), int(size)
                            except (OSError, PermissionError):
                                continue
                    except (PermissionError, OSError, AttributeError):
                        continue
        except (PermissionError, OSError):
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Retorna los N archivos más grandes encontrados en el directorio especificado."""
    root = _validate_root(directory)
    if not root: return []
    limit_val = int(limit) if isinstance(limit, (int, float)) and limit > 0 else 20
    data = _collect_summary_data(root, skip_protected, limit=limit_val)
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Calcula el espacio ocupado agrupado por extensión de archivo."""
    root = _validate_root(directory)
    if not root: return []
    limit_val = int(limit) if isinstance(limit, (int, float)) and limit > 0 else 15
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, stats.total_bytes, stats.count) for ext, stats in data.ext_stats.items()]
    return heapq.nlargest(limit_val, usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Identifica las subcarpetas de primer nivel que consumen más espacio."""
    root = _validate_root(directory)
    if not root: return []
    limit_val = int(limit) if isinstance(limit, (int, float)) and limit > 0 else 10
    folder_total_bytes: Dict[Path, int] = defaultdict(int)
    folder_file_counts: Dict[Path, int] = defaultdict(int)
    
    for path, size in walk_files(root, skip_protected):
        try:
            rel = path.relative_to(root)
            if rel.parts:
                top_level_folder = root / rel.parts[0]
                folder_total_bytes[top_level_folder] += max(0, int(size))
                folder_file_counts[top_level_folder] += 1
        except (ValueError, OSError, RuntimeError): continue

    results = [FolderUsage(p, folder_total_bytes[p], folder_file_counts[p]) for p in folder_total_bytes]
    return heapq.nlargest(limit_val, results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> SizeReport:
    """Retorna el peso total en bytes y el conteo de archivos de un directorio."""
    root = _validate_root(directory)
    if not root: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Realiza un recorrido único (single-pass) para recopilar métricas de uso.
    Mantiene heaps internos para procesar los top-N archivos en tiempo real 
    durante el recorrido del árbol.
    """
    total_bytes: int = 0
    total_files: int = 0
    ext_bytes: Dict[str, int] = defaultdict(int)
    ext_counts: Dict[str, int] = defaultdict(int)
    top_heap: List[Tuple[int, Path]] = []
    
    limit_val = int(limit) if isinstance(limit, int) else 0
    
    for path, size in walk_files(directory, skip_protected):
        try:
            current_size = max(0, int(size))
            total_bytes += current_size
            total_files += 1
            
            ext = path.suffix.lower() if path.suffix else "(sin extensión)"
            ext_bytes[ext] += current_size
            ext_counts[ext] += 1
            
            if limit_val > 0:
                if len(top_heap) < limit_val:
                    heapq.heappush(top_heap, (current_size, path))
                elif current_size > top_heap[0][0]:
                    heapq.heapreplace(top_heap, (current_size, path))
        except (ValueError, TypeError, AttributeError):
            continue
                    
    ext_stats = {ext: ExtStats(ext_bytes[ext], ext_counts[ext]) for ext in ext_bytes}
    return SummaryData(total_bytes, total_files, ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera un reporte textual resumido del uso de disco para mostrar al usuario."""
    root = _validate_root(directory)
    if not root: return ["Error: Ruta no válida."]
    data = _collect_summary_data(root, skip_protected, limit=20)
    
    if data.total_files == 0: return ["Aviso: No hay archivos accesibles."]

    lines = [f"Carpeta: {root}", f"Total: {format_size(data.total_bytes)} en {data.total_files} archivos", "", "Por tipo:"]
    sorted_exts = heapq.nlargest(8, data.ext_stats.items(), key=lambda x: x[1].total_bytes)
    for ext, stats in sorted_exts:
        lines.append(f"  {ext:<18} {format_size(stats.total_bytes):>10}  ({stats.count} archivos)")
    lines.extend(["", "Mayores archivos:"])
    lines.extend([f"  {format_size(s):>10}  {p}" for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)])
    return lines
