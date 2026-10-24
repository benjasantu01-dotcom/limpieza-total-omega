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
    except (OSError, PermissionError, ValueError):
        return None


class ExtStats:
    """
    Acumulador de métricas para una extensión de archivo.
    
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
        Incrementa las métricas si el tamaño es un valor válido no negativo.
        
        Args:
            size: Tamaño en bytes del archivo.
        """
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.count += 1


class GlobalStats:
    """
    Agregador principal de estadísticas durante el recorrido recursivo de archivos.
    
    Mantiene el estado global de bytes, conteo total y un mapa de `ExtStats`
    para permitir el desglose por tipo de archivo al finalizar el escaneo.
    """
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.total_files: int = 0
        self.ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)

    def register_file(self, size: int, suffix: str) -> None:
        """
        Registra un archivo en las estadísticas globales.
        
        Args:
            size: Tamaño en bytes del archivo.
            suffix: Extensión del archivo (incluyendo punto).
        """
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.total_files += 1
            ext: str = (suffix.lower() if suffix else "(sin extensión)")
            self.ext_stats[ext].add(size)


class FolderMetrics:
    """
    Acumulador de peso de subcarpetas utilizado durante el cálculo de `largest_folders`.
    
    Attributes:
        size: Peso total en bytes de los archivos contenidos en la carpeta.
        file_count: Cantidad total de archivos procesados en esta ruta.
    """
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
    Snapshot de resultados de un escaneo completo.
    """
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[FileRecord]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """Convierte bytes a Megabytes (float) redondeado a 2 decimales para reportes."""
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    return round(float(size_bytes) / MB_SIZE, 2)


def _validate_limit(limit: Any) -> int:
    """Filtra límites de resultados para asegurar que sean enteros positivos."""
    if isinstance(limit, int) and not isinstance(limit, bool):
        return max(0, limit)
    return 0


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """
    Resuelve y normaliza una ruta, verificando que no sea un recurso protegido.
    
    Returns:
        Objeto Path absoluto o None si la ruta es inaccesible o restringida.
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


def _is_excluded_path(entry: os.DirEntry, root_path: Path) -> bool:
    """
    Filtro de seguridad para determinar si un archivo o directorio debe omitirse.
    Verifica caracteres peligrosos, rutas fuera de raíz (jail-break) y atributos de sistema.
    """
    name = entry.name
    if not name or any(ord(c) < 32 for c in name) or len(name) > 255:
        return True
        
    if any(c in name for c in SUSPICIOUS_CHARS) or any(char in name for char in ('<', '>', ':', '"', '|', '?', '*')):
        return True

    try:
        entry_path = Path(entry.path).resolve(strict=False)
        entry_path.relative_to(root_path)

        if not (entry.is_file(follow_symlinks=False) or entry.is_dir(follow_symlinks=False)):
            return True

        if is_protected_path(entry_path):
            return True

        if entry.is_symlink():
            return True
        
        if os.name == 'nt':
            st = entry.stat(follow_symlinks=False)
            if hasattr(st, 'st_file_attributes') and (st.st_file_attributes & 0x400):
                return True
        
        return False
    except (OSError, PermissionError, AttributeError, ValueError, RuntimeError):
        return True
            
def _get_local_windows_drives() -> List[str]:
    """Retorna una lista de unidades montadas en Windows, filtrando las protegidas."""
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
    """Representación de un archivo pesado encontrado."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class ExtensionUsage:
    """Datos agregados por extensión."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class FolderUsage:
    """Datos agregados por subcarpeta de primer nivel."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class DriveUsage:
    """Métricas de uso de una unidad de almacenamiento."""
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        """Calcula el porcentaje de ocupación actual."""
        if self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def is_almost_full(self) -> bool:
        """True si la unidad tiene menos del 10% de espacio libre."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """Convierte bytes en formato legible (B, KB, MB, GB, TB)."""
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
    """Retorna información de uso de disco para un punto de montaje específico."""
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
    """Retorna una lista con el estado de todas las unidades detectadas."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Generador iterativo que recorre archivos bajo la raíz dada, evitando recursión circular.
    
    Yields:
        Tupla (ruta_archivo, tamaño_en_bytes).
    """
    root_path = _validate_root(directory)
    if root_path is None: return
    
    visited_inodes: set[Inode] = set()
    stack: List[Path] = [root_path]
    
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
                                    stack.append(Path(entry.path))
                            except (OSError, PermissionError):
                                continue
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                st = entry.stat(follow_symlinks=False)
                                if st and st.st_size >= 0:
                                    yield Path(entry.path), st.st_size
                            except (OSError, PermissionError):
                                continue
                    except (PermissionError, FileNotFoundError, OSError):
                        continue
        except (OSError, PermissionError, FileNotFoundError, NotADirectoryError, ValueError): 
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Devuelve los N archivos más grandes encontrados en la ruta."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Devuelve las extensiones que consumen mayor espacio agregado."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Agrupa el consumo de espacio por carpetas de primer nivel."""
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
    """
    Función de utilidad central que ejecuta el recorrido y agrupa datos.
    
    Usa un min-heap de tamaño fijo para mantener el top-N de archivos de forma eficiente.
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
        except (AttributeError, TypeError):
            continue
                
    return SummaryData(stats.total_bytes, stats.total_files, stats.ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera una vista textual del informe de uso para ser presentada en la UI."""
    root = _validate_root(directory)

    if root is None: 
        return ["Error: La ruta seleccionada no es válida o no tiene permisos de lectura."]
    
    try:
        data = _collect_summary_data(root, skip_protected, limit=20)
    except Exception:
        return ["Error: Fallo inesperado durante el escaneo del sistema de archivos."]
        
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
