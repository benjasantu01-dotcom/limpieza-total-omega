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
import logging
from collections import defaultdict, deque
from collections.abc import Sequence, Iterable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Optional, Union, TypeAlias, Tuple, Set

from safety import is_protected_path, is_safe_to_modify

PathLike: TypeAlias = Union[str, Path]
InodeMap: TypeAlias = Set[Tuple[int, int]]

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

PARTIAL_READ_BYTES: int = 64 * 1024
logger = logging.getLogger(__name__)

class HashStrategy(Enum):
    COMPLETE = auto()
    CASCADE = auto()

class Win32Attributes:
    REPARSE_POINT: int = 0x400  
    HIDDEN: int = 0x2          
    SYSTEM: int = 0x4          

MAX_RECURSION_DEPTH: int = 100
MAX_PATH_LENGTH: int = 260

_partial_cache: Dict[Path, str] = {}
_full_hash_cache: Dict[Path, str] = {}

def is_junction(path: Path) -> bool:
    """Verifica si la ruta es un punto de unión de NTFS (reparse point) usando la API de Windows."""
    with suppress(AttributeError, OSError, RuntimeError):
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & Win32Attributes.REPARSE_POINT))
    return False

def is_system_or_hidden(path: Path) -> bool:
    """Verifica si la ruta tiene los atributos de sistema o archivo oculto configurados."""
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & (Win32Attributes.HIDDEN | Win32Attributes.SYSTEM)))
    except (AttributeError, OSError, RuntimeError):
        return False

def is_file_locked(path: Path) -> bool:
    """Intenta abrir un archivo en modo lectura para verificar si está siendo bloqueado por otro proceso."""
    try:
        with open(path, 'rb') as f:
            f.peek(1)
            return False
    except (PermissionError, OSError, FileNotFoundError, ValueError):
        return True

@dataclass
class DuplicateGroup:
    """Representa una colección de archivos idénticos basada en su firma hash."""
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        return len(self.paths)

    @property
    def wasted_bytes(self) -> int:
        """Calcula el espacio total que podría recuperarse conservando solo una instancia."""
        return (self.count - 1) * self.size_bytes if self.count > 1 else 0

def _safe_path_check(path: Path) -> bool:
    """Valida que la ruta sea segura, no exceda límites de Windows y no sea crítica."""
    try:
        if not path or len(str(path)) >= MAX_PATH_LENGTH: 
            return False
        path.name.encode('utf-8')
        return is_safe_to_modify(path) and not is_protected_path(path) and not is_junction(path)
    except (Exception, UnicodeEncodeError):
        return False

def _is_valid_candidate(path: Path) -> bool:
    """Filtra archivos inválidos, protegidos o con atributos de sistema antes de procesar."""
    return not is_system_or_hidden(path) and _safe_path_check(path)

def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Genera el hash SHA256 completo de un archivo usando un caché persistente en memoria."""
    if not isinstance(path, Path): return None
    if path in _full_hash_cache: return _full_hash_cache[path]
    if not path.is_file() or not _is_valid_candidate(path) or is_file_locked(path): 
        return None
    
    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while (chunk := f.read(chunk_size)):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        _full_hash_cache[path] = digest
        return digest
    except (OSError, PermissionError, IOError) as e:
        logger.debug("Error procesando hash de %s: %s", path, e)
        return None

def partial_hash(path: Path, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Genera un hash rápido de los bytes iniciales (64KB) para descartar archivos únicos."""
    if not isinstance(path, Path): return None
    if path in _partial_cache: return _partial_cache[path]
    if not path.is_file() or not _is_valid_candidate(path) or is_file_locked(path): 
        return None
    
    try:
        with open(path, "rb") as f:
            header = f.read(read_bytes)
            if not header: return None
            digest_val = hashlib.sha256(header).hexdigest()
            _partial_cache[path] = digest_val
            return digest_val
    except (OSError, PermissionError, IOError) as e:
        logger.debug("Error procesando hash parcial de %s: %s", path, e)
        return None

def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Crea un mapa de tamaño a lista de rutas, filtrando archivos inaccesibles o protegidos."""
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if not p: continue
        try:
            item = Path(p)
            if _is_valid_candidate(item):
                st = item.stat(follow_symlinks=False)
                if st.st_size > 0: 
                    groups_by_size[st.st_size].append(item)
        except (OSError, PermissionError, ValueError):
            continue
    return dict(groups_by_size)

def _process_entry(
    entry: os.DirEntry, 
    depth: int, 
    min_size: int, 
    queue: deque[Tuple[Path, int]], 
    visited_inodes: InodeMap, 
    visited_files: InodeMap,
    size_map: Dict[int, List[Path]]
) -> None:
    """
    Procesa una entrada de directorio durante el escaneo BFS.
    - Si es directorio: verifica profundidad, recursividad y evita ciclos mediante Inodes.
    - Si es archivo: filtra por tamaño mínimo y agrega a size_map si es elegible.
    """
    try:
        path = Path(entry.path)
        if not path.exists() or is_junction(path) or not is_safe_to_modify(path):
            return
            
        stat = entry.stat(follow_symlinks=False)
            
        if entry.is_dir():
            if depth < MAX_RECURSION_DEPTH and _is_valid_candidate(path):
                inode = (stat.st_dev, stat.st_ino)
                if inode not in visited_inodes:
                    visited_inodes.add(inode)
                    queue.append((path, depth + 1))
        elif entry.is_file() and stat.st_size >= min_size and _is_valid_candidate(path):
            inode = (stat.st_dev, stat.st_ino)
            if inode not in visited_files:
                visited_files.add(inode)
                size_map[stat.st_size].append(path)
    except (OSError, PermissionError):
        pass

def _collect_candidates(directories: Iterable[PathLike], min_size: int) -> Dict[int, List[Path]]:
    """
    Realiza un recorrido BFS (Breadth-First Search) por las rutas proporcionadas.
    Utiliza un mapa de Inodes para evitar procesar dos veces el mismo archivo físico
    (hardlinks) y devuelve solo grupos de tamaños que tienen colisiones potenciales.
    """
    size_map: Dict[int, List[Path]] = defaultdict(list)
    visited_dirs: InodeMap = set()
    visited_files: InodeMap = set()
    queue: deque[Tuple[Path, int]] = deque()
    
    for d in directories:
        if not d: continue
        try:
            p = Path(d)
            if p.exists() and _is_valid_candidate(p):
                queue.append((p, 0))
        except (OSError, PermissionError):
            continue
    
    while queue:
        current_dir, depth = queue.popleft()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    _process_entry(entry, depth, min_size, queue, visited_dirs, visited_files, size_map)
        except (OSError, PermissionError):
            continue
            
    return {sz: paths for sz, paths in size_map.items() if len(paths) > 1}

def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica la función hash indicada y agrupa rutas por digest."""
    groups: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)): 
            groups[digest].append(path)
    return {d: p for d, p in groups.items() if len(p) > 1}

def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Implementa la estrategia de cascada: hash parcial primero, hash completo solo en coincidencias."""
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final: Dict[str, List[Path]] = {}
    for subset in partial_groups.values():
        final.update(_group_paths_by_hash(subset, hash_file))
    return final

def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona la estrategia de hash óptima según el tamaño del archivo."""
    if size <= 0 or not paths: 
        return []
    results = _group_paths_by_hash(paths, hash_file) if size <= PARTIAL_READ_BYTES else _process_large_file_subset(paths)
    return [DuplicateGroup(d, size, sorted(p)) for d, p in results.items()]

def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024) -> List[DuplicateGroup]:
    """Inicia el escaneo y procesamiento de grupos duplicados. Limpia cachés de memoria."""
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size)
    if not size_map: 
        return []
    groups = [g for sz, ps in size_map.items() for g in _decide_hash_strategy_and_process(sz, ps)]
    return sorted(groups, key=lambda g: g.wasted_bytes, reverse=True)

def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula la suma total de bytes recuperables."""
    return sum(g.wasted_bytes for g in groups) if groups else 0

def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """Sugiere el archivo más antiguo del grupo para ser conservado."""
    if group is None or not group.paths: 
        return None
    candidates: List[Tuple[float, Path]] = []
    for p in group.paths:
        if isinstance(p, Path):
            try:
                if p.exists() and _is_valid_candidate(p):
                    candidates.append((p.stat().st_mtime, p))
            except (OSError, PermissionError): 
                continue
    return min(candidates, key=lambda x: x[0])[1] if candidates else group.paths[0]

def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una representación textual formateada de un grupo de duplicados para la UI."""
    if not group or not group.paths: 
        return []
    keeper = suggest_keeper(group)
    mb, wasted = group.size_bytes / 1048576, group.wasted_bytes / 1048576
    lines = [f"{group.count} copias de {mb:.2f} MB (recuperable: {wasted:.2f} MB)"]
    for p in group.paths:
        lines.append(f"   {'[conservar]' if p == keeper else '[duplicado]'} {p}")
    return lines
