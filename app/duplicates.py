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
            Total de bytes redundantes o 0 si el grupo es inválido o unitario.
        """
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def hash_file(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo. Retorna None si el archivo es inaccesible,
    está protegido por safety.py o su tamaño cambia durante la lectura.
    """
    if path is None or chunk_size <= 0: 
        return None
        
    try:
        file_path = Path(path).resolve()
        if not file_path.is_file() or is_protected_path(file_path):
            return None

        stat_initial = file_path.stat()
        if stat_initial.st_size <= 0 or (getattr(stat_initial, 'st_file_attributes', 0) & 0x400):
            return None
            
        digest = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                buffer = f.read(chunk_size)
                if not buffer:
                    break
                digest.update(buffer)
        
        # Validar consistencia tras lectura
        if file_path.stat().st_size != stat_initial.st_size:
            return None
            
        return digest.hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, RuntimeError, IOError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Hash rápido de los primeros bytes del archivo. Ignora reparse points.
    """
    if path is None or read_bytes <= 0: 
        return None
        
    try:
        file_path = Path(path).resolve()
        if not file_path.is_file() or is_protected_path(file_path):
            return None
            
        stat = file_path.stat()
        if getattr(stat, 'st_file_attributes', 0) & 0x400:
            return None
            
        with open(file_path, "rb") as f:
            content = f.read(read_bytes)
            return hashlib.sha256(content).hexdigest() if content else None
    except (OSError, PermissionError, ValueError, TypeError, RuntimeError, IOError):
        return None


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """
    Agrupa rutas por tamaño exacto en bytes, descartando rutas protegidas.
    """
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None: 
        return groups
        
    for p in paths:
        if not isinstance(p, Path): continue
        try:
            p_res = p.resolve()
            if p_res.is_file() and not is_protected_path(p_res):
                groups[p_res.stat().st_size].append(p_res)
        except (OSError, PermissionError, FileNotFoundError):
            continue
    return groups


def _collect_candidates(
    directories: Iterable[Union[str, Path]], 
    min_size: int, 
    skip_protected: bool
) -> Dict[int, List[Path]]:
    """
    Realiza un recorrido recursivo del sistema de archivos para identificar archivos candidatos.
    
    Usa un diccionario de inodos (dev, ino) para evitar ciclos en enlaces simbólicos o
    puntos de reparse. Filtra por tamaño mínimo y validaciones de seguridad de safety.py.
    
    Returns:
        Diccionario {tamaño_bytes: [lista_rutas]} conteniendo solo grupos de al menos 2 archivos.
    """
    temp_groups: Dict[int, List[Path]] = defaultdict(list)
    visited_inodes: Dict[Tuple[int, int], bool] = {}
    
    if directories is None: return temp_groups
    
    def _scan(root_path: Path) -> None:
        try:
            with os.scandir(root_path) as dir_iterator:
                for entry in dir_iterator:
                    if entry is None: continue
                    
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                        if getattr(entry_stat, 'st_file_attributes', 0) & 0x400:
                            continue
                        
                        path_obj = Path(entry.path)
                        if skip_protected and is_protected_path(path_obj):
                            continue
                        
                        if entry.is_dir(follow_symlinks=False):
                            key = (entry_stat.st_dev, entry_stat.st_ino)
                            if key not in visited_inodes:
                                visited_inodes[key] = True
                                _scan(path_obj)
                        
                        elif entry.is_file(follow_symlinks=False):
                            if entry_stat.st_size >= min_size:
                                temp_groups[entry_stat.st_size].append(path_obj)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            pass

    for directory in directories:
        if directory is None: continue
        try:
            p = Path(directory)
            if p.is_dir():
                _scan(p)
        except (OSError, PermissionError, ValueError, TypeError): continue
            
    return {size: paths for size, paths in temp_groups.items() if len(paths) > 1}


def _refine_by_hash(
    paths: Iterable[Path], 
    hash_func: Callable[[Path], Optional[str]]
) -> Dict[str, List[Path]]:
    """
    Reduce un grupo de rutas agrupándolas según el resultado de una función de hash.
    
    Aplica 'hash_func' a cada ruta y reagrupa por el hash resultante. Solo conserva
    entradas donde la colisión de hash implica al menos dos archivos.
    """
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    if paths is None: return groups_by_digest
    
    for path in paths:
        if not isinstance(path, Path): continue
        digest = hash_func(path)
        if digest:
            groups_by_digest[digest].append(path)
                
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def find_duplicates(
    directories: Iterable[Union[str, Path]],
    min_size: int = 1024,
    skip_protected: bool = True,
) -> List[DuplicateGroup]:
    """
    Ejecuta el pipeline jerárquico de detección de duplicados.
    
    Flujo:
    1. Escaneo inicial por tamaño.
    2. Refinamiento mediante hash parcial (primeros 64KB).
    3. Refinamiento final mediante hash SHA256 completo.
    
    Returns:
        Lista de objetos DuplicateGroup ordenados por bytes desperdiciados descendentes.
    """
    if directories is None or min_size < 0: return []
    
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    
    for size, paths_in_size_group in size_map.items():
        partial_groups = _refine_by_hash(paths_in_size_group, partial_hash)
        for partial_candidates in partial_groups.values():
            full_hash_groups = _refine_by_hash(partial_candidates, hash_file)
            for digest, confirmed_paths in full_hash_groups.items():
                groups.append(DuplicateGroup(digest, size, sorted(confirmed_paths)))

    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma total de espacio redundante en un conjunto de grupos."""
    if not isinstance(groups, (list, tuple)): return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """
    Selecciona la ruta de menor antigüedad y menor longitud de nombre como 'keeper'.
    """
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None

    keepers: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        if not isinstance(p, Path) or not p.exists():
            continue
        try:
            stat_info = p.stat()
            keepers.append((float(stat_info.st_mtime), len(str(p)), p))
        except (OSError, PermissionError, AttributeError):
            continue
            
    return min(keepers, key=lambda x: (x[0], x[1]))[2] if keepers else None


def format_group(group: DuplicateGroup) -> List[str]:
    """
    Genera representación textual de un grupo para la UI.
    """
    if not isinstance(group, DuplicateGroup): 
        return []
        
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    for path in group.paths:
        if not isinstance(path, Path): 
            continue
        marca = "conservar" if (keeper and path == keeper) else "duplicado"
        lines.append(f"   [{marca}] {path}")
    return lines
