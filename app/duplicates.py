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
# Se utiliza en bitwise AND con los atributos del archivo obtenidos vía stat().
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
        """Calcula el espacio total que se liberaría borrando todos menos uno."""
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def hash_file(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del archivo para confirmación de identidad.
    Utiliza lectura en bloques para optimizar el consumo de memoria en archivos grandes.
    """
    if path is None: return None
    try:
        path_obj = Path(path)
        if not path_obj.is_file() or is_protected_path(path_obj): return None
        
        digest = hashlib.sha256()
        with open(path_obj, "rb") as f:
            while (buffer := f.read(chunk_size)):
                digest.update(buffer)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError, IsADirectoryError, ValueError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Calcula el hash SHA256 de los primeros N bytes como huella dactilar rápida.
    Permite descartar candidatos que difieren en el inicio del archivo.
    """
    if path is None: return None
    try:
        path_obj = Path(path)
        if not path_obj.is_file() or is_protected_path(path_obj): return None
        
        with open(path_obj, "rb") as f:
            content = f.read(read_bytes)
            if not content: return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError, IsADirectoryError, ValueError):
        return None


def _is_valid_candidate(path: Path) -> bool:
    """Validador central: verifica existencia, tipo archivo, permisos y protección."""
    if not isinstance(path, Path): return False
    try:
        return path.exists() and path.is_file() and not is_protected_path(path) and os.access(path, os.R_OK)
    except (OSError, ValueError):
        return False


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """Agrupa una lista plana de archivos por su tamaño en bytes."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None: return groups
    
    for p in paths:
        if not isinstance(p, Path): continue
        try:
            if _is_valid_candidate(p):
                st = p.stat()
                if st.st_size > 0:
                    groups[st.st_size].append(p)
        except (OSError, PermissionError):
            continue
    return groups


def _resolve_and_verify_root(item: Union[str, Path]) -> Optional[Path]:
    """Normaliza rutas de entrada y asegura que sean directorios accesibles."""
    try:
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
    Exploración recursiva del sistema de archivos mediante seguimiento de
    dispositivo e inodo para evitar ciclos (bucles de enlaces simbólicos).
    """
    temp_map: Dict[int, List[Path]] = defaultdict(list)
    visited_device_inodes: Set[Tuple[int, int]] = set()

    def _should_skip(path: Path) -> bool:
        return skip_protected and is_protected_path(path)

    def _scan_recursive(current_dir: Path) -> None:
        """Recorre directorios evitando punteros recursivos o sistemas protegidos."""
        try:
            for entry in current_dir.iterdir():
                if _should_skip(entry): continue
                
                try:
                    if entry.is_symlink(): continue
                    # Evitar seguir puntos de reparse (Junctions) en Windows
                    if os.name == 'nt' and (entry.stat().st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT):
                        continue
                    
                    if entry.is_dir():
                        resolved_entry = entry.resolve()
                        if is_protected_path(resolved_entry): continue
                            
                        stat = entry.stat()
                        dev_inode = (stat.st_dev, stat.st_ino)
                        if dev_inode not in visited_device_inodes:
                            visited_device_inodes.add(dev_inode)
                            _scan_recursive(entry)
                    else:
                        stat = entry.stat()
                        if stat.st_size >= min_size:
                            temp_map[int(stat.st_size)].append(entry)
                except (OSError, PermissionError): continue
        except (OSError, PermissionError): pass

    if directories:
        roots = {r for item in directories if (r := _resolve_and_verify_root(item))}
        for root in roots:
            _scan_recursive(root)
            
    return {size: files for size, files in temp_map.items() if len(files) > 1}


def _refine_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """
    Función de utilidad para particionar grupos de archivos utilizando una función
    de hashing proporcionada. Filtra archivos inaccesibles durante el proceso.
    """
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if isinstance(path, Path) and _is_valid_candidate(path) and (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _resolve_by_hashes(candidates: List[Path]) -> Dict[str, List[Path]]:
    """Refina colisiones de tamaño usando hash parcial seguido de hash completo."""
    partial_results = _refine_by_hash(candidates, partial_hash)
    final_groups: Dict[str, List[Path]] = {}
    for subset in partial_results.values():
        final_groups.update(_refine_by_hash(subset, hash_file))
    return final_groups


def _process_size_group(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """Pipeline: Determina si el hash parcial es suficiente o requiere verificación completa."""
    valid_paths = [p for p in paths if isinstance(p, Path) and _is_valid_candidate(p)]
    if len(valid_paths) < 2: return []
    
    # Si el archivo es menor o igual al bloque parcial, hash parcial confirma identidad
    results = _refine_by_hash(valid_paths, partial_hash) if size <= PARTIAL_READ_BYTES else _resolve_by_hashes(valid_paths)
            
    confirmed_groups: List[DuplicateGroup] = []
    for digest, confirmed_paths in results.items():
        confirmed_groups.append(DuplicateGroup(digest, size, sorted(confirmed_paths)))
    return confirmed_groups


def find_duplicates(directories: Iterable[Union[str, Path]], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Punto de entrada principal: identifica, agrupa y ordena duplicados por potencial de ahorro."""
    if directories is None or min_size < 0: return []
    groups: List[DuplicateGroup] = []
    for size, paths in _collect_candidates(directories, min_size, skip_protected).items():
        groups.extend(_process_size_group(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el sumatorio del espacio recuperable de una colección de grupos."""
    if not groups or not isinstance(groups, (list, tuple)): return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """
    Heurística de selección: sugiere el archivo original basándose en:
    1. Menor fecha de última modificación.
    2. En empate, la ruta más corta (prioriza directorios de nivel superior).
    """
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
        
    candidates: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        if not isinstance(p, Path): continue
        try:
            if _is_valid_candidate(p):
                stat_info = p.stat()
                candidates.append((float(stat_info.st_mtime), len(str(p)), p))
        except (OSError, PermissionError):
            continue
            
    if not candidates:
        return None
        
    return min(candidates, key=lambda x: (x[0], x[1]))[2]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una representación textual formateada del grupo para reporte."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
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
