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
    Union, TypeAlias, NamedTuple, Set, TypeGuard
)

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

JunctionChecker: TypeAlias = Callable[[str], bool]
BrowserMap: TypeAlias = Dict[str, str]
OSPath: TypeAlias = Union[str, Path]

class ScanResult(NamedTuple):
    """Resultado de la operación de conteo de bytes y su estado de éxito."""
    bytes_found: int
    success: bool

class FileAttributes(NamedTuple):
    """Máscaras de bits para atributos extendidos de Win32 (kernel32.GetFileAttributesW)."""
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
    """Función de fallback para chequeo de junctions si el runtime es anterior a Python 3.12."""
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
BYTES_TO_MB: float = 1024 * 1024

@dataclass
class BrowserCache:
    """Almacena la ubicación y el tamaño calculado de una caché de navegador."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Convierte el tamaño en bytes a Megabytes con redondeo estándar."""
        return round(self.size_bytes / BYTES_TO_MB, 2)


def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """
    Instancia el módulo kernel32 de Windows.
    Retorna None si no es Windows o si la API no está disponible,
    evitando bloqueos en entornos Linux/CI.
    """
    if os.name != 'nt':
        return None
    try:
        dll: ctypes.WinDLL = ctypes.WinDLL('kernel32.dll', use_last_error=True)
        return dll if hasattr(dll, 'GetFileAttributesW') else None
    except (OSError, AttributeError, TypeError):
        return None

def _is_unc_path(path_str: Optional[str]) -> TypeGuard[str]:
    """Verifica si la ruta corresponde a un recurso de red (UNC) no local."""
    return isinstance(path_str, str) and (path_str.startswith(r"\\") or path_str.startswith("//"))

def _ensure_within_base(target: str, base_norm: str) -> bool:
    """
    Validación de contención: comprueba que 'target' pertenezca a la jerarquía 
    'base_norm'. Previene saltos fuera del perfil de usuario mediante normalización.
    """
    try:
        target_norm: str = os.path.normcase(os.path.abspath(target))
        return target_norm.startswith(base_norm)
    except Exception:
        return False

def base_directories() -> List[Path]:
    """Identifica la ruta de LOCALAPPDATA y valida su integridad y seguridad."""
    local_env: Optional[str] = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env or _is_unc_path(local_env):
        return []
    
    try:
        p = Path(local_env)
        if p.exists() and p.is_dir():
            path_local: Path = p.resolve(strict=True)
            if is_safe_to_modify(path_local) and not is_protected_path(path_local):
                return [path_local]
    except (OSError, RuntimeError, PermissionError):
        pass
    return []

def _is_excluded_file(name: Optional[str]) -> TypeGuard[str]:
    """Filtro de listas negras para archivos críticos del navegador (ej. cookies)."""
    return name is not None and name.lower() in NEVER_TOUCH

def _is_system_hidden(entry_path: Optional[str], kernel32: Optional[ctypes.WinDLL]) -> bool:
    """
    Consulta atributos Win32 para identificar archivos marcados como ocultos, 
    de sistema o puntos de reparse (Junctions).
    """
    if kernel32 is None or not entry_path: return False
    try:
        attrs: int = kernel32.GetFileAttributesW(entry_path)
        return bool(attrs != 0xFFFFFFFF and (attrs & SYSTEM_HIDDEN_FLAGS))
    except (ctypes.ArgumentError, OSError, TypeError, Exception):
        return False

def _should_skip_entry(
    entry: os.DirEntry, 
    kernel32: Optional[ctypes.WinDLL], 
    is_junction_fn: JunctionChecker
) -> bool:
    """
    Evalúa si un DirEntry debe omitirse.
    Aplica filtros de seguridad: blacklist, rutas UNC, profundidad de caracteres,
    protección de rutas del sistema y atributos de bajo nivel (Win32).
    """
    if entry.name is None or _is_excluded_file(entry.name):
        return True
    
    if _is_unc_path(entry.path) or len(entry.path) >= MAX_PATH_LEN:
        return True
    
    try:
        path_p = Path(entry.path)
        if is_protected_path(path_p):
            return True
        if entry.is_symlink() or is_junction_fn(entry.path) or _is_system_hidden(entry.path, kernel32):
            return True
    except (OSError, AttributeError, TypeError):
        return True
        
    return False

def _is_file_in_use(path_obj: Path) -> bool:
    """
    Verifica si un archivo está bloqueado por el sistema o una aplicación.
    Intenta abrir en modo lectura exclusiva para determinar disponibilidad.
    """
    if not is_safe_to_modify(path_obj) or is_protected_path(path_obj):
        return True
    try:
        with open(path_obj, 'rb'):
            return False
    except (OSError, PermissionError):
        return True

def _sum_directory_recursive(
    root_path: Path, 
    root_abs_norm: str,
    kernel32: Optional[ctypes.WinDLL],
    visited_inodes: Set[int],
    visited_dirs: Dict[str, int],
    depth: int = 0
) -> ScanResult:
    """
    Ejecuta un recorrido recursivo con memoización para calcular el tamaño.
    Evita procesar dos veces el mismo inodo o directorio normalizado.
    """
    path_norm = os.path.normcase(str(root_path))
    if path_norm in visited_dirs:
        return ScanResult(visited_dirs[path_norm], True)

    total_bytes: int = 0
    try:
        with os.scandir(root_path) as it:
            for entry in it:
                if depth > MAX_SCAN_DEPTH or _should_skip_entry(entry, kernel32, _IS_JUNCTION_FN):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                    if st.st_ino in visited_inodes:
                        continue
                    visited_inodes.add(st.st_ino)

                    if entry.is_dir(follow_symlinks=False):
                        if not _ensure_within_base(entry.path, root_abs_norm):
                            continue
                        res = _sum_directory_recursive(Path(entry.path), root_abs_norm, kernel32, visited_inodes, visited_dirs, depth + 1)
                        total_bytes += res.bytes_found
                    elif not _is_file_in_use(Path(entry.path)):
                        total_bytes += st.st_size
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        return ScanResult(total_bytes, False)
        
    visited_dirs[path_norm] = total_bytes
    return ScanResult(total_bytes, True)

def directory_size(path: Optional[OSPath]) -> int:
    """Interfaz pública para calcular el tamaño total de un directorio de caché."""
    if path is None: return 0
    try:
        path_obj: Path = Path(path)
        if not path_obj.exists(): return 0
        resolved_p: Path = path_obj.resolve(strict=True)
        if not resolved_p.is_dir() or not is_safe_to_modify(resolved_p) or is_protected_path(resolved_p):
            return 0
        norm_root: str = os.path.normcase(str(resolved_p))
        return _sum_directory_recursive(resolved_p, norm_root, _get_kernel32(), set(), {}, 0).bytes_found
    except (OSError, RuntimeError, PermissionError):
        return 0

def _is_valid_cache_path(candidate: Path, base_abs_str: str) -> bool:
    """Valida la integridad de la ruta candidata antes de iniciar el escaneo profundo."""
    try:
        if not candidate.exists():
            return False
        real: Path = candidate.resolve(strict=True)
        if not real.is_dir() or not _ensure_within_base(str(real), os.path.normcase(base_abs_str)):
            return False
        if not is_safe_to_modify(real) or is_protected_path(real):
            return False
        return not (real.is_symlink() or _IS_JUNCTION_FN(str(real)) or _is_excluded_file(real.name))
    except (OSError, RuntimeError):
        return False

def _resolve_browser_path(real_base: Path, rel_str: str) -> Path:
    """Resuelve rutas absolutas a partir de la estructura predefinida de cada navegador, validando contención."""
    try:
        parts = rel_str.split("\\")
        target: Path = real_base.joinpath(*parts)
        if target.exists():
            target_res = target.resolve(strict=True)
            if _ensure_within_base(str(target_res), os.path.normcase(str(real_base))) and \
               is_safe_to_modify(target_res) and not is_protected_path(target_res):
                return target_res
    except (OSError, RuntimeError):
        pass
    return Path()

def detect_profiles(bases: Optional[Sequence[Path]] = None, cache_paths: Optional[BrowserMap] = None) -> List[BrowserCache]:
    """
    Pipeline principal de detección: mapea navegadores, resuelve rutas y
    ejecuta el escaneo del tamaño de caché.
    """
    if bases is not None and not isinstance(bases, (list, tuple)):
        return []
        
    raw_bases: List[Path] = list(bases) if bases is not None else base_directories()
    browser_map: BrowserMap = cache_paths if isinstance(cache_paths, dict) else BROWSER_CACHE_PATHS
    k32: Optional[ctypes.WinDLL] = _get_kernel32()
    found: List[BrowserCache] = []
    global_visited_inodes: Set[int] = set()
    global_visited_dirs: Dict[str, int] = {}
    
    for base in raw_bases:
        if not isinstance(base, Path) or not base.exists(): continue
        try:
            real_base: Path = base.resolve(strict=True)
            real_base_str: str = str(real_base)
            for browser_name, rel_str in browser_map.items():
                candidate: Path = _resolve_browser_path(real_base, rel_str)
                if candidate != Path() and _is_valid_cache_path(candidate, real_base_str):
                    scan_res: ScanResult = _sum_directory_recursive(
                        candidate, os.path.normcase(str(candidate)), k32, global_visited_inodes, global_visited_dirs, 0
                    )
                    if scan_res.bytes_found > 0:
                        found.append(BrowserCache(str(browser_name), candidate, scan_res.bytes_found))
        except (OSError, RuntimeError):
            continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found

def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    """Suma acumulada de bytes de una lista de objetos BrowserCache."""
    return sum(c.size_bytes for c in caches) if caches else 0

def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Genera un reporte textual formateado para la interfaz de usuario."""
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
