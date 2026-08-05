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


# DIRECTORIOS_BLOQUEADOS: Nombres que, si aparecen en una ruta, indican riesgo de sistema.
# Se incluyen rutas de configuración de seguridad y carpetas críticas del SO.
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

# EXTENSIONES_SENSIBLES: Archivos que son fundamentales para la integridad o configuración.
SENSITIVE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".sys", ".dll", ".exe", ".msi", ".drv", ".ocx", ".cpl", ".efi",
    ".reg", ".pol", ".key", ".pem", ".pfx", ".p12", ".crt", ".cer",
})

_SYSTEM_ROOTS: Final[list[Path]] = [
    Path(os.environ[v]) for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
]

_RESERVED_NAMES: Final[frozenset[str]] = frozenset({
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", 
    "com7", "com8", "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9"
})

_cache_system_check: dict[Path, bool] = {}


def _has_invalid_chars(path_str: str) -> bool:
    """
    Detecta caracteres no permitidos en sistemas Windows.
    Previene ataques mediante rutas con caracteres RTL o prefijos de dispositivos
    (ej: \\\\?\\) que el API de archivos de Windows interpreta de forma especial.
    """
    return bool("\0" in path_str or re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E]', path_str) or
                path_str.startswith(r"\\?") or path_str.startswith(r"\\."))


def _is_reserved_device_name(name: str) -> bool:
    """Verifica si el nombre de archivo es un dispositivo reservado por el kernel (e.g., CON, NUL)."""
    base = name.split('.')[0]
    return base.lower() in _RESERVED_NAMES


def _is_system_or_hidden(path: Path) -> bool:
    """
    Verifica los atributos de archivo mediante la API Win32.
    Bit 0x02: Oculto, Bit 0x04: Sistema. Ambos son riesgosos para manipulación automatizada.
    """
    if os.name != 'nt':
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & (0x02 | 0x04))
    except Exception:
        return False


def _is_reparse_point(path: Path) -> bool:
    """
    Identifica Junctions o Symlinks mediante atributos de sistema de archivos.
    Bloquear esto evita bucles infinitos en el escáner y previene modificaciones
    inesperadas en volúmenes montados fuera de la jerarquía esperada.
    """
    try:
        stats = path.lstat()
        return bool(getattr(stats, "st_file_attributes", 0) & 0x400) or path.is_symlink()
    except (OSError, PermissionError):
        return True 


def _is_file_in_use(path: Path) -> bool:
    """
    Intenta apertura exclusiva para validar disponibilidad.
    Si el archivo está en uso por otro proceso (ej. un servicio del sistema),
    el intento de apertura lanzará un error que usamos como indicador de bloqueo.
    """
    if not path.is_file():
        return False
    try:
        fd = os.open(path, os.O_RDWR | os.O_EXCL)
        os.close(fd)
        return False
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=1024)
def _is_readonly(path: Path) -> bool:
    """Verifica el permiso S_IWRITE a nivel de sistema de archivos."""
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=2048)
def normalize(path: PathLike) -> Path:
    """
    Canoniza una ruta para asegurar comparaciones consistentes.
    Resuelve symlinks, expande '~' y convierte a ruta absoluta, garantizando
    que el chequeo de seguridad opere sobre la ubicación física real.
    """
    if path is None or not isinstance(path, (str, os.PathLike)):
        raise TypeError(f"Entrada inválida: tipo {type(path) if path is not None else 'None'} no soportado.")
    
    str_path = str(path).strip()
    if not str_path:
        raise ValueError("La ruta proporcionada está vacía.")
        
    try:
        return Path(str_path).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(os.path.expanduser(str_path)))


def is_drive_root(path: PathLike) -> bool:
    """Comprueba si la ruta es la raíz del sistema de archivos (ej: C:\\)."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return True


@lru_cache(maxsize=1024)
def is_protected_path(path: PathLike) -> bool:
    """
    Determina si la ruta forma parte de directorios críticos del SO.
    Utiliza una lista blanca de nombres protegidos y las raíces del sistema
    detectadas dinámicamente mediante variables de entorno.
    """
    if not path:
        return True
    
    try:
        p = normalize(path)
        if p in _cache_system_check:
            return _cache_system_check[p]
        
        is_protected = False
        if any(part.lower() in PROTECTED_DIR_NAMES for part in p.parts):
            is_protected = True
        else:
            for sys_root in _SYSTEM_ROOTS:
                try:
                    if os.path.commonpath([str(p), str(sys_root)]) == str(sys_root):
                        is_protected = True
                        break
                except ValueError:
                    continue
        
        if not is_protected:
            is_protected = p == Path(p.anchor) or (p.exists() and _is_reparse_point(p))
            
        _cache_system_check[p] = is_protected
        return is_protected
    except (PermissionError, OSError, ValueError, TypeError):
        return True 


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Verifica si 'child' es descendiente de 'parent' tras normalizar ambas rutas."""
    try:
        c, p = normalize(child), normalize(parent)
        return p in c.parents or (allow_equal and c == p)
    except (ValueError, TypeError, OSError, RuntimeError):
        return False


@lru_cache(maxsize=512)
def is_sensitive_file(path: PathLike) -> bool:
    """Comprueba si la extensión del archivo está en el catálogo de riesgo."""
    try:
        return Path(path).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> Path:
    """
    Validador estricto para operaciones de escritura.
    Centraliza toda la lógica de seguridad: bloquea rutas reservadas, redes,
    archivos bloqueados, de sistema o de solo lectura.
    
    Args:
        path: Ruta a validar.
        allow_sensitive: Si es True, permite archivos de configuración crítica.
        
    Raises:
        UnsafePathError: Si la ruta no cumple con los criterios de seguridad.
    """
    if path is None:
        raise UnsafePathError("Ruta nula recibida.")

    p = normalize(path)
    str_val = str(p)

    if _has_invalid_chars(str_val):
        raise UnsafePathError("Ruta contiene caracteres de control o formato potencialmente maliciosos.")
    
    if len(str_val) > 260:
        raise UnsafePathError("Operación bloqueada: ruta demasiado larga.")

    if _is_reserved_device_name(p.stem):
        raise UnsafePathError("Operación bloqueada: nombre de dispositivo reservado.")
    if str_val.startswith(("\\\\", "//")):
        raise UnsafePathError("Operación bloqueada: rutas de red no permitidas.")
    
    if p.exists():
        if not os.access(p, os.W_OK):
            raise UnsafePathError("Operación bloqueada: sin permisos de escritura.")
        if _is_reparse_point(p) or _is_readonly(p) or _is_file_in_use(p) or _is_system_or_hidden(p):
            raise UnsafePathError("Operación bloqueada: archivo inaccesible, protegido o sistema.")
        if p.is_file() and p.stat().st_nlink > 1:
            raise UnsafePathError("Operación bloqueada: enlace físico (hard link) detectado.")

    if is_drive_root(p) or is_protected_path(p):
        raise UnsafePathError("Operación bloqueada: ruta de sistema protegida.")
    
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError("Operación bloqueada: extensión sensible.")
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Wrapper booleano para `ensure_safe_to_modify` para filtrado de listas."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una colección de rutas, retornando solo las seguras."""
    seen: set[Path] = set()
    valid: list[Path] = []
    for p in paths:
        try:
            norm_p = normalize(p)
            if norm_p not in seen:
                if is_safe_to_modify(norm_p, allow_sensitive=allow_sensitive):
                    seen.add(norm_p)
                    valid.append(norm_p)
                else:
                    seen.add(norm_p)
        except (TypeError, ValueError, OSError):
            continue
    return valid


def describe_protection(path: PathLike) -> str:
    """Devuelve la razón de por qué un archivo fue marcado como inseguro."""
    try:
        p = normalize(path)
    except (TypeError, ValueError):
        return "Ruta mal formada."
    if str(p).startswith(("\\\\", "//")):
        return f"'{p}' es una ruta de red."
    if is_drive_root(p):
        return f"'{p}' es la raíz de una unidad."
    if is_protected_path(p):
        return f"'{p}' protegida por reglas de sistema."
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
