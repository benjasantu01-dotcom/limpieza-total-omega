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
from pathlib import Path, PurePath
from typing import Iterable, Sequence, Dict, List, Optional, Callable
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

# Rutas de caché relativas a LOCALAPPDATA (Windows). Solo datos
# regenerables: si se borran, el navegador los vuelve a crear.
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

# Nombres que este módulo nunca reporta ni toca, aunque estén dentro del
# perfil: son datos del usuario, no caché.
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

@dataclass
class BrowserCache:
    """Representación de una carpeta de caché detectada y su peso en disco."""
    browser: str
    path: Path
    size_bytes: int

    @property
    def size_mb(self) -> float:
        """Retorna el tamaño en MB. Precisión de 2 decimales para reportes."""
        return round(self.size_bytes / (1024 * 1024), 2)


def base_directories() -> List[Path]:
    """
    Obtiene los directorios raíz donde se alojan perfiles (LOCALAPPDATA).
    Retorna una lista vacía si el SO no es Windows o la variable es inexistente.
    """
    if os.name != "nt":
        return []
    
    local: Optional[str] = os.environ.get("LOCALAPPDATA")
    if not local or not isinstance(local, str):
        return []
    
    try:
        path_local = Path(local).resolve()
        if path_local.is_absolute() and path_local.is_dir():
            return [path_local]
        return []
    except (OSError, RuntimeError, ValueError):
        return []


def _is_safe_path(target_path: Optional[Path], base_path: Optional[Path]) -> bool:
    """
    Valida la seguridad de la ruta mediante:
    1. Resolución de paths reales para evitar Directory Traversal.
    2. Verificación de permisos de sistema (`is_protected_path`).
    3. Detección de caracteres sospechosos (RTL/ocultos).
    4. Bloqueo de enlaces simbólicos o junctions para evitar bucles.
    """
    if not isinstance(target_path, Path) or not isinstance(base_path, Path):
        return False
        
    try:
        real_base = base_path.resolve(strict=True)
        real_target = target_path.resolve(strict=True)
        
        # Verificar que target esté bajo base_path evitando ataques de traversal
        if os.path.commonpath([real_base, real_target]) != str(real_base):
            return False

        if is_protected_path(real_target):
            return False

        # Detectar caracteres no imprimibles o RTL en el path
        if any(ord(char) < 32 or ord(char) in (0x200E, 0x200F, 0x202A, 0x202E) for char in str(target_path)):
            return False

        is_junction = getattr(os.path, 'isjunction', lambda _: False)
        if real_target.is_symlink() or is_junction(str(real_target)):
            return False

        return True
    except (OSError, ValueError, RuntimeError, PermissionError):
        return False


def _is_excluded_file(name: str) -> bool:
    """Retorna True si el nombre del archivo es un archivo de configuración crítico."""
    return name.lower() in NEVER_TOUCH


def _sum_directory_recursive(root_dir: str, is_junction_fn: Callable[[str], bool]) -> int:
    """
    Realiza un DFS sobre el árbol de directorios para calcular el peso total.
    
    Utiliza `os.scandir` para mayor eficiencia. Omite archivos protegidos por
    el sistema (GetFileAttributesW) y respeta las exclusiones definidas en 
    `NEVER_TOUCH`. No sigue enlaces simbólicos ni junctions.
    """
    if not root_dir or not os.path.exists(root_dir):
        return 0
        
    total: int = 0
    kernel32 = ctypes.windll.kernel32 if os.name == 'nt' else None
    
    try:
        with os.scandir(root_dir) as it:
            for entry in it:
                try:
                    if os.name == 'nt' and kernel32:
                        attrs = kernel32.GetFileAttributesW(entry.path)
                        # Omitir archivos ocultos o de sistema (0x04, 0x02)
                        if attrs != -1 and (attrs & 0x04 or attrs & 0x02):
                            continue

                    if entry.is_symlink() or is_junction_fn(entry.path):
                        continue
                    
                    if entry.is_dir():
                        total += _sum_directory_recursive(entry.path, is_junction_fn)
                    elif entry.is_file() and not _is_excluded_file(entry.name):
                        total += entry.stat().st_size
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        pass
    return total


def directory_size(path: str | os.PathLike | None) -> int:
    """
    Calcula el peso total de una carpeta tras validar que la ruta sea segura.
    Retorna 0 ante cualquier error de acceso o si la ruta viola las políticas de seguridad.
    """
    if path is None:
        return 0
    
    try:
        p_path = Path(path)
        if not p_path.exists():
            return 0
        root_path = p_path.resolve(strict=True)
        if not root_path.is_absolute() or not root_path.is_dir() or is_protected_path(root_path):
            return 0
        
        is_junction: Callable[[str], bool] = getattr(os.path, 'isjunction', lambda _: False)
        return _sum_directory_recursive(str(root_path), is_junction)
    except (OSError, PermissionError, RuntimeError, ValueError):
        return 0


def _is_valid_cache_path(candidate: Optional[Path], base_path: Path) -> bool:
    """Valida si un path es una carpeta de caché, siendo segura y no configurada."""
    if not isinstance(candidate, Path) or not isinstance(base_path, Path):
        return False
    try:
        return (
            candidate.exists() and 
            candidate.is_dir() and 
            _is_safe_path(candidate, base_path) and
            not _is_excluded_file(candidate.name)
        )
    except (OSError, PermissionError, RuntimeError):
        return False


def detect_profiles(
    bases: Optional[Sequence[Path]] = None, 
    cache_paths: Optional[Dict[str, str]] = None
) -> List[BrowserCache]:
    """Detecta cachés instaladas combinando rutas base y subrutas conocidas."""
    bases = bases if bases is not None else base_directories()
    cache_paths = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS
    is_junction: Callable[[str], bool] = getattr(os.path, 'isjunction', lambda _: False)

    found: List[BrowserCache] = []
    if not isinstance(bases, (list, tuple)) or not isinstance(cache_paths, dict):
        return found
        
    for base in bases:
        if not isinstance(base, Path): continue
        try:
            real_base = base.resolve(strict=True)
        except (OSError, PermissionError):
            continue
            
        for browser_name, relative_path_str in cache_paths.items():
            try:
                candidate = real_base.joinpath(*relative_path_str.split("\\")).resolve()
                if _is_valid_cache_path(candidate, real_base):
                    size: int = _sum_directory_recursive(str(candidate), is_junction)
                    if size > 0:
                        found.append(BrowserCache(
                            browser=browser_name,
                            path=candidate,
                            size_bytes=size,
                        ))
            except (OSError, ValueError, TypeError, AttributeError, PermissionError):
                continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Calcula el peso total en bytes acumulado de una colección de caché."""
    return sum(cache.size_bytes for cache in (caches or []))


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """Genera un informe textual del estado de las cachés encontradas."""
    current_caches: List[BrowserCache] = caches if caches is not None else detect_profiles()
    
    if not current_caches:
        return ["No se detectaron cachés de navegador en este sistema."]
        
    total_mb: float = round(total_cache_bytes(current_caches) / (1024 * 1024), 2)
    lines: List[str] = [f"Caché de navegadores: {total_mb} MB en {len(current_caches)} carpeta(s)", ""]
    for cache in current_caches:
        lines.append(f"  {cache.browser:<20} {cache.size_mb:>9} MB")
        lines.append(f"      {cache.path}")
    lines.extend(["", SAFETY_NOTE])
    return lines
