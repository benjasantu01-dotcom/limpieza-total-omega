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

PARTIAL_READ_BYTES: int = 64 * 1024
logger = logging.getLogger(__name__)

class Win32Attributes:
    REPARSE_POINT: int = 0x400  
    HIDDEN: int = 0x2          
    SYSTEM: int = 0x4          

MAX_RECURSION_DEPTH: int = 100

_partial_cache: Dict[Path, str] = {}
_full_hash_cache: Dict[Path, str] = {}

def is_junction(path: Path) -> bool:
    """Verifica mediante GetFileAttributesW si el path apunta a un Junction o Symlink."""
    with suppress(AttributeError, OSError, RuntimeError):
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & Win32Attributes.REPARSE_POINT))
    return False


def is_system_or_hidden(path: Path) -> bool:
    """Detecta atributos de archivo (Oculto/Sistema) en Windows que impiden la limpieza."""
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
        return len(self.paths)

    @property
    def wasted_bytes(self) -> int:
        return (self.count - 1) * self.size_bytes if self.count > 1 else 0


def _safe_path_check(path: Path) -> bool:
    """Valida seguridad, permisos de acceso y evita puntos de reparse."""
    try:
        if not path.exists(): return False
        if not os.access(path, os.R_OK): return False
        if not is_safe_to_modify(path): return False
        if is_junction(path): return False
        return not is_protected_path(path.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False


def _is_valid_candidate(path: Path, st_size: int = -1) -> bool:
    """Valida si un archivo es candidato para comparación de duplicados."""
    if not _safe_path_check(path): return False
    effective_size = st_size
    if effective_size == -1:
        with suppress(OSError):
            effective_size = path.stat(follow_symlinks=False).st_size
    return effective_size > 0 and not is_system_or_hidden(path)


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo del archivo con caché."""
    if not path.is_file(): return None
    if path in _full_hash_cache: return _full_hash_cache[path]
    if not _safe_path_check(path): return None
    
    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while (chunk := f.read(chunk_size)):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        _full_hash_cache[path] = digest
        return digest
    except (OSError, PermissionError, IOError) as e:
        logger.debug(f"Error hash_file {path}: {e}")
        return None


def partial_hash(path: Path, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula hash SHA256 sobre los bytes iniciales del archivo."""
    if not path.is_file(): return None
    if path in _partial_cache: return _partial_cache[path]
    if not _safe_path_check(path): return None
    
    try:
        with open(path, "rb") as f:
            header = f.read(read_bytes)
            if not header: return None
            digest_val = hashlib.sha256(header).hexdigest()
            _partial_cache[path] = digest_val
            return digest_val
    except (OSError, PermissionError, IOError) as e:
        logger.debug(f"Error partial_hash {path}: {e}")
        return None


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa archivos por tamaño en bytes."""
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        item = Path(p)
        if _is_valid_candidate(item):
            try:
                st = item.stat(follow_symlinks=False)
                groups_by_size[st.st_size].append(item)
            except (OSError, PermissionError) as e:
                logger.debug(f"Error stat {item}: {e}")
                continue
    return dict(groups_by_size)


def _process_entry(
    entry: os.DirEntry, 
    depth: int, 
    min_size: int, 
    queue: deque[Tuple[Path, int]], 
    visited_inodes: set[Tuple[int, int]], 
    size_map: Dict[int, List[Path]]
) -> None:
    """Evalúa una entrada de directorio para el recorrido BFS."""
    try:
        if entry.is_symlink(): return
        path = Path(entry.path)
        if not _is_valid_candidate(path): return

        if entry.is_dir():
            if depth < MAX_RECURSION_DEPTH:
                queue.append((path, depth + 1))
        elif entry.is_file():
            stat = entry.stat(follow_symlinks=False)
            if stat.st_size >= min_size:
                inode = (stat.st_dev, stat.st_ino)
                if inode not in visited_inodes:
                    visited_inodes.add(inode)
                    size_map[stat.st_size].append(path)
    except (OSError, PermissionError) as e:
        logger.debug(f"Error procesando {entry.path}: {e}")


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Realiza un recorrido BFS para agrupar archivos por tamaño."""
    size_map: Dict[int, List[Path]] = defaultdict(list)
    visited_inodes: set[Tuple[int, int]] = set()
    visited_paths: set[Path] = set()
    queue: deque[Tuple[Path, int]] = deque()
    
    for d in directories:
        try:
            p_dir = Path(d).resolve(strict=True)
            if p_dir.is_dir() and _safe_path_check(p_dir):
                queue.append((p_dir, 0))
        except (OSError, PermissionError, ValueError) as e:
            logger.warning(f"Directorio inicial inaccesible {d}: {e}")
            continue
    
    while queue:
        current_dir, depth = queue.popleft()
        if current_dir in visited_paths: continue
        visited_paths.add(current_dir)

        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    _process_entry(entry, depth, min_size, queue, visited_inodes, size_map)
        except (OSError, PermissionError) as e:
            logger.debug(f"No se pudo listar {current_dir}: {e}")
    return {sz: ps for sz, ps in size_map.items() if len(ps) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica una función de hash a rutas y agrupa por resultado."""
    groups: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups[digest].append(path)
    return {d: p for d, p in groups.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Refina duplicados usando hashing en cascada (parcial -> completo)."""
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final: Dict[str, List[Path]] = {}
    for subset in partial_groups.values():
        final.update(_group_paths_by_hash(subset, hash_file))
    return final


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona la estrategia de hash según el tamaño."""
    if size <= PARTIAL_READ_BYTES:
        results = _group_paths_by_hash(paths, hash_file)
    else:
        results = _process_large_file_subset(paths)
    return [DuplicateGroup(d, size, sorted(p)) for d, p in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Punto de entrada: analiza directorios en busca de duplicados."""
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size, skip_protected)
    if not size_map: return []
    
    groups = [g for sz, ps in size_map.items() for g in _decide_hash_strategy_and_process(sz, ps)]
    return sorted(groups, key=lambda g: g.wasted_bytes, reverse=True)


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    return sum(g.wasted_bytes for g in groups)


def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """Heurística: sugiere conservar el archivo más antiguo."""
    if not group.paths: return None
    candidates = []
    for p in group.paths:
        try:
            if p.exists():
                candidates.append((p.stat(follow_symlinks=False).st_mtime, p))
        except (OSError, PermissionError):
            continue
    return min(candidates, key=lambda x: x[0])[1] if candidates else group.paths[0]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera representación textual para la UI."""
    keeper = suggest_keeper(group)
    mb, wasted = group.size_bytes / 1048576, group.wasted_bytes / 1048576
    lines = [f"{group.count} copias de {mb:.2f} MB (recuperable: {wasted:.2f} MB)"]
    for p in group.paths:
        status = "[conservar]" if (p == keeper) else "[duplicado]"
        lines.append(f"   {status} {p}")
    return lines
