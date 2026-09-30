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
from collections import defaultdict
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


def is_junction(path: Path) -> bool:
    """Verifica si una ruta es un punto de unión (junction) de NTFS."""
    if not isinstance(path, Path) or not is_safe_to_modify(path):
        return False
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError, RuntimeError):
        return False


def is_system_or_hidden(path: Path) -> bool:
    """Verifica atributos de Windows para descartar archivos ocultos o del sistema."""
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
    """Representa una colección de archivos que comparten contenido idéntico."""
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Cantidad de archivos identificados como duplicados en el grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """Calcula el espacio total recuperable excluyendo el archivo 'keeper'."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """Determina si un archivo está bloqueado intentando abrirlo en modo lectura."""
    if not is_safe_to_modify(path):
        return True
    try:
        fd = os.open(path, os.O_RDONLY)
        os.close(fd)
        return False
    except (PermissionError, OSError, ValueError):
        return True


def _safe_path_check(path: Path) -> bool:
    """Valida la integridad de la ruta contra restricciones de seguridad del sistema."""
    return (isinstance(path, Path) and is_safe_to_modify(path) and 
            not is_protected_path(path) and not is_junction(path) and 
            not path.is_symlink())


def _validate_and_resolve_path(path: PathLike) -> Optional[Path]:
    """Normaliza y valida que la ruta sea un archivo accesible y existente."""
    if not path:
        return None
    try:
        p: Path = Path(path).absolute()
        if _safe_path_check(p) and p.is_file() and not _is_file_locked(p):
            if p.stat().st_size > 0:
                return p
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula SHA256 completo usando buffers de memoria para eficiencia."""
    if chunk_size <= 0:
        return None
        
    p = _validate_and_resolve_path(path)
    if p is None or not p.exists():
        return None
            
    try:
        digest = hashlib.sha256()
        with open(p, "rb") as f:
            while (chunk := f.read(chunk_size)):
                if not isinstance(chunk, (bytes, bytearray)):
                    return None
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError, MemoryError, ValueError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula un hash rápido basado solo en los primeros bytes del archivo."""
    if read_bytes <= 0:
        return None

    p = _validate_and_resolve_path(path)
    if p is None or not p.exists():
        return None

    try:
        with open(p, "rb") as f:
            content = f.read(read_bytes)
            if not content or not isinstance(content, (bytes, bytearray)): 
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError, ValueError):
        return None


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Filtra archivos que no cumplen los requisitos básicos de procesamiento."""
    try:
        if not path.exists() or not _safe_path_check(path):
            return False
        if is_system_or_hidden(path) or _is_file_locked(path):
            return False
        return st_size > 0
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError):
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa una secuencia de rutas según el tamaño de archivo."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if not p:
            continue
        try:
            path_obj = Path(p).absolute()
            if _safe_path_check(path_obj) and path_obj.exists():
                st_size = path_obj.stat().st_size
                if _is_valid_candidate(path_obj, st_size):
                    groups[st_size].append(path_obj)
        except (OSError, RuntimeError, ValueError):
            continue
    return groups


def _resolve_and_verify_root(item: PathLike) -> Optional[Path]:
    """Valida que un directorio raíz sea navegable y seguro."""
    try:
        if not item: return None
        root = Path(item).absolute()
        if root.is_dir() and _safe_path_check(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Realiza una búsqueda profunda para catalogar archivos según su peso."""
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    stack: List[str] = [str(r) for d in directories if (r := _resolve_and_verify_root(d))]
    visited: set[str] = set()

    while stack:
        current_dir = stack.pop()
        if current_dir in visited:
            continue
        visited.add(current_dir)
        
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        # Filtros rápidos primero (sin I/O adicional)
                        if entry.is_dir(follow_symlinks=False):
                            p_entry = Path(entry.path)
                            if _safe_path_check(p_entry) and not is_junction(p_entry):
                                stack.append(entry.path)
                            continue
                        
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        
                        stat_info = entry.stat(follow_symlinks=False)
                        if stat_info.st_size < min_size:
                            continue

                        p_entry = Path(entry.path)
                        # Chequeos de seguridad e integridad al final
                        if skip_protected and is_protected_path(p_entry):
                            continue
                        if not _safe_path_check(p_entry) or is_system_or_hidden(p_entry) or _is_file_locked(p_entry):
                            continue
                            
                        size_to_paths_map[stat_info.st_size].append(p_entry)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
            
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica una función de hash y agrupa los resultados coincidentes."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if not isinstance(path, Path):
            continue
        if (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Refina grupos mediante hashes parciales antes de confirmar con hashes completos."""
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final_results = {}
    for candidate_subset in partial_groups.values():
        full_hash_groups = _group_paths_by_hash(candidate_subset, hash_file)
        final_results.update(full_hash_groups)
    return final_results


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona el método de hashing (rápido o completo) según el tamaño del archivo."""
    if not paths or size <= 0:
        return []

    if size <= PARTIAL_READ_BYTES:
        final_groups = _group_paths_by_hash(paths, hash_file)
    else:
        final_groups = _process_large_file_subset(paths)
            
    return [DuplicateGroup(d, size, sorted(p)) for d, p in final_groups.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquestador principal que coordina la detección de archivos duplicados."""
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma total de espacio que puede liberarse del conjunto de grupos."""
    return sum(g.wasted_bytes for g in groups)


def _calculate_keeper_heuristic(path: Path) -> Optional[Tuple[float, int]]:
    """Genera métricas (mtime, longitud de ruta) para elegir el archivo conservador."""
    if not _safe_path_check(path):
        return None
    try:
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, PermissionError, ValueError, AttributeError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Sugiere el 'mejor' archivo para conservar basado en antigüedad y ruta."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    
    candidates: List[Tuple[Tuple[float, int], Path]] = []
    for p in group.paths:
        if not isinstance(p, Path):
            continue
        if score := _calculate_keeper_heuristic(p):
            candidates.append((score, p))
            
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera representación legible para humanos de un grupo de duplicados."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return ["Error: Grupo inválido o vacío"]
        
    keeper = suggest_keeper(group)
    
    mb_t, mb_w = round(group.size_bytes / 1048576, 2), round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_t} MB (recuperable: {mb_w} MB)"]
    
    for path in group.paths:
        if not isinstance(path, Path):
            continue
        try:
            if not path.exists():
                lines.append(f"   [desaparecido] {path}")
            elif not _safe_path_check(path):
                lines.append(f"   [inaccesible] {path}")
            else:
                is_keeper = (path == keeper)
                label = 'conservar' if is_keeper else 'duplicado'
                lines.append(f"   [{label}] {path}")
        except (OSError, RuntimeError):
            lines.append(f"   [error de acceso] {path}")
    return lines
