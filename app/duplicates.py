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
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Callable, Dict, List, Optional, Union, Tuple, Sequence, Set

from safety import is_protected_path

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

# Cuántos bytes leer para el hash parcial. 64 KB alcanza para descartar
# archivos distintos y es una lectura despreciable incluso en discos lentos.
PARTIAL_READ_BYTES: int = 64 * 1024

# Constante para identificar puntos de reparse (Junctions/Symlinks) en Windows.
FILE_ATTRIBUTE_REPARSE_POINT: int = 0x400


@dataclass
class DuplicateGroup:
    """Representa una colección de archivos que comparten contenido idéntico."""
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Retorna la cantidad de archivos duplicados en este grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """
        Calcula el espacio total que se liberaría borrando todos los duplicados,
        conservando únicamente una instancia (la elegida por suggest_keeper).
        """
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def hash_file(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del archivo mediante bloques de memoria constante.
    """
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                buffer = f.read(chunk_size)
                if not buffer:
                    break
                digest.update(buffer)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError, ValueError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Genera una huella dactilar rápida leyendo solo el inicio del archivo.
    """
    try:
        with open(path, "rb") as f:
            content = f.read(read_bytes)
            if not content: return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError, ValueError):
        return None


def _is_valid_candidate(path: Path) -> bool:
    """
    Valida si una ruta es un archivo legible que no pertenece a áreas protegidas.
    """
    try:
        return path.exists() and path.is_file() and not is_protected_path(path) and os.access(path, os.R_OK)
    except (OSError, ValueError):
        return False


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """
    Agrupa una lista de rutas de archivo según su tamaño en bytes.
    """
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None: return groups
    
    for p in paths:
        if isinstance(p, Path) and _is_valid_candidate(p):
            try:
                size = p.stat().st_size
                if size > 0:
                    groups[size].append(p)
            except (OSError, PermissionError):
                continue
    return groups


def _resolve_and_verify_root(item: Union[str, Path]) -> Optional[Path]:
    """Normaliza una ruta y verifica que sea un directorio no protegido."""
    try:
        if not item: return None
        root = Path(item).resolve(strict=False)
        if root.is_dir() and not is_protected_path(root):
            return root
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def _collect_candidates(
    directories: Iterable[Union[str, Path]], 
    min_size: int, 
    skip_protected: bool
) -> Dict[int, List[Path]]:
    """
    Escaneo recursivo del sistema utilizando os.scandir, filtrando por tamaño mínimo
    y evitando la recursión infinita en puntos de reparse.
    """
    temp_map: Dict[int, List[Path]] = defaultdict(list)
    visited_device_inodes: Set[Tuple[int, int]] = set()

    def _scan_recursive(current_dir: Path) -> None:
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        entry_path = Path(entry.path)
                        if skip_protected and is_protected_path(entry_path):
                            continue

                        stat = entry.stat(follow_symlinks=False)
                        
                        if entry.is_symlink() or (os.name == 'nt' and (stat.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)):
                            continue
                        
                        if entry.is_dir():
                            dev_inode = (stat.st_dev, stat.st_ino)
                            if dev_inode not in visited_device_inodes:
                                visited_device_inodes.add(dev_inode)
                                _scan_recursive(entry_path)
                        elif entry.is_file() and stat.st_size >= min_size:
                            if os.access(entry_path, os.R_OK):
                                temp_map[int(stat.st_size)].append(entry_path)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            pass

    if directories and isinstance(directories, Iterable):
        roots = {r for item in directories if item and (r := _resolve_and_verify_root(item))}
        for root in roots:
            _scan_recursive(root)
            
    return {size: files for size, files in temp_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Agrupa una lista de rutas basándose en el digest generado por hash_func."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _refine_by_deep_hash(candidates: List[Path]) -> Dict[str, List[Path]]:
    """Aplica doble filtrado: hash parcial primero, luego hash completo (SHA256) para confirmar."""
    partial_results: Dict[str, List[Path]] = _group_paths_by_hash(candidates, partial_hash)
    final_groups: Dict[str, List[Path]] = {}
    for subset in partial_results.values():
        final_groups.update(_group_paths_by_hash(subset, hash_file))
    return final_groups


def _process_size_group(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Pipeline de hashing: decide la profundidad del análisis según el tamaño del archivo."""
    if len(paths) < 2: 
        return []
    
    # Si el archivo es pequeño, un hash parcial es suficiente identificador
    if size <= PARTIAL_READ_BYTES:
        results = _group_paths_by_hash(paths, partial_hash)
    else:
        results = _refine_by_deep_hash(paths)
            
    return [DuplicateGroup(digest, size, sorted(confirmed_paths)) for digest, confirmed_paths in results.items()]


def find_duplicates(directories: Iterable[Union[str, Path]], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Punto de entrada: identifica y ordena grupos de duplicados por impacto (wasted_bytes)."""
    if directories is None or not isinstance(directories, Iterable) or isinstance(directories, (str, Path)): 
        return []
    if not isinstance(min_size, int) or min_size < 0: 
        return []
        
    groups: List[DuplicateGroup] = []
    for size, paths in _collect_candidates(directories, min_size, skip_protected).items():
        groups.extend(_process_size_group(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma total de bytes recuperables de una lista de grupos."""
    if not groups or not isinstance(groups, (list, tuple)): return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Heurística para sugerir el original: prioriza mtime antiguo y ruta más corta."""
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
            
    return min(candidates, key=lambda x: (x[0], x[1]))[2] if candidates else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Transforma un DuplicateGroup en líneas descriptivas para la UI."""
    if not isinstance(group, DuplicateGroup) or not hasattr(group, 'paths') or group.paths is None:
        return []
        
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    
    for path in group.paths:
        if not isinstance(path, Path) or not _is_valid_candidate(path):
            lines.append(f"   [inaccesible] {path}")
            continue
        label = 'conservar' if keeper is not None and path == keeper else 'duplicado'
        lines.append(f"   [{label}] {path}")
    return lines
