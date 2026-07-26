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

Los tests de `evolve/tests/` verifican estas reglas. Si la IA borrara o
debilitara estas funciones, los tests fallan y el cambio se rechaza; y si
intentara eliminarlas, la guardia AST de `evolve/guards.py` lo bloquea por
pérdida de símbolos. Es decir: esta protección no depende del criterio del
modelo, está clavada en el proceso.
"""

from __future__ import annotations
import os
from pathlib import Path, PurePath

__all__ = [
    "UnsafePathError",
    "PROTECTED_DIR_NAMES",
    "SENSITIVE_EXTENSIONS",
    "normalize",
    "is_drive_root",
    "is_protected_path",
    "is_within_directory",
    "ensure_safe_to_modify",
    "filter_safe_paths",
    "is_sensitive_file",
    "describe_protection",
]


class UnsafePathError(Exception):
    """Se intentó una operación destructiva sobre una ruta protegida."""


# Carpetas que nunca se recorren ni se modifican, en ningún sistema.
# Se comparan en minúsculas contra cada componente de la ruta, así da igual
# si la unidad es C:, D: o si el usuario tiene Windows en otra ubicación.
PROTECTED_DIR_NAMES = frozenset({
    # Windows
    "windows", "winnt", "system32", "syswow64", "system", "boot",
    "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery",
    "perflogs", "msocache", "$windows.~bt", "$windows.~ws",
    "windowsapps", "assembly", "winsxs", "drivers", "drivestore",
    # Datos de credenciales / claves del usuario
    ".ssh", ".gnupg", "microsoft\\crypto", "protect",
    # Unix / macOS (por si se corre fuera de Windows)
    "bin", "sbin", "usr", "etc", "var", "lib", "lib64", "proc", "sys",
    "dev", "boot", "root", "library", "applications",
})

# Extensiones que no se tocan aunque estén en una carpeta permitida:
# borrarlas puede dejar el sistema o un programa sin arrancar.
SENSITIVE_EXTENSIONS = frozenset({
    ".sys", ".dll", ".exe", ".msi", ".drv", ".ocx", ".cpl", ".efi",
    ".reg", ".pol", ".key", ".pem", ".pfx", ".p12", ".crt", ".cer",
})


def normalize(path: str | os.PathLike) -> Path:
    """Devuelve una ruta absoluta y resuelta, sin fallar si no existe."""
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError(f"Entrada inválida en normalize: se esperaba str o PathLike, recibió {type(path)}")
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(os.path.expanduser(str(path))))


def is_drive_root(path: str | os.PathLike) -> bool:
    """True si la ruta es la raíz de una unidad o del sistema de archivos."""
    p = normalize(path)
    return p.parent == p or str(p) == p.anchor


def is_protected_path(path: str | os.PathLike) -> bool:
    """True si la ruta cae dentro de una carpeta de sistema protegida."""
    p = normalize(path)
    if is_drive_root(p):
        return True
    parts_lower = [part.lower() for part in p.parts]
    if any(part in PROTECTED_DIR_NAMES for part in parts_lower):
        return True
    for env_var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        root = os.environ.get(env_var)
        if root and is_within_directory(p, root, allow_equal=True):
            return True
    return False


def is_within_directory(
    child: str | os.PathLike,
    parent: str | os.PathLike,
    allow_equal: bool = False,
) -> bool:
    """True si `child` está realmente contenido en `parent`."""
    try:
        c, p = normalize(child), normalize(parent)
    except TypeError:
        return False
    if c == p:
        return allow_equal
    try:
        c.relative_to(p)
        return True
    except ValueError:
        return False


def is_sensitive_file(path: str | os.PathLike) -> bool:
    """True si la extensión del archivo lo hace peligroso de borrar."""
    return normalize(path).suffix.lower() in SENSITIVE_EXTENSIONS


def ensure_safe_to_modify(path: str | os.PathLike, *, allow_sensitive: bool = False) -> Path:
    """Valida que se pueda modificar/borrar la ruta, o lanza UnsafePathError."""
    p = normalize(path)
    if is_drive_root(p):
        raise UnsafePathError(f"Operación bloqueada: '{p}' es la raíz de una unidad.")
    if is_protected_path(p):
        raise UnsafePathError(f"Operación bloqueada: '{p}' está en una ruta de sistema protegida.")
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError(
            f"Operación bloqueada: '{p.name}' tiene una extensión sensible ({p.suffix})."
        )
    return p


def filter_safe_paths(paths, *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una lista dejando solo las rutas seguras de modificar."""
    safe: list[Path] = []
    if not paths:
        return safe
    for candidate in paths:
        try:
            safe.append(ensure_safe_to_modify(candidate, allow_sensitive=allow_sensitive))
        except (UnsafePathError, TypeError):
            continue
    return safe


def describe_protection(path: str | os.PathLike) -> str:
    """Explica en una línea por qué una ruta está o no protegida."""
    try:
        p = normalize(path)
    except TypeError:
        return "Ruta mal formada: no se puede analizar."
    if is_drive_root(p):
        return f"'{p}' es la raíz de una unidad: nunca se modifica."
    if is_protected_path(p):
        protegida = next(
            (part for part in p.parts if part.lower() in PROTECTED_DIR_NAMES),
            "ruta de sistema",
        )
        return f"'{p}' está protegida por contener '{protegida}'."
    if is_sensitive_file(p):
        return f"'{p.name}' tiene extensión sensible ({p.suffix}): no se borra automáticamente."
    return f"'{p}' se puede modificar con confirmación del usuario."
