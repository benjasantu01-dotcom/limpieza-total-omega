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
    """
    Define el enfoque para la generación de huellas digitales (fingerprinting).
    
    COMPLETE: Utilizado para archivos pequeños; calcula el SHA256 completo inmediatamente.
    CASCADE: Optimización para archivos grandes; filtra primero por 64KB iniciales.
    """
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
    if not isinstance(path, Path): return True
    with suppress(AttributeError, OSError, RuntimeError):
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & Win32Attributes.REPARSE_POINT))
    return False


def is_system_or_hidden(path: Path) -> bool:
    """Detecta atributos de archivo (Oculto/Sistema) en Windows que impiden la limpieza."""
    if not isinstance(path, Path): return True
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
        """
        Calcula el total de bytes que se recuperarían si se eliminaran 
        todos los duplicados excepto uno.
        """
        return (self.count - 1) * self.size_bytes if self.count > 1 else 0


def _safe_path_check(path: Path) -> bool:
    """
    Valida si una ruta es segura para ser analizada por el módulo de duplicados.
    """
    try:
        if path is None or not isinstance(path, Path): return False
        if len(str(path)) >= MAX_PATH_LENGTH: return False
        if not path.exists(): return False
        if not is_safe_to_modify(path): return False
        if is_junction(path): return False
        return not is_protected_path(path)
    except (OSError, RuntimeError, ValueError):
        return False


def _is_valid_candidate(path: Path) -> bool:
    """Verifica si el path es un archivo regular válido y no posee atributos de sistema."""
    return _safe_path_check(path) and path.is_file() and not is_system_or_hidden(path)


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo del contenido de un archivo."""
    if path is None or not isinstance(path, Path): return None
    if path in _full_hash_cache: return _full_hash_cache[path]
    if not _is_valid_candidate(path) or chunk_size <= 0: return None
    
    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while (chunk := f.read(chunk_size)):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        _full_hash_cache[path] = digest
        return digest
    except (OSError, PermissionError, IOError) as e:
        logger.debug(f"Error procesando hash completo de {path}: {e}")
        return None


def partial_hash(path: Path, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula el hash SHA256 del encabezado inicial."""
    if path is None or not isinstance(path, Path): return None
    if path in _partial_cache: return _partial_cache[path]
    if not _is_valid_candidate(path) or read_bytes <= 0: return None
    
    try:
        with open(path, "rb") as f:
            header = f.read(read_bytes)
            if not header: return None
            digest_val = hashlib.sha256(header).hexdigest()
            _partial_cache[path] = digest_val
            return digest_val
    except (OSError, PermissionError, IOError) as e:
        logger.debug(f"Error procesando hash parcial de {path}: {e}")
        return None


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa colecciones de rutas basándose únicamente en su tamaño en bytes."""
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if p is None: continue
        item = Path(p)
        if _is_valid_candidate(item):
            with suppress(OSError):
                st = item.stat(follow_symlinks=False)
                if st.st_size > 0:
                    groups_by_size[st.st_size].append(item)
    return dict(groups_by_size)


def _process_entry(
    entry: os.DirEntry, 
    depth: int, 
    min_size: int, 
    queue: deque[Tuple[Path, int]], 
    visited_inodes: set[Tuple[int, int]], 
    visited_files: set[Tuple[int, int]],
    size_map: Dict[int, List[Path]]
) -> None:
    """Procesa una entrada de directorio validando seguridad y puntos de reparse."""
    try:
        # No seguir enlaces para evitar escapes
        if entry.is_symlink(): return
        
        stat = entry.stat(follow_symlinks=False)
        inode = (stat.st_dev, stat.st_ino)
        path = Path(entry.path)
        
        if not _safe_path_check(path): return

        if entry.is_dir():
            if inode not in visited_inodes and depth < MAX_RECURSION_DEPTH:
                visited_inodes.add(inode)
                queue.append((path, depth + 1))
        elif entry.is_file() and stat.st_size >= min_size:
            if inode not in visited_files and not is_system_or_hidden(path):
                visited_files.add(inode)
                size_map[stat.st_size].append(path)
    except (OSError, PermissionError):
        pass


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Inicia el recorrido BFS del sistema de archivos buscando candidatos válidos."""
    size_map: Dict[int, List[Path]] = defaultdict(list)
    visited_dirs: set[Tuple[int, int]] = set()
    visited_files: set[Tuple[int, int]] = set()
    queue: deque[Tuple[Path, int]] = deque()
    
    for d in directories:
        if d:
            p = Path(d)
            if p.exists() and p.is_dir() and _safe_path_check(p):
                queue.append((p, 0))
    
    while queue:
        current_dir, depth = queue.popleft()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    _process_entry(entry, depth, min_size, queue, visited_dirs, visited_files, size_map)
        except (OSError, PermissionError):
            continue
    return {sz: ps for sz, ps in size_map.items() if len(ps) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica la función de hash proporcionada y agrupa los archivos resultantes."""
    groups: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if path is not None and (digest := hash_func(path)):
            groups[digest].append(path)
    return {d: p for d, p in groups.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Optimización: ejecuta hashing en cascada para archivos grandes."""
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final: Dict[str, List[Path]] = {}
    for subset in partial_groups.values():
        final.update(_group_paths_by_hash(subset, hash_file))
    return final


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Determina la estrategia de hashing según el tamaño del archivo."""
    if size <= 0: return []
    results = _group_paths_by_hash(paths, hash_file) if size <= PARTIAL_READ_BYTES else _process_large_file_subset(paths)
    return [DuplicateGroup(d, size, sorted(p)) for d, p in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquestador principal de la detección."""
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size, skip_protected)
    if not size_map: return []
    
    groups = [g for sz, ps in size_map.items() for g in _decide_hash_strategy_and_process(sz, ps)]
    return sorted(groups, key=lambda g: g.wasted_bytes, reverse=True)


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Retorna la suma total de bytes recuperables."""
    if not groups: return 0
    return sum(g.wasted_bytes for g in groups)


def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """Selecciona la versión 'original' sugerida para conservar."""
    if not group or not group.paths: return None
    candidates: List[Tuple[float, Path]] = []
    for p in group.paths:
        try:
            if _is_valid_candidate(p):
                candidates.append((p.stat(follow_symlinks=False).st_mtime, p))
        except (OSError, PermissionError, ValueError):
            continue
    return min(candidates, key=lambda x: x[0])[1] if candidates else group.paths[0]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una representación textual legible de un grupo de duplicados."""
    if not group or not group.paths: return []
    keeper = suggest_keeper(group)
    mb, wasted = group.size_bytes / 1048576, group.wasted_bytes / 1048576
    lines = [f"{group.count} copias de {mb:.2f} MB (recuperable: {wasted:.2f} MB)"]
    for p in group.paths:
        status = "[conservar]" if (p == keeper) else "[duplicado]"
        lines.append(f"   {status} {p}")
    return lines
