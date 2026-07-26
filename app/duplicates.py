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
from typing import Iterable, Callable

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
        size_bytes: Tamaño del archivo en bytes.
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
        Calcula el espacio total que podría liberarse si se eliminaran 
        todas las copias duplicadas, preservando únicamente una.
        """
        if not self.paths or self.count <= 1:
            return 0
        return (self.count - 1) * max(0, self.size_bytes)


def hash_file(path: str | os.PathLike, chunk_size: int = 1024 * 1024) -> str | None:
    """
    Calcula el hash SHA256 completo de un archivo mediante lectura en bloques.
    Retorna None si el acceso es denegado o el archivo no es legible.
    """
    if not path or is_protected_path(path):
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                digest.update(chunk)
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError):
        return None
    return digest.hexdigest()


def partial_hash(path: str | os.PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> str | None:
    """
    Calcula un hash rápido leyendo solo el encabezado del archivo.
    Útil para descartar candidatos de forma eficiente antes de leer todo el disco.
    """
    if not path or is_protected_path(path):
        return None
    try:
        with open(path, "rb") as f:
            content = f.read(read_bytes)
            return hashlib.sha256(content).hexdigest()
    except (OSError, PermissionError, ValueError, TypeError, FileNotFoundError):
        return None


def group_by_size(paths: Iterable[Path]) -> dict[int, list[Path]]:
    """
    Organiza una lista de rutas de archivo en un diccionario usando
    estadísticas de archivo para evitar accesos redundantes al sistema.
    """
    if paths is None:
        return {}
    groups: dict[int, list[Path]] = defaultdict(list)
    for p in paths:
        try:
            stats = p.stat()
            if not is_protected_path(p):
                groups[stats.st_size].append(p)
        except (OSError, PermissionError, FileNotFoundError):
            continue
    return dict(groups)


def _collect_candidates(directories: Iterable[str | Path], min_size: int, skip_protected: bool) -> list[Path]:
    """
    Escaneo profundo de directorios para identificar archivos candidatos.
    
    Realiza una búsqueda recursiva excluyendo rutas protegidas y symlinks para
    garantizar que el conjunto de trabajo solo contenga archivos reales legibles.
    """
    if directories is None:
        return []
    candidates: list[Path] = []
    for directory in directories:
        if not directory:
            continue
        try:
            base = Path(directory).expanduser().resolve()
            if not base.is_dir():
                continue
            if skip_protected and is_protected_path(base):
                continue
            for root, subdirs, files in os.walk(base):
                root_path = Path(root).resolve()
                if not str(root_path).startswith(str(base)):
                    subdirs[:] = []
                    continue
                if skip_protected and is_protected_path(root_path):
                    subdirs[:] = []
                    continue
                for name in files:
                    candidate: Path = root_path / name
                    try:
                        if candidate.is_symlink():
                            continue
                        if skip_protected and is_protected_path(candidate):
                            continue
                        stats = candidate.stat()
                        if stats.st_size >= max(min_size, 1):
                            candidates.append(candidate)
                    except (OSError, PermissionError, FileNotFoundError):
                        continue
        except (OSError, RuntimeError, FileNotFoundError):
            continue
    return candidates


def _refine_by_hash(paths: Iterable[Path], hash_func: Callable[[str | Path], str | None]) -> dict[str, list[Path]]:
    """
    Aplica una función de hash a una lista de archivos y agrupa por resultado.
    Retorna solo aquellos grupos donde existan 2 o más coincidencias (posibles duplicados).
    """
    if paths is None:
        return {}
    by_hash: dict[str, list[Path]] = defaultdict(list)
    for path in paths:
        if digest := hash_func(path):
            by_hash[digest].append(path)
    return {h: p for h, p in by_hash.items() if len(p) > 1}


def find_duplicates(
    directories: Iterable[str | Path],
    min_size: int = 1024,
    skip_protected: bool = True,
) -> list[DuplicateGroup]:
    """
    Orquesta la estrategia de 3 pasos: 
    1. Agrupación por tamaño (lista simple).
    2. Refinamiento por hash parcial (reduce el conjunto de candidatos).
    3. Confirmación por hash completo (identificación definitiva).
    """
    if not directories:
        return []
        
    candidates = _collect_candidates(directories, min_size, skip_protected)
    if not candidates:
        return []

    groups: list[DuplicateGroup] = []
    
    # Paso 1: Filtrado por tamaño
    size_map = group_by_size(candidates)
    
    for size, same_size in size_map.items():
        if len(same_size) < 2:
            continue
            
        # Paso 2: Hash parcial para descartar archivos que difieren en el encabezado
        by_partial = _refine_by_hash(same_size, partial_hash)
        
        for partial_candidates in by_partial.values():
            
            # Paso 3: Hash completo para confirmación definitiva de igualdad
            by_full = _refine_by_hash(partial_candidates, hash_file)
            
            for digest, confirmed in by_full.items():
                if confirmed:
                    groups.append(DuplicateGroup(
                        digest=digest, 
                        size_bytes=size, 
                        paths=sorted(confirmed)
                    ))

    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups: list[DuplicateGroup]) -> int:
    """Calcula el espacio total que se liberaría sumando los bytes de todos los grupos."""
    if not isinstance(groups, list):
        return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: DuplicateGroup) -> Path | None:
    """
    Determina qué archivo es el 'original' para conservar. 
    Prioriza el archivo más antiguo y, en igualdad, el que tenga la ruta más corta.
    """
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None

    def sort_key(path: Path):
        try:
            mtime = path.stat().st_mtime
        except (OSError, PermissionError, FileNotFoundError):
            mtime = float("inf")
        return (mtime, len(str(path)))

    return min(group.paths, key=sort_key)


def format_group(group: DuplicateGroup) -> list[str]:
    """Formatea la información de un grupo de duplicados para su visualización en UI."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return []
    keeper = suggest_keeper(group)
    mb = round(group.size_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb} MB (recuperable: {round(group.wasted_bytes / (1024 * 1024), 2)} MB)"]
    for path in group.paths:
        marca = "conservar" if path == keeper else "duplicado"
        lines.append(f"   [{marca}] {path}")
    return lines
