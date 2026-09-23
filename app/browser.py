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

# Máscara combinada para identificar archivos que el sistema considera protegidos o de infraestructura.
# Se utiliza para omitir directorios que no deben ser recorridos (ocultos, sistema o junctions).
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

# Carpetas y archivos excluidos explícitamente para garantizar la integridad
# de los datos del usuario (sesiones, contraseñas, configuraciones).
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
    """
    Representación de un nodo de caché detectado.
    
    Attributes:
        browser: Nombre comercial del navegador.
        path: Ruta absoluta al sistema de archivos.
        size_bytes: Tamaño en bytes calculado recursivamente.
    """
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Calcula el tamaño en MB convirtiendo desde bytes."""
        return round(self.size_bytes / (1024 * 1024), 2)


def _get_kernel32() -> Optional[ctypes.WinDLL]:
    """
    Carga kernel32.dll para interactuar con atributos de archivos en Windows.
    Retorna None en plataformas no Windows o si el acceso es denegado.
    """
    if os.name != 'nt':
        return None
    try:
        dll = ctypes.WinDLL('kernel32.dll', use_last_error=True)
        if not hasattr(dll, 'GetFileAttributesW'):
            return None
        return dll
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return None

def _is_unc_path(path_str: Optional[str]) -> bool:
    """
    Valida si una cadena representa una ruta de red UNC (ej. \\servidor\recurso).
    Previene intentos de escaneo en recursos que podrían degradar el rendimiento.
    """
    if not isinstance(path_str, str) or not path_str:
        return False
    return path_str.startswith(r"\\") or path_str.startswith("//")

def base_directories() -> List[Path]:
    """
    Localiza la ruta %LOCALAPPDATA% y verifica su validez operativa.
    Asegura que el punto de inicio de escaneo sea una ruta local segura.
    """
    local_env = os.environ.get("LOCALAPPDATA")
    if not isinstance(local_env, str) or not local_env or _is_unc_path(local_env):
        return []
    
    try:
        p = Path(local_env)
        if not p.exists() or not p.is_dir():
            return []
        path_local = p.resolve(strict=True)
        if is_safe_to_modify(path_local) and not is_protected_path(path_local):
            return [path_local]
    except (OSError, RuntimeError, PermissionError):
        pass
    return []


def _is_path_inside_base(target_abs: str, base_abs: str) -> bool:
    """
    Confirma que 'target_abs' sea subdirectorio o hijo de 'base_abs'.
    Implementa un mecanismo de defensa contra Path Traversal.
    """
    if not isinstance(target_abs, str) or not isinstance(base_abs, str) or not target_abs or not base_abs:
        return False
    if len(target_abs) >= MAX_PATH_LEN or len(base_abs) >= MAX_PATH_LEN or any(c in target_abs for c in '\0\r\n'):
        return False
    try:
        common = os.path.commonpath([os.path.normpath(target_abs), os.path.normpath(base_abs)])
        return os.path.normpath(common) == os.path.normpath(base_abs)
    except (OSError, ValueError):
        return False


def _is_excluded_file(name: Optional[str]) -> bool:
    """Comprueba si un nombre de archivo corresponde a un elemento crítico protegido (NEVER_TOUCH)."""
    return name is not None and name.lower() in NEVER_TOUCH


def _is_system_hidden(entry_path: str, kernel32: Optional[ctypes.WinDLL]) -> bool:
    """
    Consulta atributos Win32 mediante bitmask para identificar si un archivo
    está marcado como oculto, de sistema o es un punto de reanálisis.
    """
    if kernel32 is None or not isinstance(entry_path, str) or not entry_path:
        return False
    try:
        kernel32.GetFileAttributesW.restype = ctypes.c_ulong
        attrs: int = kernel32.GetFileAttributesW(entry_path)
        # 0xFFFFFFFF indica error en la llamada Win32 API
        if attrs == 0xFFFFFFFF:
            return False 
        return bool(attrs & SYSTEM_HIDDEN_FLAGS)
    except (AttributeError, TypeError, ctypes.ArgumentError, OSError, ValueError):
        return False


def _should_skip_entry(
    entry: os.DirEntry, 
    kernel32: Optional[ctypes.WinDLL], 
    is_junction_fn: JunctionChecker
) -> bool:
    """
    Filtra entradas del sistema de archivos según políticas de seguridad.
    Omite junctions, rutas UNC, archivos protegidos y elementos de sistema.
    """
    if entry.name is None or _is_excluded_file(entry.name):
        return True
    
    try:
        path = entry.path
        if not path or len(path) >= MAX_PATH_LEN or any(c in path for c in '\0\r\n') or _is_unc_path(path):
            return True
        
        # Primero revisamos enlaces directos de Python, luego atributos de sistema Win32
        if entry.is_symlink() or is_junction_fn(path):
            return True
                
        if _is_system_hidden(path, kernel32):
            return True
    except (OSError, AttributeError):
        return True
        
    return False


def _is_safe_to_traverse(path_obj: Path, base_check_path: Optional[Path]) -> bool:
    """
    Valida permisos y contención lógica antes de recursar una jerarquía.
    Garantiza que la ruta sea local y esté dentro de los límites del perfil.
    """
    if not isinstance(path_obj, Path):
        return False
    try:
        if not path_obj.exists():
            return False
            
        # Verificación contra archivos de sistema/ocultos antes de procesar
        k32 = _get_kernel32()
        if k32 and _is_system_hidden(str(path_obj.absolute()), k32):
            return False
            
        p_res = path_obj.resolve(strict=True)
        if _is_unc_path(str(p_res)) or not p_res.is_dir() or not is_safe_to_modify(p_res) or is_protected_path(p_res):
            return False
        if base_check_path:
            try:
                base_res = base_check_path.resolve(strict=True)
                if not _is_path_inside_base(str(p_res), str(base_res)):
                    return False
            except (OSError, RuntimeError):
                return False
        return True
    except (OSError, RuntimeError, PermissionError, ValueError):
        return False


def _get_entry_size(entry: os.DirEntry) -> int:
    """Obtiene de forma segura el tamaño de un archivo individual."""
    try:
        # Pre-verificar si el archivo es accesible antes de acceder a stat
        if not os.access(entry.path, os.R_OK):
            return 0
        return int(entry.stat(follow_symlinks=False).st_size)
    except (OSError, PermissionError):
        return 0


def _sum_directory_recursive(
    root_abs: str, 
    is_junction_fn: JunctionChecker, 
    kernel32: Optional[ctypes.WinDLL],
    memo: Dict[str, int],
    depth: int = 0
) -> int:
    """
    Calcula recursivamente el peso de una carpeta, sumando el tamaño de archivos
    y resultados de subdirectorios, protegiendo contra errores de I/O mediante
    captura de excepciones localizadas y validación de seguridad.
    """
    if root_abs in memo:
        return memo[root_abs]
    if depth > MAX_SCAN_DEPTH:
        return 0

    total_bytes: int = 0
    try:
        with os.scandir(root_abs) as it:
            for entry in it:
                try:
                    # Verificación de seguridad defensiva
                    if not entry.path or len(entry.path) >= MAX_PATH_LEN or not is_safe_to_modify(Path(entry.path)):
                        continue
                    
                    if _should_skip_entry(entry, kernel32, is_junction_fn):
                        continue
                    
                    if entry.is_dir(follow_symlinks=False):
                        total_bytes += _sum_directory_recursive(
                            entry.path, is_junction_fn, kernel32, memo, depth + 1
                        )
                    elif entry.is_file(follow_symlinks=False):
                        total_bytes += _get_entry_size(entry)
                except (OSError, PermissionError, RuntimeError):
                    continue
        
        memo[root_abs] = total_bytes
        return total_bytes
    except (OSError, PermissionError, RuntimeError, ValueError):
        return 0


def directory_size(path: Optional[OSPath]) -> int:
    """
    Interfaz pública para medir el peso de una ruta; realiza validaciones
    de seguridad previas para prevenir accesos indebidos o recursión insegura.
    """
    if not path:
        return 0
    try:
        p = Path(path)
        if len(str(p)) >= MAX_PATH_LEN or not p.is_absolute() or not _is_safe_to_traverse(p, None):
            return 0
        resolved = str(p.resolve(strict=True))
        return _sum_directory_recursive(resolved, _IS_JUNCTION_FN, _get_kernel32(), {})
    except (OSError, RuntimeError, PermissionError, ValueError):
        return 0


def _is_valid_cache_path(candidate: Path, base_path: str, is_junction_fn: JunctionChecker) -> bool:
    """
    Verifica que la carpeta candidata sea un directorio válido dentro del scope,
    asegurando que no sea un enlace simbólico o una ruta protegida.
    """
    try:
        if not candidate.exists() or not candidate.is_dir():
            return False
        real_candidate = str(candidate.resolve(strict=True))
        if _is_unc_path(real_candidate) or not _is_path_inside_base(real_candidate, base_path):
            return False
        if not is_safe_to_modify(candidate) or is_protected_path(candidate):
            return False
        return not (candidate.is_symlink() or is_junction_fn(str(candidate)) or _is_excluded_file(candidate.name))
    except (OSError, PermissionError, RuntimeError, ValueError):
        return False


def _resolve_browser_path(real_base: Path, rel_str: str) -> Path:
    """
    Resuelve una ruta relativa desde la base del perfil.
    Implementa validación estricta de formato de caracteres prohibidos.
    """
    if not isinstance(real_base, Path) or not isinstance(rel_str, str) or any(c in rel_str for c in '\0\r\n'):
        return Path()
    try:
        # Uso de joinpath seguro asegurando que el path resultante no escape del base
        target = real_base.joinpath(*rel_str.split("\\"))
        return target if len(str(target)) < MAX_PATH_LEN else Path()
    except (TypeError, ValueError, OSError):
        return Path()


def detect_profiles(
    bases: Optional[Sequence[Path]] = None, 
    cache_paths: Optional[BrowserMap] = None
) -> List[BrowserCache]:
    """
    Orquestador principal que escanea las rutas definidas por BROWSER_CACHE_PATHS.
    Retorna una lista de objetos BrowserCache ordenados por uso de disco.
    """
    raw_bases = bases if bases is not None else base_directories()
    browser_map = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS
    
    if not isinstance(raw_bases, (list, tuple)):
        return []

    k32 = _get_kernel32()
    global_memo: Dict[str, int] = {}
    found: List[BrowserCache] = []
    
    for base in raw_bases:
        if not isinstance(base, Path) or not base.is_dir():
            continue
        try:
            real_base_path = base.resolve(strict=True)
            real_base_str = str(real_base_path)
            for browser_name, rel_str in browser_map.items():
                candidate = _resolve_browser_path(real_base_path, rel_str)
                if not candidate or not _is_valid_cache_path(candidate, real_base_str, _IS_JUNCTION_FN):
                    continue
                
                real_candidate = str(candidate.resolve(strict=True))
                size = _sum_directory_recursive(real_candidate, _IS_JUNCTION_FN, k32, global_memo)
                if size > 0:
                    found.append(BrowserCache(str(browser_name), Path(real_candidate), size))
        except (OSError, PermissionError, TypeError, ValueError, RuntimeError):
            continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Optional[Iterable[BrowserCache]] = None) -> int:
    """Calcula la sumatoria total de bytes de una colección de cachés, manejando casos nulos."""
    if caches is None:
        return 0
    return sum(c.size_bytes for c in caches if hasattr(c, 'size_bytes'))


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Crea un informe textual legible de las cachés detectadas."""
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
