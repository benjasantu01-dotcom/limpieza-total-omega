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
from typing import Iterable, Sequence, Dict, List
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
    """Carpeta de caché detectada de un navegador."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Convierte bytes a MB con dos decimales de precisión."""
        return round(self.size_bytes / (1024 * 1024), 2)


def base_directories() -> List[Path]:
    """Carpetas base donde buscar perfiles de navegador (solo Windows)."""
    if os.name != "nt":
        return []
    local = os.environ.get("LOCALAPPDATA")
    if not local or not isinstance(local, str):
        return []
    
    path_local = Path(local)
    return [path_local] if path_local.is_dir() else []


def _is_safe_path(target_path: Path, base_path: Path) -> bool:
    """
    Valida la integridad de la ruta contra ataques de path traversal y
    asegura que no se acceda a directorios protegidos por política de seguridad.
    """
    try:
        resolved_target = target_path.resolve(strict=True)
        resolved_base = base_path.resolve(strict=True)
        
        if is_protected_path(resolved_target):
            return False
            
        return resolved_base in resolved_target.parents or resolved_target == resolved_base
    except (OSError, RuntimeError):
        return False


@lru_cache(maxsize=32)
def directory_size(path: str | os.PathLike) -> int:
    """
    Calcula el peso total de una carpeta mediante un recorrido seguro del sistema de archivos.
    """
    if not path:
        return 0
    
    try:
        p = Path(path)
        # No seguir enlaces simbólicos para evitar bucles o lecturas fuera de rango
        if p.is_symlink():
            return 0
        target = p.resolve(strict=True)
        if is_protected_path(target) or not target.is_dir():
            return 0
    except (OSError, RuntimeError):
        return 0
    
    total_bytes: int = 0
    stack: List[str] = [str(target)]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        # Verificar si es symlink o junction antes de procesar
                        if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if not is_protected_path(Path(entry.path)):
                                stack.append(entry.path)
                        else:
                            total_bytes += entry.stat().st_size
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
            
    return total_bytes


def _is_valid_cache_path(candidate: Path, base_path: Path) -> bool:
    """Valida que la ruta sea un directorio de caché objetivo y no contenga datos personales."""
    try:
        if not candidate.exists() or candidate.is_symlink():
            return False
        return (
            _is_safe_path(candidate, base_path) and
            candidate.is_dir() and
            candidate.name.lower() not in NEVER_TOUCH
        )
    except (ValueError, OSError, RuntimeError):
        return False


def detect_profiles(
    bases: Sequence[Path] | None = None, 
    cache_paths: Dict[str, str] | None = None
) -> List[BrowserCache]:
    """
    Explora los directorios base para identificar cachés de navegadores.
    """
    if bases is None:
        bases = base_directories()
    if cache_paths is None:
        cache_paths = BROWSER_CACHE_PATHS

    found: List[BrowserCache] = []
    
    for base in bases:
        if not isinstance(base, Path) or not base.is_dir() or is_protected_path(base):
            continue
            
        try:
            resolved_base = base.resolve(strict=True)
        except (OSError, RuntimeError):
            continue

        for browser_name, relative_path_str in cache_paths.items():
            try:
                candidate = resolved_base.joinpath(*relative_path_str.split("\\"))
                
                if not _is_valid_cache_path(candidate, resolved_base):
                    continue
                    
                found.append(BrowserCache(
                    browser=browser_name,
                    path=candidate,
                    size_bytes=directory_size(str(candidate)),
                ))
            except (OSError, PermissionError, ValueError, AttributeError, TypeError):
                continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Calcula la suma agregada de bytes en una colección de cachés."""
    if caches is None:
        return 0
    return sum(cache.size_bytes for cache in caches)


def summarize(caches: List[BrowserCache] | None = None) -> List[str]:
    """Genera una representación formateada del reporte para la interfaz de usuario."""
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
