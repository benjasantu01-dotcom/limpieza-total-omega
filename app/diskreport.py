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

# Identificador único de inodo en sistema de archivos: (ID de dispositivo, Número de inodo)
Inode: TypeAlias = Tuple[int, int]
# Métricas básicas: (tamaño_total_en_bytes, cantidad_total_de_archivos)
SizeReport: TypeAlias = Tuple[int, int]


def _safe_stat(path: Union[str, Path]) -> Optional[os.stat_result]:
    """
    Intenta obtener metadatos sin seguir enlaces simbólicos (evita bucles y acceso externo).
    Retorna None si la ruta es inaccesible o si fallan los permisos.
    """
    if not path:
        return None
    try:
        return os.stat(path, follow_symlinks=False)
    except (OSError, PermissionError, FileNotFoundError, ValueError):
        return None


class ExtStats:
    """Acumulador de estadísticas para una extensión de archivo específica."""
    __slots__ = ('total_bytes', 'count')
    
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0

    def add(self, size: int) -> None:
        """Suma el peso de un nuevo archivo y actualiza el contador de ocurrencias."""
        self.total_bytes += size
        self.count += 1


class GlobalStats:
    """
    Gestiona el estado consolidado de un escaneo de disco completo.
    Mantiene contadores globales y desgloses por extensión.
    """
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.total_files: int = 0
        self.ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)

    def register_file(self, size: int, path: Path) -> None:
        """Registra métricas de un archivo individual en los totales del escaneo."""
        self.total_bytes += size
        self.total_files += 1
        ext = path.suffix.lower() or "(sin extensión)"
        self.ext_stats[ext].add(size)


class FolderMetrics:
    """
    Contenedor de datos para el conteo de espacio utilizado por carpetas.
    Utilizado internamente para agregaciones durante el cálculo de `largest_folders`.
    """
    __slots__ = ('size', 'file_count')
    def __init__(self) -> None:
        self.size: int = 0
        self.file_count: int = 0


class SummaryData(NamedTuple):
    """
    Estructura inmutable que transporta los resultados procesados de un escaneo.
    """
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[Tuple[int, Path]]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """
    Convierte bytes a Megabytes (float) con precisión de dos decimales.
    Retorna 0.0 ante entradas inválidas o negativas.
    """
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    try:
        return round(float(size_bytes) / MB_SIZE, 2)
    except (ZeroDivisionError, OverflowError, ValueError):
        return 0.0


def _validate_limit(limit: Any) -> int:
    """
    Normaliza el límite de resultados para funciones de ranking. 
    Asegura un valor entero no negativo, ignorando tipos no numéricos.
    """
    if isinstance(limit, int) and not isinstance(limit, bool):
        return max(0, limit)
    return 0


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """
    Valida y resuelve una ruta de inicio.
    Verifica existencia, accesibilidad y si es una ruta protegida.
    """
    if directory is None:
        return None
    try:
        p = Path(directory).expanduser().resolve(strict=True)
        if not p.is_dir() or is_protected_path(p) or not os.access(p, os.R_OK):
            return None
        return p
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(entry: os.DirEntry) -> bool:
    """
    Determina si una entrada del sistema de archivos debe ser excluida del escaneo.
    Aplica filtros de seguridad contra caracteres maliciosos y rutas de sistema.
    """
    try:
        if not entry.name or '\0' in entry.name or any(c in entry.name for c in SUSPICIOUS_CHARS):
            return True
        
        if entry.is_symlink():
            return True
            
        if is_protected_path(Path(entry.path)):
            return True

        if os.name == 'nt':
            try:
                st = entry.stat(follow_symlinks=False)
                if hasattr(st, 'st_file_attributes') and (st.st_file_attributes & 0x0400):
                     return True
            except OSError:
                return True
            
        return False
    except (OSError, PermissionError, AttributeError, RuntimeError, TypeError):
        return True
            
def _get_local_windows_drives() -> List[str]:
    """Retorna una lista de rutas de unidades físicas montadas en Windows."""
    import string
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if os.path.exists(drive) and not is_protected_path(Path(drive)):
            drives.append(drive)
    return drives


@dataclass(frozen=True)
class FileEntry:
    """Representación de un archivo individual para reportes externos."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class ExtensionUsage:
    """Representación de uso de disco agregado por extensión de archivo."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class FolderUsage:
    """Representación de uso de disco de una carpeta particular."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class DriveUsage:
    """Estado del espacio disponible en una unidad de disco."""
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
    """Convierte un tamaño en bytes a su representación humana legible."""
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
    """Obtiene estadísticas de espacio de un punto de montaje o unidad."""
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
    """Retorna una lista de estados de todas las unidades o los montajes indicados."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Recorre el sistema de archivos de manera iterativa (evitando recursión profunda).
    Utiliza un stack para DFS y inodos para detectar bucles simbólicos.
    """
    root_path = _validate_root(directory)
    if root_path is None: return
    
    visited_inodes: set[Inode] = set()
    stack: List[str] = [str(root_path)]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if skip_protected and _is_excluded_path(entry):
                            continue
                        
                        if entry.is_dir(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            inode = (st.st_dev, st.st_ino)
                            if inode not in visited_inodes:
                                visited_inodes.add(inode)
                                stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            yield Path(entry.path), int(st.st_size)
                    except (OSError, PermissionError, FileNotFoundError):
                        continue
        except (PermissionError, OSError, FileNotFoundError): 
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Retorna la lista de archivos más pesados en el directorio especificado."""
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Calcula la distribución de espacio de disco agrupada por extensión."""
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Identifica las subcarpetas de primer nivel que consumen más espacio."""
    root = _validate_root(directory)
    if not root: return []
    stats: Dict[Path, FolderMetrics] = defaultdict(FolderMetrics)
    
    for path, size in walk_files(root, skip_protected):
        try:
            relative = path.relative_to(root)
            if not relative.parts: continue
            top_folder = root / relative.parts[0]
            
            curr = stats[top_folder]
            curr.size += size
            curr.file_count += 1
        except (ValueError, IndexError, OSError, PermissionError):
            continue

    results = [FolderUsage(p, m.size, m.file_count) for p, m in stats.items()]
    return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> SizeReport:
    """Devuelve el tamaño total en bytes y el conteo de archivos de un directorio."""
    root = _validate_root(directory)
    if not root: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Ejecuta el escaneo completo de disco y agrega las métricas necesarias.
    Utiliza un heap (min-heap de tamaño fijo 'limit') para mantener los N archivos más grandes
    con una complejidad temporal de O(N log L) donde L es el límite.
    """
    stats = GlobalStats()
    top_heap: List[Tuple[int, Path]] = [] 
    
    try:
        for path, size_bytes in walk_files(directory, skip_protected):
            stats.register_file(size_bytes, path)
            
            if limit > 0:
                if len(top_heap) < limit: 
                    heapq.heappush(top_heap, (size_bytes, path))
                elif size_bytes > top_heap[0][0]: 
                    heapq.heapreplace(top_heap, (size_bytes, path))
    except (OSError, PermissionError, RuntimeError):
        pass
                
    return SummaryData(stats.total_bytes, stats.total_files, stats.ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera una representación de texto del reporte de uso de disco."""
    root = _validate_root(directory)
    if root is None: 
        return ["Error: Ruta no válida, protegida o inaccesible."]
    
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
