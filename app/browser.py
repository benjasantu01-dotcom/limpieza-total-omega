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

MAX_SCAN_DEPTH: int = 15

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


def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """
    Intenta cargar kernel32.dll para acceder a atributos de archivo de bajo nivel.
    Retorna None si no es Windows o si la carga falla por políticas de sistema.
    """
    if os.name != 'nt':
        return None
    try:
        return ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None


def base_directories() -> List[Path]:
    """
    Identifica la ruta LOCALAPPDATA del usuario actual.
    Valida la existencia y seguridad de la ruta antes de retornarla.
    """
    if os.name != "nt":
        return []
    
    local = os.environ.get("LOCALAPPDATA")
    if not isinstance(local, str) or not local:
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
    Valida mediante resolución absoluta y chequeos de seguridad (is_protected_path)
    que la ruta objetivo resida dentro de la base permitida, previniendo escalada de directorios.
    """
    if not isinstance(target_path, Path) or not isinstance(base_path, Path):
        return False
    
    try:
        if not target_path.is_absolute() or not base_path.is_absolute():
            return False

        if is_protected_path(target_path) or is_protected_path(base_path):
            return False

        real_base = base_path.resolve(strict=True)
        real_target = target_path.resolve(strict=True)
        
        if not str(real_target).startswith(str(real_base)):
            return False

        is_junction: Callable[[str], bool] = getattr(os.path, 'isjunction', lambda _: False)
        if real_target.is_symlink() or is_junction(str(real_target)):
            return False

        return True
    except (OSError, ValueError, RuntimeError, PermissionError):
        return False


def _is_excluded_file(name: Optional[str]) -> bool:
    """Verifica si un nombre de archivo está en la lista negra de componentes críticos (NEVER_TOUCH)."""
    if not isinstance(name, str) or not name:
        return True
    return name.lower() in NEVER_TOUCH


def _is_system_hidden(entry_path: str | None, kernel32: ctypes.WinDLL | None) -> bool:
    """
    Consulta los atributos de archivo de Windows (GetFileAttributesW) para descartar 
    objetos con banderas de SISTEMA o HIDDEN, evitando procesar archivos protegidos del SO.
    """
    if not kernel32 or not isinstance(entry_path, str) or not entry_path:
        return False
    try:
        if not os.path.exists(entry_path):
            return False
        path_to_check = entry_path if not entry_path.startswith(r"\\?") else entry_path[4:]
        attrs: int = kernel32.GetFileAttributesW(path_to_check)
        if attrs == 0xFFFFFFFF:
            return False
        
        # Constantes de atributos de archivo de Windows:
        # FILE_ATTRIBUTE_HIDDEN (0x02) | FILE_ATTRIBUTE_SYSTEM (0x04)
        return bool(attrs & 0x04 or attrs & 0x02)
    except (OSError, AttributeError, TypeError, ValueError, MemoryError, ctypes.ArgumentError):
        return False


def _should_skip_entry(entry: os.DirEntry, kernel32: ctypes.WinDLL | None, is_junction_fn: Callable[[str], bool]) -> bool:
    """
    Determina si un objeto del sistema de archivos es inseguro de procesar 
    basándose en su nombre, atributos de sistema o tipo (symlinks/junctions).
    """
    if _is_excluded_file(entry.name):
        return True
    
    try:
        if is_protected_path(Path(entry.path)):
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
    is_junction_fn: Callable[[str], bool], 
    kernel32: ctypes.WinDLL | None,
    memo: Dict[str, int]
) -> int:
    """
    Suma el tamaño de archivos en una jerarquía aplicando límites de seguridad y profundidad.
    
    Utiliza un recorrido DFS (Depth-First Search) con memorización para optimizar el acceso
    al disco y evitar re-procesar subdirectorios ya visitados. Se detiene al alcanzar
    MAX_SCAN_DEPTH para prevenir desbordamientos en estructuras de archivos complejas.
    """
    if not isinstance(root_dir, str) or not root_dir:
        return 0
    
    try:
        abs_root = Path(root_dir).resolve(strict=True)
        if not abs_root.is_dir() or is_protected_path(abs_root) or abs_root.is_symlink() or is_junction_fn(str(abs_root)):
            return 0
        root_key = str(abs_root)
    except (OSError, PermissionError, RuntimeError):
        return 0

    if root_key in memo:
        return memo[root_key]

    def _walk(current_dir: str, depth: int) -> int:
        if depth > MAX_SCAN_DEPTH:
            return 0
        
        if current_dir in memo:
            return memo[current_dir]
        
        total: int = 0
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if _should_skip_entry(entry, kernel32, is_junction_fn):
                        continue
                    try:
                        if is_protected_path(Path(entry.path)):
                            continue
                        if entry.is_dir():
                            total += _walk(entry.path, depth + 1)
                        elif entry.is_file():
                            total += entry.stat(follow_symlinks=False).st_size
                    except (OSError, PermissionError, FileNotFoundError):
                        continue
        except (PermissionError, OSError, FileNotFoundError):
            return 0
        
        memo[current_dir] = total
        return total

    result = _walk(root_key, 0)
    memo[root_key] = result
    return result


def directory_size(path: Union[str, os.PathLike, None]) -> int:
    """Calcula el tamaño total en bytes de un directorio, aplicando validaciones de seguridad."""
    if path is None:
        return 0
    try:
        p_obj = Path(path)
        if not p_obj.exists():
            return 0
        p_path = p_obj.resolve(strict=True)
        if not p_path.is_absolute() or not p_path.is_dir() or is_protected_path(p_path):
            return 0
        
        is_junction: Callable[[str], bool] = getattr(os.path, 'isjunction', lambda _: False)
        return _sum_directory_recursive(str(p_path), is_junction, _get_kernel32(), {})
    except (OSError, PermissionError, RuntimeError, ValueError):
        return 0


def _is_valid_cache_path(candidate: Optional[Path], base_path: Path) -> bool:
    """Valida si un candidato es una ruta de caché existente y segura."""
    if not isinstance(candidate, Path) or not isinstance(base_path, Path):
        return False
    try:
        return (candidate.exists() and candidate.is_dir() and not is_protected_path(candidate) and
                _is_safe_path(candidate, base_path) and not _is_excluded_file(candidate.name))
    except (OSError, PermissionError, RuntimeError):
        return False


def detect_profiles(
    bases: Optional[Sequence[Path]] = None, 
    cache_paths: Optional[Dict[str, str]] = None
) -> List[BrowserCache]:
    """
    Escanea las ubicaciones base buscando las rutas definidas en BROWSER_CACHE_PATHS.
    Utiliza inyección de dependencias para permitir pruebas en entornos CI.
    """
    raw_bases = bases if bases is not None else base_directories()
    cache_paths = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS
    
    if not isinstance(raw_bases, (list, tuple)) or not isinstance(cache_paths, dict):
        return []

    is_junction: Callable[[str], bool] = getattr(os.path, 'isjunction', lambda _: False)
    k32 = _get_kernel32()
    
    perf_cache: Dict[str, int] = {}
    found: List[BrowserCache] = []
    
    for base in raw_bases:
        if not isinstance(base, Path): continue
        try:
            real_base = base.resolve(strict=True)
        except (OSError, PermissionError): continue
            
        for browser_name, rel_str in cache_paths.items():
            if not isinstance(browser_name, str) or not isinstance(rel_str, str): 
                continue
            try:
                candidate = real_base.joinpath(*rel_str.split("\\"))
                if _is_valid_cache_path(candidate, real_base):
                    c_path = candidate.resolve()
                    size = _sum_directory_recursive(str(c_path), is_junction, k32, perf_cache)
                    if size > 0:
                        found.append(BrowserCache(browser_name, c_path, size))
            except (OSError, PermissionError): continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Suma total en bytes de una colección de cachés."""
    return sum(cache.size_bytes for cache in (caches or []))


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Genera un reporte legible por humanos de las cachés encontradas y su peso."""
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
