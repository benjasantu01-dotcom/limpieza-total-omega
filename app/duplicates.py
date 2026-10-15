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

PARTIAL_READ_BYTES: int = 64 * 1024
FILE_ATTRIBUTE_REPARSE_POINT: int = 0x400
FILE_ATTRIBUTE_HIDDEN: int = 0x2
FILE_ATTRIBUTE_SYSTEM: int = 0x4
MAX_RECURSION_DEPTH: int = 100
ERROR_SHARING_VIOLATION: int = 32
ERROR_ACCESS_DENIED: int = 5


def is_junction(path: Path) -> bool:
    """Verifica si la ruta es un punto de reanálisis (Junction/Symlink)."""
    if not is_safe_to_modify(path):
        return True
    if path.is_symlink():
        return True
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError, RuntimeError):
        return False


def is_system_or_hidden(path: Path) -> bool:
    """Verifica si un archivo posee atributos de sistema u oculto mediante Win32 API."""
    if not is_safe_to_modify(path):
        return True
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == -1:
            return False
        return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))
    except (AttributeError, OSError, RuntimeError):
        return False


@dataclass
class DuplicateGroup:
    """Contenedor de datos para grupos de archivos con contenido idéntico."""
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Devuelve el número total de archivos en el grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """Calcula el espacio total recuperable excluyendo la copia original."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """
    Intenta validar si el archivo es accesible para lectura exclusiva.
    Retorna True si el archivo está bloqueado por otro proceso o inaccesible.
    """
    if not is_safe_to_modify(path):
        return True
    try:
        if not os.access(path, os.R_OK):
            return True
        with open(path, "rb") as f:
            return False
    except (OSError, IOError) as e:
        if isinstance(e, OSError) and e.winerror in (ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED):
            return True
        return True


def _safe_path_check(path: Path) -> bool:
    """
    Gatekeeper principal: valida que la ruta sea segura, no protegida por el
    sistema y no sea una estructura compleja (junction) que cause bucles.
    """
    try:
        if not path.exists():
            return False
        return (is_safe_to_modify(path) and 
                not is_protected_path(path) and 
                not is_junction(path))
    except (OSError, RuntimeError, PermissionError, AttributeError):
        return False


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Filtra archivos inválidos, bloqueados, ocultos o de sistema antes del análisis."""
    if st_size <= 0:
        return False
    if is_protected_path(path) or not _safe_path_check(path) or is_system_or_hidden(path):
        return False
    if _is_file_locked(path):
        return False
    return True


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el SHA256 completo de un archivo mediante lectura en bloques (streaming)."""
    if chunk_size <= 0 or not is_safe_to_modify(path):
        return None
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            while (chunk := f.read(chunk_size)):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, IOError, PermissionError):
        return None


def partial_hash(path: Path, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula un hash SHA256 sobre el segmento inicial del archivo para optimización."""
    if read_bytes <= 0 or not is_safe_to_modify(path):
        return None
    try:
        with open(path, "rb") as f:
            content = f.read(read_bytes)
            return hashlib.sha256(content).hexdigest() if content else None
    except (OSError, IOError, PermissionError):
        return None


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Paso 1: Agrupa rutas por su tamaño en bytes para descartar archivos únicos."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if not p: continue
        try:
            path_obj = Path(p).resolve()
            if path_obj.is_file():
                st_size = path_obj.stat().st_size
                if _is_valid_candidate(path_obj, st_size):
                    groups[st_size].append(path_obj)
        except (OSError, ValueError, TypeError):
            continue
    return groups


def _resolve_and_verify_root(directory_path: PathLike) -> Optional[Path]:
    """Valida si la ruta raíz de inicio es un directorio navegable y seguro."""
    if not directory_path: return None
    try:
        root_obj = Path(directory_path).resolve()
        if root_obj.is_dir() and _safe_path_check(root_obj):
            return root_obj
    except (OSError, ValueError, RuntimeError, TypeError):
        return None
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Recolección BFS de archivos candidatos, evitando recursión infinita y ciclos."""
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    queue: deque[Tuple[Path, int]] = deque()
    visited_dirs: set[Path] = set()
    visited_inodes: set[Tuple[int, int]] = set()

    for d in directories:
        if (root := _resolve_and_verify_root(d)):
            queue.append((root, 0))
    
    while queue:
        current_dir, depth = queue.popleft()
        if current_dir in visited_dirs or depth > MAX_RECURSION_DEPTH:
            continue
        visited_dirs.add(current_dir)
            
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not entry.is_symlink():
                                p_entry = Path(entry.path)
                                if _safe_path_check(p_entry):
                                    queue.append((p_entry, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            p_entry = Path(entry.path)
                            if not skip_protected and is_protected_path(p_entry):
                                continue
                            st = entry.stat()
                            if st.st_size >= min_size and (st.st_dev, st.st_ino) not in visited_inodes:
                                if _is_valid_candidate(p_entry, st.st_size):
                                    visited_inodes.add((st.st_dev, st.st_ino))
                                    size_to_paths_map[st.st_size].append(p_entry)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica una función de hashing (parcial o total) para agrupar candidatos colisionados."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Pipeline de refinamiento: reduce candidatos usando hash parcial y confirma con SHA256."""
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final_results: Dict[str, List[Path]] = {}
    for candidate_subset in partial_groups.values():
        final_results.update(_group_paths_by_hash(candidate_subset, hash_file))
    return final_results


def _decide_hash_strategy_and_process(size_bytes: int, file_paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona entre hashing directo o procesado multietapa según el tamaño del archivo."""
    if not file_paths or size_bytes <= 0: return []
    if size_bytes <= PARTIAL_READ_BYTES:
        final_groups = _group_paths_by_hash(file_paths, hash_file)
    else:
        final_groups = _process_large_file_subset(file_paths)
    return [DuplicateGroup(d, size_bytes, sorted(p)) for d, p in final_groups.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquesta el pipeline completo de detección: recolección, filtrado y hashing."""
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma el total de bytes que se recuperarían tras procesar los grupos."""
    return sum(g.wasted_bytes for g in groups)


def _calculate_keeper_heuristic(path: Path) -> Optional[Tuple[float, int]]:
    """Calcula métricas de selección: (timestamp de modificación, longitud de ruta)."""
    try:
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, AttributeError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Sugiere el archivo 'original' (keeper) basado en el criterio de mayor antigüedad."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    candidates: List[Tuple[Tuple[float, int], Path]] = []
    for p in group.paths:
        score = _calculate_keeper_heuristic(p)
        if score is not None:
            candidates.append((score, p))
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def _get_path_label(path: Path, keeper: Optional[Path]) -> str:
    """Genera etiqueta para diferenciar visualmente el archivo keeper de las réplicas."""
    return "[conservar]" if keeper is not None and path == keeper else "[duplicado]"


def format_group(group: DuplicateGroup) -> List[str]:
    """Transforma un objeto DuplicateGroup en una lista de strings para informe visual."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return ["Error: Grupo inválido o vacío"]
    keeper = suggest_keeper(group)
    mb_t, mb_w = round(group.size_bytes / 1048576, 2), round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_t} MB (recuperable: {mb_w} MB)"]
    for path in group.paths:
        lines.append(f"   {_get_path_label(path, keeper)} {path}")
    return lines
