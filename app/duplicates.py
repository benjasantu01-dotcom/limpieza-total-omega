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
import io
import ctypes
from collections import defaultdict, deque
from collections.abc import Sequence, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union, Callable, TypeAlias, Tuple

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
MAX_PATH_LIMIT: int = 260


def is_junction(path: Path) -> bool:
    """
    Verifica mediante la API de Windows si una ruta es un punto de reanálisis.
    """
    if not isinstance(path, Path) or not is_safe_to_modify(path):
        return False
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError, RuntimeError):
        return False


def is_system_or_hidden(path: Path) -> bool:
    """
    Determina si un archivo tiene atributos de sistema u oculto en Windows.
    """
    if not isinstance(path, Path) or not is_safe_to_modify(path):
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
    """
    Contenedor de datos para grupos de archivos idénticos.
    """
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Número de archivos en el grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """Calcula el espacio total recuperable excluyendo una copia."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """
    Verifica si un archivo está en uso intentando un acceso de lectura.
    """
    if not isinstance(path, Path) or not is_safe_to_modify(path):
        return True
    try:
        with open(path, "rb") as f:
            f.peek(1)
            return False
    except (PermissionError, OSError, ValueError, io.UnsupportedOperation):
        return True


def _safe_path_check(path: Path) -> bool:
    """
    Validación de seguridad: verifica protección, puntos de reanálisis y symlinks.
    """
    if not isinstance(path, Path):
        return False
    try:
        return (is_safe_to_modify(path) and 
                not is_protected_path(path) and 
                not is_junction(path) and 
                not path.is_symlink())
    except (OSError, RuntimeError):
        return False


def _validate_and_resolve_path(path_input: PathLike) -> Optional[Path]:
    """
    Normaliza y valida que una ruta sea un archivo procesable.
    """
    if not path_input:
        return None
    try:
        path_obj: Path = Path(path_input).resolve()
        if path_obj.is_file() and _safe_path_check(path_obj) and not _is_file_locked(path_obj):
            if path_obj.stat().st_size > 0:
                return path_obj
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del archivo usando buffers de memoria controlados.
    """
    valid_path: Optional[Path] = _validate_and_resolve_path(path)
    if valid_path is None:
        return None
            
    try:
        digest = hashlib.sha256()
        with open(valid_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PermissionError, MemoryError, ValueError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Genera el hash SHA256 solo de los primeros N bytes para filtrado rápido.
    """
    valid_path: Optional[Path] = _validate_and_resolve_path(path)
    if valid_path is None:
        return None

    try:
        with open(valid_path, "rb") as f:
            content = f.read(read_bytes)
            if not content: 
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, ValueError):
        return None


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Filtro de pre-selección para evitar procesar archivos bloqueados o protegidos."""
    if st_size <= 0:
        return False
    try:
        return (_safe_path_check(path) and 
                not is_system_or_hidden(path) and 
                not _is_file_locked(path))
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError):
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa rutas según el tamaño del archivo."""
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
    """Valida si un directorio es navegable y seguro."""
    if not directory_path: return None
    try:
        root_obj = Path(directory_path).resolve()
        if root_obj.is_dir() and _safe_path_check(root_obj):
            return root_obj
    except (OSError, ValueError, RuntimeError, TypeError):
        return None
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Recorre rutas usando BFS evitando ciclos y redundancias de inodos."""
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
                            if not (skip_protected and is_protected_path(Path(entry.path))):
                                queue.append((Path(entry.path), depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat()
                            if st.st_size >= min_size and (st.st_dev, st.st_ino) not in visited_inodes:
                                file_path = Path(entry.path)
                                if _is_valid_candidate(file_path, st.st_size):
                                    visited_inodes.add((st.st_dev, st.st_ino))
                                    size_to_paths_map[st.st_size].append(file_path)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
            
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Helper para agrupar rutas mediante una función de hash proporcionada."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if is_safe_to_modify(path) and (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Ejecuta filtrado jerárquico: hash parcial seguido de confirmación completa."""
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final_results: Dict[str, List[Path]] = {}
    for candidate_subset in partial_groups.values():
        final_results.update(_group_paths_by_hash(candidate_subset, hash_file))
    return final_results


def _decide_hash_strategy_and_process(size_bytes: int, file_paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona el método de hashing más eficiente según el tamaño del archivo."""
    if not file_paths or size_bytes <= 0:
        return []

    if size_bytes <= PARTIAL_READ_BYTES:
        final_groups = _group_paths_by_hash(file_paths, hash_file)
    else:
        final_groups = _process_large_file_subset(file_paths)
    return [DuplicateGroup(d, size_bytes, sorted(p)) for d, p in final_groups.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquesta la detección de archivos duplicados y calcula el impacto de espacio."""
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Total de bytes recuperables sumando el exceso de todos los grupos."""
    return sum(g.wasted_bytes for g in groups)


def _calculate_keeper_heuristic(path: Path) -> Optional[Tuple[float, int]]:
    """Calcula score de antigüedad y longitud de ruta para sugerir el 'original'."""
    if not isinstance(path, Path):
        return None
    try:
        if not path.is_file():
            return None
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, PermissionError, ValueError, AttributeError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Selecciona el mejor archivo candidato a preservar basándose en métricas."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    
    candidates: List[Tuple[Tuple[float, int], Path]] = []
    for p in group.paths:
        if isinstance(p, Path):
            if (score := _calculate_keeper_heuristic(p)) is not None:
                candidates.append((score, p))
            
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def _get_path_label(path: Path, keeper: Optional[Path]) -> str:
    """Devuelve una etiqueta legible según el estado de la ruta en el grupo."""
    if not isinstance(path, Path) or not path.is_file():
        return "[desaparecido]"
    if not _safe_path_check(path):
        return "[inaccesible]"
    if keeper is not None and path == keeper:
        return "[conservar]"
    return "[duplicado]"


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera informe textual del grupo para la interfaz de usuario."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return ["Error: Grupo inválido o vacío"]
        
    keeper = suggest_keeper(group)
    mb_t, mb_w = round(group.size_bytes / 1048576, 2), round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_t} MB (recuperable: {mb_w} MB)"]
    
    for path in group.paths:
        if not isinstance(path, Path):
            continue
        label = _get_path_label(path, keeper)
        lines.append(f"   {label} {path}")
    return lines
