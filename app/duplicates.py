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
from typing import Iterable, Callable, Dict, List, Optional, Union, Tuple, Sequence, Set

from safety import is_protected_path

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
    """
    Calcula el hash SHA256 completo de un archivo mediante lectura en bloques.
    """
    if path is None: return None
    try:
        path_obj = Path(path).resolve()
        if not path_obj.is_file() or is_protected_path(path_obj) or not os.access(path_obj, os.R_OK):
            return None
        
        digest = hashlib.sha256()
        with open(path_obj, "rb") as f:
            while (buffer := f.read(chunk_size)):
                digest.update(buffer)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Calcula el hash SHA256 de los primeros N bytes para filtrado heurístico.
    """
    if path is None: return None
    try:
        path_obj = Path(path).resolve()
        if not path_obj.is_file() or is_protected_path(path_obj) or not os.access(path_obj, os.R_OK):
            return None
        
        with open(path_obj, "rb") as f:
            content = f.read(read_bytes)
            if not content: return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def _is_valid_candidate(path: Path) -> bool:
    """Valida que la ruta sea un archivo real, accesible y no protegido."""
    if not isinstance(path, Path): return False
    try:
        return path.is_file() and not is_protected_path(path)
    except (OSError, ValueError):
        return False


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """Clasifica rutas según su tamaño en bytes, filtrando rutas protegidas."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None: return groups
    
    for p in paths:
        if p is None: continue
        try:
            target = Path(p).resolve()
            if _is_valid_candidate(target):
                st = target.stat()
                if st.st_size > 0:
                    groups[st.st_size].append(target)
        except (OSError, PermissionError):
            continue
    return groups


def _resolve_and_verify_root(item: Union[str, Path]) -> Optional[Path]:
    """Valida y resuelve una ruta inicial de escaneo, retornando None si es inválida."""
    try:
        root = Path(item).resolve(strict=False)
        if root.is_dir() and not is_protected_path(root):
            return root
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def _collect_candidates(
    directories: Iterable[Union[str, Path]], 
    min_size: int, 
    skip_protected: bool
) -> Dict[int, List[Path]]:
    """
    Recorre recursivamente los directorios buscando candidatos a duplicados.
    Evita procesar el mismo inodo dos veces para prevenir ciclos.
    """
    temp_groups: Dict[int, List[Path]] = defaultdict(list)
    visited_device_inodes: Set[Tuple[int, int]] = set()
    processed_dirs: Set[Path] = set()
    processed_files: Set[Path] = set()

    def _should_skip(path: Path) -> bool:
        return skip_protected and is_protected_path(path)

    def _scan_recursive(current_dir: Path) -> None:
        try:
            resolved_dir = current_dir.resolve(strict=False)
            if resolved_dir in processed_dirs: return
            processed_dirs.add(resolved_dir)
            
            for entry in resolved_dir.iterdir():
                try:
                    if entry.is_symlink(): continue
                    if _should_skip(entry): continue
                    
                    if entry.is_dir():
                        stat = entry.stat()
                        dev_inode = (stat.st_dev, stat.st_ino)
                        if dev_inode not in visited_device_inodes:
                            visited_device_inodes.add(dev_inode)
                            _scan_recursive(entry)
                    elif entry.is_file():
                        stat = entry.stat()
                        if stat.st_size < min_size: continue
                        if entry in processed_files: continue
                        processed_files.add(entry)
                        temp_groups[int(stat.st_size)].append(entry)
                except (OSError, PermissionError): continue
        except (OSError, PermissionError, RuntimeError): pass

    if directories:
        unique_roots = {r for item in directories if (r := _resolve_and_verify_root(item))}
        for root in unique_roots:
            _scan_recursive(root)
    return {size: files for size, files in temp_groups.items() if len(files) > 1}


def _refine_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Agrupa archivos por colisiones de contenido usando la función de hash provista."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if path and (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _process_size_group(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Pipeline de verificación optimizado: solo calcula hash completo en grupos con colisiones parciales."""
    confirmed_groups: List[DuplicateGroup] = []
    valid_paths = [p for p in paths if _is_valid_candidate(p)]
    
    if size <= PARTIAL_READ_BYTES:
        results = _refine_by_hash(valid_paths, hash_file)
    else:
        partial_results = _refine_by_hash(valid_paths, partial_hash)
        results = {}
        for candidates in partial_results.values():
            if len(candidates) > 1:
                results.update(_refine_by_hash(candidates, hash_file))
            
    for digest, confirmed_paths in results.items():
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
    """
    Selecciona el archivo original a conservar usando fecha de modificación (más antiguo)
    y, en caso de empate, la longitud de la ruta (más corta).
    """
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
        
    candidates: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        try:
            p_obj = Path(p).resolve()
            if _is_valid_candidate(p_obj):
                stat_info = p_obj.stat()
                candidates.append((float(stat_info.st_mtime), len(str(p_obj)), p_obj))
        except (OSError, PermissionError, RuntimeError):
            continue
            
    if not candidates:
        return None
        
    return min(candidates, key=lambda x: (x[0], x[1]))[2]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una representación textual formateada de un grupo para reportes."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return []
        
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    
    for path in group.paths:
        try:
            p_obj = Path(path).resolve()
            if not _is_valid_candidate(p_obj):
                lines.append(f"   [inaccesible] {path}")
                continue
            label = 'conservar' if keeper is not None and p_obj == keeper else 'duplicado'
            lines.append(f"   [{label}] {path}")
        except (OSError, PermissionError, RuntimeError):
            lines.append(f"   [error] {path}")
    return lines
