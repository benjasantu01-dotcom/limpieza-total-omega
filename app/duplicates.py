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
    Intenta abrir un archivo en modo lectura para verificar si está bloqueado.
    El acceso exclusivo se detecta mediante la excepción al intentar leer 1 byte.
    """
    try:
        with open(path, 'rb') as f:
            f.read(1)
            return False
    except (OSError, PermissionError, FileNotFoundError, BlockingIOError):
        return True


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del archivo tras validar permisos.
    Retorna None si el acceso es denegado o el archivo falla chequeos de seguridad.
    """
    if path is None or chunk_size <= 0:
        return None
        
    try:
        p = Path(path).resolve(strict=True)
        if not p.is_file() or not is_safe_to_modify(p) or _is_file_locked(p):
            return None
            
        digest = hashlib.sha256()
        with open(p, "rb") as f:
            while (chunk := f.read(chunk_size)):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Hash de los primeros N bytes para pre-filtrado rápido de duplicados grandes.
    Si el archivo es menor a read_bytes, se procesa íntegramente.
    """
    if path is None or read_bytes <= 0:
        return None

    try:
        p = Path(path).resolve(strict=True)
        if not p.is_file() or not is_safe_to_modify(p) or _is_file_locked(p):
            return None

        with open(p, "rb") as f:
            content = f.read(read_bytes)
            if not content: return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def _is_valid_candidate(path: Path, st: os.stat_result) -> bool:
    """
    Filtro de seguridad central. La lógica excluye archivos de sistema,
    junctions (evitando recursión infinita) y archivos bloqueados.
    """
    try:
        if (st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT) if hasattr(st, 'st_file_attributes') else path.is_symlink():
            return False
        if is_protected_path(path) or not is_safe_to_modify(path):
            return False
        if is_system_or_hidden(path):
            return False
            
        return st.st_size > 0 and st.st_nlink == 1 and not _is_file_locked(path)
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa rutas por tamaño para evitar cómputos de hash innecesarios."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if p is None: continue
        try:
            path_obj = Path(p).resolve(strict=True)
            if is_protected_path(path_obj) or not is_safe_to_modify(path_obj):
                continue
            st = path_obj.stat()
            if _is_valid_candidate(path_obj, st):
                groups[st.st_size].append(path_obj)
        except (OSError, RuntimeError):
            continue
    return groups


def _resolve_and_verify_root(item: PathLike) -> Optional[Path]:
    """Verifica que el directorio de inicio sea seguro antes de iniciar el escaneo."""
    try:
        if not item: return None
        root = Path(item).resolve(strict=True)
        if root.is_dir() and not is_protected_path(root) and is_safe_to_modify(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Escaneo recursivo con prevención de ciclos mediante visited_dirs."""
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    visited_dirs: set[str] = set()

    def _scan_dir(current_dir: Path) -> None:
        try:
            real_dir = current_dir.resolve(strict=True)
            dir_str = str(real_dir)
            if dir_str in visited_dirs or is_protected_path(real_dir) or not is_safe_to_modify(real_dir):
                return
            visited_dirs.add(dir_str)
            
            with os.scandir(real_dir) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not is_junction(Path(entry.path)):
                                _scan_dir(Path(entry.path))
                        else:
                            st = entry.stat(follow_symlinks=False)
                            if st.st_size >= min_size:
                                path = Path(entry.path)
                                if _is_valid_candidate(path, st):
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
    """Clasifica archivos por su firma digital (digest)."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """
    Selecciona la estrategia de hashing basada en tamaño para optimizar I/O.
    Retorna la lista de grupos finales confirmados tras el hash completo.
    """
    if size <= PARTIAL_READ_BYTES:
        results = _group_paths_by_hash(paths, hash_file)
    else:
        partial_groups = _group_paths_by_hash(paths, partial_hash)
        results: Dict[str, List[Path]] = {}
        for subset in partial_groups.values():
            results.update(_group_paths_by_hash(subset, hash_file))
            
    return [DuplicateGroup(digest, size, sorted(p)) for digest, p in results.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquestador principal que devuelve grupos de duplicados ordenados por desperdicio."""
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el total de bytes que se podrían liberar excluyendo el archivo original."""
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def _get_keeper_score(path: Path) -> Optional[Tuple[float, int]]:
    """Métrica heurística: menor tiempo de modificación y ruta más corta (más cercana a raíz)."""
    try:
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, PermissionError, ValueError, AttributeError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Selecciona el archivo 'conservar' basado en el score heurístico."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    
    candidates = []
    for p in group.paths:
        if not isinstance(p, Path):
            continue
        try:
            if not p.exists() or not is_safe_to_modify(p):
                continue
            if score := _get_keeper_score(p):
                candidates.append((score, p))
        except (OSError, PermissionError):
            continue
            
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Formatea la información del grupo para visualización en UI."""
    if not isinstance(group, DuplicateGroup):
        return ["Error: Grupo inválido"]
        
    keeper = suggest_keeper(group)
    mb_t, mb_w = round(group.size_bytes / 1048576, 2), round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_t} MB (recuperable: {mb_w} MB)"]
    
    for path in group.paths:
        try:
            if not isinstance(path, Path) or not path.exists() or not is_safe_to_modify(path):
                lines.append(f"   [inaccesible] {path}")
            else:
                label = 'conservar' if (keeper is not None and path == keeper) else 'duplicado'
                lines.append(f"   [{label}] {path}")
        except (OSError, PermissionError):
            lines.append(f"   [error de acceso] {path}")
    return lines
