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
    if path is None or chunk_size <= 0: 
        return None
        
    try:
        file_path = Path(path)
        if not file_path.is_file() or is_protected_path(file_path):
            return None
        
        st = file_path.stat()
        if st.st_size <= 0:
            return None
            
        digest = hashlib.sha256()
        with open(file_path, "rb", buffering=0) as f:
            buffer = bytearray(chunk_size)
            mv = memoryview(buffer)
            while True:
                n = f.readinto(mv)
                if n == 0:
                    break
                digest.update(mv[:n])
        return digest.hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, RuntimeError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Calcula un hash SHA256 sobre los primeros N bytes para comparación rápida.
    """
    if path is None or read_bytes <= 0: 
        return None
        
    try:
        file_path = Path(path)
        if not file_path.is_file() or is_protected_path(file_path):
            return None

        st = file_path.stat()
        if st.st_size <= 0:
            return None

        with open(file_path, "rb") as f:
            content = f.read(read_bytes)
            if not content:
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, RuntimeError):
        return None


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """
    Agrupa rutas de archivos por su tamaño, filtrando accesos protegidos.
    """
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None: 
        return groups
        
    for p in paths:
        if not isinstance(p, Path): continue
        try:
            if p.is_file() and not is_protected_path(p):
                st = p.stat()
                groups[st.st_size].append(p)
        except (OSError, PermissionError, RuntimeError):
            continue
    return groups


def _collect_candidates(
    directories: Iterable[Union[str, Path]], 
    min_size: int, 
    skip_protected: bool
) -> Dict[int, List[Path]]:
    """
    Escaneo recursivo para indexar archivos por tamaño usando inodos para evitar ciclos.
    
    Ignora reparse points (Junctions) mediante chequeo de atributos de archivo y 
    valida cada ruta contra `is_protected_path` si skip_protected está activo.
    """
    temp_groups: Dict[int, List[Path]] = defaultdict(list)
    visited_inodes: Dict[int, set[int]] = defaultdict(set)
    
    if directories is None: return temp_groups
    
    def _scan(root_path: Path) -> None:
        try:
            with os.scandir(root_path) as dir_iterator:
                for entry in dir_iterator:
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                        
                        # 0x400: FILE_ATTRIBUTE_REPARSE_POINT. No seguir junctions/links.
                        if getattr(entry_stat, 'st_file_attributes', 0) & 0x400:
                            continue
                            
                        if entry.is_dir(follow_symlinks=False):
                            # Evitar ciclos de directorio mediante inodos.
                            if entry_stat.st_ino not in visited_inodes[entry_stat.st_dev]:
                                visited_inodes[entry_stat.st_dev].add(entry_stat.st_ino)
                                if not (skip_protected and is_protected_path(Path(entry.path))):
                                    _scan(Path(entry.path))
                        
                        elif entry.is_file(follow_symlinks=False):
                            if entry_stat.st_size >= min_size:
                                path_obj = Path(entry.path)
                                if not (skip_protected and is_protected_path(path_obj)):
                                    temp_groups[entry_stat.st_size].append(path_obj)
                    except (OSError, PermissionError): continue
        except (OSError, PermissionError): pass

    for directory in directories:
        if directory is None: continue
        path_obj = Path(directory)
        if path_obj.is_dir() and not (skip_protected and is_protected_path(path_obj)):
            _scan(path_obj)
            
    return {size: paths for size, paths in temp_groups.items() if len(paths) > 1}


def _refine_by_hash(
    paths: Iterable[Path], 
    hash_func: Callable[[Path], Optional[str]]
) -> Dict[str, List[Path]]:
    """
    Refina un grupo de archivos candidatos aplicando una función de hash.
    La función hash_func realiza sus propias verificaciones de seguridad internas.
    """
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    if paths is None: return groups_by_digest
    
    for path in paths:
        if path is None or not path.exists() or is_protected_path(path): continue
        if digest := hash_func(path):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def find_duplicates(
    directories: Iterable[Union[str, Path]],
    min_size: int = 1024,
    skip_protected: bool = True,
) -> List[DuplicateGroup]:
    """
    Ejecuta el pipeline de detección de duplicados en tres etapas:
    1. Agrupación por tamaño (collect_candidates).
    2. Refinamiento por hash parcial (64KB).
    3. Confirmación final por hash completo.
    """
    if directories is None: return []
    
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
    """Suma el total de espacio en bytes recuperable de todos los grupos."""
    if not groups: return 0
    return sum(g.wasted_bytes for g in groups)


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """
    Selecciona el archivo candidato para conservar basado en la fecha de modificación
    más antigua (o menor longitud de ruta en caso de empate).
    """
    if not group or not group.paths:
        return None

    keepers: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        if p is None: continue
        try:
            if p.exists() and not is_protected_path(p):
                stat_info = p.stat()
                keepers.append((float(stat_info.st_mtime), len(str(p)), p))
        except (OSError, PermissionError, AttributeError):
            continue
            
    if not keepers:
        return None
        
    return min(keepers, key=lambda x: (x[0], x[1]))[2]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera un reporte legible de un grupo de archivos duplicados."""
    if not isinstance(group, DuplicateGroup): return []
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    for path in group.paths:
        marca = "conservar" if (keeper and path == keeper) else "duplicado"
        lines.append(f"   [{marca}] {path}")
    return lines
