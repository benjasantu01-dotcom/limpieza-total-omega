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
import logging
from collections import defaultdict, deque
from collections.abc import Sequence, Iterable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Optional, Union, TypeAlias, Tuple, Set

from safety import is_protected_path, is_safe_to_modify

PathLike: TypeAlias = Union[str, Path]
# Representa un par (ID de dispositivo, Número de inodo) para identificar archivos de forma única
InodeMap: TypeAlias = Set[Tuple[int, int]]

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
logger = logging.getLogger(__name__)

class HashStrategy(Enum):
    COMPLETE = auto()
    CASCADE = auto()

class Win32Attributes:
    REPARSE_POINT: int = 0x400  
    HIDDEN: int = 0x2          
    SYSTEM: int = 0x4          

MAX_RECURSION_DEPTH: int = 100
MAX_PATH_LENGTH: int = 260

# Cachés de nivel de módulo para evitar E/S redundante en archivos ya procesados
_partial_cache: Dict[Path, str] = {}
_full_hash_cache: Dict[Path, str] = {}

def is_junction(path: Path) -> bool:
    """Verifica si la ruta es un punto de unión de NTFS (reparse point) usando la API de Windows."""
    with suppress(AttributeError, OSError, RuntimeError):
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & Win32Attributes.REPARSE_POINT))
    return False

def is_system_or_hidden(path: Path) -> bool:
    """Verifica si la ruta tiene los atributos de sistema o archivo oculto configurados."""
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & (Win32Attributes.HIDDEN | Win32Attributes.SYSTEM)))
    except (AttributeError, OSError, RuntimeError):
        return False

def is_file_locked(path: Path) -> bool:
    """
    Determina si un archivo está bloqueado intentando abrirlo en modo lectura exclusiva.
    Esta función es puramente informativa y no modifica el estado del archivo.
    """
    try:
        with open(path, 'rb'):
            return False
    except (PermissionError, OSError, FileNotFoundError):
        return True

@dataclass
class DuplicateGroup:
    """Contenedor de archivos que comparten el mismo hash y tamaño."""
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        return len(self.paths)

    @property
    def wasted_bytes(self) -> int:
        return (self.count - 1) * self.size_bytes if self.count > 1 else 0

def _safe_path_check(path: Path) -> bool:
    """
    Valida la integridad de la ruta frente a restricciones de seguridad.
    Verifica longitud máxima, protecciones del sistema y puntos de reparse (junctions).
    """
    if not path or len(str(path)) >= MAX_PATH_LENGTH: return False
    return is_safe_to_modify(path) and not is_protected_path(path) and not is_junction(path)

def _is_valid_candidate(path: Path) -> bool:
    """Filtra archivos según atributos de sistema y seguridad antes de procesarlos."""
    return not is_system_or_hidden(path) and _safe_path_check(path)

def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Genera SHA256 completo del archivo; utiliza caché para evitar re-lectura innecesaria."""
    if path in _full_hash_cache: return _full_hash_cache[path]
    if not _is_valid_candidate(path) or is_file_locked(path): return None
    
    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as f:
            while (chunk := f.read(chunk_size)):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        _full_hash_cache[path] = digest
        return digest
    except (OSError, PermissionError, IOError):
        return None

def partial_hash(path: Path, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """Genera un hash rápido de los primeros bytes (64KB) para descartar duplicados rápidamente."""
    if path in _partial_cache: return _partial_cache[path]
    if not _is_valid_candidate(path) or is_file_locked(path): return None
    
    try:
        with open(path, "rb") as f:
            header = f.read(read_bytes)
            if not header: return None
            digest_val = hashlib.sha256(header).hexdigest()
            _partial_cache[path] = digest_val
            return digest_val
    except (OSError, PermissionError, IOError):
        return None

def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """
    Agrupa rutas por su tamaño en disco. 
    Ignora archivos no válidos o inaccesibles basándose en _is_valid_candidate.
    """
    groups_by_size: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        item = Path(p)
        if _is_valid_candidate(item):
            try:
                st = item.stat(follow_symlinks=False)
                if st.st_size > 0: groups_by_size[st.st_size].append(item)
            except (OSError, FileNotFoundError):
                continue
    return dict(groups_by_size)

def _process_entry(
    entry: os.DirEntry, 
    depth: int, 
    min_size: int, 
    queue: deque[Tuple[Path, int]], 
    visited_inodes: InodeMap, 
    visited_files: InodeMap,
    size_map: Dict[int, List[Path]]
) -> None:
    """Procesamiento interno usando la stat ya obtenida por os.scandir."""
    try:
        path = Path(entry.path)
        if not _is_valid_candidate(path):
            return
        
        stat = entry.stat(follow_symlinks=False)
            
        if entry.is_dir():
            if depth < MAX_RECURSION_DEPTH:
                inode = (stat.st_dev, stat.st_ino)
                if inode not in visited_inodes:
                    visited_inodes.add(inode)
                    queue.append((path, depth + 1))
        elif entry.is_file() and stat.st_size >= min_size:
            inode = (stat.st_dev, stat.st_ino)
            if inode not in visited_files:
                visited_files.add(inode)
                size_map[stat.st_size].append(path)
    except (OSError, PermissionError):
        pass

def _collect_candidates(directories: Iterable[PathLike], min_size: int) -> Dict[int, List[Path]]:
    """Recorre recursivamente los directorios seleccionados buscando candidatos a duplicados."""
    size_map: Dict[int, List[Path]] = defaultdict(list)
    visited_dirs: InodeMap = set()
    visited_files: InodeMap = set()
    queue: deque[Tuple[Path, int]] = deque()
    
    for d in directories:
        p = Path(d)
        if _is_valid_candidate(p): queue.append((p, 0))
    
    while queue:
        current_dir, depth = queue.popleft()
        with suppress(OSError, PermissionError):
            with os.scandir(current_dir) as it:
                for entry in it:
                    _process_entry(entry, depth, min_size, queue, visited_dirs, visited_files, size_map)
    return {sz: ps for sz, ps in size_map.items() if len(ps) > 1}

def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Agrupa rutas que devuelven un mismo hash mediante la función proporcionada."""
    groups: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        # Nota: El tamaño ya fue validado en _collect_candidates o grupo_by_size
        if (digest := hash_func(path)): groups[digest].append(path)
    return {d: p for d, p in groups.items() if len(p) > 1}

def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """Aplica cascada: primero reduce el conjunto con hash parcial, luego verifica con hash completo."""
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final: Dict[str, List[Path]] = {}
    for subset in partial_groups.values():
        final.update(_group_paths_by_hash(subset, hash_file))
    return final

def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Elige si procesar con hash completo o cascada según el tamaño del archivo."""
    if size <= 0: return []
    results = _group_paths_by_hash(paths, hash_file) if size <= PARTIAL_READ_BYTES else _process_large_file_subset(paths)
    return [DuplicateGroup(d, size, sorted(p)) for d, p in results.items()]

def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024) -> List[DuplicateGroup]:
    """Punto de entrada principal para buscar duplicados en directorios dados."""
    _partial_cache.clear()
    _full_hash_cache.clear()
    size_map = _collect_candidates(directories, min_size)
    if not size_map: return []
    groups = [g for sz, ps in size_map.items() for g in _decide_hash_strategy_and_process(sz, ps)]
    return sorted(groups, key=lambda g: g.wasted_bytes, reverse=True)

def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el total de bytes que podrían liberarse si se eliminaran todos los duplicados."""
    return sum(g.wasted_bytes for g in groups) if groups else 0

def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """Sugiere el archivo más antiguo del grupo para ser conservado como 'original'."""
    if not group.paths: return None
    candidates: List[Tuple[float, Path]] = []
    for p in group.paths:
        try:
            candidates.append((p.stat().st_mtime, p))
        except (OSError, PermissionError): continue
    return min(candidates, key=lambda x: x[0])[1] if candidates else group.paths[0]

def format_group(group: DuplicateGroup) -> List[str]:
    """Retorna una representación textual formateada del grupo para el informe."""
    if not group.paths: return []
    keeper = suggest_keeper(group)
    mb, wasted = group.size_bytes / 1048576, group.wasted_bytes / 1048576
    lines = [f"{group.count} copias de {mb:.2f} MB (recuperable: {wasted:.2f} MB)"]
    for p in group.paths:
        lines.append(f"   {'[conservar]' if p == keeper else '[duplicado]'} {p}")
    return lines
