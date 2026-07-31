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


def _is_reparse_point(path: Path) -> bool:
    """
    Verifica si la ruta es un punto de reparse (Junction/Symlink).
    """
    try:
        # 0x400 es FILE_ATTRIBUTE_REPARSE_POINT en la API de Windows
        stats = path.lstat()
        return bool(getattr(stats, "st_file_attributes", 0) & 0x400) or path.is_symlink()
    except (OSError, PermissionError):
        return False


def _is_readonly(path: Path) -> bool:
    """Verifica si el archivo tiene el atributo de solo lectura activado."""
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


def normalize(path: PathLike) -> Path:
    """
    Normaliza rutas para comparaciones seguras.
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
    """Verifica si la ruta apunta a la raíz de una unidad (ej. C:\\)."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return False


@lru_cache(maxsize=1024)
def is_protected_path(path: PathLike) -> bool:
    """
    Evalúa si una ruta es peligrosa por definición.
    """
    if not path or not isinstance(path, (str, os.PathLike)):
        return True
    
    raw_path = str(path).strip()
    if not raw_path or raw_path.startswith(("\\\\", "//")):
        return True
        
    try:
        p = normalize(path)
        if not p.is_absolute():
            return True

        # Optimización: chequeo rápido antes de llamadas al disco
        path_parts = {part.lower() for part in p.parts}
        if not _ALL_PROTECTED_TOKENS.isdisjoint(path_parts):
            return True
            
        if p == Path(p.anchor):
            return True
        
        # Solo verificar estado del disco si la ruta realmente existe
        if p.exists() and _is_reparse_point(p):
            return True

        return False
    except (PermissionError, OSError, ValueError, TypeError):
        return True 


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """
    Valida confinamiento: retorna True si 'child' está dentro de 'parent'.
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
    """Determina si la extensión de archivo está en SENSITIVE_EXTENSIONS."""
    if path is None:
        return True
    try:
        return normalize(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> Path:
    """
    Valida que una ruta sea apta para operaciones de escritura/modificación.

    Esta función actúa como una guardia estricta: si la ruta presenta cualquier
    riesgo (sistema, raíz, reparse point, solo lectura o extensión prohibida),
    lanza UnsafePathError.

    Reglas de uso:
    - NUNCA usar como condicional (if ensure_safe_to_modify(...)). La función
      siempre devuelve el objeto Path si es segura.
    - Úsela para preparar la ruta antes de una operación de disco, dejando que
      la excepción interrumpa el flujo si la validación falla.
    - Si necesita un booleano, use is_safe_to_modify() en su lugar.
    """
    if path is None:
        raise UnsafePathError("Ruta nula recibida.")
        
    try:
        p = normalize(path)
    except (TypeError, ValueError, OSError) as e:
        raise UnsafePathError(f"Error al normalizar la ruta: {e}")

    if not p.parts:
        raise UnsafePathError("Ruta inválida: no contiene componentes detectables.")

    if str(p).startswith(("\\\\", "//")):
        raise UnsafePathError("Operación bloqueada: rutas UNC o de red no permitidas.")
    
    if p.exists():
        if _is_reparse_point(p):
            raise UnsafePathError("Operación bloqueada: punto de reparse detectado.")
        if _is_readonly(p):
            raise UnsafePathError("Operación bloqueada: el archivo es de solo lectura.")

    if is_drive_root(p) or is_protected_path(p):
        raise UnsafePathError("Operación bloqueada: ruta de sistema protegida o raíz.")
    
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError(f"Operación bloqueada: extensión sensible detectada ({p.suffix}).")
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """
    Versión booleana para bucles. No lanza excepciones.
    """
    try:
        return isinstance(ensure_safe_to_modify(path, allow_sensitive=allow_sensitive), Path)
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una colección de rutas, retornando solo las validadas."""
    if not isinstance(paths, Iterable):
        return []
        
    safe_list = []
    for p in paths:
        try:
            normalized_p = normalize(p)
            if is_safe_to_modify(normalized_p, allow_sensitive=allow_sensitive):
                safe_list.append(normalized_p)
        except (TypeError, ValueError, OSError):
            continue
    return safe_list


def describe_protection(path: PathLike) -> str:
    """Provee la causa del bloqueo de una ruta para la UI o logs."""
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
            (part for part in p.parts if part.lower() in _ALL_PROTECTED_TOKENS),
            "ruta de sistema",
        )
        return f"'{p}' está protegida por contener '{protegida}'."
    if p.exists() and _is_readonly(p):
        return f"'{p}' tiene atributos de solo lectura."
    if is_sensitive_file(p):
        return f"'{p.name}' tiene extensión sensible ({p.suffix})."
    return f"'{p}' se puede modificar con confirmación."
