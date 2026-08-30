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
    Calcula el hash SHA256 completo. 
    Se utiliza lectura en bloques (chunked) para garantizar un uso constante 
    y predecible de memoria, independientemente del tamaño del archivo.
    """
    path_obj = Path(path) if path is not None else None
    if path_obj is None or not _is_valid_candidate(path_obj):
        return None
    try:
        digest = hashlib.sha256()
        with open(path_obj, "rb") as f:
            while True:
                buffer = f.read(chunk_size)
                if not buffer:
                    break
                digest.update(buffer)
        return digest.hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Genera una 'huella dactilar' rápida leyendo solo el inicio del archivo.
    Es vital para descartar candidatos de manera eficiente antes de realizar 
    lecturas I/O costosas sobre archivos grandes.
    """
    path_obj = Path(path) if path is not None else None
    if path_obj is None or not _is_valid_candidate(path_obj):
        return None
    try:
        with open(path_obj, "rb") as f:
            content = f.read(read_bytes)
            if not content: return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def _is_valid_candidate(path: Path) -> bool:
    """
    Filtro de seguridad obligatorio para evitar operar sobre rutas del sistema,
    archivos bloqueados por el SO o directorios protegidos.
    """
    if not isinstance(path, Path): return False
    try:
        return path.exists() and path.is_file() and not is_protected_path(path) and os.access(path, os.R_OK)
    except (OSError, ValueError):
        return False


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """
    Clasifica archivos por tamaño. Los archivos de tamaño único se descartan
    tempranamente, ya que no pueden ser duplicados.
    """
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
    Escaneo recursivo que utiliza el dispositivo e inodo para prevenir ciclos 
    infinitos causados por enlaces simbólicos o puntos de reparse (junctions).
    """
    temp_map: Dict[int, List[Path]] = defaultdict(list)
    visited_device_inodes: Set[Tuple[int, int]] = set()

    def _scan_recursive(current_dir: Path) -> None:
        try:
            for entry in current_dir.iterdir():
                if skip_protected and is_protected_path(entry):
                    continue
                
                try:
                    stat = entry.stat()
                    if os.name == 'nt' and (stat.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT):
                        continue
                    
                    if entry.is_dir():
                        dev_inode = (stat.st_dev, stat.st_ino)
                        if dev_inode not in visited_device_inodes:
                            visited_device_inodes.add(dev_inode)
                            _scan_recursive(entry)
                    elif stat.st_size >= min_size:
                        temp_map[int(stat.st_size)].append(entry)
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            pass

    if directories:
        roots = {r for item in directories if (r := _resolve_and_verify_root(item))}
        for root in roots:
            _scan_recursive(root)
            
    return {size: files for size, files in temp_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Agrupa rutas que comparten un mismo digest devuelto por hash_func."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if isinstance(path, Path) and (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _refine_by_deep_hash(candidates: List[Path]) -> Dict[str, List[Path]]:
    """
    Refinamiento iterativo: reduce el conjunto de candidatos mediante un hash 
    parcial (rápido) seguido de un hash completo (lento pero preciso).
    """
    partial_results = _group_paths_by_hash(candidates, partial_hash)
    final_groups: Dict[str, List[Path]] = {}
    for subset in partial_results.values():
        final_groups.update(_group_paths_by_hash(subset, hash_file))
    return final_groups


def _process_size_group(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """
    Pipeline de confirmación: decide el nivel de hashing basado en el tamaño.
    Optimización: Si el archivo cabe en un buffer de hash parcial, no se 
    realiza una segunda lectura (ahorro de I/O).
    """
    valid_paths = [p for p in paths if isinstance(p, Path) and _is_valid_candidate(p)]
    if len(valid_paths) < 2: 
        return []
    
    if size <= PARTIAL_READ_BYTES:
        results = _group_paths_by_hash(valid_paths, partial_hash)
    else:
        results = _refine_by_deep_hash(valid_paths)
            
    confirmed_groups: List[DuplicateGroup] = []
    for digest, confirmed_paths in results.items():
        confirmed_groups.append(DuplicateGroup(digest, size, sorted(confirmed_paths)))
    return confirmed_groups


def find_duplicates(directories: Iterable[Union[str, Path]], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Punto de entrada: identifica y ordena grupos de duplicados por impacto (wasted_bytes)."""
    if not isinstance(directories, Iterable) or isinstance(directories, (str, Path)): 
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
    """
    Heurística de selección de archivo 'original':
    Prioriza el más antiguo (mtime) y luego la ruta más corta, sin re-estadear archivos.
    """
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
        
    candidates: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        try:
            # Reutilizamos el stat del sistema de archivos implícito en el acceso
            stat_info = p.stat()
            candidates.append((float(stat_info.st_mtime), len(str(p)), p))
        except (OSError, PermissionError):
            continue
            
    return min(candidates, key=lambda x: (x[0], x[1]))[2] if candidates else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Transforma un DuplicateGroup en líneas de texto descriptivas para UI/Reporte."""
    if not isinstance(group, DuplicateGroup) or group.paths is None:
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
