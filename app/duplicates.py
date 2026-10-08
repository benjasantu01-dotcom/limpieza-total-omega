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
    Los puntos de reanálisis (Junctions/Symlinks) se ignoran para prevenir
    recursión infinita o escaneo de unidades externas montadas.
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
    El escaneo debe evitar estos archivos para no comprometer la estabilidad
    del sistema operativo ni alterar archivos de configuración críticos.
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
    """Representa un conjunto de archivos con contenido idéntico agrupados por su digest (hash)."""
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Retorna el número de archivos encontrados en este grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """Calcula el espacio total que se liberaría si se conservara solo uno (N-1 archivos)."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """
    Verifica si un archivo está bloqueado intentando una lectura no destructiva.
    Si el archivo está abierto en modo exclusivo por otro proceso (ej: sistema),
    se considera bloqueado y se excluye del análisis para evitar errores de E/S.
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
    """Valida si la ruta es apta para análisis, excluyendo protegidas, junctions o enlaces simbólicos."""
    if not isinstance(path, Path):
        return False
    try:
        return (is_safe_to_modify(path) and 
                not is_protected_path(path) and 
                not is_junction(path) and 
                not path.is_symlink())
    except (OSError, RuntimeError):
        return False


def _validate_and_resolve_path(path: PathLike) -> Optional[Path]:
    """Normaliza y valida que la ruta sea un archivo accesible, no nulo y con tamaño positivo."""
    if not path:
        return None
    try:
        p: Path = Path(path).resolve()
        if p.is_file() and _safe_path_check(p) and not _is_file_locked(p):
            if p.stat().st_size > 0:
                return p
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo del archivo tras verificar su integridad y acceso."""
    p: Optional[Path] = _validate_and_resolve_path(path)
    if not p:
        return None
            
    try:
        digest = hashlib.sha256()
        with open(p, "rb") as f:
            while (chunk := f.read(chunk_size)):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PermissionError, EOFError, MemoryError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Genera un hash SHA256 de los primeros N bytes para optimizar el filtrado de candidatos."""
    p: Optional[Path] = _validate_and_resolve_path(path)
    if not p:
        return None

    try:
        with open(p, "rb") as f:
            content = f.read(read_bytes)
            if not content: 
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, EOFError):
        return None


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Valida que un archivo sea apto para duplicación descartando sistemas, ocultos y bloqueados."""
    if st_size <= 0:
        return False
    try:
        if not _safe_path_check(path):
            return False
        return not is_system_or_hidden(path) and not _is_file_locked(path)
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError):
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa rutas por su tamaño en bytes, filtrando elementos que violan reglas de seguridad."""
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


def _resolve_and_verify_root(item: PathLike) -> Optional[Path]:
    """Resuelve la ruta raíz del directorio y verifica si está permitida por seguridad."""
    if not item: return None
    try:
        root = Path(item).resolve()
        if root.is_dir() and _safe_path_check(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        return None
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """
    Recorre jerárquicamente las rutas usando BFS, evitando procesar inodos duplicados o niveles profundos.
    Retorna un diccionario mapeando tamaños de archivo a listas de rutas candidatas.
    """
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    queue: deque[Tuple[Path, int]] = deque()
    visited_dirs: set[Path] = set()
    visited_inodes: set[Tuple[int, int]] = set()

    for d in directories:
        if (r := _resolve_and_verify_root(d)):
            queue.append((r, 0))
    
    while queue:
        current_dir, depth = queue.popleft()
        if current_dir in visited_dirs or depth > MAX_RECURSION_DEPTH:
            continue
        visited_dirs.add(current_dir)
            
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        p_entry = Path(entry.path)
                        if not _safe_path_check(p_entry) or (skip_protected and is_protected_path(p_entry)):
                            continue
                        
                        if entry.is_dir(follow_symlinks=False):
                            queue.append((p_entry, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat()
                            if st.st_size >= min_size:
                                inode_id = (st.st_dev, st.st_ino)
                                if inode_id not in visited_inodes and not is_system_or_hidden(p_entry) and not _is_file_locked(p_entry):
                                    visited_inodes.add(inode_id)
                                    size_to_paths_map[st.st_size].append(p_entry)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
            
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica una función de hash a rutas y agrupa las que colisionan (posibles duplicados)."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Refina los grupos: primero filtrado por hash parcial (64KB) y luego confirmación por hash completo."""
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final_results: Dict[str, List[Path]] = {}
    for candidate_subset in partial_groups.values():
        final_results.update(_group_paths_by_hash(candidate_subset, hash_file))
    return final_results


def _decide_hash_strategy_and_process(size_bytes: int, file_paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona el flujo de hashing óptimo según el tamaño: evita hash parcial para archivos pequeños."""
    if not file_paths or size_bytes <= 0:
        return []

    final_groups = _group_paths_by_hash(file_paths, hash_file) if size_bytes <= PARTIAL_READ_BYTES else _process_large_file_subset(file_paths)
    return [DuplicateGroup(d, size_bytes, sorted(p)) for d, p in final_groups.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """
    Función de entrada principal para detectar duplicados.
    Orquesta la recolección, agrupación y refinamiento mediante hashing.
    """
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula la suma total de espacio recuperable (en bytes) de los grupos detectados."""
    return sum(g.wasted_bytes for g in groups)


def _calculate_keeper_heuristic(path: Path) -> Optional[Tuple[float, int]]:
    """
    Calcula una métrica de 'originalidad' basada en la antigüedad (mtime) y longitud de ruta.
    Retorna un puntaje (mtime, longitud_path) que se usa para elegir el original.
    """
    if not isinstance(path, Path):
        return None
    try:
        # Validación de que el archivo sigue existiendo antes de statear
        if not path.is_file():
            return None
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, PermissionError, ValueError, AttributeError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Sugiere el archivo más adecuado para conservar dentro de un grupo de duplicados."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    
    candidates: List[Tuple[Tuple[float, int], Path]] = []
    for p in group.paths:
        if isinstance(p, Path):
            if (score := _calculate_keeper_heuristic(p)) is not None:
                candidates.append((score, p))
            
    # Elegimos el más antiguo (menor mtime) como original/keeper
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def _get_path_label(path: Path, keeper: Optional[Path]) -> str:
    """Retorna una etiqueta descriptiva para la interfaz de usuario según el rol del archivo."""
    if not isinstance(path, Path) or not path.is_file():
        return "[desaparecido]"
    if not _safe_path_check(path):
        return "[inaccesible]"
    if keeper is not None and path == keeper:
        return "[conservar]"
    return "[duplicado]"


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una representación de texto del grupo para logs o informes de usuario."""
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
