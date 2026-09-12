"""
duplicates.py — detección de archivos duplicados.

SOLO LECTURA: este módulo encuentra y agrupa duplicados, y sugiere cuál
conservar, pero **nunca borra ni mueve nada**.

Estrategia en tres pasos:
| Paso | Técnica | Propósito |
| :--- | :--- | :--- |
| 1 | Tamaño (stat) | Descarta archivos únicos rápidamente (comparación O(1)). |
| 2 | Hash Parcial | Filtra falsos positivos (igual tamaño, distinto contenido) leyendo solo el inicio. |
| 3 | Hash Completo | Confirmación final de identidad (SHA256) del contenido total. |
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
    """Verifica si una ruta es un punto de reparse (junction) en Windows."""
    if not isinstance(path, Path):
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
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
        """Calcula el espacio total recuperable excluyendo una instancia del total."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """Intenta abrir el archivo en modo exclusivo para detectar bloqueos de E/S."""
    try:
        with open(path, 'ab'):
            return False
    except (OSError, PermissionError):
        return True


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo del archivo tras validar permisos."""
    if path is None or chunk_size <= 0:
        return None
        
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size == 0 or is_protected_path(p) or not is_safe_to_modify(p) or _is_file_locked(p):
            return None
            
        digest = hashlib.sha256()
        with open(p, "rb") as f:
            while (buffer := f.read(chunk_size)):
                digest.update(buffer)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Calcula el hash SHA256 sobre una porción inicial (64KB por defecto)."""
    if path is None or read_bytes <= 0:
        return None

    try:
        p = Path(path)
        st = p.stat()
        if not p.is_file() or st.st_size == 0 or is_protected_path(p) or not is_safe_to_modify(p) or _is_file_locked(p):
            return None

        # Si el archivo es menor al buffer, el hash parcial es el hash total
        with open(p, "rb") as f:
            content = f.read(read_bytes)
            if not content:
                return None
            if st.st_size <= read_bytes:
                return hashlib.sha256(content).hexdigest()
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def _is_valid_candidate(path: Path, st: Optional[os.stat_result] = None) -> bool:
    """Filtra archivos inválidos, protegidos o bloqueados para el escaneo."""
    try:
        if path.is_symlink() or is_protected_path(path) or not is_safe_to_modify(path) or _is_file_locked(path):
            return False
        if is_system_or_hidden(path):
            return False
            
        st = st or path.stat()
        return st.st_size > 0 and st.st_nlink == 1
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Clasifica rutas por tamaño en bytes, filtrando candidatos no procesables."""
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
    """Resuelve la ruta absoluta y verifica si es un directorio escaneable."""
    try:
        if not item: return None
        root = Path(item).resolve(strict=False)
        if root.exists() and root.is_dir() and not is_protected_path(root) and is_safe_to_modify(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Recolección recursiva de archivos candidatos indexados por tamaño."""
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    visited_dirs: set[str] = set()

    def _process_entry(entry: os.DirEntry, current_dir: Path) -> None:
        entry_path = Path(entry.path)
        if not entry_path.exists() or is_protected_path(entry_path) or entry_path.is_symlink():
            return
        
        if entry.is_dir(follow_symlinks=False):
            if not is_junction(entry_path) and is_safe_to_modify(entry_path):
                _scan_dir(entry_path)
        elif entry.is_file(follow_symlinks=False):
            file_stat = entry.stat()
            if file_stat.st_size >= min_size and _is_valid_candidate(entry_path, file_stat):
                size_to_paths_map[file_stat.st_size].append(entry_path)

    def _scan_dir(current_dir: Path) -> None:
        dir_str = str(current_dir.resolve())
        if dir_str in visited_dirs or is_protected_path(current_dir):
            return
        visited_dirs.add(dir_str)
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    _process_entry(entry, current_dir)
        except (OSError, PermissionError):
            pass

    for item in directories:
        if (root := _resolve_and_verify_root(item)):
            _scan_dir(root)
            
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Agrupa rutas aplicando una función de hash y filtrando resultados únicos."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if path is not None and (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona la estrategia de hashing según el tamaño para optimizar E/S."""
    if size <= PARTIAL_READ_BYTES:
        results = _group_paths_by_hash(paths, hash_file)
    else:
        results = _group_paths_by_hash(paths, partial_hash)
        # Solo refinamos con hash completo si hay colisiones en el hash parcial
        final_groups: Dict[str, List[Path]] = {}
        for subset in results.values():
            final_groups.update(_group_paths_by_hash(subset, hash_file))
        results = final_groups
    return [DuplicateGroup(digest, size, sorted(p)) for digest, p in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquestador principal para la detección de duplicados."""
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el total de bytes liberables acumulado."""
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Heurística: conserva el archivo más antiguo (mtime) o el de ruta más corta."""
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
    """Genera representación textual para la interfaz con etiquetas de estado."""
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
