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


def _safe_stat(path: str) -> Optional[os.stat_result]:
    """
    Intenta recuperar los metadatos de un archivo sin seguir enlaces simbólicos.
    
    Returns:
        os.stat_result si es accesible, None en caso de error de acceso (permisos, 
        archivo no encontrado o rutas demasiado largas).
    """
    try:
        if len(path) > 32767:
            return None
        return os.stat(path, follow_symlinks=False)
    except (OSError, PermissionError, FileNotFoundError):
        return None


class ExtStats:
    """Contenedor de métricas para agrupar estadísticas por extensión."""
    __slots__ = ('total_bytes', 'count')
    
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0

    def add(self, size: int) -> None:
        """Incrementa el contador total de bytes y de archivos para la extensión."""
        self.total_bytes += size
        self.count += 1


class GlobalStats:
    """
    Acumulador central que orquesta las métricas durante el escaneo.
    Centraliza el conteo global y la desagregación por tipo de archivo.
    """
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.total_files: int = 0
        self.ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)

    def register_file(self, size: int, path: Path) -> None:
        """Registra un archivo, actualizando los totales globales y por tipo."""
        self.total_bytes += size
        self.total_files += 1
        ext = path.suffix.lower() or "(sin extensión)"
        self.ext_stats[ext].add(size)


class FolderMetrics:
    """
    Contenedor mutable utilizado para realizar agregaciones temporales 
    de peso por subcarpeta durante el proceso de escaneo.
    """
    __slots__ = ('size', 'file_count')
    def __init__(self) -> None:
        self.size = 0
        self.file_count = 0


class SummaryData(NamedTuple):
    """
    Estructura consolidada que agrupa los resultados de una pasada de escaneo:
    - total_bytes/total_files: Métricas globales.
    - ext_stats: Diccionario de acumuladores por tipo.
    - top_files: Heap de archivos encontrados (ordenados por tamaño).
    """
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[Tuple[int, Path]]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """Convierte bytes a MB con precisión de dos decimales."""
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    try:
        return round(float(size_bytes) / MB_SIZE, 2)
    except (ZeroDivisionError, OverflowError, ValueError):
        return 0.0


def _validate_limit(limit: Any) -> int:
    """Normaliza un límite de resultados asegurando un entero no negativo."""
    if isinstance(limit, int) and not isinstance(limit, bool):
        return max(0, limit)
    return 0


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """Valida que una ruta sea un directorio existente y seguro para ser escaneado."""
    if directory is None:
        return None
    try:
        p = Path(directory).expanduser().resolve()
        if not p.exists() or not p.is_dir():
            return None
        if is_protected_path(p) or not os.access(p, os.R_OK):
            return None
        return p
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(entry: os.DirEntry, root_path: Path) -> bool:
    """
    Filtra entradas durante el escaneo para evitar bucles o acceso a áreas restringidas.
    Realiza chequeos de seguridad basados en la ruta y en metadatos del inodo.
    """
    try:
        name = entry.name
        if not name or '\0' in name or any(c in name for c in SUSPICIOUS_CHARS):
            return True
        
        # Validar pertenencia rápida comparando partes de la ruta
        path_parts = Path(entry.path).parts
        if len(path_parts) < len(root_path.parts) or path_parts[:len(root_path.parts)] != root_path.parts:
            return True

        try:
            st = entry.stat(follow_symlinks=False)
            is_reparse = (st.st_file_attributes & 0x0400) if os.name == 'nt' else entry.is_symlink()
            if is_reparse:
                return True
        except (OSError, PermissionError, AttributeError):
            return True
        return is_protected_path(Path(entry.path))
    except (OSError, PermissionError, AttributeError, RuntimeError, TypeError):
        return True
            
def _get_local_windows_drives() -> List[str]:
    """Retorna unidades montadas en Windows excluyendo protegidas."""
    import string
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            if os.path.exists(drive):
                p = Path(drive)
                if not is_protected_path(p):
                    drives.append(drive)
        except (OSError, PermissionError, RuntimeError):
            continue
    return drives


@dataclass(frozen=True)
class FileEntry:
    """Representa un archivo individual hallado durante el escaneo."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class ExtensionUsage:
    """Métricas agregadas para un grupo de archivos con la misma extensión."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class FolderUsage:
    """Métricas de uso para una subcarpeta específica."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class DriveUsage:
    """Estadísticas de espacio de una unidad de almacenamiento."""
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
    """Convierte bytes a formato legible (B, KB, MB, GB, TB)."""
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
    """Consulta el estado del espacio de disco en el punto de montaje indicado."""
    if mount is None:
        return None
    try:
        path_str = os.fspath(mount)
        p = Path(path_str).resolve()
        if p.exists() and not is_protected_path(p):
            usage = shutil.disk_usage(p)
            return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        return None
    return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """Retorna una lista con el estado de las unidades analizadas."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Recorre el sistema de archivos de forma iterativa usando una pila para evitar recursión profunda.
    Mantiene un conjunto de inodos visitados para evitar ciclos en enlaces simbólicos a directorios.
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
                        if skip_protected and _is_excluded_path(entry, root_path):
                            continue
                        
                        if entry.is_dir(follow_symlinks=False):
                            st = _safe_stat(entry.path)
                            if st:
                                inode = (st.st_dev, st.st_ino)
                                if inode not in visited_inodes:
                                    visited_inodes.add(inode)
                                    stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = _safe_stat(entry.path)
                            if st is not None and hasattr(st, 'st_size'):
                                yield Path(entry.path), int(st.st_size)
                    except (OSError, PermissionError, AttributeError, ValueError, RuntimeError):
                        continue
        except (PermissionError, OSError, FileNotFoundError, RuntimeError): 
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Retorna los N archivos más grandes encontrados en la ruta raíz."""
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Calcula el uso de disco total agrupado por extensión de archivo."""
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """
    Calcula el peso total de las subcarpetas de primer nivel respecto a la raíz.
    Agrupa recursivamente los tamaños de los archivos encontrados bajo cada directorio directo.
    """
    root = _validate_root(directory)
    if not root: return []
    stats: Dict[Path, FolderMetrics] = defaultdict(FolderMetrics)
    
    for path, size in walk_files(root, skip_protected):
        try:
            rel = path.relative_to(root)
            # Manejo defensivo: si path es igual a root o no tiene partes, omitir
            if not rel.parts: continue
            top_folder = root / rel.parts[0]
            curr = stats[top_folder]
            curr.size += size
            curr.file_count += 1
        except (ValueError, IndexError, RuntimeError, TypeError):
            continue

    results = [FolderUsage(p, m.size, m.file_count) for p, m in stats.items()]
    return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> SizeReport:
    """Calcula la suma de bytes y cantidad de archivos en la ruta especificada."""
    root = _validate_root(directory)
    if not root: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Ejecuta el escaneo completo manteniendo las estadísticas requeridas.
    Utiliza un heap para mantener de forma eficiente los 'top archivos' encontrados.
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
    """Genera un reporte de texto legible con un resumen del escaneo del directorio."""
    root = _validate_root(directory)
    if root is None: return ["Error: Ruta no válida, protegida o inaccesible."]
    
    try:
        data = _collect_summary_data(root, skip_protected, limit=20)
    except (OSError, RuntimeError, PermissionError):
        return ["Error durante el escaneo de archivos."]
        
    if data.total_files == 0: return ["Aviso: No hay archivos accesibles."]
    
    lines = [f"Carpeta: {root}", f"Total: {format_size(data.total_bytes)} en {data.total_files} archivos", "", "Por tipo:"]
    for ext, stats in heapq.nlargest(8, data.ext_stats.items(), key=lambda x: x[1].total_bytes):
        lines.append(f"  {ext:<18} {format_size(stats.total_bytes):>10}  ({stats.count} archivos)")
    
    if data.top_files:
        lines.extend(["", "Mayores archivos:"])
        for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True):
            lines.append(f"  {format_size(s):>10}  {str(p)}")
    return lines
