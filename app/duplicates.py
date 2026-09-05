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
import ctypes
from collections import defaultdict
from collections.abc import Sequence, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union, Tuple, Set, Callable

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

# Cuántos bytes leer para el hash parcial. 64 KB es un equilibrio técnico:
# es suficiente para capturar headers únicos en formatos de archivo comunes,
# evitando colisiones en archivos grandes sin penalizar la performance de E/S.
PARTIAL_READ_BYTES: int = 64 * 1024

# Constante de API de Windows para identificar puntos de reparse (Junctions/Symlinks).
FILE_ATTRIBUTE_REPARSE_POINT: int = 0x400


def is_junction(path: Path) -> bool:
    """Verifica si una ruta es un punto de reparse mediante atributos de sistema."""
    if not isinstance(path, Path):
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError):
        return False


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
        Calcula el espacio total recuperable.
        La lógica asume que 1 archivo se conserva y el resto son excedentes.
        """
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def hash_file(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del archivo. 
    Usa un buffer intermedio para mantener el consumo de RAM constante independientemente del tamaño del archivo.
    """
    path_obj = Path(path) if isinstance(path, str) else path
    
    if not _is_valid_candidate(path_obj) or chunk_size <= 0:
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
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def partial_hash(path: Union[str, Path], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Genera una huella rápida leyendo el inicio del archivo.
    Es la primera barrera de filtrado para optimizar el tiempo de escaneo.
    """
    path_obj = Path(path) if isinstance(path, str) else path

    if not _is_valid_candidate(path_obj) or read_bytes <= 0:
        return None

    try:
        with open(path_obj, "rb") as f:
            content = f.read(read_bytes)
            if not content:
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError, TypeError, ValueError):
        return None


def _is_valid_candidate(path: Path) -> bool:
    """Valida requisitos básicos: debe ser archivo, no estar protegido y ser legible."""
    if not isinstance(path, Path):
        return False
    try:
        return (
            path.is_file() and 
            not is_protected_path(path) and 
            os.access(path, os.R_OK)
        )
    except (OSError, ValueError, TypeError):
        return False


def _get_entry_stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    """Obtiene stat de forma segura usando el objeto DirEntry para evitar llamadas extra a disco."""
    try:
        return entry.stat(follow_symlinks=False)
    except OSError:
        return None


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """Agrupa rutas por su tamaño en bytes para identificar potenciales candidatos."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    if paths is None or not isinstance(paths, Iterable): return groups
    
    for p in paths:
        path_obj = Path(p) if isinstance(p, (str, Path)) else None
        if path_obj and _is_valid_candidate(path_obj):
            try:
                size = path_obj.stat().st_size
                if size > 0:
                    groups[size].append(path_obj)
            except (OSError, PermissionError):
                continue
    return groups


def _resolve_and_verify_root(item: Union[str, Path]) -> Optional[Path]:
    """Normaliza y valida que una ruta sea un directorio raíz válido para escaneo."""
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
    Recorre el sistema de archivos buscando archivos.
    Usa `os.scandir` para minimizar llamadas a sistema (stat) por archivo.
    """
    size_map: Dict[int, List[Path]] = defaultdict(list)
    visited_inodes: Set[Tuple[int, int]] = set()

    def _scan_directory_recursive(current_dir: Path) -> None:
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            subdir_path = Path(entry.path)
                            if not is_protected_path(subdir_path) and not is_junction(subdir_path):
                                _scan_directory_recursive(subdir_path)
                        else:
                            st = _get_entry_stat(entry)
                            # Verificamos existencia y accesibilidad antes de considerar
                            p = Path(entry.path)
                            if st and st.st_size >= min_size and st.st_nlink == 1 and _is_valid_candidate(p):
                                size_map[st.st_size].append(p)
                                visited_inodes.add((st.st_dev, st.st_ino))
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            pass

    if directories and isinstance(directories, Iterable):
        roots = {Path(r).resolve() for item in directories if (r := _resolve_and_verify_root(item))}
        for root in roots:
            _scan_directory_recursive(root)
            
    return {size: files for size, files in size_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Agrupa una lista de archivos por el digest retornado por una función hash."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _refine_by_deep_hash(candidates: List[Path]) -> Dict[str, List[Path]]:
    """
    Filtra candidatos mediante una estrategia de dos capas:
    1. Hash parcial para descartar diferencias obvias.
    2. Hash SHA256 completo para validar identidad absoluta.
    """
    partial_results: Dict[str, List[Path]] = _group_paths_by_hash(candidates, partial_hash)
    final_groups: Dict[str, List[Path]] = {}
    
    for subset in partial_results.values():
        full_hash_groups = _group_paths_by_hash(subset, hash_file)
        final_groups.update(full_hash_groups)
        
    return final_groups


def _process_size_group(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """
    Selecciona la estrategia de refinamiento según el tamaño:
    Archivos pequeños (<64KB) se validan con hash parcial, otros requieren hash completo.
    """
    if not isinstance(size, int) or size <= 0 or not paths or len(paths) < 2: 
        return []
    
    if size <= PARTIAL_READ_BYTES:
        results = _group_paths_by_hash(paths, partial_hash)
    else:
        results = _refine_by_deep_hash(paths)
            
    return [DuplicateGroup(digest, size, sorted(confirmed_paths)) for digest, confirmed_paths in results.items()]


def find_duplicates(directories: Iterable[Union[str, Path]], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Punto de entrada principal para detectar archivos duplicados."""
    if not isinstance(directories, Iterable) or isinstance(directories, (str, Path)): 
        return []
    if not isinstance(min_size, int) or min_size < 0: 
        return []
        
    groups: List[DuplicateGroup] = []
    size_map = _collect_candidates(directories, min_size, skip_protected)
    for size, paths in size_map.items():
        groups.extend(_process_size_group(size, paths))
        
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Suma total de espacio que se liberaría si se eliminan todos los duplicados."""
    if not groups or not isinstance(groups, (list, tuple)): return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """
    Selecciona el archivo original a conservar. 
    Heurística: archivo modificado hace más tiempo (más antiguo) o el de menor longitud de ruta.
    """
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
        
    candidates: List[Tuple[float, int, Path]] = []
    for p in group.paths:
        if not isinstance(p, Path): continue
        try:
            if not p.is_file(): continue
            stat_info = p.stat()
            candidates.append((float(stat_info.st_mtime), len(str(p)), p))
        except (OSError, PermissionError):
            continue
    
    if not candidates:
        return None
        
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera strings legibles para la interfaz de usuario."""
    if not isinstance(group, DuplicateGroup) or group.paths is None:
        return []
        
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    
    for path in group.paths:
        if not isinstance(path, Path):
            continue
        if not path.exists():
            lines.append(f"   [desaparecido] {path}")
            continue
        elif not _is_valid_candidate(path):
            lines.append(f"   [inaccesible] {path}")
            continue
        
        label = 'conservar' if keeper is not None and path == keeper else 'duplicado'
        lines.append(f"   [{label}] {path}")
    return lines
