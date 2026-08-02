"""
safety.py — capa de seguridad para la manipulación de archivos.

Este módulo provee funciones de validación para prevenir modificaciones
accidentales o maliciosas en directorios del sistema o archivos críticos.
Todo cambio destructivo debe pasar por `ensure_safe_to_modify`.
"""

from __future__ import annotations
import os
import stat
import re
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Final
from functools import lru_cache

PathLike: TypeAlias = Union[str, os.PathLike]

__all__ = [
    "UnsafePathError",
    "PROTECTED_DIR_NAMES",
    "SENSITIVE_EXTENSIONS",
    "normalize",
    "is_drive_root",
    "is_protected_path",
    "is_within_directory",
    "ensure_safe_to_modify",
    "is_safe_to_modify",
    "filter_safe_paths",
    "is_sensitive_file",
    "describe_protection",
]


class UnsafePathError(Exception):
    """Lanzada cuando una operación intenta manipular rutas protegidas."""


PROTECTED_DIR_NAMES: Final[frozenset[str]] = frozenset({
    "windows", "winnt", "system32", "syswow64", "system", "boot",
    "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery",
    "perflogs", "msocache", "$windows.~bt", "$windows.~ws",
    "windowsapps", "assembly", "winsxs", "drivers", "drivestore",
    ".ssh", ".gnupg", "microsoft\\crypto", "protect",
    "bin", "sbin", "usr", "etc", "var", "lib", "lib64", "proc", "sys",
    "dev", "root", "library", "applications",
})

SENSITIVE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".sys", ".dll", ".exe", ".msi", ".drv", ".ocx", ".cpl", ".efi",
    ".reg", ".pol", ".key", ".pem", ".pfx", ".p12", ".crt", ".cer",
})

_SYSTEM_ROOTS_NAMES: Final[frozenset[str]] = frozenset({
    os.path.basename(os.environ.get(v, "")).lower() 
    for v in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
} - {""})
_ALL_PROTECTED_TOKENS: Final[frozenset[str]] = PROTECTED_DIR_NAMES | _SYSTEM_ROOTS_NAMES

_RESERVED_NAMES: Final[frozenset[str]] = frozenset({
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", 
    "com7", "com8", "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9"
})


def _is_reparse_point(path: Path) -> bool:
    """Retorna True si la ruta es un punto de reparse, enlace simbólico o unión."""
    try:
        stats = path.lstat()
        is_reparse = bool(getattr(stats, "st_file_attributes", 0) & 0x400)
        return is_reparse or path.is_symlink()
    except (OSError, PermissionError):
        return True 


def _is_file_in_use(path: Path) -> bool:
    """Intenta abrir el archivo en modo exclusivo para detectar bloqueos de SO."""
    if not path.is_file():
        return False
    try:
        fd = os.open(path, os.O_RDWR | os.O_EXCL)
        os.close(fd)
        return False
    except (OSError, PermissionError):
        return True


def _is_readonly(path: Path) -> bool:
    """Verifica si el atributo de solo lectura (S_IWRITE) está ausente."""
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=2048)
def normalize(path: PathLike) -> Path:
    """
    Expande, resuelve y convierte una ruta a objeto Path absoluto.
    :raises TypeError: Si el tipo de entrada no es compatible.
    :raises ValueError: Si la ruta normalizada resulta vacía.
    """
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError(f"Entrada inválida: tipo {type(path)} no soportado.")
    
    str_path = str(path).strip()
    if not str_path:
        raise ValueError("La ruta proporcionada está vacía.")
        
    try:
        return Path(str_path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(os.path.expanduser(str_path)))


def is_drive_root(path: PathLike) -> bool:
    """Verifica si la ruta apunta a la raíz de un sistema de archivos."""
    try:
        if path is None: return True
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return True


@lru_cache(maxsize=1024)
def is_protected_path(path: PathLike) -> bool:
    """
    Valida si la ruta pertenece a directorios de sistema o dispositivos reservados.
    """
    if not path:
        return True
    
    try:
        # Usamos abspath para evitar el costo de I/O de resolve() al verificar tokens
        p_str = str(Path(path).expanduser().absolute())
        parts = {p.lower() for p in Path(p_str).parts}
        
        if not _ALL_PROTECTED_TOKENS.isdisjoint(parts):
            return True
            
        p = Path(p_str)
        if p == Path(p.anchor):
            return True
        if p.exists() and _is_reparse_point(p):
            return True
        return False
    except (PermissionError, OSError, ValueError, TypeError):
        return True 


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """
    Verifica si 'child' es descendiente de 'parent'.
    Nota: Utiliza rutas resueltas; los puntos de reparse pueden afectar el resultado.
    """
    if child is None or parent is None:
        return False
    try:
        c, p = normalize(child), normalize(parent)
        return p in c.parents or (allow_equal and c == p)
    except (ValueError, TypeError, OSError, RuntimeError):
        return False


@lru_cache(maxsize=512)
def is_sensitive_file(path: PathLike) -> bool:
    """Evalúa si la extensión del archivo es crítica para la integridad del SO."""
    if path is None:
        return True
    try:
        return Path(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> Path:
    """
    Valida rigurosamente si una ruta es segura para ser modificada o borrada.
    :raises UnsafePathError: Si la ruta incumple los estándares de seguridad.
    """
    if path is None:
        raise UnsafePathError("Ruta nula recibida.")
        
    str_val = str(path)
    if re.search(r'[\u202a-\u202e\x00-\x1f]', str_val):
        raise UnsafePathError("Ruta con caracteres de control maliciosos.")
    
    try:
        p = normalize(path)
    except (TypeError, ValueError, OSError) as e:
        raise UnsafePathError(f"Error al normalizar: {e}")

    if p.stem.lower() in _RESERVED_NAMES:
        raise UnsafePathError("Operación bloqueada: nombre de dispositivo reservado.")
    if len(str(p)) > 260:
        raise UnsafePathError("Operación bloqueada: ruta demasiado larga.")
    if not p.parts:
        raise UnsafePathError("Ruta sin componentes.")
    if str(p).startswith(("\\\\", "//")):
        raise UnsafePathError("Operación bloqueada: rutas de red no permitidas.")
    
    if p.exists():
        if not os.access(p, os.W_OK):
            raise UnsafePathError("Operación bloqueada: sin permisos de escritura.")
        if _is_reparse_point(p) or _is_readonly(p) or _is_file_in_use(p):
            raise UnsafePathError("Operación bloqueada: archivo inaccesible, protegido o en uso.")
        if p.is_file() and p.stat().st_nlink > 1:
            raise UnsafePathError("Operación bloqueada: enlace físico detectado.")

    if is_drive_root(p) or is_protected_path(p):
        raise UnsafePathError("Operación bloqueada: ruta de sistema protegida.")
    
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError("Operación bloqueada: extensión sensible.")
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Versión booleana de `ensure_safe_to_modify` para iteraciones seguras."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Retorna una lista de rutas que han superado los filtros de seguridad."""
    if not isinstance(paths, Iterable):
        return []
    return [normalize(p) for p in paths if is_safe_to_modify(p, allow_sensitive=allow_sensitive)]


def describe_protection(path: PathLike) -> str:
    """Genera una explicación amigable sobre la denegación de acceso a una ruta."""
    if not path:
        return "La ruta está vacía."
    try:
        p = normalize(path)
    except (TypeError, ValueError):
        return "Ruta mal formada."
    if str(p).startswith(("\\\\", "//")):
        return f"'{p}' es una ruta de red."
    if is_drive_root(p):
        return f"'{p}' es la raíz de una unidad."
    if is_protected_path(p):
        protegida = next((part for part in p.parts if part.lower() in _ALL_PROTECTED_TOKENS), "sistema")
        return f"'{p}' protegida por directorio '{protegida}'."
    if p.exists():
        if not os.access(p, os.W_OK):
            return f"'{p}' sin permisos de escritura."
        if _is_readonly(p):
            return f"'{p}' atributos de solo lectura."
        if _is_file_in_use(p):
            return f"'{p}' está en uso por otro proceso."
    if is_sensitive_file(p):
        return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
