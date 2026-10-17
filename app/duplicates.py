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

_partial_cache: Dict[Path, str] = {}

def is_junction(path: Path) -> bool:
    """
    Verifica si la ruta es un punto de reanálisis (Junction/Symlink).
    
    Utiliza Win32 API para identificar si el archivo es un punto de montaje o enlace 
    simbólico, evitando seguir estructuras que podrían causar bucles infinitos.
    """
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
        """
        Calcula el espacio total recuperable.
        
        Se asume que uno de los archivos del grupo es el 'original' y debe preservarse, 
        por lo tanto el espacio recuperable es (Total - 1) * tamaño.
        """
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """
    Verifica si el archivo está en uso exclusivo mediante sondeo de E/S.
    
    Intenta abrir el archivo en modo lectura compartida. Si el sistema operativo
    deniega el acceso por violaciones de compartición, se marca como bloqueado.
    """
    if not is_safe_to_modify(path):
        return True
    try:
        if not path.is_file() or not os.access(path, os.R_OK):
            return True
        with open(path, "rb") as f:
            f.peek(1)
            return False
    except OSError as e:
        if hasattr(e, 'winerror') and e.winerror in (ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED):
            return True
        return True
    except (IOError, PermissionError, ValueError):
        return True


def _safe_path_check(path: Path) -> bool:
    """
    Valida la integridad y seguridad de la ruta antes de cualquier operación.
    
    Combina validación lógica contra listas protegidas, detección de puntos de
    reanálisis y resolución de rutas para evitar escapes fuera de sandbox.
    """
    try:
        # Resolvemos la ruta antes de verificar para evitar bypass por '..'
        resolved = path.resolve()
        return (is_safe_to_modify(path) and 
                not is_protected_path(path) and 
                not is_junction(path) and
                is_safe_to_modify(resolved))
    except (OSError, RuntimeError, PermissionError, AttributeError):
        return False


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Predicado de filtrado para determinar si un archivo es apto para el análisis."""
    if st_size <= 0 or not path.exists():
        return False
    # Verificación estricta de seguridad defensiva
    if is_protected_path(path) or not _safe_path_check(path) or is_system_or_hidden(path):
        return False
    if _is_file_locked(path):
        return False
    return True


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo de un archivo mediante streaming.
    
    Utiliza lectura por bloques configurables para minimizar el consumo de memoria 
    en archivos de gran tamaño.
    """
    if chunk_size <= 0 or not path.is_file() or not is_safe_to_modify(path):
        return None
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            while (chunk := f.read(chunk_size)):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, IOError, PermissionError, ValueError):
        return None


def partial_hash(path: Path, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Calcula el hash SHA256 de los primeros N bytes del archivo.
    
    Implementa un caché interno `_partial_cache` para optimizar el paso de 
    filtrado en el pipeline de comparación de duplicados.
    """
    if path in _partial_cache:
        return _partial_cache[path]
    if read_bytes <= 0 or not path.is_file() or not is_safe_to_modify(path):
        return None
    try:
        with open(path, "rb") as f:
            content = f.read(read_bytes)
            if not content:
                return None
            res = hashlib.sha256(content).hexdigest()
            _partial_cache[path] = res
            return res
    except (OSError, IOError, PermissionError, ValueError):
        return None


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa rutas por su tamaño en bytes para descartar archivos únicos."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if not p: continue
        try:
            path_obj = Path(p).resolve()
            if path_obj.exists() and path_obj.is_file() and is_safe_to_modify(path_obj):
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
        if root_obj.exists() and root_obj.is_dir() and _safe_path_check(root_obj):
            return root_obj
    except (OSError, ValueError, RuntimeError, TypeError):
        return None
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """
    Recolección BFS de archivos candidatos para el análisis de duplicados.
    
    Utiliza un conjunto de inodos (dev, ino) para evitar el procesamiento redundante
    de hardlinks o el seguimiento de bucles circulares de directorios.
    """
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    queue: deque[Tuple[Path, int]] = deque()
    visited_inodes: set[Tuple[int, int]] = set()

    for d in directories:
        if (root := _resolve_and_verify_root(d)):
            queue.append((root, 0))
    
    while queue:
        current_dir, depth = queue.popleft()
        if depth > MAX_RECURSION_DEPTH:
            continue
            
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    p_entry = Path(entry.path)
                    if not _safe_path_check(p_entry):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            queue.append((p_entry, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat()
                            if st.st_size < min_size or (st.st_dev, st.st_ino) in visited_inodes:
                                continue
                            if not skip_protected and is_protected_path(p_entry):
                                continue
                            if _is_valid_candidate(p_entry, st.st_size):
                                visited_inodes.add((st.st_dev, st.st_ino))
                                size_to_paths_map[st.st_size].append(p_entry)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica una función de hashing para generar grupos de colisión."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if path.exists() and is_safe_to_modify(path) and (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Pipeline jerárquico: Refina candidatos mediante hash parcial seguido de completo."""
    partial_groups: Dict[str, List[Path]] = _group_paths_by_hash(paths, partial_hash)
    final_results: Dict[str, List[Path]] = {}
    for candidate_subset in partial_groups.values():
        final_results.update(_group_paths_by_hash(candidate_subset, hash_file))
    return final_results


def _decide_hash_strategy_and_process(size_bytes: int, file_paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona la estrategia de hashing según el tamaño del archivo."""
    if not file_paths or size_bytes <= 0: return []
    if size_bytes <= PARTIAL_READ_BYTES:
        final_groups = _group_paths_by_hash(file_paths, hash_file)
    else:
        final_groups = _process_large_file_subset(file_paths)
    return [DuplicateGroup(d, size_bytes, sorted(p)) for d, p in final_groups.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquesta el pipeline completo: recolección, filtrado y hashing jerárquico."""
    _partial_cache.clear()
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
    """Calcula métricas de antigüedad y longitud de ruta para selección de keeper."""
    if not isinstance(path, Path) or not is_safe_to_modify(path): return None
    try:
        if not path.exists(): return None
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, AttributeError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Sugiere el 'original' basándose en la menor antigüedad del archivo."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    candidates: List[Tuple[Tuple[float, int], Path]] = []
    for p in group.paths:
        if not isinstance(p, Path): continue
        score = _calculate_keeper_heuristic(p)
        if score is not None:
            candidates.append((score, p))
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def _get_path_label(path: Path, keeper: Optional[Path]) -> str:
    """Etiqueta visual para distinguir entre el archivo keeper y las réplicas."""
    return "[conservar]" if keeper is not None and path == keeper else "[duplicado]"


def format_group(group: DuplicateGroup) -> List[str]:
    """Convierte un objeto DuplicateGroup en líneas de texto formateadas."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return ["Error: Grupo inválido o vacío"]
    keeper = suggest_keeper(group)
    mb_t, mb_w = round(group.size_bytes / 1048576, 2), round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_t} MB (recuperable: {mb_w} MB)"]
    for path in group.paths:
        if isinstance(path, Path):
            lines.append(f"   {_get_path_label(path, keeper)} {path}")
    return lines
