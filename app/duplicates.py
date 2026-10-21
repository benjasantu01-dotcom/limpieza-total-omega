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
ERROR_SHARING_VIOLATION: int = 32
ERROR_ACCESS_DENIED: int = 5

_partial_cache: Dict[Path, str] = {}
_full_hash_cache: Dict[Path, str] = {}

def is_junction(path: Path) -> bool:
    """Valida si un path es un punto de reanálisis (Junction/Symlink)."""
    if path.is_symlink():
        return True
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError, RuntimeError):
        return False


def is_system_or_hidden(path: Path) -> bool:
    """Determina si un archivo tiene atributos de sistema u oculto mediante Win32 API."""
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == -1:
            return False
        return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))
    except (AttributeError, OSError, RuntimeError):
        return False


@dataclass
class DuplicateGroup:
    """Colección de archivos con idéntico contenido hash."""
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Cantidad de archivos en el grupo."""
        return len(self.paths)

    @property
    def wasted_bytes(self) -> int:
        """Espacio redundante (total archivos - 1) * tamaño."""
        if self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """Verifica si un archivo puede abrirse para lectura exclusiva."""
    if not is_safe_to_modify(path):
        return True
    # Verificación preliminar de acceso sin intentar abrir el handle
    if not os.access(path, os.R_OK):
        return True
    try:
        with open(path, "rb") as f:
            f.peek(1)
            return False
    except (OSError, IOError, PermissionError, ValueError):
        return True


def _safe_path_check(path: Path) -> bool:
    """Verifica si el path es seguro, no protegido y no es un junction."""
    try:
        resolved = path.resolve()
        return (is_safe_to_modify(path) and 
                not is_protected_path(path) and 
                not is_junction(path) and
                is_safe_to_modify(resolved))
    except (OSError, RuntimeError, PermissionError, AttributeError):
        return False


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Filtra archivos válidos para análisis de duplicados."""
    return (st_size > 0 and 
            is_safe_to_modify(path) and 
            not is_system_or_hidden(path) and 
            not _is_file_locked(path))


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo de un archivo."""
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
    except (OSError, PermissionError, IOError):
        return None


def partial_hash(path: Path, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula el hash SHA256 de los primeros 64KB (filtrado rápido)."""
    if path in _partial_cache: return _partial_cache.get(path)
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
    """Agrupa paths válidos por tamaño de archivo en bytes."""
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    for item in paths:
        path = Path(item)
        if path.is_file() and _safe_path_check(path):
            try:
                st_size = path.stat().st_size
                if _is_valid_candidate(path, st_size):
                    groups_by_size[st_size].append(path)
            except OSError:
                continue
    return groups_by_size


def _resolve_and_verify_root(directory_path: PathLike) -> Optional[Path]:
    """Valida y resuelve el directorio base para el escaneo."""
    try:
        root = Path(directory_path).resolve()
        return root if root.is_dir() and _safe_path_check(root) else None
    except (OSError, ValueError, TypeError):
        return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Recolecta archivos candidatos mediante BFS controlado."""
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    queue: deque[Tuple[Path, int]] = deque((root, 0) for d in directories if (root := _resolve_and_verify_root(d)))
    visited_inodes: set[Tuple[int, int]] = set()
    
    while queue:
        current_dir, depth = queue.popleft()
        if depth > MAX_RECURSION_DEPTH: continue
            
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    # Garantizar path absoluto para validación de seguridad
                    path = Path(entry.path).resolve()
                    if not is_safe_to_modify(path): continue
                    
                    if entry.is_dir(follow_symlinks=False):
                        if not is_junction(path): queue.append((path, depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        if stat.st_size >= min_size:
                            inode = (stat.st_dev, stat.st_ino)
                            if inode not in visited_inodes and _safe_path_check(path):
                                if not (skip_protected and is_protected_path(path)) and _is_valid_candidate(path, stat.st_size):
                                    visited_inodes.add(inode)
                                    size_to_paths_map[stat.st_size].append(path)
        except (OSError, PermissionError): continue
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Agrupa una lista de paths por el resultado de una función de hash."""
    groups: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups[digest].append(path)
    return {d: p for d, p in groups.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Refina grupos mediante hashing parcial seguido de hashing completo."""
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final_results: Dict[str, List[Path]] = {}
    for candidate_subset in partial_groups.values():
        final_results.update(_group_paths_by_hash(candidate_subset, hash_file))
    return final_results


def _decide_hash_strategy_and_process(size_bytes: int, file_paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona estrategia de hashing según el tamaño del archivo."""
    if not file_paths: return []
    if size_bytes <= PARTIAL_READ_BYTES:
        results = _group_paths_by_hash(file_paths, hash_file)
    else:
        results = _process_large_file_subset(file_paths)
    return [DuplicateGroup(d, size_bytes, sorted(p)) for d, p in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquesta la detección de duplicados en los directorios especificados."""
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma los bytes desperdiciados en todos los grupos."""
    return sum(g.wasted_bytes for g in groups)


def _calculate_keeper_heuristic(path: Path) -> Optional[Tuple[float, int]]:
    """Obtiene score (fecha de modificación, longitud ruta) para sugerir keeper."""
    try:
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, AttributeError):
        return None


def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """Sugiere el archivo más antiguo como original a conservar."""
    if not group.paths: return None
    candidates = [(s, p) for p in group.paths if (s := _calculate_keeper_heuristic(p))]
    return min(candidates, key=lambda x: x[0])[1] if candidates else group.paths[0]


def format_group(group: DuplicateGroup) -> List[str]:
    """Formatea la información del grupo para el reporte."""
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / 1048576, 2)
    mb_wasted = round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    for path in group.paths:
        label = "[conservar]" if path == keeper else "[duplicado]"
        lines.append(f"   {label} {path}")
    return lines
