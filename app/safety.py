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
from enum import Enum, auto, IntEnum
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Final, NamedTuple, Callable, TypeGuard
from functools import lru_cache
import unicodedata

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

class SafetyValidationErrorCode(IntEnum):
    """Códigos de error para diagnósticos específicos en fallos de seguridad."""
    GENERIC = 0
    NULL_CHAR = 1
    INVALID_CHARS = 2
    RESERVED_NAME = 3
    UNC_PATH = 4
    PATH_TOO_LONG = 5
    OUT_OF_BOUNDS = 6
    ROOT_ACCESS = 7
    PROTECTED_SYSTEM_PATH = 8
    REPARSE_POINT_DETECTED = 9
    FILE_IN_USE = 10
    SENSITIVE_EXTENSION = 11
    HARD_LINK_DETECTED = 12
    ACCESS_DENIED = 13
    IO_ERROR = 14
    RELATIVE_PATH_NOT_ALLOWED = 15
    SUSPICIOUS_ENCODING = 16

class UnsafePathError(Exception):
    """Lanzada cuando una operación intenta manipular rutas protegidas."""
    def __init__(self, message: str, code: SafetyValidationErrorCode = SafetyValidationErrorCode.GENERIC):
        super().__init__(f"[{code.name}] {message}")
        self.code = code

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
    INVALID_TYPE = "tipo de archivo no soportado"


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

_SYSTEM_ROOT_PATHS_STR: Final[tuple[str, ...]] = tuple(p.lower() for p in _SYSTEM_ROOT_PATHS)

_RESERVED_NAMES_PATTERN: Final[re.Pattern] = re.compile(
    r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$', re.IGNORECASE
)


class _IntegrityCheck(NamedTuple):
    """Define una regla de seguridad para archivos."""
    reason: ProtectionReason
    predicate: ViolationPredicate


class _CheckResult(NamedTuple):
    """Resultado del chequeo de integridad para fines de reporte."""
    is_safe: bool
    reason: ProtectionReason | None = None


@lru_cache(maxsize=1)
def is_running_as_admin() -> bool:
    """Verifica si el proceso actual posee privilegios elevados usando llamadas a WinAPI."""
    if os.name != 'nt':
        try:
            return os.geteuid() == 0
        except (AttributeError, OSError):
            return False
    try:
        shell32 = ctypes.windll.shell32
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False


def _has_invalid_chars(path_str: str | None) -> bool:
    """Detecta caracteres de control y no imprimibles que Windows rechaza en nombres de archivo."""
    if not isinstance(path_str, str) or not path_str: 
        return True
    return bool(re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E]', path_str))


@lru_cache(maxsize=128)
def _is_reserved_device_name(name: str) -> bool:
    """Valida si el nombre coincide con dispositivos legacy de DOS/Windows (ej: CON, LPT1)."""
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))


@lru_cache(maxsize=512)
def _has_alternate_data_stream(path_name: str) -> bool:
    """Detecta la presencia de NTFS ADS (Alternative Data Streams) usando el separador ':'."""
    return ":" in path_name and len(path_name.split(":")) > 2


@lru_cache(maxsize=2048)
def _is_system_or_hidden(path_str: str | None) -> bool:
    """Verifica mediante la estructura de atributos de archivo si es oculto o de sistema."""
    if not path_str: return False
    try:
        path = Path(path_str)
        st = path.lstat()
        attrs = getattr(st, 'st_file_attributes', 0)
        return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_OFFLINE))
    except (AttributeError, OSError, FileNotFoundError):
        return False 


@lru_cache(maxsize=2048)
def _is_reparse_point(path_str: str) -> bool:
    """Determina si un archivo es un punto de reparse (Junction o Symlink) vía WinAPI."""
    if os.name != 'nt':
        return os.path.islink(path_str)
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path_str)
        if attrs == 0xFFFFFFFF: return False
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError):
        return False


@lru_cache(maxsize=1024)
def _is_file_in_use(path_str: str) -> bool:
    """Intenta abrir un handle en modo lectura exclusiva para determinar si un proceso está bloqueando el archivo."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str:
        return False
    if not os.path.exists(path_str):
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(path_str, 0x80000000, 0, None, 3, 0x00000080, None)
        if handle == -1 or handle == 0xFFFFFFFF: 
            return True
        kernel32.CloseHandle(handle)
        return False
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError):
        return True


def _is_sensitive_extension(path: Path) -> bool:
    """Verifica si la extensión del archivo está listada como crítica/ejecutable."""
    return path.suffix.lower() in SENSITIVE_EXTENSIONS


_VALIDATORS: Final[list[_IntegrityCheck]] = [
    _IntegrityCheck(ProtectionReason.REPARSE_POINT, lambda p, _: _is_reparse_point(str(p))),
    _IntegrityCheck(ProtectionReason.READ_ONLY, lambda _, st: not bool(st.st_mode & stat.S_IWRITE)),
    _IntegrityCheck(ProtectionReason.IN_USE, lambda p, _: _is_file_in_use(str(p))),
    _IntegrityCheck(ProtectionReason.SYSTEM_HIDDEN, lambda p, _: _is_system_or_hidden(str(p))),
    _IntegrityCheck(ProtectionReason.HARD_LINK, lambda p, st: p.is_file() and st.st_nlink > 1),
    _IntegrityCheck(ProtectionReason.ADS, lambda p, _: _has_alternate_data_stream(p.name)),
    _IntegrityCheck(ProtectionReason.EMPTY_FILE, lambda p, st: p.is_file() and st.st_size == 0),
    _IntegrityCheck(ProtectionReason.EXCESSIVE_SIZE, lambda p, st: p.is_file() and st.st_size > MAX_FILE_SIZE),
    _IntegrityCheck(ProtectionReason.MOUNT_POINT, lambda p, _: os.path.ismount(p)),
    _IntegrityCheck(ProtectionReason.INVALID_TYPE, lambda _, st: not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode))),
]


def _check_file_integrity(path: Path) -> None:
    """Ejecuta la batería de reglas de validación sobre el estado del archivo."""
    try:
        file_stat = path.stat()
    except (PermissionError, OSError) as e:
        if isinstance(e, FileNotFoundError):
            return
        code = SafetyValidationErrorCode.ACCESS_DENIED if isinstance(e, PermissionError) else SafetyValidationErrorCode.IO_ERROR
        raise UnsafePathError(f"Error de acceso en {path}: {e}", code)
        
    for rule in _VALIDATORS:
        try:
            if rule.predicate(path, file_stat):
                code = SafetyValidationErrorCode.HARD_LINK_DETECTED if rule.reason == ProtectionReason.HARD_LINK else SafetyValidationErrorCode.GENERIC
                raise UnsafePathError(f"Violación de integridad ({rule.reason.value})", code)
        except (PermissionError, OSError, AttributeError):
            continue


@lru_cache(maxsize=2048)
def _is_readonly(path_str: str) -> bool:
    """Verifica el bit de modo POSIX/Windows para determinar si el archivo es de solo lectura."""
    try:
        return not bool(Path(path_str).stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError, FileNotFoundError):
        return True


@lru_cache(maxsize=4096)
def normalize(path: PathLike) -> Path:
    """Estandariza una ruta, validando intentos de path traversal y resolviendo enlaces simbólicos."""
    if path is None: raise ValueError("Ruta nula recibida.")
    path_str = str(path).strip()
    if not path_str: raise ValueError("Entrada de ruta vacía.")
    
    if unicodedata.normalize('NFKC', path_str) != path_str:
         raise ValueError("Ruta contiene secuencias Unicode sospechosas.")
        
    try:
        p = Path(path_str)
        if ".." in p.parts: raise ValueError("Path traversal detectado.")
        
        if p.exists():
            return p.resolve()
        return Path(os.path.abspath(path_str))
    except (OSError, RuntimeError, TypeError, PermissionError) as e:
        raise ValueError(f"Error irrecuperable al normalizar {path_str}: {e}")


def is_absolute_path_allowed(path: PathLike) -> bool:
    """Valida que la ruta sea absoluta para evitar dependencias del entorno de ejecución (CWD)."""
    try:
        return Path(path).is_absolute()
    except (TypeError, ValueError):
        return False


def is_drive_root(path: PathLike) -> bool:
    """Determina si una ruta apunta a la raíz de un dispositivo de almacenamiento."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (ValueError, TypeError, OSError): return True


@lru_cache(maxsize=2048)
def _is_system_path_cached(path_str: str) -> bool:
    """Compara recursivamente los componentes de la ruta contra listas de directorios protegidos."""
    try:
        p_str_low = path_str.lower()
        if any(p_str_low.startswith(root) for root in _SYSTEM_ROOT_PATHS_STR):
            return True
        
        # Split y check de componentes optimizado usando el conjunto de protección
        return any(part in PROTECTED_DIR_NAMES for part in p_str_low.split(os.sep))
    except (OSError, RuntimeError):
        return True


@lru_cache(maxsize=2048)
def is_protected_path(path: PathLike) -> bool:
    """Verifica si la ruta se encuentra dentro de carpetas restringidas por el sistema."""
    if not path: return True
    try:
        p = normalize(path)
        p_str = str(p)
        return _is_system_path_cached(p_str) or p == Path(p.anchor)
    except (ValueError, TypeError, OSError, RuntimeError): 
        return True


def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Valida jerarquía: verifica si 'child' es un subdirectorio o archivo contenido en 'parent'."""
    if child is None or parent is None: return False
    try:
        c_path = normalize(child)
        p_path = normalize(parent)
        
        if is_drive_root(c_path) or is_protected_path(c_path):
            return False
            
        parts_c = c_path.parts
        parts_p = p_path.parts
        
        if len(parts_c) < len(parts_p): return False
        if not allow_equal and parts_c == parts_p: return False
        return parts_c[:len(parts_p)] == parts_p
    except (ValueError, TypeError, OSError, RuntimeError): return False


@lru_cache(maxsize=2048)
def is_sensitive_file(path: PathLike) -> bool:
    """Determina si un archivo es sensible basándose puramente en su extensión."""
    if not path: return True
    try:
        return _is_sensitive_extension(Path(str(path)))
    except (TypeError, ValueError, OSError): return True 


def _validate_structural_safety(target_path: Path, path_string: str) -> None:
    """Realiza chequeos estructurales antes de acceder al sistema de archivos."""
    if not isinstance(path_string, str):
        raise UnsafePathError("Ruta no es texto.", SafetyValidationErrorCode.GENERIC)
    if "\0" in path_string:
        raise UnsafePathError("Inyección de carácter nulo.", SafetyValidationErrorCode.NULL_CHAR)
    if _has_invalid_chars(path_string):
        raise UnsafePathError("Caracteres inválidos detectados.", SafetyValidationErrorCode.INVALID_CHARS)
    
    try:
        if not target_path.parts or len(target_path.parts) == 1 and target_path.parts[0] == os.sep:
             raise UnsafePathError("Ruta raíz no permitida.", SafetyValidationErrorCode.ROOT_ACCESS)

        for part in target_path.parts:
            if not part or part.strip() != part or part.endswith(('.', ' ')):
                raise UnsafePathError(f"Componente '{part}' malformado.", SafetyValidationErrorCode.INVALID_CHARS)
            
            # Detectar componentes con espacios múltiples o internos sospechosos
            if "  " in part:
                 raise UnsafePathError(f"Componente '{part}' con espacios excesivos.", SafetyValidationErrorCode.INVALID_CHARS)
            
            parts_split = part.split('.')
            name_only = parts_split[0]
            if name_only and _is_reserved_device_name(name_only):
                raise UnsafePathError(f"Nombre reservado '{part}'.", SafetyValidationErrorCode.RESERVED_NAME)
    except AttributeError:
        raise UnsafePathError("Estructura de ruta inválida.", SafetyValidationErrorCode.GENERIC)

    if path_string.startswith(("\\\\", "//")):
        raise UnsafePathError("Rutas UNC bloqueadas.", SafetyValidationErrorCode.UNC_PATH)
    if len(str(target_path)) >= MAX_PATH_LENGTH:
        raise UnsafePathError("Ruta excede MAX_PATH.", SafetyValidationErrorCode.PATH_TOO_LONG)


def _validate_boundary_conditions(target_path: Path, root_directory: PathLike | None) -> None:
    """Aplica restricciones de alcance para prevenir manipulación fuera del entorno."""
    if not is_absolute_path_allowed(target_path):
        raise UnsafePathError("Solo se permiten rutas absolutas.", SafetyValidationErrorCode.RELATIVE_PATH_NOT_ALLOWED)
        
    if root_directory and not is_within_directory(target_path, root_directory, allow_equal=True):
        raise UnsafePathError("Fuera de alcance permitido.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    
    try:
        app_root: Path = Path(os.getcwd()).resolve()
        if target_path == app_root or app_root in target_path.parents:
            raise UnsafePathError("Modificación de App denegada.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    except (OSError, RuntimeError):
        pass
        
    if is_drive_root(target_path):
        raise UnsafePathError("Acceso a raíz denegado.", SafetyValidationErrorCode.ROOT_ACCESS)
    if is_protected_path(target_path):
        raise UnsafePathError("Ruta protegida por sistema.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)
    if _is_reparse_point(str(target_path)):
        raise UnsafePathError("Nodo de reparse detectado.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)


def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: PathLike | None = None) -> Path:
    """Valida rigurosamente si una ruta es segura para ser modificada."""
    if path is None: 
        raise UnsafePathError("Ruta nula.", SafetyValidationErrorCode.GENERIC)
    
    try:
        p = normalize(path)
    except ValueError as e:
        raise UnsafePathError(f"Ruta no normalizable: {e}", SafetyValidationErrorCode.GENERIC)
    
    if not allow_sensitive and _is_sensitive_extension(p):
        raise UnsafePathError(f"Extensión bloqueada '{p.suffix}'.", SafetyValidationErrorCode.SENSITIVE_EXTENSION)

    _validate_structural_safety(p, str(p))
    _validate_boundary_conditions(p, base_dir)
    
    if p.exists():
        # Verificación final contra redirecciones por puntos de reparse reales
        if os.name == 'nt':
            try:
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.CreateFileW(str(p), 0, 0, None, 3, 0x02000000, None)
                if handle != -1:
                    buf = ctypes.create_unicode_buffer(1024)
                    kernel32.GetFinalPathNameByHandleW(handle, buf, 1024, 0)
                    kernel32.CloseHandle(handle)
                    if not Path(buf.value).resolve().as_posix().startswith(p.resolve().as_posix()[:len(str(p.parent))+1]):
                         raise UnsafePathError("Redirección detectada vía reparse.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
            except Exception: pass
            
        _check_file_integrity(p)
    elif p.parent and is_protected_path(p.parent):
        raise UnsafePathError("Directorio contenedor restringido.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)
            
    return p


def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> TypeGuard[PathLike]:
    """Predicado booleano que determina si una ruta es segura para manipulación."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, ValueError, TypeError, OSError): return False


def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Iterador optimizado que filtra una lista de rutas evitando normalizaciones redundantes."""
    results = []
    for p in paths:
        try:
            if p is not None:
                results.append(ensure_safe_to_modify(p, allow_sensitive=allow_sensitive))
        except UnsafePathError:
            continue
    return results


def describe_protection(path: PathLike) -> str:
    """Genera un reporte legible sobre el estado de seguridad de una ruta."""
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
            if _is_reparse_point(str(p)): return f"'{p}' es un punto de reparse (Junction/Symlink)."
            if os.path.ismount(p): return f"'{p}' es un punto de montaje."
            if _is_readonly(str(p)): return f"'{p}' es solo lectura."
            if _is_file_in_use(str(p)): return f"'{p}' en uso."
            if _is_system_or_hidden(str(p)): return f"'{p}' atributo oculto/sistema/offline."
            if _has_alternate_data_stream(p.name): return f"'{p}' contiene ADS."
            if not (p.is_file() or p.is_dir()): return f"'{p}' tipo de objeto no soportado."
            if p.is_file() and p.stat().st_size == 0: return f"'{p}' archivo vacío."
            if p.is_file() and p.stat().st_size > MAX_FILE_SIZE: return f"'{p}' tamaño excesivo."
            if p.is_file() and p.stat().st_nlink > 1: return f"'{p}' detectado como hard link."
    except (OSError, FileNotFoundError):
        pass
    if _is_sensitive_extension(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
