"""
duplicates.py — detección de archivos duplicados.

SOLO LECTURA: este módulo encuentra y agrupa duplicados, y sugiere cuál
conservar, pero **nunca borra ni mueve nada**.

Estrategia en tres pasos:
| Paso | Técnica | Propósito |
| :--- | :--- | :--- |
| 1 | Tamaño (stat) | Descarta archivos únicos rápidamente (comparación O(1)). |
| 2 | Hash Parcial | Filtra falsos positivos leyendo solo el inicio (64KB). |
| 3 | Hash Completo | Confirmación final mediante SHA256 del contenido total. |
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
    """Verifica atributos de sistema/oculto; evita el procesamiento de archivos críticos del OS."""
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
        """Calcula el espacio total recuperable excluyendo una instancia (la original)."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """Verifica si el archivo está en uso exclusivo, manejando errores de sistema durante la apertura."""
    try:
        with open(path, 'rb') as f:
            # Intenta una lectura mínima para asegurar accesibilidad real
            f.read(1)
            return False
    except (OSError, PermissionError, FileNotFoundError, BlockingIOError):
        return True


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo tras validar permisos y estado de bloqueo."""
    if path is None or chunk_size <= 0:
        return None
        
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size == 0 or is_protected_path(p) or not is_safe_to_modify(p):
            return None
            
        digest = hashlib.sha256()
        with open(p, "rb") as f:
            while (buffer := f.read(chunk_size)):
                digest.update(buffer)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Crea una huella dactilar rápida leyendo solo el inicio para descartar archivos claramente distintos."""
    if path is None or read_bytes <= 0:
        return None

    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size == 0 or is_protected_path(p) or not is_safe_to_modify(p):
            return None

        with open(p, "rb") as f:
            content = f.read(read_bytes)
            if not content:
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def _is_valid_candidate(path: Path, st: Optional[os.stat_result] = None) -> bool:
    """Valida que el archivo sea procesable (no oculto, no sistema, no bloqueado, no link)."""
    try:
        if path.is_symlink() or is_protected_path(path) or not is_safe_to_modify(path) or _is_file_locked(path):
            return False
        if is_system_or_hidden(path):
            return False
            
        st = st or path.stat()
        return st.st_size > 0 and st.st_nlink == 1
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def _should_include_entry(entry: os.DirEntry, min_size: int) -> bool:
    """Lógica central de filtrado: determina si un objeto del sistema de archivos debe ser indexado."""
    try:
        path = Path(entry.path)
        if not is_safe_to_modify(path) or is_protected_path(path):
            return False
        if entry.is_file(follow_symlinks=False):
            return entry.stat().st_size >= min_size and _is_valid_candidate(path)
        return False
    except OSError:
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa archivos por su tamaño en disco, optimizando la pre-selección."""
    groups: Dict[int, List[Path]] = defaultdict(list)
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
    """Normaliza y valida que la ruta raíz sea un directorio seguro y existente."""
    try:
        if not item: return None
        root = Path(item).resolve(strict=False)
        if root.exists() and root.is_dir() and not is_protected_path(root) and is_safe_to_modify(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Escaneo recursivo de directorios buscando archivos candidatos indexados por tamaño."""
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    visited_dirs: set[str] = set()

    def _scan_dir(current_dir: Path) -> None:
        dir_str = str(current_dir.resolve())
        if dir_str in visited_dirs or is_protected_path(current_dir) or not is_safe_to_modify(current_dir):
            return
        visited_dirs.add(dir_str)
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    if entry.is_dir(follow_symlinks=False):
                        if not is_junction(Path(entry.path)):
                            _scan_dir(Path(entry.path))
                    elif _should_include_entry(entry, min_size):
                        size_to_paths_map[entry.stat().st_size].append(Path(entry.path))
        except (OSError, PermissionError):
            pass

    for item in directories:
        if (root := _resolve_and_verify_root(item)):
            _scan_dir(root)
            
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Divide un conjunto de archivos en grupos basados en el resultado de una función de hashing."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if path is not None and (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Optimización de E/S: usa hash parcial para archivos grandes, completo para pequeños."""
    if size <= PARTIAL_READ_BYTES:
        results = _group_paths_by_hash(paths, hash_file)
    else:
        results = _group_paths_by_hash(paths, partial_hash)
        final_groups: Dict[str, List[Path]] = {}
        for subset in results.values():
            final_groups.update(_group_paths_by_hash(subset, hash_file))
        results = final_groups
    return [DuplicateGroup(digest, size, sorted(p)) for digest, p in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquestador: coordina la recolección, filtrado por hash y creación de grupos."""
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma total de bytes desperdiciados en una lista de grupos."""
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Heurística para sugerir el archivo a conservar (preferencia: más antiguo, luego ruta corta)."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    candidates: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        try:
            stat = p.stat()
            candidates.append((float(stat.st_mtime), len(str(p)), p))
        except (OSError, PermissionError):
            continue
    return min(candidates, key=lambda x: (x[0], x[1]))[2] if candidates else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Formatea la información de un grupo para la visualización en la UI."""
    if not isinstance(group, DuplicateGroup):
        return ["Error: Grupo inválido"]
        
    keeper = suggest_keeper(group)
    mb_t, mb_w = round(group.size_bytes / 1048576, 2), round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_t} MB (recuperable: {mb_w} MB)"]
    
    for path in group.paths:
        if not path.exists():
            lines.append(f"   [desaparecido] {path}")
        elif not _is_valid_candidate(path):
            lines.append(f"   [inaccesible] {path}")
        else:
            label = 'conservar' if (keeper and path == keeper) else 'duplicado'
            lines.append(f"   [{label}] {path}")
    return lines
