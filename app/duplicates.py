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
o `is_protected_path para garantizar que no se interactúe con zonas críticas.
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

# Configuración de lectura y flags de sistema (Win32 API)
PARTIAL_READ_BYTES: int = 64 * 1024
FILE_ATTRIBUTE_REPARSE_POINT: int = 0x400  
FILE_ATTRIBUTE_HIDDEN: int = 0x2          
FILE_ATTRIBUTE_SYSTEM: int = 0x4          
MAX_RECURSION_DEPTH: int = 100

# Cachés para evitar re-procesamiento durante una única corrida de análisis
_partial_cache: Dict[Path, str] = {}
_full_hash_cache: Dict[Path, str] = {}

def is_junction(path: Path) -> bool:
    """Verifica si el path es un punto de reanálisis (Junction o Symlink) usando Win32 API."""
    if not isinstance(path, Path): return False
    with suppress(AttributeError, OSError, RuntimeError):
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    return False


def is_system_or_hidden(path: Path) -> bool:
    """Verifica si el archivo posee atributos de sistema u oculto (Win32)."""
    if not isinstance(path, Path): return False
    with suppress(AttributeError, OSError, RuntimeError):
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)))
    return False


@dataclass
class DuplicateGroup:
    """
    Representa un conjunto de archivos idénticos hallados en el sistema.
    
    Attributes:
        digest: Hash identificativo del contenido (SHA256).
        size_bytes: Tamaño de cada archivo integrante en bytes.
        paths: Lista de rutas absolutas que contienen este contenido.
    """
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Cantidad total de archivos duplicados en el grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """Cálculo del espacio redundante (total - una copia base)."""
        return (self.count - 1) * self.size_bytes if self.count > 1 else 0


def _safe_path_check(path: Path) -> bool:
    """
    Validación de seguridad integral: filtra archivos protegidos, puntos de reanálisis
    y valida permisos de lectura mediante las guardas del proyecto.
    """
    if not isinstance(path, Path): return False
    try:
        real_path = Path(os.path.realpath(path))
    except (OSError, RuntimeError):
        return False
        
    return (
        real_path.is_file() and 
        is_safe_to_modify(real_path) and 
        is_protected_path(real_path) is False and 
        not is_junction(real_path)
    )


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Filtro de archivos candidatos basado en tamaño positivo y atributos de visibilidad."""
    if not isinstance(path, Path): return False
    return st_size > 0 and _safe_path_check(path) and not is_system_or_hidden(path)


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo del archivo usando lectura en chunks."""
    if not isinstance(path, Path) or not _safe_path_check(path): 
        return None
    if path in _full_hash_cache: return _full_hash_cache[path]
    
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
    """Genera un fingerprint rápido basado en los primeros K bytes del archivo."""
    if not isinstance(path, Path) or not _safe_path_check(path): 
        return None
    if path in _partial_cache: return _partial_cache[path]
    
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
    """Clasifica una secuencia de rutas según su tamaño (bloque fundamental de la detección)."""
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    if paths is None: return groups_by_size
    
    for p in paths:
        if not p: continue
        try:
            item = Path(p)
            st = item.stat()
            if _is_valid_candidate(item, st.st_size):
                groups_by_size[st.st_size].append(item)
        except (OSError, PermissionError, FileNotFoundError, TypeError): 
            continue
    return groups_by_size


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Recolección recursiva (BFS) de archivos, evitando bucles y puntos de reanálisis."""
    raw_map: Dict[int, List[Path]] = defaultdict(list)
    size_counts: Dict[int, int] = defaultdict(int)
    
    if not directories: return raw_map
    queue: deque[Tuple[Path, int]] = deque()
    
    for d_raw in directories:
        if not d_raw: continue
        with suppress(TypeError, ValueError):
            d = Path(d_raw)
            if d.is_dir() and is_safe_to_modify(d):
                queue.append((d, 0))
    
    visited: set[Tuple[int, int]] = set()
    
    while queue:
        current_dir, depth = queue.popleft()
        if depth > MAX_RECURSION_DEPTH: continue
            
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        path = Path(entry.path)
                        if skip_protected and is_protected_path(path): continue
                        if entry.is_dir(follow_symlinks=False):
                            if is_safe_to_modify(path) and not is_junction(path):
                                queue.append((path, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            if stat.st_size >= min_size and _is_valid_candidate(path, stat.st_size):
                                inode = (stat.st_dev, stat.st_ino)
                                if inode not in visited:
                                    visited.add(inode)
                                    raw_map[stat.st_size].append(path)
                                    size_counts[stat.st_size] += 1
                    except (OSError, PermissionError, FileNotFoundError): continue
        except (OSError, PermissionError, FileNotFoundError): continue
    return {sz: paths for sz, paths in raw_map.items() if size_counts[sz] > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica una función de hash y agrupa rutas por colisión de digest."""
    groups: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if not isinstance(path, Path): continue
        if (d := hash_func(path)): groups[d].append(path)
    return {d: p for d, p in groups.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """
    Refina grupos de archivos grandes mediante hashing de dos niveles:
    primero aplica un fingerprint parcial (64KB) y luego el hash completo.
    """
    if not paths: return {}
    partial_groups: Dict[str, List[Path]] = _group_paths_by_hash(paths, partial_hash)
    final: Dict[str, List[Path]] = {}
    for subset in partial_groups.values():
        final.update(_group_paths_by_hash(subset, hash_file))
    return final


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """
    Selecciona la estrategia de hashing óptima: hash directo si el archivo es pequeño,
    o el proceso de dos niveles para archivos grandes (>PARTIAL_READ_BYTES).
    """
    if not paths: return []
    results: Dict[str, List[Path]] = _group_paths_by_hash(paths, hash_file) if size <= PARTIAL_READ_BYTES else _process_large_file_subset(paths)
    return [DuplicateGroup(d, size, sorted(p)) for d, p in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquestación del proceso: recolección -> filtrado por tamaño -> resolución por hash."""
    if not directories: return []
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups = [g for sz, ps in size_map.items() for g in _decide_hash_strategy_and_process(sz, ps)]
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma total de bytes recuperables de una lista de grupos de duplicados."""
    if not groups: return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def _get_mtime(path: Path) -> float:
    """Devuelve el timestamp de modificación o infinito si no es accesible."""
    if not isinstance(path, Path): return float('inf')
    with suppress(OSError, PermissionError, FileNotFoundError):
        return path.stat().st_mtime
    return float('inf')


def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """Heurística: propone conservar el archivo con la fecha de modificación más antigua."""
    if not isinstance(group, DuplicateGroup) or not group.paths: 
        return None
        
    candidates = [( _get_mtime(p), p) for p in group.paths]
    valid_candidates = [c for c in candidates if c[0] != float('inf')]
    
    if not valid_candidates:
        return group.paths[0]
        
    return min(valid_candidates, key=lambda x: x[0])[1]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera representación textual de un grupo para la visualización en la interfaz."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return []
    
    keeper = suggest_keeper(group)
    if keeper is None: return []

    mb = round(group.size_bytes / 1048576, 2)
    wasted = round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb} MB (recuperable: {wasted} MB)"]
    lines.extend(f"   {'[conservar]' if p == keeper else '[duplicado]'} {p}" for p in group.paths)
    return lines
