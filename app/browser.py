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
VisitedDirs: TypeAlias = Set[str]

PATH_FORBIDDEN_CHARS: Set[str] = {'*', '?', '<', '>', '|', '"'}

@dataclass
class ScanContext:
    """
    Estado de control durante el recorrido recursivo para garantizar integridad.
    """
    sandbox_base: Path
    kernel32: Optional[ctypes.WinDLL]
    is_junction: JunctionChecker
    visited_files: Set[tuple[int, int]]
    visited_dirs: VisitedDirs
    never_touch_lower: frozenset[str]

def safe_path_operation(default: Any) -> Callable[[Callable], Callable]:
    """
    Decorador: encapsula operaciones de E/S en bloques try-except para absorber 
    errores de acceso (Access Denied/In Use) comunes en escaneos de disco.
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
    """Métrica de salida: bytes acumulados y flag de éxito en el escaneo."""
    bytes_found: int
    success: bool

class FileAttributes(NamedTuple):
    """Constantes de máscara de bits de la API Win32 para metadatos de archivos."""
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
    """Valida que una ruta relativa no contenga secuencias de escape ('..') o caracteres ilegales."""
    if not p or not isinstance(p, str): return False
    if any(ord(c) < 32 for c in p): return False
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
    """Representación normalizada de una unidad de caché detectada para su reporte."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Convierte bytes a MB con truncamiento para consistencia en la interfaz."""
        return round(self.size_bytes / BYTES_TO_MB, 2)

@safe_path_operation(None)
def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """Carga dinámica de la API de Windows para inspección avanzada de atributos."""
    if os.name != 'nt': return None
    dll = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    return dll if hasattr(dll, 'GetFileAttributesW') else None

def _is_unc_path(path_str: Optional[str]) -> TypeGuard[str]:
    """Valida si una cadena de ruta corresponde a una ruta UNC (red)."""
    return isinstance(path_str, str) and (path_str.startswith(r"\\") or path_str.startswith("//"))

def _is_within_sandbox(target: Path, base: Path) -> bool:
    """Verifica mediante resolución de ruta absoluta que el destino esté bajo el sandbox."""
    try:
        t_res = target.resolve(strict=False)
        b_res = base.resolve(strict=True)
        return os.path.normcase(str(t_res)).startswith(os.path.normcase(str(b_res)))
    except (OSError, RuntimeError):
        return False

def base_directories() -> List[Path]:
    """Retorna los puntos de inicio de escaneo autorizados dentro de LOCALAPPDATA."""
    local_env = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env.strip() or _is_unc_path(local_env):
        return []
    
    p = Path(local_env)
    try:
        if p.exists() and p.is_dir() and os.access(p, os.R_OK):
            path_local = p.resolve(strict=True)
            if is_safe_to_modify(path_local) and not is_protected_path(path_local):
                return [path_local]
    except (OSError, RuntimeError):
        return []
    return []

@safe_path_operation(False)
def _is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """Consulta la API de Win32 para verificar atributos ocultos o de sistema."""
    if kernel32 is None: return False
    attrs = kernel32.GetFileAttributesW(entry_path)
    return attrs != 0xFFFFFFFF and bool(attrs & SYSTEM_HIDDEN_FLAGS)

def _should_skip_entry(entry: os.DirEntry, ctx: ScanContext) -> bool:
    """
    Evalúa si una entrada del sistema debe omitirse para evitar daños o recursión infinita.
    Aplica filtros contra: carpetas sensibles (NEVER_TOUCH), longitud de ruta, 
    caracteres ilegales, enlaces simbólicos y puntos de reparse (Junctions).
    """
    if entry.name.lower() in ctx.never_touch_lower: return True
    path_str = entry.path
    if len(path_str) >= MAX_PATH_LEN or any(c in path_str for c in PATH_FORBIDDEN_CHARS): return True
    
    try:
        if entry.is_symlink() or ctx.is_junction(path_str) or _is_system_hidden(path_str, ctx.kernel32): return True
    except OSError: return True
    return False

def _sum_directory_recursive(root_path: str, ctx: ScanContext, depth: int = 0) -> ScanResult:
    """
    Realiza un recorrido recursivo (DFS) sobre el sistema de archivos.
    
    Args:
        root_path: Ruta raíz del sub-árbol a analizar.
        ctx: Contexto de escaneo con set de archivos ya visitados (evita doble conteo en hardlinks).
        depth: Profundidad actual (limitada por MAX_SCAN_DEPTH para evitar stack overflow).
        
    Returns:
        ScanResult con la suma total de bytes y éxito de la operación.
    """
    if depth > MAX_SCAN_DEPTH or not root_path: return ScanResult(0, True)
    
    norm_path = os.path.normcase(root_path)
    if norm_path in ctx.visited_dirs: return ScanResult(0, True)
    ctx.visited_dirs.add(norm_path)
    
    total_bytes = 0
    try:
        with os.scandir(root_path) as it:
            for entry in it:
                if _should_skip_entry(entry, ctx): continue
                
                try:
                    if entry.is_dir(follow_symlinks=False):
                        res = _sum_directory_recursive(entry.path, ctx, depth + 1)
                        total_bytes += res.bytes_found
                    else:
                        st = entry.stat(follow_symlinks=False)
                        if st.st_size > 0:
                            file_id = (st.st_dev, st.st_ino)
                            if file_id not in ctx.visited_files:
                                ctx.visited_files.add(file_id)
                                total_bytes += st.st_size
                except (OSError, PermissionError): continue
        return ScanResult(total_bytes, True)
    except (OSError, PermissionError): return ScanResult(0, False)

@safe_path_operation(0)
def directory_size(path: Optional[OSPath]) -> int:
    """Calcula el peso total de una ruta, validando seguridad antes del escaneo."""
    if not path or not isinstance(path, (str, Path)): return 0
    path_obj = Path(path)
    if not path_obj.exists() or not path_obj.is_dir(): return 0
    
    resolved = path_obj.resolve(strict=True)
    if not is_safe_to_modify(resolved) or is_protected_path(resolved): return 0
    
    res_str = str(resolved)
    ctx = ScanContext(Path(res_str), _get_kernel32(), _IS_JUNCTION_FN, set(), set(), NEVER_TOUCH)
    return _sum_directory_recursive(res_str, ctx, 0).bytes_found

@safe_path_operation(False)
def _is_valid_cache_path(candidate: Path, base: Path) -> bool:
    """Valida la integridad de una ruta antes de considerarla un caché apto para análisis."""
    if not candidate.exists() or not candidate.is_dir() or not os.access(candidate, os.R_OK): return False
    
    try:
        real = candidate.resolve(strict=True)
        if not _is_within_sandbox(real, base) or not os.access(real, os.W_OK): return False
        return not (candidate.is_symlink() or _IS_JUNCTION_FN(str(real)) or candidate.name.lower() in NEVER_TOUCH)
    except (OSError, RuntimeError): return False

@safe_path_operation(Path())
def _resolve_browser_path(real_base: Path, rel_str: str) -> Path:
    """Resuelve la ruta completa del caché a partir de una base normalizada usando partes de ruta."""
    if not rel_str or not _is_valid_rel_path(rel_str): return Path()
    target = real_base.joinpath(*rel_str.split("\\"))
    if not target.exists() or not target.is_dir(): return Path()
    return target

def detect_profiles(bases: Optional[Sequence[Path]] = None, cache_paths: Optional[BrowserMap] = None) -> List[BrowserCache]:
    """Escaneo coordinado de cachés, filtrando por rutas base autorizadas y heurísticas de seguridad."""
    if bases is not None and not isinstance(bases, (list, tuple)): return []
    raw_bases = list(bases) if bases is not None else base_directories()
    
    browser_map = cache_paths if isinstance(cache_paths, dict) else BROWSER_CACHE_PATHS
    found: List[BrowserCache] = []
    kernel = _get_kernel32()
    
    for base in raw_bases:
        if not isinstance(base, Path) or not base.exists(): continue
        try:
            real_base = base.resolve(strict=True)
            ctx = ScanContext(real_base, kernel, _IS_JUNCTION_FN, set(), set(), NEVER_TOUCH)
            
            for browser_key, relative_path in browser_map.items():
                candidate = _resolve_browser_path(real_base, relative_path)
                if candidate != Path() and _is_valid_cache_path(candidate, ctx.sandbox_base):
                    scan_res = _sum_directory_recursive(str(candidate), ctx, 0)
                    if scan_res.success and scan_res.bytes_found > 0:
                        found.append(BrowserCache(str(browser_key), candidate, scan_res.bytes_found))
        except (OSError, RuntimeError): continue
    
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found

def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    """Retorna el peso total acumulado de un conjunto de cachés."""
    return sum(c.size_bytes for c in caches) if isinstance(caches, (list, tuple, set)) else 0

def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Formatea los resultados del análisis de caché en un reporte legible para la UI."""
    current_caches = caches if isinstance(caches, list) else detect_profiles()
    if not current_caches: return ["No se detectaron cachés de navegador en este sistema."]
    total_mb = round(total_cache_bytes(current_caches) / BYTES_TO_MB, 2)
    lines = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
