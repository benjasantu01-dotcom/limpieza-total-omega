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
    """
    Determina si la ruta es un punto de unión (junction) de NTFS.
    Evita la recursión en estructuras fuera del sistema de archivos plano.
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
    Verifica los atributos de archivo de Windows para descartar elementos
    del sistema que no deben ser analizados por el usuario.
    """
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
        """
        Calcula el espacio total que se puede recuperar.
        Se excluye una instancia (el 'keeper') del cálculo.
        """
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """
    Prueba si un archivo es accesible para lectura exclusiva.
    Si os.open lanza PermissionError, el archivo está bloqueado por otro proceso.
    """
    if not is_safe_to_modify(path):
        return True
    try:
        fd = os.open(path, os.O_RDONLY)
        os.close(fd)
        return False
    except (PermissionError, OSError):
        return True


def _validate_and_resolve_path(path: PathLike) -> Optional[Path]:
    """Normaliza y valida una ruta: debe ser un archivo existente y no un enlace simbólico."""
    if not path:
        return None
    try:
        p: Path = Path(path).resolve(strict=True)
        if p.is_symlink() or is_junction(p):
            return None
        if p.is_file() and is_safe_to_modify(p) and not _is_file_locked(p):
            return p
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo del archivo mediante buffer de lectura."""
    if chunk_size <= 0:
        return None
        
    p = _validate_and_resolve_path(path)
    if not p or not p.is_file():
        return None
            
    try:
        digest = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(chunk_size):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError, MemoryError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Optimización: calcula hash SHA256 solo de los primeros 64KB para descartar candidatos."""
    if read_bytes <= 0:
        return None

    p = _validate_and_resolve_path(path)
    if not p or not p.is_file():
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
    """Filtro de seguridad para determinar si un archivo puede entrar en el proceso de agrupación."""
    try:
        if not path.is_file() or is_protected_path(path) or not is_safe_to_modify(path):
            return False
        if is_system_or_hidden(path) or path.is_symlink() or is_junction(path):
            return False
        return st_size > 0 and not _is_file_locked(path)
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError):
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa rutas por su tamaño en bytes, filtrando candidatos no procesables."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if not p: continue
        try:
            path_obj = Path(p).resolve(strict=True)
            if is_safe_to_modify(path_obj) and not path_obj.is_symlink():
                st = path_obj.stat()
                if _is_valid_candidate(path_obj, st.st_size):
                    groups[st.st_size].append(path_obj)
        except (OSError, RuntimeError, ValueError):
            continue
    return groups


def _resolve_and_verify_root(item: PathLike) -> Optional[Path]:
    """Valida que una ruta base sea un directorio navegable y seguro."""
    try:
        if not item: return None
        root = Path(item).resolve(strict=True)
        if root.is_dir() and not is_protected_path(root) and is_safe_to_modify(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Exploración recursiva del sistema de archivos recolectando candidatos aptos para deduplicación."""
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    visited_files: set[str] = set()

    def _scan_dir(current_dir: Path) -> None:
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            path_entry = Path(entry.path)
                            if not is_junction(path_entry) and is_safe_to_modify(path_entry):
                                _scan_dir(path_entry)
                            continue
                        
                        if entry.path in visited_files:
                            continue
                            
                        st = entry.stat(follow_symlinks=False)
                        if st.st_size < min_size:
                            continue
                        
                        p_entry = Path(entry.path)
                        if (skip_protected and is_protected_path(p_entry)) or not is_safe_to_modify(p_entry):
                            continue
                            
                        if is_system_or_hidden(p_entry) or _is_file_locked(p_entry):
                            continue
                            
                        size_to_paths_map[st.st_size].append(p_entry)
                        visited_files.add(entry.path)
                    except (FileNotFoundError, OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            return

    for item in directories:
        if (root := _resolve_and_verify_root(item)):
            _scan_dir(root)
            
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Aplica una función hash a un grupo y retorna solo aquellos con colisiones."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if path.is_file() and is_safe_to_modify(path) and not path.is_symlink():
            if (digest := hash_func(path)):
                groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Pipeline de hashing: usa hash parcial para reducir I/O antes de confirmar con hash completo."""
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
    """Orquestador principal: agrupa por tamaño y luego refina mediante hashing progresivo."""
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el total de bytes que podrían ser liberados."""
    return sum(g.wasted_bytes for g in groups)


def _calculate_keeper_heuristic(path: Path) -> Optional[Tuple[float, int]]:
    """
    Retorna métricas para elegir el archivo a conservar: 
    (timestamp de modificación, longitud de la ruta).
    """
    try:
        if not path.exists():
            return None
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, PermissionError, ValueError, AttributeError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """
    Algoritmo de selección de 'keeper': elige la instancia más antigua.
    En caso de empate en fecha, prefiere la ruta más corta.
    """
    if not group or not group.paths:
        return None
    
    candidates: List[Tuple[Tuple[float, int], Path]] = []
    for p in group.paths:
        if isinstance(p, Path) and p.exists() and is_safe_to_modify(p):
            if score := _calculate_keeper_heuristic(p):
                candidates.append((score, p))
            
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Convierte un grupo de duplicados a una lista de strings para visualización en UI."""
    if not group or not isinstance(group, DuplicateGroup) or not group.paths:
        return ["Error: Grupo inválido o vacío"]
        
    keeper = suggest_keeper(group)
    keeper_resolved = keeper.resolve() if keeper else None
    
    mb_t, mb_w = round(group.size_bytes / 1048576, 2), round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_t} MB (recuperable: {mb_w} MB)"]
    
    for path in group.paths:
        if not isinstance(path, Path):
            continue
        try:
            if not is_safe_to_modify(path):
                lines.append(f"   [inaccesible] {path}")
            else:
                is_keeper = (keeper_resolved is not None and path.resolve() == keeper_resolved)
                label = 'conservar' if is_keeper else 'duplicado'
                lines.append(f"   [{label}] {path}")
        except (OSError, PermissionError):
            lines.append(f"   [error de acceso] {path}")
    return lines
