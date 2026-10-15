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

Inode: TypeAlias = Tuple[int, int]
SizeReport: TypeAlias = Tuple[int, int]
FileRecord: TypeAlias = Tuple[int, Path]


def _safe_stat(path: Union[str, Path]) -> Optional[os.stat_result]:
    """Obtiene metadatos básicos de un archivo o directorio de forma segura."""
    if not path:
        return None
    try:
        return os.stat(path, follow_symlinks=False)
    except (OSError, PermissionError, ValueError):
        return None


class ExtStats:
    """Acumulador para métricas agregadas de archivos por extensión."""
    __slots__ = ('total_bytes', 'count')
    
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0

    def add(self, size: int) -> None:
        """Suma el tamaño de un archivo y aumenta el contador de la extensión."""
        if not isinstance(size, int) or size < 0:
            return
        self.total_bytes += size
        self.count += 1


class GlobalStats:
    """Clase principal de agregación que rastrea totales globales y por extensión."""
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.total_files: int = 0
        self.ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)

    def register_file(self, size: int, suffix: str) -> None:
        """Registra un archivo en las estadísticas globales y específicas de extensión."""
        if not isinstance(size, int) or size < 0:
            return
        self.total_bytes += size
        self.total_files += 1
        ext: str = (suffix.lower() if suffix else "(sin extensión)")
        self.ext_stats[ext].add(size)


class FolderMetrics:
    """Acumulador temporal para el peso total y conteo de archivos en directorios."""
    __slots__ = ('size', 'file_count')
    def __init__(self) -> None:
        self.size: int = 0
        self.file_count: int = 0

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size)


class SummaryData(NamedTuple):
    """Estructura inmutable del resultado del proceso de escaneo."""
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[FileRecord]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """Convierte bytes a MB con redondeo a dos decimales y manejo de errores."""
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    try:
        return round(float(size_bytes) / MB_SIZE, 2)
    except (ZeroDivisionError, OverflowError, ValueError):
        return 0.0


def _validate_limit(limit: Any) -> int:
    """Asegura que el límite de resultados sea un entero no negativo."""
    if isinstance(limit, int) and not isinstance(limit, bool):
        return max(0, limit)
    return 0


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """Valida y resuelve una ruta raíz, confirmando existencia y permisos de lectura."""
    if not directory:
        return None
    try:
        p = Path(directory).expanduser()
        if not p.exists():
            return None
        
        resolved_path = p.resolve()
        if not resolved_path.is_dir() or is_protected_path(resolved_path) or not os.access(resolved_path, os.R_OK):
            return None
        return resolved_path
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(entry: os.DirEntry, root_str: str) -> bool:
    """
    Determina si una entrada de sistema debe excluirse del análisis.
    """
    try:
        if not entry.path: return True
        name = entry.name
        if not name or '\0' in name or any(c in name for c in SUSPICIOUS_CHARS):
            return True
        if any(char in name for char in ('<', '>', ':', '"', '|', '?', '*')):
            return True
        
        if entry.is_symlink():
            return True
        
        # Acceso preventivo para verificar permisos
        if not os.access(entry.path, os.R_OK):
            return True

        if not entry.path.startswith(root_str):
            return True
        
        if is_protected_path(Path(entry.path)):
            return True
            
        return False
    except (OSError, PermissionError, AttributeError, TypeError, RuntimeError):
        return True
            
def _get_local_windows_drives() -> List[str]:
    """Lista las unidades lógicas disponibles en Windows, evitando bloqueos de sistema."""
    import string
    drives: List[str] = []
    try:
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive) and not is_protected_path(Path(drive)):
                drives.append(drive)
    except Exception:
        pass
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
        if self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def is_almost_full(self) -> bool:
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """Formatea bytes a una cadena legible con su unidad correspondiente."""
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
    """Obtiene el estado de uso de una unidad de montaje específica."""
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
    """Retorna una lista de uso de disco para todas las unidades detectadas."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """Generador que recorre recursivamente el sistema de archivos, ignorando rutas protegidas."""
    root_path = _validate_root(directory)
    if root_path is None: return
    root_str = str(root_path)
    
    visited_inodes: set[Inode] = set()
    stack: List[Path] = [root_path]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if skip_protected and _is_excluded_path(entry, root_str):
                            continue
                        
                        if entry.is_dir(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            inode = (st.st_dev, st.st_ino)
                            if inode not in visited_inodes:
                                visited_inodes.add(inode)
                                stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            yield Path(entry.path), st.st_size
                    except (OSError, PermissionError):
                        continue
        except (PermissionError, OSError, FileNotFoundError): 
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    root = _validate_root(directory)
    if root is None: return []
    stats: Dict[str, FolderMetrics] = defaultdict(FolderMetrics)
    root_parts = root.parts
    
    for path, size in walk_files(root, skip_protected):
        try:
            parts = path.parts
            if len(parts) <= len(root_parts): continue
            top_folder = root / parts[len(root_parts)]
            curr = stats[str(top_folder)]
            curr.size += size
            curr.file_count += 1
        except (ValueError, IndexError, OSError, AttributeError):
            continue

    results = [FolderUsage(Path(p), m.size, m.file_count) for p, m in stats.items()]
    return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> SizeReport:
    root = _validate_root(directory)
    if root is None: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Ejecuta el escaneo del directorio y agrupa estadísticas de uso mediante un min-heap.
    """
    stats = GlobalStats()
    top_heap: List[FileRecord] = [] 
    
    for path, size_bytes in walk_files(directory, skip_protected):
        try:
            stats.register_file(size_bytes, path.suffix)
            
            if limit > 0:
                if len(top_heap) < limit: 
                    heapq.heappush(top_heap, (size_bytes, path))
                elif size_bytes > top_heap[0][0]: 
                    heapq.heapreplace(top_heap, (size_bytes, path))
        except (AttributeError, TypeError, ValueError):
            continue
                
    return SummaryData(stats.total_bytes, stats.total_files, stats.ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera un informe textual resumen del análisis de disco realizado."""
    root = _validate_root(directory)
    if root is None: 
        return ["Error: La ruta seleccionada no es válida o no tiene permisos de lectura."]
    
    try:
        data = _collect_summary_data(root, skip_protected, limit=20)
    except Exception:
        return ["Error: Falló el escaneo del directorio raíz."]
        
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
