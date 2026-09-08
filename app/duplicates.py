"""
duplicates.py — detección de archivos duplicados.

SOLO LECTURA: este módulo encuentra y agrupa duplicados, y sugiere cuál
conservar, pero **nunca borra ni mueve nada**.

Estrategia en tres pasos:
| Paso | Técnica | Propósito |
| :--- | :--- | :--- |
| 1 | Tamaño (stat) | Descarta archivos únicos rápidamente. |
| 2 | Hash Parcial | Filtra falsos positivos (igual tamaño, distinto contenido). |
| 3 | Hash Completo | Confirmación final de identidad (SHA256). |
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

from safety import is_protected_path

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


def is_junction(path: Path) -> bool:
    """Verifica si una ruta es un punto de reparse mediante atributos de sistema (Windows)."""
    if not isinstance(path, Path):
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError):
        return False


@dataclass
class DuplicateGroup:
    """Representa una colección de archivos que comparten contenido idéntico."""
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Devuelve el número total de archivos en este grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """Calcula el espacio total (en bytes) recuperable eliminando los duplicados."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo del archivo para confirmación de identidad.
    Retorna None si el archivo no existe, no es accesible o es de tamaño cero."""
    if path is None or chunk_size <= 0:
        return None
        
    try:
        path_obj = Path(path).resolve(strict=False)
        if not _is_valid_candidate(path_obj):
            return None
            
        if path_obj.stat().st_size == 0:
            return None
            
        digest = hashlib.sha256()
        with open(path_obj, "rb") as f:
            while True:
                buffer = f.read(chunk_size)
                if not buffer:
                    break
                digest.update(buffer)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula un hash SHA256 de los primeros N bytes para filtrado rápido.
    Se utiliza como optimización inicial antes de realizar un cálculo de hash completo."""
    if path is None or read_bytes <= 0:
        return None

    try:
        path_obj = Path(path).resolve(strict=False)
        if not _is_valid_candidate(path_obj):
            return None

        if path_obj.stat().st_size == 0:
            return None

        with open(path_obj, "rb") as f:
            content = f.read(read_bytes)
            if not content:
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def _is_valid_candidate(path: Path) -> bool:
    """Validador estricto de elegibilidad de archivos para análisis.
    Verifica accesibilidad, que no sea enlace simbólico y que no esté en la lista bloqueada."""
    if not isinstance(path, Path):
        return False
    try:
        if not path.exists():
            return False
        return (
            path.is_file() and 
            not path.is_symlink() and
            not is_protected_path(path) and 
            os.access(path, os.R_OK) and
            path.stat().st_nlink == 1
        )
    except (OSError, ValueError, TypeError):
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Clasifica rutas por tamaño en bytes, descartando archivos no válidos."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None or not isinstance(paths, Iterable): 
        return groups
    
    for p in paths:
        if p is None: continue
        path_obj = Path(p)
        if _is_valid_candidate(path_obj):
            try:
                size = path_obj.stat().st_size
                if size > 0:
                    groups[size].append(path_obj)
            except (OSError, PermissionError):
                continue
    return groups


def _resolve_and_verify_root(item: PathLike) -> Optional[Path]:
    """Resuelve rutas base y descarta directorios protegidos o inexistentes."""
    try:
        if not item: 
            return None
        root = Path(item).resolve(strict=False)
        if root.exists() and root.is_dir() and not is_protected_path(root):
            return root
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def _collect_candidates(
    directories: Iterable[PathLike], 
    min_size: int, 
    skip_protected: bool
) -> Dict[int, List[Path]]:
    """Realiza un barrido recursivo del sistema de archivos, manteniendo el estado de directorios visitados."""
    size_map: Dict[int, List[Path]] = defaultdict(list)
    visited: set[str] = set()

    def _scan_directory_recursive(current_dir: Path) -> None:
        try:
            real_path = str(current_dir.resolve(strict=False))
            if real_path in visited or is_protected_path(current_dir):
                return
            visited.add(real_path)
            
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not is_junction(Path(entry.path)):
                                _scan_directory_recursive(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            if st.st_size >= min_size:
                                path_obj = Path(entry.path)
                                if (not skip_protected or not is_protected_path(path_obj)) and _is_valid_candidate(path_obj):
                                    size_map[st.st_size].append(path_obj)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            pass

    if directories and isinstance(directories, Iterable):
        roots = {r for item in directories if (r := _resolve_and_verify_root(item))}
        for root in roots:
            _scan_directory_recursive(root)
            
    return {size: files for size, files in size_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica la función hash indicada a una colección y agrupa rutas por digest resultante."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if path is not None and (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _refine_by_deep_hash(candidates: List[Path]) -> Dict[str, List[Path]]:
    """Refina grupos existentes aplicando hashing SHA256 completo tras un filtrado por hash parcial."""
    partial_results: Dict[str, List[Path]] = _group_paths_by_hash(candidates, partial_hash)
    final_groups: Dict[str, List[Path]] = {}
    
    for subset in partial_results.values():
        full_hash_groups = _group_paths_by_hash(subset, hash_file)
        final_groups.update(full_hash_groups)
        
    return final_groups


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona la estrategia de hashing eficiente según el tamaño del archivo para reducir IO."""
    if not isinstance(size, int) or size <= 0 or not paths or len(paths) < 2: 
        return []
    
    if size <= PARTIAL_READ_BYTES:
        results = _group_paths_by_hash(paths, partial_hash)
    else:
        results = _refine_by_deep_hash(paths)
            
    return [DuplicateGroup(digest, size, sorted(confirmed_paths)) for digest, confirmed_paths in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquestador principal que coordina el escaneo, filtrado de tamaño y resolución profunda de duplicados."""
    if not isinstance(directories, Iterable) or isinstance(directories, (str, Path)): 
        return []
    if not isinstance(min_size, int) or min_size < 0: 
        return []
        
    groups: List[DuplicateGroup] = []
    valid_dirs = [d for d in directories if d is not None]
    if not valid_dirs: return []

    size_map = _collect_candidates(valid_dirs, min_size, skip_protected)
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
        
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el total de bytes liberables acumulado en todos los grupos identificados."""
    if not groups or not isinstance(groups, (list, tuple)): 
        return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Aplica heurística: el archivo conservado es el más antiguo (menor mtime).
    En caso de empate en mtime, se utiliza la longitud de la ruta como desempate."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
        
    candidates: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        if not isinstance(p, Path): 
            continue
        try:
            stat_info = p.stat()
            candidates.append((float(stat_info.st_mtime), len(str(p)), p))
        except (OSError, PermissionError):
            continue
    
    if not candidates:
        return None
        
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una lista de líneas descriptivas del grupo, identificando archivos por conservar o duplicados."""
    if not isinstance(group, DuplicateGroup) or group.paths is None:
        return []
        
    keeper = suggest_keeper(group)
    try:
        mb_total = round(group.size_bytes / (1024 * 1024), 2)
        mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return ["Error calculando tamaño de grupo"]

    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    
    for path in group.paths:
        if not isinstance(path, Path):
            continue
        try:
            if not path.exists():
                lines.append(f"   [desaparecido] {path}")
            elif not _is_valid_candidate(path):
                lines.append(f"   [inaccesible] {path}")
            else:
                label = 'conservar' if keeper is not None and path == keeper else 'duplicado'
                lines.append(f"   [{label}] {path}")
        except (OSError, PermissionError):
            lines.append(f"   [error] {path}")
            
    return lines
