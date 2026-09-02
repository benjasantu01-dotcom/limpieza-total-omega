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
import time
from enum import Enum, auto
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Final, NamedTuple, Callable, TypeGuard
from functools import lru_cache

PathLike: TypeAlias = Union[str, os.PathLike]
ViolationPredicate: TypeAlias = Callable[[Path, os.stat_result], bool]

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

# Constantes de atributos de archivo Win32
FILE_ATTRIBUTE_HIDDEN: Final[int] = 0x02
FILE_ATTRIBUTE_SYSTEM: Final[int] = 0x04
FILE_ATTRIBUTE_OFFLINE: Final[int] = 0x1000
FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x400
MAX_PATH_LENGTH: Final[int] = 260
MAX_FILE_SIZE: Final[int] = 2 * 1024 * 1024 * 1024  # 2GB límite de seguridad

class UnsafePathError(Exception):
    """Lanzada cuando una operación intenta manipular rutas protegidas."""


class ProtectionReason(Enum):
    """Categorías de riesgo detectadas durante la validación de integridad."""
    INACCESSIBLE = "inaccesible"
    REPARSE_POINT = "punto de reparse"
    READ_ONLY = "solo lectura"
    IN_USE = "en uso"
    SYSTEM_HIDDEN = "sistema/oculto/offline"
    HARD_LINK = "hard link detectado"
    SYMLINK = "enlace simbólico detectado"
    ADS = "ADS (flujos alternativos)"
    EMPTY_FILE = "archivo vacío"
    EXCESSIVE_DEPTH = "profundidad excesiva"
    MOUNT_POINT = "punto de montaje detectado"
    EXCESSIVE_SIZE = "tamaño de archivo excedido"


class ValidationContext(Enum):
    """Define si la validación es puramente estructural o requiere acceso a disco."""
    STRUCTURAL = auto()
    INTEGRITY = auto()


# Directorios críticos del sistema que nunca deben ser modificados
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

# Extensiones ejecutables o de configuración consideradas riesgosas
SENSITIVE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".sys", ".dll", ".exe", ".msi", ".drv", ".ocx", ".cpl", ".efi",
    ".reg", ".pol", ".key", ".pem", ".pfx", ".p12", ".crt", ".cer",
})

_SYSTEM_ROOT_PATHS: Final[tuple[str, ...]] = tuple(
    os.path.normcase(os.environ[v]) for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
)

_RESERVED_NAMES_PATTERN: Final[re.Pattern] = re.compile(
    r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$', re.IGNORECASE
)

_INTEGRITY_CACHE: dict[str, tuple[float, bool]] = {}
CACHE_TTL: Final[float] = 2.0


class _IntegrityCheck(NamedTuple):
    """
    Define una regla de seguridad para archivos.
    
    Attributes:
        reason: El motivo técnico por el cual un archivo podría ser bloqueado.
        predicate: Función que recibe (Path, stat_result) y retorna True si es inseguro.
    """
    reason: ProtectionReason
    predicate: ViolationPredicate


class _CheckResult(NamedTuple):
    """Resultado del chequeo de integridad para fines de reporte."""
    is_safe: bool
    reason: ProtectionReason | None = None


def is_running_as_admin() -> bool:
    """
    Verifica si el proceso actual tiene privilegios de administrador.
    
    Returns:
        bool: True si el proceso tiene privilegios elevados, False en caso contrario.
    """
    if os.name != 'nt':
        try:
            return os.geteuid() == 0
        except (AttributeError, OSError):
            return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _has_invalid_chars(path_str: str | None) -> bool:
    """
    Valida la ausencia de caracteres de control o marcas RTL en la ruta.
    
    Args:
        path_str: Cadena de la ruta a validar.
        
    Returns:
        bool: True si detecta caracteres de riesgo (RTL, control, etc).
    """
    if not isinstance(path_str, str) or not path_str: 
        return True
    return bool(re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E]', path_str))


def _is_reserved_device_name(name: str) -> bool:
    """Comprueba si el nombre del archivo colisiona con dispositivos reservados de Windows."""
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))


def _has_alternate_data_stream(path: Path) -> bool:
    """Detecta flujos de datos alternativos (ADS) mediante la presencia de ':' adicional."""
    return ":" in path.name and len(path.name.split(":")) > 2


@lru_cache(maxsize=2048)
def _is_system_or_hidden(path: Path) -> bool:
    """
    Verifica si el archivo tiene atributos de sistema u oculto.
    Usa el campo `st_file_attributes` disponible en Windows.
    """
    try:
        return bool(path.lstat().st_file_attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_OFFLINE))
    except (AttributeError, OSError):
        return False 


@lru_cache(maxsize=2048)
def _is_junction(path: Path) -> bool:
    """
    Identifica puntos de unión (Junctions) mediante llamada a WinAPI kernel32.
    Evita la recursión en rutas que apuntan fuera del directorio actual.
    """
    if os.name != 'nt': return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return bool(attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT))
    except (AttributeError, OSError, TypeError):
        return False


@lru_cache(maxsize=2048)
def _is_reparse_point(path: Path) -> bool:
    """
    Determina si la ruta es un punto de reparse (Junction o Symlink).
    Combina atributos nativos y validación por `pathlib`.
    """
    try:
        return bool(path.lstat().st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT) or _is_junction(path)
    except (AttributeError, OSError):
        return path.is_symlink()


@lru_cache(maxsize=1024)
def _is_file_in_use(path_str: str) -> bool:
    """
    Verifica si un archivo está bloqueado por otro proceso.
    Intenta abrir el archivo con acceso exclusivo de lectura/escritura controlado.
    """
    if os.name != 'nt' or not isinstance(path_str, str):
        return False
    try:
        # GENERIC_READ = 0x80000000, FILE_SHARE_READ = 0x00000001
        handle = ctypes.windll.kernel32.CreateFileW(
            path_str, 0x80000000, 0x00000001, None, 3, 0x00000080, None
        )
        if handle == -1 or handle == 0xFFFFFFFF: return True
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    except (AttributeError, OSError, PermissionError, TypeError):
        return True


def _is_sensitive_extension(path: Path) -> bool:
    """Determina si la extensión del archivo está en la lista de riesgo SENSITIVE_EXTENSIONS."""
    return path.suffix.lower() in SENSITIVE_EXTENSIONS


_VALIDATORS: Final[list[_IntegrityCheck]] = [
    _IntegrityCheck(ProtectionReason.REPARSE_POINT, lambda p, _: _is_reparse_point(p)),
    _IntegrityCheck(ProtectionReason.READ_ONLY, lambda _, st: not bool(st.st_mode & stat.S_IWRITE)),
    _IntegrityCheck(ProtectionReason.IN_USE, lambda p, _: _is_file_in_use(str(p))),
    _IntegrityCheck(ProtectionReason.SYSTEM_HIDDEN, lambda p, _: _is_system_or_hidden(p)),
    _IntegrityCheck(ProtectionReason.HARD_LINK, lambda p, st: p.is_file() and st.st_nlink > 1),
    _IntegrityCheck(ProtectionReason.ADS, lambda p, _: _has_alternate_data_stream(p)),
    _IntegrityCheck(ProtectionReason.EMPTY_FILE, lambda p, st: p.is_file() and st.st_size == 0),
    _IntegrityCheck(ProtectionReason.EXCESSIVE_SIZE, lambda p, st: p.is_file() and st.st_size > MAX_FILE_SIZE),
    _IntegrityCheck(ProtectionReason.MOUNT_POINT, lambda p, _: os.path.ismount(p)),
]


def _check_file_integrity(path: Path) -> None:
    """
    Realiza un chequeo exhaustivo de integridad.
    
    Verifica el estado del archivo mediante una serie de predicados de seguridad. 
    Usa caché con TTL para optimizar el rendimiento en iteraciones de bucle frecuentes.
    
    Args:
        path: Ruta a verificar.
        
    Raises:
        UnsafePathError: Si el archivo incumple cualquier regla de seguridad.
    """
    path_key = str(path)
    now = time.monotonic()
    
    if path_key in _INTEGRITY_CACHE:
        timestamp, is_safe = _INTEGRITY_CACHE[path_key]
        if now - timestamp < CACHE_TTL:
            if not is_safe: raise UnsafePathError("Operación denegada (cache hit).")
            return

    if not path.exists():
        raise UnsafePathError("La ruta no existe.")

    try:
        # Usamos lstat para evitar seguir enlaces simbólicos durante el chequeo
        file_stat = path.lstat()
    except (PermissionError, OSError):
        raise UnsafePathError("Error al acceder a los metadatos del archivo.")

    for rule in _VALIDATORS:
        try:
            if rule.predicate(path, file_stat):
                _INTEGRITY_CACHE[path_key] = (now, False)
                raise UnsafePathError(f"Operación denegada: {rule.reason.value}")
        except (OSError, AttributeError, TypeError):
            continue
            
    _INTEGRITY_CACHE[path_key] = (now, True)


@lru_cache(maxsize=2048)
def _is_readonly(path: Path) -> bool:
    """Valida si el archivo carece de permisos de escritura a nivel de sistema de archivos."""
    try:
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError):
        return True


@lru_cache(maxsize=4096)
def normalize(path: PathLike) -> Path:
    """Estandariza rutas, resuelve enlaces y aplica límites de seguridad de longitud."""
    if path is None: raise ValueError("Ruta nula recibida.")
    try:
        path_str = str(path).strip()
    except (TypeError, ValueError): raise ValueError("Entrada no convertible a string.")

    if not path_str: raise ValueError("Entrada de ruta vacía.")
        
    try:
        p = Path(path_str)
        if ".." in p.parts: raise ValueError("Path traversal detectado.")
        resolved = p.resolve()
        
        # Validación de seguridad: Verificar que ninguno de los padres sea un enlace
        current = p
        while current != current.parent:
            if _is_reparse_point(current):
                raise ValueError(f"Acceso restringido: componente {current} es un punto de reparse.")
            current = current.parent
            
        return resolved
    except (OSError, RuntimeError, TypeError) as e:
        raise ValueError(f"Error irrecuperable al normalizar {path_str}: {e}")


def is_drive_root(path: PathLike) -> bool:
    """Determina si la ruta normalizada es la raíz (unidad) del sistema."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError): return True


@lru_cache(maxsize=2048)
def is_protected_path(path: PathLike) -> bool:
    """Verifica si la ruta reside dentro de directorios de sistema protegidos."""
    if not path: return True
    try:
        p = normalize(path)
        norm_case = os.path.normcase(str(p))
        if any(norm_case.startswith(root) for root in _SYSTEM_ROOT_PATHS):
            return True
        if any(part.lower() in PROTECTED_DIR_NAMES for part in p.parts):
            return True
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError, RuntimeError): 
        return True


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Valida si una ruta hijo es descendiente de un directorio padre dado."""
    if child is None or parent is None: return False
    try:
        c_path = normalize(child)
        p_path = normalize(parent)
        
        if is_drive_root(c_path) or is_protected_path(c_path):
            return False
            
        return os.path.commonpath([str(c_path), str(p_path)]) == str(p_path) if allow_equal else \
               (c_path != p_path and os.path.commonpath([str(c_path), str(p_path)]) == str(p_path))
    except (ValueError, TypeError, OSError, RuntimeError): return False


@lru_cache(maxsize=2048)
def is_sensitive_file(path: PathLike) -> bool:
    """Wrapper para verificar si un archivo tiene una extensión crítica."""
    if not path: return True
    try:
        return _is_sensitive_extension(Path(str(path)))
    except (TypeError, ValueError, OSError): return True 


def _validate_structural_safety(target_path: Path, path_string: str) -> None:
    """
    Valida la integridad técnica de la cadena de la ruta contra:
    - Caracteres de control no permitidos (RTL/Unicode).
    - Nombres reservados de dispositivos Windows (CON, PRN, etc).
    - Rutas de red UNC (seguridad contra ejecución remota).
    - Límites de longitud MAX_PATH de Windows.
    """
    if _has_invalid_chars(path_string):
        raise UnsafePathError("La ruta contiene caracteres inválidos o no soportados.")
    
    # Validación recursiva de nombres reservados en los componentes de la ruta
    for part in target_path.parts:
        if _is_reserved_device_name(part):
            raise UnsafePathError(f"El nombre '{part}' es un dispositivo reservado por el sistema.")

    if path_string.startswith(("\\\\", "//")):
        raise UnsafePathError("Operación en rutas de red (UNC) bloqueada por seguridad.")
    if len(str(target_path)) >= MAX_PATH_LENGTH:
        raise UnsafePathError(f"Ruta demasiado larga (límite {MAX_PATH_LENGTH} caracteres).")


def _validate_boundary_conditions(target_path: Path, root_directory: PathLike | None) -> None:
    """
    Valida las restricciones lógicas y de alcance (scope) del sistema.
    
    Verifica que la ruta esté dentro del sandbox definido, no sea una raíz de
    unidad, no esté protegida por sistema y no sea un nodo de reparse.
    
    Args:
        target_path: Ruta a validar.
        root_directory: Directorio base permitido para el alcance.
        
    Raises:
        UnsafePathError: Si la ruta infringe los límites definidos.
    """
    if root_directory and not is_within_directory(target_path, root_directory, allow_equal=True):
        raise UnsafePathError("La ruta objetivo está fuera del alcance definido por el usuario.")
    if is_within_directory(target_path, Path.cwd(), allow_equal=True):
        raise UnsafePathError("No se permite modificar archivos en el directorio raíz de la aplicación.")
    if is_drive_root(target_path):
        raise UnsafePathError("Operación denegada: intento de acceso a la raíz de una unidad lógica.")
    if is_protected_path(target_path):
        raise UnsafePathError("Operación denegada: la ruta se encuentra en un directorio de sistema protegido.")
    if _is_reparse_point(target_path):
        raise UnsafePathError("Seguridad denegada: nodo de reparse (junction/symlink) detectado.")


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: PathLike | None = None) -> Path:
    """
    Valida si una ruta puede ser modificada, levantando excepciones ante riesgos.
    
    Esta función es el punto de entrada crítico antes de realizar cualquier cambio
    en el sistema de archivos (borrar o mover). Implementa validación estructural,
    geográfica y de integridad.
    
    Raises:
        UnsafePathError: Si la ruta no es segura, está protegida o viola restricciones.
    """
    if path is None: raise UnsafePathError("Ruta nula recibida para validación.")

    # Validación previa antes de invocar Path.resolve() para prevenir fallos de sistema
    if not isinstance(path, (str, Path)) or str(path).strip() == "":
        raise UnsafePathError("Ruta vacía o tipo no soportado.")

    try:
        p = normalize(path)
    except ValueError as e:
        raise UnsafePathError(f"Ruta inválida: {e}")
    
    _validate_structural_safety(p, str(p))
    _validate_boundary_conditions(p, base_dir)
    
    try:
        exists = p.exists() or os.path.lexists(p)
    except OSError:
        raise UnsafePathError("Error al verificar existencia de ruta.")

    if exists:
        try:
            is_file_or_dir = p.is_file() or p.is_dir()
        except OSError:
            raise UnsafePathError("Error al validar el tipo de objeto en la ruta.")
            
        if not is_file_or_dir:
            raise UnsafePathError("Tipo de objeto no soportado para modificación.")
        try:
            if not os.access(p, os.W_OK):
                raise UnsafePathError("No se dispone de permisos de escritura sobre el archivo.")
        except OSError:
            raise UnsafePathError("Error de sistema al verificar permisos de acceso.")
        _check_file_integrity(p)
    else:
        # Validación preventiva para rutas no existentes
        if is_protected_path(p.parent):
            raise UnsafePathError("Escritura bloqueada: el directorio contenedor está protegido.")
    
    if not allow_sensitive and _is_sensitive_extension(p):
        raise UnsafePathError(f"La extensión '{p.suffix}' está marcada como sensible.")
            
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> TypeGuard[PathLike]:
    """
    Retorna True si una ruta es segura para ser modificada; 
    debe usarse en condiciones `if` dentro de los módulos de procesamiento.
    """
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, TypeError, ValueError, OSError): return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """
    Filtra una colección de rutas reteniendo solo aquellas que superan el control de seguridad.
    Resulta ideal para pre-procesar listas de archivos antes de su manipulación masiva.
    """
    results = []
    for p in paths:
        if p is not None and is_safe_to_modify(p, allow_sensitive=allow_sensitive):
            try: results.append(normalize(p))
            except (ValueError, TypeError, OSError): continue
    return results


def describe_protection(path: PathLike) -> str:
    """Retorna una descripción legible sobre por qué una ruta no es segura para modificar."""
    if path is None: return "Ruta nula."
    try:
        p = normalize(path)
        raw_str = str(path)
    except (TypeError, ValueError): return "Ruta mal formada."

    if raw_str.startswith(("\\\\", "//")): return f"'{raw_str}' es ruta de red."
    if is_drive_root(p): return f"'{p}' es raíz de unidad."
    if is_protected_path(p): return f"'{p}' protegida por sistema."
    try:
        if p.exists():
            if len(str(p)) >= MAX_PATH_LENGTH: return f"'{p}' longitud excesiva."
            if not os.access(p, os.W_OK): return f"'{p}' sin permisos de escritura."
            if os.path.islink(p): return f"'{p}' es un enlace simbólico."
            if os.path.ismount(p): return f"'{p}' es un punto de montaje."
            if _is_readonly(p): return f"'{p}' es solo lectura."
            if _is_file_in_use(str(p)): return f"'{p}' en uso por otro proceso."
            if _is_system_or_hidden(p): return f"'{p}' atributo oculto/sistema/offline."
            if _has_alternate_data_stream(p): return f"'{p}' contiene ADS."
            if p.is_file() and p.stat().st_size == 0: return f"'{p}' es un archivo vacío."
            if p.is_file() and p.stat().st_size > MAX_FILE_SIZE: return f"'{p}' tamaño excesivo."
    except OSError:
        pass
    if _is_sensitive_extension(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
