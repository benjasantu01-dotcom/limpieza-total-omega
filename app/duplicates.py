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
    """Verifica si una ruta es un punto de reparse mediante atributos de sistema (Windows)."""
    if not isinstance(path, Path):
        return False
    try:
        resolved = path.resolve()
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(resolved))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError, RuntimeError):
        return False


def is_system_or_hidden(path: Path) -> bool:
    """Verifica si un archivo tiene atributos de sistema u oculto en Windows."""
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
        """Devuelve el número total de archivos en este grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """Calcula el espacio total (en bytes) recuperable eliminando los duplicados."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """Intenta abrir el archivo en modo exclusivo para detectar si está bloqueado."""
    try:
        with open(path, 'ab'):
            return False
    except (OSError, PermissionError):
        return True


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del archivo.
    
    Aplica chequeos de seguridad obligatorios antes de procesar para evitar
    acceder a rutas restringidas. Retorna None en caso de error o restricción.
    """
    if path is None or chunk_size <= 0:
        return None
        
    try:
        p = Path(path).resolve(strict=True)
        if not p.is_file() or p.stat().st_size == 0 or is_protected_path(p) or not is_safe_to_modify(p) or _is_file_locked(p):
            return None
            
        digest = hashlib.sha256()
        with open(p, "rb") as f:
            while True:
                buffer = f.read(chunk_size)
                if not buffer:
                    break
                digest.update(buffer)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Calcula hash SHA256 de los primeros N bytes para filtrado rápido.
    
    Útil para descartar archivos distintos rápidamente sin leer la totalidad del archivo.
    """
    if path is None or read_bytes <= 0:
        return None

    try:
        p = Path(path).resolve(strict=True)
        if not p.is_file() or p.stat().st_size == 0 or is_protected_path(p) or not is_safe_to_modify(p) or _is_file_locked(p):
            return None

        with open(p, "rb") as f:
            content = f.read(read_bytes)
            if not content:
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def _is_valid_candidate(path: Path, st: Optional[os.stat_result] = None) -> bool:
    """
    Validador estricto para filtrar candidatos a duplicados.
    """
    try:
        resolved = path.resolve(strict=True)
        if resolved.is_symlink() or is_protected_path(resolved) or not is_safe_to_modify(resolved) or _is_file_locked(resolved):
            return False
            
        if is_system_or_hidden(resolved):
            return False
            
        st = st or resolved.stat()
        return st.st_size > 0 and st.st_nlink == 1
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Clasifica rutas por tamaño en bytes, descartando archivos no válidos."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    if not isinstance(paths, Iterable): 
        return groups
    
    for p in paths:
        if p is None: continue
        try:
            path_obj = Path(p).absolute()
            if _is_valid_candidate(path_obj):
                size = path_obj.stat().st_size
                if size > 0:
                    groups[size].append(path_obj)
        except (OSError, PermissionError, TypeError):
            continue
    return groups


def _resolve_and_verify_root(item: PathLike) -> Optional[Path]:
    """Resuelve la ruta absoluta y verifica si es un directorio escaneable."""
    try:
        if not item: 
            return None
        root = Path(item).resolve(strict=False)
        if root.exists() and root.is_dir() and not is_protected_path(root) and is_safe_to_modify(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return None


def _collect_candidates(
    directories: Iterable[PathLike], 
    min_size: int, 
    skip_protected: bool
) -> Dict[int, List[Path]]:
    """
    Escaneo recursivo para recolectar candidatos indexados por tamaño en bytes.
    Mantiene un set de rutas visitadas para evitar ciclos en enlaces simbólicos.
    """
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    visited_dirs: set[str] = set()

    def _scan_directory_recursive(current_dir: Path) -> None:
        try:
            resolved_dir = current_dir.resolve(strict=False)
            dir_str = str(resolved_dir)
            if dir_str in visited_dirs or is_protected_path(current_dir) or not is_safe_to_modify(current_dir):
                return
            visited_dirs.add(dir_str)
            
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            entry_path = Path(entry.path)
                            if not is_junction(entry_path) and is_safe_to_modify(entry_path):
                                _scan_directory_recursive(entry_path)
                        elif entry.is_file(follow_symlinks=False):
                            file_stat = entry.stat()
                            if file_stat.st_size >= min_size:
                                path_obj = Path(entry.path)
                                if _is_valid_candidate(path_obj, file_stat):
                                    size_to_paths_map[file_stat.st_size].append(path_obj)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError, RuntimeError):
            return

    if isinstance(directories, Iterable):
        for item in directories:
            if (root_path := _resolve_and_verify_root(item)):
                _scan_directory_recursive(root_path)
            
    return {size: files for size, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Agrupa una lista de archivos aplicando una función hash, ignorando fallos de acceso."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    if not isinstance(paths, Iterable):
        return {}
    for path in paths:
        try:
            if path is not None and (digest := hash_func(path)):
                groups_by_digest[digest].append(path)
        except (OSError, PermissionError):
            continue
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _refine_by_deep_hash(candidates: List[Path]) -> Dict[str, List[Path]]:
    """
    Refina grupos candidatos usando una estrategia jerárquica de hashing.
    """
    if not isinstance(candidates, list) or not candidates:
        return {}
    partial_results: Dict[str, List[Path]] = _group_paths_by_hash(candidates, partial_hash)
    final_groups: Dict[str, List[Path]] = {}
    
    for subset in partial_results.values():
        full_hash_groups = _group_paths_by_hash(subset, hash_file)
        final_groups.update(full_hash_groups)
        
    return final_groups


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona estrategia de hashing según tamaño y robustez ante E/S."""
    if not isinstance(size, int) or size <= 0 or not isinstance(paths, list) or len(paths) < 2:
        return []
    
    results = _group_paths_by_hash(paths, partial_hash) if size <= PARTIAL_READ_BYTES else _refine_by_deep_hash(paths)
    return [DuplicateGroup(digest, size, sorted(confirmed_paths)) for digest, confirmed_paths in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquestador principal que coordina el escaneo, filtrado de tamaño y resolución profunda de duplicados."""
    if not isinstance(directories, Iterable) or isinstance(directories, (str, Path)): 
        return []
    if not isinstance(min_size, int) or min_size < 0: 
        return []
        
    groups: List[DuplicateGroup] = []
    size_map = _collect_candidates(directories, min_size, skip_protected)
    
    for size, paths in size_map.items():
        if isinstance(paths, list):
            groups.extend(_decide_hash_strategy_and_process(size, paths))
        
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el total de bytes liberables acumulado en todos los grupos identificados."""
    if not groups or not isinstance(groups, (list, tuple)): 
        return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """
    Heurística de selección: sugiere conservar el archivo más antiguo.
    
    En caso de empate en mtime, utiliza la longitud de la ruta como desempate determinista.
    """
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
        
    candidates: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        if not isinstance(p, Path):
            continue
        try:
            stat_info = p.stat()
            candidates.append((float(stat_info.st_mtime), len(str(p)), p))
        except (OSError, PermissionError, FileNotFoundError):
            continue
    
    return min(candidates, key=lambda x: (x[0], x[1]))[2] if candidates else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una lista de líneas descriptivas del grupo para la interfaz."""
    if not isinstance(group, DuplicateGroup) or not hasattr(group, 'paths'):
        return ["Error: Grupo inválido"]
        
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
                label = 'conservar' if (keeper and path == keeper) else 'duplicado'
                lines.append(f"   [{label}] {path}")
        except (OSError, PermissionError):
            lines.append(f"   [error] {path}")
            
    return lines
