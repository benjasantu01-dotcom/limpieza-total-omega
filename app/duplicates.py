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

_partial_cache: Dict[Path, str] = {}
_full_hash_cache: Dict[Path, str] = {}

def is_junction(path: Path) -> bool:
    """Verifica si el path es un punto de reanálisis (Junction o Symlink) usando Win32 API."""
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
        return len(self.paths)

    @property
    def wasted_bytes(self) -> int:
        return (self.count - 1) * self.size_bytes if self.count > 1 else 0


def _safe_path_check(path: Path) -> bool:
    """Valida la integridad de un path frente a restricciones de seguridad."""
    return is_safe_to_modify(path) and not is_protected_path(path) and not is_junction(path)


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Filtra archivos (excluye sistemas/ocultos) que poseen contenido procesable."""
    return st_size > 0 and not is_system_or_hidden(path)


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo del archivo, utilizando caché para rendimiento."""
    if path in _full_hash_cache: return _full_hash_cache[path]
    if not path.is_file() or not _safe_path_check(path): return None
    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while (chunk := f.read(chunk_size)):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        _full_hash_cache[path] = digest
        return digest
    except (OSError, PermissionError):
        return None


def partial_hash(path: Path, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula un hash SHA256 basado solo en los primeros bytes para filtrado rápido."""
    if path in _partial_cache: return _partial_cache[path]
    if not path.is_file() or not _safe_path_check(path): return None
    try:
        with open(path, "rb") as f:
            if not (header := f.read(read_bytes)): return None
            digest_val = hashlib.sha256(header).hexdigest()
            _partial_cache[path] = digest_val
            return digest_val
    except (OSError, PermissionError):
        return None


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa rutas según el tamaño en bytes, filtrando elementos no procesables."""
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    for item in paths:
        if not item: continue
        path = Path(item)
        if _safe_path_check(path) and path.is_file():
            try:
                st = path.stat()
                if _is_valid_candidate(path, st.st_size):
                    groups_by_size[st.st_size].append(path)
            except OSError: continue
    return groups_by_size


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """
    Recorre recursivamente directorios para obtener archivos candidatos.
    Implementa un registro de inodos (dev, ino) para prevenir el procesamiento de enlaces duros
    y normaliza rutas para evitar escapes de directorio.
    """
    if not directories: return {}
    raw_map: Dict[int, List[Path]] = defaultdict(list)
    queue: deque[Tuple[Path, int]] = deque()
    
    for d in directories:
        if d:
            try:
                p = Path(os.path.realpath(d))
                if p.exists() and _safe_path_check(p):
                    queue.append((p, 0))
            except OSError: continue
            
    visited: set[Tuple[int, int]] = set()
    
    while queue:
        current_dir, depth = queue.popleft()
        if depth > MAX_RECURSION_DEPTH:
            continue
            
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    path = Path(entry.path)
                    if not _safe_path_check(path):
                        continue
                        
                    if entry.is_dir(follow_symlinks=False):
                        queue.append((Path(os.path.realpath(path)), depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        if stat.st_size >= min_size and _is_valid_candidate(path, stat.st_size):
                            inode = (stat.st_dev, stat.st_ino)
                            if inode not in visited:
                                if not (skip_protected and is_protected_path(path)):
                                    visited.add(inode)
                                    raw_map[stat.st_size].append(path)
        except (OSError, PermissionError): continue
    return {sz: paths for sz, paths in raw_map.items() if len(paths) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica una función de hash a una lista de rutas y agrupa los resultados coincidentes."""
    groups: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (d := hash_func(path)): groups[d].append(path)
    return {d: p for d, p in groups.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Refina grupos de archivos grandes mediante hashing en dos etapas (parcial -> completo)."""
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final: Dict[str, List[Path]] = {}
    for subset in partial_groups.values():
        final.update(_group_paths_by_hash(subset, hash_file))
    return final


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona la estrategia de hash: evita lecturas pesadas innecesarias si el archivo es pequeño."""
    if size <= PARTIAL_READ_BYTES:
        results = _group_paths_by_hash(paths, hash_file)
    else:
        results = _process_large_file_subset(paths)
    return [DuplicateGroup(d, size, sorted(p)) for d, p in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Flujo principal: identifica, agrupa y verifica duplicados en las rutas dadas."""
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups = [g for sz, ps in size_map.items() for g in _decide_hash_strategy_and_process(sz, ps)]
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el total de bytes que podrían liberarse si se eliminan las copias sobrantes."""
    return sum(g.wasted_bytes for g in groups)


def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """Sugiere el archivo más antiguo del grupo para mantener como original (protección de datos)."""
    if not group or not group.paths: return None
    candidates = []
    for p in group.paths:
        try: candidates.append((p.stat().st_mtime, p))
        except OSError: continue
    return min(candidates, key=lambda x: x[0])[1] if candidates else group.paths[0]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una representación legible del grupo para el informe de salida."""
    keeper = suggest_keeper(group)
    mb = round(group.size_bytes / 1048576, 2)
    wasted = round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb} MB (recuperable: {wasted} MB)"]
    lines.extend(f"   {'[conservar]' if p == keeper else '[duplicado]'} {p}" for p in group.paths)
    return lines
