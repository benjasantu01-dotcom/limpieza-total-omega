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
        """Retorna el tamaño acumulado convertido a megabytes."""
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
    """
    Valida, expande y resuelve una ruta raíz.
    
    Verifica:
    1. Si la ruta existe.
    2. Si es un directorio y no es un directorio protegido.
    3. Si el proceso tiene permisos de lectura sobre la ruta.
    
    Returns:
        Path resuelto si es válido, None en caso contrario.
    """
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


def _is_excluded_path(entry: os.DirEntry, root_path: Path) -> bool:
    """
    Filtra entradas de sistema, enlaces simbólicos o rutas peligrosas.
    
    Args:
        entry: Objeto DirEntry de os.scandir.
        root_path: Ruta raíz del análisis para evitar path traversal.
    """
    try:
        if os.path.islink(entry.path):
            return True
            
        entry_path = Path(entry.path).resolve()
        # Seguridad: Bloquea rutas que intenten salir del directorio raíz (path traversal)
        if root_path not in entry_path.parents and entry_path != root_path:
            return True
            
        if is_protected_path(entry_path):
            return True

        name = entry.name
        if not name or '\0' in name or any(c in name for c in SUSPICIOUS_CHARS):
            return True
        if any(char in name for char in ('<', '>', ':', '"', '|', '?', '*')):
            return True
        
        if os.name == 'nt':
            st = entry.stat(follow_symlinks=False)
            if bool(st.st_file_attributes & 0x400):
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
    except (OSError, PermissionError):
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
    """Formatea bytes a una cadena legible (ej: 1.5 MB) con su unidad correspondiente."""
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
    """Retorna un objeto `DriveUsage` con estadísticas de la unidad raíz indicada."""
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
    """Retorna una lista de `DriveUsage` para las unidades detectadas o especificadas."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Generador recursivo que recorre archivos bajo `directory`.
    
    Args:
        directory: Ruta base de búsqueda.
        skip_protected: Si es True, ignora directorios protegidos por el sistema.
    
    Yields:
        Tuplas (path, size_bytes).
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
                                st = entry.stat(follow_symlinks=False)
                                yield Path(entry.path), st.st_size
                            except (OSError, PermissionError):
                                continue
                    except (OSError, PermissionError):
                        continue
        except (PermissionError, OSError, FileNotFoundError): 
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Retorna una lista de los `limit` archivos más pesados encontrados en `directory`."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Agrupa el espacio ocupado por extensión y retorna las `limit` extensiones principales."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Calcula el peso total de las subcarpetas inmediatas al directorio base."""
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
    """Calcula el tamaño acumulado (bytes) y la cantidad de archivos accesibles."""
    root = _validate_root(directory)
    if root is None: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Escanea archivos y consolida estadísticas totales y top-N. 
    Usa un min-heap para mantener el ranking de archivos más grandes en memoria.
    """
    stats = GlobalStats()
    min_heap_for_top_files: List[FileRecord] = [] 
    
    try:
        for path, size_bytes in walk_files(directory, skip_protected):
            stats.register_file(size_bytes, path.suffix)
            
            if limit > 0:
                if len(min_heap_for_top_files) < limit: 
                    heapq.heappush(min_heap_for_top_files, (size_bytes, path))
                elif size_bytes > min_heap_for_top_files[0][0]: 
                    heapq.heapreplace(min_heap_for_top_files, (size_bytes, path))
    except (OSError, PermissionError, Exception):
        pass
                
    return SummaryData(stats.total_bytes, stats.total_files, stats.ext_stats, min_heap_for_top_files)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera un informe resumen en formato texto plano para UI o exportación."""
    root = _validate_root(directory)
    if root is None: 
        return ["Error: La ruta seleccionada no es válida o no tiene permisos de lectura."]
    
    try:
        data = _collect_summary_data(root, skip_protected, limit=20)
    except (OSError, PermissionError, Exception):
        return ["Error: Falló el escaneo del directorio raíz por falta de permisos o error de sistema."]
        
    if data.total_files == 0: 
        return ["Aviso: No se encontraron archivos accesibles en la ruta seleccionada."]
    
    lines = [f"Carpeta: {root}", f"Total: {format_size(data.total_bytes)} en {data.total_files} archivos", "", "Por tipo:"]
    
    if data.ext_stats:
        for ext, stats in heapq.nlargest(8, data.ext_stats.items(), key=lambda x: x[1].total_bytes):
            lines.append(f"  {ext:<18} {format_size(stats.total_bytes):>10}  ({stats.count} archivos)")
    
    if data.top_files:
        lines.extend(["", "Mayores archivos:"])
        for record in data.top_files:
            try:
                s, p = record
                lines.append(f"  {format_size(s):>10}  {str(p)}")
            except (TypeError, ValueError):
                continue
    return lines
