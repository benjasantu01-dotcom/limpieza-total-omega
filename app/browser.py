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
    Contenedor de estado compartido durante el recorrido recursivo del árbol de archivos.
    
    Attributes:
        sandbox_base_norm: Ruta normalizada que actúa como raíz del perfil de usuario
                           para evitar escapes de directorio.
        kernel32: Handle a la librería de sistema para verificar atributos de archivos
                  ocultos o del sistema (solo Windows).
        is_junction: Función inyectada para detectar puntos de reparse (Junctions/Symlinks).
        visited_files: Set de tuplas (dev, ino) para evitar contar hardlinks duplicados.
        visited_dirs: Set de rutas absolutas ya procesadas para detectar ciclos.
    """
    sandbox_base_norm: str
    kernel32: Optional[ctypes.WinDLL]
    is_junction: JunctionChecker
    visited_files: Set[tuple[int, int]]
    visited_dirs: VisitedDirs

def safe_path_operation(default: Any) -> Callable[[Callable], Callable]:
    """
    Decorador: intercepta excepciones de E/S comunes en Windows (como archivos 
    bloqueados por el sistema) para garantizar que el proceso de escaneo 
    no se interrumpa prematuramente.
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
    """Resultado del conteo de bytes y estatus de éxito de una rama del sistema de archivos."""
    bytes_found: int
    success: bool

class FileAttributes(NamedTuple):
    """Máscaras de bits de la API de Win32 para identificar atributos críticos."""
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
    """
    Valida que una ruta relativa esté libre de inyecciones de directorio ('..')
    y cumpla con las restricciones de caracteres del sistema operativo.
    """
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
    """Modelo de datos que representa una ubicación de caché descubierta."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Retorna el peso del caché convertido a Megabytes (formateado)."""
        return round(self.size_bytes / BYTES_TO_MB, 2)

@safe_path_operation(None)
def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """Accede a kernel32.dll para ejecutar verificaciones de bajo nivel (atributos de archivo)."""
    if os.name != 'nt': return None
    dll = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    return dll if hasattr(dll, 'GetFileAttributesW') else None

def _is_unc_path(path_str: Optional[str]) -> TypeGuard[str]:
    """Valida si la ruta es una ubicación de red (UNC), restringiendo su procesamiento."""
    return isinstance(path_str, str) and (path_str.startswith(r"\\") or path_str.startswith("//"))

def _is_within_sandbox(target_norm: str, base_norm: str) -> bool:
    """Asegura que una ruta escaneada pertenezca al directorio base del perfil del usuario."""
    return target_norm.startswith(base_norm)

def base_directories() -> List[Path]:
    """Retorna las rutas base autorizadas (LOCALAPPDATA) para realizar el escaneo."""
    local_env = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env.strip() or _is_unc_path(local_env):
        return []
    
    p = Path(local_env)
    try:
        if p.exists() and p.is_dir():
            path_local = p.resolve(strict=True)
            if is_safe_to_modify(path_local) and not is_protected_path(path_local):
                return [path_local]
    except (OSError, RuntimeError):
        return []
    return []

def _is_excluded_file(name: str) -> bool:
    """Verifica si el nombre de archivo está en la lista de archivos protegidos (NEVER_TOUCH)."""
    return name.lower() in NEVER_TOUCH

@safe_path_operation(False)
def _is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """Verifica mediante Win32 API si un archivo posee atributos de sistema o está oculto."""
    if kernel32 is None: return False
    attrs = kernel32.GetFileAttributesW(entry_path)
    return attrs != 0xFFFFFFFF and bool(attrs & SYSTEM_HIDDEN_FLAGS)

def _should_skip_entry(entry: os.DirEntry, ctx: ScanContext) -> bool:
    """Filtro de seguridad: descarta entradas si violan las políticas de archivos o seguridad."""
    if _is_excluded_file(entry.name): return True
    path_str = entry.path
    if len(path_str) >= MAX_PATH_LEN or any(c in path_str for c in PATH_FORBIDDEN_CHARS) or any(ord(c) < 32 for c in path_str): return True
    
    try:
        if entry.is_symlink() or ctx.is_junction(path_str) or _is_system_hidden(path_str, ctx.kernel32): return True
    except OSError: return True
    return False

def _sum_directory_recursive(root_path: str, ctx: ScanContext, depth: int = 0) -> ScanResult:
    """Algoritmo de escaneo recursivo optimizado."""
    if depth > MAX_SCAN_DEPTH or len(root_path) >= MAX_PATH_LEN: 
        return ScanResult(0, True)
    
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
    """Cálculo del tamaño en bytes para una ruta dada, aplicando contextos de seguridad."""
    if not path or not isinstance(path, (str, Path)): return 0
    path_obj = Path(path)
    if not path_obj.exists() or not path_obj.is_dir(): return 0
    
    resolved_path = str(path_obj.resolve(strict=True))
    if not is_safe_to_modify(Path(resolved_path)): return 0
    
    ctx = ScanContext(os.path.normpath(resolved_path), _get_kernel32(), _IS_JUNCTION_FN, set(), set())
    return _sum_directory_recursive(resolved_path, ctx, 0).bytes_found

@safe_path_operation(False)
def _is_valid_cache_path(candidate: Path, base_abs_str: str) -> bool:
    """Valida la integridad de un directorio de caché como candidato para escaneo."""
    if not candidate.exists() or not candidate.is_dir() or not os.access(candidate, os.R_OK): return False
    
    try:
        real = str(candidate.resolve(strict=True))
        if any(ord(c) < 32 for c in real): return False
        if not _is_within_sandbox(os.path.normpath(real), base_abs_str): return False
        if not is_safe_to_modify(Path(real)) or is_protected_path(Path(real)): return False
        return not (candidate.is_symlink() or _IS_JUNCTION_FN(real) or _is_excluded_file(candidate.name))
    except (OSError, RuntimeError): return False

@safe_path_operation(Path())
def _resolve_browser_path(real_base: Path, rel_str: str) -> Path:
    """Concatena de forma segura la base del sistema con la ruta relativa de un navegador."""
    if not rel_str or not _is_valid_rel_path(rel_str): return Path()
    target = real_base.joinpath(*rel_str.split("\\"))
    if not target.exists() or not target.is_dir(): return Path()
    return target

def detect_profiles(bases: Optional[Sequence[Path]] = None, cache_paths: Optional[BrowserMap] = None) -> List[BrowserCache]:
    """Identifica y lista todos los directorios de caché de navegadores instalados."""
    raw_bases = list(bases) if bases is not None else base_directories()
    if not raw_bases: return []
    
    browser_map = cache_paths if isinstance(cache_paths, dict) else BROWSER_CACHE_PATHS
    found: List[BrowserCache] = []
    kernel = _get_kernel32()
    
    for base in raw_bases:
        if not isinstance(base, Path) or not base.exists(): continue
        try:
            real_base = base.resolve(strict=True)
            ctx = ScanContext(os.path.normpath(str(real_base)), kernel, _IS_JUNCTION_FN, set(), set())
            
            for browser_key, relative_path in browser_map.items():
                candidate = _resolve_browser_path(real_base, relative_path)
                if candidate != Path() and _is_valid_cache_path(candidate, ctx.sandbox_base_norm):
                    scan_res = _sum_directory_recursive(str(candidate), ctx, 0)
                    if scan_res.bytes_found > 0:
                        found.append(BrowserCache(str(browser_key), candidate, scan_res.bytes_found))
        except (OSError, RuntimeError): continue
    
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found

def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    """Calcula el peso total de todos los cachés detectados."""
    return sum(c.size_bytes for c in caches) if caches else 0

def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Genera un reporte legible de los hallazgos para su despliegue en la interfaz gráfica."""
    current_caches = caches if isinstance(caches, list) else detect_profiles()
    if not current_caches: return ["No se detectaron cachés de navegador en este sistema."]
    total_mb = round(total_cache_bytes(current_caches) / BYTES_TO_MB, 2)
    lines = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
