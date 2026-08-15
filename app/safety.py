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
import sys
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Final, NamedTuple, Callable
from functools import lru_cache

PathLike: TypeAlias = Union[str, os.PathLike]
ViolationPredicate: TypeAlias = Callable[[], bool]

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
    "is_running_as_admin",
]


class UnsafePathError(Exception):
    """Lanzada cuando una operación intenta manipular rutas protegidas."""


# DIRECTORIOS_BLOQUEADOS: Nombres que, si aparecen en una ruta, indican riesgo de sistema.
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

# EXTENSIONES_SENSIBLES: Archivos fundamentales para la integridad o configuración.
SENSITIVE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".sys", ".dll", ".exe", ".msi", ".drv", ".ocx", ".cpl", ".efi",
    ".reg", ".pol", ".key", ".pem", ".pfx", ".p12", ".crt", ".cer",
})

_SYSTEM_ROOT_PARTS: Final[frozenset[tuple[str, ...]]] = frozenset(
    Path(os.environ[v]).parts for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
)

_RESERVED_NAMES_PATTERN: Final[re.Pattern] = re.compile(
    r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$', re.IGNORECASE
)


class _IntegrityCheck(NamedTuple):
    """Representa un criterio de validación para un archivo específico."""
    reason: str
    predicate: ViolationPredicate


def is_running_as_admin() -> bool:
    """Verifica si el proceso actual tiene privilegios elevados (Administrador)."""
    if os.name != 'nt':
        return os.getuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _is_permission_denied(e: Exception) -> bool:
    """Determina si el error de acceso es por restricciones del SO (Win/Unix)."""
    return isinstance(e, (PermissionError, OSError)) and getattr(e, 'errno', 0) in (13, 5)


def _has_invalid_chars(path_str: str) -> bool:
    """Detecta caracteres nulos, secuencias de control o rutas de dispositivos inválidas."""
    norm = os.path.normpath(path_str)
    return bool("\0" in path_str or re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E]', path_str) or
                norm.startswith(r"\\?") or norm.startswith(r"\\."))


def _is_reserved_device_name(name: str) -> bool:
    """Verifica colisiones con dispositivos de E/S reservados (ej. CON, NUL)."""
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))


def _has_alternate_data_stream(path: Path) -> bool:
    """Detecta flujos de datos NTFS que podrían ocultar contenido malicioso."""
    return ":" in path.name and len(path.name.split(":")) > 2


@lru_cache(maxsize=2048)
def _is_system_or_hidden(path: Path) -> bool:
    """Consulta los atributos de archivo Win32; devuelve True si es de sistema u oculto."""
    if os.name != 'nt':
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & (0x02 | 0x04))
    except (OSError, AttributeError, TypeError, PermissionError):
        return False


@lru_cache(maxsize=2048)
def _is_reparse_point(path: Path) -> bool:
    """Identifica junctions o symlinks; son puntos de riesgo para recursión no deseada."""
    if os.name != 'nt':
        return path.is_symlink()
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & 0x400)
    except (OSError, AttributeError, TypeError, PermissionError):
        return False


def _is_file_in_use(path: Path) -> bool:
    """Intenta abrir el archivo en modo exclusivo para verificar si está bloqueado."""
    if not path.exists() or not path.is_file():
        return False
    try:
        fd = os.open(path, os.O_RDWR | os.O_EXCL)
        os.close(fd)
        return False
    except OSError as e:
        if hasattr(e, 'winerror') and e.winerror == 32:
            return True
        return False


def _check_file_integrity(p: Path) -> None:
    """
    Realiza una batería de pruebas de integridad.
    La verificación es fail-fast: la primera condición que no se cumpla
    lanza un UnsafePathError para proteger el estado del sistema.
    """
    if not p.exists():
        raise UnsafePathError(f"El archivo {p.name} ya no existe.")
    if len(p.parts) > 32:
        raise UnsafePathError("Ruta demasiado profunda.")

    def _safe_stat(path: Path) -> os.stat_result:
        try:
            return path.stat()
        except OSError:
            raise UnsafePathError(f"No se pudo acceder a metadatos de {path.name}")

    violation_checks: list[_IntegrityCheck] = [
        _IntegrityCheck("inaccesible", lambda: not os.access(p, os.W_OK)),
        _IntegrityCheck("punto de reparse", lambda: _is_reparse_point(p)),
        _IntegrityCheck("solo lectura", lambda: _is_readonly(p)),
        _IntegrityCheck("en uso", lambda: _is_file_in_use(p)),
        _IntegrityCheck("sistema/oculto", lambda: _is_system_or_hidden(p)),
        _IntegrityCheck("hard link detectado", lambda: p.is_file() and _safe_stat(p).st_nlink > 1),
        _IntegrityCheck("ADS (flujos alternativos)", lambda: _has_alternate_data_stream(p)),
        _IntegrityCheck("archivo vacío", lambda: p.is_file() and _safe_stat(p).st_size == 0)
    ]

    for check in violation_checks:
        if check.predicate():
            raise UnsafePathError(f"Operación denegada en {p.name}: {check.reason}.")


@lru_cache(maxsize=2048)
def _is_readonly(path: Path) -> bool:
    """Verifica si el bit de solo lectura está activo en el sistema de archivos."""
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=4096)
def normalize(path: PathLike) -> Path:
    """Transforma una ruta en su forma absoluta, resolviendo symlinks y validando longitud."""
    if path is None:
        raise ValueError("Ruta nula recibida.")
    
    path_str = str(path).strip()
    if not path_str:
        raise ValueError("Entrada de ruta vacía.")
    
    if len(path_str) > 260:
        raise ValueError("Longitud de ruta excedida.")
        
    try:
        return Path(path_str).expanduser().resolve()
    except (OSError, RuntimeError) as e:
        if _is_permission_denied(e):
            raise ValueError("Acceso denegado durante la normalización.")
        raise ValueError(f"Error al normalizar: {e}")


def is_drive_root(path: PathLike) -> bool:
    """Verifica si la ruta proporcionada es la raíz de una unidad (ej. C:\\)."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError):
        return True


@lru_cache(maxsize=2048)
def is_protected_path(path: PathLike) -> bool:
    """Valida si la ruta reside en carpetas de sistema o es una ubicación crítica."""
    if not path:
        return True
    
    try:
        p = normalize(path)
        p_parts = p.parts
        if any(p_parts[:len(root)] == root for root in _SYSTEM_ROOT_PARTS):
            return True
        if any(part.lower() in PROTECTED_DIR_NAMES for part in p_parts):
            return True
        if p.exists() and _is_reparse_point(p):
            return True
        return p == Path(p.anchor)
    except (PermissionError, OSError, ValueError, TypeError, RuntimeError):
        return True 


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Comprueba si 'child' es hijo lógico de 'parent' tras normalización."""
    if child is None or parent is None:
        return False
    try:
        c, p = normalize(child), normalize(parent)
        if allow_equal and c == p:
            return True
        return p in c.parents
    except (ValueError, TypeError, OSError, RuntimeError):
        return False


@lru_cache(maxsize=2048)
def is_sensitive_file(path: PathLike) -> bool:
    """Filtra archivos cuya extensión los identifica como críticos para el sistema."""
    try:
        return Path(str(path)).suffix.lower() in SENSITIVE_EXTENSIONS
    except (TypeError, ValueError, OSError):
        return True 


def _validate_basic_path_safety(p: Path, path_str: str) -> None:
    """Verifica estructuralmente que la ruta no sea un riesgo de inyección o traversal."""
    if p.exists() and _is_reparse_point(p):
        raise UnsafePathError("La ruta apunta a un enlace simbólico o punto de unión.")

    if p.exists() and p.parent and not p.parent.exists():
        raise UnsafePathError("Directorio padre inaccesible.")

    if any(part in ("..", "...") for part in path_str.replace("/", os.sep).split(os.sep)):
        raise UnsafePathError("Intento de path traversal detectado.")

    if _has_invalid_chars(path_str) or _is_reserved_device_name(p.name):
        raise UnsafePathError("Nombre de ruta o dispositivo inválido.")
    
    if path_str.startswith(("\\\\", "//")):
        raise UnsafePathError("Rutas de red (UNC) no permitidas.")

    if not p.anchor or not os.path.exists(p.anchor):
        raise UnsafePathError("Unidad o punto de montaje no disponible.")


def _validate_boundary_conditions(p: Path, base_dir: PathLike | None) -> None:
    """Asegura que la operación no escape del directorio base o toque el directorio de la app."""
    if base_dir and not is_within_directory(p, base_dir, allow_equal=True):
        raise UnsafePathError("Operación fuera del directorio base permitido.")
    
    if is_within_directory(p, Path.cwd(), allow_equal=True):
        raise UnsafePathError("Operación denegada en el directorio de ejecución.")

    if is_drive_root(p) or is_protected_path(p):
        raise UnsafePathError("Ruta de sistema protegida.")


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: PathLike | None = None) -> Path:
    """Validador principal que debe llamarse antes de realizar cualquier cambio en disco."""
    if path is None:
        raise UnsafePathError("Ruta nula recibida para validación.")

    try:
        p = normalize(path)
        path_str = str(path)
    except (ValueError, TypeError) as e:
        raise UnsafePathError(f"Ruta inválida o mal formada: {e}")

    _validate_basic_path_safety(p, path_str)
    _validate_boundary_conditions(p, base_dir)
    
    if p.exists():
        _check_file_integrity(p)
    
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError("Extensión de archivo sensible.")
    
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Wrapper booleano: devuelve True solo si la ruta es apta para modificación."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError):
        return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Retorna solo las rutas que superan todas las pruebas de seguridad."""
    valid: list[Path] = []
    if paths is None: return valid
    for p in paths:
        if p is not None and is_safe_to_modify(p, allow_sensitive=allow_sensitive):
            try:
                valid.append(normalize(p))
            except (TypeError, ValueError, OSError):
                continue
    return valid


def describe_protection(path: PathLike) -> str:
    """Explica el motivo técnico del bloqueo de una ruta específica."""
    if path is None: return "Ruta nula."
    try:
        p = normalize(path)
        raw_str = str(path)
    except (TypeError, ValueError):
        return "Ruta mal formada."

    if raw_str.startswith(("\\\\", "//")): return f"'{raw_str}' es ruta de red."
    if is_drive_root(p): return f"'{p}' es raíz de unidad."
    if is_protected_path(p): return f"'{p}' protegida por sistema."
    if p.exists():
        if not os.access(p, os.W_OK): return f"'{p}' sin permisos de escritura."
        if _is_readonly(p): return f"'{p}' es solo lectura."
        if _is_file_in_use(p): return f"'{p}' en uso por otro proceso."
        if _is_system_or_hidden(p): return f"'{p}' atributo oculto/sistema."
        if _has_alternate_data_stream(p): return f"'{p}' contiene ADS."
        if p.is_file() and p.stat().st_size == 0: return f"'{p}' es un archivo vacío."
    if is_sensitive_file(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
