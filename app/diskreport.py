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
from typing import Generator, Iterable, Dict, List, Tuple, Optional, Union, NamedTuple

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


class SummaryData(NamedTuple):
    """
    Contenedor inmutable de resultados para el análisis unificado.
    
    Attributes:
        total_bytes: Suma total de bytes escaneados.
        total_files: Cantidad total de archivos procesados.
        ext_sizes: Diccionario {extensión: bytes_totales}.
        ext_counts: Diccionario {extensión: cantidad_archivos}.
        top_files: Lista de tuplas (tamaño, path) de los mayores archivos.
    """
    total_bytes: int
    total_files: int
    ext_sizes: Dict[str, int]
    ext_counts: Dict[str, int]
    top_files: List[Tuple[int, Path]]


def _bytes_to_mb(size_bytes: int | float) -> float:
    """
    Convierte una medida de bytes a Megabytes (MB).
    
    Args:
        size_bytes: Cantidad de bytes a convertir.
        
    Returns:
        Valor en MB redondeado a dos decimales. Retorna 0.0 ante errores de tipo o valores negativos.
    """
    if not isinstance(size_bytes, (int, float)):
        return 0.0
    val = float(size_bytes)
    if val <= 0:
        return 0.0
    return round(val / MB_SIZE, 2)


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    """
    Normaliza y valida una ruta raíz antes de iniciar cualquier escaneo.
    
    Args:
        directory: Ruta a validar como directorio raíz.
    
    Returns:
        Path absoluto y validado, o None si la ruta es inaccesible o prohibida.
    """
    try:
        if directory is None:
            return None
        p = Path(os.fspath(directory)).resolve(strict=True)
        if p.is_dir() and not is_protected_path(p):
            return p
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        pass
    return None


def _get_local_windows_drives() -> List[str]:
    """
    Detecta unidades físicas/lógicas disponibles en Windows mediante el sistema de archivos.
    """
    import string
    return [f"{letter}:\\" for letter in string.ascii_uppercase
            if os.path.exists(f"{letter}:\\")]


@dataclass
class FileEntry:
    """
    Registro de un archivo detectado. Contiene lógica para conversión de tamaño en MB.
    """
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass
class ExtensionUsage:
    """
    Agrupación estadística de archivos por extensión para reportes de consumo.
    """
    extension: str
    size_bytes: int
    count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass
class FolderUsage:
    """
    Métrica de uso para directorios, sumando el peso recursivo de sus hijos.
    """
    path: Path
    size_bytes: int
    file_count: int

    @property
    def size_mb(self) -> float:
        return _bytes_to_mb(self.size_bytes)


@dataclass
class DriveUsage:
    """
    Estado de almacenamiento de una unidad lógica.
    """
    mount: str
    total: int
    used: int
    free: int

    @property
    def used_percent(self) -> float:
        """Calcula el porcentaje de ocupación (0.0 a 100.0)."""
        if self.total <= 0:
            return 0.0
        return round(self.used / self.total * 100, 1)

    @property
    def is_almost_full(self) -> bool:
        """Determina si la unidad está al borde de su capacidad (< 10% libre)."""
        return self.total > 0 and (self.free / self.total) < 0.10


def format_size(num: Union[int, float, None]) -> str:
    """
    Formatea bytes a una representación legible (ej: '1.2 GB').
    
    Args:
        num: Tamaño en bytes a formatear.
        
    Returns:
        String formateado con la unidad correspondiente (B, KB, MB, GB, TB).
    """
    if num is None or not isinstance(num, (int, float)):
        return "0 B"
    
    value = float(num)
    if value < 0:
        return "0 B"
        
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            decimals = 0 if unit == "B" else 1
            return f"{value:.{decimals}f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def drive_usage(mount: Union[str, os.PathLike, None]) -> Optional[DriveUsage]:
    """
    Consulta el estado de una unidad. 
    
    Args:
        mount: Ruta del punto de montaje a consultar.
        
    Returns:
        Instancia de DriveUsage o None si la ruta es inaccesible o prohibida.
    """
    if mount is None:
        return None
        
    try:
        p = Path(os.fspath(mount)).resolve()
        # Verificar que la ruta no sea UNC y sea una carpeta válida en disco local
        if not p.exists() or not p.is_dir() or p.parts[0].startswith(("\\\\", "//")):
            return None
        
        if is_protected_path(p) or not os.access(p, os.R_OK):
            return None
            
        usage = shutil.disk_usage(p)
        return DriveUsage(mount=str(p), total=usage.total, used=usage.used, free=usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    """
    Obtiene métricas de todas las unidades locales o de una lista provista.
    
    Args:
        mounts: Opcional, lista de rutas a unidades para analizar.
        
    Returns:
        Lista de objetos DriveUsage con las métricas de las unidades encontradas.
    """
    target_mounts: Iterable[str]
    if mounts is None:
        target_mounts = _get_local_windows_drives() if os.name == "nt" else ["/"]
    else:
        target_mounts = mounts
    
    results: List[DriveUsage] = []
    for mount in target_mounts:
        if isinstance(mount, str) and mount:
            usage = drive_usage(mount)
            if usage:
                results.append(usage)
    return results


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
    """
    Recorrido profundo mediante DFS iterativo. 
    Evita ciclos de reparse points (NTFS junctions) y rutas protegidas.
    
    Args:
        directory: Raíz del escaneo.
        skip_protected: Si es True, ignora rutas marcadas en `safety.is_protected_path`.
        
    Yields:
        Tuplas (Path, tamaño_en_bytes) de cada archivo encontrado.
    """
    root_path = _validate_root(directory)
    if not root_path:
        return

    REPARSE_POINT_ATTR = 0x400
    visited_inodes: set[Tuple[int, int]] = set()
    stack: List[Path] = [root_path]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except (PermissionError, OSError, FileNotFoundError):
                        continue

                    if not entry.name or any(c < ' ' for c in entry.name):
                        continue
                    
                    if entry.is_symlink() or (os.name == 'nt' and (getattr(st, 'st_file_attributes', 0) & REPARSE_POINT_ATTR)):
                        continue
                    
                    if entry.is_dir(follow_symlinks=False):
                        child_path = Path(entry.path)
                        if skip_protected and is_protected_path(child_path):
                            continue
                                
                        inode_key = (st.st_dev, st.st_ino)
                        if inode_key not in visited_inodes:
                            visited_inodes.add(inode_key)
                            stack.append(child_path)
                                
                    elif entry.is_file(follow_symlinks=False):
                        # Se vuelve a verificar stat aquí por si el archivo fue eliminado al instante
                        try:
                            file_size = int(entry.stat().st_size)
                            yield Path(entry.path), max(0, file_size)
                        except (PermissionError, OSError, FileNotFoundError):
                            continue
        except (PermissionError, OSError, FileNotFoundError):
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    """
    Identifica los N archivos más grandes en la jerarquía proporcionada.
    """
    if not isinstance(limit, int) or limit <= 0:
        return []
    
    root_path = _validate_root(directory)
    if not root_path:
        return []
    
    items: Generator[Tuple[int, Path], None, None] = ((s, p) for p, s in walk_files(root_path, skip_protected))
    return [FileEntry(path=p, size_bytes=s) for s, p in heapq.nlargest(limit, items, key=lambda x: x[0])]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    """
    Agrega el consumo de disco clasificado por extensión de archivo.
    """
    if not isinstance(limit, int) or limit <= 0:
        return []

    root_path = _validate_root(directory)
    if not root_path:
        return []
        
    size_map: Dict[str, int] = defaultdict(int)
    count_map: Dict[str, int] = defaultdict(int)
    
    for path, size in walk_files(root_path, skip_protected):
        ext = path.suffix.lower() or "(sin extensión)"
        size_map[ext] += size
        count_map[ext] += 1
    
    usage_list: List[ExtensionUsage] = [
        ExtensionUsage(extension=ext, size_bytes=size, count=count_map[ext])
        for ext, size in size_map.items()
    ]
    
    return heapq.nlargest(limit, usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    """
    Agrupa el uso de disco por carpeta de primer nivel respecto al directorio base.
    """
    if not isinstance(limit, int) or limit <= 0:
        return []
    
    p_base = _validate_root(directory)
    if not p_base:
        return []
            
    sums: Dict[Path, int] = defaultdict(int)
    counts: Dict[Path, int] = defaultdict(int)

    for path, size in walk_files(p_base, skip_protected):
        try:
            rel = path.relative_to(p_base)
            top_folder = p_base / rel.parts[0]
            
            if skip_protected and is_protected_path(top_folder):
                continue
            
            sums[top_folder] += size
            counts[top_folder] += 1
        except (ValueError, IndexError):
            sums[p_base] += size
            counts[p_base] += 1

    results: List[FolderUsage] = [FolderUsage(p, sums[p], counts[p]) for p in sums]
    return heapq.nlargest(limit, results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Tuple[int, int]:
    """
    Calcula el tamaño total en bytes y el número total de archivos encontrados.
    
    Returns:
        Tupla (total_bytes, total_files).
    """
    total_bytes, file_count = 0, 0
    try:
        for _, size in walk_files(directory, skip_protected):
            total_bytes += size
            file_count += 1
    except (OSError, PermissionError, TypeError):
        pass
    return total_bytes, file_count


def _collect_summary_data(directory: Path, skip_protected: bool) -> SummaryData:
    """
    Recolección interna de métricas en una única pasada (O(n)).
    Usa un heap de tamaño 8 para mantener el top de archivos de forma eficiente.
    """
    total_bytes: int = 0
    total_files: int = 0
    ext_sizes: Dict[str, int] = defaultdict(int)
    ext_counts: Dict[str, int] = defaultdict(int)
    
    top_files_heap: List[Tuple[int, Path]] = []
    
    for path, size in walk_files(directory, skip_protected):
        if not path or not isinstance(size, (int, float)):
            continue
            
        total_bytes += size
        total_files += 1
        
        ext = path.suffix.lower() or "(sin extensión)"
        ext_sizes[ext] += size
        ext_counts[ext] += 1
        
        if len(top_files_heap) < 8:
            heapq.heappush(top_files_heap, (size, path))
        else:
            heapq.heappushpop(top_files_heap, (size, path))
            
    top_files = sorted(top_files_heap, key=lambda x: x[0], reverse=True)
    return SummaryData(total_bytes, total_files, dict(ext_sizes), dict(ext_counts), top_files)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    """Genera un informe textual resumen con las métricas principales."""
    p_input = _validate_root(directory)
    if not p_input:
        return ["Error: Ruta no proporcionada, inexistente o formato inválido."]
            
    try:
        data: SummaryData = _collect_summary_data(p_input, skip_protected)
    except (OSError, PermissionError, RuntimeError):
        return ["Error: Fallo inesperado durante el análisis del disco."]
    
    if data.total_files == 0:
        return ["Aviso: No se encontraron archivos accesibles en la ruta especificada."]

    lines = [
        f"Carpeta analizada: {p_input}", 
        f"Total: {format_size(data.total_bytes)} en {data.total_files} archivos", 
        "", 
        "Por tipo de archivo:"
    ]
    
    sorted_exts = heapq.nlargest(8, data.ext_sizes.items(), key=lambda item: item[1])
    for ext, size in sorted_exts:
        lines.append(f"  {ext:<18} {format_size(size):>10}  ({data.ext_counts[ext]} archivos)")
        
    lines.extend(["", "Archivos más grandes:"])
    for size, path in data.top_files:
        lines.append(f"  {format_size(size):>10}  {path}")
    return lines
