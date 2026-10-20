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
FILE_ATTRIBUTE_REPARSE_POINT: int = 0x400  # Identifica puntos de montaje o junctions
FILE_ATTRIBUTE_HIDDEN: int = 0x2          # Archivo oculto
FILE_ATTRIBUTE_SYSTEM: int = 0x4          # Archivo de sistema
MAX_RECURSION_DEPTH: int = 100
ERROR_SHARING_VIOLATION: int = 32
ERROR_ACCESS_DENIED: int = 5

_partial_cache: Dict[Path, str] = {}
_full_hash_cache: Dict[Path, str] = {}

def is_junction(path: Path) -> bool:
    """
    Determina si un directorio es un punto de reanálisis (junction) o enlace simbólico.
    
    Usa la API de Windows para evitar recursión infinita en bucles del sistema.
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
    """
    Verifica los atributos de bajo nivel (oculto/sistema) mediante Win32 API.
    """
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == -1:
            return False
        return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))
    except (AttributeError, OSError, RuntimeError):
        return False


@dataclass
class DuplicateGroup:
    """Representa una colección de archivos con el mismo contenido (hash)."""
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Cantidad de archivos duplicados en este grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """Bytes que se liberarían si se eliminaran todas las copias menos una."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """
    Verifica si un archivo está en uso por otro proceso mediante intento de apertura exclusiva.
    """
    if not is_safe_to_modify(path):
        return True
    try:
        with open(path, "rb") as f:
            return False
    except OSError as e:
        if hasattr(e, 'winerror') and e.winerror in (ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED):
            return True
        return True
    except (IOError, PermissionError, ValueError):
        return True


def _safe_path_check(path: Path) -> bool:
    """
    Valida la seguridad de la ruta, verificando protecciones y puntos de reanálisis.
    
    Devuelve True solo si la ruta es accesible y no representa un riesgo sistémico.
    """
    try:
        resolved_path = path.resolve()
        return (is_safe_to_modify(path) and 
                not is_protected_path(path) and 
                not is_junction(path) and
                is_safe_to_modify(resolved_path))
    except (OSError, RuntimeError, PermissionError, AttributeError):
        return False


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """
    Filtro previo: valida si un archivo debe ser considerado para análisis.
    
    Excluye archivos ocultos, de sistema, bloqueados o de tamaño nulo.
    """
    if st_size <= 0:
        return False
    if is_system_or_hidden(path):
        return False
    if _is_file_locked(path):
        return False
    return True


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del archivo (Paso 3) leyendo en bloques de 1MB.
    
    Retorna el hash hexadecimal o None si el archivo es inaccesible o inseguro.
    """
    if not isinstance(path, Path): return None
    if path in _full_hash_cache:
        return _full_hash_cache[path]
    if chunk_size <= 0 or not path.is_file():
        return None
    if not _safe_path_check(path):
        return None
    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while (chunk := f.read(chunk_size)):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        _full_hash_cache[path] = digest
        return digest
    except (OSError, IOError, PermissionError, ValueError):
        return None


def partial_hash(path: Path, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Calcula el hash SHA256 de los primeros 64KB (Paso 2) usando caché de resultados.
    
    Es significativamente más rápido que el hash completo para archivos grandes.
    """
    if not isinstance(path, Path): return None
    if path in _partial_cache:
        return _partial_cache.get(path)
    if read_bytes <= 0 or not path.is_file():
        return None
    if not _safe_path_check(path):
        return None
    try:
        with open(path, "rb") as f:
            header_content = f.read(read_bytes)
            if not header_content:
                return None
            digest_val = hashlib.sha256(header_content).hexdigest()
            _partial_cache[path] = digest_val
            return digest_val
    except (OSError, IOError, PermissionError, ValueError):
        return None


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """
    Agrupa una lista de rutas por su tamaño en bytes (Paso 1).
    
    Solo incluye archivos válidos que pasaron los filtros de seguridad.
    """
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    if paths is None:
        return groups_by_size
    for path_str in paths:
        if not path_str: continue
        try:
            path_obj = Path(path_str)
            if path_obj.is_file() and _safe_path_check(path_obj):
                file_size = path_obj.stat().st_size
                if _is_valid_candidate(path_obj, file_size):
                    groups_by_size[file_size].append(path_obj)
        except (OSError, ValueError, TypeError):
            continue
    return groups_by_size


def _resolve_and_verify_root(directory_path: PathLike) -> Optional[Path]:
    """Valida que una ruta inicial sea un directorio existente, accesible y seguro."""
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
    Escaneo iterativo (BFS) para recolectar candidatos potenciales a duplicados.
    
    Utiliza un set de inodos para evitar procesar enlaces físicos duplicados 
    y recursión infinita en sistemas de archivos complejos.
    """
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    if directories is None: return size_to_paths_map
    queue: deque[Tuple[Path, int]] = deque()
    visited_inodes: set[Tuple[int, int]] = set()

    for dir_path in directories:
        if (root := _resolve_and_verify_root(dir_path)):
            queue.append((root, 0))
    
    while queue:
        current_dir, depth = queue.popleft()
        if depth > MAX_RECURSION_DEPTH:
            continue
            
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        path_entry = Path(entry.path)
                        # Verificación de seguridad proactiva antes de acceder a metadatos
                        if not _safe_path_check(path_entry):
                            continue

                        if entry.is_dir(follow_symlinks=False):
                            queue.append((path_entry, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            file_stat = entry.stat(follow_symlinks=False)
                            if file_stat.st_size < min_size or (file_stat.st_dev, file_stat.st_ino) in visited_inodes:
                                continue
                            
                            if skip_protected and is_protected_path(path_entry):
                                continue
                            
                            if _is_valid_candidate(path_entry, file_stat.st_size):
                                visited_inodes.add((file_stat.st_dev, file_stat.st_ino))
                                size_to_paths_map[file_stat.st_size].append(path_entry)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica una función de hashing (parcial o completa) y retorna grupos de colisión."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if not isinstance(path, Path): continue
        if (digest_val := hash_func(path)):
            groups_by_digest[digest_val].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Refina el filtrado de duplicados encadenando hash parcial y hash completo."""
    partial_groups: Dict[str, List[Path]] = _group_paths_by_hash(paths, partial_hash)
    final_results: Dict[str, List[Path]] = {}
    for candidate_subset in partial_groups.values():
        final_results.update(_group_paths_by_hash(candidate_subset, hash_file))
    return final_results


def _decide_hash_strategy_and_process(size_bytes: int, file_paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona si realizar hashing directo o escalonado según el tamaño del archivo."""
    if not file_paths or size_bytes <= 0: return []
    if size_bytes <= PARTIAL_READ_BYTES:
        final_groups = _group_paths_by_hash(file_paths, hash_file)
    else:
        final_groups = _process_large_file_subset(file_paths)
    return [DuplicateGroup(d, size_bytes, sorted(p)) for d, p in final_groups.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquesta la detección de duplicados mediante recolección, filtrado y hashing."""
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula la suma total de bytes recuperables de un conjunto de grupos."""
    if not groups: return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def _calculate_keeper_heuristic(path: Path) -> Optional[Tuple[float, int]]:
    """
    Retorna metadatos (tiempo de modificación, longitud ruta) para la elección del keeper.
    """
    try:
        if not isinstance(path, Path) or not path.is_file():
            return None
        stat_info = path.stat()
        return float(stat_info.st_mtime), len(str(path))
    except (OSError, AttributeError, FileNotFoundError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """
    Sugiere qué archivo conservar basándose en la fecha de modificación (menor mtime).
    """
    if group is None or not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    
    candidates: List[Tuple[Tuple[float, int], Path]] = []
    for path_item in group.paths:
        if not isinstance(path_item, Path): 
            continue
        score = _calculate_keeper_heuristic(path_item)
        if score is not None:
            candidates.append((score, path_item))
            
    if not candidates:
        return None
        
    return min(candidates, key=lambda x: x[0])[1]


def _get_path_label(path: Path, keeper: Optional[Path]) -> str:
    """Retorna una etiqueta textual para diferenciar el original de los duplicados."""
    if keeper is None:
        return "[?]"
    return "[conservar]" if path == keeper else "[duplicado]"


def format_group(group: DuplicateGroup) -> List[str]:
    """
    Formatea un grupo de duplicados para su presentación en reportes de texto.
    """
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return ["Error: Grupo inválido o vacío"]
    
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / 1048576, 2)
    mb_wasted = round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    
    for path_item in group.paths:
        if isinstance(path_item, Path):
            lines.append(f"   {_get_path_label(path_item, keeper)} {path_item}")
        else:
            lines.append(f"   [error: ruta inválida]")
            
    return lines
