"""
duplicates.py — detección de archivos duplicados.

SOLO LECTURA: este módulo encuentra y agrupa duplicados, y sugiere cuál
conservar, pero **nunca borra ni mueve nada**.

Estrategia en tres pasos para detección de duplicados:
| Paso | Técnica | Propósito |
| :--- | :--- | :--- |
| 1 | Tamaño (stat) | Descarta archivos únicos rápidamente (comparación O(1)). |
| 2 | Hash Parcial | Filtra falsos positivos leyendo solo el inicio (64KB). |
| 3 | Hash Completo | Confirmación final mediante SHA256 del contenido total. |

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
    """Verifica si una ruta es un punto de reparse (junction) en Windows usando Win32 API."""
    if not isinstance(path, Path):
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError, RuntimeError):
        return False


def is_system_or_hidden(path: Path) -> bool:
    """Valida atributos de sistema o visibilidad (oculto) en Windows."""
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
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
        """Calcula el espacio total recuperable restando una copia (el 'keeper')."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """
    Verifica si un archivo está bloqueado por otro proceso intentando abrirlo 
    en modo lectura exclusiva.
    """
    try:
        with open(path, 'rb') as f:
            f.read(1)
        return False
    except (OSError, PermissionError, FileNotFoundError, BlockingIOError, IsADirectoryError, EOFError):
        return True


def _validate_and_resolve_path(path: PathLike) -> Optional[Path]:
    """Valida integridad, permisos de seguridad y accesibilidad de bloqueo."""
    if not path:
        return None
    try:
        p = Path(path).resolve(strict=True)
        if p.is_file() and is_safe_to_modify(p) and not _is_file_locked(p):
            return p
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo. Retorna None si el archivo es inaccesible o está bloqueado."""
    if chunk_size <= 0:
        return None
        
    p = _validate_and_resolve_path(path)
    if not p or not p.exists():
        return None
            
    try:
        digest = hashlib.sha256()
        with open(p, "rb") as f:
            while (chunk := f.read(chunk_size)):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError, MemoryError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula hash SHA256 de los primeros N bytes. Se usa para pre-filtrar candidatos de forma eficiente."""
    if read_bytes <= 0:
        return None

    p = _validate_and_resolve_path(path)
    if not p or not p.exists():
        return None

    try:
        with open(p, "rb") as f:
            content = f.read(read_bytes)
            if not content: 
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Filtro de integridad: evalúa atributos de sistema y seguridad de la ruta."""
    try:
        if is_protected_path(path) or not is_safe_to_modify(path):
            return False
        if is_system_or_hidden(path):
            return False
        return st_size > 0 and not _is_file_locked(path)
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError):
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa una lista de rutas basándose únicamente en su tamaño en bytes."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if not p: continue
        try:
            path_obj = Path(p).resolve(strict=True)
            if not is_safe_to_modify(path_obj):
                continue
            st = path_obj.stat()
            if _is_valid_candidate(path_obj, st.st_size):
                groups[st.st_size].append(path_obj)
        except (OSError, RuntimeError, ValueError):
            continue
    return groups


def _resolve_and_verify_root(item: PathLike) -> Optional[Path]:
    """Valida que un directorio sea accesible y permitido por seguridad."""
    try:
        if not item: return None
        root = Path(item).resolve(strict=True)
        if root.is_dir() and not is_protected_path(root) and is_safe_to_modify(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Recorrido recursivo del disco recolectando archivos aptos para análisis."""
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    visited_directories: set[str] = set()

    def _scan_dir(current_dir: Path) -> None:
        try:
            dir_str = str(current_dir.resolve())
            if dir_str in visited_directories: return
            visited_directories.add(dir_str)
            
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            path = Path(entry.path)
                            if not is_junction(path) and not is_protected_path(path):
                                _scan_dir(path)
                        else:
                            st = entry.stat(follow_symlinks=False)
                            if st.st_size >= min_size:
                                path = Path(entry.path)
                                if _is_valid_candidate(path, st.st_size):
                                    size_to_paths_map[st.st_size].append(path)
                    except (FileNotFoundError, OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            return

    for item in directories:
        if (root := _resolve_and_verify_root(item)):
            _scan_dir(root)
            
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica una función de hash para clasificar archivos en subgrupos."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """
    Gestiona el refinamiento de duplicados según el tamaño.
    - Archivos pequeños (<=64KB): Hash directo para evitar overhead de I/O.
    - Archivos grandes: Hash parcial seguido de confirmación por hash completo.
    """
    if not paths or size < 0:
        return []

    if size <= PARTIAL_READ_BYTES:
        final_groups = _group_paths_by_hash(paths, hash_file)
    else:
        partial_groups = _group_paths_by_hash(paths, partial_hash)
        final_groups = {}
        for candidate_subset in partial_groups.values():
            full_hash_groups = _group_paths_by_hash(candidate_subset, hash_file)
            final_groups.update(full_hash_groups)
            
    return [DuplicateGroup(d, size, sorted(p)) for d, p in final_groups.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquestador: coordina la recolección, filtrado y agrupación final de duplicados."""
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el total de bytes recuperables sumando los desperdicios de cada grupo."""
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def _get_keeper_score(path: Path) -> Optional[Tuple[float, int]]:
    """Calcula una puntuación (basada en tiempo y profundidad) para elegir el keeper."""
    try:
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, PermissionError, ValueError, AttributeError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Sugerencia heurística del mejor archivo para conservar en un grupo."""
    if not group or not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    
    candidates: List[Tuple[Tuple[float, int], Path]] = []
    for p in group.paths:
        if not isinstance(p, Path) or not p.exists():
            continue
        try:
            if not is_safe_to_modify(p):
                continue
            if score := _get_keeper_score(p):
                candidates.append((score, p))
        except (OSError, PermissionError):
            continue
            
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Formatea un grupo de duplicados para su presentación legible en la interfaz."""
    if not group or not isinstance(group, DuplicateGroup) or not group.paths:
        return ["Error: Grupo inválido o vacío"]
        
    keeper = suggest_keeper(group)
    mb_t, mb_w = round(group.size_bytes / 1048576, 2), round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_t} MB (recuperable: {mb_w} MB)"]
    
    for path in group.paths:
        if not isinstance(path, Path):
            lines.append(f"   [error] ruta no es objeto Path")
            continue
        try:
            if not is_safe_to_modify(path):
                lines.append(f"   [inaccesible] {path}")
            else:
                label = 'conservar' if (keeper is not None and path == keeper) else 'duplicado'
                lines.append(f"   [{label}] {path}")
        except (OSError, PermissionError):
            lines.append(f"   [error de acceso] {path}")
    return lines
