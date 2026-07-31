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

# Cache interno para evitar re-escaneo de discos si no hubo cambios
_DIR_SIZE_CACHE: Dict[str, tuple[int, float]] = {}


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
    
    try:
        path_local = Path(local)
        return [path_local] if path_local.is_dir() else []
    except (OSError, RuntimeError):
        return []


def _is_safe_path(target_path: Optional[Path], base_path: Optional[Path]) -> bool:
    """
    Valida la integridad de la ruta para prevenir escapes de directorio
    mediante resolución de rutas absolutas.
    """
    if not target_path or not base_path:
        return False
    try:
        if is_protected_path(target_path):
            return False
        # Se normaliza la ruta para asegurar que el target esté realmente contenido
        # dentro de la estructura esperada del directorio base.
        abs_base = base_path.resolve(strict=False)
        abs_target = target_path.resolve(strict=False)
        return abs_base in abs_target.parents or abs_base == abs_target
    except (OSError, RuntimeError, ValueError, PermissionError):
        return False


def directory_size(path: str | os.PathLike | None) -> int:
    """
    Calcula el tamaño total en bytes mediante suma recursiva.
    Usa un caché basado en el tiempo de modificación del directorio.
    """
    if not path:
        return 0
    
    path_str = str(path)
    try:
        root = Path(path).resolve(strict=False)
        if not root.exists() or not root.is_dir() or root.is_symlink() or is_protected_path(root):
            return 0
        
        current_mtime = root.stat().st_mtime
        if path_str in _DIR_SIZE_CACHE:
            cached_size, cached_mtime = _DIR_SIZE_CACHE[path_str]
            if cached_mtime == current_mtime:
                return cached_size
    except (OSError, RuntimeError, PermissionError, ValueError):
        return 0
    
    total_bytes: int = 0
    stack: List[str] = [str(root)]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        # Saltar enlaces simbólicos y puntos de unión para evitar bucles
                        # infinitos o conteo duplicado de datos externos al caché.
                        if entry.is_symlink() or (hasattr(os.path, 'isjunction') and os.path.isjunction(entry.path)):
                            continue
                        if entry.is_dir():
                            stack.append(entry.path)
                        elif entry.is_file():
                            total_bytes += entry.stat().st_size
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
            
    _DIR_SIZE_CACHE[path_str] = (total_bytes, current_mtime)
    return total_bytes


def _is_valid_cache_path(candidate: Path | None, base_path: Path) -> bool:
    """
    Valida que la ruta exista, sea un directorio, no sea un enlace simbólico
    y cumpla con las políticas de seguridad de la app.
    """
    try:
        return (
            candidate is not None and
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
    Explora los directorios base buscando las rutas de caché predefinidas.
    """
    bases = bases if bases is not None else base_directories()
    cache_paths = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS

    found: List[BrowserCache] = []
    if not bases:
        return found
        
    for base in bases:
        for browser_name, relative_path_str in cache_paths.items():
            try:
                candidate = base.joinpath(*relative_path_str.split("\\"))
                
                if _is_valid_cache_path(candidate, base):
                    size = directory_size(str(candidate))
                    if size > 0:
                        found.append(BrowserCache(
                            browser=browser_name,
                            path=candidate,
                            size_bytes=size,
                        ))
            except (OSError, ValueError, TypeError):
                continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Suma los bytes de una colección de objetos BrowserCache."""
    return sum(cache.size_bytes for cache in (caches or []))


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """
    Genera un informe textual listo para la UI con el total de MB detectados.
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
