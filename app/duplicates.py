"""
duplicates.py — detección de archivos duplicados.

SOLO LECTURA: este módulo encuentra y agrupa duplicados, y sugiere cuál
conservar, pero **nunca borra ni mueve nada**. La decisión de qué hacer con
un duplicado queda en la interfaz, y pasa por la cuarentena o por la
carpeta de revisión, nunca por un borrado directo.

Estrategia en tres pasos, de lo barato a lo caro, para no leer gigas al
vacío:
  1. Agrupar por tamaño exacto (dos archivos de distinto tamaño no pueden
     ser iguales; esto descarta la enorme mayoría sin leer nada).
  2. Dentro de cada grupo, hash de los primeros KB (descarta casi todo el
     resto leyendo muy poco).
  3. Solo a los que siguen coincidiendo, hash completo para confirmar.
"""

from __future__ import annotations
import hashlib
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Callable, Dict, List, Optional, Union

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
PARTIAL_READ_BYTES = 64 * 1024


@dataclass
class DuplicateGroup:
    """
    Representa una colección de archivos que comparten contenido idéntico.
    
    Attributes:
        digest: Hash SHA256 calculado sobre el contenido total del archivo.
        size_bytes: Tamaño de cada archivo en el grupo en bytes.
        paths: Lista de objetos Path que contienen los archivos duplicados.
    """
    digest: str
    size_bytes: int
    paths: list[Path]

    @property
    def count(self) -> int:
        """Número de copias encontradas del archivo."""
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """
        Calcula el espacio total que podría liberarse si se conservara
        solo una copia del archivo (n-1 copias).
        """
        if not self.paths or self.count <= 1:
            return 0
        return (self.count - 1) * max(0, self.size_bytes)


def hash_file(path: Union[str, os.PathLike], chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calcula el hash SHA256 completo de un archivo mediante lectura en bloques.
    
    Args:
        path: Ruta del archivo a procesar.
        chunk_size: Tamaño del buffer (default 1MB).
        
    Returns:
        Hash SHA256 (hex) si es accesible, None si es protegido o inaccesible.
    """
    if not path or is_protected_path(path):
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                digest.update(chunk)
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError, IsADirectoryError):
        return None
    return digest.hexdigest()


def partial_hash(path: Union[str, os.PathLike], read_bytes: int = PARTIAL_READ_BYTES) -> Optional[str]:
    """
    Calcula un hash rápido de los primeros N bytes de un archivo.
    Actúa como filtro de alto rendimiento para descartar archivos distintos rápidamente.
    """
    if not path or is_protected_path(path):
        return None
    try:
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            content = f.read(read_bytes)
            if not content:
                return None
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError, IsADirectoryError):
        return None


def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    """
    Agrupa rutas de archivos basándose en su tamaño (st_size).
    Retorna un diccionario donde la llave es el tamaño en bytes y el valor 
    es una lista de rutas que comparten dicho tamaño.
    """
    if paths is None:
        return {}
    groups: Dict[int, List[Path]] = defaultdict(list)
    for p in paths:
        if not isinstance(p, Path):
            continue
        try:
            # Usamos lstat para evitar resolución innecesaria de enlaces durante el conteo
            stat = p.lstat()
            if stat.st_size > 0:
                groups[stat.st_size].append(p)
        except (OSError, PermissionError, FileNotFoundError, AttributeError):
            continue
    return groups


def _collect_candidates(directories: Iterable[Union[str, Path]], min_size: int, skip_protected: bool) -> List[Path]:
    """
    Realiza un recorrido recursivo en el sistema de archivos para recolectar candidatos.
    Verifica seguridad de rutas antes de cada acceso para evitar seguir junctions o enlaces maliciosos.
    """
    if directories is None:
        return []
    candidates: List[Path] = []
    
    for directory in directories:
        if not directory:
            continue
        try:
            base = Path(directory).expanduser()
            if not base.is_dir() or (skip_protected and is_protected_path(base)):
                continue
            
            for root, subdirs, files in os.walk(base):
                root_path = Path(root)
                
                # Pre-filtrar subdirectorios protegidos evitando que os.walk entre en ellos
                if skip_protected:
                    subdirs[:] = [d for d in subdirs if not is_protected_path(root_path / d)]
                    
                for name in files:
                    candidate = root_path / name
                    try:
                        # Verificar seguridad y que no sea un enlace simbólico (lstat)
                        if os.path.islink(candidate):
                            continue
                            
                        st = candidate.lstat()
                        if st.st_size >= min_size and os.path.isfile(candidate):
                            if not skip_protected or not is_protected_path(candidate):
                                candidates.append(candidate)
                    except (OSError, PermissionError, FileNotFoundError):
                        continue
        except (OSError, RuntimeError, FileNotFoundError):
            continue
    return candidates


def _refine_by_hash(paths: Iterable[Path], hash_func: Callable[[Path], Optional[str]]) -> Dict[str, List[Path]]:
    """
    Aplica una función de hash a un iterable de archivos y agrupa las coincidencias.
    Retorna solo grupos con tamaño > 1 para descartar archivos únicos en esta etapa.
    """
    if paths is None:
        return {}
    by_hash: Dict[str, List[Path]] = defaultdict(list)
    for path in paths:
        if digest := hash_func(path):
            by_hash[digest].append(path)
    return {h: p for h, p in by_hash.items() if len(p) > 1}


def find_duplicates(
    directories: Iterable[Union[str, Path]],
    min_size: int = 1024,
    skip_protected: bool = True,
) -> List[DuplicateGroup]:
    """
    Ejecuta el pipeline de detección de duplicados en tres etapas:
    1. Recolección y filtrado por tamaño mínimo.
    2. Filtrado por hash parcial (fase rápida).
    3. Filtrado por hash completo (fase de confirmación final).
    """
    if not directories:
        return []
        
    candidates = _collect_candidates(directories, min_size, skip_protected)
    if not candidates:
        return []

    groups: List[DuplicateGroup] = []
    size_map = {s: p for s, p in group_by_size(candidates).items() if len(p) > 1}
    
    for size, same_size in size_map.items():
        by_partial = _refine_by_hash(same_size, partial_hash)
        
        for partial_candidates in by_partial.values():
            by_full = _refine_by_hash(partial_candidates, hash_file)
            
            for digest, confirmed in by_full.items():
                groups.append(DuplicateGroup(
                    digest=digest, 
                    size_bytes=size, 
                    paths=sorted(confirmed)
                ))

    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: List[DuplicateGroup]) -> int:
    """Suma el espacio recuperable de todos los grupos identificados."""
    if not isinstance(groups, list):
        return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: DuplicateGroup) -> Optional[Path]:
    """
    Heurística para sugerir el archivo a conservar: el más antiguo.
    En caso de empate en fecha (mtime), se favorece el que tenga una ruta más corta.
    """
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None

    valid_paths: List[tuple[float, int, Path]] = []
    for p in group.paths:
        try:
            if p.exists():
                mtime = p.stat().st_mtime
                valid_paths.append((mtime, len(str(p)), p))
        except (OSError, PermissionError, FileNotFoundError):
            continue
            
    if not valid_paths:
        return group.paths[0] if group.paths else None

    # Ordenar por mtime (ascendente: el más antiguo primero), luego por longitud de ruta
    best = min(valid_paths, key=lambda x: (x[0], x[1]))
    return best[2]


def format_group(group: DuplicateGroup) -> List[str]:
    """Prepara una representación en texto del grupo para la interfaz."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return []
    keeper = suggest_keeper(group)
    mb_total = round(group.size_bytes / (1024 * 1024), 2)
    mb_wasted = round(group.wasted_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb_total} MB (recuperable: {mb_wasted} MB)"]
    for path in group.paths:
        marca = "conservar" if path == keeper else "duplicado"
        lines.append(f"   [{marca}] {path}")
    return lines
