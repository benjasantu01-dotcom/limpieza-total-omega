"""
browser.py — detección de cachés de navegadores.

SOLO LECTURA: detecta qué navegadores hay instalados, dónde guardan su
caché y cuánto ocupa. **No borra nada.** Limpiar caché de navegador es
seguro en general, pero borrar la carpeta equivocada del perfil puede
hacerte perder sesiones, contraseñas guardadas o marcadores, así que acá
solo se reportan las carpetas y su tamaño; la limpieza pasa por la carpeta
de revisión de `organizer.py`, con confirmación del usuario.

A propósito se listan solo carpetas de CACHÉ (datos regenerables) y nunca
las de credenciales o marcadores, ni siquiera para reportar su tamaño.

Diseño testeable: `detect_profiles` recibe la carpeta base por parámetro,
así en CI se puede simular una instalación con carpetas temporales.
"""

from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, Dict, List, Optional
from functools import lru_cache
from safety import is_protected_path

__all__ = [
    "BrowserCache",
    "BROWSER_CACHE_PATHS",
    "NEVER_TOUCH",
    "base_directories",
    "detect_profiles",
    "directory_size",
    "total_cache_bytes",
    "summarize",
    "SAFETY_NOTE",
]

# Rutas de caché relativas a LOCALAPPDATA (Windows). Solo datos
# regenerables: si se borran, el navegador los vuelve a crear.
BROWSER_CACHE_PATHS: Dict[str, str] = {
    "Google Chrome": r"Google\Chrome\User Data\Default\Cache",
    "Microsoft Edge": r"Microsoft\Edge\User Data\Default\Cache",
    "Brave": r"BraveSoftware\Brave-Browser\User Data\Default\Cache",
    "Opera": r"Opera Software\Opera Stable\Cache",
    "Vivaldi": r"Vivaldi\User Data\Default\Cache",
    "Chrome (código)": r"Google\Chrome\User Data\Default\Code Cache",
    "Edge (código)": r"Microsoft\Edge\User Data\Default\Code Cache",
    "Chrome (GPU)": r"Google\Chrome\User Data\Default\GPUCache",
}

# Nombres que este módulo nunca reporta ni toca, aunque estén dentro del
# perfil: son datos del usuario, no caché.
NEVER_TOUCH: frozenset[str] = frozenset({
    "login data", "cookies", "web data", "bookmarks", "history",
    "preferences", "local state", "extensions", "profile",
})

SAFETY_NOTE: str = (
    "Solo se listan carpetas de caché, que el navegador regenera solo. "
    "Nunca se tocan contraseñas, cookies, marcadores ni historial. "
    "Cerrá el navegador antes de limpiar su caché, o los archivos en uso "
    "no se van a poder mover."
)


@dataclass
class BrowserCache:
    """Representación de una carpeta de caché detectada."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño en MB. Precisión de 2 decimales para reportes."""
        return round(self.size_bytes / (1024 * 1024), 2)


def base_directories() -> List[Path]:
    """Obtiene directorios base (LOCALAPPDATA) para búsqueda de perfiles (Windows)."""
    if os.name != "nt":
        return []
    local = os.environ.get("LOCALAPPDATA")
    if not local or not isinstance(local, str):
        return []
    
    path_local = Path(local)
    return [path_local] if path_local.is_dir() else []


def _is_safe_path(target_path: Optional[Path], base_path: Optional[Path]) -> bool:
    """
    Verifica que la ruta sea un descendiente legítimo de base_path.
    """
    if not target_path or not base_path or not isinstance(target_path, Path):
        return False
    try:
        if is_protected_path(target_path):
            return False
        # Comparación absoluta para evitar bypass por rutas relativas
        resolved_target = target_path.resolve()
        resolved_base = base_path.resolve()
        return resolved_base in resolved_target.parents or resolved_target == resolved_base
    except (OSError, RuntimeError):
        return False


@lru_cache(maxsize=32)
def directory_size(path: str | os.PathLike) -> int:
    """
    Calcula el tamaño total en bytes de un directorio mediante suma recursiva,
    validando que cada subdirectorio esté contenido en el root original.
    """
    root_path = Path(path).resolve()
    
    if not root_path.is_dir() or is_protected_path(root_path):
        return 0
    
    total_bytes: int = 0
    stack: List[Path] = [root_path]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir():
                        entry_path = Path(entry.path).resolve()
                        if root_path in entry_path.parents and not is_protected_path(entry_path):
                            stack.append(entry_path)
                    elif entry.is_file():
                        try:
                            total_bytes += entry.stat().st_size
                        except (OSError, PermissionError):
                            continue
        except (OSError, PermissionError):
            continue
    return total_bytes


def _is_valid_cache_path(candidate: Path, base_path: Path) -> bool:
    """Filtro estricto: valida existencia, seguridad y exclusión de sensibles."""
    try:
        return (
            isinstance(candidate, Path) and
            candidate.exists() and 
            candidate.is_dir() and 
            not candidate.is_symlink() and
            _is_safe_path(candidate, base_path) and
            candidate.name.lower() not in NEVER_TOUCH
        )
    except (ValueError, OSError, RuntimeError, TypeError):
        return False


def detect_profiles(
    bases: Sequence[Path] | None = None, 
    cache_paths: Dict[str, str] | None = None
) -> List[BrowserCache]:
    """Explora directorios base en busca de cachés definidas."""
    bases = bases or base_directories()
    cache_paths = cache_paths or BROWSER_CACHE_PATHS

    found: List[BrowserCache] = []
    for base in bases:
        if not isinstance(base, Path) or not base.is_dir():
            continue
            
        for browser_name, relative_path_str in cache_paths.items():
            candidate = base.joinpath(*relative_path_str.split("\\"))
            if _is_valid_cache_path(candidate, base):
                found.append(BrowserCache(
                    browser=browser_name,
                    path=candidate,
                    size_bytes=directory_size(str(candidate)),
                ))
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Calcula el total de bytes de una lista de objetos BrowserCache."""
    return sum(cache.size_bytes for cache in (caches or []))


def summarize(caches: List[BrowserCache] | None = None) -> List[str]:
    """Formatea el reporte de caché para visualización en la interfaz (UI)."""
    current_caches = caches if caches is not None else detect_profiles()
    
    if not current_caches:
        return ["No se detectaron cachés de navegador en este sistema."]
        
    total_mb = round(total_cache_bytes(current_caches) / (1024 * 1024), 2)
    lines = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
