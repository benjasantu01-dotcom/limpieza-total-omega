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

from safety import is_protected_path, is_safe_to_modify

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

# Identificador único de archivo/directorio (dispositivo + inode) para evitar recursión en enlaces
Inode: TypeAlias = Tuple[int, int]
# Tupla para el heap de archivos más grandes: (tamaño_en_bytes, ruta_completa)
FileRecord: TypeAlias = Tuple[int, Path]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """Convierte bytes a Megabytes con precisión de dos decimales."""
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    return round(float(size_bytes) / MB_SIZE, 2)


def _safe_stat(path: Union[str, Path, None]) -> Optional[os.stat_result]:
    """
    Realiza una inspección lstat segura sin seguir enlaces simbólicos.
    Captura errores de acceso comunes en sistemas operativos restrictivos.
    """
    if path is None:
        return None
    try:
        return os.lstat(path)
    except (OSError, PermissionError, ValueError, AttributeError, OverflowError):
        return None


def _is_reparse_point(stat: os.stat_result) -> bool:
    """
    Identifica puntos de reanálisis (Junctions/Reparse Points) en Windows
    usando atributos de archivo para evitar bucles infinitos en el escaneo.
    """
    try:
        if os.name == 'nt' and hasattr(stat, 'st_file_attributes'):
            return bool(stat.st_file_attributes & 0x400)
        return False 
    except (AttributeError, OSError, TypeError):
        return False


def _get_extension(path: str) -> str:
    """Extrae y normaliza la extensión de un archivo, devolviendo un marcador por defecto si falta."""
    _, ext = os.path.splitext(path)
    return ext.lower() or "(sin extensión)"


def _get_immediate_child(root: Path, path: Path) -> Path:
    """
    Calcula el nodo de nivel superior relativo a 'root'.
    Útil para agrupar archivos por subcarpeta raíz en reportes.
    """
    try:
        if not path.exists():
            return path
        if path == root:
            return path
        parts = path.relative_to(root).parts
        return root / parts[0]
    except (ValueError, IndexError, OSError):
        return path


class ExtStats:
    """
    Acumulador para métricas agregadas de una extensión de archivo.
    Usa __slots__ para optimizar el consumo de memoria al manejar miles de archivos.
    """
    __slots__ = ('total_bytes', 'count')
    
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0

    def add(self, size: int) -> None:
        """Incrementa el contador y la suma de bytes para la extensión."""
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.count += 1


class GlobalStats:
    """
    Gestor del estado de agregación durante el escaneo recursivo.
    """
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.total_files: int = 0
        self.ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)

    def register_file(self, size: int, ext: str) -> None:
        """Actualiza las estadísticas globales y las de la extensión."""
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.total_files += 1
            self.ext_stats[ext].add(size)


class FolderMetrics:
    """
    Contenedor de métricas para el tamaño de subcarpetas individuales.
    """
    __slots__ = ('size', 'file_count')
    
    def __init__(self) -> None:
        self.size: int = 0
        self.file_count: int = 0

    def add(self, size: int) -> None:
        """Incrementa los bytes y el contador de archivos para la carpeta actual."""
        if isinstance(size, int) and size >= 0:
            self.size += size
            self.file_count += 1

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño en MB del acumulado de archivos en la carpeta."""
        return _bytes_to_mb(self.size)


class SummaryData(NamedTuple):
    """
    Resultados consolidados de un escaneo de directorio para uso interno.
    """
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[FileRecord]


def _validate_limit(limit: Any) -> int:
    """Valida que el límite sea un entero positivo, retornando 0 ante error."""
    return int(limit) if isinstance(limit, int) and limit > 0 else 0


def _is_unc_path(p: Path) -> bool:
    """Verifica si una ruta es UNC (comienza con \\) para evitar errores de red."""
    return str(p).startswith(os.sep * 2)


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """
    Normaliza la ruta raíz, verifica su existencia y comprueba restricciones de seguridad.
    """
    if not directory:
        return None
    try:
        p = Path(directory).expanduser().resolve(strict=True)
        # Se asegura que la ruta sea una carpeta y que pase el filtro de seguridad básico
        if not p.is_dir() or _is_unc_path(p) or is_protected_path(p) or not is_safe_to_modify(p):
            return None
        return p
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(name: str) -> bool:
    """
    Valida si un nombre de archivo o carpeta contiene caracteres sospechosos o inválidos.
    """
    if not name or len(name) > 255:
        return True
    invalid_chars = set('<>:"|?*')
    if any(c in invalid_chars or ord(c) < 32 for c in name):
        return True
    return any(c in name for c in SUSPICIOUS_CHARS)
            
def _get_local_windows_drives() -> List[str]:
    """Obtiene una lista de unidades físicas montadas accesibles en Windows."""
    import string
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        path_drive = Path(drive)
        if os.path.exists(drive) and not is_protected_path(path_drive) and is_safe_to_modify(path_drive):
            drives.append(drive)
    return drives


@dataclass(frozen=True)
class FileEntry:
    """Representación de un archivo para reportes de los más pesados."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class ExtensionUsage:
    """Agregado estadístico por extensión."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class FolderUsage:
    """Resultado estadístico de una carpeta específica."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class DriveUsage:
    """Estadísticas de uso de una unidad de almacenamiento."""
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        """Porcentaje de espacio utilizado en la unidad."""
        return round(self.used / self.total * 100, 1) if self.total > 0 else 0.0

    @property
    def is_almost_full(self) -> bool:
        """Retorna True si la unidad tiene menos del 10% de espacio disponible."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """Convierte bytes a representación legible para humanos (B, KB, MB, GB, TB)."""
    try:
        if not isinstance(num, (int, float)) or num < 0:
            return "0 B"
        value = float(num)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                decimals = 0 if unit == "B" else 1
                return f"{value:.{decimals}f} {unit}"
            value /= 1024
        return f"{value:.1f} TB"
    except (ValueError, TypeError, OverflowError):
        return "0 B"


def drive_usage(mount: Union[str, os.PathLike, None]) -> Optional[DriveUsage]:
    """Obtiene métricas de uso de disco de una unidad específica de forma segura."""
    if not mount: return None
    try:
        p = Path(mount).resolve()
        if not p.is_dir() or _is_unc_path(p) or is_protected_path(p) or not is_safe_to_modify(p):
            return None
        usage = shutil.disk_usage(p)
        return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """Devuelve métricas de uso para todas las unidades detectadas en el sistema."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    results = [d for m in targets if (d := drive_usage(m)) is not None]
    return results


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Recorre el árbol de directorios de forma iterativa, manteniendo un set de 
    inodos visitados para prevenir bucles causados por enlaces simbólicos.
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
        if not os.access(current_dir, os.R_OK):
            continue
            
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    # 1. Filtros preliminares de seguridad
                    if _is_excluded_path(entry.name):
                        continue
                    
                    entry_path = Path(entry.path)
                    if (skip_protected and is_protected_path(entry_path)) or not is_safe_to_modify(entry_path):
                        continue
                    
                    # 2. Obtención de metadatos y validación de tipos
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except (PermissionError, OSError):
                        continue
                        
                    # 3. Clasificación y navegación
                    if entry.is_dir(follow_symlinks=False):
                        if not _is_reparse_point(st):
                            inode = (st.st_dev, st.st_ino)
                            if inode not in visited_inodes:
                                visited_inodes.add(inode)
                                stack.append(entry.path)
                    
                    elif entry.is_file(follow_symlinks=False):
                        size = getattr(st, 'st_size', 0)
                        if isinstance(size, int) and size >= 0:
                            yield entry_path, size
        except (PermissionError, FileNotFoundError, OSError, ValueError):
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Retorna una lista ordenada de los N archivos más pesados encontrados."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Calcula la distribución de espacio agrupada por extensión de archivo."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Calcula el uso de disco de las subcarpetas inmediatas al directorio dado."""
    root = _validate_root(directory)
    if root is None: return []
    
    stats: Dict[Path, FolderMetrics] = defaultdict(FolderMetrics)
    
    for path, size in walk_files(root, skip_protected):
        try:
            top_level = _get_immediate_child(root, path)
            stats[top_level].add(size)
        except (ValueError, TypeError, PermissionError, OSError):
            continue
            
    results = [FolderUsage(p, m.size, m.file_count) for p, m in stats.items()]
    return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Tuple[int, int]:
    """Calcula el tamaño total en bytes y la cantidad total de archivos encontrados."""
    root = _validate_root(directory)
    if root is None: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Proceso central de escaneo. Consolida estadísticas globales y mantiene un 
    heap para identificar los archivos más pesados en una sola pasada.
    """
    stats = GlobalStats()
    top_heap: List[FileRecord] = []
    
    for path, size in walk_files(directory, skip_protected):
        try:
            if not isinstance(size, int) or size < 0:
                continue
            stats.register_file(size, _get_extension(str(path)))
            
            if limit > 0:
                if len(top_heap) < limit:
                    heapq.heappush(top_heap, (size, path))
                elif top_heap and size > top_heap[0][0]:
                    heapq.heapreplace(top_heap, (size, path))
        except (AttributeError, TypeError, ValueError):
            continue
                
    return SummaryData(stats.total_bytes, stats.total_files, stats.ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera un informe textual resumen con las estadísticas de uso del disco."""
    root = _validate_root(directory)
    if root is None: 
        return ["Error: Ruta inaccesible, protegida o no válida."]
        
    data = _collect_summary_data(root, skip_protected, limit=20)

    if data.total_files == 0: 
        return ["Aviso: No se encontraron archivos para analizar o falta permiso de lectura."]
    
    lines = [f"Carpeta: {root}", f"Total: {format_size(data.total_bytes)} ({data.total_files} archivos)", "", "Por tipo:"]
    
    for ext, stats in heapq.nlargest(8, data.ext_stats.items(), key=lambda x: x[1].total_bytes):
        lines.append(f"  {ext:<18} {format_size(stats.total_bytes):>10}  ({stats.count} archivos)")
    
    if data.top_files:
        lines.extend(["", "Mayores archivos:"])
        for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True):
            lines.append(f"  {format_size(s):>10}  {str(p)}")
    return lines
