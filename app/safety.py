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
_SYSTEM_ROOTS_PARTS: Final[frozenset[str]] = frozenset({p.name.lower() for p in _SYSTEM_ROOTS})
_PROTECTED_AND_SYSTEM: Final[frozenset[str]] = PROTECTED_DIR_NAMES | _SYSTEM_ROOTS_PARTS


def _is_reparse_point(path: Path) -> bool:
    """
    Verifica atributos de sistema (reparse points) para evitar seguir Junctions
    o Symlinks que apunten fuera del árbol de directorios esperado.
    """
    try:
        # 0x400 es FILE_ATTRIBUTE_REPARSE_POINT en Windows API
        return bool(path.lstat().st_file_attributes & 0x400) if hasattr(path.lstat(), "st_file_attributes") else path.is_symlink()
    except (OSError, PermissionError):
        return False


def normalize(path: PathLike) -> Path:
    """
    Estandariza rutas mediante expansión de usuario y resolución de enlaces.
    Garantiza que la ruta sea un objeto Path absoluto para chequeos de seguridad.
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


def is_drive_root(path: PathLike) -> bool:
    r"""Verifica si la ruta corresponde a la raíz absoluta de una unidad (ej. C:\)."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return False


@lru_cache(maxsize=1024)
def is_protected_path(path: PathLike) -> bool:
    """
    Analiza si una ruta debe ser excluida de manipulación.
    Bloquea: rutas UNC, raíces de disco, directorios definidos en PROTECTED_DIR_NAMES,
    y reparse points que podrían causar recursión infinita o escape a zona protegida.
    """
    if not path or not isinstance(path, (str, os.PathLike)):
        return True
    
    raw_path = str(path).strip()
    if not raw_path or raw_path.startswith(("\\\\", "//")):
        return True
        
    try:
        p = normalize(path)
        
        # Optimización: buscar coincidencias en los nombres de los componentes (parts)
        # usando sets en lugar de iteración simple si la ruta es muy profunda.
        if not _PROTECTED_AND_SYSTEM.isdisjoint(part.lower() for part in p.parts):
            return True
            
        if p == Path(p.anchor):
            return True
        
        if p.exists() and _is_reparse_point(p):
            return True

        return False
    except (PermissionError, OSError, ValueError, TypeError):
        return True 


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """
    Valida confinamiento: retorna True si 'child' está contenido dentro de 'parent'.
    Evita ataques de path traversal verificando que 'child' no escape mediante '..'.
    """
    if child is None or parent is None:
        return False
    try:
        c, p = normalize(child), normalize(parent)
        if not c.is_absolute() or not p.is_absolute():
            return False
            
        return p in c.parents or (allow_equal and c == p)
    except (ValueError, TypeError, OSError):
        return False


@lru_cache(maxsize=512)
def is_sensitive_file(path: PathLike) -> bool:
    """Determina si un archivo es crítico por extensión (ej. .sys, .dll, .exe)."""
    if path is None:
        return True
    try:
        return normalize(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> Path:
    """
    Valida la seguridad de la ruta antes de una operación destructiva (borrado/mover).
    Lanza UnsafePathError ante cualquier sospecha.
    """
    if path is None:
        raise UnsafePathError("Ruta nula recibida.")
        
    p = normalize(path)
    if not p.parts:
        raise UnsafePathError("Ruta inválida: no contiene componentes detectables.")

    if str(p).startswith(("\\\\", "//")):
        raise UnsafePathError("Operación bloqueada: rutas UNC o de red no permitidas.")
    
    if p.exists() and _is_reparse_point(p):
        raise UnsafePathError("Operación bloqueada: punto de reparse detectado.")

    if is_drive_root(p) or is_protected_path(p):
        raise UnsafePathError("Operación bloqueada: ruta de sistema protegida o raíz.")
    
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError(f"Operación bloqueada: extensión sensible detectada ({p.suffix}).")
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Versión booleana para chequeos preventivos, ideal para bucles de procesamiento."""
    try:
        return isinstance(ensure_safe_to_modify(path, allow_sensitive=allow_sensitive), Path)
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una lista de rutas, conservando solo aquellas que son seguras para manipular."""
    if not isinstance(paths, Iterable) or paths is None:
        return []
        
    safe_list = []
    for p in paths:
        try:
            if p and is_safe_to_modify(p, allow_sensitive=allow_sensitive):
                safe_list.append(normalize(p))
        except (TypeError, ValueError, OSError):
            continue
    return safe_list


def describe_protection(path: PathLike) -> str:
    """
    Provee una descripción humana de por qué una ruta específica ha sido bloqueada.
    Útil para mostrar al usuario la razón detrás de un bloqueo de seguridad.
    """
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
            (part for part in p.parts if part.lower() in _PROTECTED_AND_SYSTEM),
            "ruta de sistema",
        )
        return f"'{p}' está protegida por contener '{protegida}'."
    if is_sensitive_file(p):
        return f"'{p.name}' tiene extensión sensible ({p.suffix})."
    return f"'{p}' se puede modificar con confirmación."
