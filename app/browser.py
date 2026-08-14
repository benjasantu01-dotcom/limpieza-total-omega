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
import ctypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, Dict, List, Optional, Callable, Set, Union
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

# Mapa de navegadores soportados a sus rutas relativas dentro de LOCALAPPDATA.
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

# Conjunto de nombres que representan datos críticos de usuario.
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
        """Retorna el tamaño en MB con precisión de 2 decimales."""
        return round(self.size_bytes / (1024 * 1024), 2)


def base_directories() -> List[Path]:
    """Retorna la lista de directorios base del sistema para buscar perfiles.
    
    Returns:
        Lista conteniendo LOCALAPPDATA si es una ruta válida, existente, 
        absoluta y no protegida. Retorna lista vacía ante cualquier error.
    """
    if os.name != "nt":
        return []
    
    local: Optional[str] = os.environ.get("LOCALAPPDATA")
    if not local or not isinstance(local, str):
        return []
    
    try:
        path_local = Path(local).resolve()
        if path_local.is_absolute() and path_local.is_dir() and not is_protected_path(path_local):
            return [path_local]
        return []
    except (OSError, RuntimeError, ValueError):
        return []


def _is_safe_path(target_path: Optional[Path], base_path: Optional[Path]) -> bool:
    """
    Valida la integridad de la ruta contra ataques de Path Traversal y 
    verificaciones de seguridad de nivel de sistema.
    """
    if not isinstance(target_path, Path) or not isinstance(base_path, Path):
        return False
    
    path_str = str(target_path)
    if "\0" in path_str or any(ord(char) < 32 or ord(char) in (0x200E, 0x200F, 0x202A, 0x202E) for char in path_str):
        return False
        
    try:
        real_base = base_path.resolve(strict=True)
        real_target = target_path.resolve(strict=True)
        
        if is_protected_path(real_target) or is_protected_path(real_base):
            return False

        try:
            real_target.relative_to(real_base)
        except ValueError:
            return False

        is_junction: Callable[[str], bool] = getattr(os.path, 'isjunction', lambda _: False)
        if real_target.is_symlink() or is_junction(str(real_target)):
            return False

        return True
    except (OSError, ValueError, RuntimeError, PermissionError):
        return False


def _is_excluded_file(name: str | None) -> bool:
    """Verifica si el nombre de archivo está en la lista de exclusión (NEVER_TOUCH)."""
    if not isinstance(name, str) or not name:
        return True
    return name.lower() in NEVER_TOUCH


def _is_system_hidden(entry_path: str | None, kernel32: ctypes.WinDLL | None) -> bool:
    """Consulta atributos de archivo oculto/sistema vía Win32 API para ignorar componentes internos."""
    if not kernel32 or not isinstance(entry_path, str) or not entry_path:
        return False
    try:
        attrs = kernel32.GetFileAttributesW(entry_path)
        if attrs == 0xFFFFFFFF:
            return False
        return bool(attrs & 0x04 or attrs & 0x02)
    except (OSError, AttributeError, TypeError, ValueError, MemoryError, ctypes.ArgumentError):
        return False


def _should_skip_entry(entry: os.DirEntry, kernel32: ctypes.WinDLL | None, is_junction_fn: Callable[[str], bool]) -> bool:
    """Determina si un item en el sistema de archivos debe ser omitido del cálculo de tamaño."""
    if not isinstance(entry, os.DirEntry) or not hasattr(entry, 'path'):
        return True
    try:
        if _is_excluded_file(entry.name):
            return True
        if _is_system_hidden(entry.path, kernel32):
            return True
        if entry.is_symlink() or is_junction_fn(entry.path):
            return True
    except (OSError, PermissionError):
        return True
    return False


def _sum_directory_recursive(
    root_dir: str, 
    base_dir: Path,
    is_junction_fn: Callable[[str], bool], 
    kernel32: ctypes.WinDLL | None,
    visited: Set[str], 
    cache: Dict[str, int], 
    depth: int = 0
) -> int:
    """
    Calcula el peso total en bytes mediante DFS limitado.
    
    Args:
        root_dir: Ruta actual a procesar.
        visited: Set para prevenir ciclos en el grafo del FS.
        depth: Límite de recursión (evita desbordamiento de pila en estructuras profundas).
    """
    if depth > 20 or root_dir in visited:
        return 0
    if root_dir in cache:
        return cache[root_dir]
    
    current_path = Path(root_dir)
    try:
        if not current_path.exists() or is_protected_path(current_path):
            return 0
    except (OSError, PermissionError):
        return 0
        
    visited.add(root_dir)
    total_size: int = 0
    try:
        with os.scandir(root_dir) as it:
            for entry in it:
                if _should_skip_entry(entry, kernel32, is_junction_fn):
                    continue
                try:
                    if entry.is_dir():
                        total_size += _sum_directory_recursive(entry.path, base_dir, is_junction_fn, kernel32, visited, cache, depth + 1)
                    else:
                        total_size += entry.stat().st_size
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        return 0
    
    cache[root_dir] = total_size
    return total_size


def directory_size(path: Union[str, os.PathLike, None]) -> int:
    """Calcula el tamaño en bytes de una carpeta tras validar que no sea una ruta de sistema."""
    if path is None:
        return 0
    
    try:
        p_path = Path(path)
        if not p_path.exists():
            return 0
        root_path = p_path.resolve(strict=True)
        if not root_path.is_absolute() or not root_path.is_dir() or is_protected_path(root_path):
            return 0
        
        is_junction: Callable[[str], bool] = getattr(os.path, 'isjunction', lambda _: False)
        k32 = ctypes.windll.kernel32 if os.name == 'nt' else None
        return max(0, _sum_directory_recursive(str(root_path), root_path, is_junction, k32, set(), {}))
    except (OSError, PermissionError, RuntimeError, ValueError):
        return 0


def _is_valid_cache_path(candidate: Optional[Path], base_path: Path) -> bool:
    """Verifica si un directorio es candidato válido para ser analizado como caché de navegador."""
    if not isinstance(candidate, Path) or not isinstance(base_path, Path):
        return False
    try:
        return (
            candidate.exists() and 
            candidate.is_dir() and 
            not is_protected_path(candidate) and
            _is_safe_path(candidate, base_path) and
            not _is_excluded_file(candidate.name)
        )
    except (OSError, PermissionError, RuntimeError):
        return False


def detect_profiles(
    bases: Optional[Sequence[Path]] = None, 
    cache_paths: Optional[Dict[str, str]] = None
) -> List[BrowserCache]:
    """Escanea el sistema buscando rutas de caché conocidas y retorna una lista de objetos BrowserCache."""
    raw_bases = bases if bases is not None else base_directories()
    cache_paths = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS
    is_junction: Callable[[str], bool] = getattr(os.path, 'isjunction', lambda _: False)
    k32 = ctypes.windll.kernel32 if os.name == 'nt' else None
    
    perf_cache: Dict[str, int] = {}
    visited: Set[str] = set()
    found: List[BrowserCache] = []
    
    if not isinstance(raw_bases, (list, tuple)) or not isinstance(cache_paths, dict):
        return found
        
    for base in raw_bases:
        if not isinstance(base, Path): continue
        try:
            real_base = base.resolve(strict=True)
        except (OSError, PermissionError):
            continue
            
        for browser_name, relative_path_str in cache_paths.items():
            if not isinstance(relative_path_str, str): continue
            
            try:
                candidate = real_base.joinpath(*relative_path_str.split("\\"))
                if _is_valid_cache_path(candidate, real_base):
                    c_path = candidate.resolve()
                    c_path_str = str(c_path)
                    
                    size: int = _sum_directory_recursive(c_path_str, c_path, is_junction, k32, visited, perf_cache)
                    if size > 0:
                        found.append(BrowserCache(
                            browser=str(browser_name),
                            path=c_path,
                            size_bytes=size,
                        ))
            except (OSError, PermissionError):
                continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Retorna la sumatoria total de bytes de una colección de cachés."""
    return sum(cache.size_bytes for cache in (caches or []))


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Genera una representación textual formateada de los resultados del análisis."""
    current_caches: List[BrowserCache] = caches if caches is not None else detect_profiles()
    
    if not current_caches:
        return ["No se detectaron cachés de navegador en este sistema."]
        
    total_mb: float = round(total_cache_bytes(current_caches) / (1024 * 1024), 2)
    lines: List[str] = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
