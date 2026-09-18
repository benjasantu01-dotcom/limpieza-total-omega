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
    except (OSError, PermissionError, FileNotFoundError, BlockingIOError, IsADirectoryError):
        return True


def _validate_and_resolve_path(path: PathLike) -> Optional[Path]:
    """Helper para validar y resolver rutas de forma segura."""
    if path is None:
        return None
    try:
        p = Path(path).resolve(strict=True)
        if p.is_file() and is_safe_to_modify(p) and not _is_file_locked(p):
            return p
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del contenido de un archivo.
    """
    if chunk_size <= 0:
        return None
        
    p = _validate_and_resolve_path(path)
    if not p:
        return None
            
    try:
        digest = hashlib.sha256()
        with open(p, "rb") as f:
            while (chunk := f.read(chunk_size)):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Calcula el hash SHA256 de los primeros N bytes de un archivo.
    """
    if read_bytes <= 0:
        return None

    p = _validate_and_resolve_path(path)
    if not p:
        return None

    try:
        with open(p, "rb") as f:
            content = f.read(read_bytes)
            if not content: return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def _is_valid_candidate(path: Path, st_result: os.stat_result) -> bool:
    """
    Filtro de integridad: evalúa atributos de sistema, reparse points y 
    restricciones de seguridad antes de incluir un archivo en el escaneo.
    """
    try:
        if (st_result.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT) if hasattr(st_result, 'st_file_attributes') else path.is_symlink():
            return False
        if is_protected_path(path) or not is_safe_to_modify(path):
            return False
        if is_system_or_hidden(path):
            return False
            
        return st_result.st_size > 0 and st_result.st_nlink == 1 and not _is_file_locked(path)
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError):
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa una lista de rutas basándose únicamente en su tamaño en bytes."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if p is None: continue
        try:
            path_obj = Path(p).resolve(strict=True)
            if not is_safe_to_modify(path_obj):
                continue
            st_result = path_obj.stat()
            if _is_valid_candidate(path_obj, st_result):
                groups[st_result.st_size].append(path_obj)
        except (OSError, RuntimeError, ValueError):
            continue
    return groups


def _resolve_and_verify_root(item: PathLike) -> Optional[Path]:
    """Valida que un directorio raíz sea accesible y permitido por las reglas de seguridad."""
    try:
        if not item: return None
        root = Path(item).resolve(strict=True)
        if root.is_dir() and not is_protected_path(root) and is_safe_to_modify(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Realiza un recorrido recursivo por el sistema de archivos buscando archivos candidatos."""
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    visited_dirs: set[str] = set()

    def _scan_dir(current_dir: Path) -> None:
        try:
            resolved_dir = current_dir.resolve(strict=True)
            if not is_safe_to_modify(resolved_dir):
                return
            dir_str = str(resolved_dir)
            if dir_str in visited_dirs:
                return
            visited_dirs.add(dir_str)
            
            with os.scandir(dir_str) as iterator:
                for entry in iterator:
                    try:
                        entry_path = Path(entry.path)
                        if not is_safe_to_modify(entry_path):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if not is_junction(entry_path):
                                _scan_dir(entry_path)
                        else:
                            st_result = entry.stat(follow_symlinks=False)
                            if st_result.st_size >= min_size:
                                if _is_valid_candidate(entry_path, st_result):
                                    size_to_paths_map[st_result.st_size].append(entry_path)
                    except (FileNotFoundError, OSError, PermissionError, ValueError):
                        continue
        except (OSError, PermissionError, ValueError):
            return

    for item in directories:
        if (root := _resolve_and_verify_root(item)):
            _scan_dir(root)
            
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Clasifica una lista de archivos según el hash provisto por la función hash_func."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """
    Selecciona entre hash completo o parcial para identificar duplicados reales.
    Si el archivo es mayor al umbral, usa hash parcial primero como filtro.
    """
    if not paths or size < 0:
        return []

    # Estrategia: Hash completo directo para archivos pequeños, o filtrado por parcial.
    if size <= PARTIAL_READ_BYTES:
        final_groups = _group_paths_by_hash(paths, hash_file)
    else:
        partial_groups = _group_paths_by_hash(paths, partial_hash)
        final_groups = {}
        for candidate_subset in partial_groups.values():
            # Refinamiento: solo los que pasaron el filtro parcial se procesan con hash completo
            full_hash_groups = _group_paths_by_hash(candidate_subset, hash_file)
            final_groups.update(full_hash_groups)
            
    return [DuplicateGroup(d, size, sorted(p)) for d, p in final_groups.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquestador principal: identifica y agrupa duplicados en los directorios indicados."""
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el total de bytes que se recuperarían eliminando duplicados redundantes."""
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def _get_keeper_score(path: Path) -> Optional[Tuple[float, int]]:
    """
    Calcula una puntuación heurística para sugerir qué archivo conservar 
    (prioriza los más antiguos y rutas más cortas).
    """
    try:
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, PermissionError, ValueError, AttributeError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Determina, dentro de un grupo, cuál es el mejor archivo para conservar."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    
    candidates = []
    for p in group.paths:
        if not isinstance(p, Path):
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
    """Genera una representación textual legible de un grupo de duplicados para la UI."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return ["Error: Grupo inválido o vacío"]
        
    keeper = suggest_keeper(group)
    mb_t, mb_w = round(group.size_bytes / 1048576, 2), round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_t} MB (recuperable: {mb_w} MB)"]
    
    for path in group.paths:
        if not isinstance(path, Path):
            lines.append(f"   [error] ruta inválida")
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
