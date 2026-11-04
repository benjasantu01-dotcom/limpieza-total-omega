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

# Inode representado como (id_dispositivo, numero_inode) para detectar ciclos
Inode: TypeAlias = Tuple[int, int]
FileRecord: TypeAlias = Tuple[int, Path]


def _safe_stat(path: Union[str, Path, None]) -> Optional[os.stat_result]:
    """
    Intenta obtener metadatos mediante lstat sin seguir enlaces simbólicos.
    
    Args:
        path: Ruta a inspeccionar.
        
    Returns:
        os.stat_result si tiene éxito, None en caso de error o ruta inválida.
    """
    if path is None:
        return None
    try:
        return os.lstat(path)
    except (OSError, PermissionError, ValueError, FileNotFoundError, AttributeError, OverflowError):
        return None


def _is_reparse_point(entry: os.DirEntry) -> bool:
    """
    Verifica si una entrada de directorio es un punto de unión o reanálisis en Windows.
    
    Args:
        entry: Objeto os.DirEntry de la ruta a validar.
        
    Returns:
        True si la ruta es un punto de reanálisis o enlace simbólico.
    """
    try:
        if os.name == 'nt':
            # 0x400 es FILE_ATTRIBUTE_REPARSE_POINT (Puntos de unión/Mount points)
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400)
        return entry.is_symlink()
    except (AttributeError, OSError, TypeError):
        return False


class ExtStats:
    """
    Contenedor de métricas para una extensión de archivo.
    
    Attributes:
        total_bytes: Tamaño acumulado en bytes.
        count: Cantidad total de archivos procesados con esta extensión.
    """
    __slots__ = ('total_bytes', 'count')
    
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0

    def add(self, size: int) -> None:
        """Suma un nuevo archivo a las estadísticas de la extensión."""
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.count += 1


class GlobalStats:
    """
    Acumulador durante el escaneo recursivo para mantener el estado global.
    """
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.total_files: int = 0
        self.ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)

    def register_file(self, size: int, ext: str) -> None:
        """Registra un archivo y actualiza totales y métricas de extensión."""
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.total_files += 1
            self.ext_stats[ext].add(size)


class FolderMetrics:
    """
    Contenedor temporal para el cálculo del peso de un subdirectorio.
    """
    __slots__ = ('size', 'file_count')
    
    def __init__(self) -> None:
        self.size: int = 0
        self.file_count: int = 0

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size)


class SummaryData(NamedTuple):
    """Estructura que consolida los resultados finales de un escaneo."""
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[FileRecord]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """Convierte bytes a Megabytes, normalizando valores negativos o None."""
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    return round(float(size_bytes) / MB_SIZE, 2)


def _validate_limit(limit: Any) -> int:
    """Valida que un límite numérico sea un entero positivo."""
    if isinstance(limit, int) and not isinstance(limit, bool):
        return max(0, limit)
    return 0


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """
    Valida la ruta de inicio para un escaneo.
    
    Verifica que la ruta sea absoluta, no esté bloqueada por `is_protected_path`,
    no sea una ruta UNC (red) y tenga permisos de lectura.
    """
    if not directory:
        return None
    try:
        p = Path(directory).expanduser().resolve(strict=True)
        # Seguridad defensiva: Bloqueo de UNC y protegidos antes de acceder
        if str(p).startswith('\\\\') or is_protected_path(p):
            return None
        if not p.is_dir() or not os.access(p, os.R_OK):
            return None
        return p
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(name: str) -> bool:
    """Detecta nombres de archivo sospechosos o inválidos."""
    if not name or '\0' in name or any(ord(c) < 32 for c in name) or len(name) > 255:
        return True
    return any(c in name for c in SUSPICIOUS_CHARS) or any(char in name for char in ('<', '>', ':', '"', '|', '?', '*'))
            
def _get_local_windows_drives() -> List[str]:
    """Lista las unidades lógicas locales disponibles en Windows."""
    import string
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            if os.path.exists(drive) and not is_protected_path(Path(drive).resolve()):
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
    """Formatea una cantidad de bytes a una cadena legible (ej. '1.5 GB')."""
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
    """Obtiene estadísticas de uso de espacio para una unidad montada."""
    if mount is None: return None
    try:
        p = Path(mount).expanduser().resolve(strict=True)
        if is_protected_path(p) or str(p).startswith('\\\\'):
            return None
        usage = shutil.disk_usage(p)
        return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """Obtiene métricas de uso para todas las unidades detectadas."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Generador recursivo que recorre archivos evitando ciclos de directorios y rutas protegidas.
    """
    root_path = _validate_root(directory)
    if not root_path: return
    
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
                        if _is_excluded_path(entry.name):
                            continue
                        
                        if entry.is_dir(follow_symlinks=False):
                            if _is_reparse_point(entry):
                                continue
                            if skip_protected and is_protected_path(Path(entry.path)):
                                continue
                            
                            st = entry.stat(follow_symlinks=False)
                            inode = (st.st_dev, st.st_ino)
                            if inode not in visited_inodes:
                                visited_inodes.add(inode)
                                stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            if isinstance(st.st_size, int) and st.st_size >= 0:
                                yield Path(entry.path), st.st_size
                    except (OSError, PermissionError, FileNotFoundError):
                        continue
        except (OSError, PermissionError, NotADirectoryError):
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Retorna una lista ordenada con los archivos más pesados en el directorio."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Agrupa el uso de espacio por extensión de archivo."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Calcula las subcarpetas de primer nivel más pesadas en la ruta indicada."""
    root = _validate_root(directory)
    if root is None: return []
    
    stats: Dict[Path, FolderMetrics] = defaultdict(FolderMetrics)
    
    for path, size in walk_files(root, skip_protected):
        try:
            # Optimización: evitar path.parts múltiples veces, usar relpath si es necesario
            rel = path.relative_to(root)
            if not rel.parts: continue
            top_level = root / rel.parts[0]
            m = stats[top_level]
            m.size += size
            m.file_count += 1
        except (ValueError, OSError, RuntimeError):
            continue
            
    results = [FolderUsage(path, m.size, m.file_count) for path, m in stats.items()]
    return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Tuple[int, int]:
    """Retorna una tupla con (tamaño_total_en_bytes, cantidad_total_de_archivos)."""
    root = _validate_root(directory)
    if root is None: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Realiza un escaneo consolidado del directorio y retorna métricas agregadas.
    """
    stats = GlobalStats()
    top_heap: List[FileRecord] = []
    
    for path, size in walk_files(directory, skip_protected):
        ext = path.suffix.lower() or "(sin extensión)"
        stats.register_file(size, ext)
        
        if limit > 0:
            if len(top_heap) < limit:
                heapq.heappush(top_heap, (size, path))
            elif size > top_heap[0][0]:
                heapq.heapreplace(top_heap, (size, path))
                
    return SummaryData(stats.total_bytes, stats.total_files, stats.ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera una representación textual formateada de los resultados del escaneo."""
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
