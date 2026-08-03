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
import ctypes
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Final, Mapping, Sequence
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


def _is_system_or_hidden(path: Path) -> bool:
    """
    Verifica mediante la API Win32 si un archivo posee atributos de sistema o oculto.
    Retorna False en entornos no Windows o ante errores de lectura de atributos.
    """
    if os.name != 'nt' or not path.exists():
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == -1: return False
        return bool(attrs & 0x02) or bool(attrs & 0x04)
    except Exception:
        return False


def _is_reparse_point(path: Path) -> bool:
    """
    Determina si la ruta es un punto de reanálisis (Junction/Symlink).
    El acceso a estos puede causar recursión infinita o borrados fuera del árbol deseado.
    """
    if not path.exists():
        return False
    try:
        stats = path.lstat()
        is_reparse = bool(getattr(stats, "st_file_attributes", 0) & 0x400)
        return is_reparse or path.is_symlink()
    except (OSError, PermissionError):
        return True 


def _is_file_in_use(path: Path) -> bool:
    """
    Valida la disponibilidad de escritura mediante un intento de apertura exclusiva.
    Útil para prevenir conflictos con procesos de sistema o antivirus activos.
    """
    if not path.is_file():
        return False
    try:
        fd = os.open(path, os.O_RDWR | os.O_EXCL)
        os.close(fd)
        return False
    except (OSError, PermissionError):
        return True


def _is_readonly(path: Path) -> bool:
    """
    Verifica el bit de modo S_IWRITE. Si no está presente, el archivo es tratado
    como protegido contra escritura a nivel de sistema de archivos.
    """
    if not path.exists():
        return False
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=2048)
def normalize(path: PathLike) -> Path:
    """
    Transforma una ruta en una instancia de Path absoluta y resuelta.
    La cache evita llamadas costosas al sistema de archivos para rutas repetidas.
    """
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError(f"Entrada inválida: tipo {type(path)} no soportado.")
    
    str_path = str(path).strip()
    if not str_path:
        raise ValueError("La ruta proporcionada está vacía.")
        
    try:
        return Path(str_path).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(os.path.expanduser(str_path)))


def is_drive_root(path: PathLike) -> bool:
    """Retorna True si la ruta normalizada apunta a la raíz de un dispositivo de almacenamiento."""
    if path is None: return True
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return True


@lru_cache(maxsize=1024)
def is_protected_path(path: PathLike) -> bool:
    """
    Evalúa si la ruta reside en directorios críticos definidos en _ALL_PROTECTED_TOKENS.
    No requiere existencia física del archivo para retornar True si el nombre coincide.
    """
    if not path:
        return True
    
    try:
        p = normalize(path)
        if any(part.lower() in _ALL_PROTECTED_TOKENS for part in p.parts):
            return True
        if p == Path(p.anchor):
            return True
        return p.exists() and _is_reparse_point(p)
    except (PermissionError, OSError, ValueError, TypeError):
        return True 


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Valida la relación de pertenencia física entre dos rutas mediante resolución absoluta."""
    if child is None or parent is None:
        return False
    try:
        c, p = normalize(child), normalize(parent)
        return p in c.parents or (allow_equal and c == p)
    except (ValueError, TypeError, OSError, RuntimeError):
        return False


@lru_cache(maxsize=512)
def is_sensitive_file(path: PathLike) -> bool:
    """Verifica si la extensión del archivo está presente en el set de extensiones críticas."""
    if path is None:
        return True
    try:
        return Path(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> Path:
    """
    Validador estricto para operaciones de escritura/borrado.
    Lanza UnsafePathError ante cualquier irregularidad en la ruta, permisos o criticidad.
    """
    if not isinstance(path, (str, os.PathLike)):
        raise UnsafePathError(f"Ruta de tipo inválido recibida: {type(path)}")
        
    str_val = str(path)
    if "\0" in str_val or re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E]', str_val) or \
       str_val.startswith(r"\\?") or str_val.startswith(r"\\."):
        raise UnsafePathError("Ruta contiene caracteres de control o formato potencialmente maliciosos.")
    
    if len(str_val) > 260:
        raise UnsafePathError("Operación bloqueada: ruta demasiado larga.")
    
    try:
        p = normalize(path)
    except (TypeError, ValueError, OSError) as e:
        raise UnsafePathError(f"Error al normalizar: {e}")

    if not p.parts:
        raise UnsafePathError("Ruta sin componentes válidos tras normalización.")

    if p.stem.lower() in _RESERVED_NAMES:
        raise UnsafePathError("Operación bloqueada: nombre de dispositivo reservado.")
    if str(p).startswith(("\\\\", "//")):
        raise UnsafePathError("Operación bloqueada: rutas de red no permitidas.")
    
    try:
        if p.exists():
            if not os.access(p, os.W_OK):
                raise UnsafePathError("Operación bloqueada: sin permisos de escritura.")
            if _is_reparse_point(p) or _is_readonly(p) or _is_file_in_use(p) or _is_system_or_hidden(p):
                raise UnsafePathError("Operación bloqueada: archivo inaccesible, protegido o sistema.")
            if p.is_file() and p.stat().st_nlink > 1:
                raise UnsafePathError("Operación bloqueada: enlace físico detectado.")
    except OSError as e:
        raise UnsafePathError(f"Error al verificar estado del archivo: {e}")

    if is_drive_root(p) or is_protected_path(p):
        raise UnsafePathError("Operación bloqueada: ruta de sistema protegida.")
    
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError("Operación bloqueada: extensión sensible.")
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Wrapper booleano para operaciones condicionales que evitan el uso directo de excepciones."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Procesa una secuencia de rutas y retorna únicamente aquellas aptas para ser manipuladas."""
    if not isinstance(paths, Iterable):
        return []
    valid = []
    for p in paths:
        try:
            if is_safe_to_modify(p, allow_sensitive=allow_sensitive):
                valid.append(normalize(p))
        except (TypeError, ValueError):
            continue
    return valid


def describe_protection(path: PathLike) -> str:
    """Diagnóstico detallado del motivo de rechazo de una ruta por el motor de seguridad."""
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
        if _is_system_or_hidden(p):
            return f"'{p}' archivo de sistema o oculto."
    if is_sensitive_file(p):
        return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
