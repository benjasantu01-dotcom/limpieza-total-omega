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
from typing import Iterable

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
    """Conjunto de archivos con contenido idéntico."""
    digest: str
    size_bytes: int
    paths: list[Path]

    @property
    def count(self) -> int:
        return len(self.paths) if self.paths else 0

    @property
    def wasted_bytes(self) -> int:
        """Espacio recuperable: todas las copias menos una."""
        if not self.paths or self.count <= 1:
            return 0
        return (self.count - 1) * max(0, self.size_bytes)


def hash_file(path: str | os.PathLike, chunk_size: int = 1024 * 1024) -> str | None:
    """Hash completo del archivo (sha256). None si no se pudo leer."""
    if not path or is_protected_path(path):
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                digest.update(chunk)
    except (OSError, PermissionError, ValueError):
        return None
    return digest.hexdigest()


def partial_hash(path: str | os.PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> str | None:
    """Hash de los primeros bytes del archivo. None si no se pudo leer."""
    if not path or is_protected_path(path):
        return None
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read(read_bytes)).hexdigest()
    except (OSError, PermissionError, ValueError):
        return None


def group_by_size(paths: list[str | Path]) -> dict[int, list[Path]]:
    """Agrupa rutas por tamaño exacto, descartando las inaccesibles."""
    if not paths:
        return {}
    groups: dict[int, list[Path]] = defaultdict(list)
    for raw in paths:
        if not raw:
            continue
        try:
            p = Path(raw)
            if not p.is_file() or is_protected_path(p):
                continue
            groups[p.stat().st_size].append(p)
        except (OSError, PermissionError, TypeError):
            continue
    return dict(groups)


def _collect_candidates(directories: Iterable[str | Path], min_size: int, skip_protected: bool) -> list[Path]:
    """Recorre las carpetas y junta archivos candidatos a comparar."""
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
                    candidate = root_path / name
                    try:
                        if candidate.is_symlink() or (skip_protected and is_protected_path(candidate)):
                            continue
                        stats = candidate.stat()
                        if stats.st_size >= max(min_size, 1):
                            candidates.append(candidate)
                    except (OSError, PermissionError):
                        continue
        except (OSError, RuntimeError):
            continue
    return candidates


def _refine_by_hash(paths: Iterable[Path], hash_func: callable) -> dict[str, list[Path]]:
    """Aplica una función de hash a una lista de archivos para agrupar por contenido."""
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
    """Busca duplicados en las carpetas indicadas aplicando la estrategia de 3 pasos."""
    if not directories:
        return []
        
    candidates = _collect_candidates(directories, min_size, skip_protected)
    if not candidates:
        return []

    groups: list[DuplicateGroup] = []
    
    # Paso 1: Filtrado por tamaño
    size_map = group_by_size(candidates)
    
    for size, same_size in size_map.items():
        if not same_size or len(same_size) < 2:
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
    """Espacio total que se liberaría dejando una copia de cada grupo."""
    if not groups or not isinstance(groups, list):
        return 0
    return sum(g.wasted_bytes for g in groups if isinstance(g, DuplicateGroup))


def suggest_keeper(group: DuplicateGroup) -> Path | None:
    """Sugiere qué copia conservar: la más antigua y de ruta más corta."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return None

    def sort_key(path: Path):
        try:
            mtime = path.stat().st_mtime
        except (OSError, PermissionError):
            mtime = float("inf")
        return (mtime, len(str(path)))

    return min(group.paths, key=sort_key)


def format_group(group: DuplicateGroup) -> list[str]:
    """Formatea un grupo para mostrarlo, marcando la copia sugerida."""
    if not isinstance(group, DuplicateGroup) or not group.paths:
        return []
    keeper = suggest_keeper(group)
    mb = round(group.size_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb} MB (recuperable: {round(group.wasted_bytes / (1024 * 1024), 2)} MB)"]
    for path in group.paths:
        marca = "conservar" if path == keeper else "duplicado"
        lines.append(f"   [{marca}] {path}")
    return lines
