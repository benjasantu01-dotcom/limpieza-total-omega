"""
safety.py — la capa que impide que la app rompa el sistema.

POR QUÉ EXISTE (leer antes de tocar nada acá)
---------------------------------------------
El código de `app/` lo reescribe una IA de forma autónoma, sin supervisión,
durante días. Confiar en que "la IA va a tener cuidado" no es una defensa.
Este módulo convierte el cuidado en una regla que se puede verificar:

  - Ninguna operación destructiva puede tocar rutas de sistema.
  - Ninguna operación destructiva puede tocar la raíz de una unidad.
  - Mover o restaurar archivos no puede escapar de la carpeta destino
    (protección contra rutas maliciosas tipo "../../Windows").
  - Todo borrado es explícito del usuario; nunca automático.
"""

from __future__ import annotations
import os
import stat
import re
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Final
from functools import lru_cache

# Alias para facilitar la lectura de firmas de funciones que aceptan rutas
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
    """Excepción lanzada cuando una operación intenta manipular rutas protegidas."""


# Carpetas que nunca se recorren ni se modifican, en ningún sistema.
PROTECTED_DIR_NAMES: Final[frozenset[str]] = frozenset({
    # Windows
    "windows", "winnt", "system32", "syswow64", "system", "boot",
    "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery",
    "perflogs", "msocache", "$windows.~bt", "$windows.~ws",
    "windowsapps", "assembly", "winsxs", "drivers", "drivestore",
    # Datos de credenciales / claves del usuario
    ".ssh", ".gnupg", "microsoft\\crypto", "protect",
    # Unix / macOS
    "bin", "sbin", "usr", "etc", "var", "lib", "lib64", "proc", "sys",
    "dev", "root", "library", "applications",
})

# Extensiones que no se tocan aunque estén en una carpeta permitida:
SENSITIVE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".sys", ".dll", ".exe", ".msi", ".drv", ".ocx", ".cpl", ".efi",
    ".reg", ".pol", ".key", ".pem", ".pfx", ".p12", ".crt", ".cer",
})

# Cache de rutas críticas para evitar llamadas repetidas al entorno
_SYSTEM_ROOTS: Final[tuple[Path, ...]] = tuple(
    Path(os.environ[v]).resolve() 
    for v in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
)
_SYSTEM_ROOTS_NAMES: Final[frozenset[str]] = frozenset({p.name.lower() for p in _SYSTEM_ROOTS})
_ALL_PROTECTED_TOKENS: Final[frozenset[str]] = PROTECTED_DIR_NAMES | _SYSTEM_ROOTS_NAMES

# Nombres de dispositivos reservados en Windows que pueden causar cuelgues o ataques
_RESERVED_NAMES: Final[frozenset[str]] = frozenset({
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", 
    "com7", "com8", "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9"
})


def _is_reparse_point(path: Path) -> bool:
    """Verifica si la ruta es un punto de reparse (Junction/Symlink)."""
    try:
        stats = path.lstat()
        is_reparse = bool(getattr(stats, "st_file_attributes", 0) & 0x400)
        return is_reparse or path.is_symlink()
    except (OSError, PermissionError):
        return True 


def _is_file_in_use(path: Path) -> bool:
    """Intenta abrir el archivo en modo exclusivo para detectar bloqueos."""
    if not path.is_file():
        return False
    try:
        fd = os.open(path, os.O_RDWR | os.O_EXCL)
        os.close(fd)
        return False
    except (OSError, PermissionError):
        return True


def _is_readonly(path: Path) -> bool:
    """Verifica si el bit S_IWRITE está ausente en el modo del archivo."""
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=2048)
def normalize(path: PathLike) -> Path:
    """
    Convierte una ruta a un objeto Path absoluto, resuelto y expandido.
    Raises: ValueError si la ruta está vacía.
    """
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError(f"Entrada inválida: se esperaba str o PathLike, recibió {type(path)}")
    
    str_path = str(path).strip()
    if not str_path:
        raise ValueError("La ruta proporcionada está vacía.")
        
    try:
        return Path(str_path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(os.path.expanduser(str_path)))


def is_drive_root(path: PathLike) -> bool:
    """Determina si la ruta normalizada es un punto de montaje o raíz de unidad."""
    try:
        if path is None: return True
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return True


@lru_cache(maxsize=1024)
def is_protected_path(path: PathLike) -> bool:
    """Verifica si la ruta coincide con directorios críticos o raíces."""
    if not path:
        return True
    
    try:
        p = normalize(path)
        if not p.is_absolute():
            return True

        # Optimización: uso de intersección de sets para verificar tokens protegidos
        if not _ALL_PROTECTED_TOKENS.isdisjoint(part.lower() for part in p.parts):
            return True
            
        if p == Path(p.anchor):
            return True
        
        if p.exists() and _is_reparse_point(p):
            return True

        return False
    except (PermissionError, OSError, ValueError, TypeError):
        return True 


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Valida si 'child' reside estrictamente dentro de 'parent'."""
    if child is None or parent is None:
        return False
    try:
        c, p = normalize(child), normalize(parent)
        return p in c.parents or (allow_equal and c == p)
    except (ValueError, TypeError, OSError, RuntimeError):
        return False


@lru_cache(maxsize=512)
def is_sensitive_file(path: PathLike) -> bool:
    """Evalúa si la extensión del archivo es crítica para la estabilidad del sistema."""
    if path is None:
        return True
    try:
        return normalize(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> Path:
    """
    Validación rigurosa antes de modificar el sistema.
    Raises: UnsafePathError si la ruta no cumple los estándares de seguridad.
    """
    if path is None:
        raise UnsafePathError("Ruta nula recibida.")
        
    str_val = str(path)
    # Detección de caracteres de control, RTL y nombres de dispositivo Windows
    if re.search(r'[\u202a-\u202e\x00-\x1f]', str_val):
        raise UnsafePathError("Ruta con caracteres de control maliciosos.")
    
    try:
        p = normalize(path)
    except (TypeError, ValueError, OSError) as e:
        raise UnsafePathError(f"Error al normalizar: {e}")

    # Validar nombres de dispositivos reservados
    if p.stem.lower() in _RESERVED_NAMES:
        raise UnsafePathError("Operación bloqueada: nombre de dispositivo reservado.")

    if len(str(p)) > 260:
        raise UnsafePathError("Operación bloqueada: ruta demasiado larga.")

    if not p.parts:
        raise UnsafePathError("Ruta sin componentes.")

    if str(p).startswith(("\\\\", "//")):
        raise UnsafePathError("Operación bloqueada: rutas de red.")
    
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
    """Versión booleana de ensure_safe_to_modify para uso en iteradores."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una colección y devuelve solo aquellas rutas seguras para manipular."""
    if not isinstance(paths, Iterable):
        return []
    return [normalize(p) for p in paths if is_safe_to_modify(p, allow_sensitive=allow_sensitive)]


def describe_protection(path: PathLike) -> str:
    """Genera una explicación amigable sobre por qué se denegó el acceso a una ruta."""
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
        protegida = next((part for part in p.parts if part.lower() in _ALL_PROTECTED_TOKENS), "ruta de sistema")
        return f"'{p}' protegida por '{protegida}'."
    if p.exists():
        if not os.access(p, os.W_OK):
            return f"'{p}' sin permisos de escritura."
        if _is_readonly(p):
            return f"'{p}' tiene atributos de solo lectura."
        if _is_file_in_use(p):
            return f"'{p}' está en uso."
    if is_sensitive_file(p):
        return f"'{p.name}' extensión sensible ({p.suffix})."
    return f"'{p}' es candidata a modificación."
