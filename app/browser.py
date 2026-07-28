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
NEVER_TOUCH = frozenset({
    "login data", "cookies", "web data", "bookmarks", "history",
    "preferences", "local state", "extensions", "profile",
})

SAFETY_NOTE = (
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
    # Validación estricta: debe ser una cadena no vacía y una ruta existente
    if not local or not isinstance(local, str):
        return []
    
    path_local = Path(local)
    return [path_local] if path_local.is_dir() else []


def _is_safe_path(p: Path, base: Path) -> bool:
    """Verifica que p sea un subdirectorio real bajo base, evitando escapes."""
    try:
        resolved_p = p.resolve(strict=True)
        resolved_base = base.resolve(strict=True)
        # Seguridad adicional: verificar protección global antes de procesar
        if is_protected_path(resolved_p):
            return False
        return resolved_base in resolved_p.parents or resolved_p == resolved_base
    except (OSError, RuntimeError):
        return False


@lru_cache(maxsize=32)
def directory_size(path: str | os.PathLike) -> int:
    """
    Calcula el tamaño total de una carpeta de forma recursiva (mediante stack).

    Args:
        path: Ruta al directorio a medir.
        
    Returns:
        Tamaño total en bytes. Retorna 0 en caso de error o ruta protegida.
    """
    if not path or not isinstance(path, (str, Path)):
        return 0
    
    try:
        p = Path(path).resolve(strict=True)
        if is_protected_path(p) or not p.is_dir():
            return 0
    except (OSError, RuntimeError):
        return 0
    
    total_bytes = 0
    stack = [p]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        # Nunca seguir enlaces simbólicos o puntos de reparse
                        if entry.is_symlink():
                            continue
                        if entry.is_dir():
                            path_entry = Path(entry.path)
                            if not is_protected_path(path_entry):
                                stack.append(entry.path)
                        elif entry.is_file():
                            total_bytes += entry.stat().st_size
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
            
    return total_bytes


def _is_valid_cache_path(candidate: Path, base_path: Path) -> bool:
    """
    Verifica si una ruta es un directorio de caché válido y seguro.
    """
    try:
        if not candidate.exists():
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
    Explora directorios base buscando carpetas de caché definidas.

    Args:
        bases: Lista de directorios raíz donde buscar perfiles de navegador.
        cache_paths: Diccionario de rutas relativas de caché por navegador.

    Returns:
        Lista de objetos BrowserCache con la información de las carpetas halladas.
    """
    if bases is None:
        bases = base_directories()
    if cache_paths is None:
        cache_paths = BROWSER_CACHE_PATHS

    if not isinstance(bases, (list, tuple)) or not isinstance(cache_paths, dict):
        return []

    found: List[BrowserCache] = []
    
    for base in bases:
        if not isinstance(base, Path) or not base.is_dir() or is_protected_path(base):
            continue
            
        try:
            resolved_base = base.resolve(strict=True)
        except (OSError, RuntimeError):
            continue

        for browser_name, relative_path_str in cache_paths.items():
            if not isinstance(relative_path_str, str) or not relative_path_str:
                continue
                
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
    """Suma el tamaño total en bytes de una lista de cachés."""
    if caches is None:
        directory_size.cache_clear()
        caches = detect_profiles()
    
    if not isinstance(caches, (list, tuple)):
        return 0

    return sum(cache.size_bytes for cache in caches)


def summarize(caches: List[BrowserCache] | None = None) -> List[str]:
    """Genera un reporte legible como lista de strings para mostrar en UI."""
    if caches is None:
        directory_size.cache_clear()
        current_caches = detect_profiles()
    else:
        if not isinstance(caches, list):
            return ["Error: Formato de datos de caché inválido."]
        current_caches = caches
    
    if not current_caches:
        return ["No se detectaron cachés de navegador en este sistema."]
        
    total_mb = round(total_cache_bytes(current_caches) / (1024 * 1024), 2)
    lines = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
