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
        return len(self.paths)

    @property
    def wasted_bytes(self) -> int:
        """Espacio recuperable: todas las copias menos una."""
        return max(0, self.count - 1) * self.size_bytes


def hash_file(path: str | os.PathLike, chunk_size: int = 1024 * 1024) -> str | None:
    """Hash completo del archivo (sha256). None si no se pudo leer.

    Se lee por trozos para no cargar en memoria un archivo grande, y se
    devuelve None en lugar de propagar el error para que un solo archivo
    inaccesible no aborte todo el escaneo.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                digest.update(chunk)
    except (OSError, PermissionError):
        return None
    return digest.hexdigest()


def partial_hash(path: str | os.PathLike, read_bytes: int = PARTIAL_READ_BYTES) -> str | None:
    """Hash de los primeros bytes del archivo. None si no se pudo leer."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read(read_bytes)).hexdigest()
    except (OSError, PermissionError):
        return None


def group_by_size(paths) -> dict[int, list[Path]]:
    """Agrupa rutas por tamaño exacto, descartando las inaccesibles."""
    groups: dict[int, list[Path]] = defaultdict(list)
    for raw in paths:
        p = Path(raw)
        try:
            if not p.is_file():
                continue
            groups[p.stat().st_size].append(p)
        except (OSError, PermissionError):
            continue
    return dict(groups)


def _collect_candidates(directories, min_size: int, skip_protected: bool) -> list[Path]:
    """Recorre las carpetas y junta archivos candidatos a comparar."""
    candidates: list[Path] = []
    for directory in directories:
        base = Path(directory).expanduser()
        if not base.is_dir():
            continue
        if skip_protected and is_protected_path(base):
            continue
        for root, subdirs, files in os.walk(base):
            if skip_protected and is_protected_path(root):
                subdirs[:] = []
                continue
            for name in files:
                candidate = Path(root) / name
                try:
                    if candidate.is_symlink():
                        continue  # no seguir enlaces: evita ciclos y falsos duplicados
                    if candidate.stat().st_size >= min_size:
                        candidates.append(candidate)
                except (OSError, PermissionError):
                    continue
    return candidates


def find_duplicates(
    directories,
    min_size: int = 1024,
    skip_protected: bool = True,
) -> list[DuplicateGroup]:
    """Busca duplicados en las carpetas indicadas. No modifica nada.

    `min_size` evita reportar cientos de archivos vacíos o diminutos, que
    son duplicados técnicamente pero no aportan espacio recuperable.
    `skip_protected` mantiene el escaneo fuera de carpetas de sistema.
    """
    candidates = _collect_candidates(directories, min_size, skip_protected)

    groups: list[DuplicateGroup] = []
    for size, same_size in group_by_size(candidates).items():
        if len(same_size) < 2:
            continue

        # Paso 2: descarte barato por hash parcial.
        by_partial: dict[str, list[Path]] = defaultdict(list)
        for path in same_size:
            digest = partial_hash(path)
            if digest is not None:
                by_partial[digest].append(path)

        # Paso 3: confirmación con hash completo.
        for partial_candidates in by_partial.values():
            if len(partial_candidates) < 2:
                continue
            by_full: dict[str, list[Path]] = defaultdict(list)
            for path in partial_candidates:
                digest = hash_file(path)
                if digest is not None:
                    by_full[digest].append(path)
            for digest, duplicates in by_full.items():
                if len(duplicates) > 1:
                    groups.append(DuplicateGroup(digest=digest, size_bytes=size, paths=sorted(duplicates)))

    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups


def reclaimable_bytes(groups) -> int:
    """Espacio total que se liberaría dejando una copia de cada grupo."""
    return sum(group.wasted_bytes for group in groups)


def suggest_keeper(group: DuplicateGroup) -> Path | None:
    """Sugiere qué copia conservar: la más antigua y de ruta más corta.

    La más antigua suele ser el original, y a igual antigüedad se prefiere
    la ruta más corta porque tiende a ser la ubicación "principal" en vez
    de una copia enterrada en subcarpetas.
    """
    if not group.paths:
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
    keeper = suggest_keeper(group)
    mb = round(group.size_bytes / (1024 * 1024), 2)
    lines = [f"{group.count} copias de {mb} MB (recuperable: {round(group.wasted_bytes / (1024 * 1024), 2)} MB)"]
    for path in group.paths:
        marca = "conservar" if path == keeper else "duplicado"
        lines.append(f"   [{marca}] {path}")
    return lines
