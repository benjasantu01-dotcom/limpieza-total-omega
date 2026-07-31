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
PARTIAL_READ_BYTES = 64 * 1024


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

    Args:
        path: Ruta del archivo a procesar.
        chunk_size: Tamaño del búfer de lectura en bytes.

    Returns:
        Hexdigest del hash completo o None si el archivo es inaccesible o protegido.
    """
    if not path:
        return None
    
    p = Path(path).resolve()
    if not p.is_file() or p.is_symlink() or is_protected_path(p):
        return None
        
    digest = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            while chunk := f.read(chunk_size):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError, IsADirectoryError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Calcula el hash SHA256 de un prefijo del archivo para comparación rápida.

    Args:
        path: Ruta del archivo.
        read_bytes: Cantidad de bytes a leer (def: PARTIAL_READ_BYTES).

    Returns:
        Hexdigest del hash parcial o None si el archivo es inaccesible.
    """
    if not path:
        return None

    p = Path(path).resolve()
    if not p.is_file() or p.is_symlink() or is_protected_path(p):
        return None

    try:
        with open(p, "rb") as f:
            content = f.read(read_bytes)
            if not content:
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError, IsADirectoryError):
        return None


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """
    Organiza rutas de archivos en un diccionario indexado por su tamaño en bytes.
    
    Usa el par (dispositivo, inodo) para identificar archivos únicos físicamente
    y omitir hardlinks redundantes que reportarían el mismo contenido.
    """
    if paths is None:
        return {}
    groups: Dict[int, List[Path]] = defaultdict(list)
    seen_inodes: set[Tuple[int, int]] = set()
    for p in paths:
        if not isinstance(p, Path):
            continue
        try:
            resolved_p = p.resolve()
            if is_protected_path(resolved_p):
                continue
            st = resolved_p.lstat()
            if st.st_size > 0:
                inode_id = (st.st_dev, st.st_ino)
                if inode_id in seen_inodes:
                    continue
                seen_inodes.add(inode_id)
                groups[st.st_size].append(resolved_p)
        except (OSError, PermissionError, FileNotFoundError, AttributeError):
            continue
    return groups


def _collect_candidates(directories: Iterable[Union[str, Path]], min_size: int, skip_protected: bool) -> List[Path]:
    """
    Explora directorios para identificar candidatos a duplicados filtrando enlaces y rutas seguras.
    """
    if directories is None:
        return []
    candidates: List[Path] = []
    
    for directory in directories:
        if not directory:
            continue
        try:
            base_dir = Path(directory).expanduser().resolve()
            if not base_dir.is_dir() or (skip_protected and is_protected_path(base_dir)):
                continue
            
            for root, subdirs, files in os.walk(base_dir):
                root_path = Path(root)
                subdirs[:] = [
                    d for d in subdirs 
                    if not (root_path / d).is_symlink()
                    and not (skip_protected and is_protected_path((root_path / d).resolve()))
                ]
                
                for name in files:
                    file_path = root_path / name
                    try:
                        if file_path.is_symlink():
                            continue
                        if skip_protected and is_protected_path(file_path.resolve()):
                            continue
                        st = file_path.lstat()
                        if st.st_size >= min_size:
                            candidates.append(file_path)
                    except (OSError, PermissionError, FileNotFoundError):
                        continue
        except (OSError, RuntimeError):
            continue
    return candidates


def _refine_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """
    Aplica una función de hash para subdividir una lista de rutas en grupos coincidentes.
    """
    if paths is None:
        return {}
    by_hash: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if path and (digest := hash_func(path)):
            by_hash[digest].append(path)
    return {h: p for h, p in by_hash.items() if len(p) > 1}


def find_duplicates(
    directories: Iterable[Union[str, Path]],
    min_size: int = 1024,
    skip_protected: bool = True,
) -> List[DuplicateGroup]:
    """
    Ejecuta el pipeline completo de detección de duplicados por tamaño y hash.
    """
    if not directories:
        return []

    candidates = _collect_candidates(directories, min_size, skip_protected)
    if not candidates:
        return []

    raw_groups: Dict[int, List[Path]] = group_by_size(candidates)
    size_map: Dict[int, List[Path]] = {s: p for s, p in raw_groups.items() if len(p) > 1}
    if not size_map:
        return []

    groups: List[DuplicateGroup] = []
    
    for size, same_size in size_map.items():
        by_partial: Dict[str, List[Path]] = _refine_by_hash(same_size, partial_hash)
        
        for partial_candidates in by_partial.values():
            by_full: Dict[str, List[Path]] = _refine_by_hash(partial_candidates, hash_file)
            
            for digest, confirmed in by_full.items():
                groups.append(DuplicateGroup(
                    digest=digest, 
                    size_bytes=size, 
                    paths=sorted(confirmed)
                ))

    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma el total de espacio en bytes que se recuperaría al eliminar redundancias."""
    return sum(g.wasted_bytes for g in groups) if groups else 0


def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """
    Determina la mejor ruta para conservar basada en antigüedad (mtime) y longitud de ruta.
    """
    if group is None or not group.paths:
        return None

    valid_paths: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        if not isinstance(p, Path):
            p = Path(p)
        if not p.exists() or not p.is_file():
            continue
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
    Genera un listado descriptivo de un grupo de duplicados para interfaces de usuario.
    """
    if not group or not group.paths:
        return []
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    for path in group.paths:
        marca = "conservar" if keeper and path == keeper else "duplicado"
        lines.append(f"   [{marca}] {path}")
    return lines
