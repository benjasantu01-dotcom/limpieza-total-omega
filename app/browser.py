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
    """
    Obtiene directorios base (LOCALAPPDATA) para búsqueda de perfiles.
    Retorna una lista vacía si el sistema no es Windows o la variable
    no está definida.
    """
    if os.name != "nt":
        return []
    local = os.environ.get("LOCALAPPDATA")
    if not local or not isinstance(local, str):
        return []
    
    path_local = Path(local)
    return [path_local] if path_local.is_dir() else []


def _is_safe_path(target_path: Optional[Path], base_path: Optional[Path]) -> bool:
    """
    Verifica que la ruta sea un descendiente legítimo de base_path y
    no esté marcada como protegida por `safety.py`. 
    """
    if not target_path or not base_path:
        return False
    try:
        if is_protected_path(target_path):
            return False
        # Normalización robusta para evitar errores de comparación entre sistemas de archivos
        abs_base = base_path.resolve(strict=False)
        abs_target = target_path.resolve(strict=False)
        return abs_base in abs_target.parents or abs_base == abs_target
    except (OSError, RuntimeError, ValueError):
        return False


@lru_cache(maxsize=32)
def directory_size(path: str | os.PathLike | None) -> int:
    """
    Calcula el tamaño total en bytes mediante suma recursiva no destructiva.
    """
    if path is None:
        return 0
    try:
        root_path = Path(path).resolve(strict=False)
        if not root_path.exists() or not root_path.is_dir() or is_protected_path(root_path):
            return 0
    except (OSError, RuntimeError):
        return 0
    
    total_bytes: int = 0
    stack: List[Path] = [root_path]
    visited: set[Path] = set()
    
    while stack:
        current_dir = stack.pop()
        if current_dir in visited:
            continue
        visited.add(current_dir)
        
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir():
                            stack.append(Path(entry.path))
                        elif entry.is_file():
                            total_bytes += entry.stat().st_size
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
    return total_bytes


def _is_valid_cache_path(candidate: Path, base_path: Path) -> bool:
    """
    Filtro estricto para validar que la ruta candidata pertenezca a un 
    navegador y sea un directorio de caché seguro.
    """
    try:
        return (
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
    """
    Explora directorios base en busca de cachés definidas en `cache_paths`.
    Retorna una lista ordenada de objetos `BrowserCache` por tamaño descendente.
    """
    bases = bases if bases is not None else base_directories()
    cache_paths = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS

    found: List[BrowserCache] = []
    for base in bases:
        if not base.is_dir():
            continue
            
        for browser_name, relative_path_str in cache_paths.items():
            try:
                candidate = base.joinpath(*relative_path_str.split("\\"))
                if _is_valid_cache_path(candidate, base):
                    found.append(BrowserCache(
                        browser=browser_name,
                        path=candidate,
                        size_bytes=directory_size(str(candidate)),
                    ))
            except (TypeError, AttributeError):
                continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Suma los bytes de una colección de objetos BrowserCache."""
    return sum(cache.size_bytes for cache in (caches or []))


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """
    Genera un informe textual listo para la UI, resumiendo el uso
    de disco de los cachés detectados.
    """
    current_caches: List[BrowserCache] = caches if caches is not None else detect_profiles()
    
    if not current_caches:
        return ["No se detectaron cachés de navegador en este sistema."]
        
    total_mb = round(total_cache_bytes(current_caches) / (1024 * 1024), 2)
    lines = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
