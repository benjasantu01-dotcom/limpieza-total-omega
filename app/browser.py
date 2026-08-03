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
La exclusión se gestiona mediante la constante `NEVER_TOUCH`.

Diseño testeable: `detect_profiles` recibe la carpeta base por parámetro,
así en CI se puede simular una instalación con carpetas temporales.
"""

from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, Dict, List, Optional
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
    "Chrome (GPU)": r"Microsoft\Edge\User Data\Default\GPUCache",
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
    """Representación de una carpeta de caché detectada y su peso en disco."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño en MB. Precisión de 2 decimales para reportes."""
        return round(self.size_bytes / (1024 * 1024), 2)


def base_directories() -> List[Path]:
    """
    Obtiene los directorios raíz donde se alojan perfiles (LOCALAPPDATA).
    """
    if os.name != "nt":
        return []
    
    local = os.environ.get("LOCALAPPDATA")
    if not local or not isinstance(local, str):
        return []
    
    try:
        path_local = Path(local)
        # Validación estricta de existencia y tipo antes de retornar
        if path_local.is_absolute() and path_local.is_dir():
            return [path_local]
        return []
    except (OSError, RuntimeError, ValueError):
        return []


def _is_safe_path(target_path: Optional[Path], base_path: Optional[Path]) -> bool:
    """
    Valida la integridad de la ruta para prevenir Directory Traversal y 
    seguimiento accidental de puntos de reparse (junctions).
    """
    if not isinstance(target_path, Path) or not isinstance(base_path, Path):
        return False
    try:
        if any(ord(char) < 32 for char in target_path.name):
            return False

        real_base = Path(os.path.realpath(str(base_path)))
        real_target = Path(os.path.realpath(str(target_path)))
        
        if real_target.is_symlink() or (hasattr(os.path, 'isjunction') and os.path.isjunction(str(real_target))):
            return False

        if is_protected_path(target_path):
            return False
            
        return str(real_target).startswith(str(real_base))
    except (OSError, RuntimeError, ValueError, PermissionError):
        return False


def directory_size(path: str | os.PathLike | None) -> int:
    """
    Calcula el tamaño total en bytes de un directorio mediante búsqueda iterativa eficiente.
    """
    if path is None:
        return 0
    
    try:
        root = Path(path).resolve()
        # Validación de seguridad: no procesar nada que esté protegido
        if not root.exists() or not root.is_dir() or is_protected_path(root):
            return 0
    except (OSError, TypeError, ValueError):
        return 0
    
    total_bytes: int = 0
    stack: List[str] = [str(root)]
    
    while stack:
        current_dir_str = stack.pop()
        try:
            with os.scandir(current_dir_str) as it:
                for entry in it:
                    try:
                        # Saltar elementos protegidos o inválidos
                        if entry.name.lower() in NEVER_TOUCH:
                            continue
                        if any(ord(c) < 32 for c in entry.name):
                            continue
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            # Captura de errores de sistema al acceder a archivos
                            try:
                                total_bytes += entry.stat().st_size
                            except (OSError, PermissionError):
                                continue
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError, FileNotFoundError):
            continue
            
    return total_bytes


def _is_valid_cache_path(candidate: Path | None, base_path: Path) -> bool:
    """
    Verifica si una ruta es un directorio de caché candidato legítimo.
    """
    if not isinstance(candidate, Path) or not isinstance(base_path, Path):
        return False
    try:
        return (
            candidate.exists() and 
            candidate.is_dir() and 
            not candidate.is_symlink() and
            _is_safe_path(candidate, base_path) and
            candidate.name.lower() not in NEVER_TOUCH
        )
    except (OSError, PermissionError):
        return False


def detect_profiles(
    bases: Optional[Sequence[Path]] = None, 
    cache_paths: Optional[Dict[str, str]] = None
) -> List[BrowserCache]:
    """
    Explora directorios base buscando caché de navegadores.
    """
    bases = bases if bases is not None else base_directories()
    cache_paths = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS

    found: List[BrowserCache] = []
    if not bases:
        return found
        
    for base in bases:
        if not isinstance(base, Path): continue
        for browser_name, relative_path_str in cache_paths.items():
            try:
                parts = relative_path_str.split("\\")
                candidate = base.joinpath(*parts)
                
                if _is_valid_cache_path(candidate, base):
                    size = directory_size(candidate)
                    if size > 0:
                        found.append(BrowserCache(
                            browser=browser_name,
                            path=candidate,
                            size_bytes=size,
                        ))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Suma total de bytes de una colección de objetos BrowserCache."""
    return sum(cache.size_bytes for cache in (caches or []))


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """
    Genera un informe textual formateado para la UI.
    """
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
