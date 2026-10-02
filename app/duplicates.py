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
MAX_RECURSION_DEPTH: int = 100


def is_junction(path: Path) -> bool:
    """Verifica si una ruta es un punto de unión (junction) de NTFS para evitar bucles infinitos."""
    if not isinstance(path, Path) or not is_safe_to_modify(path):
        return False
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError, RuntimeError):
        return False


def is_system_or_hidden(path: Path) -> bool:
    """Valida si el archivo posee atributos de sistema u oculto de Windows."""
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
    """Representa un clúster de archivos con el mismo contenido identificado por hash."""
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Cantidad de ejemplares del archivo detectados."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """Espacio ocupado por duplicados (excluyendo el archivo original)."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """Intenta abrir un archivo en modo lectura para verificar si el SO lo tiene bloqueado."""
    if not isinstance(path, Path) or not is_safe_to_modify(path) or not path.exists():
        return True
    try:
        fd = os.open(path, os.O_RDONLY)
        os.close(fd)
        return False
    except (PermissionError, OSError, ValueError, FileNotFoundError):
        return True


def _safe_path_check(path: Path) -> bool:
    """Valida que la ruta pase los filtros de seguridad y no sea un enlace simbólico/junction."""
    return (isinstance(path, Path) and is_safe_to_modify(path) and 
            not is_protected_path(path) and not is_junction(path) and 
            not path.is_symlink())


def _validate_and_resolve_path(path: PathLike) -> Optional[Path]:
    """Normaliza, valida existencia y accesibilidad de lectura para una ruta dada."""
    if not path:
        return None
    try:
        p: Path = Path(path).absolute()
        if _safe_path_check(p) and p.is_file() and not _is_file_locked(p):
            if p.stat().st_size > 0:
                return p
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula SHA256 completo del archivo procesándolo en bloques para optimizar memoria."""
    if chunk_size <= 0:
        return None
        
    p = _validate_and_resolve_path(path)
    if p is None:
        return None
            
    try:
        digest = hashlib.sha256()
        with open(p, "rb") as f:
            while (chunk := f.read(chunk_size)):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError, MemoryError, ValueError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula SHA256 sobre una muestra inicial para filtrar descartes rápidamente."""
    if read_bytes <= 0:
        return None

    p = _validate_and_resolve_path(path)
    if p is None:
        return None

    try:
        with open(p, "rb") as f:
            content = f.read(read_bytes)
            if not content: 
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError, ValueError):
        return None


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Filtra archivos según criterios de seguridad y estado de sistema."""
    if not isinstance(path, Path) or st_size <= 0:
        return False
    try:
        return (_safe_path_check(path) and 
                not is_system_or_hidden(path) and 
                not _is_file_locked(path))
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError):
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Crea un diccionario mapeando tamaño de archivo a lista de rutas."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if not p: continue
        try:
            path_obj = Path(p).absolute()
            if not path_obj.exists(): continue
            st_size = path_obj.stat().st_size
            if _is_valid_candidate(path_obj, st_size):
                groups[st_size].append(path_obj)
        except (OSError, ValueError, TypeError):
            continue
    return groups


def _resolve_and_verify_root(item: PathLike) -> Optional[Path]:
    """Valida la raíz de búsqueda para asegurar que es un directorio accesible."""
    if not item: return None
    try:
        root = Path(item).absolute()
        if root.is_dir() and _safe_path_check(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        return None
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Escaneo recursivo para recolectar archivos candidatos para el análisis de duplicados."""
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    stack: List[Tuple[Path, int]] = []
    for d in directories:
        if (r := _resolve_and_verify_root(d)):
            stack.append((r, 0))
    
    visited: set[str] = set()
    while stack:
        current_dir, depth = stack.pop()
        if depth > MAX_RECURSION_DEPTH:
            continue
            
        try:
            real_path = current_dir.resolve()
            if str(real_path) in visited: continue
            visited.add(str(real_path))
            
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            p_entry = Path(entry.path)
                            if _safe_path_check(p_entry) and not is_junction(p_entry):
                                stack.append((p_entry, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            stat = entry.stat()
                            if stat.st_size >= min_size:
                                p_entry = Path(entry.path)
                                if not (skip_protected and is_protected_path(p_entry)) and _is_valid_candidate(p_entry, stat.st_size):
                                    size_to_paths_map[stat.st_size].append(p_entry)
                    except OSError:
                        continue
        except (OSError, PermissionError, RuntimeError):
            continue
            
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Agrupa rutas que comparten un mismo valor de digest (hash)."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if path.exists() and (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Refinamiento: reduce el set de candidatos usando hash parcial antes del hash completo."""
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final_results = {}
    for candidate_subset in partial_groups.values():
        full_hash_groups = _group_paths_by_hash(candidate_subset, hash_file)
        final_results.update(full_hash_groups)
    return final_results


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona el método de hashing (rápido o completo) basado en el tamaño de archivo."""
    if not paths or size <= 0:
        return []

    if size <= PARTIAL_READ_BYTES:
        final_groups = _group_paths_by_hash(paths, hash_file)
    else:
        final_groups = _process_large_file_subset(paths)
            
    return [DuplicateGroup(d, size, sorted(p)) for d, p in final_groups.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquestador principal: busca duplicados en directorios dados y retorna grupos ordenados por ahorro."""
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el total de bytes que se recuperarían borrando todos los duplicados menos un original."""
    if not groups: return 0
    return sum(g.wasted_bytes for g in groups)


def _calculate_keeper_heuristic(path: Path) -> Optional[Tuple[float, int]]:
    """Métrica para sugerir el 'keeper': prioriza archivos más antiguos (mtime) y rutas cortas."""
    if not isinstance(path, Path) or not _safe_path_check(path):
        return None
    try:
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, PermissionError, ValueError, AttributeError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Determina la mejor ruta para preservar en un grupo de duplicados."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    
    candidates: List[Tuple[Tuple[float, int], Path]] = []
    for p in group.paths:
        if p.exists() and (score := _calculate_keeper_heuristic(p)):
            candidates.append((score, p))
            
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera representación textual amigable para reporte de grupos de duplicados."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return ["Error: Grupo inválido o vacío"]
        
    keeper = suggest_keeper(group)
    
    mb_t, mb_w = round(group.size_bytes / 1048576, 2), round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_t} MB (recuperable: {mb_w} MB)"]
    
    for path in group.paths:
        try:
            if not path.exists():
                lines.append(f"   [desaparecido] {path}")
            elif not _safe_path_check(path):
                lines.append(f"   [inaccesible] {path}")
            else:
                is_keeper = (keeper is not None and path == keeper)
                label = 'conservar' if is_keeper else 'duplicado'
                lines.append(f"   [{label}] {path}")
        except (OSError, RuntimeError):
            lines.append(f"   [error de acceso] {path}")
    return lines
