"""
duplicates.py — detección de archivos duplicados.

SOLO LECTURA: este módulo encuentra y agrupa duplicados, y sugiere cuál
conservar, pero **nunca borra ni mueve nada**.

Estrategia en tres pasos:
| Paso | Técnica | Propósito |
| :--- | :--- | :--- |
| 1 | Tamaño (stat) | Descarta archivos únicos rápidamente. |
| 2 | Hash Parcial | Filtra falsos positivos (igual tamaño, distinto contenido). |
| 3 | Hash Completo | Confirmación final de identidad (SHA256). |
"""

from __future__ import annotations
import hashlib
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Callable, Dict, List, Optional, Union, Tuple, Sequence

from safety import is_protected_path, is_safe_to_modify

__all__ = [
    "DuplicateGroup",
    "PARTIAL_READ_BYTES",
    "hash_file",
    "partial_hash",
    "group_by_size",
    "find_duplicates",
    "reclaimable_bytes",
    "suggest_keeper",
    "format_group",
]

# Cuántos bytes leer para el hash parcial. 64 KB alcanza para descartar
# archivos distintos y es una lectura despreciable incluso en discos lentos.
PARTIAL_READ_BYTES: int = 64 * 1024


@dataclass
class DuplicateGroup:
    """
    Representa una colección de archivos que comparten contenido idéntico.
    """
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def hash_file(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            while (buffer := f.read(chunk_size)):
                digest.update(buffer)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            content = f.read(read_bytes)
            if not content: return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None: return groups
    for p in paths:
        try:
            target = Path(p)
            if target.is_file() and not is_protected_path(target) and is_safe_to_modify(target):
                st = target.stat()
                if st.st_size > 0:
                    groups[st.st_size].append(target)
        except (OSError, PermissionError, FileNotFoundError):
            continue
    return groups


def _collect_candidates(
    directories: Iterable[Union[str, Path]], 
    min_size: int, 
    skip_protected: bool
) -> Dict[int, List[Path]]:
    temp_groups: Dict[int, List[Path]] = defaultdict(list)
    visited_inodes: set[Tuple[int, int]] = set()

    def _scan(root_path: Path) -> None:
        try:
            with os.scandir(root_path) as it:
                for entry in it:
                    try:
                        p_entry = Path(entry.path)
                        if skip_protected and (is_protected_path(p_entry) or not is_safe_to_modify(p_entry)):
                            continue
                        
                        st = entry.stat(follow_symlinks=False)
                        if getattr(st, 'st_file_attributes', 0) & 0x400: continue
                                
                        if entry.is_dir(follow_symlinks=False):
                            inode = (st.st_dev, st.st_ino)
                            if inode not in visited_inodes:
                                visited_inodes.add(inode)
                                _scan(p_entry)
                        elif entry.is_file() and st.st_size >= min_size:
                            temp_groups[int(st.st_size)].append(p_entry)
                    except (OSError, PermissionError): continue
        except (OSError, PermissionError): pass

    for item in directories:
        if item and Path(item).is_dir():
            _scan(Path(item))
    return {size: files for size, files in temp_groups.items() if len(files) > 1}


def _refine_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _process_size_group(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    confirmed_groups: List[DuplicateGroup] = []
    partial_results = _refine_by_hash(paths, partial_hash)
    for partial_candidates in partial_results.values():
        full_hash_groups = _refine_by_hash(partial_candidates, hash_file)
        for digest, confirmed_paths in full_hash_groups.items():
            confirmed_groups.append(DuplicateGroup(digest, size, sorted(confirmed_paths)))
    return confirmed_groups


def find_duplicates(directories: Iterable[Union[str, Path]], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    if directories is None or min_size < 0: return []
    groups: List[DuplicateGroup] = []
    for size, paths in _collect_candidates(directories, min_size, skip_protected).items():
        groups.extend(_process_size_group(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    return sum(g.wasted_bytes for g in groups) if groups else 0


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    keepers: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        try:
            stat_info = p.stat()
            keepers.append((float(stat_info.st_mtime), len(str(p)), p))
        except (OSError, PermissionError):
            continue
    return min(keepers, key=lambda x: (x[0], x[1]))[2] if keepers else None


def format_group(group: DuplicateGroup) -> List[str]:
    if not isinstance(group, DuplicateGroup) or not group.paths: return []
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    for path in group.paths:
        lines.append(f"   [{'conservar' if path == keeper else 'duplicado'}] {path}")
    return lines
