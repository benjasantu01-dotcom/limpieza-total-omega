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
from typing import (
    Iterable, Sequence, Dict, List, Optional, Callable, 
    Union, TypeAlias, NamedTuple, Set, TypeGuard, Any
)
import functools

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

# Aliases para mejorar la legibilidad de las firmas de funciones
JunctionChecker: TypeAlias = Callable[[str], bool]
BrowserMap: TypeAlias = Dict[str, str]
OSPath: TypeAlias = Union[str, Path]
VisitedDirs: TypeAlias = Dict[str, int]
SafetyCache: TypeAlias = Dict[str, bool]

PATH_FORBIDDEN_CHARS: Set[str] = {'*', '?', '<', '>', '|', '"'}

@dataclass
class ScanContext:
    """
    Contexto de ejecución que mantiene el estado del escaneo actual.

    Attributes:
        sandbox_base_norm: Ruta base normalizada (en minúsculas) usada para restringir el alcance.
        kernel32: Handle de la API de Windows para consultas de bajo nivel o None.
        is_junction: Función callback para detectar puntos de reparse (Junctions/Symlinks).
        visited_files: Historial de tuplas (dev, ino) para evitar procesar el mismo archivo dos veces.
        visited_dirs: Cache de tamaños de directorios ya procesados para evitar re-escaneo.
        safety_cache: Diccionario con resultados previos de validación de seguridad (is_safe_to_modify).
    """
    sandbox_base_norm: str
    kernel32: Optional[ctypes.WinDLL]
    is_junction: JunctionChecker
    visited_files: Set[tuple[int, int]]
    visited_dirs: VisitedDirs
    safety_cache: SafetyCache

def safe_path_operation(default: Any) -> Callable:
    """
    Decorador que envuelve funciones de E/S.
    Si ocurre una excepción de sistema o permisos, retorna el valor 'default'
    en lugar de propagar el error, permitiendo que el escaneo continúe.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except (OSError, PermissionError, RuntimeError, AttributeError, ValueError, TypeError):
                return default
        return wrapper
    return decorator

class ScanResult(NamedTuple):
    """Resultado inmutable de un escaneo de directorio."""
    bytes_found: int
    success: bool

class FileAttributes(NamedTuple):
    """Máscaras de bits constantes para la Win32 API."""
    READONLY: int = 0x01
    HIDDEN: int = 0x02
    SYSTEM: int = 0x04
    REPARSE_POINT: int = 0x400

SYSTEM_HIDDEN_FLAGS: int = (
    FileAttributes().HIDDEN | 
    FileAttributes().SYSTEM | 
    FileAttributes().REPARSE_POINT
)

def _is_junction_default(path: str) -> bool:
    return False

_IS_JUNCTION_FN: JunctionChecker = getattr(os.path, 'isjunction', _is_junction_default)

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

def _is_valid_rel_path(p: str) -> bool:
    """Valida que una ruta relativa no contenga secuencias de escape, caracteres ilegales o referencias absolutas."""
    if not p or not isinstance(p, str): return False
    # Detecta referencias a unidades o raíces absolutas
    if os.path.isabs(p) or p.startswith(('\\', '/')): return False
    if '..' in p or any(c in p for c in PATH_FORBIDDEN_CHARS): return False
    return True

for _k, _v in BROWSER_CACHE_PATHS.items():
    if not _is_valid_rel_path(_v):
        raise ValueError(f"Ruta de configuración insegura detectada: {_v}")

NEVER_TOUCH: frozenset[str] = frozenset({
    "login data", "cookies", "web data", "bookmarks", "history",
    "preferences", "local state", "extensions", "profile",
})

SAFETY_NOTE: str = (
    "Solo se listan carpetas de caché, que el navegador regenera solo. "
    "Nunca se tocan contraseñas, cookies, marcadores ni historial."
)

MAX_SCAN_DEPTH: int = 15
MAX_PATH_LEN: int = 260
BYTES_TO_MB: float = 1024 * 1024

@dataclass
class BrowserCache:
    """Modelo de datos para representar una instancia de caché de navegador detectada."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño en MB convertido y redondeado."""
        return round(self.size_bytes / BYTES_TO_MB, 2)

@safe_path_operation(None)
def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """Carga kernel32.dll bajo demanda para consultas de atributos específicos de Windows."""
    if os.name != 'nt':
        return None
    dll = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    return dll if hasattr(dll, 'GetFileAttributesW') else None

def _is_unc_path(path_str: Optional[str]) -> TypeGuard[str]:
    """Identifica rutas de red (UNC) para limitar el escaneo solo a unidades locales."""
    return isinstance(path_str, str) and (path_str.startswith(r"\\") or path_str.startswith("//"))

def _is_within_sandbox(target_norm: str, sandbox_base_norm: str) -> bool:
    """Verifica si la ruta destino está contenida estrictamente en la base de datos permitida."""
    return target_norm.startswith(sandbox_base_norm)

def base_directories() -> List[Path]:
    """Retorna la lista de rutas base permitidas (LOCALAPPDATA) para búsqueda de perfiles."""
    local_env = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env or _is_unc_path(local_env):
        return []
    
    p = Path(local_env)
    if p.exists() and p.is_dir():
        try:
            path_local: Path = p.resolve(strict=True)
            if is_safe_to_modify(path_local) and not is_protected_path(path_local):
                return [path_local]
        except (OSError, RuntimeError):
            return []
    return []

def _is_excluded_file(name: Optional[str]) -> TypeGuard[str]:
    """Verifica si el nombre de archivo o carpeta está en la lista de exclusión (NEVER_TOUCH)."""
    return isinstance(name, str) and name.lower() in NEVER_TOUCH

@safe_path_operation(False)
def _is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """Determina si un archivo tiene atributos de sistema u oculto mediante la Win32 API."""
    if kernel32 is None: return False
    attrs: int = kernel32.GetFileAttributesW(entry_path)
    return attrs != 0xFFFFFFFF and bool(attrs & SYSTEM_HIDDEN_FLAGS)

def _is_path_safe(path_str: str, ctx: ScanContext) -> bool:
    """Valida seguridad de una ruta utilizando el caché de validaciones del contexto actual."""
    if not isinstance(path_str, str): return False
    if path_str not in ctx.safety_cache:
        p = Path(path_str)
        ctx.safety_cache[path_str] = is_safe_to_modify(p) and not is_protected_path(p)
    return ctx.safety_cache[path_str]

def _should_skip_entry(entry: os.DirEntry, ctx: ScanContext) -> bool:
    """
    Reglas de filtrado: omite entradas bloqueadas, fuera de sandbox, 
    links simbólicos, junctions, o archivos del sistema.
    """
    if _is_excluded_file(entry.name):
        return True
    
    path_str = entry.path
    if _is_unc_path(path_str) or len(path_str) >= MAX_PATH_LEN or any(c in path_str for c in PATH_FORBIDDEN_CHARS):
        return True
    
    if not _is_within_sandbox(os.path.normcase(path_str), ctx.sandbox_base_norm):
        return True
    
    if not _is_path_safe(path_str, ctx):
        return True

    if entry.is_symlink() or ctx.is_junction(path_str) or _is_system_hidden(path_str, ctx.kernel32):
        return True
        
    return False

def _process_file_node(entry: os.DirEntry, ctx: ScanContext) -> int:
    """
    Calcula el tamaño de archivo detectando inodos duplicados 
    (para evitar conteos cíclicos o recursivos en sistemas con links).
    """
    try:
        if not os.access(entry.path, os.R_OK) or not _is_path_safe(entry.path, ctx):
            return 0
        st = entry.stat(follow_symlinks=False)
        if not hasattr(st, 'st_dev') or not hasattr(st, 'st_ino'):
            return 0
        file_id = (st.st_dev, st.st_ino)
        if file_id in ctx.visited_files:
            return 0
        ctx.visited_files.add(file_id)
        return st.st_size
    except (OSError, PermissionError, FileNotFoundError, AttributeError):
        return 0

def _sum_directory_recursive(root_path: str, ctx: ScanContext, depth: int = 0) -> ScanResult:
    """Recorrido recursivo del sistema de archivos limitado por MAX_SCAN_DEPTH."""
    if depth > MAX_SCAN_DEPTH or len(root_path) >= MAX_PATH_LEN or not isinstance(root_path, str):
        return ScanResult(0, True)
    
    if not _is_path_safe(root_path, ctx):
        return ScanResult(0, True)
    
    path_norm = os.path.normcase(root_path)
    if path_norm in ctx.visited_dirs:
        return ScanResult(ctx.visited_dirs[path_norm], True)

    total_bytes: int = 0
    try:
        with os.scandir(root_path) as it:
            for entry in it:
                if _should_skip_entry(entry, ctx):
                    continue
                        
                if entry.is_dir(follow_symlinks=False):
                    res = _sum_directory_recursive(entry.path, ctx, depth + 1)
                    total_bytes += res.bytes_found
                else:
                    total_bytes += _process_file_node(entry, ctx)
        
        ctx.visited_dirs[path_norm] = total_bytes
        return ScanResult(total_bytes, True)
    except (OSError, PermissionError):
        return ScanResult(total_bytes, False)

@safe_path_operation(0)
def directory_size(path: Optional[OSPath]) -> int:
    """Calcula el tamaño total en bytes de un directorio, garantizando seguridad de acceso."""
    if not isinstance(path, (str, Path)): return 0
    path_obj: Path = Path(path)
    if not path_obj.exists() or not path_obj.is_dir(): return 0
    
    resolved_p: Path = path_obj.resolve(strict=True)
    if not is_safe_to_modify(resolved_p) or is_protected_path(resolved_p):
        return 0
    
    path_str = str(resolved_p)
    ctx = ScanContext(os.path.normcase(path_str), _get_kernel32(), _IS_JUNCTION_FN, set(), {}, {})
    # Pre-cargamos el caché de seguridad para el nodo raíz
    ctx.safety_cache[path_str] = True
    return _sum_directory_recursive(path_str, ctx, 0).bytes_found

@safe_path_operation(False)
def _is_valid_cache_path(candidate: Path, base_abs_str: str) -> bool:
    """Valida que una ruta candidata resuelta permanezca dentro del sandbox."""
    if not isinstance(candidate, Path) or not isinstance(base_abs_str, str): return False
    if not candidate.is_dir() or not os.access(candidate, os.R_OK): return False
    
    real: Path = candidate.resolve(strict=True)
    if not _is_within_sandbox(os.path.normcase(str(real)), os.path.normcase(base_abs_str)):
        return False
    if not is_safe_to_modify(real) or is_protected_path(real):
        return False
    return not (real.is_symlink() or _IS_JUNCTION_FN(str(real)) or _is_excluded_file(real.name))

@safe_path_operation(Path())
def _resolve_browser_path(real_base: Path, rel_str: str) -> Path:
    """Resuelve la ruta absoluta del caché garantizando confinamiento estricto al perfil del usuario."""
    if not isinstance(real_base, Path) or not isinstance(rel_str, str) or not _is_valid_rel_path(rel_str): 
        return Path()
    
    parts = [p for p in rel_str.split("\\") if p and p != "." and p != ".."]
    if not parts: return Path()
        
    target: Path = real_base.joinpath(*parts)
    if not target.exists() or not target.is_dir(): return Path()
    
    target_res = target.resolve(strict=True)
    if _is_within_sandbox(os.path.normcase(str(target_res)), os.path.normcase(str(real_base))) and \
       is_safe_to_modify(target_res) and not is_protected_path(target_res) and \
       not (target_res.is_symlink() or _IS_JUNCTION_FN(str(target_res))):
        return target_res
    return Path()

def detect_profiles(bases: Optional[Sequence[Path]] = None, cache_paths: Optional[BrowserMap] = None) -> List[BrowserCache]:
    """Realiza un inventario de cachés detectando perfiles de navegador en el sistema."""
    raw_bases = list(bases) if bases is not None else base_directories()
    if not isinstance(raw_bases, list) or not raw_bases: return []

    browser_map = cache_paths if isinstance(cache_paths, dict) else BROWSER_CACHE_PATHS
    found: List[BrowserCache] = []
    
    ctx = ScanContext("", _get_kernel32(), _IS_JUNCTION_FN, set(), {}, {})
    
    for base in raw_bases:
        if not isinstance(base, Path) or not base.exists(): continue
        try:
            real_base: Path = base.resolve(strict=True)
            ctx.sandbox_base_norm = os.path.normcase(str(real_base))
            
            for browser_name, rel_str in browser_map.items():
                candidate = _resolve_browser_path(real_base, rel_str)
                if candidate != Path() and _is_valid_cache_path(candidate, ctx.sandbox_base_norm):
                    scan_res = _sum_directory_recursive(str(candidate), ctx, 0)
                    if scan_res.bytes_found > 0:
                        found.append(BrowserCache(str(browser_name), candidate, scan_res.bytes_found))
        except (OSError, RuntimeError):
            continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found

def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    """Calcula el total de bytes ocupados por el conjunto de objetos BrowserCache."""
    if caches is None: return 0
    return sum(c.size_bytes for c in caches)

def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Genera una representación textual formateada de los resultados del escaneo."""
    current_caches = caches if isinstance(caches, list) else detect_profiles()
    if not current_caches:
        return ["No se detectaron cachés de navegador en este sistema."]
        
    total_mb: float = round(total_cache_bytes(current_caches) / BYTES_TO_MB, 2)
    lines: List[str] = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
