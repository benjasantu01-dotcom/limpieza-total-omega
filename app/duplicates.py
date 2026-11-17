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

class HashStrategy(Enum):
    """Define si un archivo requiere hash completo o puede usar hash parcial por ser pequeño."""
    COMPLETE = auto()
    CASCADE = auto()

class Win32Attributes:
    """Constantes de atributos de archivo para llamadas directas a la API de Windows."""
    REPARSE_POINT: int = 0x400  
    HIDDEN: int = 0x2          
    SYSTEM: int = 0x4          

MAX_RECURSION_DEPTH: int = 100
MAX_PATH_LENGTH: int = 260

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
        """Cantidad de archivos en este grupo."""
        return len(self.paths)

    @property
    def wasted_bytes(self) -> int:
        """Total de bytes redundantes que podrían liberarse."""
        return (self.count - 1) * self.size_bytes if self.count > 1 else 0


def _safe_path_check(path: Path) -> bool:
    """
    Validación de seguridad consolidada. 
    Asegura que el archivo sea accesible, no esté protegido por el sistema
    y no sea un punto de reparse que pueda causar bucles infinitos o problemas.
    """
    try:
        if not path or not path.exists(): return False
        if not os.access(path, os.R_OK): return False
        if not is_safe_to_modify(path): return False
        if is_junction(path): return False
        return not is_protected_path(path)
    except (OSError, RuntimeError, ValueError):
        return False


def _is_valid_candidate(path: Path) -> bool:
    """Filtra archivos de sistema o protegidos. Solo candidatos seguros pasan a indexación."""
    if not _safe_path_check(path): return False
    return not is_system_or_hidden(path)


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo. 
    Usa caché global `_full_hash_cache` para evitar I/O redundante en archivos grandes.
    """
    if not path or path in _full_hash_cache: return _full_hash_cache.get(path)
    if not _safe_path_check(path) or len(str(path)) >= MAX_PATH_LENGTH: return None
    
    try:
        if not path.is_file(): return None
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
    """
    Calcula un hash rápido basado únicamente en la cabecera (primeros 64KB).
    Crucial para el rendimiento en pasos de filtrado inicial (cascada).
    """
    if not path or path in _partial_cache: return _partial_cache.get(path)
    if not _safe_path_check(path) or len(str(path)) >= MAX_PATH_LENGTH: return None
    
    try:
        if not path.is_file(): return None
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
    """Agrupa rutas por tamaño de archivo, descartando archivos vacíos o inaccesibles."""
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if not p: continue
        try:
            item = Path(p)
            if item.exists() and item.is_file():
                st = item.stat(follow_symlinks=False)
                if st.st_size > 0 and _is_valid_candidate(item):
                    groups_by_size[st.st_size].append(item)
        except (OSError, PermissionError, ValueError):
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
    """Helper de recorrido BFS: maneja la lógica de exploración de directorios y registro de archivos."""
    try:
        path = Path(entry.path)
        stat = entry.stat(follow_symlinks=False)
        inode = (stat.st_dev, stat.st_ino)
        
        if entry.is_dir():
            if inode not in visited_inodes and depth < MAX_RECURSION_DEPTH and _safe_path_check(path):
                visited_inodes.add(inode)
                queue.append((path, depth + 1))
        elif entry.is_file():
            if stat.st_size >= min_size and inode not in visited_inodes and _is_valid_candidate(path):
                visited_inodes.add(inode)
                size_map[stat.st_size].append(path)
    except (OSError, PermissionError, FileNotFoundError) as e:
        logger.debug(f"Error procesando {entry.path}: {e}")


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Realiza un recorrido BFS del sistema de archivos, limitando la profundidad para evitar latencia."""
    size_map: Dict[int, List[Path]] = defaultdict(list)
    visited_inodes: set[Tuple[int, int]] = set()
    queue: deque[Tuple[Path, int]] = deque()
    
    for d in directories:
        if not d: continue
        path_obj = Path(d)
        if path_obj.exists() and path_obj.is_dir() and _safe_path_check(path_obj):
            queue.append((path_obj, 0))
    
    while queue:
        current_dir, depth = queue.popleft()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    _process_entry(entry, depth, min_size, queue, visited_inodes, size_map)
        except (OSError, PermissionError, FileNotFoundError):
            continue
    return {sz: ps for sz, ps in size_map.items() if len(ps) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica el hash proporcionado y agrupa las rutas que colisionan (posibles duplicados)."""
    groups: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if not path: continue
        if (digest := hash_func(path)):
            groups[digest].append(path)
    return {d: p for d, p in groups.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Implementa hashing en cascada: reduce falsos positivos con hash parcial y confirma con total."""
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final: Dict[str, List[Path]] = {}
    for subset in partial_groups.values():
        final.update(_group_paths_by_hash(subset, hash_file))
    return final


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona la estrategia de hashing: archivos minúsculos usan Hash Completo, grandes usan Cascada."""
    strategy = HashStrategy.COMPLETE if size <= PARTIAL_READ_BYTES else HashStrategy.CASCADE
    
    if strategy == HashStrategy.COMPLETE:
        results = _group_paths_by_hash(paths, hash_file)
    else:
        results = _process_large_file_subset(paths)
        
    return [DuplicateGroup(d, size, sorted(p)) for d, p in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """
    Orquestador principal: 
    1. Limpia cachés de sesiones anteriores.
    2. Recolecta candidatos mediante BFS (size-map).
    3. Clasifica duplicados por estrategia de hash.
    """
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size, skip_protected)
    if not size_map: return []
    
    groups = [g for sz, ps in size_map.items() for g in _decide_hash_strategy_and_process(sz, ps)]
    return sorted(groups, key=lambda g: g.wasted_bytes, reverse=True)


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula la suma total de bytes recuperables de un set de grupos."""
    if not groups: return 0
    return sum(g.wasted_bytes for g in groups)


def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """Heurística: sugiere conservar el archivo más antiguo (mtime) como el 'original'."""
    if not group or not group.paths: return None
    candidates: List[Tuple[float, Path]] = []
    for p in group.paths:
        if not p: continue
        try:
            if p.exists() and _safe_path_check(p):
                candidates.append((p.stat(follow_symlinks=False).st_mtime, p))
        except (OSError, PermissionError, ValueError):
            continue
    return min(candidates, key=lambda x: x[0])[1] if candidates else group.paths[0]


def format_group(group: DuplicateGroup) -> List[str]:
    """Transforma el grupo de duplicados en una representación textual para la interfaz."""
    if not group: return []
    keeper = suggest_keeper(group)
    mb, wasted = group.size_bytes / 1048576, group.wasted_bytes / 1048576
    lines = [f"{group.count} copias de {mb:.2f} MB (recuperable: {wasted:.2f} MB)"]
    for p in group.paths:
        status = "[conservar]" if (p == keeper) else "[duplicado]"
        lines.append(f"   {status} {p}")
    return lines
