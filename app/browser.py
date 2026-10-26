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
    Estado inmutable compartido durante el recorrido del árbol de directorios.
    
    Attributes:
        sandbox_base_norm: Ruta normalizada raíz para evitar fugas de directorio.
        kernel32: Instancia de la API de Windows para consultas de bajo nivel.
        is_junction: Función delegada para detectar puntos de reparse (Junctions).
        visited_files: Set de tuplas (dev, ino) para evitar conteo doble (hardlinks).
        visited_dirs: Set de rutas normalizadas ya visitadas para evitar ciclos.
        never_touch_lower: Conjunto de nombres de archivo/carpeta prohibidos.
        max_depth: Profundidad máxima de recursión permitida.
    """
    sandbox_base_norm: str
    kernel32: Optional[ctypes.WinDLL]
    is_junction: JunctionChecker
    visited_files: Set[tuple[int, int]]
    visited_dirs: VisitedDirs
    never_touch_lower: frozenset[str]
    max_depth: int = 20

def safe_path_operation(default: Any) -> Callable[[Callable], Callable]:
    """
    Decorador para envolver operaciones de disco en bloques try-except.
    Garantiza que errores transitorios de I/O no detengan el escaneo.
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
    """Resultado del cálculo de tamaño recursivo: total en bytes y éxito de operación."""
    bytes_found: int
    success: bool

class FileAttributes:
    """Namespace inmutable para flags de atributos de archivo (Windows API)."""
    READONLY = 0x01
    HIDDEN = 0x02
    SYSTEM = 0x04
    REPARSE_POINT = 0x400

SYSTEM_HIDDEN_FLAGS: int = (
    FileAttributes.HIDDEN | 
    FileAttributes.SYSTEM | 
    FileAttributes.REPARSE_POINT
)

def _is_junction_default(path: str) -> bool:
    """Fallback por defecto si os.path.isjunction no está disponible (Linux/OSX)."""
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

def _is_valid_rel_path(path_candidate: Any) -> TypeGuard[str]:
    """Valida la integridad de una ruta relativa contra inyecciones y caracteres inválidos."""
    if not isinstance(path_candidate, str) or not path_candidate.strip(): return False
    if os.path.isabs(path_candidate) or path_candidate.startswith(('\\', '/')): return False
    if ".." in os.path.normpath(path_candidate): return False
    if any(c in path_candidate for c in PATH_FORBIDDEN_CHARS): return False
    if any(ord(c) < 32 for c in path_candidate): return False
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
BYTES_TO_MB: float = 1024 * 1024

@dataclass
class BrowserCache:
    """Datos calculados de un caché de navegador detectado."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Tamaño convertido a MB para visualización en UI."""
        return round(self.size_bytes / BYTES_TO_MB, 2)

@safe_path_operation(None)
def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """Carga de forma segura el módulo kernel32 para inspección de atributos."""
    if os.name != 'nt': return None
    dll = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    return dll if hasattr(dll, 'GetFileAttributesW') else None

def _is_unc_path(path_str: Optional[str]) -> TypeGuard[str]:
    """Detecta rutas de red (UNC) que no deben procesarse."""
    return isinstance(path_str, str) and (path_str.startswith(r"\\") or path_str.startswith("//"))

def _is_within_sandbox(path_norm: str, base_norm: str) -> bool:
    """Valida que la ruta sea descendiente del directorio raíz permitido."""
    return path_norm.startswith(base_norm)

def base_directories() -> List[Path]:
    """Determina las carpetas locales de usuario autorizadas para el escaneo."""
    local_env = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env.strip() or _is_unc_path(local_env):
        return []
    
    try:
        p = Path(local_env).resolve()
        if p.exists() and p.is_dir() and os.access(p, os.R_OK):
            if is_safe_to_modify(p) and not is_protected_path(p):
                return [p]
    except (OSError, RuntimeError):
        pass
    return []

@safe_path_operation(False)
def _is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """Consulta atributos de sistema de Windows para identificar carpetas críticas."""
    if kernel32 is None: return False
    attrs = kernel32.GetFileAttributesW(entry_path)
    return attrs != 0xFFFFFFFF and bool(attrs & SYSTEM_HIDDEN_FLAGS)

def _should_skip_entry(entry: os.DirEntry, scan_ctx: ScanContext, norm_path: str) -> bool:
    """
    Evalúa mediante heurísticas de seguridad si una entrada de disco debe ser ignorada.
    """
    if entry.name.lower() in scan_ctx.never_touch_lower: return True
    if not _is_within_sandbox(norm_path, scan_ctx.sandbox_base_norm): return True
    if is_protected_path(Path(entry.path)): return True
    
    try:
        if entry.is_symlink() or scan_ctx.is_junction(entry.path) or _is_system_hidden(entry.path, scan_ctx.kernel32): 
            return True
    except OSError: return True
    return False

def _process_file_in_dir(entry: os.DirEntry, scan_ctx: ScanContext) -> int:
    """Calcula el tamaño de un archivo individual evitando conteos múltiples por hardlinks."""
    try:
        st = entry.stat(follow_symlinks=False)
        if st.st_size > 0:
            file_id = (st.st_dev, st.st_ino)
            if file_id not in scan_ctx.visited_files:
                scan_ctx.visited_files.add(file_id)
                return st.st_size
    except (OSError, PermissionError): pass
    return 0

def _sum_directory_recursive(root_path: str, scan_ctx: ScanContext, depth: int = 0) -> ScanResult:
    """
    Calcula el tamaño acumulado de una jerarquía de directorios de forma recursiva.
    """
    if not root_path or depth > scan_ctx.max_depth:
        return ScanResult(0, True)
    
    try:
        root_norm = os.path.normcase(os.path.abspath(root_path))
    except (OSError, ValueError):
        return ScanResult(0, False)
    
    if not _is_within_sandbox(root_norm, scan_ctx.sandbox_base_norm) or root_norm in scan_ctx.visited_dirs:
        return ScanResult(0, True)

    scan_ctx.visited_dirs.add(root_norm)
    total_bytes = 0
    
    try:
        with os.scandir(root_path) as iterator:
            for entry in iterator:
                try:
                    entry_norm = os.path.normcase(entry.path)
                    if _should_skip_entry(entry, scan_ctx, entry_norm): continue
                    
                    if entry.is_dir(follow_symlinks=False):
                        subdir_result = _sum_directory_recursive(entry.path, scan_ctx, depth + 1)
                        total_bytes += subdir_result.bytes_found
                    elif entry.is_file(follow_symlinks=False):
                        total_bytes += _process_file_in_dir(entry, scan_ctx)
                except (OSError, PermissionError):
                    continue
        return ScanResult(total_bytes, True)
    except (OSError, PermissionError): return ScanResult(0, False)

@safe_path_operation(0)
def directory_size(path: Optional[OSPath]) -> int:
    """Interfaz pública para calcular el peso de una carpeta de caché con validación estricta."""
    if not isinstance(path, (str, Path)) or not path: return 0
    path_obj = Path(path)
    if not path_obj.exists() or not path_obj.is_dir(): return 0
    
    if not is_safe_to_modify(path_obj) or is_protected_path(path_obj): return 0
    res_str = os.path.abspath(str(path_obj))
    scan_ctx = ScanContext(os.path.normcase(res_str), _get_kernel32(), _IS_JUNCTION_FN, set(), set(), NEVER_TOUCH)
    return _sum_directory_recursive(res_str, scan_ctx, 0).bytes_found

@safe_path_operation(False)
def _is_valid_cache_path(candidate: Path, base_norm: str) -> bool:
    """Verifica si una ruta es apta para ser escaneada como caché de navegador."""
    if not isinstance(candidate, Path) or not candidate.exists() or not candidate.is_dir(): return False
    kernel = _get_kernel32()
    
    cand_abs = os.path.abspath(str(candidate))
    cand_norm = os.path.normcase(cand_abs)
    
    if not is_safe_to_modify(candidate) or not _is_within_sandbox(cand_norm, base_norm): return False
    if _is_system_hidden(cand_abs, kernel): return False
    
    return not (candidate.is_symlink() or _IS_JUNCTION_FN(cand_abs) or is_protected_path(candidate) or candidate.name.lower() in NEVER_TOUCH)

def _resolve_browser_path(base_resolved: Path, rel_str: str) -> Path:
    """Combina el directorio base del usuario ya resuelto con la ruta relativa del navegador."""
    if not _is_valid_rel_path(rel_str): return Path()
    try:
        parts = rel_str.split("\\")
        target = base_resolved.joinpath(*parts)
        if target.exists() and target.is_dir():
            target_norm = os.path.normcase(str(target.resolve()))
            base_norm = os.path.normcase(str(base_resolved))
            # Verificamos si la ruta resuelta está confinada dentro del sandbox original
            if _is_within_sandbox(target_norm, base_norm) and is_safe_to_modify(target):
                return target
        return Path()
    except (OSError, RuntimeError):
        return Path()

def detect_profiles(bases: Optional[Sequence[Path]] = None, cache_paths: Optional[BrowserMap] = None) -> List[BrowserCache]:
    """
    Escanea las rutas base proporcionadas en busca de perfiles de navegador conocidos.
    Devuelve una lista de objetos BrowserCache ordenados de mayor a menor tamaño ocupado.
    """
    if bases is not None and not isinstance(bases, (list, tuple)): return []
    raw_bases = list(bases) if bases else base_directories()
    browser_map = cache_paths if isinstance(cache_paths, dict) else BROWSER_CACHE_PATHS
    
    if not raw_bases or not browser_map: return []
        
    found: List[BrowserCache] = []
    kernel = _get_kernel32()
    
    for base in raw_bases:
        if not isinstance(base, Path) or not base.exists() or not os.access(base, os.R_OK): continue
        try:
            base_resolved = base.resolve()
            base_norm = os.path.normcase(str(base_resolved))
        except (OSError, RuntimeError): continue
        
        scan_ctx = ScanContext(base_norm, kernel, _IS_JUNCTION_FN, set(), set(), NEVER_TOUCH)
        
        for browser_key, relative_path in browser_map.items():
            candidate = _resolve_browser_path(base_resolved, relative_path)
            if candidate != Path() and _is_valid_cache_path(candidate, base_norm):
                scan_res = _sum_directory_recursive(str(candidate), scan_ctx, 0)
                if scan_res.success and scan_res.bytes_found > 0:
                    found.append(BrowserCache(str(browser_key), candidate, scan_res.bytes_found))
    
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found

def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    """Calcula la suma total en bytes de los hallazgos de caché procesados."""
    if not isinstance(caches, (list, tuple, set)): return 0
    return sum(c.size_bytes for c in caches if isinstance(c, BrowserCache))

def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Formatea los resultados del escaneo para su visualización en la interfaz."""
    current_caches = caches if isinstance(caches, list) else detect_profiles()
    if not current_caches: return ["No se detectaron cachés de navegador en este sistema."]
    
    total_bytes = total_cache_bytes(current_caches)
    total_mb = round(total_bytes / BYTES_TO_MB, 2)
    lines = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        if isinstance(cache, BrowserCache):
            lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
            lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
