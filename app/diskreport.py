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

MB_SIZE: Final[int] = 1024 * 1024
REPARSE_POINT_ATTRIBUTE: Final[int] = 0x400
SUSPICIOUS_CHARS: Final[Tuple[str, ...]] = ('\u202E', '\u202D', '\u200E', '\u200F', '\0')
RESERVED_NAMES: Final[Tuple[str, ...]] = (
    'CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 
    'COM6', 'COM7', 'COM8', 'COM9', 'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 
    'LPT6', 'LPT7', 'LPT8', 'LPT9'
)

# Inode: Identificador único de sistema de archivos (dispositivo, inodo) para prevenir recursión infinita
# al recorrer estructuras como enlaces simbólicos de directorio o puntos de montaje.
Inode: TypeAlias = Tuple[int, int]
# FileRecord: Tupla que almacena (tamaño_en_bytes, ruta_completa). Usado principalmente en min-heaps.
FileRecord: TypeAlias = Tuple[int, Path]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """Convierte bytes a Megabytes. La precisión de 2 decimales permite lecturas rápidas sin ruido."""
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return 0.0
    return round(float(size_bytes) / MB_SIZE, 2)


def _safe_stat(path: Union[str, Path]) -> Optional[os.stat_result]:
    """
    Inspección segura usando `lstat` para evitar resolución de enlaces (evita seguir
    puntos de reanálisis fuera de la ruta raíz).
    """
    try:
        p = Path(path)
        if is_protected_path(p) or not is_safe_to_modify(p):
            return None
        return p.lstat()
    except (OSError, PermissionError, FileNotFoundError, TypeError):
        return None


def _is_reparse_point(entry: os.DirEntry) -> bool:
    """
    Identifica Junctions/Reparse Points en Windows. Se usa el atributo de archivo 
    del sistema operativo directamente desde `os.DirEntry.stat` para eficiencia.
    """
    if os.name != 'nt':
        return False
    try:
        return bool(entry.stat(follow_symlinks=False).st_file_attributes & REPARSE_POINT_ATTRIBUTE)
    except (OSError, PermissionError, AttributeError):
        return False


def _get_extension(path: Path) -> str:
    """Extrae y normaliza la extensión. Agrupa archivos sin extensión para evitar diccionarios dispersos."""
    return path.suffix.lower() or "(sin extensión)"


class ExtStats:
    """
    Contenedor de agregación por extensión.
    __slots__ optimiza el uso de memoria ya que se instancian miles de estos objetos durante el escaneo.
    """
    __slots__ = ('total_bytes', 'count')
    
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0

    def add(self, size: int) -> None:
        """Suma bytes y cuenta archivos si la metadata es válida."""
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.count += 1


class GlobalStats:
    """Mantiene el estado durante el recorrido del disco. Centraliza la lógica de registro."""
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.total_files: int = 0
        self.ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)

    def register_file(self, size: int, ext: str) -> None:
        """Actualiza el acumulador global y las estadísticas de extensión específicas."""
        if isinstance(size, int) and size >= 0:
            self.total_bytes += size
            self.total_files += 1
            self.ext_stats[ext].add(size)


class FolderMetrics:
    """Acumula métricas para una jerarquía de carpetas específica."""
    __slots__ = ('size', 'file_count')
    
    def __init__(self) -> None:
        self.size: int = 0
        self.file_count: int = 0

    def add(self, size: int) -> None:
        """Acumula tamaño en bytes y contador de archivos para la carpeta."""
        if isinstance(size, int) and size >= 0:
            self.size += size
            self.file_count += 1

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size)


class SummaryData(NamedTuple):
    """Estructura inmutable que contiene el resultado final de un escaneo completo."""
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[FileRecord]


def _validate_limit(limit: Any) -> int:
    """Convierte parámetros de límite a enteros positivos para evitar errores en funciones de ordenamiento."""
    try:
        if limit is None: return 0
        return max(0, int(limit))
    except (ValueError, TypeError):
        return 0


def _is_unc_path(p: Path) -> bool:
    """Verifica si la ruta es una red (UNC) para restringir el escaneo de red."""
    return str(p).startswith(os.sep * 2)


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """Resuelve la ruta absoluta y aplica validaciones de seguridad estrictas."""
    if not directory:
        return None
    try:
        target = Path(directory).expanduser()
        if not target.exists():
            return None
        # Resolver canonicalmente para evitar trucos de '..' o enlaces
        path = target.resolve()
        if not path.is_dir() or _is_unc_path(path) or path.is_symlink():
            return None
        if is_protected_path(path) or not is_safe_to_modify(path) or not os.access(path, os.R_OK):
            return None
        return path
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(name: str | None) -> bool:
    """Filtra archivos con nombres reservados o caracteres de control que suelen ser errores del sistema."""
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
    """Lista letras de unidad disponibles. Requiere verificación de seguridad por ruta."""
    import string
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive: str = f"{letter}:\\"
        try:
            if os.path.exists(drive) and not is_protected_path(Path(drive)) and is_safe_to_modify(Path(drive)):
                drives.append(drive)
        except (OSError, PermissionError):
            continue
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
        return round(self.used / self.total * 100, 1) if self.total > 0 else 0.0

    @property
    def is_almost_full(self) -> bool:
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """Formatea bytes a string humano. Escala automáticamente a la unidad más apropiada."""
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
    """Consulta capacidad de disco mediante `shutil`. Incluye validación de seguridad."""
    try:
        if not mount: return None
        p = Path(mount).resolve()
        if is_protected_path(p) or not is_safe_to_modify(p) or not p.is_dir() or _is_unc_path(p) or not os.access(p, os.R_OK):
            return None
        usage = shutil.disk_usage(p)
        return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, TypeError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """Obtiene el estado de uso de todas las unidades. Si no se especifican, auto-detecta locales."""
    try:
        targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
        return [d for m in targets if (d := drive_usage(m)) is not None]
    except (TypeError, AttributeError):
        return []


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Recorre el sistema de archivos usando una pila (stack) para evitar desbordamiento de pila.
    Resiliente ante permisos denegados y rutas bloqueadas.
    """
    root_path = _validate_root(directory)
    if not root_path: return
    
    visited_inodes: set[Inode] = set()
    stack: List[str] = [str(root_path)]
    
    try:
        st = root_path.stat()
        visited_inodes.add((st.st_dev, st.st_ino))
    except (OSError, PermissionError): return
    
    while stack:
        current_dir = stack.pop()
        
        try:
            with os.scandir(current_dir) as iterator:
                while True:
                    try:
                        entry = next(iterator)
                        if _is_excluded_path(entry.name):
                            continue
                        
                        # Validar seguridad antes de seguir
                        if skip_protected and (is_protected_path(Path(entry.path)) or not is_safe_to_modify(Path(entry.path))):
                            continue

                        if entry.is_dir(follow_symlinks=False):
                            if not _is_reparse_point(entry):
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
                                if st.st_size >= 0:
                                    yield Path(entry.path), st.st_size
                            except (OSError, PermissionError):
                                continue
                    except StopIteration:
                        break
                    except (OSError, PermissionError):
                        continue
        except (PermissionError, FileNotFoundError, OSError):
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Retorna una lista de archivos ordenados de mayor a menor tamaño según el `limit` especificado."""
    root = _validate_root(directory)
    if not root: return []
    try:
        data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
        return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]
    except (TypeError, ValueError):
        return []


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Calcula uso total agrupado por extensión."""
    root = _validate_root(directory)
    if not root: return []
    try:
        data = _collect_summary_data(root, skip_protected, limit=0)
        usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
        return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)
    except (TypeError, ValueError):
        return []


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Calcula el uso de carpetas de primer nivel bajo el directorio raíz."""
    root = _validate_root(directory)
    if not root: return []
    
    stats: Dict[Path, FolderMetrics] = defaultdict(FolderMetrics)
    root_parts = root.parts
    
    for path, size in walk_files(root, skip_protected):
        parts = path.parts
        if len(parts) > len(root_parts):
            top_folder = Path(*parts[:len(root_parts) + 1])
            stats[top_folder].add(size)
            
    try:
        results = [FolderUsage(p, m.size, m.file_count) for p, m in stats.items() if m.size > 0]
        return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)
    except (TypeError, ValueError):
        return []


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Tuple[int, int]:
    """Retorna (total_bytes, contador_archivos) para un directorio dado."""
    root = _validate_root(directory)
    if not root: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Función interna core: utiliza un min-heap de tamaño `limit` para mantener los 
    archivos más grandes en memoria con eficiencia O(N log K).
    """
    stats = GlobalStats()
    top_heap: List[FileRecord] = []
    
    for path, size in walk_files(directory, skip_protected):
        # Doble chequeo de seguridad antes de procesar
        if not is_safe_to_modify(path) or is_protected_path(path):
            continue
        stats.register_file(size, _get_extension(path))
        if limit > 0:
            if len(top_heap) < limit:
                heapq.heappush(top_heap, (size, path))
            elif size > (top_heap[0][0] if top_heap else -1):
                heapq.heapreplace(top_heap, (size, path))
                
    return SummaryData(stats.total_bytes, stats.total_files, stats.ext_stats, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera un resumen textual legible de todo el análisis para el usuario."""
    root = _validate_root(directory)
    if root is None: return ["Error: Ruta inaccesible o no válida."]
    
    data = _collect_summary_data(root, skip_protected, limit=20)
    if data.total_files == 0: return ["Aviso: No se encontraron archivos procesables en esta ubicación."]
    
    lines = [f"Carpeta: {root}", f"Total: {format_size(data.total_bytes)} ({data.total_files} archivos)", "", "Por tipo:"]
    for ext, stats in heapq.nlargest(8, data.ext_stats.items(), key=lambda x: x[1].total_bytes):
        lines.append(f"  {ext:<18} {format_size(stats.total_bytes):>10}  ({stats.count} archivos)")
    
    if data.top_files:
        lines.extend(["", "Mayores archivos:"])
        for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True):
            lines.append(f"  {format_size(s):>10}  {str(p)}")
    return lines
