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
Inode: TypeAlias = Tuple[int, int]


class SummaryData(NamedTuple):
    total_bytes: int
    total_files: int
    ext_sizes: Dict[str, int]
    ext_counts: Dict[str, int]
    top_files: List[Tuple[int, Path]]


def _bytes_to_mb(size_bytes: int | float) -> float:
    if not isinstance(size_bytes, (int, float)):
        return 0.0
    val = float(size_bytes)
    return round(val / MB_SIZE, 2) if val > 0 else 0.0


def _validate_root(directory: Union[str, os.PathLike, None]) -> Optional[Path]:
    if directory is None:
        return None
    try:
        path_obj = Path(os.fspath(directory)).resolve(strict=True)
        if path_obj.is_dir() and not is_protected_path(path_obj) and os.access(path_obj, os.R_OK):
            return path_obj
    except (OSError, RuntimeError, PermissionError, TypeError, ValueError):
        pass
    return None


def _is_excluded_path(entry: os.DirEntry) -> bool:
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
    if mount is None:
        return None
    try:
        p = Path(os.fspath(mount)).resolve()
        if p.exists() and p.is_dir() and not is_protected_path(p) and os.access(p, os.R_OK):
            usage = shutil.disk_usage(p)
            return DriveUsage(str(p), usage.total, usage.used, usage.free)
    except (OSError, PermissionError, ValueError, RuntimeError, TypeError):
        pass
    return None


def all_drives_usage(mounts: Optional[Iterable[str]] = None) -> List[DriveUsage]:
    targets = mounts if mounts is not None else (_get_local_windows_drives() if os.name == "nt" else ["/"])
    return [d for m in targets if (d := drive_usage(m)) is not None]


def walk_files(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Generator[Tuple[Path, int], None, None]:
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
                        
                        if entry.is_dir(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            inode = (getattr(st, 'st_dev', 0), getattr(st, 'st_ino', 0))
                            if inode[0] != 0 and inode not in visited_inodes:
                                entry_path = Path(entry.path)
                                if not skip_protected or not is_protected_path(entry_path):
                                    visited_inodes.add(inode)
                                    stack.append(entry_path)
                        elif entry.is_file(follow_symlinks=False):
                            entry_path = Path(entry.path)
                            if not skip_protected or not is_protected_path(entry_path):
                                yield entry_path, max(0, int(entry.stat().st_size))
                    except (PermissionError, OSError, AttributeError):
                        continue
        except (PermissionError, OSError, FileNotFoundError):
            continue


def largest_files(directory: Union[str, os.PathLike, None], limit: int = 20, skip_protected: bool = True) -> List[FileEntry]:
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected)
    return [FileEntry(p, s) for s, p in sorted(data.top_files, reverse=True)[:max(1, limit)]]


def usage_by_extension(directory: Union[str, os.PathLike, None], limit: int = 15, skip_protected: bool = True) -> List[ExtensionUsage]:
    root = _validate_root(directory)
    if not root: return []
    data = _collect_summary_data(root, skip_protected)
    usage_list = [ExtensionUsage(e, data.ext_sizes[e], data.ext_counts[e]) for e in data.ext_sizes]
    return heapq.nlargest(max(1, limit), usage_list, key=lambda u: u.size_bytes)


def largest_folders(directory: Union[str, os.PathLike, None], limit: int = 10, skip_protected: bool = True) -> List[FolderUsage]:
    root = _validate_root(directory)
    if not root: return []
    sums: Dict[Path, int] = defaultdict(int)
    counts: Dict[Path, int] = defaultdict(int)
    
    for path, size in walk_files(root, skip_protected):
        try:
            rel = path.relative_to(root)
            if rel.parts:
                top = root / rel.parts[0]
                sums[top] += size
                counts[top] += 1
        except (ValueError, OSError): continue

    results = [FolderUsage(p, sums[p], counts[p]) for p in sums]
    return heapq.nlargest(max(1, limit), results, key=lambda f: f.size_bytes)


def total_size(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> Tuple[int, int]:
    root = _validate_root(directory)
    if not root: return 0, 0
    data = _collect_summary_data(root, skip_protected)
    return data.total_bytes, data.total_files


def _collect_summary_data(directory: Path, skip_protected: bool) -> SummaryData:
    total_bytes = total_files = 0
    ext_sizes: Dict[str, int] = defaultdict(int)
    ext_counts: Dict[str, int] = defaultdict(int)
    top_heap: List[Tuple[int, Path]] = []
    
    for path, size in walk_files(directory, skip_protected):
        total_bytes += size
        total_files += 1
        ext = path.suffix.lower() or "(sin extensión)"
        ext_sizes[ext] += size
        ext_counts[ext] += 1
        
        if len(top_heap) < 20:
            heapq.heappush(top_heap, (size, path))
        elif size > top_heap[0][0]:
            heapq.heapreplace(top_heap, (size, path))
            
    return SummaryData(total_bytes, total_files, ext_sizes, ext_counts, top_heap)


def summarize(directory: Union[str, os.PathLike, None], skip_protected: bool = True) -> List[str]:
    root = _validate_root(directory)
    if not root: return ["Error: Ruta no válida."]
    data = _collect_summary_data(root, skip_protected)
    
    if data.total_files == 0: return ["Aviso: No hay archivos accesibles."]

    lines = [f"Carpeta: {root}", f"Total: {format_size(data.total_bytes)} en {data.total_files} archivos", "", "Por tipo:"]
    for ext, size in heapq.nlargest(8, data.ext_sizes.items(), key=lambda x: x[1]):
        lines.append(f"  {ext:<18} {format_size(size):>10}  ({data.ext_counts[ext]} archivos)")
    lines.extend(["", "Mayores archivos:"])
    lines.extend([f"  {format_size(s):>10}  {p}" for s, p in sorted(data.top_files, reverse=True)])
    return lines
