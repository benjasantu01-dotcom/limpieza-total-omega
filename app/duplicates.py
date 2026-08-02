"""
duplicates.py — detección de archivos duplicados.

SOLO LECTURA: este módulo encuentra y agrupa duplicados, y sugiere cuál
conservar, pero **nunca borra ni mueve nada**. La decisión de qué hacer con
un duplicado queda en la interfaz, y pasa por la cuarentena o por la
carpeta de revisión, nunca por un borrado directo.

Estrategia en tres pasos, de lo barato a lo caro, para no leer gigas al
vacío:
  1. Agrupar por tamaño exacto (dos archivos de distinto tamaño no pueden
     ser iguales; esto descarta la enorme mayoría sin leer nada).
  2. Dentro de cada grupo, hash de los primeros KB (descarta casi todo el
     resto leyendo muy poco).
  3. Solo a los que siguen coincidiendo, hash completo para confirmar.
"""

from __future__ import annotations
import hashlib
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Callable, Dict, List, Optional, Union, Tuple, Sequence

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
            Total de bytes redundantes o 0 si no hay duplicados.
        """
        if not self.paths or self.count <= 1:
            return 0
        return (self.count - 1) * max(0, self.size_bytes)


def hash_file(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del archivo mediante bloques.
    """
    try:
        if path is None: return None
        p = Path(path).resolve()
        if not p.exists() or not p.is_file() or p.is_symlink() or is_protected_path(p):
            return None
            
        digest = hashlib.sha256()
        with open(p, "rb", buffering=chunk_size) as f:
            while chunk := f.read(chunk_size):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError, IsADirectoryError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Calcula el hash SHA256 de un prefijo del archivo para comparación rápida.
    """
    try:
        if path is None: return None
        p = Path(path).resolve()
        if not p.exists() or not p.is_file() or p.is_symlink() or is_protected_path(p):
            return None

        with open(p, "rb", buffering=read_bytes) as f:
            content = f.read(read_bytes)
            if not content:
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError, IsADirectoryError):
        return None


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """
    Organiza rutas de archivos en un diccionario indexado por su tamaño en bytes.
    """
    groups: Dict[int, List[Path]] = defaultdict(list)
    seen_inodes: set[Tuple[int, int]] = set()
    for p in paths:
        if p is None: continue
        try:
            p_res = Path(p).resolve()
            if not p_res.exists() or not p_res.is_file() or p_res.is_symlink() or is_protected_path(p_res):
                continue
            
            st = p_res.stat()
            if st.st_size <= 0:
                continue
            
            inode_id = (st.st_dev, st.st_ino)
            if inode_id in seen_inodes:
                continue
                
            seen_inodes.add(inode_id)
            groups[st.st_size].append(p_res)
        except (OSError, PermissionError, FileNotFoundError, AttributeError):
            continue
    return groups


def _collect_candidates(directories: Iterable[Union[str, Path]], min_size: int, skip_protected: bool) -> List[Path]:
    """
    Explora directorios recursivamente para identificar archivos candidatos.
    """
    candidates: List[Path] = []
    visited_inodes: set[Tuple[int, int]] = set()
    
    def _scan(root_path: Path):
        try:
            with os.scandir(root_path) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        
                        resolved_path = Path(entry.path).resolve()
                        if not resolved_path.exists():
                            continue
                        if skip_protected and is_protected_path(resolved_path):
                            continue
                        
                        if entry.is_dir():
                            _scan(resolved_path)
                        elif entry.is_file():
                            st = entry.stat()
                            inode_id = (st.st_dev, st.st_ino)
                            if inode_id in visited_inodes:
                                continue
                            if st.st_size >= min_size:
                                visited_inodes.add(inode_id)
                                candidates.append(resolved_path)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            pass

    for directory in directories:
        if directory is None: continue
        path_obj = Path(directory).resolve()
        if path_obj.exists() and path_obj.is_dir():
            if not is_protected_path(path_obj):
                _scan(path_obj)
    return candidates


def _refine_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """
    Subdivide una lista de archivos en grupos usando la función de hash provista.
    """
    by_hash: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        digest = hash_func(path)
        if digest:
            by_hash[digest].append(path)
    return {digest: paths_list for digest, paths_list in by_hash.items() if len(paths_list) > 1}


def find_duplicates(
    directories: Iterable[Union[str, Path]],
    min_size: int = 1024,
    skip_protected: bool = True,
) -> List[DuplicateGroup]:
    """
    Ejecuta el pipeline completo de detección.
    """
    candidates = _collect_candidates(directories, min_size, skip_protected)
    if not candidates:
        return []

    size_map = group_by_size(candidates)
    potential_groups = [paths for paths in size_map.values() if len(paths) > 1]
    
    if not potential_groups:
        return []

    groups: List[DuplicateGroup] = []
    
    for same_size_paths in potential_groups:
        partial_map = _refine_by_hash(same_size_paths, partial_hash)
        
        for partial_candidates in partial_map.values():
            full_map = _refine_by_hash(partial_candidates, hash_file)
            
            for digest, confirmed_paths in full_map.items():
                if not confirmed_paths: continue
                try:
                    size = confirmed_paths[0].stat().st_size
                    groups.append(DuplicateGroup(
                        digest=digest, 
                        size_bytes=size, 
                        paths=sorted(confirmed_paths)
                    ))
                except (OSError, FileNotFoundError, PermissionError):
                    continue

    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma el total de espacio en bytes que se recuperaría al eliminar redundancias."""
    if not groups: return 0
    return sum(g.wasted_bytes for g in groups)


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """
    Determina la mejor ruta para conservar basada en antigüedad y longitud de ruta.
    """
    if group is None or not group.paths:
        return None

    valid_paths: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        if p is None: continue
        try:
            stat = p.stat()
            valid_paths.append((stat.st_mtime, len(str(p)), p))
        except (OSError, PermissionError):
            continue
            
    if not valid_paths:
        return group.paths[0] if group.paths else None

    return min(valid_paths, key=lambda x: (x[0], x[1]))[2]


def format_group(group: DuplicateGroup) -> List[str]:
    """
    Genera un listado descriptivo de un grupo de duplicados.
    """
    if group is None: return []
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    for path in group.paths:
        marca = "conservar" if keeper and path == keeper else "duplicado"
        lines.append(f"   [{marca}] {path}")
    return lines
