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
    """Representa una colección de archivos que comparten contenido idéntico."""
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Retorna la cantidad de archivos duplicados en este grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """Calcula el espacio total que se liberaría borrando todos menos uno."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def hash_file(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo de un archivo mediante lectura en bloques."""
    if path is None: return None
    try:
        path_obj = Path(path).resolve()
        if not path_obj.exists() or not path_obj.is_file(): return None
        if is_protected_path(path_obj) or not is_safe_to_modify(path_obj): return None
        if not os.access(path_obj, os.R_OK): return None
        
        digest = hashlib.sha256()
        with open(path_obj, "rb") as f:
            while (buffer := f.read(chunk_size)):
                digest.update(buffer)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError, ValueError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula el hash SHA256 de los primeros N bytes para filtrado heurístico."""
    if path is None: return None
    try:
        path_obj = Path(path).resolve()
        if not path_obj.exists() or not path_obj.is_file(): return None
        if is_protected_path(path_obj) or not is_safe_to_modify(path_obj): return None
        if not os.access(path_obj, os.R_OK): return None
        
        with open(path_obj, "rb") as f:
            content = f.read(read_bytes)
            if not content: return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError, ValueError):
        return None


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """Clasifica rutas según su tamaño en bytes, filtrando rutas protegidas."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None: return groups
    
    for p in paths:
        if p is None: continue
        try:
            target = Path(p).resolve()
            if target.exists() and not is_protected_path(target) and is_safe_to_modify(target):
                st = target.stat()
                if st.st_size > 0:
                    groups[st.st_size].append(target)
        except (OSError, PermissionError, ValueError):
            continue
    return groups


def _collect_candidates(
    directories: Iterable[Union[str, Path]], 
    min_size: int, 
    skip_protected: bool
) -> Dict[int, List[Path]]:
    """Realiza un recorrido recursivo buscando candidatos mayores a min_size."""
    temp_groups: Dict[int, List[Path]] = defaultdict(list)
    visited_device_inodes: set[Tuple[int, int]] = set()
    processed_paths: set[Path] = set()

    def _scan(current_dir: Path) -> None:
        try:
            resolved_dir = current_dir.resolve()
            if not resolved_dir.is_dir() or resolved_dir in processed_paths: return
            processed_paths.add(resolved_dir)
            
            with os.scandir(resolved_dir) as it:
                for entry in it:
                    try:
                        if entry.is_symlink(): continue
                        stat = entry.stat(follow_symlinks=False)
                        
                        if entry.is_dir(follow_symlinks=False):
                            if getattr(stat, 'st_reparse_tag', 0) != 0: continue
                            dev_inode = (stat.st_dev, stat.st_ino)
                            if dev_inode not in visited_device_inodes:
                                visited_device_inodes.add(dev_inode)
                                _scan(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            if stat.st_size < min_size: continue
                            p = Path(entry.path).resolve()
                            if (not skip_protected or not is_protected_path(p)) and is_safe_to_modify(p):
                                temp_groups[int(stat.st_size)].append(p)
                    except (OSError, PermissionError): continue
        except (OSError, PermissionError, RuntimeError): pass

    if directories:
        for item in set(directories):
            if not item: continue
            try:
                root = Path(item).resolve(strict=True)
                if root.is_dir() and not is_protected_path(root) and is_safe_to_modify(root):
                    _scan(root)
            except (OSError, ValueError, RuntimeError): continue
    return {size: files for size, files in temp_groups.items() if len(files) > 1}


def _refine_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Agrupa archivos por colisiones de contenido usando hash_func."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if path and (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _process_size_group(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Pipeline de verificación de duplicados de dos niveles (parcial y completo)."""
    confirmed_groups: List[DuplicateGroup] = []
    partial_results = _refine_by_hash(paths, partial_hash)
    
    for partial_candidates in partial_results.values():
        full_hash_groups = _refine_by_hash(partial_candidates, hash_file)
        for digest, confirmed_paths in full_hash_groups.items():
            confirmed_groups.append(DuplicateGroup(digest, size, sorted(confirmed_paths)))
    return confirmed_groups


def find_duplicates(directories: Iterable[Union[str, Path]], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquesta la búsqueda de duplicados y retorna grupos por desperdicio de espacio."""
    if directories is None or min_size < 0: return []
    groups: List[DuplicateGroup] = []
    for size, paths in _collect_candidates(directories, min_size, skip_protected).items():
        groups.extend(_process_size_group(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma total de bytes recuperables de una lista de grupos."""
    if not groups or not isinstance(groups, (list, tuple)): return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Sugiere el archivo original según antigüedad (mtime) y longitud de ruta."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
        
    candidates: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        if p is None: continue
        try:
            p_obj = Path(p).resolve()
            if p_obj.exists() and p_obj.is_file() and not is_protected_path(p_obj) and is_safe_to_modify(p_obj):
                stat_info = p_obj.stat()
                candidates.append((float(stat_info.st_mtime), len(str(p_obj)), p_obj))
        except (OSError, PermissionError, ValueError):
            continue
            
    return min(candidates, key=lambda x: (x[0], x[1]))[2] if candidates else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera representación textual de un grupo para reportes."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return []
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    for path in group.paths:
        if not isinstance(path, Path): continue
        try:
            p_obj = path.resolve()
            if is_protected_path(p_obj) or not is_safe_to_modify(p_obj):
                lines.append(f"   [inseguro] {path}")
                continue
            label = 'conservar' if keeper is not None and p_obj == keeper else 'duplicado'
            lines.append(f"   [{label}] {path}")
        except (OSError, ValueError):
            lines.append(f"   [error] {path}")
    return lines
