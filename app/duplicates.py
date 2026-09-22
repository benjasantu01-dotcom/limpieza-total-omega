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
    if not isinstance(path, Path) or not is_safe_to_modify(path):
        return False
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError, RuntimeError):
        return False


def is_system_or_hidden(path: Path) -> bool:
    """Valida atributos de sistema o visibilidad (oculto) en Windows."""
    if not is_safe_to_modify(path):
        return True
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
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
        """
        Calcula el espacio total recuperable.
        Se excluye una instancia del total (el 'keeper') asumiendo que 
        esa copia original se preservará.
        """
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """
    Verifica disponibilidad de archivo.
    Intenta abrir en modo solo lectura; si falla, el archivo está siendo usado
    exclusivamente por otro proceso, impidiendo lectura confiable del hash.
    """
    if not is_safe_to_modify(path):
        return True
    try:
        fd = os.open(path, os.O_RDONLY)
        os.close(fd)
        return False
    except (PermissionError, OSError):
        return True


def _validate_and_resolve_path(path: PathLike) -> Optional[Path]:
    """Normaliza, resuelve y valida una ruta de archivo para operaciones de lectura segura."""
    if not path:
        return None
    try:
        p: Path = Path(path).resolve(strict=True)
        if p.is_file() and is_safe_to_modify(p) and not _is_file_locked(p):
            return p
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo. 
    Usa buffering chunks para evitar saturar la memoria RAM con archivos grandes.
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
    except (OSError, PermissionError, IOError, MemoryError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Hash heurístico de prefijo.
    Se utiliza para descartar rápidamente archivos con encabezados distintos
    antes de realizar el costoso cómputo de un hash completo.
    """
    if read_bytes <= 0:
        return None

    p = _validate_and_resolve_path(path)
    if not p:
        return None

    try:
        with open(p, "rb") as f:
            content = f.read(read_bytes)
            if not content: 
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, IOError):
        return None


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Valida si un archivo es apto para análisis según reglas de seguridad y bloqueo."""
    try:
        if is_protected_path(path) or not is_safe_to_modify(path):
            return False
        if is_system_or_hidden(path):
            return False
        return st_size > 0 and not _is_file_locked(path)
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError):
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Crea un mapa de tamaño (bytes) a lista de rutas para archivos con el mismo peso."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if not p: continue
        try:
            path_obj = Path(p).resolve(strict=True)
            if is_safe_to_modify(path_obj):
                st = path_obj.stat()
                if _is_valid_candidate(path_obj, st.st_size):
                    groups[st.st_size].append(path_obj)
        except (OSError, RuntimeError, ValueError):
            continue
    return groups


def _resolve_and_verify_root(item: PathLike) -> Optional[Path]:
    """Valida que una ruta base sea un directorio accesible y no protegido por el sistema."""
    try:
        if not item: return None
        root = Path(item).resolve(strict=True)
        if root.is_dir() and not is_protected_path(root) and is_safe_to_modify(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """
    Recorre directorios recursivamente mediante os.scandir para optimizar el I/O.
    Filtra symlinks/junctions para evitar ciclos infinitos o lectura fuera de límites.
    """
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    visited_files: set[str] = set()

    def _scan_dir(current_dir: Path) -> None:
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        # Prevenir seguir estructuras que no son archivos planos
                        if entry.is_symlink() or is_junction(Path(entry.path)):
                            continue
                        if entry.is_dir():
                            _scan_dir(Path(entry.path))
                            continue
                        
                        p = Path(entry.path)
                        if (skip_protected and is_protected_path(p)) or not is_safe_to_modify(p):
                            continue
                        if is_system_or_hidden(p):
                            continue
                        
                        st = entry.stat(follow_symlinks=False)
                        if st.st_size < min_size or entry.path in visited_files:
                            continue
                            
                        if not _is_file_locked(p):
                            size_to_paths_map[st.st_size].append(p)
                            visited_files.add(entry.path)
                    except (FileNotFoundError, OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            return

    for item in directories:
        if (root := _resolve_and_verify_root(item)):
            _scan_dir(root)
            
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Clasifica rutas según la salida de hash_func, manteniendo solo grupos con colisiones."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _decide_hash_strategy_and_process(size: int, paths: List[Path]) -> List[DuplicateGroup]:
    """
    Motor de decisión de hashing.
    Si el archivo es pequeño, se hashea completo directamente.
    Si es grande, se aplica un hash de prefijo primero para descartar diferencias
    rápidamente, minimizando el tiempo de procesamiento total.
    """
    if not paths or size < 0:
        return []

    if size <= PARTIAL_READ_BYTES:
        final_groups = _group_paths_by_hash(paths, hash_file)
    else:
        # Paso 2: Prefiltrado con hash parcial
        partial_groups = _group_paths_by_hash(paths, partial_hash)
        final_groups = {}
        # Paso 3: Confirmación solo en grupos con potencial colisión parcial
        for candidate_subset in partial_groups.values():
            full_hash_groups = _group_paths_by_hash(candidate_subset, hash_file)
            final_groups.update(full_hash_groups)
            
    return [DuplicateGroup(d, size, sorted(p)) for d, p in final_groups.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Orquestador principal: gestiona el ciclo de vida de la detección de duplicados."""
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Calcula el espacio total que se liberaría si se eliminan los duplicados."""
    return sum(g.wasted_bytes for g in groups)


def _get_keeper_score(path: Path) -> Optional[Tuple[float, int]]:
    """
    Calcula score de preferencia. 
    Se prioriza fecha de modificación más antigua (estabilidad) y 
    longitud de ruta (menor profundidad/simplicidad).
    """
    try:
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, PermissionError, ValueError, AttributeError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Selecciona el archivo candidato para conservar basado en score de antigüedad/path."""
    if not group or not group.paths:
        return None
    
    candidates: List[Tuple[Tuple[float, int], Path]] = []
    for p in group.paths:
        try:
            if p.exists() and is_safe_to_modify(p):
                if score := _get_keeper_score(p):
                    candidates.append((score, p))
        except OSError:
            continue
            
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def format_group(group: DuplicateGroup) -> List[str]:
    """Serializa un grupo de duplicados en formato legible para la UI."""
    if not group or not group.paths:
        return ["Error: Grupo inválido o vacío"]
        
    keeper = suggest_keeper(group)
    mb_t, mb_w = round(group.size_bytes / 1048576, 2), round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_t} MB (recuperable: {mb_w} MB)"]
    
    for path in group.paths:
        try:
            if not is_safe_to_modify(path):
                lines.append(f"   [inaccesible] {path}")
            else:
                label = 'conservar' if (keeper is not None and path.resolve() == keeper.resolve()) else 'duplicado'
                lines.append(f"   [{label}] {path}")
        except (OSError, PermissionError):
            lines.append(f"   [error de acceso] {path}")
    return lines
