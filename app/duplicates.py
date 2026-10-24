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
    if not isinstance(path, Path): return True
    if path.is_symlink():
        return True
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError, RuntimeError):
        return False


def is_system_or_hidden(path: Path) -> bool:
    """Verifica si el archivo posee atributos de sistema u oculto (Win32)."""
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)))
    except (AttributeError, OSError, RuntimeError):
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
        return len(self.paths)

    @property
    def wasted_bytes(self) -> int:
        """Calcula el espacio redundante (se conserva 1 copia, el resto es pérdida)."""
        return (self.count - 1) * self.size_bytes if self.count > 1 else 0


def _safe_path_check(path: Path) -> bool:
    """Valida la integridad de un path frente a restricciones de seguridad y enlaces simbólicos."""
    if path is None: return False
    return is_safe_to_modify(path) and not is_protected_path(path) and not is_junction(path)


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Determina si un archivo es apto para el análisis evitando archivos vacíos o de sistema."""
    return st_size > 0 and not is_system_or_hidden(path)


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del archivo usando caché de sesión.
    
    Args:
        path: Ruta del archivo a procesar.
        chunk_size: Tamaño del buffer de lectura en bytes.
        
    Returns:
        Hash hexadecimal del archivo o None si no es accesible.
    """
    if path is None or not path.exists() or path in _full_hash_cache: 
        return _full_hash_cache.get(path) if path in _full_hash_cache else None
    
    if not path.is_file() or not _safe_path_check(path): return None
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
    """Calcula un hash SHA256 sobre los primeros bytes para reducir I/O en archivos grandes."""
    if path is None or not path.exists() or path in _partial_cache: 
        return _partial_cache.get(path) if path in _partial_cache else None
        
    if not path.is_file() or not _safe_path_check(path): return None
    try:
        with open(path, "rb") as f:
            if not (header := f.read(read_bytes)): return None
            digest_val = hashlib.sha256(header).hexdigest()
            _partial_cache[path] = digest_val
            return digest_val
    except (OSError, PermissionError, IOError):
        return None


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa rutas según el tamaño en bytes, filtrando elementos no aptos."""
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    if paths is None: return groups_by_size
    for item in (Path(p) for p in paths if p):
        if _safe_path_check(item) and item.exists() and item.is_file():
            try:
                st = item.stat()
                if _is_valid_candidate(item, st.st_size):
                    groups_by_size[st.st_size].append(item)
            except (OSError, PermissionError): continue
    return groups_by_size


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """
    Exploración recursiva (BFS) para recolectar archivos candidatos.
    
    Usa la tupla (st_dev, st_ino) para identificar enlaces físicos y evitar ciclos.
    """
    if not directories: return {}
    
    raw_map: Dict[int, List[Path]] = defaultdict(list)
    queue: deque[Tuple[Path, int]] = deque((Path(d), 0) for d in directories if d and Path(d).exists())
    visited: set[Tuple[int, int]] = set()
    
    while queue:
        current_dir, depth = queue.popleft()
        if depth > MAX_RECURSION_DEPTH or not _safe_path_check(current_dir): continue
            
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        queue.append((Path(entry.path), depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        try:
                            stat = entry.stat(follow_symlinks=False)
                            if stat.st_size >= min_size and _is_valid_candidate(Path(entry.path), stat.st_size):
                                inode = (stat.st_dev, stat.st_ino)
                                if inode not in visited:
                                    path = Path(entry.path)
                                    if not (skip_protected and is_protected_path(path)):
                                        visited.add(inode)
                                        raw_map[stat.st_size].append(path)
                        except (OSError, PermissionError): continue
        except (OSError, PermissionError): continue
    return {sz: paths for sz, paths in raw_map.items() if len(paths) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica una función de hash a una colección y agrupa por coincidencias de digest."""
    groups: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if path and (d := hash_func(path)): groups[d].append(path)
    return {d: p for d, p in groups.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Refina grupos mediante hashing en dos etapas: parcial (rápido) y completo (preciso)."""
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final: Dict[str, List[Path]] = {}
    for subset in partial_groups.values():
        final.update(_group_paths_by_hash(subset, hash_file))
    return final


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona la estrategia de hash según el peso del archivo para optimizar rendimiento."""
    if size <= PARTIAL_READ_BYTES:
        results = _group_paths_by_hash(paths, hash_file)
    else:
        results = _process_large_file_subset(paths)
    return [DuplicateGroup(d, size, sorted(p)) for d, p in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Flujo principal de detección de duplicados en el árbol de archivos dado."""
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups = [g for sz, ps in size_map.items() for g in _decide_hash_strategy_and_process(sz, ps)]
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el total de bytes que podrían liberarse al eliminar copias."""
    if not groups: return 0
    return sum(g.wasted_bytes for g in groups)


def _get_mtime(path: Path) -> float:
    """Obtiene el tiempo de modificación de un archivo de forma segura."""
    try:
        return path.stat().st_mtime
    except (OSError, PermissionError):
        return float('inf')


def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """Sugiere el archivo más antiguo como original para minimizar riesgo de pérdida."""
    if not isinstance(group, DuplicateGroup) or not group.paths: 
        return None
        
    candidates: List[Tuple[float, Path]] = []
    for p in group.paths:
        if p.exists():
            candidates.append((_get_mtime(p), p))
    
    if not candidates:
        return group.paths[0] if group.paths else None
    return min(candidates, key=lambda x: x[0])[1]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una representación textual legible del grupo de duplicados para el reporte."""
    if not group: return []
    keeper = suggest_keeper(group)
    mb = round(group.size_bytes / 1048576, 2)
    wasted = round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb} MB (recuperable: {wasted} MB)"]
    lines.extend(f"   {'[conservar]' if p == keeper else '[duplicado]'} {p}" for p in group.paths)
    return lines
