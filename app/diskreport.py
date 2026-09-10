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
from typing import Generator, Iterable, Dict, List, Tuple, Optional, Union, NamedTuple, TypeAlias

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
# Alias para representar identificadores únicos de sistema de archivos (dev, ino)
Inode: TypeAlias = Tuple[int, int]
# Alias para el reporte de (total_bytes, total_files)
SizeReport: TypeAlias = Tuple[int, int]


class SummaryData(NamedTuple):
    """Contenedor de resultados agregados durante un recorrido completo de directorio."""
    total_bytes: int
    total_files: int
    ext_sizes: Dict[str, int]
    ext_counts: Dict[str, int]
    top_files: List[Tuple[int, Path]]


def _bytes_to_mb(size_bytes: int | float) -> float:
    """
    Convierte bytes a MB con precisión de 2 decimales.
    
    Args:
        size_bytes: Tamaño en bytes (int o float).
        
    Returns:
        Tamaño en MB como float. Retorna 0.0 si el valor es negativo o no es numérico.
    """
    if not isinstance(size_bytes, (int, float)):
        return 0.0
    if size_bytes < 0:
        return 0.0
    return round(float(size_bytes) / MB_SIZE, 2)


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """
    Normaliza una ruta de entrada y valida su existencia, accesibilidad y seguridad.
    
    Args:
        directory: La ruta a verificar.
        
    Returns:
        Objeto Path resuelto si la ruta es un directorio, no es protegida y es legible; None en caso contrario.
    """
    if directory is None:
        return None
    try:
        path_str = os.fspath(directory)
        path_obj = Path(path_str).resolve(strict=True)
        if path_obj.is_dir() and not is_protected_path(path_obj) and os.access(path_obj, os.R_OK):
            return path_obj
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        pass
    return None


def _is_excluded_path(entry: os.DirEntry) -> bool:
    """
    Determina si una entrada de directorio debe ser excluida del escaneo.
    
    Identifica symlinks y puntos de reparse (Junctions) en Windows para evitar 
    recorridos redundantes o bucles infinitos en el sistema de archivos.
    """
    REPARSE_POINT_ATTR = 0x400
    try:
        if entry.is_symlink():
            return True
        if os.name == 'nt':
            st = entry.stat(follow_symlinks=False)
            return bool(getattr(st, 'st_file_attributes', 0) & REPARSE_POINT_ATTR)
    except (OSError, PermissionError, AttributeError):
        pass
    return False


def _get_local_windows_drives() -> List[str]:
    """
    Obtiene las letras de unidad locales disponibles en sistemas Windows.
    
    Filtra unidades de red o protegidas según `safety.is_protected_path`.
    """
    import string
    drives = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        try:
            if os.path.exists(drive) and not is_protected_path(Path(drive)):
                drives.append(drive)
        except (OSError, PermissionError):
            continue
    return drives


@dataclass(frozen=True)
class FileEntry:
    """Representa un archivo individual con su ruta y tamaño."""
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Tamaño convertido a MB."""
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class ExtensionUsage:
    """Representa el uso acumulado de espacio para una extensión de archivo."""
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        """Tamaño total acumulado de archivos con esta extensión en MB."""
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class FolderUsage:
    """Representa el peso de una subcarpeta inmediata."""
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        """Tamaño total de la carpeta en MB."""
        return _bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class DriveUsage:
    """Métricas de uso de almacenamiento de una unidad de disco."""
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        """Porcentaje de espacio utilizado (0.0 a 100.0)."""
        if not isinstance(self.total, (int, float)) or self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def is_almost_full(self) -> bool:
        """Retorna True si la unidad tiene menos del 10% de espacio libre."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """
    Formatea bytes a un string legible, escalando automáticamente de B a TB.
    
    Args:
        num: Tamaño en bytes.
        
    Returns:
        String formateado (ej: "1.0 KB"). Retorna "0 B" para valores no válidos.
    """
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
    """
    Obtiene el uso de espacio de un punto de montaje específico.
    
    Args:
        mount: Ruta o punto de montaje.
        
    Returns:
        Instancia de DriveUsage si la ruta es accesible y segura, None en caso contrario.
    """
    if mount is None:
        return None
    try:
        p = Path(os.fspath(mount)).resolve(strict=True)
        if p.is_dir() and not is_protected_path(p) and os.access(p, os.R_OK):
            usage = shutil.disk_usage(p)
            return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        pass
    return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """
    Recopila el uso de disco de todas las unidades locales disponibles.
    
    Args:
        mounts: Opcional, lista de puntos de montaje a escanear. Si es None, escanea todas las unidades locales.
        
    Returns:
        Lista de objetos DriveUsage detectados.
    """
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Generador que recorre recursivamente el sistema de archivos usando `os.scandir`.
    
    Implementa prevención de ciclos mediante chequeo de inodos y omisión de puntos de reparse.
    """
    root_path = _validate_root(directory)
    if root_path is None:
        return

    visited_inodes: set[Inode] = set()
    stack: List[Path] = [root_path]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if _is_excluded_path(entry): continue
                        
                        entry_path = Path(entry.path)
                        
                        if entry.is_dir(follow_symlinks=False):
                            if skip_protected and is_protected_path(entry_path): continue
                            st = entry.stat(follow_symlinks=False)
                            inode = (getattr(st, 'st_dev', 0), getattr(st, 'st_ino', 0))
                            if inode[0] != 0 and inode not in visited_inodes:
                                visited_inodes.add(inode)
                                stack.append(entry_path)
                        elif entry.is_file(follow_symlinks=False):
                            if skip_protected and is_protected_path(entry_path): continue
                            st = entry.stat()
                            size = getattr(st, 'st_size', 0)
                            yield entry_path, int(size) if size is not None else 0
                    except (PermissionError, OSError, AttributeError):
                        continue
        except (PermissionError, OSError):
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """
    Retorna los N archivos más pesados.
    
    Utiliza `_collect_summary_data` para realizar un único recorrido eficiente.
    """
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=limit)
    return [FileEntry(p, s) for s, p in heapq.nlargest(limit, data.top_files, key=lambda x: x[0])]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """
    Agrupa el uso de espacio por extensión de archivo.
    
    Realiza un único recorrido para totalizar tamaños y conteos.
    """
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected, limit=0)
    usage_list = [ExtensionUsage(e, data.ext_sizes[e], data.ext_counts[e]) for e in data.ext_sizes]
    return heapq.nlargest(max(1, limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """
    Calcula el peso total por subcarpeta inmediata en la raíz indicada.
    
    Nota: Este método realiza una agregación post-recorrido basada en la profundidad relativa.
    """
    root = _validate_root(directory)
    if not root: return []
    folder_total_bytes: Dict[Path, int] = defaultdict(int)
    folder_file_counts: Dict[Path, int] = defaultdict(int)
    
    for path, size in walk_files(root, skip_protected):
        try:
            rel = path.relative_to(root)
            if rel.parts:
                top_level_folder = root / rel.parts[0]
                folder_total_bytes[top_level_folder] += size
                folder_file_counts[top_level_folder] += 1
        except (ValueError, OSError): continue

    results = [FolderUsage(p, folder_total_bytes[p], folder_file_counts[p]) for p in folder_total_bytes]
    return heapq.nlargest(max(1, limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> SizeReport:
    """
    Calcula el total de bytes y cantidad de archivos en un único recorrido.
    """
    root = _validate_root(directory)
    if not root: return (0, 0)
    data = _collect_summary_data(root, skip_protected, limit=0)
    return (data.total_bytes, data.total_files)


def _collect_summary_data(directory: Path, skip_protected: bool, limit: int = 0) -> SummaryData:
    """
    Recolector central de estadísticas (motor de lectura única).
    
    Optimiza el rendimiento evitando recorridos redundantes: calcula tamaños, 
    conteos de extensiones y mantiene un heap para los archivos más grandes 
    (si limit > 0) simultáneamente durante el recorrido de `walk_files`.
    """
    total_bytes = total_files = 0
    ext_sizes: Dict[str, int] = defaultdict(int)
    ext_counts: Dict[str, int] = defaultdict(int)
    top_heap: List[Tuple[int, Path]] = []
    
    for path, size in walk_files(directory, skip_protected):
        # Validar nuevamente que es un archivo antes de procesar, por seguridad concurrente
        if not path.is_file():
            continue
            
        safe_size = int(size) if isinstance(size, (int, float)) else 0
        total_bytes += safe_size
        total_files += 1
        extension = path.suffix.lower() or "(sin extensión)"
        ext_sizes[extension] += safe_size
        ext_counts[extension] += 1
        
        if limit > 0:
            if len(top_heap) < limit:
                heapq.heappush(top_heap, (safe_size, path))
            elif safe_size > top_heap[0][0]:
                heapq.heapreplace(top_heap, (safe_size, path))
            
    return SummaryData(total_bytes, total_files, ext_sizes, ext_counts, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """
    Genera un reporte textual estructurado de los hallazgos en la carpeta dada.
    
    Combina los datos recolectados en `_collect_summary_data` para presentar 
    la vista de resumen de uso de disco.
    """
    root = _validate_root(directory)
    if not root: return ["Error: Ruta no válida."]
    data = _collect_summary_data(root, skip_protected, limit=20)
    
    if data.total_files == 0: return ["Aviso: No hay archivos accesibles."]

    lines = [f"Carpeta: {root}", f"Total: {format_size(data.total_bytes)} en {data.total_files} archivos", "", "Por tipo:"]
    for ext, size in heapq.nlargest(8, data.ext_sizes.items(), key=lambda x: x[1]):
        lines.append(f"  {ext:<18} {format_size(size):>10}  ({data.ext_counts[ext]} archivos)")
    lines.extend(["", "Mayores archivos:"])
    lines.extend([f"  {format_size(s):>10}  {p}" for s, p in heapq.nlargest(20, data.top_files, key=lambda x: x[0])])
    return lines
