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
from safety import is_protected_path, is_safe_to_modify

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

# Alias para funciones de chequeo de reparse points / junctions
JunctionChecker = Callable[[str], bool]

def _is_junction_default(path: str) -> bool:
    """Fallback si el entorno no soporta la detección de junctions."""
    return False

# Acceso seguro a la funcionalidad de junctions si existe en el runtime
_IS_JUNCTION_FN: JunctionChecker = getattr(os.path, 'isjunction', _is_junction_default)

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

# Conjunto de nombres de archivos/carpetas que NUNCA deben ser procesados
# por contener datos persistentes críticos para el usuario.
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
# Atributos: FILE_ATTRIBUTE_HIDDEN (0x01) | FILE_ATTRIBUTE_SYSTEM (0x02) | FILE_ATTRIBUTE_REPARSE_POINT (0x400)
SYSTEM_HIDDEN_FLAGS: int = 0x01 | 0x02 | 0x400

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
    Carga dinámicamente kernel32.dll para llamadas a la API de Win32.
    Retorna None si no es Windows o si la carga falla.
    """
    if os.name != 'nt':
        return None
    try:
        return ctypes.windll.kernel32
    except (AttributeError, OSError, ValueError, RuntimeError):
        return None


def base_directories() -> List[Path]:
    """
    Determina los directorios base de perfil de usuario (LOCALAPPDATA).
    Verifica la existencia y los permisos mediante `safety.py` antes de retornar.
    """
    local_env = os.environ.get("LOCALAPPDATA")
    if not local_env:
        return []
    
    try:
        path_local = Path(local_env).resolve(strict=True)
        if path_local.is_dir() and is_safe_to_modify(path_local) and not is_protected_path(path_local):
            return [path_local]
        return []
    except (OSError, RuntimeError, ValueError):
        return []


def _is_path_inside_base(real_target: Path, real_base: Path) -> bool:
    """
    Valida la jerarquía: confirma que 'real_target' se encuentra dentro de 'real_base'.
    Requiere rutas resueltas (absolutas y sin symlinks) para evitar saltos de directorio.
    """
    try:
        return real_base == real_target or real_base in real_target.parents
    except Exception:
        return False


def _is_excluded_file(name: str) -> bool:
    """Verifica si un nombre de archivo está en la lista de bloqueo permanente (NEVER_TOUCH)."""
    return name.lower() in NEVER_TOUCH


def __is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """
    Consulta los atributos de archivo mediante la API de Win32 para identificar
    elementos marcados como ocultos o de sistema, evitando el escaneo innecesario.
    """
    if kernel32 is None:
        return False
    try:
        attrs: int = kernel32.GetFileAttributesW(entry_path)
        if attrs == 0xFFFFFFFF:
            return False 
        return bool(attrs & SYSTEM_HIDDEN_FLAGS)
    except (OSError, AttributeError, TypeError, ValueError, MemoryError, ctypes.ArgumentError):
        return False


def _should_skip_entry(entry: os.DirEntry, kernel32: Optional[ctypes.WinDLL], is_junction_fn: JunctionChecker) -> bool:
    """
    Determina si una entrada (archivo o carpeta) debe ignorarse basándose en
    atributos de sistema, tipos de enlace (junctions/symlinks) o bloqueo de nombres.
    """
    if _is_excluded_file(entry.name):
        return True
        
    try:
        if entry.is_symlink() or is_junction_fn(entry.path) or os.path.ismount(entry.path):
            return True
        if __is_system_hidden(entry.path, kernel32):
            return True
    except (OSError, PermissionError, FileNotFoundError):
        return True
    return False


def _is_safe_to_traverse(path_obj: Path, base_check_path: Optional[Path]) -> bool:
    """
    Valida si el acceso a la ruta es seguro: verifica permisos, inexistencia
    de protecciones y, si se provee, que esté dentro del 'base_check_path' resuelto.
    """
    try:
        if not is_safe_to_modify(path_obj) or is_protected_path(path_obj):
            return False
        if base_check_path and not _is_path_inside_base(path_obj, base_check_path):
            return False
        return True
    except (OSError, RuntimeError):
        return False


def _sum_directory_recursive(
    root_abs: str, 
    is_junction_fn: JunctionChecker, 
    kernel32: Optional[ctypes.WinDLL],
    memo: Dict[str, int],
    base_check_path: Optional[Path] = None,
    depth: int = 0
) -> int:
    """
    Calcula el tamaño acumulado de archivos dentro de 'root_abs'. 
    Utiliza un diccionario 'memo' para cachear resultados de subdirectorios 
    y evitar redundancia, limitando la recursión mediante 'MAX_SCAN_DEPTH'.
    """
    if depth > MAX_SCAN_DEPTH:
        return 0
    
    if root_abs in memo:
        return memo[root_abs]

    total: int = 0
    try:
        with os.scandir(root_abs) as it:
            for entry in it:
                if _should_skip_entry(entry, kernel32, is_junction_fn):
                    continue
                
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _is_safe_to_traverse(Path(entry.path), base_check_path):
                            total += _sum_directory_recursive(
                                entry.path, is_junction_fn, kernel32, memo, base_check_path, depth + 1
                            )
                    elif entry.is_file(follow_symlinks=False):
                        try:
                            total += entry.stat(follow_symlinks=False).st_size
                        except (OSError, PermissionError):
                            continue
                except (OSError, PermissionError):
                    continue
    except (PermissionError, OSError):
        return 0
    
    memo[root_abs] = total
    return total


def directory_size(path: Union[str, Path, None]) -> int:
    """
    Punto de entrada para obtener el tamaño (bytes) de una ruta.
    Valida que la ruta sea segura y la resuelve antes de iniciar el escaneo recursivo.
    """
    if path is None:
        return 0
    try:
        p_res = Path(path).resolve(strict=True)
        if not p_res.is_dir() or not _is_safe_to_traverse(p_res, None):
            return 0
        return _sum_directory_recursive(str(p_res), _IS_JUNCTION_FN, _get_kernel32(), {})
    except (OSError, PermissionError, RuntimeError, ValueError):
        return 0


def _is_valid_cache_path(candidate: Path, base_path: Path, is_junction_fn: JunctionChecker) -> bool:
    """
    Verifica que la carpeta de caché candidata no sea un enlace externo,
    que sea una ruta segura, y que se mantenga bajo el 'base_path' permitido.
    """
    try:
        if not candidate.exists():
            return False
        real_candidate = candidate.resolve(strict=True)
        if (real_candidate.is_symlink() or is_junction_fn(str(real_candidate)) or 
            os.path.ismount(str(real_candidate)) or not real_candidate.is_dir() or 
            not _is_safe_to_traverse(real_candidate, base_path) or
            _is_excluded_file(real_candidate.name)):
            return False
        return True
    except (OSError, PermissionError, RuntimeError, ValueError):
        return False


def detect_profiles(
    bases: Optional[Sequence[Path]] = None, 
    cache_paths: Optional[Dict[str, str]] = None
) -> List[BrowserCache]:
    """
    Escanea las rutas definidas en 'cache_paths' relativas a 'bases'.
    Retorna una lista de objetos 'BrowserCache' ordenados por peso, utilizando 
    memoización para optimizar el cálculo del tamaño total en disco.
    """
    raw_bases = bases if bases is not None else base_directories()
    browser_map = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS
    
    if not raw_bases or not isinstance(browser_map, dict):
        return []
    
    k32 = _get_kernel32()
    perf_cache: Dict[str, int] = {}
    found: List[BrowserCache] = []
    
    for base in raw_bases:
        try:
            real_base = base.resolve(strict=True)
            for browser_name, rel_str in browser_map.items():
                candidate = real_base.joinpath(*rel_str.split("\\"))
                if _is_valid_cache_path(candidate, real_base, _IS_JUNCTION_FN):
                    c_path = candidate.resolve()
                    size = _sum_directory_recursive(str(c_path), _IS_JUNCTION_FN, k32, perf_cache, real_base)
                    if size > 0:
                        found.append(BrowserCache(browser_name, c_path, size))
        except (OSError, PermissionError, TypeError): 
            continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Calcula el acumulado de bytes de todas las cachés identificadas."""
    return sum(cache.size_bytes for cache in (caches or []))


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Genera una representación de texto para el reporte de salud del sistema."""
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
