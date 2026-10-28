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
    Obtiene metadatos del archivo sin seguir enlaces simbólicos (evita escapes).

    Args:
        path: Ruta (string o Path) a inspeccionar.

    Returns:
        Objeto `os.stat_result` con permisos y tamaño, o None si hay error de acceso.
    """
    if not path:
        return None
    try:
        return os.stat(path, follow_symlinks=False)
    except (OSError, PermissionError, ValueError, FileNotFoundError):
        return None


class ExtStats:
    """
    Estructura acumuladora para métricas agregadas por tipo de extensión.
    """
    __slots__ = ('total_bytes', 'count')
    
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0

    def add(self, size: int) -> None:
        """Suma el tamaño de un archivo y incrementa el contador de la extensión."""
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.count += 1


class GlobalStats:
    """
    Contenedor principal para consolidar métricas durante un escaneo completo.
    """
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.total_files: int = 0
        self.ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)

    def register_file(self, size: int, ext: str) -> None:
        """Registra un archivo procesado y actualiza el estado global."""
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.total_files += 1
            self.ext_stats[ext].add(size)


class FolderMetrics:
    """Acumulador temporal para el cálculo de peso total por carpeta."""
    __slots__ = ('size', 'file_count')
    
    def __init__(self) -> None:
        self.size: int = 0
        self.file_count: int = 0

    @property
    def size_mb(self) -> float:
        """Retorna el peso total convertido a Megabytes."""
        return _bytes_to_mb(self.size)


class SummaryData(NamedTuple):
    """Resultados consolidados obtenidos tras ejecutar una iteración de escaneo."""
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[FileRecord]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """
    Convierte bytes a Megabytes.
    
    Args:
        size_bytes: Tamaño en bytes, entero o flotante.
    
    Returns:
        Valor en MB redondeado a 2 decimales, 0.0 si la entrada es inválida.
    """
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    return round(float(size_bytes) / MB_SIZE, 2)


def _validate_limit(limit: Any) -> int:
    """
    Valida y normaliza el límite de resultados para funciones de ordenamiento.

    Returns:
        Un entero positivo o cero, filtrando entradas inválidas o booleanos.
    """
    if isinstance(limit, int) and not isinstance(limit, bool):
        return max(0, limit)
    return 0


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """
    Normaliza y valida que la ruta sea un directorio raíz apto para escaneo.
    
    Returns:
        Path absoluto si es válido, None si es ruta protegida o inaccesible.
    """
    if not directory:
        return None
    try:
        p = Path(directory).expanduser().resolve(strict=False)
        # Verificación estricta: existencia y acceso de lectura
        if not p.exists() or not p.is_dir() or is_protected_path(p) or not os.access(p, os.R_OK):
            return None
        return p
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(name: str, full_path: str) -> bool:
    """Detecta nombres de archivo o rutas que contienen caracteres potencialmente maliciosos."""
    if not name or any(ord(c) < 32 for c in name) or len(name) > 255:
        return True
    return any(c in name for c in SUSPICIOUS_CHARS) or any(char in name for char in ('<', '>', ':', '"', '|', '?', '*'))
            
def _get_local_windows_drives() -> List[str]:
    """Descubre unidades de disco montadas en Windows mediante enumeración de letras A-Z."""
    import string
    drives: List[str] = []
    try:
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            # Verificar si existe, es accesible y es un directorio real antes de añadir
            if os.path.exists(drive) and not is_protected_path(Path(drive)):
                try:
                    # os.access con R_OK asegura que podemos leer el contenido de la raíz
                    if os.access(drive, os.R_OK):
                        drives.append(drive)
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        pass
    return drives


@dataclass(frozen=True)
class FileEntry:
    """DTO para reportar archivos individuales."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class ExtensionUsage:
    """DTO para reportar resúmenes por tipo de extensión."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class FolderUsage:
    """DTO para reportar carga de carpetas pesadas."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class DriveUsage:
    """DTO para representar el espacio de almacenamiento de una unidad."""
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        """Porcentaje de ocupación sobre el total de la unidad."""
        return round(self.used / self.total * 100, 1) if self.total > 0 else 0.0

    @property
    def is_almost_full(self) -> bool:
        """Evalúa si la unidad tiene menos del 10% de espacio disponible."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """Convierte bytes a un formato legible por humanos (B, KB, MB, GB, TB)."""
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
    """Obtiene métricas de uso de disco para un punto de montaje dado."""
    if mount is None: return None
    try:
        p = Path(mount).expanduser().resolve(strict=False)
        # Verificamos acceso real antes de pedir el uso para evitar bloqueos en unidades vacías
        if not p.exists() or is_protected_path(p) or not os.access(p, os.R_OK):
            return None
        usage = shutil.disk_usage(p)
        return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """Retorna una lista con el uso de todas las unidades disponibles."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Recorrido recursivo iterativo de directorios para evitar desbordamientos de pila.
    
    Yields:
        Tuplas (ruta, tamaño_bytes) por cada archivo encontrado.
    """
    root_path = _validate_root(directory)
    if root_path is None: return
    
    visited_inodes: set[Inode] = set()
    stack: List[Path] = [root_path]
    
    try:
        root_stat = root_path.stat()
        visited_inodes.add((root_stat.st_dev, root_stat.st_ino))
    except (OSError, PermissionError):
        return
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        # Saltar si es protegido o tiene caracteres ilegales
                        if skip_protected and (is_protected_path(Path(entry.path)) or _is_excluded_path(entry.name, entry.path)):
                            continue
                        
                        # Impedir seguimiento de symlinks y reparse points (junctions)
                        if entry.is_symlink():
                            continue
                        
                        st = _safe_stat(entry.path)
                        if st is None: continue
                            
                        if entry.is_dir():
                            inode = (st.st_dev, st.st_ino)
                            if inode not in visited_inodes:
                                visited_inodes.add(inode)
                                stack.append(Path(entry.path))
                        elif entry.is_file():
                            yield Path(entry.path), st.st_size
                            
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Obtiene una lista de los N archivos más grandes en la ruta especificada."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Obtiene una lista de extensiones ordenadas por el volumen de datos ocupado."""
    root = _validate_root(directory)
    if root is None: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Analiza carpetas de primer nivel y retorna las que más espacio consumen."""
    root = _validate_root(directory)
    if root is None: return []
    
    stats: Dict[Path, FolderMetrics] = defaultdict(FolderMetrics)
    
    for path, size in walk_files(root, skip_protected):
        try:
            # Encontrar el ancestro inmediato al root para agrupar
            relative = path.relative_to(root)
            if relative.parts:
                top_level = root / relative.parts[0]
                metrics = stats[top_level]
                metrics.size += size
                metrics.file_count += 1
        except (ValueError, IndexError):
            continue
            
    results = [FolderUsage(path, m.size, m.file_count) for path, m in stats.items()]
    return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Tuple[int, int]:
    """Calcula bytes totales y cantidad total de archivos en una ruta."""
    root = _validate_root(directory)
    if root is None: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """Función de utilidad centralizada para recorrer el disco y acumular estadísticas."""
    stats = GlobalStats()
    top_heap: List[FileRecord] = []
    
    for path, size in walk_files(directory, skip_protected):
        ext = path.suffix.lower() if path.suffix else "(sin extensión)"
        stats.register_file(size, ext)
        
        if limit > 0:
            if len(top_heap) < limit:
                heapq.heappush(top_heap, (size, path))
            elif top_heap and size > top_heap[0][0]:
                heapq.heapreplace(top_heap, (size, path))
                
    return SummaryData(stats.total_bytes, stats.total_files, stats.ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera un reporte legible listo para ser renderizado en la interfaz."""
    root = _validate_root(directory)
    if root is None: 
        return ["Error: La ruta seleccionada no es válida o no tiene permisos de lectura."]
    
    try:
        data = _collect_summary_data(root, skip_protected, limit=20)
    except (OSError, PermissionError):
        return ["Error: Acceso denegado durante el escaneo del directorio."]
        
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
