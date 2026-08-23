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
        """Calcula el espacio total que se liberaría borrando todos menos uno."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def hash_file(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo de un archivo. Retorna None en caso de error de acceso."""
    path_obj = Path(path)
    if not is_safe_to_modify(path_obj) or not path_obj.is_file(): return None
    try:
        digest = hashlib.sha256()
        with open(path_obj, "rb") as f:
            while (buffer := f.read(chunk_size)):
                digest.update(buffer)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula el hash de los primeros N bytes para filtrado rápido."""
    path_obj = Path(path)
    if not is_safe_to_modify(path_obj) or not path_obj.is_file(): return None
    try:
        with open(path_obj, "rb") as f:
            content = f.read(read_bytes)
            if not content: return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """Agrupa una lista de rutas existentes por su tamaño en bytes."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None: return groups
    for p in paths:
        try:
            if not p: continue
            target = Path(p).resolve()
            if target.is_file() and not is_protected_path(target) and is_safe_to_modify(target):
                st = target.stat()
                if st.st_size > 0:
                    groups[st.st_size].append(target)
        except (OSError, PermissionError, FileNotFoundError, RuntimeError):
            continue
    return groups


def _collect_candidates(
    directories: Iterable[Union[str, Path]], 
    min_size: int, 
    skip_protected: bool
) -> Dict[int, List[Path]]:
    """
    Realiza un recorrido recursivo en profundidad por el sistema de archivos.
    """
    temp_groups: Dict[int, List[Path]] = defaultdict(list)
    visited_device_inodes: set[Tuple[int, int]] = set()

    def _scan(current_dir: Path) -> None:
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                        if getattr(entry_stat, 'st_reparse_tag', 0) != 0:
                            continue
                            
                        entry_path = Path(entry.path).resolve()
                        if skip_protected and is_protected_path(entry_path):
                            continue
                            
                        if entry.is_dir(follow_symlinks=False):
                            device_inode = (entry_stat.st_dev, entry_stat.st_ino)
                            if device_inode not in visited_device_inodes:
                                visited_device_inodes.add(device_inode)
                                _scan(entry_path)
                        elif entry.is_file(follow_symlinks=False):
                            if entry_stat.st_size >= min_size and is_safe_to_modify(entry_path):
                                temp_groups[int(entry_stat.st_size)].append(entry_path)
                    except (OSError, PermissionError): continue
        except (OSError, PermissionError): pass

    if directories is not None:
        for item in directories:
            if item:
                root_path = Path(item).resolve()
                if root_path.is_dir() and not is_protected_path(root_path) and is_safe_to_modify(root_path):
                    _scan(root_path)
    return {size: files for size, files in temp_groups.items() if len(files) > 1}


def _refine_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Agrupa rutas idénticas aplicando una función de hash."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _process_size_group(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Pipeline de confirmación reduciendo candidatos."""
    confirmed_groups: List[DuplicateGroup] = []
    
    partial_results = _refine_by_hash(paths, partial_hash)
    for partial_candidates in partial_results.values():
        full_hash_groups = _refine_by_hash(partial_candidates, hash_file)
        for digest, confirmed_paths in full_hash_groups.items():
            confirmed_groups.append(DuplicateGroup(digest, size, sorted(confirmed_paths)))
    return confirmed_groups


def find_duplicates(directories: Iterable[Union[str, Path]], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Función principal: orquesta la búsqueda y retorna grupos ordenados por ahorro potencial."""
    if directories is None or min_size < 0: return []
    groups: List[DuplicateGroup] = []
    for size, paths in _collect_candidates(directories, min_size, skip_protected).items():
        groups.extend(_process_size_group(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma total de espacio recuperable (bytes)."""
    return sum(g.wasted_bytes for g in groups) if groups is not None else 0


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Selecciona el mejor archivo para conservar: prioriza antigüedad y ruta corta."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    keepers: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        try:
            p_obj = Path(p)
            if p_obj.is_file() and is_safe_to_modify(p_obj):
                stat_info = p_obj.stat()
                keepers.append((float(stat_info.st_mtime), len(str(p_obj)), p_obj))
        except (OSError, PermissionError, FileNotFoundError):
            continue
    return min(keepers, key=lambda x: (x[0], x[1]))[2] if keepers else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Retorna una representación legible para reportes."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return []
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    for path in group.paths:
        label = 'conservar' if keeper is not None and path == keeper else 'duplicado'
        lines.append(f"   [{label}] {path}")
    return lines
