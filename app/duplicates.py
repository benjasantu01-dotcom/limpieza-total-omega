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
    Intenta abrir un archivo en modo lectura para verificar si está bloqueado por otro proceso.
    Retorna True si el archivo está inaccesible o en uso exclusivo.
    """
    try:
        with open(path, 'rb') as f:
            f.read(1)
            return False
    except (OSError, PermissionError, FileNotFoundError, BlockingIOError):
        return True


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Calcula el hash SHA256 completo del archivo tras validar permisos y seguridad."""
    if path is None or chunk_size <= 0:
        return None
        
    try:
        p = Path(path).resolve()
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
    """Calcula el hash de los primeros N bytes del archivo para una identificación rápida."""
    if path is None or read_bytes <= 0:
        return None

    try:
        p = Path(path).resolve()
        if not p.is_file() or not is_safe_to_modify(p) or _is_file_locked(p):
            return None

        with open(p, "rb") as f:
            content = f.read(read_bytes)
            return hashlib.sha256(content).hexdigest() if content else None
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def _is_valid_candidate(path: Path, st: os.stat_result) -> bool:
    """
    Filtro de seguridad central. Evalúa si un archivo es apto para ser procesado 
    (no es junction, ni protegido, ni sistema, ni está bloqueado).
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


def _should_include_entry(entry: os.DirEntry, min_size: int) -> tuple[bool, Optional[os.stat_result]]:
    """Valida una entrada del sistema de archivos según requisitos de tamaño y seguridad."""
    try:
        st = entry.stat(follow_symlinks=False)
        if st.st_size < min_size:
            return False, None
        path = Path(entry.path)
        if _is_valid_candidate(path, st):
            return True, st
        return False, None
    except OSError:
        return False, None


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa una secuencia de rutas en un diccionario indexado por el tamaño en bytes."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if p is None: continue
        path_obj = Path(p).resolve()
        try:
            st = path_obj.stat()
            if _is_valid_candidate(path_obj, st):
                groups[st.st_size].append(path_obj)
        except OSError:
            continue
    return groups


def _resolve_and_verify_root(item: PathLike) -> Optional[Path]:
    """Normaliza una ruta de entrada y verifica que sea un directorio seguro para escanear."""
    try:
        if not item: return None
        root = Path(item).resolve(strict=False)
        if root.is_dir() and not is_protected_path(root) and is_safe_to_modify(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """Escanea recursivamente los directorios proporcionados buscando archivos candidatos a duplicados."""
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    visited_dirs: set[str] = set()

    def _scan_dir(current_dir: Path) -> None:
        try:
            real_dir = current_dir.resolve()
            dir_str = str(real_dir)
            if dir_str in visited_dirs or is_protected_path(real_dir) or not is_safe_to_modify(real_dir):
                return
            visited_dirs.add(dir_str)
            
            with os.scandir(real_dir) as iterator:
                for entry in iterator:
                    if entry.is_dir(follow_symlinks=False):
                        if not is_junction(Path(entry.path)):
                            _scan_dir(Path(entry.path))
                    else:
                        valid, st = _should_include_entry(entry, min_size)
                        if valid and st:
                            size_to_paths_map[st.st_size].append(Path(entry.path))
        except (OSError, PermissionError):
            pass

    for item in directories:
        if (root := _resolve_and_verify_root(item)):
            _scan_dir(root)
            
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Clasifica una lista de archivos agrupándolos por un hash generado mediante hash_func."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """
    Optimiza el proceso de hashing: archivos pequeños se hashean completamente, 
    archivos grandes se filtran primero por hash parcial.
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
    """Orquestador principal: identifica y agrupa duplicados en los directorios indicados."""
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula la sumatoria total de bytes que se podrían liberar."""
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def _get_keeper_score(path: Path) -> Optional[Tuple[float, int]]:
    """Calcula una métrica heurística para determinar la idoneidad de conservar un archivo."""
    try:
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, PermissionError, ValueError, AttributeError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Selecciona la mejor ruta dentro de un grupo para ser conservada."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    
    candidates = []
    for p in group.paths:
        if not isinstance(p, Path) or not p.exists():
            continue
        if score := _get_keeper_score(p):
            candidates.append((score, p))
            
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una representación textual formateada de un grupo de duplicados para el usuario."""
    if not isinstance(group, DuplicateGroup):
        return ["Error: Grupo inválido"]
        
    keeper = suggest_keeper(group)
    mb_t, mb_w = round(group.size_bytes / 1048576, 2), round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_t} MB (recuperable: {mb_w} MB)"]
    
    for path in group.paths:
        if not isinstance(path, Path) or not path.exists() or not is_safe_to_modify(path):
            lines.append(f"   [inaccesible] {path}")
        else:
            label = 'conservar' if (keeper is not None and path == keeper) else 'duplicado'
            lines.append(f"   [{label}] {path}")
    return lines
