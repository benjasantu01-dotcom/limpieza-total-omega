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

# Identificador único de inodo en sistema de archivos (dev, ino)
Inode: TypeAlias = Tuple[int, int]
# Métricas básicas: (tamaño_total_bytes, total_archivos)
SizeReport: TypeAlias = Tuple[int, int]


class ExtStats:
    """Contenedor mutable para acumular métricas por extensión durante el escaneo secuencial."""
    __slots__ = ('total_bytes', 'count')
    def __init__(self) -> None:
        self.total_bytes: int = 0
        self.count: int = 0


class SummaryData(NamedTuple):
    """Estructura inmutable que consolida las métricas tras un escaneo."""
    total_bytes: int
    total_files: int
    ext_stats: Dict[str, ExtStats]
    top_files: List[Tuple[int, Path]]


def _bytes_to_mb(size_bytes: int | float | None) -> float:
    """Convierte bytes a megabytes redondeados a 2 decimales; retorna 0.0 si es inválido."""
    try:
        if size_bytes is None or not isinstance(size_bytes, (int, float)) or size_bytes < 0:
            return 0.0
        return round(float(size_bytes) / MB_SIZE, 2)
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def _validate_limit(limit: Any) -> int:
    """Filtra y normaliza el límite de resultados para asegurar un entero >= 0."""
    try:
        if isinstance(limit, int) and not isinstance(limit, bool):
            return max(0, limit)
    except (ValueError, TypeError):
        pass
    return 0


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """Valida que la ruta sea existente, accesible y permitida por políticas de seguridad."""
    if directory is None:
        return None
    try:
        raw_path = Path(directory).resolve()
        # Prevenir rutas UNC inválidas o inexistentes
        if not raw_path.exists() or not raw_path.is_dir():
            return None
        if is_protected_path(raw_path) or not os.access(raw_path, os.R_OK):
            return None
        return raw_path
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        return None


def _is_excluded_path(entry: os.DirEntry, root_path: Path) -> bool:
    """
    Determina si una entrada de directorio debe ser omitida del análisis.
    
    Args:
        entry: Objeto DirEntry de os.scandir.
        root_path: Path raíz original para verificar límites.
        
    Returns:
        True si la ruta es insegura, externa a la raíz o protegida.
    """
    try:
        if any(c in entry.name for c in SUSPICIOUS_CHARS) or '\0' in entry.name:
            return True
        
        # Bloquear rutas UNC preventivamente
        path_str = entry.path
        if path_str.startswith(r'\\'):
            return True
        
        try:
            # 0x400 (FILE_ATTRIBUTE_REPARSE_POINT) detecta Junctions/Mount Points
            if entry.is_symlink() or (os.name == 'nt' and entry.is_dir() and (entry.stat().st_file_attributes & 0x400)):
                return True
        except (OSError, PermissionError):
            return True
        
        entry_path = Path(entry.path).resolve()
        try:
            if not entry_path.is_relative_to(root_path):
                return True
        except (ValueError, AttributeError):
            return True
            
        return is_protected_path(entry_path)
    except (OSError, PermissionError, AttributeError, RuntimeError, TypeError):
        return True


def _get_local_windows_drives() -> List[str]:
    """Escanea letras de unidad (A-Z) disponibles, omitiendo rutas de sistema protegidas."""
    import string
    drives: List[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            p = Path(drive)
            if p.exists() and not is_protected_path(p):
                drives.append(drive)
        except (OSError, PermissionError, RuntimeError):
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
        if not isinstance(self.total, (int, float)) or self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def is_almost_full(self) -> bool:
        """Retorna True si la unidad tiene menos del 10% de espacio libre."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """Convierte bytes a formato legible (e.g., 1.5 GB)."""
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
    """Calcula el uso de disco de una unidad montada."""
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
    """Obtiene reporte de uso de todas las unidades detectadas."""
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Generador recursivo de archivos omitiendo ciclos y rutas protegidas.
    
    Yields:
        Tupla (Path, size_bytes).
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
                            except (OSError, PermissionError): continue
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                st = entry.stat(follow_symlinks=False)
                                if st.st_size >= 0: yield Path(entry.path), st.st_size
                            except (OSError, PermissionError): continue
                    except (PermissionError, OSError): continue
        except (PermissionError, OSError): continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """Retorna los N archivos más grandes encontrados en el árbol de directorios."""
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=_validate_limit(limit))
    return [FileEntry(p, s) for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True)]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """Agrupa el peso total de archivos por extensión."""
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(ext, s.total_bytes, s.count) for ext, s in data.ext_stats.items()]
    return heapq.nlargest(_validate_limit(limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """Analiza las subcarpetas de primer nivel para identificar las de mayor peso."""
    root = _validate_root(directory)
    if not root: return []
    stats: Dict[Path, List[int]] = defaultdict(lambda: [0, 0])
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.is_dir() and not (skip_protected and is_protected_path(Path(entry.path))):
                    path = Path(entry.path)
                    try:
                        for f_path, f_size in walk_files(path, skip_protected):
                            stats[path][0] += f_size
                            stats[path][1] += 1
                    except (OSError, PermissionError): continue
    except (OSError, PermissionError): pass
    results = [FolderUsage(p, s[0], s[1]) for p, s in stats.items()]
    return heapq.nlargest(_validate_limit(limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> SizeReport:
    """Calcula el espacio total ocupado y la cantidad de archivos accesibles."""
    root = _validate_root(directory)
    if not root: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Realiza un escaneo profundo consolidando métricas y archivos pesados (vía heap).
    
    Args:
        directory: Raíz a escanear.
        skip_protected: Filtrar rutas seguras.
        limit: Tamaño del heap para archivos más grandes (0 para no recolectar).
        
    Returns:
        SummaryData con métricas agregadas.
    """
    total_bytes, total_files = 0, 0
    ext_stats: Dict[str, ExtStats] = defaultdict(ExtStats)
    top_heap: List[Tuple[int, Path]] = []
    
    for path, size_bytes in walk_files(directory, skip_protected):
        safe_size = max(0, size_bytes)
        total_bytes += safe_size
        total_files += 1
        ext = path.suffix.lower() or "(sin extensión)"
        stats = ext_stats[ext]
        stats.total_bytes += safe_size
        stats.count += 1
        
        if limit > 0:
            if len(top_heap) < limit: 
                heapq.heappush(top_heap, (safe_size, path))
            elif safe_size > top_heap[0][0]: 
                heapq.heapreplace(top_heap, (safe_size, path))
                
    return SummaryData(total_bytes, total_files, dict(ext_stats), top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera un reporte legible por humanos de la distribución del espacio."""
    root = _validate_root(directory)
    if root is None: return ["Error: Ruta no válida o inaccesible."]
    data = _collect_summary_data(root, skip_protected, limit=20)
    if data.total_files == 0: return ["Aviso: No hay archivos accesibles."]
    
    lines = [f"Carpeta: {root}", f"Total: {format_size(data.total_bytes)} en {data.total_files} archivos", "", "Por tipo:"]
    for ext, stats in heapq.nlargest(8, data.ext_stats.items(), key=lambda x: x[1].total_bytes):
        lines.append(f"  {ext:<18} {format_size(stats.total_bytes):>10}  ({stats.count} archivos)")
    
    if data.top_files:
        lines.extend(["", "Mayores archivos:"])
        for s, p in sorted(data.top_files, key=lambda x: x[0], reverse=True):
            lines.append(f"  {format_size(s):>10}  {str(p)}")
    return lines
