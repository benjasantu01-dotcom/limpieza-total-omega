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
    """
    Calcula el hash SHA256 completo.
    Retorna None si el archivo es inaccesible, protegido o ocurre un error de E/S.
    """
    if path is None or chunk_size <= 0:
        return None
        
    try:
        path_obj = Path(path).resolve(strict=True)
        if not _is_valid_candidate(path_obj) or path_obj.stat().st_size == 0:
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
    """
    Calcula hash SHA256 de los primeros N bytes para filtrado rápido.
    Ignora errores de lectura (ej. bloqueos por el sistema operativo).
    """
    if path is None or read_bytes <= 0:
        return None

    try:
        path_obj = Path(path).resolve(strict=True)
        if not _is_valid_candidate(path_obj) or path_obj.stat().st_size == 0:
            return None

        with open(path_obj, "rb") as f:
            content = f.read(read_bytes)
            return hashlib.sha256(content).hexdigest() if content else None
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def _is_valid_candidate(path: Path) -> bool:
    """Validador estricto: comprueba existencia, permisos y que no sea enlace simbólico o junction."""
    if not isinstance(path, Path):
        return False
    try:
        if not path.is_file() or path.is_symlink() or is_junction(path):
            return False
        return (
            not is_protected_path(path) and 
            os.access(path, os.R_OK) and
            path.stat().st_nlink == 1
        )
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
            path_obj = Path(p)
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
        if root.exists() and root.is_dir() and not is_protected_path(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return None


def _collect_candidates(
    directories: Iterable[PathLike], 
    min_size: int, 
    skip_protected: bool
) -> Dict[int, List[Path]]:
    """Realiza un escaneo recursivo del sistema para identificar candidatos a duplicados."""
    size_map: Dict[int, List[Path]] = defaultdict(list)
    visited: set[str] = set()

    def _scan_directory_recursive(current_dir: Path) -> None:
        try:
            resolved_dir = current_dir.resolve(strict=False)
            path_str = str(resolved_dir)
            if not resolved_dir.exists() or path_str in visited or is_protected_path(resolved_dir):
                return
            visited.add(path_str)
            
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not is_junction(Path(entry.path)):
                                _scan_directory_recursive(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat()
                            if st.st_size >= min_size:
                                path_obj = Path(entry.path)
                                if _is_valid_candidate(path_obj):
                                    size_map[st.st_size].append(path_obj)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError, FileNotFoundError, RuntimeError):
            return

    if isinstance(directories, Iterable):
        for item in directories:
            if item:
                resolved = _resolve_and_verify_root(item)
                if resolved:
                    _scan_directory_recursive(resolved)
            
    return {size: files for size, files in size_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Agrupa una lista de archivos aplicando una función hash (parcial o completa)."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if path is not None and (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _refine_by_deep_hash(candidates: List[Path]) -> Dict[str, List[Path]]:
    """Aplica un refinamiento de hashing en dos fases: parcial primero, luego completo."""
    partial_results: Dict[str, List[Path]] = _group_paths_by_hash(candidates, partial_hash)
    final_groups: Dict[str, List[Path]] = {}
    
    for subset in partial_results.values():
        full_hash_groups = _group_paths_by_hash(subset, hash_file)
        final_groups.update(full_hash_groups)
        
    return final_groups


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Determina si aplicar hash rápido o completo basándose en el tamaño del archivo."""
    if not isinstance(size, int) or size <= 0 or not paths or len(paths) < 2: 
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
        groups.extend(_decide_hash_strategy_and_process(size, paths))
        
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el total de bytes liberables acumulado en todos los grupos identificados."""
    if not groups or not isinstance(groups, (list, tuple)): 
        return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Aplica heurística de selección: sugiere conservar el archivo más antiguo (mtime)."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
        
    candidates: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        if not isinstance(p, Path): continue
        try:
            stat_info = p.stat()
            candidates.append((float(stat_info.st_mtime), len(str(p)), p))
        except (OSError, PermissionError):
            continue
    
    if not candidates:
        return None
        
    return min(candidates, key=lambda x: (x[0], x[1]))[2]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una lista de líneas descriptivas del grupo para la interfaz."""
    if not isinstance(group, DuplicateGroup) or group.paths is None:
        return ["Error: Grupo inválido"]
        
    keeper = suggest_keeper(group)
    try:
        mb_total = round(group.size_bytes / (1024 * 1024), 2)
        mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return ["Error calculando tamaño de grupo"]

    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    for path in group.paths:
        if not isinstance(path, Path): continue
        if not path.exists():
            lines.append(f"   [desaparecido] {path}")
        elif not _is_valid_candidate(path):
            lines.append(f"   [inaccesible] {path}")
        else:
            label = 'conservar' if keeper and path == keeper else 'duplicado'
            lines.append(f"   [{label}] {path}")
            
    return lines
