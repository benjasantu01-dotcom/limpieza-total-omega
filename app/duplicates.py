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
from typing import Iterable, Callable, Dict, List, Optional, Union

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
    paths: list[Path]

    @property
    def count(self) -> int:
        """Número de copias encontradas del archivo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """
        Calcula el espacio total que podría liberarse si se conservara
        solo una copia del archivo (n-1 copias).
        """
        if not self.paths or self.count <= 1:
            return 0
        return (self.count - 1) * max(0, self.size_bytes)


def hash_file(path: Union[str, os.PathLike], chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo de un archivo mediante lectura en bloques.
    """
    if path is None or is_protected_path(path):
        return None
    
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError, IsADirectoryError):
        return None


def partial_hash(path: Union[str, os.PathLike], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Calcula un hash rápido de los primeros N bytes de un archivo.
    """
    if path is None or is_protected_path(path):
        return None
    
    try:
        with open(path, "rb") as f:
            content = f.read(read_bytes)
            if not content:
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError, IsADirectoryError):
        return None


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """
    Clasifica rutas de archivos según su tamaño en disco (st_size).
    """
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None:
        return groups
        
    for p in paths:
        try:
            stat = p.lstat()
            if stat.st_size > 0:
                groups[stat.st_size].append(p)
        except (OSError, PermissionError, FileNotFoundError, AttributeError):
            continue
    return groups


def _collect_candidates(directories: Iterable[Union[str, Path]], min_size: int, skip_protected: bool) -> List[Path]:
    """
    Realiza un recorrido recursivo del sistema de archivos para recolectar candidatos.
    """
    if directories is None:
        return []
    candidates: List[Path] = []
    
    for directory in directories:
        if not directory:
            continue
        try:
            base = Path(directory).expanduser()
            if not base.is_dir() or (skip_protected and is_protected_path(base)):
                continue
            
            for root, subdirs, files in os.walk(base):
                root_path = Path(root)
                subdirs[:] = [
                    d for d in subdirs 
                    if not (root_path / d).is_symlink() and not is_protected_path(root_path / d)
                ]
                
                for name in files:
                    candidate = root_path / name
                    try:
                        if candidate.is_symlink():
                            continue
                        if skip_protected and is_protected_path(candidate):
                            continue
                        st = candidate.stat()
                        if st.st_size >= min_size and os.path.isfile(candidate):
                            candidates.append(candidate)
                    except (OSError, PermissionError, FileNotFoundError):
                        continue
        except (OSError, RuntimeError, FileNotFoundError):
            continue
    return candidates


def _refine_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """
    Refina una lista de archivos agrupándolos por el resultado de una función de hash.
    """
    if paths is None:
        return {}
    by_hash: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if digest := hash_func(path):
            by_hash[digest].append(path)
    return {h: p for h, p in by_hash.items() if len(p) > 1}


def find_duplicates(
    directories: Iterable[Union[str, Path]],
    min_size: int = 1024,
    skip_protected: bool = True,
) -> List[DuplicateGroup]:
    """
    Pipeline de detección de duplicados en tres etapas.
    """
    if not directories:
        return []
        
    candidates = _collect_candidates(directories, min_size, skip_protected)
    if not candidates:
        return []

    groups: List[DuplicateGroup] = []
    size_map = {s: p for s, p in group_by_size(candidates).items() if len(p) > 1}
    
    for size, same_size in size_map.items():
        by_partial = _refine_by_hash(same_size, partial_hash)
        
        for partial_candidates in by_partial.values():
            by_full = _refine_by_hash(partial_candidates, hash_file)
            
            for digest, confirmed in by_full.items():
                groups.append(DuplicateGroup(
                    digest=digest, 
                    size_bytes=size, 
                    paths=sorted(confirmed)
                ))

    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: List[DuplicateGroup]) -> int:
    """Calcula la suma total de espacio desperdiciado por todos los duplicados."""
    if not groups:
        return 0
    return sum(g.wasted_bytes for g in groups)


def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """
    Selecciona el mejor candidato a conservar mediante heurística.
    """
    if not group or not group.paths:
        return None

    valid_paths: List[tuple[float, int, Path]] = []
    for p in group.paths:
        try:
            mtime = p.stat().st_mtime
            valid_paths.append((mtime, len(str(p)), p))
        except (OSError, PermissionError, FileNotFoundError):
            continue
            
    if not valid_paths:
        return group.paths[0]

    best = min(valid_paths, key=lambda x: (x[0], x[1]))
    return best[2]


def format_group(group: DuplicateGroup) -> List[str]:
    """Prepara una representación en texto del grupo para visualización en UI."""
    if not group or not group.paths:
        return []
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    for path in group.paths:
        marca = "conservar" if path == keeper else "duplicado"
        lines.append(f"   [{marca}] {path}")
    return lines
