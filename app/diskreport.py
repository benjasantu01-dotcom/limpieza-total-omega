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
from typing import Generator, Iterable, Dict, List, Tuple, Optional, Union, NamedTuple, TypeAlias, Any, Callable

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

# Identificador único de inodo en sistema de archivos (dispositivo, número de inodo)
Inode: TypeAlias = Tuple[int, int]
# Métricas básicas: (tamaño_total_en_bytes, cantidad_total_de_archivos)
SizeReport: TypeAlias = Tuple[int, int]


class ExtStats:
    """Acumulador de métricas para una extensión específica durante el escaneo."""
    __slots__ = ('total_bytes', 'count')
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0


class SummaryData(NamedTuple):
    """
    Consolidado inmutable de un escaneo.
    Contiene el total global y los diccionarios de agregación necesarios para 
    reportes de extensiones y archivos pesados.
    """
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[Tuple[int, Path]]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """Convierte bytes a megabytes (float) redondeados a 2 decimales; retorna 0.0 si es inválido."""
    try:
        if size_bytes is None or not isinstance(size_bytes, (int, float)) or size_bytes < 0:
            return 0.0
        return round(float(size_bytes) / MB_SIZE, 2)
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def _validate_limit(limit: Any) -> int:
    """Filtra y normaliza el límite de resultados para asegurar un entero >= 0."""
    try:
        if isinstance(limit, int) and not isinstance(limit, bool):
            return max(0, limit)
    except (ValueError, TypeError):
        pass
    return 0


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """Valida que la ruta sea existente, sea un directorio, y sea segura según `safety.py`."""
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


def _is_excluded_path(entry: os.DirEntry, root_path_str: str) -> bool:
    """
    Evalúa si un `os.DirEntry` debe omitirse del análisis (RTL, nulos, symlinks inseguros o protegidos).
    """
    try:
        if any(c in entry.name for c in SUSPICIOUS_CHARS) or '\0' in entry.name:
            return True
        
        real_entry_path = str(Path(entry.path).resolve())
        if not real_entry_path.startswith(root_path_str):
            return True
            
        try:
            # Detectar symlinks y puntos de reparse (reparse points) a nivel de sistema
            st = entry.stat(follow_symlinks=False)
            if (st.st_file_attributes & 0x0400) if os.name == 'nt' else entry.is_symlink():
                return True
        except (OSError, PermissionError):
            return True
            
        return is_protected_path(Path(entry.path))
    except (OSError, PermissionError, AttributeError, RuntimeError, TypeError):
        return True


def _get_local_windows_drives() -> List[str]:
    """Lista letras de unidad (A-Z) disponibles en Windows, filtrando rutas protegidas."""
    import string
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            p = Path(drive)
            if p.exists() and not is_protected_path(p):
                drives.append(drive)
        except (OSError, PermissionError, RuntimeError):
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
        """Determina si la unidad está al límite (menos del 10% de espacio libre)."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """Convierte bytes crudos a una cadena formateada legible (e.g., 1.5 GB)."""
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
    """Devuelve las métricas de uso de disco para un punto de montaje específico."""
    if mount is None:
        return None
    try:
        p = Path(mount).resolve()
        if p.exists() and not is_protected_path(p):
            usage = shutil.disk_usage(p)
            return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        return None
    return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """Obtiene reporte de uso para múltiples unidades o las detectadas por defecto."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """Generador recursivo de archivos (yields Path, size) evitando ciclos mediante inodos."""
    root_path = _validate_root(directory)
    if root_path is None: return
    root_path_str = str(root_path)
    visited_inodes: set[Inode] = set()
    stack: List[str] = [root_path_str]
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if skip_protected and _is_excluded_path(entry, root_path_str):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            inode = (st.st_dev, st.st_ino)
                            if inode not in visited_inodes:
                                visited_inodes.add(inode)
                                stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            if st.st_size >= 0: yield Path(entry.path), st.st_size
                    except (OSError, PermissionError, FileNotFoundError): continue
        except (PermissionError, OSError): continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Identifica los N archivos más pesados en el directorio dado."""
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Calcula estadísticas agregadas por extensión de archivo."""
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Analiza carpetas de primer nivel para determinar su tamaño total acumulado en una sola pasada."""
    root = _validate_root(directory)
    if not root: return []
    
    stats: Dict[Path, List[int]] = defaultdict(lambda: [0, 0])
    try:
        top_folders = {root / e.name: root / e.name for e in os.scandir(root) if e.is_dir()}
    except (OSError, PermissionError): return []
    
    for path, size in walk_files(root, skip_protected):
        try:
            parts = path.relative_to(root).parts
            if parts:
                top_folder = root / parts[0]
                if top_folder in top_folders:
                    stats[top_folder][0] += size
                    stats[top_folder][1] += 1
        except (ValueError, OSError): continue

    results = [FolderUsage(p, s[0], s[1]) for p, s in stats.items()]
    return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> SizeReport:
    """Retorna una tupla (tamaño_total_bytes, conteo_archivos) para la ruta indicada."""
    root = _validate_root(directory)
    if not root: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Recorre el sistema de archivos de forma exhaustiva y consolida métricas globales.
    Usa un heap para mantener los N archivos más grandes durante el escaneo en una sola pasada.
    """
    total_bytes: int = 0
    total_files: int = 0
    ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)
    top_heap: List[Tuple[int, Path]] = []
    
    get_ext: Callable[[Path], str] = lambda p: p.suffix.lower() or "(sin extensión)"
    
    for path, size_bytes in walk_files(directory, skip_protected):
        try:
            total_bytes += size_bytes
            total_files += 1
            stats = ext_stats[get_ext(path)]
            stats.total_bytes += size_bytes
            stats.count += 1
            
            if limit > 0:
                if len(top_heap) < limit: 
                    heapq.heappush(top_heap, (size_bytes, path))
                elif size_bytes > top_heap[0][0]: 
                    heapq.heapreplace(top_heap, (size_bytes, path))
        except (KeyError, TypeError, OSError):
            continue
                
    return SummaryData(total_bytes, total_files, dict(ext_stats), top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera una representación en texto del análisis de disco para visualización."""
    root = _validate_root(directory)
    if root is None: return ["Error: Ruta no válida o inaccesible."]
    data = _collect_summary_data(root, skip_protected, limit=20)
    if data.total_files == 0: return ["Aviso: No hay archivos accesibles."]
    
    lines = [f"Carpeta: {root}", f"Total: {format_size(data.total_bytes)} en {data.total_files} archivos", "", "Por tipo:"]
    for ext, stats in heapq.nlargest(8, data.ext_stats.items(), key=lambda x: x[1].total_bytes):
        lines.append(f"  {ext:<18} {format_size(stats.total_bytes):>10}  ({stats.count} archivos)")
    
    if data.top_files:
        lines.extend(["", "Mayores archivos:"])
        for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True):
            lines.append(f"  {format_size(s):>10}  {str(p)}")
    return lines
