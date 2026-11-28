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
    """Determina si un archivo está bloqueado intentando una lectura atómica mínima."""
    try:
        if not path.is_file(): return True
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
        """Cantidad de archivos que comparten este mismo hash."""
        return len(self.paths)

    @property
    def wasted_bytes(self) -> int:
        """Calcula el espacio total que podría recuperarse conservando solo una instancia."""
        return (self.count - 1) * self.size_bytes if self.count > 1 else 0

def _safe_path_check(path: Path) -> bool:
    """
    Valida la integridad de la ruta: longitud, permisos de escritura, 
    protección del sistema y ausencia de puntos de reparse (junctions).
    """
    if not isinstance(path, Path): return False
    try:
        return (len(str(path)) < MAX_PATH_LENGTH and 
                is_safe_to_modify(path) and 
                not is_protected_path(path) and 
                not is_junction(path))
    except Exception:
        return False

def _is_valid_candidate(path: Path) -> bool:
    """Filtra archivos ocultos, de sistema o rutas restringidas antes de procesar."""
    return not is_system_or_hidden(path) and _safe_path_check(path)

def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el SHA256 completo. Utiliza caché para evitar re-lecturas de disco."""
    if path in _full_hash_cache: return _full_hash_cache[path]
    if not _is_valid_candidate(path) or is_file_locked(path): return None
    
    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            try:
                while (chunk := f.read(chunk_size)):
                    hasher.update(chunk)
            finally:
                f.close()
        digest: str = hasher.hexdigest()
        _full_hash_cache[path] = digest
        return digest
    except (OSError, PermissionError, IOError, ValueError):
        return None

def partial_hash(path: Path, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula un hash rápido de los primeros bytes para descartar falsos positivos."""
    if path in _partial_cache: return _partial_cache[path]
    if not _is_valid_candidate(path) or is_file_locked(path): return None
    
    try:
        with open(path, "rb") as f:
            try:
                header: bytes = f.read(read_bytes)
                if not header: return None
                digest_val: str = hashlib.sha256(header).hexdigest()
            finally:
                f.close()
        _partial_cache[path] = digest_val
        return digest_val
    except (OSError, PermissionError, IOError, ValueError):
        return None

def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa rutas por tamaño (bytes), omitiendo archivos protegidos o inválidos."""
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if not p: continue
        item = Path(p)
        if _is_valid_candidate(item):
            try:
                if item.is_file():
                    sz = item.stat(follow_symlinks=False).st_size
                    if sz > 0:
                        groups_by_size[sz].append(item)
            except (OSError, PermissionError):
                continue
    return dict(groups_by_size)

def _collect_candidates(directories: Iterable[PathLike], min_size: int) -> Dict[int, List[Path]]:
    """
    Recorre recursivamente los directorios buscando archivos candidatos.
    Utiliza un mapa de inodos (dev, ino) para evitar procesar el mismo archivo físico varias veces.
    """
    size_map: Dict[int, List[Path]] = defaultdict(list)
    visited_files: InodeMap = set()
    queue: deque[Tuple[Path, int]] = deque((Path(d), 0) for d in directories if d)
    
    while queue:
        current_dir, depth = queue.popleft()
        if depth > MAX_RECURSION_DEPTH or not _safe_path_check(current_dir):
            continue
            
        with suppress(OSError):
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        p = Path(os.fsdecode(entry.path))
                        if entry.is_dir(follow_symlinks=False):
                            queue.append((p, depth + 1))
                        elif entry.is_file(follow_symlinks=False) and _is_valid_candidate(p):
                            try:
                                st = entry.stat()
                                if st.st_size >= min_size:
                                    inode: Tuple[int, int] = (st.st_dev, st.st_ino)
                                    if inode not in visited_files:
                                        visited_files.add(inode)
                                        size_map[st.st_size].append(p)
                            except OSError:
                                continue
                    except (OSError, PermissionError): continue
    return {sz: paths for sz, paths in size_map.items() if len(paths) > 1}

def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica un algoritmo de hashing a una lista y retorna solo grupos con múltiples coincidencias."""
    groups: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)): 
            groups[digest].append(path)
    return {d: p for d, p in groups.items() if len(p) > 1}

def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Aplica estrategia de reducción en dos pasos: hash parcial previo al costoso hash completo."""
    final: Dict[str, List[Path]] = {}
    for subset in _group_paths_by_hash(paths, partial_hash).values():
        final.update(_group_paths_by_hash(subset, hash_file))
    return final

def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona la técnica de hash: hash directo para archivos pequeños, cascada para grandes."""
    if size <= 0 or not paths: return []
    results: Dict[str, List[Path]] = (
        _group_paths_by_hash(paths, hash_file) 
        if size <= PARTIAL_READ_BYTES 
        else _process_large_file_subset(paths)
    )
    return [DuplicateGroup(d, size, sorted(p)) for d, p in results.items()]

def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024) -> List[DuplicateGroup]:
    """Flujo principal: colecta, categoriza y calcula duplicados ordenados por espacio desperdiciado."""
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size)
    groups: List[DuplicateGroup] = [g for sz, ps in size_map.items() for g in _decide_hash_strategy_and_process(sz, ps)]
    return sorted(groups, key=lambda g: g.wasted_bytes, reverse=True)

def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma total de bytes recuperables de una lista de grupos."""
    return sum(g.wasted_bytes for g in groups) if groups else 0

def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """Heurística: retorna la ruta del archivo con la fecha de modificación más antigua."""
    if not group or not group.paths: return None
    candidates: List[Tuple[float, Path]] = []
    for p in group.paths:
        try:
            candidates.append((p.stat().st_mtime, p))
        except (OSError, PermissionError, FileNotFoundError):
            continue
    return min(candidates, key=lambda x: x[0])[1] if candidates else group.paths[0]

def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una representación textual formateada de un grupo para reportes."""
    if not group or not group.paths: return []
    keeper = suggest_keeper(group)
    mb: float = group.size_bytes / 1048576
    wasted: float = group.wasted_bytes / 1048576
    lines: List[str] = [f"{group.count} copias de {mb:.2f} MB (recuperable: {wasted:.2f} MB)"]
    for p in group.paths:
        lines.append(f"   {'[conservar]' if p == keeper else '[duplicado]'} {p}")
    return lines
