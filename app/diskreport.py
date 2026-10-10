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
    """Acumulador de métricas para una extensión de archivo particular."""
    __slots__ = ('total_bytes', 'count')
    
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0

    def add(self, size: int) -> None:
        """Incrementa el peso total y el contador de archivos para esta extensión."""
        self.total_bytes += size
        self.count += 1


class GlobalStats:
    """
    Agregador global de métricas durante el escaneo de un directorio.
    Centraliza el cálculo de totales y la clasificación por extensiones.
    """
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.total_files: int = 0
        self.ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)

    def register_file(self, size: int, path: Path) -> None:
        """Actualiza las métricas globales con los datos de un archivo procesado."""
        self.total_bytes += size
        self.total_files += 1
        ext = path.suffix.lower() or "(sin extensión)"
        self.ext_stats[ext].add(size)


class FolderMetrics:
    """Acumulador temporal de peso para el cálculo de `largest_folders`."""
    __slots__ = ('size', 'file_count')
    def __init__(self) -> None:
        self.size: int = 0
        self.file_count: int = 0


class SummaryData(NamedTuple):
    """
    Resultado inmutable del proceso de escaneo.
    
    Attributes:
        total_bytes: Peso total acumulado en bytes.
        total_files: Cantidad total de archivos procesados.
        ext_stats: Mapeo de extensión -> ExtStats (datos de distribución).
        top_files: Lista (heap) de archivos más grandes encontrados.
    """
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[Tuple[int, Path]]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """Convierte bytes a MB (float) redondeado a 2 decimales. Maneja nulos y negativos."""
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    try:
        return round(float(size_bytes) / MB_SIZE, 2)
    except (ZeroDivisionError, OverflowError, ValueError):
        return 0.0


def _validate_limit(limit: Any) -> int:
    """Normaliza el límite de resultados para asegurar un entero no negativo."""
    if isinstance(limit, int) and not isinstance(limit, bool):
        return max(0, limit)
    return 0


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """
    Valida la existencia y seguridad de la ruta raíz.
    Retorna Path resuelto y validado, o None si no es seguro o no existe.
    """
    if directory is None:
        return None
    try:
        p = Path(directory).resolve()
        base = Path(os.getcwd()).resolve()
        
        # Seguridad: evitar salida de contexto del directorio de trabajo actual
        if not str(p).startswith(str(base)) and not p.is_absolute():
            return None

        if p.is_symlink() or not p.exists():
            return None
        if not p.is_dir() or is_protected_path(p) or not os.access(p, os.R_OK):
            return None
        return p
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(entry: os.DirEntry) -> bool:
    """Determina si un nodo del sistema de archivos debe ser ignorado."""
    try:
        if not entry.name or '\0' in entry.name or any(c in entry.name for c in SUSPICIOUS_CHARS):
            return True
        
        if entry.is_symlink():
            return True
            
        if is_protected_path(Path(entry.path)):
            return True

        if not os.access(entry.path, os.R_OK):
            return True

        if os.name == 'nt':
            try:
                st = entry.stat(follow_symlinks=False)
                # Verifica el atributo FILE_ATTRIBUTE_REPARSE_POINT (0x0400)
                if hasattr(st, 'st_file_attributes') and (st.st_file_attributes & 0x0400):
                     return True
            except (OSError, PermissionError):
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
    """Representación de un archivo individual con conversión de tamaño a MB."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class ExtensionUsage:
    """Métricas de uso por extensión (datos de resumen)."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class FolderUsage:
    """Métricas de uso por directorio (datos de resumen)."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class DriveUsage:
    """Información de capacidad de una unidad montada."""
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
    """Convierte un tamaño en bytes a string formateado (ej: '1.2 GB')."""
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
    """Obtiene el estado de espacio para una ruta de montaje."""
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
    """Retorna estados de todas las unidades (detecta SO para definir la raíz)."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Recorre el sistema de archivos (DFS) usando inodos para prevenir bucles.
    Yields: Tupla (ruta del archivo, tamaño en bytes).
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
                            # Acceso granular a stat para manejar errores de archivos bloqueados
                            st = entry.stat(follow_symlinks=False)
                            yield Path(entry.path), int(st.st_size)
                    except (OSError, PermissionError):
                        continue
        except (PermissionError, OSError): 
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Lista los archivos más grandes encontrados en la ruta raíz especificada."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Calcula la distribución del espacio agrupada por la extensión del archivo."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Identifica subcarpetas de primer nivel con mayor consumo de almacenamiento."""
    root = _validate_root(directory)
    if root is None: return []
    stats: Dict[str, FolderMetrics] = defaultdict(FolderMetrics)
    root_str = str(root)
    
    for path, size in walk_files(root, skip_protected):
        try:
            rel = str(path.parent)[len(root_str):].lstrip(os.sep)
            top_folder = rel.split(os.sep)[0]
            if not top_folder: continue
            
            target = root / top_folder
            curr = stats[str(target)]
            curr.size += size
            curr.file_count += 1
        except (ValueError, IndexError, OSError, PermissionError):
            continue

    results = [FolderUsage(Path(p), m.size, m.file_count) for p, m in stats.items()]
    return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> SizeReport:
    """Retorna el peso total (bytes) y cantidad de archivos en un directorio."""
    root = _validate_root(directory)
    if root is None: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Motor central: recorre el directorio, consolida métricas globales y mantiene
    un heap para identificar los archivos más pesados según el límite solicitado.
    """
    stats = GlobalStats()
    top_heap: List[Tuple[int, Path]] = [] 
    
    for path, size_bytes in walk_files(directory, skip_protected):
        try:
            stats.register_file(size_bytes, path)
            
            # Mantenimiento de heap para los 'limit' archivos más pesados
            if limit > 0:
                if len(top_heap) < limit: 
                    heapq.heappush(top_heap, (size_bytes, path))
                elif size_bytes > top_heap[0][0]: 
                    heapq.heapreplace(top_heap, (size_bytes, path))
        except (OSError, PermissionError, AttributeError):
            continue
                
    return SummaryData(stats.total_bytes, stats.total_files, stats.ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera una salida textual legible para el usuario final con el reporte."""
    root = _validate_root(directory)
    if root is None: 
        return ["Error: La ruta seleccionada no es válida o no tiene permisos de lectura."]
    
    data = _collect_summary_data(root, skip_protected, limit=20)
        
    if data.total_files == 0: 
        return ["Aviso: No se encontraron archivos accesibles en la ruta seleccionada."]
    
    lines = [f"Carpeta: {root}", f"Total: {format_size(data.total_bytes)} en {data.total_files} archivos", "", "Por tipo:"]
    for ext, stats in heapq.nlargest(8, data.ext_stats.items(), key=lambda x: x[1].total_bytes):
        lines.append(f"  {ext:<18} {format_size(stats.total_bytes):>10}  ({stats.count} archivos)")
    
    if data.top_files:
        lines.extend(["", "Mayores archivos:"])
        for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True):
            lines.append(f"  {format_size(s):>10}  {str(p)}")
    return lines
