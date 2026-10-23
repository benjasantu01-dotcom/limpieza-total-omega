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

# Inode representado como (device_id, inode_number) para detectar ciclos de enlace duro
Inode: TypeAlias = Tuple[int, int]
# Tupla para el min-heap: (tamaño_en_bytes, ruta_completa)
FileRecord: TypeAlias = Tuple[int, Path]


def _safe_stat(path: Union[str, Path]) -> Optional[os.stat_result]:
    """
    Intenta obtener los atributos de un archivo/directorio sin seguir symlinks.
    Retorna None en caso de error de acceso, archivo inexistente o problemas de permisos.
    """
    if not path:
        return None
    try:
        return os.stat(path, follow_symlinks=False)
    except (OSError, PermissionError, ValueError):
        return None


class ExtStats:
    """
    Contenedor mutable de métricas para una extensión de archivo específica.
    """
    __slots__ = ('total_bytes', 'count')
    
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0

    def add(self, size: int) -> None:
        """Suma el tamaño de un archivo encontrado a la estadística acumulada."""
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.count += 1


class GlobalStats:
    """
    Clase centralizada para consolidar datos durante el recorrido recursivo.
    Mantiene el contador global y un diccionario de estadísticos por extensión.
    """
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.total_files: int = 0
        self.ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)

    def register_file(self, size: int, suffix: str) -> None:
        """
        Actualiza los contadores globales y los específicos de la extensión.
        Las extensiones se normalizan a minúsculas para unificar variantes (ej: .JPG -> .jpg).
        """
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.total_files += 1
            ext: str = (suffix.lower() if suffix else "(sin extensión)")
            self.ext_stats[ext].add(size)


class FolderMetrics:
    """Acumulador ligero para calcular el peso total de una subcarpeta."""
    __slots__ = ('size', 'file_count')
    
    def __init__(self) -> None:
        self.size: int = 0
        self.file_count: int = 0

    @property
    def size_mb(self) -> float:
        """Retorna el peso acumulado convertido a Megabytes."""
        return _bytes_to_mb(self.size)


class SummaryData(NamedTuple):
    """
    Estructura inmutable que transporta el resultado final del escaneo
    para evitar persistir el estado de GlobalStats tras finalizar el recorrido.
    """
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[FileRecord]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """Convierte bytes crudos a Megabytes (float) con redondeo a 2 decimales."""
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    return round(float(size_bytes) / MB_SIZE, 2)


def _validate_limit(limit: Any) -> int:
    """Filtra y normaliza los límites de resultados para evitar valores negativos o tipos inválidos."""
    if isinstance(limit, int) and not isinstance(limit, bool):
        return max(0, limit)
    return 0


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """
    Resuelve, normaliza y valida que una ruta raíz sea apta para escaneo.
    Verifica existencia, permisos de lectura y si no está en la lista de protegidas.
    """
    if not directory:
        return None
    try:
        p = Path(directory).expanduser()
        resolved_path = p.resolve(strict=False)
        
        if not resolved_path.exists() or not resolved_path.is_dir():
            return None
        if is_protected_path(resolved_path) or not os.access(resolved_path, os.R_OK):
            return None
            
        return resolved_path
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(entry: os.DirEntry, root_path: Path) -> bool:
    """
    Determina mediante heurísticas de seguridad si una entrada de directorio debe ser ignorada.
    """
    name = entry.name
    if not name or any(ord(c) < 32 for c in name) or len(name) > 255:
        return True
    if any(c in name for c in SUSPICIOUS_CHARS):
        return True
    if any(char in name for char in ('<', '>', ':', '"', '|', '?', '*')):
        return True

    try:
        path_obj = Path(entry.path).resolve(strict=False)
        
        # Validación de escape de raíz (seguridad defensiva)
        try:
            path_obj.relative_to(root_path)
        except ValueError:
            return True

        if is_protected_path(path_obj):
            return True

        if entry.is_symlink():
            return True
        
        if os.name == 'nt':
            st = entry.stat(follow_symlinks=False)
            if hasattr(st, 'st_file_attributes') and (st.st_file_attributes & 0x400):
                return True
        
        return False
    except (OSError, PermissionError, AttributeError, TypeError, RuntimeError, FileNotFoundError):
        return True
            
def _get_local_windows_drives() -> List[str]:
    """Enlista las unidades de disco montadas en Windows, saltando las protegidas."""
    import string
    drives: List[str] = []
    try:
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive) and not is_protected_path(Path(drive)):
                drives.append(drive)
    except (OSError, PermissionError):
        pass
    return drives


@dataclass(frozen=True)
class FileEntry:
    """Registro inmutable de un archivo encontrado en el escaneo."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class ExtensionUsage:
    """Reporte de consumo agregado por extensión de archivo."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class FolderUsage:
    """Reporte de consumo de una carpeta de primer nivel respecto al root escaneado."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class DriveUsage:
    """Snapshot del espacio en disco de una unidad."""
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        """Calcula el porcentaje de uso (0-100)."""
        if self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def is_almost_full(self) -> bool:
        """Retorna True si la unidad tiene menos del 10% de espacio disponible."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """Convierte bytes crudos en una cadena legible con unidad (B, KB, MB, GB, TB)."""
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
    """Retorna un objeto DriveUsage si la ruta es accesible y válida."""
    if mount is None:
        return None
    try:
        p = Path(mount).resolve(strict=False)
        if p.exists() and not is_protected_path(p):
            usage = shutil.disk_usage(p)
            return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        return None
    return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """Obtiene el estado de uso de todas las unidades montadas en el sistema."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Generador iterativo para recorrer el sistema de archivos, restringido a la raíz original.
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
                            try:
                                st = entry.stat(follow_symlinks=False)
                                inode = (st.st_dev, st.st_ino)
                                if inode not in visited_inodes:
                                    visited_inodes.add(inode)
                                    stack.append(entry.path)
                            except (OSError, PermissionError):
                                continue
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                st = entry.stat()
                                if hasattr(st, 'st_size') and st.st_size >= 0:
                                    yield Path(entry.path), st.st_size
                            except (OSError, PermissionError):
                                continue
                    except (PermissionError, FileNotFoundError, OSError):
                        continue
        except (OSError, PermissionError, FileNotFoundError, NotADirectoryError, ValueError): 
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Retorna los archivos de mayor tamaño encontrados dentro de la ruta."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Retorna las extensiones que consumen más espacio (agregado)."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Identifica las subcarpetas de primer nivel que ocupan más espacio."""
    root = _validate_root(directory)
    if root is None: return []
    
    stats: Dict[str, FolderMetrics] = defaultdict(FolderMetrics)
    
    for path, size in walk_files(root, skip_protected):
        try:
            parts = path.relative_to(root).parts
            if parts:
                top_folder = root / parts[0]
                metrics = stats[str(top_folder)]
                metrics.size += size
                metrics.file_count += 1
        except (ValueError, OSError):
            continue
            
    results = [FolderUsage(Path(p), m.size, m.file_count) for p, m in stats.items()]
    return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Tuple[int, int]:
    """Retorna (tamaño total en bytes, cantidad total de archivos)."""
    root = _validate_root(directory)
    if root is None: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """Escanea el disco consolidando todas las métricas en una estructura SummaryData."""
    stats = GlobalStats()
    top_heap: List[FileRecord] = []
    
    try:
        for path, size in walk_files(directory, skip_protected):
            stats.register_file(size, path.suffix)
            
            if limit > 0:
                if len(top_heap) < limit:
                    heapq.heappush(top_heap, (size, path))
                elif size > top_heap[0][0]:
                    heapq.heapreplace(top_heap, (size, path))
    except (PermissionError, OSError, Exception):
        pass
                
    return SummaryData(stats.total_bytes, stats.total_files, stats.ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera una lista de líneas de texto con un informe de uso para interfaz de usuario."""
    root = _validate_root(directory)
    if root is None: 
        return ["Error: La ruta seleccionada no es válida o no tiene permisos de lectura."]
    
    try:
        data = _collect_summary_data(root, skip_protected, limit=20)
    except (OSError, PermissionError, Exception):
        return ["Error: Fallo inesperado al realizar el escaneo."]
        
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
