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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, Dict, List, Optional
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
    Valida la integridad de la ruta, previniendo escapes y enlaces simbólicos.
    Verifica que la ruta esté contenida dentro de la base permitida.
    """
    if not isinstance(target_path, Path) or not isinstance(base_path, Path):
        return False
        
    try:
        # Prevenir rutas UNC (servidores externos)
        if str(target_path).startswith(r"\\"):
            return False

        if not target_path.exists():
            return False
            
        if any(ord(char) < 32 for char in target_path.name):
            return False

        real_base = base_path.resolve()
        real_target = target_path.resolve()
        
        if is_protected_path(real_target):
            return False

        # Verifica si el target es symlink o junction
        if real_target.is_symlink() or (hasattr(os.path, 'isjunction') and os.path.isjunction(str(real_target))):
            return False

        # Verifica que real_target sea hijo de real_base
        real_target.relative_to(real_base)
        return True
    except (OSError, ValueError, RuntimeError, PermissionError):
        return False


def directory_size(path: str | os.PathLike | None) -> int:
    """
    Calcula el peso total en bytes de un directorio mediante recorrido iterativo optimizado.
    Usa os.scandir para evitar llamadas adicionales al sistema.
    """
    if path is None:
        return 0
    
    try:
        root = Path(path).resolve()
        if not root.is_dir() or is_protected_path(root):
            return 0
    except (OSError, TypeError, ValueError, PermissionError):
        return 0
    
    total_bytes: int = 0
    stack: List[str] = [str(root)]
    
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        # entry.is_symlink y is_junction validados explícitamente
                        is_junction = hasattr(os.path, 'isjunction') and os.path.isjunction(entry.path)
                        if entry.is_symlink() or is_junction:
                            continue

                        if entry.is_dir(follow_symlinks=False):
                            if not is_protected_path(Path(entry.path)):
                                stack.append(entry.path)
                        else:
                            name_lower = entry.name.lower()
                            if name_lower not in NEVER_TOUCH and not any(ord(c) < 32 for c in entry.name):
                                total_bytes += entry.stat(follow_symlinks=False).st_size
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
            
    return total_bytes


def _is_valid_cache_path(candidate: Path | None, base_path: Path) -> bool:
    """
    Valida si una ruta candidata es un objetivo legítimo de limpieza.
    """
    if not isinstance(candidate, Path) or not isinstance(base_path, Path):
        return False
    try:
        return (
            candidate.exists() and 
            candidate.is_dir() and 
            _is_safe_path(candidate, base_path) and
            candidate.name.lower() not in NEVER_TOUCH
        )
    except (OSError, PermissionError, RuntimeError):
        return False


def detect_profiles(
    bases: Optional[Sequence[Path]] = None, 
    cache_paths: Optional[Dict[str, str]] = None
) -> List[BrowserCache]:
    """
    Mapea rutas de caché sobre directorios base y retorna una lista de
    instancias BrowserCache con información de tamaño. Ordena por tamaño descendente.
    """
    bases = bases if bases is not None else base_directories()
    cache_paths = cache_paths if cache_paths is not None else BROWSER_CACHE_PATHS

    found: List[BrowserCache] = []
    if not bases:
        return found
        
    for base in bases:
        if not isinstance(base, Path): continue
        for browser_name, relative_path_str in cache_paths.items():
            try:
                if not isinstance(relative_path_str, str): continue
                parts: List[str] = relative_path_str.split("\\")
                candidate: Path = base.joinpath(*parts)
                
                if _is_valid_cache_path(candidate, base):
                    size: int = directory_size(candidate)
                    if size > 0:
                        found.append(BrowserCache(
                            browser=browser_name,
                            path=candidate,
                            size_bytes=size,
                        ))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
                
    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return found


def total_cache_bytes(caches: Iterable[BrowserCache] | None = None) -> int:
    """Calcula el peso total en bytes acumulado de una colección de caché."""
    return sum(cache.size_bytes for cache in (caches or []))


def summarize(caches: Optional[List[BrowserCache]] = None) -> List[str]:
    """
    Genera un informe textual legible con el resumen de cachés detectados.
    """
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
