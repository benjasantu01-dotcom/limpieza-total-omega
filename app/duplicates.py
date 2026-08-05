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
    Calcula el hash SHA256 completo del archivo mediante bloques de datos.
    Retorna None si el archivo es inaccesible, protegido o inválido.
    """
    if path is None or chunk_size <= 0: return None
    try:
        p = Path(path).resolve(strict=True)
        if is_protected_path(p) or not p.is_file() or p.is_symlink():
            return None
        
        if p.stat().st_size == 0: return None
            
        digest = hashlib.sha256()
        with open(p, "rb", buffering=chunk_size) as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk: break
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError, IsADirectoryError, RuntimeError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Calcula un hash SHA256 sobre los primeros N bytes de un archivo para comparación rápida.
    """
    if path is None or read_bytes <= 0: return None
    try:
        p = Path(path).resolve(strict=True)
        if is_protected_path(p) or not p.is_file() or p.is_symlink():
            return None
            
        if p.stat().st_size == 0: return None

        with open(p, "rb", buffering=read_bytes) as f:
            content = f.read(read_bytes)
            if not content:
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError, IsADirectoryError, RuntimeError):
        return None


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """
    Agrupa rutas de archivos basándose en su tamaño en bytes, filtrando accesos protegidos.
    """
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None: return groups
    for p in paths:
        if not isinstance(p, Path): continue
        try:
            resolved = p.resolve(strict=True)
            if is_protected_path(resolved) or resolved.is_symlink() or not resolved.is_file(): continue
            groups[resolved.stat().st_size].append(resolved)
        except (OSError, PermissionError, FileNotFoundError, RuntimeError):
            continue
    return groups


def _collect_candidates(directories: Iterable[Union[str, Path]], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """
    Realiza un recorrido recursivo del sistema de archivos para indexar candidatos por tamaño.
    """
    groups: Dict[int, List[Path]] = defaultdict(list)
    visited_inodes: set[Tuple[int, int]] = set()
    
    def _scan(root_path: Path) -> None:
        try:
            root_st = root_path.stat()
            visited_inodes.add((root_st.st_dev, root_st.st_ino))
        except (OSError, PermissionError):
            return

        try:
            with os.scandir(root_path) as it:
                for entry in it:
                    try:
                        # Seguridad: no seguir symlinks, ni siquiera de directorios, para evitar bucles.
                        if entry.is_symlink(): continue
                        
                        if entry.is_dir():
                            st = entry.stat()
                            inode_key = (st.st_dev, st.st_ino)
                            if inode_key not in visited_inodes:
                                _scan(Path(entry.path))
                        elif entry.is_file():
                            st = entry.stat()
                            if st.st_size < min_size: continue
                            
                            p = Path(entry.path)
                            if is_protected_path(p): continue
                            
                            inode_key = (st.st_dev, st.st_ino)
                            if inode_key in visited_inodes: continue
                            visited_inodes.add(inode_key)
                            
                            groups[st.st_size].append(p.resolve())
                    except (OSError, PermissionError, FileNotFoundError): continue
        except (OSError, PermissionError, FileNotFoundError): pass

    if directories is not None:
        for directory in directories:
            if directory is None: continue
            try:
                path_obj = Path(directory).resolve(strict=True)
                if path_obj.is_dir() and not is_protected_path(path_obj):
                    _scan(path_obj)
            except (OSError, PermissionError, RuntimeError): continue
    return groups


def _refine_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """
    Filtra una lista de archivos, agrupándolos según el resultado de una función de hash proporcionada.
    Solo retorna grupos que contengan más de un elemento.
    """
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    if paths is None: return {}
    for path in paths:
        if not isinstance(path, Path): continue
        digest = hash_func(path)
        if digest:
            groups_by_digest[digest].append(path)
    return {digest: paths for digest, paths in groups_by_digest.items() if len(paths) > 1}


def find_duplicates(
    directories: Iterable[Union[str, Path]],
    min_size: int = 1024,
    skip_protected: bool = True,
) -> List[DuplicateGroup]:
    """
    Ejecuta el pipeline de detección en tres etapas: Tamaño -> Hash Parcial -> Hash Completo.
    """
    if directories is None: return []
    size_map: Dict[int, List[Path]] = _collect_candidates(directories, min_size, skip_protected)
    potential_groups: List[Tuple[int, List[Path]]] = [(size, paths) for size, paths in size_map.items() if len(paths) > 1]
    
    if not potential_groups:
        return []

    groups: List[DuplicateGroup] = []
    
    for size, same_size_paths in potential_groups:
        # Etapa 2: Refinamiento por cabecera
        partial_map: Dict[str, List[Path]] = _refine_by_hash(same_size_paths, partial_hash)
        
        for partial_candidates in partial_map.values():
            # Etapa 3: Confirmación por hash completo
            full_map: Dict[str, List[Path]] = _refine_by_hash(partial_candidates, hash_file)
            
            for digest, confirmed_paths in full_map.items():
                groups.append(DuplicateGroup(
                    digest=digest, 
                    size_bytes=size, 
                    paths=sorted(confirmed_paths)
                ))

    # Ordenar grupos por mayor impacto de recuperación de espacio
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma el total de espacio en bytes que se recuperaría al eliminar redundancias."""
    if not groups: return 0
    return sum((g.wasted_bytes for g in groups), 0)


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """
    Heurística de selección: sugiere conservar el archivo más antiguo, y en caso de empate, el de ruta más corta.
    """
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None

    valid_paths: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        if not isinstance(p, Path): continue
        try:
            if p.exists() and p.is_file() and not is_protected_path(p):
                stat = p.stat()
                valid_paths.append((stat.st_mtime, len(str(p)), p))
        except (OSError, PermissionError, FileNotFoundError):
            continue
            
    if not valid_paths:
        return None

    # Conservar el más antiguo (menor mtime), luego el de ruta más corta
    return min(valid_paths, key=lambda x: (x[0], x[1]))[2]


def format_group(group: DuplicateGroup) -> List[str]:
    """
    Genera un informe textual legible de un grupo de duplicados.
    """
    if not isinstance(group, DuplicateGroup): return []
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    for path in group.paths:
        marca = "conservar" if keeper and path == keeper else "duplicado"
        lines.append(f"   [{marca}] {path}")
    return lines
