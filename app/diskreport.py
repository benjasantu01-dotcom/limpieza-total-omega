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
from typing import Generator, Iterable, Dict, List, Tuple, Optional, Union, NamedTuple, TypeAlias, Any

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
SUSPICIOUS_CHARS: Tuple[str, ...] = ('\u202E', '\u202D', '\u200E', '\u200F')

# Inode representado como (id_dispositivo, numero_inode) para detectar ciclos de enlaces duros
Inode: TypeAlias = Tuple[int, int]
# Tupla para el min-heap: (tamaño_en_bytes, ruta_completa)
FileRecord: TypeAlias = Tuple[int, Path]


def _safe_stat(path: Union[str, Path, None]) -> Optional[os.stat_result]:
    """
    Intenta obtener los metadatos del sistema de archivos mediante lstat.
    Retorna None si la ruta es inaccesible o no existe.
    """
    if path is None:
        return None
    try:
        return os.lstat(path)
    except (OSError, PermissionError, ValueError, FileNotFoundError, AttributeError):
        return None


def _is_reparse_point(path: Path) -> bool:
    """
    Determina si un directorio es un punto de unión o enlace simbólico.
    """
    try:
        if os.name == 'nt':
            import ctypes
            FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs == -1: return False
            return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
        return path.is_symlink()
    except (AttributeError, OSError, TypeError):
        return False


class ExtStats:
    """
    Contenedor de métricas para un grupo de archivos con la misma extensión.
    """
    __slots__ = ('total_bytes', 'count')
    
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0

    def add(self, size: int) -> None:
        """Incrementa el total de bytes y el contador tras validar tamaño no negativo."""
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.count += 1


class GlobalStats:
    """
    Agregador central de estadísticas durante el escaneo.
    """
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.total_files: int = 0
        self.ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)

    def register_file(self, size: int, ext: str) -> None:
        """Acumula métricas globales y por extensión."""
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.total_files += 1
            self.ext_stats[ext].add(size)


class FolderMetrics:
    """
    Contenedor temporal para calcular el peso total de una carpeta.
    """
    __slots__ = ('size', 'file_count')
    
    def __init__(self) -> None:
        self.size: int = 0
        self.file_count: int = 0

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size)


class SummaryData(NamedTuple):
    """
    Resultados inmutables tras finalizar un proceso de escaneo completo.
    """
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[FileRecord]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """Conversión de bytes a Megabytes con redondeo."""
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    return round(float(size_bytes) / MB_SIZE, 2)


def _validate_limit(limit: Any) -> int:
    """Asegura un entero positivo para límites de resultados."""
    if isinstance(limit, int) and not isinstance(limit, bool):
        return max(0, limit)
    return 0


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """
    Valida y normaliza la ruta raíz para operaciones de escaneo.
    """
    if not directory:
        return None
    try:
        p = Path(directory).expanduser().resolve()
        if not p.exists() or str(p).startswith('\\\\'):
            return None
        if not p.is_dir() or is_protected_path(p) or not os.access(p, os.R_OK):
            return None
        return p
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(name: str, full_path: Path) -> bool:
    """Detecta nombres de archivo con caracteres sospechosos o inválidos."""
    if not name or '\0' in name or any(ord(c) < 32 for c in name) or len(name) > 255:
        return True
    return any(c in name for c in SUSPICIOUS_CHARS) or any(char in name for char in ('<', '>', ':', '"', '|', '?', '*'))
            
def _get_local_windows_drives() -> List[str]:
    """Descubre unidades locales en Windows."""
    import string
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            if os.path.exists(drive) and not is_protected_path(Path(drive)):
                if os.access(drive, os.R_OK):
                    drives.append(drive)
        except (OSError, PermissionError, TypeError):
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
        return round(self.used / self.total * 100, 1) if self.total > 0 else 0.0

    @property
    def is_almost_full(self) -> bool:
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """Formatea bytes a string legible."""
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
    """Obtiene métricas de espacio disponible."""
    if mount is None: return None
    try:
        p = Path(mount).expanduser().resolve()
        if not p.exists() or _is_reparse_point(p) or is_protected_path(p):
            return None
        if str(p).startswith('\\\\'): return None
        usage = shutil.disk_usage(p)
        return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """Retorna métricas de todas las unidades."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """Recorrido recursivo iterativo para evitar desbordamiento de pila."""
    root_path = _validate_root(directory)
    if root_path is None: return
    
    visited_inodes: set[Inode] = set()
    stack: List[str] = [str(root_path)]
    
    root_stat = _safe_stat(root_path)
    if root_stat:
        visited_inodes.add((root_stat.st_dev, root_stat.st_ino))
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if _is_excluded_path(entry.name, Path(entry.path)):
                            continue
                        
                        if entry.is_symlink() or _is_reparse_point(Path(entry.path)):
                            continue
                        
                        st = entry.stat(follow_symlinks=False)
                            
                        if entry.is_dir():
                            if skip_protected and is_protected_path(Path(entry.path)):
                                continue
                            inode = (st.st_dev, st.st_ino)
                            if inode not in visited_inodes:
                                visited_inodes.add(inode)
                                stack.append(entry.path)
                        elif entry.is_file():
                            yield Path(entry.path), st.st_size
                    except (OSError, PermissionError, FileNotFoundError, RuntimeError, AttributeError):
                        continue
        except (OSError, PermissionError, FileNotFoundError, NotADirectoryError):
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Retorna los N archivos más grandes."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Retorna extensiones ordenadas por peso."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Analiza y retorna las subcarpetas más pesadas."""
    root = _validate_root(directory)
    if root is None: return []
    
    stats: Dict[Path, FolderMetrics] = defaultdict(FolderMetrics)
    
    for path, size in walk_files(root, skip_protected):
        try:
            rel = path.relative_to(root)
            if not rel.parts: continue
            top_level = root / rel.parts[0]
            if (skip_protected and is_protected_path(top_level)) or _is_reparse_point(top_level):
                continue
            m = stats[top_level]
            m.size += size
            m.file_count += 1
        except (ValueError, IndexError, OSError, PermissionError):
            continue
            
    results = [FolderUsage(path, m.size, m.file_count) for path, m in stats.items()]
    return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Tuple[int, int]:
    """Retorna (bytes_totales, cantidad_archivos)."""
    root = _validate_root(directory)
    if root is None: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """Escaneo centralizado que acumula estadísticas y mantiene el heap de top archivos."""
    stats = GlobalStats()
    top_heap: List[FileRecord] = []
    
    for path, size in walk_files(directory, skip_protected):
        _, ext = os.path.splitext(path.name)
        ext = ext.lower() if ext else "(sin extensión)"
        stats.register_file(size, ext)
        
        if limit > 0:
            if len(top_heap) < limit:
                heapq.heappush(top_heap, (size, path))
            elif size > top_heap[0][0]:
                heapq.heapreplace(top_heap, (size, path))
                
    return SummaryData(stats.total_bytes, stats.total_files, stats.ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera reporte textual legible."""
    root = _validate_root(directory)
    if root is None: 
        return ["Error: La ruta seleccionada no es válida o no tiene permisos de lectura."]
    
    data = _collect_summary_data(root, skip_protected, limit=20)
        
    if data.total_files == 0: 
        return ["Aviso: No se encontraron archivos accesibles en la ruta seleccionada."]
    
    lines = [f"Carpeta: {root}", f"Total: {format_size(data.total_bytes)} en {data.total_files} archivos", "", "Por tipo:"]
    
    if data.ext_stats:
        for ext, stats in heapq.nlargest(8, data.ext_stats.items(), key=lambda x: x[1].total_bytes):
            lines.append(f"  {ext:<18} {format_size(stats.total_bytes):>10}  ({stats.count} archivos)")
    
    if data.top_files:
        lines.extend(["", "Mayores archivos:"])
        for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True):
            lines.append(f"  {format_size(s):>10}  {str(p)}")
    return lines
