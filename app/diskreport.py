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
from typing import Generator, Iterable, Dict, List, Tuple, Optional, Union, NamedTuple, TypeAlias, Any, Final

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

MB_SIZE: Final[int] = 1024 * 1024
SUSPICIOUS_CHARS: Final[Tuple[str, ...]] = ('\u202E', '\u202D', '\u200E', '\u200F')
RESERVED_NAMES: Final[Tuple[str, ...]] = (
    'CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 
    'COM6', 'COM7', 'COM8', 'COM9', 'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 
    'LPT6', 'LPT7', 'LPT8', 'LPT9'
)

# Identificador único de archivo/directorio (dispositivo + inode) para evitar recursión en enlaces
Inode: TypeAlias = Tuple[int, int]
# Tupla para el heap de archivos más grandes: (tamaño_en_bytes, ruta_completa)
FileRecord: TypeAlias = Tuple[int, Path]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """Convierte bytes a Megabytes con precisión de dos decimales."""
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    return round(float(size_bytes) / MB_SIZE, 2)


def _safe_stat(path: Union[str, Path]) -> Optional[os.stat_result]:
    """Realiza una inspección lstat segura sin seguir enlaces simbólicos."""
    try:
        return os.lstat(str(path))
    except (OSError, PermissionError, FileNotFoundError, TypeError):
        return None


def _is_reparse_point(path: Union[str, Path]) -> bool:
    """
    Identifica puntos de reanálisis (Junctions/Reparse Points) en Windows
    usando atributos de archivo para prevenir bucles de montaje recursivo.
    """
    if os.name != 'nt':
        return False
    try:
        if not os.path.exists(path) and not os.path.islink(path):
            return False
        stat_res = os.lstat(path)
        return bool(getattr(stat_res, 'st_file_attributes', 0) & 0x400)
    except (OSError, PermissionError, AttributeError, ValueError):
        return False


def _get_extension(path: Path) -> str:
    """Extrae y normaliza la extensión de un archivo, manejando casos sin extensión."""
    return path.suffix.lower() or "(sin extensión)"


class ExtStats:
    """
    Acumulador para estadísticas de uso agrupadas por extensión de archivo.
    Centraliza el conteo de archivos y el peso total acumulado.
    """
    __slots__ = ('total_bytes', 'count')
    
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0

    def add(self, size: int) -> None:
        """Suma el tamaño del nuevo archivo hallado al contador de la extensión."""
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.count += 1


class GlobalStats:
    """
    Objeto de estado para el escaneo global, orquestando la agregación de 
    datos crudos (tamaño total, conteo y estadísticas por extensión).
    """
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.total_files: int = 0
        self.ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)

    def register_file(self, size: int, ext: str) -> None:
        """Registra la métrica de un archivo procesado en el cómputo global."""
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.total_files += 1
            self.ext_stats[ext].add(size)


class FolderMetrics:
    """
    Acumulador para métricas jerárquicas a nivel de directorio, facilitando
    el cálculo del espacio ocupado por cada subcarpeta directa.
    """
    __slots__ = ('size', 'file_count')
    
    def __init__(self) -> None:
        self.size: int = 0
        self.file_count: int = 0

    def add(self, size: int) -> None:
        """Incrementa el peso y el contador de archivos de la carpeta evaluada."""
        if isinstance(size, int) and size >= 0:
            self.size += size
            self.file_count += 1

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño en MB para propósitos de visualización."""
        return _bytes_to_mb(self.size)


class SummaryData(NamedTuple):
    """Resultados consolidados obtenidos tras una iteración de escaneo."""
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[FileRecord]


def _validate_limit(limit: Any) -> int:
    """Convierte cualquier entrada a un límite entero no negativo."""
    try:
        if limit is None: return 0
        return max(0, int(limit))
    except (ValueError, TypeError):
        return 0


def _is_unc_path(p: Path) -> bool:
    """Verifica si una ruta utiliza el formato UNC (Universal Naming Convention)."""
    return str(p).startswith(os.sep * 2)


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """
    Valida, normaliza y resuelve una ruta raíz, aplicando filtros de seguridad
    de protección de sistema, rutas UNC y enlaces reparse.
    """
    if not directory:
        return None
    try:
        path = Path(directory).expanduser().resolve(strict=True)
        
        if not path.is_dir():
            return None
        if _is_unc_path(path):
            return None
        if is_protected_path(path):
            return None
        if _is_reparse_point(path):
            return None
            
        return path
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(name: str) -> bool:
    """Detecta nombres de archivo sospechosos, reservados o inválidos según estándares del SO."""
    if not name or len(name) > 255:
        return True
    
    base_name = name.split('.')[0].upper()
    if base_name in RESERVED_NAMES:
        return True
        
    invalid_chars = set('<>:"|?*')
    if any(c in invalid_chars or ord(c) < 32 for c in name):
        return True
    return any(c in name for c in SUSPICIOUS_CHARS)
            
def _get_local_windows_drives() -> List[str]:
    """
    Escanea las letras de unidad disponibles en Windows y filtra aquellas 
    protegidas por el sistema.
    """
    import string
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive: str = f"{letter}:\\"
        try:
            if os.path.exists(drive):
                if not is_protected_path(Path(drive)):
                    drives.append(drive)
        except (OSError, PermissionError):
            continue
    return drives


@dataclass(frozen=True)
class FileEntry:
    """Representación inmutable de un archivo para reportes de usuario."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Tamaño en MB del archivo."""
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class ExtensionUsage:
    """Representación inmutable de las estadísticas por tipo de archivo."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        """Tamaño total en MB ocupado por esta extensión."""
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class FolderUsage:
    """Representación inmutable del uso de disco de una carpeta."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        """Tamaño en MB de la carpeta."""
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class DriveUsage:
    """Estadísticas de ocupación de una unidad lógica."""
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        """Calcula el porcentaje de ocupación (0-100)."""
        return round(self.used / self.total * 100, 1) if self.total > 0 else 0.0

    @property
    def is_almost_full(self) -> bool:
        """Retorna True si la unidad tiene menos del 10% de espacio disponible."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """Convierte un valor en bytes a una cadena legible (B, KB, MB, GB, TB)."""
    if not isinstance(num, (int, float)) or num < 0:
        return "0 B"
    try:
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
    """Obtiene métricas de uso de una unidad específica, validando su acceso."""
    if not mount: return None
    try:
        p = Path(mount).resolve()
        if not p.exists() or not p.is_dir() or _is_unc_path(p) or is_protected_path(p):
            return None
        usage = shutil.disk_usage(p)
        return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, TypeError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """Retorna métricas para todas las unidades locales detectadas."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Recorre el árbol de directorios de forma iterativa (DFS), evitando
    la recursión infinita en enlaces simbólicos o puntos de montaje.
    """
    root_path = _validate_root(directory)
    if not root_path: return
    
    visited_inodes: set[Inode] = set()
    stack: List[Path] = [root_path]
    
    st = _safe_stat(root_path)
    if st: visited_inodes.add((st.st_dev, st.st_ino))
    
    while stack:
        current_dir = stack.pop()
        
        if not os.access(current_dir, os.R_OK | os.X_OK):
            continue
            
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if _is_excluded_path(entry.name):
                            continue
                        
                        full_path = Path(entry.path)
                        if skip_protected and is_protected_path(full_path):
                            continue
                        
                        if entry.is_dir(follow_symlinks=False):
                            if not _is_reparse_point(full_path):
                                st_dir = entry.stat(follow_symlinks=False)
                                inode = (st_dir.st_dev, st_dir.st_ino)
                                if inode not in visited_inodes:
                                    visited_inodes.add(inode)
                                    stack.append(full_path)
                        elif entry.is_file(follow_symlinks=False):
                            st_file = entry.stat(follow_symlinks=False)
                            size = getattr(st_file, 'st_size', None)
                            if isinstance(size, int) and size >= 0:
                                yield full_path, size
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Retorna la lista de archivos más grandes contenidos en un directorio dado."""
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Genera un ranking de extensiones de archivo según el tamaño total ocupado."""
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Calcula el uso de disco de las subcarpetas de nivel superior inmediatas."""
    root = _validate_root(directory)
    if not root: return []
    
    stats: Dict[Path, FolderMetrics] = defaultdict(FolderMetrics)
    
    for path, size in walk_files(root, skip_protected):
        try:
            rel = path.relative_to(root)
            if rel.parts:
                immediate_folder = root / rel.parts[0]
                stats[immediate_folder].add(size)
        except (ValueError, IndexError):
            continue
                
    results = [FolderUsage(p, m.size, m.file_count) for p, m in stats.items() if m.size > 0]
    return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Tuple[int, int]:
    """Retorna un par con el tamaño total en bytes y el conteo de archivos procesados."""
    root = _validate_root(directory)
    if not root: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Realiza un escaneo consolidado del directorio, utilizando un min-heap para
    mantener eficientemente los `limit` archivos más pesados durante el recorrido.
    """
    stats = GlobalStats()
    top_heap: List[FileRecord] = []
    
    for path, size in walk_files(directory, skip_protected):
        stats.register_file(size, _get_extension(path))
        if limit > 0:
            if len(top_heap) < limit:
                heapq.heappush(top_heap, (size, path))
            elif size > top_heap[0][0]:
                heapq.heapreplace(top_heap, (size, path))
                
    return SummaryData(stats.total_bytes, stats.total_files, stats.ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera un informe textual resumen del uso de disco para su visualización."""
    root = _validate_root(directory)
    if root is None: return ["Error: Ruta inaccesible o no válida."]
        
    try:
        data = _collect_summary_data(root, skip_protected, limit=20)
    except (OSError, PermissionError):
        return ["Error: Fallo al analizar el sistema de archivos."]
        
    if data.total_files == 0: return ["Aviso: No se encontraron archivos procesables."]
    
    lines = [f"Carpeta: {root}", f"Total: {format_size(data.total_bytes)} ({data.total_files} archivos)", "", "Por tipo:"]
    for ext, stats in heapq.nlargest(8, data.ext_stats.items(), key=lambda x: x[1].total_bytes):
        lines.append(f"  {ext:<18} {format_size(stats.total_bytes):>10}  ({stats.count} archivos)")
    
    if data.top_files:
        lines.extend(["", "Mayores archivos:"])
        for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True):
            lines.append(f"  {format_size(s):>10}  {str(p)}")
    return lines
