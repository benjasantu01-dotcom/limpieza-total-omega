"""
browser.py — detección de cachés de navegadores.

SOLO LECTURA: detecta qué navegadores hay instalados, dónde guardan su
caché y cuánto ocupa. **No borra nada.** Limpiar caché de navegador es
seguro en general, pero borrar la carpeta equivocada del perfil puede
hacerte perder sesiones, contraseñas guardadas o marcadores, así que acá
solo se reportan las carpetas y su tamaño; la limpieza pasa por la carpeta
de revisión de `organizer.py`, con confirmación del usuario.

GARANTÍAS DE OPERACIÓN:
- El módulo nunca escala privilegios ni intenta modificar el disco.
- Las excepciones en el acceso a archivos (archivos bloqueados, permisos) 
  se capturan silenciosamente para evitar abortos en escaneos masivos.
- Solo se procesan rutas contenidas en el perfil de usuario (LOCALAPPDATA) 
  para prevenir la navegación fuera del scope permitido.
"""

from __future__ import annotations
import os
import ctypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, Dict, List, Optional, Callable, Union, TypeAlias, NamedTuple

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

# Tipos definidos para mejorar la legibilidad de la arquitectura del módulo
JunctionChecker: TypeAlias = Callable[[str], bool]
BrowserMap: TypeAlias = Dict[str, str]
OSPath: TypeAlias = Union[str, Path]

class FileAttributes(NamedTuple):
    """Flags de Win32 para filtrar archivos del sistema mediante bitmask."""
    READONLY: int = 0x01
    HIDDEN: int = 0x02
    SYSTEM: int = 0x04
    REPARSE_POINT: int = 0x400

# Máscara usada para ignorar archivos de sistema, ocultos o puntos de unión (junctions).
SYSTEM_HIDDEN_FLAGS: int = (
    FileAttributes().HIDDEN | 
    FileAttributes().SYSTEM | 
    FileAttributes().REPARSE_POINT
)

def _is_junction_default(path: str) -> bool:
    """Retorna siempre False; utilizado como fallback si os.path.isjunction no existe."""
    return False

# Acceso seguro a la funcionalidad de junctions si existe en el runtime (Python 3.12+)
_IS_JUNCTION_FN: JunctionChecker = getattr(os.path, 'isjunction', _is_junction_default)

# Mapeo de nombres descriptivos a rutas relativas dentro de LOCALAPPDATA.
BROWSER_CACHE_PATHS: BrowserMap = {
    "Google Chrome": r"Google\Chrome\User Data\Default\Cache",
    "Microsoft Edge": r"Microsoft\Edge\User Data\Default\Cache",
    "Brave": r"BraveSoftware\Brave-Browser\User Data\Default\Cache",
    "Opera": r"Opera Software\Opera Stable\Cache",
    "Vivaldi": r"Vivaldi\User Data\Default\Cache",
    "Chrome (código)": r"Google\Chrome\User Data\Default\Code Cache",
    "Edge (código)": r"Microsoft\Edge\User Data\Default\Code Cache",
    "Chrome (GPU)": r"Microsoft\Edge\User Data\Default\GPUCache",
}

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
MAX_PATH_LEN: int = 260

@dataclass
class BrowserCache:
    """Representa un hallazgo de caché de navegador con su ubicación y tamaño."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño en Megabytes con dos decimales de precisión."""
        return round(self.size_bytes / (1024 * 1024), 2)


def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """
    Inicializa la interfaz a la API de Windows mediante ctypes.
    
    Returns:
        La DLL kernel32 cargada o None si el SO no es Windows.
    """
    if os.name != 'nt':
        return None
    try:
        dll = ctypes.WinDLL('kernel32.dll', use_last_error=True)
        return dll if hasattr(dll, 'GetFileAttributesW') else None
    except Exception:
        return None

def _is_unc_path(path_str: Optional[str]) -> bool:
    """Valida si una cadena representa una ruta UNC (Universal Naming Convention)."""
    if not isinstance(path_str, str) or not path_str:
        return False
    return path_str.startswith(r"\\") or path_str.startswith("//")

def base_directories() -> List[Path]:
    """
    Identifica la raíz LOCALAPPDATA del sistema actual.
    
    Returns:
        Lista conteniendo el path de LOCALAPPDATA si es seguro, vacío si no.
    """
    local_env = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env or _is_unc_path(local_env):
        return []
    
    try:
        p = Path(local_env)
        if p.exists() and p.is_dir():
            path_local = p.resolve(strict=True)
            if is_safe_to_modify(path_local) and not is_protected_path(path_local):
                return [path_local]
    except (OSError, RuntimeError, PermissionError):
        pass
    return []


def _is_path_inside_base(target_abs: str, base_abs: str) -> bool:
    """
    Verifica que target_abs esté contenido jerárquicamente dentro de base_abs.
    
    Args:
        target_abs: Ruta absoluta candidata.
        base_abs: Ruta absoluta base de confianza.
        
    Returns:
        True si la ruta está contenida en la base, False en caso contrario.
    """
    if not isinstance(target_abs, str) or not isinstance(base_abs, str):
        return False
    try:
        target_norm = os.path.normcase(os.path.normpath(target_abs))
        base_norm = os.path.normcase(os.path.normpath(base_abs))
        if len(target_norm) >= MAX_PATH_LEN or '\0' in target_norm:
            return False
        return target_norm.startswith(base_norm)
    except (OSError, ValueError):
        return False


def _is_excluded_file(name: Optional[str]) -> bool:
    """Valida si un nombre de archivo está explícitamente en NEVER_TOUCH."""
    return name is not None and name.lower() in NEVER_TOUCH


def _is_system_hidden(entry_path: Optional[str], kernel32: Optional[ctypes.WinDLL]) -> bool:
    """
    Determina si un archivo tiene atributos de sistema, ocultos o es punto de reparse
    utilizando el bitmask definido en SYSTEM_HIDDEN_FLAGS vía API de Win32.
    """
    if kernel32 is None or not entry_path: return False
    try:
        attrs = kernel32.GetFileAttributesW(entry_path)
        return False if attrs == 0xFFFFFFFF else bool(attrs & SYSTEM_HIDDEN_FLAGS)
    except Exception:
        return False


def _should_skip_entry(
    entry: os.DirEntry, 
    kernel32: Optional[ctypes.WinDLL], 
    is_junction_fn: JunctionChecker
) -> bool:
    """Aplica políticas de filtrado antes de procesar una entrada de sistema de archivos."""
    if entry.name is None or _is_excluded_file(entry.name):
        return True
    
    try:
        path = entry.path
        if not path or len(path) >= MAX_PATH_LEN or _is_unc_path(path):
            return True
        if entry.is_symlink() or is_junction_fn(path) or _is_system_hidden(path, kernel32):
            return True
    except (OSError, AttributeError):
        return True
    return False

def _process_file_entry(
    entry: os.DirEntry,
    is_junction_fn: JunctionChecker,
    kernel32: Optional[ctypes.WinDLL],
    memo: Dict[int, int],
    depth: int
) -> int:
    """Procesa una entrada de directorio: recurre si es carpeta o suma bytes si es archivo."""
    try:
        child_path = Path(entry.path)
        if not is_safe_to_modify(child_path) or is_protected_path(child_path):
            return 0
            
        if entry.is_dir(follow_symlinks=False):
            return _sum_directory_recursive(child_path, is_junction_fn, kernel32, memo, depth + 1)
        else:
            return entry.stat(follow_symlinks=False).st_size
    except (OSError, PermissionError):
        return 0

def _sum_directory_recursive(
    root_path: Path, 
    is_junction_fn: JunctionChecker, 
    kernel32: Optional[ctypes.WinDLL],
    memo: Dict[int, int],
    depth: int = 0
) -> int:
    """Recorre el árbol de directorios para sumar el tamaño de archivos."""
    if depth > MAX_SCAN_DEPTH:
        return 0
    
    try:
        root_stat = root_path.stat()
        if root_stat.st_ino in memo:
            return 0
        memo[root_stat.st_ino] = root_stat.st_size
    except (OSError, PermissionError):
        return 0

    total_bytes: int = 0
    try:
        with os.scandir(root_path) as it:
            for entry in it:
                if _should_skip_entry(entry, kernel32, is_junction_fn):
                    continue
                total_bytes += _process_file_entry(entry, is_junction_fn, kernel32, memo, depth)
    except (OSError, PermissionError):
        pass
        
    return total_bytes


def directory_size(path: Optional[OSPath]) -> int:
    """Punto de entrada público para calcular el tamaño de una ruta."""
    if not path: return 0
    try:
        p = Path(path).resolve(strict=True)
        if not p.is_dir() or not is_safe_to_modify(p) or is_protected_path(p):
            return 0
        return _sum_directory_recursive(p, _IS_JUNCTION_FN, _get_kernel32(), {})
    except (OSError, RuntimeError, PermissionError):
        return 0


def _is_valid_cache_path(candidate: Path, base_abs_str: str, is_junction_fn: JunctionChecker) -> bool:
    """Valida si un path es un directorio de caché legítimo y seguro dentro de LOCALAPPDATA."""
    try:
        real = candidate.resolve(strict=True)
        if not real.is_dir() or not _is_path_inside_base(str(real), base_abs_str):
            return False
        if not is_safe_to_modify(real) or is_protected_path(real):
            return False
        return not (real.is_symlink() or is_junction_fn(str(real)) or _is_excluded_file(real.name))
    except (OSError, RuntimeError):
        return False


def _resolve_browser_path(real_base: Path, rel_str: str) -> Path:
    """Une la base con el path relativo y verifica su integridad estructural."""
    if not isinstance(real_base, Path) or not rel_str:
        return Path()
    try:
        # Uso de Path.joinpath para unir componentes de ruta de manera segura
        target = real_base.joinpath(*rel_str.split("\\")).resolve(strict=True)
        if not _is_path_inside_base(str(target), str(real_base)):
            return Path()
        if not is_safe_to_modify(target) or is_protected_path(target):
            return Path()
        return target if len(str(target)) < MAX_PATH_LEN else Path()
    except (OSError, RuntimeError):
        return Path()


def detect_profiles(bases: Optional[Sequence[Path]] = None, cache_paths: Optional[BrowserMap] = None) -> List[BrowserCache]:
    """Escaneo principal: detecta, valida y calcula el tamaño de los directorios de caché."""
    raw_bases = bases if bases is not None else base_directories()
    browser_map = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS
    k32 = _get_kernel32()
    found: List[BrowserCache] = []
    
    global_memo: Dict[int, int] = {}
    
    for base in raw_bases:
        try:
            real_base = base.resolve(strict=True)
            for browser_name, rel_str in browser_map.items():
                candidate = _resolve_browser_path(real_base, rel_str)
                if not candidate or not _is_valid_cache_path(candidate, str(real_base), _IS_JUNCTION_FN):
                    continue
                
                size = _sum_directory_recursive(candidate, _IS_JUNCTION_FN, k32, global_memo)
                if size > 0:
                    found.append(BrowserCache(str(browser_name), candidate, size))
        except (OSError, RuntimeError):
            continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    """Calcula el total de bytes sumando las cachés detectadas."""
    return sum(c.size_bytes for c in caches) if caches else 0


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Genera un reporte textual formateado con los resultados obtenidos."""
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
