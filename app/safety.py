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
    Verifica si una ruta es un punto de reparse (Junction/Symlink).
    Evitamos seguirlos para prevenir bucles infinitos en escaneos o 
    modificaciones accidentales fuera del árbol esperado.
    """
    try:
        stats = path.lstat()
        # 0x400 (FILE_ATTRIBUTE_REPARSE_POINT) detecta Junctions en Windows
        is_reparse = bool(getattr(stats, "st_file_attributes", 0) & 0x400)
        return is_reparse or path.is_symlink()
    except (OSError, PermissionError):
        return True # Por seguridad, ante la duda, denegar el acceso.


def _is_file_in_use(path: Path) -> bool:
    """
    Verifica si el archivo está bloqueado por otro proceso.
    Intenta obtener un descriptor de archivo con acceso exclusivo.
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
    """Valida el bit de solo lectura para evitar errores de escritura fallidos."""
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=2048)
def normalize(path: PathLike) -> Path:
    """
    Normaliza rutas para comparaciones consistentes.
    Resuelve symlinks y rutas relativas para evitar evasion por '..'
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
    """Verifica si la ruta normalizada coincide con la raíz (anchor) de su unidad."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return False


@lru_cache(maxsize=1024)
def is_protected_path(path: PathLike) -> bool:
    """
    Determina si una ruta es peligrosa de modificar.
    Comprueba jerarquías de sistema, raíces de unidad y enlaces simbólicos.
    """
    if not path:
        return True
    
    try:
        p = normalize(path)
        if not p.is_absolute():
            return True

        # Verifica si algún segmento de la ruta coincide con tokens protegidos
        if any(part.lower() in _ALL_PROTECTED_TOKENS for part in p.parts):
            return True
            
        if p == Path(p.anchor):
            return True
        
        if p.exists() and _is_reparse_point(p):
            return True

        return False
    except (PermissionError, OSError, ValueError, TypeError):
        return True 


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Verifica el confinamiento: impide que una operación escape del directorio base."""
    if child is None or parent is None:
        return False
    try:
        c, p = normalize(child), normalize(parent)
        return p in c.parents or (allow_equal and c == p)
    except (ValueError, TypeError, OSError):
        return False


@lru_cache(maxsize=512)
def is_sensitive_file(path: PathLike) -> bool:
    """Determina si el archivo tiene extensión de sistema o seguridad."""
    if path is None:
        return True
    try:
        return normalize(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> Path:
    """
    Validador estricto para operaciones destructivas. 
    Lanza UnsafePathError ante cualquier irregularidad en el archivo o ruta.
    """
    if path is None:
        raise UnsafePathError("Ruta nula recibida.")
        
    # Protección contra inyección de caracteres nulos (null bytes)
    if isinstance(path, str) and "\0" in path:
        raise UnsafePathError("Ruta maliciosa detectada: contiene caracteres nulos.")

    try:
        p = normalize(path)
    except (TypeError, ValueError, OSError) as e:
        raise UnsafePathError(f"Error al normalizar la ruta: {e}")

    # Evitar desbordamiento de buffer en sistemas legacy
    if len(str(p)) > 260:
        raise UnsafePathError("Operación bloqueada: ruta demasiado larga.")

    if not p.parts:
        raise UnsafePathError("Ruta inválida: no contiene componentes detectables.")

    if str(p).startswith(("\\\\", "//")):
        raise UnsafePathError("Operación bloqueada: rutas UNC o de red no permitidas.")
    
    if p.exists():
        if not os.access(p, os.W_OK):
            raise UnsafePathError("Operación bloqueada: sin permisos de escritura.")
        if _is_reparse_point(p):
            raise UnsafePathError("Operación bloqueada: punto de reparse detectado.")
        if _is_readonly(p):
            raise UnsafePathError("Operación bloqueada: archivo de solo lectura.")
        if _is_file_in_use(p):
            raise UnsafePathError("Operación bloqueada: archivo en uso por otro proceso.")
        # Previene riesgos asociados a enlaces físicos (hard links)
        if p.is_file() and p.stat().st_nlink > 1:
            raise UnsafePathError("Operación bloqueada: el archivo tiene múltiples enlaces físicos.")

    if is_drive_root(p) or is_protected_path(p):
        raise UnsafePathError("Operación bloqueada: ruta de sistema protegida o raíz.")
    
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError(f"Operación bloqueada: extensión sensible detectada ({p.suffix}).")
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """
    Versión booleana de ensure_safe_to_modify.
    Ideal para filtrado seguro en bucles de procesamiento masivo.
    """
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una colección de rutas devolviendo solo las que superan los chequeos."""
    if not isinstance(paths, Iterable):
        return []
        
    safe_list = []
    for p in paths:
        try:
            if is_safe_to_modify(p, allow_sensitive=allow_sensitive):
                safe_list.append(normalize(p))
        except (TypeError, ValueError, OSError):
            continue
    return safe_list


def describe_protection(path: PathLike) -> str:
    """Genera un mensaje de diagnóstico detallado sobre por qué una ruta fue rechazada."""
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
    if p.exists():
        if not os.access(p, os.W_OK):
            return f"'{p}' no tiene permisos de escritura."
        if _is_readonly(p):
            return f"'{p}' tiene atributos de solo lectura."
        if _is_file_in_use(p):
            return f"'{p}' está en uso por otro programa."
        if p.is_file() and p.stat().st_nlink > 1:
            return f"'{p}' tiene múltiples enlaces físicos (potencial riesgo)."
    if is_sensitive_file(p):
        return f"'{p.name}' tiene extensión sensible ({p.suffix})."
    return f"'{p}' se puede modificar con confirmación."
