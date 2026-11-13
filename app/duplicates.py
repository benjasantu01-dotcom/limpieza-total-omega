"""
duplicates.py — detección de archivos duplicados.

SOLO LECTURA: este módulo encuentra y agrupa duplicados, y sugiere cuál
conservar, pero **nunca borra ni mueve nada**.

Estrategia en tres pasos para detección de duplicados:
| Paso | Técnica | Propósito | Complejidad |
| :--- | :--- | :--- | :--- |
| 1 | Tamaño (stat) | Descarta archivos únicos rápidamente. | O(N) |
| 2 | Hash Parcial | Filtra falsos positivos (64KB iniciales). | O(M) |
| 3 | Hash Completo | Confirmación final mediante SHA256. | O(K) |

REGLA DE ORO: Toda operación de acceso a disco debe pasar por `is_safe_to_modify`
o `is_protected_path` para garantizar que no se interactúe con zonas críticas.
"""

from __future__ import annotations
import hashlib
import os
import ctypes
from collections import defaultdict, deque
from collections.abc import Sequence, Iterable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union, TypeAlias, Tuple

from safety import is_protected_path, is_safe_to_modify

PathLike: TypeAlias = Union[str, Path]

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

# Configuración de lectura y constantes de la Win32 API para atributos de archivo
PARTIAL_READ_BYTES: int = 64 * 1024

class Win32Attributes:
    REPARSE_POINT: int = 0x400  
    HIDDEN: int = 0x2          
    SYSTEM: int = 0x4          

MAX_RECURSION_DEPTH: int = 100

# Cachés para evitar re-procesamiento durante una única corrida de análisis
_partial_cache: Dict[Path, str] = {}
_full_hash_cache: Dict[Path, str] = {}

def is_junction(path: Path) -> bool:
    """Verifica mediante GetFileAttributesW si el path apunta a un Junction o Symlink."""
    if not isinstance(path, Path): return True
    with suppress(AttributeError, OSError, RuntimeError):
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & Win32Attributes.REPARSE_POINT))
    return False


def is_system_or_hidden(path: Path) -> bool:
    """Detecta atributos de archivo (Oculto/Sistema) en Windows que impiden la limpieza."""
    if not isinstance(path, Path): return False
    with suppress(AttributeError, OSError, RuntimeError):
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & (Win32Attributes.HIDDEN | Win32Attributes.SYSTEM)))
    return False


@dataclass
class DuplicateGroup:
    """Representa un conjunto de rutas de archivos idénticos y métricas de espacio."""
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Cantidad total de archivos en este grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """Espacio liberable: (N-1) copias ocupan espacio innecesario."""
        return (self.count - 1) * self.size_bytes if self.count > 1 else 0


def _safe_path_check(path: Path) -> bool:
    """
    Filtra una ruta validando seguridad y permisos de acceso.
    
    Args:
        path: Ruta a verificar.
        
    Returns:
        True si la ruta existe, es segura para leer y no es un punto de reparse.
    """
    if not isinstance(path, Path): return False
    try:
        if not path.exists(): return False
        if not is_safe_to_modify(path): return False
        if is_junction(path): return False
        abs_path = path.resolve(strict=True)
        return not is_protected_path(abs_path)
    except (OSError, RuntimeError, ValueError):
        return False


def _is_valid_candidate(path: Path, st_size: int = -1) -> bool:
    """Valida si un archivo es candidato para comparación de duplicados."""
    if not isinstance(path, Path) or not _safe_path_check(path): return False
    effective_size = st_size
    if effective_size == -1:
        with suppress(OSError):
            effective_size = path.stat(follow_symlinks=False).st_size
    return effective_size > 0 and not is_system_or_hidden(path)


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo de un archivo.
    
    Args:
        path: Ruta del archivo.
        chunk_size: Tamaño del buffer para lectura.
        
    Returns:
        Hexdigest SHA256 si la lectura es exitosa, None en caso contrario.
    """
    if not isinstance(path, Path) or not path.is_file(): return None
    if path in _full_hash_cache: return _full_hash_cache.get(path)
    if not _safe_path_check(path): return None
    
    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while (chunk := f.read(chunk_size)):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        _full_hash_cache[path] = digest
        return digest
    except (OSError, PermissionError, IOError, ValueError):
        return None


def partial_hash(path: Path, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Calcula un hash SHA256 sobre los bytes iniciales para identificación rápida.
    
    Args:
        path: Ruta del archivo.
        read_bytes: Cantidad de bytes a leer desde el inicio.
        
    Returns:
        Hexdigest SHA256 del fragmento o None si falla.
    """
    if not isinstance(path, Path) or not path.is_file(): return None
    if path in _partial_cache: return _partial_cache.get(path)
    if not _safe_path_check(path): return None
    
    try:
        with open(path, "rb") as f:
            header = f.read(read_bytes)
            if not header: return None
            digest_val = hashlib.sha256(header).hexdigest()
            _partial_cache[path] = digest_val
            return digest_val
    except (OSError, PermissionError, IOError, ValueError):
        return None


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa archivos por tamaño en bytes."""
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    if not isinstance(paths, Iterable): return {}
    
    for p in paths:
        if p is None: continue
        try:
            item = Path(p)
            if _safe_path_check(item):
                st = item.stat(follow_symlinks=False)
                if _is_valid_candidate(item, st.st_size):
                    groups_by_size[st.st_size].append(item)
        except (OSError, PermissionError, TypeError): 
            continue
    return dict(groups_by_size)


def _process_entry(
    entry: os.DirEntry, 
    depth: int, 
    min_size: int, 
    skip_protected: bool, 
    queue: deque[Tuple[Path, int]], 
    visited_inodes: set[Tuple[int, int]], 
    size_map: Dict[int, List[Path]]
) -> None:
    """Evalúa una entrada de directorio para el recorrido BFS, filtrando por inodos visitados."""
    try:
        if entry.is_symlink(): return
        entry_path = Path(entry.path)
        if is_junction(entry_path): return
        if not _safe_path_check(entry_path): return
        
        stat = entry.stat(follow_symlinks=False)
        
        if entry.is_dir():
            if depth < MAX_RECURSION_DEPTH:
                queue.append((entry_path, depth + 1))
        elif entry.is_file():
            if stat.st_size >= min_size and not is_system_or_hidden(entry_path):
                if skip_protected and is_protected_path(entry_path.resolve()): return
                inode_id = (stat.st_dev, stat.st_ino)
                if inode_id not in visited_inodes:
                    visited_inodes.add(inode_id)
                    size_map[stat.st_size].append(entry_path)
    except (OSError, PermissionError, ValueError, AttributeError):
        return


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Realiza un recorrido BFS de archivos para agrupar por tamaño."""
    if directories is None or not isinstance(directories, Iterable) or min_size < 0: 
        return {}
    
    size_map: Dict[int, List[Path]] = defaultdict(list)
    visited_inodes: set[Tuple[int, int]] = set()
    visited_paths: set[str] = set()
    queue: deque[Tuple[Path, int]] = deque()
    
    for d in directories:
        if d is None: continue
        try:
            p_dir = Path(d).resolve(strict=True)
            if p_dir.is_dir() and _safe_path_check(p_dir):
                queue.append((p_dir, 0))
        except (OSError, PermissionError, TypeError, ValueError):
            continue
    
    while queue:
        current_dir, depth = queue.popleft()
        str_path = str(current_dir)
        if str_path in visited_paths: continue
        visited_paths.add(str_path)

        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    _process_entry(entry, depth, min_size, skip_protected, queue, visited_inodes, size_map)
        except (OSError, PermissionError):
            continue
    return {sz: ps for sz, ps in size_map.items() if len(ps) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica una función de hash a una lista de rutas y agrupa por resultado."""
    if not isinstance(paths, Iterable): return {}
    groups: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if isinstance(path, Path):
            digest = hash_func(path)
            if digest:
                groups[digest].append(path)
    return {d: p for d, p in groups.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Refina duplicados usando hashing en cascada (parcial primero, luego completo)."""
    if not paths or not isinstance(paths, list): return {}
    partial_groups: Dict[str, List[Path]] = _group_paths_by_hash(paths, partial_hash)
    final: Dict[str, List[Path]] = {}
    for subset in partial_groups.values():
        final.update(_group_paths_by_hash(subset, hash_file))
    return final


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona la estrategia de hash eficiente basada en el tamaño del archivo."""
    if not paths or not isinstance(paths, list): return []
    if size <= PARTIAL_READ_BYTES:
        results = _group_paths_by_hash(paths, hash_file)
    else:
        results = _process_large_file_subset(paths)
    return [DuplicateGroup(d, size, sorted(p)) for d, p in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """
    Punto de entrada: analiza directorios en busca de duplicados.
    
    Args:
        directories: Lista de rutas a escanear.
        min_size: Tamaño mínimo para considerar un archivo.
        skip_protected: Si se deben ignorar rutas protegidas del sistema.
        
    Returns:
        Lista de objetos DuplicateGroup ordenados por espacio desperdiciado.
    """
    if directories is None or not isinstance(directories, Iterable): return []
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups = [g for sz, ps in size_map.items() for g in _decide_hash_strategy_and_process(sz, ps)]
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma total de bytes recuperables en una lista de grupos."""
    if not isinstance(groups, Sequence): return 0
    return sum(g.wasted_bytes for g in groups)


def _get_mtime(path: Path) -> Optional[float]:
    """Obtiene el timestamp de modificación de un archivo de forma segura."""
    if not isinstance(path, Path): return None
    try:
        return path.stat(follow_symlinks=False).st_mtime
    except (OSError, PermissionError):
        return None


def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """
    Heurística de selección: sugiere conservar el archivo más antiguo
    como el 'maestro' en un grupo de duplicados.
    """
    if not isinstance(group, DuplicateGroup) or not group.paths: return None
    candidates = [(m, p) for p in group.paths if (m := _get_mtime(p)) is not None]
    if not candidates: return group.paths[0]
    return min(candidates, key=lambda x: x[0])[1]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una representación textual formateada para la UI del grupo de duplicados."""
    if not isinstance(group, DuplicateGroup) or not group.paths: return []
    keeper = suggest_keeper(group)
    mb, wasted = group.size_bytes / 1048576, group.wasted_bytes / 1048576
    lines = [f"{group.count} copias de {mb:.2f} MB (recuperable: {wasted:.2f} MB)"]
    for p in group.paths:
        status = "[conservar]" if (p == keeper) else "[duplicado]"
        lines.append(f"   {status} {p}")
    return lines
