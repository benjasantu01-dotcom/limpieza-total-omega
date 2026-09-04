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
import ctypes
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Callable, Dict, List, Optional, Union, Tuple, Set

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
# archivos distintos con alta probabilidad estadística y es una lectura 
# despreciable incluso en discos mecánicos lentos.
PARTIAL_READ_BYTES: int = 64 * 1024

# Constante para identificar puntos de reparse (Junctions/Symlinks) en Windows.
FILE_ATTRIBUTE_REPARSE_POINT: int = 0x400


def is_junction(path: Path) -> bool:
    """Verifica si una ruta es un punto de reparse mediante atributos de sistema."""
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError):
        return False


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
        """
        Calcula el espacio total que se liberaría borrando todos los duplicados,
        conservando únicamente una instancia (la elegida por suggest_keeper).
        """
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def hash_file(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del archivo mediante bloques de memoria constante.
    
    Args:
        path: Ruta al archivo.
        chunk_size: Bytes por bloque para lectura incremental.
    
    Returns:
        Hex del hash o None si el archivo es inaccesible.
    """
    if not path or chunk_size <= 0:
        return None
        
    try:
        path_obj = Path(path)
        if not path_obj.is_file() or is_protected_path(path_obj) or not os.access(path_obj, os.R_OK):
            return None
        
        digest = hashlib.sha256()
        with open(path_obj, "rb") as f:
            while True:
                buffer = f.read(chunk_size)
                if not buffer:
                    break
                digest.update(buffer)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Genera una huella rápida leyendo solo el inicio del archivo.
    
    Args:
        path: Ruta al archivo.
        read_bytes: Límite de bytes a leer.
        
    Returns:
        Hex del hash del fragmento o None si el archivo es inaccesible.
    """
    if not path or read_bytes <= 0:
        return None

    try:
        path_obj = Path(path)
        if not path_obj.is_file() or is_protected_path(path_obj) or not os.access(path_obj, os.R_OK):
            return None
        
        with open(path_obj, "rb") as f:
            content = f.read(read_bytes)
            if not content:
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def _is_valid_candidate(path: Path) -> bool:
    """Valida que la ruta sea un archivo real, legible y no protegido."""
    try:
        return (
            isinstance(path, Path) and 
            path.is_file() and 
            not is_protected_path(path) and 
            os.access(path, os.R_OK)
        )
    except (OSError, ValueError, TypeError):
        return False


def _is_candidate_file(entry: os.DirEntry, min_size: int) -> bool:
    """Verifica si una entrada de directorio es un archivo candidato para procesar."""
    try:
        return (
            entry.is_file(follow_symlinks=False) and
            not is_junction(Path(entry.path)) and
            entry.stat(follow_symlinks=False).st_size >= min_size and
            not is_protected_path(Path(entry.path))
        )
    except (OSError, PermissionError):
        return False


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """Agrupa rutas por su tamaño en bytes (Paso 1)."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None or not isinstance(paths, Iterable): return groups
    
    for p in paths:
        path_obj = Path(p)
        if _is_valid_candidate(path_obj):
            try:
                size = path_obj.stat().st_size
                if size > 0:
                    groups[size].append(path_obj)
            except (OSError, PermissionError):
                continue
    return groups


def _resolve_and_verify_root(item: Union[str, Path]) -> Optional[Path]:
    """Normaliza y valida que la ruta raíz sea un directorio procesable."""
    try:
        if not item: return None
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
    """Recorre directorios y retorna un mapa {tamaño: lista_de_rutas} optimizado."""
    temp_map: Dict[int, List[Path]] = defaultdict(list)
    visited_inodes: Set[Tuple[int, int]] = set()

    def _scan_recursive(current_dir: Path) -> None:
        try:
            stat_root = current_dir.stat()
            inode = (stat_root.st_dev, stat_root.st_ino)
            if inode in visited_inodes or is_junction(current_dir):
                return
            visited_inodes.add(inode)

            with os.scandir(current_dir) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        p_dir = Path(entry.path)
                        if not is_protected_path(p_dir) and not is_junction(p_dir):
                            _scan_recursive(p_dir)
                    elif _is_candidate_file(entry, min_size):
                        p = Path(entry.path)
                        temp_map[int(entry.stat(follow_symlinks=False).st_size)].append(p)
        except (OSError, PermissionError):
            pass

    if directories and isinstance(directories, Iterable):
        roots = {r for item in directories if item and (r := _resolve_and_verify_root(item))}
        for root in roots:
            _scan_recursive(root)
            
    return {size: files for size, files in temp_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica la función hash indicada y agrupa las rutas por su digest."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _refine_by_deep_hash(candidates: List[Path]) -> Dict[str, List[Path]]:
    """Refina los grupos iniciales mediante hashes completos (Paso 3)."""
    partial_results: Dict[str, List[Path]] = _group_paths_by_hash(candidates, partial_hash)
    final_groups: Dict[str, List[Path]] = {}
    
    for subset in partial_results.values():
        full_hash_groups = _group_paths_by_hash(subset, hash_file)
        final_groups.update(full_hash_groups)
        
    return final_groups


def _process_size_group(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona la estrategia de comparación según el tamaño del archivo."""
    if not isinstance(size, int) or size <= 0 or not paths or len(paths) < 2: 
        return []
    
    if size <= PARTIAL_READ_BYTES:
        results = _group_paths_by_hash(paths, partial_hash)
    else:
        results = _refine_by_deep_hash(paths)
            
    return [DuplicateGroup(digest, size, sorted(confirmed_paths)) for digest, confirmed_paths in results.items()]


def find_duplicates(directories: Iterable[Union[str, Path]], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquestador principal: escanea directorios y agrupa duplicados encontrados."""
    if not isinstance(directories, Iterable) or isinstance(directories, (str, Path)): 
        return []
    if not isinstance(min_size, int) or min_size < 0: 
        return []
        
    groups: List[DuplicateGroup] = []
    size_map = _collect_candidates(directories, min_size, skip_protected)
    for size, paths in size_map.items():
        groups.extend(_process_size_group(size, paths))
        
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Retorna la suma total de bytes recuperables de un conjunto de grupos."""
    if not groups or not isinstance(groups, (list, tuple)): return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """
    Selecciona el archivo candidato para conservar (heurística: más antiguo).
    En caso de empate en antigüedad, se prefiere la ruta más corta.
    """
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
        
    candidates: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        if not isinstance(p, Path): continue
        try:
            if not p.is_file(): continue
            stat_info = p.stat()
            candidates.append((float(stat_info.st_mtime), len(str(p)), p))
        except (OSError, PermissionError):
            continue
    
    return min(candidates, key=lambda x: (x[0], x[1]))[2] if candidates else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una representación en texto del grupo para su visualización en UI."""
    if not isinstance(group, DuplicateGroup) or not hasattr(group, 'paths') or group.paths is None:
        return []
        
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    
    for path in group.paths:
        if not isinstance(path, Path):
            continue
        if not path.exists():
            lines.append(f"   [desaparecido] {path}")
            continue
        elif not _is_valid_candidate(path):
            lines.append(f"   [inaccesible] {path}")
            continue
        
        label = 'conservar' if keeper is not None and path == keeper else 'duplicado'
        lines.append(f"   [{label}] {path}")
    return lines
