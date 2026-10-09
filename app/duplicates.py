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
import io
import ctypes
from collections import defaultdict, deque
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
MAX_RECURSION_DEPTH: int = 100
MAX_PATH_LIMIT: int = 260


def is_junction(path: Path) -> bool:
    """
    Verifica mediante la API de Windows si una ruta es un punto de reanálisis.
    
    Usa GetFileAttributesW para identificar 'Reparse Points'. Se ignora el 
    seguimiento de estas rutas para evitar ciclos infinitos y el escaneo
    innecesario de unidades montadas.
    """
    if not isinstance(path, Path) or not is_safe_to_modify(path):
        return False
    try:
        attrs: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError, RuntimeError):
        return False


def is_system_or_hidden(path: Path) -> bool:
    """
    Determina si un archivo tiene atributos de sistema u oculto en Windows.
    
    El escaneo debe evitar estos archivos para no comprometer la estabilidad
    del sistema operativo ni alterar archivos de configuración críticos.
    """
    if not isinstance(path, Path) or not is_safe_to_modify(path):
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
    """
    Contenedor de datos para grupos de archivos idénticos.
    
    Almacena el hash SHA256 (digest), el tamaño compartido en bytes y 
    la lista de rutas que componen el grupo.
    """
    digest: str
    size_bytes: int
    paths: List[Path]

    @property
    def count(self) -> int:
        """Retorna el número de archivos encontrados en este grupo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """
        Calcula el espacio total que se liberaría si se conservara solo uno.
        
        Formula: (N - 1) * tamaño_archivo. Solo contabiliza el exceso.
        """
        if not self.paths or self.count <= 1 or self.size_bytes < 0:
            return 0
        return (self.count - 1) * self.size_bytes


def _is_file_locked(path: Path) -> bool:
    """
    Verifica si un archivo está en uso mediante un intento de lectura.
    
    Intenta un 'peek' o apertura de lectura. Si el acceso es denegado o
    el archivo está bloqueado por el sistema, retorna True para evitar
    errores de I/O durante la fase de hashing.
    """
    if not isinstance(path, Path) or not is_safe_to_modify(path):
        return True
    try:
        with open(path, "rb") as f:
            f.peek(1)
            return False
    except (PermissionError, OSError, ValueError, io.UnsupportedOperation):
        return True


def _safe_path_check(path: Path) -> bool:
    """
    Validación holística de seguridad para una ruta dada.
    
    Verifica que la ruta no sea protegida por el sistema, no sea una unión/enlace
    y cumpla con los filtros definidos en safety.py.
    """
    if not isinstance(path, Path):
        return False
    try:
        return (is_safe_to_modify(path) and 
                not is_protected_path(path) and 
                not is_junction(path) and 
                not path.is_symlink())
    except (OSError, RuntimeError):
        return False


def _validate_and_resolve_path(path_input: PathLike) -> Optional[Path]:
    """
    Normaliza una ruta y valida su integridad para procesamiento posterior.
    
    Realiza una resolución de ruta absoluta y chequea que el archivo exista,
    sea accesible, no esté bloqueado y tenga un tamaño mayor a 0 bytes.
    """
    if not path_input:
        return None
    try:
        p: Path = Path(path_input).resolve()
        if p.is_file() and _safe_path_check(p) and not _is_file_locked(p):
            if p.stat().st_size > 0:
                return p
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo del archivo.
    
    Utiliza buffers de 1MB para leer archivos grandes de manera eficiente
    sin agotar la memoria disponible (Memory-safe).
    """
    p: Optional[Path] = _validate_and_resolve_path(path)
    if p is None:
        return None
            
    try:
        digest = hashlib.sha256()
        with open(p, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PermissionError, MemoryError, ValueError):
        return None


def partial_hash(path: PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Genera un hash SHA256 de los primeros N bytes del archivo.
    
    Esta técnica optimiza la identificación de duplicados descartando archivos 
    que difieren desde el inicio sin necesidad de leer el archivo completo.
    """
    p: Optional[Path] = _validate_and_resolve_path(path)
    if p is None:
        return None

    try:
        with open(p, "rb") as f:
            content = f.read(read_bytes)
            if not content: 
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, ValueError):
        return None


def _is_valid_candidate(path: Path, st_size: int) -> bool:
    """Filtro de pre-selección para asegurar que un archivo es candidato a duplicado."""
    if st_size <= 0:
        return False
    try:
        return (_safe_path_check(path) and 
                not is_system_or_hidden(path) and 
                not _is_file_locked(path))
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError):
        return False


def group_by_size(paths: Iterable[PathLike]) -> Dict[int, List[Path]]:
    """Agrupa una lista de rutas según el tamaño del archivo."""
    groups: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if not p: continue
        try:
            path_obj = Path(p).resolve()
            if path_obj.is_file():
                st_size = path_obj.stat().st_size
                if _is_valid_candidate(path_obj, st_size):
                    groups[st_size].append(path_obj)
        except (OSError, ValueError, TypeError):
            continue
    return groups


def _resolve_and_verify_root(directory_path: PathLike) -> Optional[Path]:
    """Verifica que una ruta sea un directorio válido y seguro para ser analizado."""
    if not directory_path: return None
    try:
        root = Path(directory_path).resolve()
        if root.is_dir() and _safe_path_check(root):
            return root
    except (OSError, ValueError, RuntimeError, TypeError):
        return None
    return None


def _collect_candidates(directories: Iterable[PathLike], min_size: int, skip_protected: bool) -> Dict[int, List[Path]]:
    """
    Recorre jerárquicamente las rutas usando BFS (Breadth-First Search).
    
    Implementa control de ciclos mediante 'visited_inodes' (dev, ino) para evitar
    el procesamiento redundante de duplicados en el árbol de archivos.
    """
    size_to_paths_map: Dict[int, List[Path]] = defaultdict(list)
    queue: deque[Tuple[Path, int]] = deque()
    visited_dirs: set[Path] = set()
    visited_inodes: set[Tuple[int, int]] = set()

    for d in directories:
        if (r := _resolve_and_verify_root(d)):
            queue.append((r, 0))
    
    while queue:
        current_dir, depth = queue.popleft()
        if current_dir in visited_dirs or depth > MAX_RECURSION_DEPTH:
            continue
        visited_dirs.add(current_dir)
            
        try:
            with os.scandir(current_dir) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            p_entry = Path(entry.path)
                            if _safe_path_check(p_entry) and not (skip_protected and is_protected_path(p_entry)):
                                queue.append((p_entry, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat()
                            if st.st_size >= min_size and (st.st_dev, st.st_ino) not in visited_inodes:
                                p_entry = Path(entry.path)
                                if _is_valid_candidate(p_entry, st.st_size):
                                    visited_inodes.add((st.st_dev, st.st_ino))
                                    size_to_paths_map[st.st_size].append(p_entry)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
            
    return {sz: files for sz, files in size_to_paths_map.items() if len(files) > 1}


def _group_paths_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """Helper genérico para agrupar una lista de rutas según una función de hash."""
    groups_by_digest: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if is_safe_to_modify(path) and (digest := hash_func(path)):
            groups_by_digest[digest].append(path)
    return {d: p for d, p in groups_by_digest.items() if len(p) > 1}


def _process_large_file_subset(paths: List[Path]) -> Dict[str, List[Path]]:
    """
    Ejecuta un proceso de filtrado de dos niveles: 
    primero reduce con hash parcial, luego confirma con hash completo.
    """
    partial_groups = _group_paths_by_hash(paths, partial_hash)
    final_results: Dict[str, List[Path]] = {}
    for candidate_subset in partial_groups.values():
        final_results.update(_group_paths_by_hash(candidate_subset, hash_file))
    return final_results


def _decide_hash_strategy_and_process(size_bytes: int, file_paths: List[Path]) -> List[DuplicateGroup]:
    """Selecciona la estrategia de hashing basada en el tamaño del archivo."""
    if not file_paths or size_bytes <= 0:
        return []

    if size_bytes <= PARTIAL_READ_BYTES:
        final_groups = _group_paths_by_hash(file_paths, hash_file)
    else:
        final_groups = _process_large_file_subset(file_paths)
    return [DuplicateGroup(d, size_bytes, sorted(p)) for d, p in final_groups.items()]


def find_duplicates(directories: Iterable[PathLike], min_size: int = 1024, skip_protected: bool = True) -> List[DuplicateGroup]:
    """Función principal: orquesta la detección y retorno de grupos de duplicados."""
    size_map = _collect_candidates(directories, min_size, skip_protected)
    groups: List[DuplicateGroup] = []
    for size, paths in size_map.items():
        groups.extend(_decide_hash_strategy_and_process(size, paths))
    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: Sequence[DuplicateGroup]) -> int:
    """Retorna el total de bytes que se podrían recuperar."""
    return sum(g.wasted_bytes for g in groups)


def _calculate_keeper_heuristic(path: Path) -> Optional[Tuple[float, int]]:
    """
    Calcula una métrica de selección para determinar el archivo 'original'.
    Se basa en: (fecha_modificación, longitud_ruta).
    """
    if not isinstance(path, Path):
        return None
    try:
        if not path.is_file():
            return None
        stat = path.stat()
        return float(stat.st_mtime), len(str(path))
    except (OSError, PermissionError, ValueError, AttributeError):
        return None


def suggest_keeper(group: Optional[DuplicateGroup]) -> Optional[Path]:
    """Sugiere el archivo a conservar basándose en antigüedad y estructura de ruta."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None
    
    candidates: List[Tuple[Tuple[float, int], Path]] = []
    for p in group.paths:
        if isinstance(p, Path):
            if (score := _calculate_keeper_heuristic(p)) is not None:
                candidates.append((score, p))
            
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def _get_path_label(path: Path, keeper: Optional[Path]) -> str:
    """Devuelve un string legible para UI representando el estado de una ruta en un grupo."""
    if not isinstance(path, Path) or not path.is_file():
        return "[desaparecido]"
    if not _safe_path_check(path):
        return "[inaccesible]"
    if keeper is not None and path == keeper:
        return "[conservar]"
    return "[duplicado]"


def format_group(group: DuplicateGroup) -> List[str]:
    """Genera una lista de strings descriptivos para informes visuales."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return ["Error: Grupo inválido o vacío"]
        
    keeper = suggest_keeper(group)
    mb_t, mb_w = round(group.size_bytes / 1048576, 2), round(group.wasted_bytes / 1048576, 2)
    lines = [f"{group.count} copias de {mb_t} MB (recuperable: {mb_w} MB)"]
    
    for path in group.paths:
        if not isinstance(path, Path):
            continue
        label = _get_path_label(path, keeper)
        lines.append(f"   {label} {path}")
    return lines
