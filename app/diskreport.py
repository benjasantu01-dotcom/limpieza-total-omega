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
    
    Args:
        path: Ruta a verificar.
        
    Returns:
        os.stat_result si es accesible, None en caso contrario.
    """
    if not path:
        return None
    try:
        return os.stat(path, follow_symlinks=False)
    except (OSError, PermissionError, ValueError, FileNotFoundError):
        return None


class ExtStats:
    """
    Acumulador de métricas para una extensión de archivo específica.
    
    Attributes:
        total_bytes: Suma acumulada de bytes para esta extensión.
        count: Cantidad total de archivos encontrados con esta extensión.
    """
    __slots__ = ('total_bytes', 'count')
    
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0

    def add(self, size: int) -> None:
        """
        Incrementa el acumulador si el tamaño es un valor válido.
        
        Args:
            size: Tamaño en bytes a sumar.
        """
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.count += 1


class GlobalStats:
    """
    Agregador global durante el recorrido recursivo del sistema de archivos.
    
    Mantiene el total del volumen escaneado y clasifica el uso por extensión
    usando un diccionario de objetos ExtStats.
    """
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.total_files: int = 0
        self.ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)

    def register_file(self, size: int, suffix: str) -> None:
        """
        Registra un archivo y actualiza los totales y el mapa de extensiones.
        
        Args:
            size: Tamaño del archivo en bytes.
            suffix: Extensión del archivo incluyendo el punto.
        """
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.total_files += 1
            ext: str = (suffix.lower() if suffix else "(sin extensión)")
            self.ext_stats[ext].add(size)


class FolderMetrics:
    """
    Contenedor de métricas para el análisis de directorios.
    
    Attributes:
        size: Bytes totales acumulados.
        file_count: Cantidad total de archivos procesados.
    """
    __slots__ = ('size', 'file_count')
    
    def __init__(self) -> None:
        self.size: int = 0
        self.file_count: int = 0

    @property
    def size_mb(self) -> float:
        """Retorna el peso acumulado en Megabytes."""
        return _bytes_to_mb(self.size)


class SummaryData(NamedTuple):
    """Estructura inmutable con el resultado consolidado de un escaneo."""
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[FileRecord]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """Convierte bytes a Megabytes (float) redondeado a 2 decimales."""
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    return round(float(size_bytes) / MB_SIZE, 2)


def _validate_limit(limit: Any) -> int:
    """Valida que el límite sea un entero positivo, retornando 0 ante error."""
    if isinstance(limit, int) and not isinstance(limit, bool):
        return max(0, limit)
    return 0


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """
    Normaliza la ruta a un objeto Path absoluto y valida acceso de lectura.
    
    Returns:
        Objeto Path validado o None si la ruta es inválida, no existe, 
        está protegida o no permite acceso de lectura.
    """
    if not directory:
        return None
    try:
        p = Path(directory).expanduser().resolve(strict=False)
        if not p.exists() or not p.is_dir():
            return None
        if is_protected_path(p) or not os.access(p, os.R_OK):
            return None
            
        return p
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(name: str, full_path: str) -> bool:
    """
    Verifica si una ruta debe ser excluida del análisis por seguridad o formato.
    
    Args:
        name: Nombre del archivo o directorio.
        full_path: Ruta completa absoluta.
    """
    if not name or any(ord(c) < 32 for c in name) or len(name) > 255:
        return True
    if any(c in name for c in SUSPICIOUS_CHARS) or any(char in name for char in ('<', '>', ':', '"', '|', '?', '*')):
        return True
    return False
            
def _get_local_windows_drives() -> List[str]:
    """Obtiene unidades de disco disponibles en Windows ignorando protegidas."""
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
    """Representación de un archivo pesado para visualización."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class ExtensionUsage:
    """Agregado de datos por tipo de archivo."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class FolderUsage:
    """Agregado de datos por carpeta raíz."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class DriveUsage:
    """Estado de uso de una unidad de disco."""
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
        """Indica si la unidad tiene menos del 10% de espacio disponible."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """Transforma bytes a string amigable (ej: '1.2 GB')."""
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
    """Obtiene el uso de disco del punto de montaje provisto, validando seguridad."""
    if mount is None:
        return None
    try:
        p = Path(mount).resolve(strict=False)
        if not p.exists() or is_protected_path(p):
            return None
        usage = shutil.disk_usage(p)
        return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """Devuelve estado de todas las unidades detectadas."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Generador recursivo que recorre archivos evitando bucles de enlaces y rutas protegidas.
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
                        if skip_protected and _is_excluded_path(entry.name, entry.path):
                            continue
                        
                        if skip_protected and is_protected_path(Path(entry.path)):
                            continue
                        
                        st = entry.stat(follow_symlinks=False)
                            
                        if entry.is_dir(follow_symlinks=False):
                            inode = (st.st_dev, st.st_ino)
                            if inode not in visited_inodes:
                                visited_inodes.add(inode)
                                stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path), st.st_size
                            
                    except (OSError, PermissionError, FileNotFoundError):
                        continue
        except (OSError, PermissionError, FileNotFoundError, NotADirectoryError):
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Retorna los N archivos más grandes encontrados en la ruta."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Retorna una lista de ExtensionUsage ordenados por mayor uso de bytes."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Agrega y reporta el uso de carpetas de primer nivel dentro del directorio base."""
    root = _validate_root(directory)
    if root is None: return []
    
    stats: Dict[str, FolderMetrics] = defaultdict(FolderMetrics)
    
    for path, size in walk_files(root, skip_protected):
        try:
            relative = path.relative_to(root)
            if relative.parts:
                top_folder_name = relative.parts[0]
                metrics = stats[top_folder_name]
                metrics.size += size
                metrics.file_count += 1
        except ValueError:
            continue
            
    results = [FolderUsage(root / name, m.size, m.file_count) for name, m in stats.items()]
    return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Tuple[int, int]:
    """Devuelve (bytes totales, número total de archivos) en el árbol dado."""
    root = _validate_root(directory)
    if root is None: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Centraliza el recorrido y la agregación de métricas.
    
    Args:
        directory: Directorio raíz a analizar.
        skip_protected: Si se debe excluir rutas protegidas.
        limit: Tamaño del heap para almacenar los archivos más grandes.
    """
    stats = GlobalStats()
    top_heap: List[FileRecord] = []
    
    for path, size in walk_files(directory, skip_protected):
        try:
            stats.register_file(size, path.suffix)
            
            if limit > 0:
                if len(top_heap) < limit:
                    heapq.heappush(top_heap, (size, path))
                elif size > top_heap[0][0]:
                    heapq.heapreplace(top_heap, (size, path))
        except (AttributeError, TypeError, OSError):
            continue
                
    return SummaryData(stats.total_bytes, stats.total_files, stats.ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Crea una representación textual legible del reporte para la interfaz de usuario."""
    root = _validate_root(directory)

    if root is None: 
        return ["Error: La ruta seleccionada no es válida o no tiene permisos de lectura."]
    
    try:
        data = _collect_summary_data(root, skip_protected, limit=20)
    except (OSError, PermissionError):
        return ["Error: Fallo inesperado durante el acceso al sistema de archivos."]
        
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
