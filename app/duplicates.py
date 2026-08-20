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
    
    Attributes:
        digest: Hash SHA256 calculado sobre el contenido total del archivo.
        size_bytes: Tamaño de cada archivo en el grupo en bytes.
        paths: Lista de objetos Path que contienen los archivos duplicados.
    """
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Retorna el número total de archivos en este grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """
        Calcula el espacio total recuperable excluyendo una copia (n-1).
        
        Returns:
            int: Total de bytes redundantes o 0 si el grupo es inválido.
        """
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def hash_file(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del archivo tras validar su seguridad.
    Verifica atributos del sistema para evitar errores de acceso en metadatos.
    """
    if path is None or chunk_size <= 0: 
        return None
        
    try:
        file_path = Path(path).resolve(strict=True)
        if not file_path.is_file() or is_protected_path(file_path) or not is_safe_to_modify(file_path):
            return None

        stat_initial = file_path.stat()
        # 0x400 es el atributo FILE_ATTRIBUTE_REPARSE_POINT (Junctions/Symlinks)
        if stat_initial.st_size <= 0 or (getattr(stat_initial, 'st_file_attributes', 0) & 0x400):
            return None
            
        digest = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                buffer = f.read(chunk_size)
                if not buffer:
                    break
                digest.update(buffer)
        
        return digest.hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, RuntimeError, IOError, AttributeError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Hash rápido de los primeros bytes para comparación heurística.
    Es una optimización para evitar leer archivos grandes que difieren al inicio.
    """
    if path is None or read_bytes <= 0: 
        return None
        
    try:
        file_path = Path(path).resolve(strict=True)
        if not file_path.is_file() or is_protected_path(file_path) or not is_safe_to_modify(file_path):
            return None
            
        with open(file_path, "rb") as f:
            content = f.read(read_bytes)
            if not content:
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, RuntimeError, IOError, AttributeError):
        return None


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """
    Clasifica una colección de rutas según su tamaño en bytes.
    Filtra symlinks y rutas protegidas antes del procesamiento.
    """
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None: 
        return groups
        
    for p in paths:
        try:
            if not p: continue
            target = Path(p).resolve(strict=True)
            if not target.is_file() or target.is_symlink(): continue
            if is_protected_path(target) or not is_safe_to_modify(target): continue
            groups[target.stat().st_size].append(target)
        except (OSError, PermissionError, FileNotFoundError, TypeError):
            continue
    return groups


def _collect_candidates(
    directories: Iterable[Union[str, Path]], 
    min_size: int, 
    skip_protected: bool
) -> Dict[int, List[Path]]:
    """
    Realiza un recorrido recursivo del sistema de archivos para agrupar archivos por tamaño.
    """
    temp_groups: Dict[int, List[Path]] = defaultdict(list)
    visited_inodes: set[Tuple[int, int]] = set()

    def _scan(root_path: Path) -> None:
        try:
            with os.scandir(root_path) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                        if getattr(st, 'st_file_attributes', 0) & 0x400:
                            continue
                            
                        if entry.is_dir(follow_symlinks=False):
                            inode: Tuple[int, int] = (st.st_dev, st.st_ino)
                            if inode not in visited_inodes:
                                visited_inodes.add(inode)
                                _scan(Path(entry.path))
                        elif entry.is_file() and st.st_size >= min_size:
                            temp_groups[st.st_size].append(Path(entry.path))
                    except (OSError, PermissionError): continue
        except (OSError, PermissionError): pass

    if directories is None: return {}
    for item in directories:
        try:
            if not item: continue
            path_item = Path(item).resolve(strict=True)
            if path_item.is_dir(): _scan(path_item)
        except (OSError, RuntimeError, ValueError, TypeError): continue
            
    final_groups = defaultdict(list)
    for size, paths in temp_groups.items():
        for p in paths:
            try:
                target = p.resolve(strict=True)
                if not skip_protected or (not is_protected_path(target) and is_safe_to_modify(target)):
                    final_groups[size].append(target)
            except (OSError, RuntimeError): continue
                
    return {size: paths for size, paths in final_groups.items() if len(paths) > 1}


def _refine_by_hash(
    paths: Iterable[Path], 
    hash_func: Callable[[Path], Optional[str]]
) -> Dict[str, List[Path]]:
    """
    Agrupa candidatos basándose en el resultado de una función de hash específica.
    """
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    
    for path in paths:
        try:
            target = path.resolve(strict=True)
            if not target.is_file(): continue
            digest = hash_func(target)
            if digest:
                groups_by_digest[digest].append(target)
        except (OSError, PermissionError, FileNotFoundError, TypeError):
            continue
                
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _process_size_group(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """
    Ejecuta el pipeline de refinamiento (Hash Parcial -> Hash Completo).
    """
    confirmed_groups: List[DuplicateGroup] = []
    partial_groups = _refine_by_hash(paths, partial_hash)
    for partial_candidates in partial_groups.values():
        if len(partial_candidates) < 2:
            continue
        full_hash_groups = _refine_by_hash(partial_candidates, hash_file)
        for digest, confirmed_paths in full_hash_groups.items():
            confirmed_groups.append(DuplicateGroup(digest, size, sorted(confirmed_paths)))
    return confirmed_groups


def find_duplicates(
    directories: Iterable[Union[str, Path]],
    min_size: int = 1024,
    skip_protected: bool = True,
) -> List[DuplicateGroup]:
    """
    Pipeline principal: filtra por tamaño -> hash parcial -> hash completo.
    """
    if directories is None or min_size < 0: return []
    
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    
    for size, paths_in_size_group in size_map.items():
        groups.extend(_process_size_group(size, paths_in_size_group))

    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma total de bytes redundantes en los grupos detectados."""
    if not groups: return 0
    return sum(g.wasted_bytes for g in groups)


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """
    Selecciona el 'archivo maestro' conservando el más antiguo (mtime).
    Ante igualdad, se prioriza la ruta más corta (menor profundidad).
    """
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None

    keepers: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        try:
            target = p.resolve(strict=True)
            if not target.is_file() or is_protected_path(target) or not is_safe_to_modify(target):
                continue
            stat_info = target.stat()
            mtime = float(getattr(stat_info, 'st_mtime', 0.0))
            keepers.append((mtime, len(str(target)), target))
        except (OSError, PermissionError, AttributeError, ValueError, FileNotFoundError, TypeError):
            continue
            
    return min(keepers, key=lambda x: (x[0], x[1]))[2] if keepers else None


def format_group(group: DuplicateGroup) -> List[str]:
    """
    Genera representación textual de un grupo para visualización en UI.
    """
    if not isinstance(group, DuplicateGroup) or not group.paths: 
        return []
        
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    for path in group.paths:
        try:
            target = path.resolve(strict=True)
            is_keeper = (keeper is not None and target == keeper)
        except (OSError, PermissionError, FileNotFoundError, TypeError):
            is_keeper = False
        marca = "conservar" if is_keeper else "duplicado"
        lines.append(f"   [{marca}] {path}")
    return lines
