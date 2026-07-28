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
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Final

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
_SYSTEM_ROOTS_PARTS: Final[set[str]] = {p.name.lower() for p in _SYSTEM_ROOTS}


def normalize(path: PathLike) -> Path:
    """
    Convierte la entrada a Path absoluto, resolviendo symlinks y referencias '..'.

    Args:
        path: Ruta a normalizar.

    Returns:
        Un objeto Path absoluto y resuelto.

    Raises:
        TypeError: Si la entrada no es str o os.PathLike.
        ValueError: Si la ruta está vacía.
    """
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError(f"Entrada inválida: se esperaba str o PathLike, recibió {type(path)}")
    
    str_path = str(path).strip()
    if not str_path:
        raise ValueError("La ruta proporcionada está vacía.")
        
    try:
        p = Path(str_path).expanduser()
        return p.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(os.path.expanduser(str_path)))


def _contains_protected_name(path: Path) -> bool:
    """Verifica si alguno de los componentes de la ruta es un nombre reservado."""
    return not PROTECTED_DIR_NAMES.isdisjoint(part.lower() for part in path.parts)


def is_drive_root(path: PathLike) -> bool:
    r"""Verifica si la ruta corresponde a la raíz del sistema de archivos (ej. C:\)."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return False


def is_protected_path(path: PathLike) -> bool:
    """
    Determina si una ruta es crítica para el sistema operativo o es una ruta UNC.

    Args:
        path: Ruta a evaluar.

    Returns:
        True si la ruta está protegida, False si es segura para lectura.
    """
    try:
        raw_path = str(path).strip()
        if not raw_path or raw_path.startswith(("\\\\", "//")):
            return True
            
        p = normalize(path)
        if p.is_symlink():
            return True
    except (PermissionError, OSError, ValueError, TypeError):
        return True 
        
    if is_drive_root(p):
        return True
    
    if _contains_protected_name(p):
        return True
        
    return any(part.lower() in _SYSTEM_ROOTS_PARTS for part in p.parts)


def is_within_directory(
    child: PathLike,
    parent: PathLike,
    allow_equal: bool = False,
) -> bool:
    """
    Verifica si 'child' es descendiente de 'parent' tras resolver enlaces.

    Args:
        child: Ruta que se intenta validar.
        parent: Ruta del directorio contenedor.
        allow_equal: Si True, considera True si las rutas coinciden.
    """
    if not child or not parent:
        return False
    try:
        c, p = normalize(child), normalize(parent)
        if not c.is_absolute() or not p.is_absolute():
            return False
        if any(part.is_symlink() for part in c.parents):
            return False
            
        if c == p:
            return allow_equal
        c.relative_to(p)
        return True
    except (ValueError, TypeError, OSError):
        return False


def is_sensitive_file(path: PathLike) -> bool:
    """Verifica si el archivo tiene una extensión sensible (ej. .exe, .sys)."""
    try:
        if not path:
            return True
        return normalize(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> Path:
    """
    Valida la seguridad de la ruta antes de modificarla (escritura/borrado).

    Args:
        path: Ruta a validar.
        allow_sensitive: Si True, permite extensiones sensibles.

    Returns:
        La ruta normalizada si es segura.

    Raises:
        UnsafePathError: Si la ruta es insegura o restringida.
    """
    if not path:
        raise UnsafePathError("La ruta proporcionada está vacía o es None.")
        
    try:
        p = normalize(path)
    except (TypeError, ValueError) as e:
        raise UnsafePathError(f"Ruta mal formada: {path}") from e

    if not p.parts:
        raise UnsafePathError("Ruta inválida: no contiene componentes detectables.")

    str_p = str(p)
    if str_p.startswith(("\\\\", "//")):
        raise UnsafePathError("Operación bloqueada: rutas UNC o de red no permitidas.")
    
    try:
        if p.is_symlink():
            raise UnsafePathError("Operación bloqueada: enlaces simbólicos no permitidos.")
        if p.exists() and (p.is_block_device() or p.is_char_device()):
            raise UnsafePathError("Operación bloqueada: dispositivo especial detectado.")
    except (OSError, PermissionError):
        pass

    if is_drive_root(p):
        raise UnsafePathError("Operación bloqueada: raíz de unidad.")
    if is_protected_path(p):
        raise UnsafePathError("Operación bloqueada: ruta de sistema protegida.")
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError(f"Operación bloqueada: extensión sensible detectada ({p.suffix}).")
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Versión booleana para chequeos preventivos sin levantar excepciones."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """
    Filtra una lista de rutas, retornando solo las que pueden ser modificadas.
    
    Args:
        paths: Iterable con rutas a filtrar.
        allow_sensitive: Propagado a ensure_safe_to_modify.
    """
    safe: list[Path] = []
    if paths is None:
        return safe
    for candidate in paths:
        try:
            if candidate:
                safe.append(ensure_safe_to_modify(candidate, allow_sensitive=allow_sensitive))
        except (UnsafePathError, TypeError, ValueError):
            continue
    return safe


def describe_protection(path: PathLike) -> str:
    """Retorna una cadena explicativa sobre por qué una ruta no es segura."""
    if not path:
        return "La ruta está vacía."
    try:
        p = normalize(path)
    except (TypeError, ValueError):
        return "Ruta mal formada: no se puede analizar."
    if str(p).startswith(("\\\\", "//")):
        return f"'{p}' es una ruta de red."
    if is_drive_root(p):
        return f"'{p}' es la raíz de una unidad."
    if is_protected_path(p):
        protegida = next(
            (part for part in p.parts if part.lower() in PROTECTED_DIR_NAMES),
            "ruta de sistema",
        )
        return f"'{p}' está protegida por contener '{protegida}'."
    if is_sensitive_file(p):
        return f"'{p.name}' tiene extensión sensible ({p.suffix})."
    return f"'{p}' se puede modificar con confirmación."
