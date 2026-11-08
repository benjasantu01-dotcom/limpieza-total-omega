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
    """
    Verifica si el path es un punto de reanálisis (Junction o Symlink) usando Win32 API.
    Retorna True si la ruta no puede ser verificada o es un reparse point.
    """
    if not isinstance(path, Path): return True
    with suppress(AttributeError, OSError, RuntimeError):
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    return False


def is_system_or_hidden(path: Path) -> bool:
    """
    Verifica si el archivo posee atributos de sistema u oculto (Win32).
    Utilizado para ignorar archivos que el sistema operativo gestiona activamente.
    """
    if not isinstance(path, Path): return False
    with suppress(AttributeError, OSError, RuntimeError):
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)))
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
        """Calcula el espacio total recuperable excluyendo el archivo 'keeper'."""
        return (self.count - 1) * self.size_bytes if self.count > 1 else 0


def _safe_path_check(path: Path) -> bool:
    """
    Valida la seguridad de la ruta mediante el módulo safety antes de cualquier I/O.
    """
    if not isinstance(path, Path): return False
    try:
        if not path.exists(): return False
        abs_path = path.resolve(strict=True)
        if not abs_path.is_file() or not is_safe_to_modify(abs_path):
            return False
        return not is_protected_path(abs_path) and not is_junction(abs_path)
    except (OSError, RuntimeError, ValueError):
        return False


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """
    Valida si un archivo es candidato apto según tamaño y atributos de sistema.
    """
    return st_size > 0 and not is_system_or_hidden(path) and _safe_path_check(path)


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo. Utiliza caché interna de sesión.
    """
    if not isinstance(path, Path): return None
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
    Calcula el hash SHA256 de los primeros bytes (fingerprint).
    """
    if not isinstance(path, Path): return None
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
    """Agrupa archivos por tamaño exacto para filtrar candidatos únicos."""
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if p is None: continue
        item = Path(p)
        try:
            if item.exists():
                st = item.stat()
                if _is_valid_candidate(item, st.st_size):
                    groups_by_size[st.st_size].append(item)
        except (OSError, PermissionError, TypeError): 
            continue
    return dict(groups_by_size)


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """
    Realiza un recorrido BFS del sistema de archivos, recolectando candidatos
    según el tamaño y validando integridad contra el módulo safety.
    """
    if directories is None or min_size < 0: return {}
    size_map: Dict[int, List[Path]] = defaultdict(list)
    visited: set[Tuple[int, int]] = set()
    queue: deque[Tuple[Path, int]] = deque()
    
    for d in directories:
        if d: 
            try:
                p = Path(d)
                if p.exists(): queue.append((p, 0))
            except (OSError, TypeError): continue
    
    while queue:
        current_dir, depth = queue.popleft()
        if depth > MAX_RECURSION_DEPTH or not is_safe_to_modify(current_dir) or is_junction(current_dir): 
            continue
            
        with suppress(OSError, PermissionError):
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        if entry is None: continue
                        if entry.is_dir(follow_symlinks=False):
                            if depth + 1 <= MAX_RECURSION_DEPTH:
                                queue.append((Path(entry.path), depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            entry_path = Path(entry.path)
                            if skip_protected and is_protected_path(entry_path): continue
                            stat = entry.stat(follow_symlinks=False)
                            if stat.st_size >= min_size:
                                inode_id = (stat.st_dev, stat.st_ino)
                                if inode_id not in visited and _is_valid_candidate(entry_path, stat.st_size):
                                    visited.add(inode_id)
                                    size_map[stat.st_size].append(entry_path)
                    except (OSError, PermissionError):
                        continue
    return {sz: ps for sz, ps in size_map.items() if len(ps) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Agrupa una lista de rutas basándose en el resultado de la función de hash suministrada."""
    groups: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if path is not None and (d := hash_func(path)): groups[d].append(path)
    return {d: p for d, p in groups.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Aplica estrategia de reducción mediante doble hash (parcial luego completo)."""
    partial_groups: Dict[str, List[Path]] = _group_paths_by_hash(paths, partial_hash)
    final: Dict[str, List[Path]] = {}
    for subset in partial_groups.values():
        final.update(_group_paths_by_hash(subset, hash_file))
    return final


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Determina si es necesario el hashing parcial según el tamaño del archivo."""
    if size <= PARTIAL_READ_BYTES:
        results: Dict[str, List[Path]] = _group_paths_by_hash(paths, hash_file)
    else:
        results: Dict[str, List[Path]] = _process_large_file_subset(paths)
    return [DuplicateGroup(d, size, sorted(p)) for d, p in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Función de entrada principal para la detección, gestionando cachés y orquestación."""
    if not directories: return []
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups = [g for sz, ps in size_map.items() for g in _decide_hash_strategy_and_process(sz, ps)]
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el espacio total que se liberaría si se eliminaran los duplicados."""
    return sum(g.wasted_bytes for g in groups)


def _get_mtime(path: Path) -> Optional[float]:
    """Obtiene el timestamp de modificación o retorna None si falla el acceso."""
    try:
        return path.stat().st_mtime
    except (OSError, PermissionError):
        return None


def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """
    Heurística: sugiere conservar el archivo más antiguo (menor mtime).
    """
    if not group or not group.paths: return None
    candidates = [(m, p) for p in group.paths if (m := _get_mtime(p)) is not None]
    return min(candidates, key=lambda x: x[0])[1] if candidates else group.paths[0]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una representación textual formateada de un grupo de duplicados para la UI."""
    if not group or not group.paths: return []
    keeper = suggest_keeper(group)
    mb, wasted = group.size_bytes / 1048576, group.wasted_bytes / 1048576
    lines = [f"{group.count} copias de {mb:.2f} MB (recuperable: {wasted:.2f} MB)"]
    for p in group.paths:
        status = "[conservar]" if (p == keeper) else "[duplicado]"
        lines.append(f"   {status} {p}")
    return lines
