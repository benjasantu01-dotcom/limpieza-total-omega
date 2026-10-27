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
    """Representa un conjunto de archivos con contenido idéntico."""
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Devuelve el número total de archivos en el grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """Calcula el espacio redundante (se conserva 1 copia, el resto es pérdida)."""
        return (self.count - 1) * self.size_bytes if self.count > 1 else 0


def _safe_path_check(path: Path) -> bool:
    """
    Validación de seguridad compuesta.
    Verifica que la ruta no esté protegida, sea segura para leer y no sea un punto de reanálisis.
    """
    return isinstance(path, Path) and path.is_file() and is_safe_to_modify(path) and not is_protected_path(path) and not is_junction(path)


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Determina si un archivo es apto para el análisis evitando archivos vacíos o de sistema."""
    return st_size > 0 and _safe_path_check(path) and not is_system_or_hidden(path)


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del archivo. 
    Maneja excepciones de acceso para asegurar que archivos bloqueados no aborten el proceso.
    """
    if not _safe_path_check(path): 
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
    except (OSError, PermissionError, IOError):
        return None


def partial_hash(path: Path, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula un fingerprint rápido leyendo solo los primeros N bytes del archivo."""
    if not _safe_path_check(path): 
        return None
    if path in _partial_cache: return _partial_cache[path]
    try:
        with open(path, "rb") as f:
            if not (header := f.read(read_bytes)): return None
            digest_val = hashlib.sha256(header).hexdigest()
            _partial_cache[path] = digest_val
            return digest_val
    except (OSError, PermissionError, IOError):
        return None


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa una lista de rutas según su tamaño en bytes, filtrando archivos no válidos."""
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    if paths is None: return groups_by_size
    for p in paths:
        if not p: continue
        item = Path(p)
        try:
            st = item.stat()
            if _is_valid_candidate(item, st.st_size):
                groups_by_size[st.st_size].append(item)
        except (OSError, PermissionError, FileNotFoundError): continue
    return groups_by_size


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """
    Exploración recursiva (BFS) para identificar archivos candidatos a duplicados.
    Utiliza un conjunto `visited` de tuplas (st_dev, st_ino) para evitar procesar enlaces físicos múltiples veces.
    """
    raw_map: Dict[int, List[Path]] = defaultdict(list)
    size_counts: Dict[int, int] = defaultdict(int)
    
    if not directories: return raw_map
    queue: deque[Tuple[Path, int]] = deque()
    for d_raw in directories:
        if d_raw and (d := Path(d_raw)).is_dir() and is_safe_to_modify(d):
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
    """Aplica una función de hash a una lista y agrupa por coincidencias de digest."""
    groups: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (d := hash_func(path)): groups[d].append(path)
    return {d: p for d, p in groups.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Refina grupos mediante hashing en dos etapas: parcial (rápido) y completo (preciso)."""
    if not paths: return {}
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final: Dict[str, List[Path]] = {}
    for subset in partial_groups.values():
        final.update(_group_paths_by_hash(subset, hash_file))
    return final


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona la estrategia de hash según el tamaño para optimizar rendimiento."""
    if not paths: return []
    results = _group_paths_by_hash(paths, hash_file) if size <= PARTIAL_READ_BYTES else _process_large_file_subset(paths)
    return [DuplicateGroup(d, size, sorted(p)) for d, p in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Flujo principal de detección de duplicados en los directorios especificados."""
    if not directories: return []
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups = [g for sz, ps in size_map.items() for g in _decide_hash_strategy_and_process(sz, ps)]
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma total de espacio que se recuperaría si se eliminan los duplicados."""
    return sum(g.wasted_bytes for g in groups) if groups else 0


def _get_mtime(path: Path) -> float:
    """Obtiene el tiempo de última modificación del archivo de forma segura."""
    if not isinstance(path, Path): return float('inf')
    with suppress(OSError, PermissionError, FileNotFoundError):
        return path.stat().st_mtime
    return float('inf')


def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """Sugiere el archivo más antiguo (menor mtime) como original para reducir riesgos."""
    if not isinstance(group, DuplicateGroup) or not group.paths: 
        return None
        
    candidates = [( _get_mtime(p), p) for p in group.paths]
    valid_candidates = [c for c in candidates if c[0] != float('inf')]
    
    if not valid_candidates:
        return group.paths[0]
        
    return min(valid_candidates, key=lambda x: x[0])[1]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una lista de strings detallando el grupo de duplicados para el reporte."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return []
    
    keeper = suggest_keeper(group)
    if keeper is None: return []

    mb = round(group.size_bytes / 1048576, 2)
    wasted = round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb} MB (recuperable: {wasted} MB)"]
    lines.extend(f"   {'[conservar]' if p == keeper else '[duplicado]'} {p}" for p in group.paths)
    return lines
