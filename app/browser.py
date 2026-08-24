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

# Tipo alias para claridad en funciones de callback de sistema
JunctionChecker = Callable[[str], bool]

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

    Returns:
        Un objeto WinDLL para acceder a GetFileAttributesW, o None en entornos
        no Windows o si la carga es denegada.
    """
    if os.name != 'nt':
        return None
    try:
        return ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None


def base_directories() -> List[Path]:
    """
    Determina los directorios base de perfil de usuario (LOCALAPPDATA).

    Returns:
        Lista de objetos Path validados y seguros para exploración. Retorna
        una lista vacía si el entorno no es válido o las restricciones de
        safety.py bloquean el acceso.
    """
    local_env = os.environ.get("LOCALAPPDATA")
    if not local_env:
        return []
    
    try:
        path_local = Path(local_env).resolve(strict=True)
        if path_local.is_dir() and is_safe_to_modify(path_local):
            return [path_local]
        return []
    except (OSError, RuntimeError, ValueError):
        return []


def _is_path_inside_base(real_target: Path, real_base: Path) -> bool:
    """
    Verifica confinamiento de ruta (evitar Path Traversal).

    Args:
        real_target: Ruta resuelta (canonical) a verificar.
        real_base: Ruta base permitida.

    Returns:
        True si target es subdirectorio de base, False en caso contrario.
    """
    try:
        if not is_safe_to_modify(real_target) or not is_safe_to_modify(real_base):
            return False
            
        target_parts = real_target.resolve().parts
        base_parts = real_base.resolve().parts
        
        if len(target_parts) <= len(base_parts):
            return False
        return target_parts[:len(base_parts)] == base_parts
    except (OSError, ValueError, RuntimeError, PermissionError):
        return False


def _is_excluded_file(name: str) -> bool:
    """Valida si un nombre de archivo está en la lista de bloqueo permanente."""
    return name.lower() in NEVER_TOUCH


def _is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """
    Consulta atributos de bajo nivel en Windows para detectar archivos de sistema.

    Args:
        entry_path: Ruta completa del archivo.
        kernel32: Instancia de ctypes.WinDLL o None.

    Returns:
        True si el archivo está marcado como oculto, sistema o solo lectura.
    """
    if not kernel32:
        return False
    try:
        attrs: int = kernel32.GetFileAttributesW(entry_path)
        if attrs == 0xFFFFFFFF:
            return False 
        return bool(attrs & (0x01 | 0x02 | 0x04))
    except (OSError, AttributeError, TypeError, ValueError, MemoryError, ctypes.ArgumentError):
        return False


def _should_skip_entry(entry: os.DirEntry, kernel32: Optional[ctypes.WinDLL], is_junction_fn: JunctionChecker) -> bool:
    """
    Filtro principal para omitir recursión en carpetas no deseadas.
    
    Aplica chequeos de seguridad (safety.py), exclusiones de usuario y 
    detección de puntos de reparse para evitar bucles infinitos.
    """
    if _is_excluded_file(entry.name) or is_protected_path(Path(entry.path)):
        return True
    
    try:
        is_reparse = entry.is_symlink() or is_junction_fn(entry.path)
        if is_reparse:
            return True
        if _is_system_hidden(entry.path, kernel32):
            return True
    except (OSError, PermissionError, FileNotFoundError):
        return True
    return False


def _is_within_depth_limit(depth: int, current_path: str) -> bool:
    """Verifica límites de recursión y restricciones de seguridad del sistema."""
    return depth <= MAX_SCAN_DEPTH and not is_protected_path(Path(current_path))


def _sum_directory_recursive(
    root_dir: str, 
    is_junction_fn: JunctionChecker, 
    kernel32: Optional[ctypes.WinDLL],
    memo: Dict[str, int]
) -> int:
    """
    Realiza un recorrido en profundidad para sumar tamaños de archivo con memoización.
    """
    if root_dir in memo:
        return memo[root_dir]

    def _walk(current_dir: str, depth: int) -> int:
        if not _is_within_depth_limit(depth, current_dir):
            return 0
        
        total: int = 0
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if _should_skip_entry(entry, kernel32, is_junction_fn):
                        continue
                    
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            total += _walk(entry.path, depth + 1)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except (OSError, PermissionError):
                        continue
        except (PermissionError, OSError, FileNotFoundError):
            return 0
        return total

    result = _walk(root_dir, 0)
    memo[root_dir] = result
    return result


def directory_size(path: Union[str, Path]) -> int:
    """
    Calcula el tamaño de una carpeta tras validar que sea una ruta segura.
    """
    if path is None:
        return 0
    try:
        p_obj = Path(path).resolve(strict=True)
        if not p_obj.is_dir() or not is_safe_to_modify(p_obj) or is_protected_path(p_obj):
            return 0
        
        is_junction: JunctionChecker = getattr(os.path, 'isjunction', lambda _: False)
        return _sum_directory_recursive(str(p_obj), is_junction, _get_kernel32(), {})
    except (OSError, PermissionError, RuntimeError, ValueError):
        return 0


def _is_valid_cache_path(candidate: Path, base_path: Path, is_junction_fn: JunctionChecker) -> bool:
    """
    Realiza las validaciones de seguridad finales sobre una ruta candidata.
    """
    try:
        if not candidate.exists(): return False
        real_candidate = candidate.resolve(strict=True)
        
        if (real_candidate.is_symlink() or is_junction_fn(str(real_candidate)) or
            not real_candidate.is_dir() or not is_safe_to_modify(real_candidate) or 
            is_protected_path(real_candidate) or not _is_path_inside_base(real_candidate, base_path) or 
            _is_excluded_file(real_candidate.name)):
            return False
        return True
    except (OSError, PermissionError, RuntimeError):
        return False


def detect_profiles(
    bases: Optional[Sequence[Path]] = None, 
    cache_paths: Optional[Dict[str, str]] = None
) -> List[BrowserCache]:
    """
    Escanea el sistema buscando rutas de caché conocidas con optimización de caché.
    """
    raw_bases = bases if bases is not None else base_directories()
    browser_map = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS
    
    if not raw_bases or not browser_map:
        return []
    
    is_junction: JunctionChecker = getattr(os.path, 'isjunction', lambda _: False)
    k32 = _get_kernel32()
    
    # Cacheo de resultados por ruta absoluta para evitar re-escaneo
    perf_cache: Dict[str, int] = {}
    found: List[BrowserCache] = []
    
    for base in raw_bases:
        if not base: continue
        try:
            real_base = base.resolve(strict=True)
            for browser_name, rel_str in browser_map.items():
                if not rel_str: continue
                candidate = real_base.joinpath(*rel_str.split("\\"))
                
                if _is_valid_cache_path(candidate, real_base, is_junction):
                    c_path = candidate.resolve()
                    path_str = str(c_path)
                    
                    if path_str not in perf_cache:
                        size = _sum_directory_recursive(path_str, is_junction, k32, perf_cache)
                        perf_cache[path_str] = size
                    
                    size = perf_cache[path_str]
                    if size > 0:
                        found.append(BrowserCache(browser_name, c_path, size))
        except (OSError, PermissionError): 
            continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Suma total de bytes de una lista de objetos BrowserCache."""
    return sum(cache.size_bytes for cache in (caches or []))


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """
    Genera un informe formateado de las cachés detectadas.
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
