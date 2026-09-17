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
from typing import Union, Iterable, TypeAlias, Final, NamedTuple, Callable, Optional
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
FILE_ATTRIBUTE_DIRECTORY: Final[int] = 0x10
FILE_ATTRIBUTE_COMPRESSED: Final[int] = 0x800
FILE_ATTRIBUTE_ENCRYPTED: Final[int] = 0x4000
MAX_PATH_LENGTH: Final[int] = 260
MAX_FILE_SIZE: Final[int] = 2 * 1024 * 1024 * 1024  # 2GB límite de seguridad

# Constantes Win32 Drive Types
DRIVE_UNKNOWN: Final[int] = 0
DRIVE_NO_ROOT_DIR: Final[int] = 1
DRIVE_REMOVABLE: Final[int] = 2
DRIVE_FIXED: Final[int] = 3
DRIVE_REMOTE: Final[int] = 4
DRIVE_CDROM: Final[int] = 5
DRIVE_RAMDISK: Final[int] = 6

def _to_long_path(path_str: str) -> str:
    """Asegura el manejo de rutas largas en Windows prefijándolas con \\\\?\\."""
    if os.name == 'nt' and not path_str.startswith("\\\\?\\"):
        if path_str.startswith("\\\\"): return "\\\\?\\UNC" + path_str[1:]
        return "\\\\?\\" + path_str
    return path_str

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
    ADS_DETECTED = 17
    OFFLINE_FILE = 18
    ENCRYPTED_OR_COMPRESSED = 19
    VOLUME_READ_ONLY = 20
    REMOTE_DRIVE_DETECTED = 21
    REMOVABLE_DRIVE_DETECTED = 22
    KERNEL_LOCKED_FILE = 23
    WRITE_ACCESS_DENIED = 24

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
    OFFLINE = "archivo offline (nube)"
    ENCRYPTED_OR_COMPRESSED = "cifrado o comprimido"
    VOLUME_READ_ONLY = "volumen de solo lectura"
    REMOTE_DRIVE = "unidad de red detectada"
    REMOVABLE_DRIVE = "unidad extraíble detectada"
    KERNEL_LOCKED = "archivo bloqueado por kernel"
    ACCESS_WRITE = "acceso de escritura denegado"

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

_SYSTEM_ROOT_PATHS_SET: Final[frozenset[str]] = frozenset(p.lower() for p in _SYSTEM_ROOT_PATHS)

_RESERVED_NAMES_PATTERN: Final[re.Pattern] = re.compile(
    r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$', re.IGNORECASE
)

class _IntegrityCheck(NamedTuple):
    """
    Define una regla de seguridad para archivos.
    reason: Motivo de la denegación.
    predicate: Función que devuelve True si se detecta una violación.
    """
    reason: ProtectionReason
    predicate: ViolationPredicate

class _CheckResult(NamedTuple):
    """Resultado del chequeo de integridad para fines de reporte."""
    is_safe: bool
    reason: Optional[ProtectionReason] = None

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

def _has_invalid_chars(path_str: Optional[str]) -> bool:
    """Detecta caracteres de control y no imprimibles que Windows rechaza en nombres de archivo."""
    if not isinstance(path_str, str) or not path_str: 
        return True
    return bool(re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E]|[\x00-\x1f\x7f]', path_str))

@lru_cache(maxsize=128)
def _is_reserved_device_name(name: str) -> bool:
    """Valida si el nombre coincide con dispositivos legacy de DOS/Windows (ej: CON, LPT1)."""
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))

@lru_cache(maxsize=512)
def _has_alternate_data_stream(path_name: str) -> bool:
    """Detecta la presencia de NTFS ADS (Alternative Data Streams) usando el separador ':'."""
    return ":" in path_name and len(path_name.split(":")) > 2

@lru_cache(maxsize=2048)
def _is_system_or_hidden(path_str: str) -> bool:
    """Verifica mediante la estructura de atributos de archivo si es oculto o de sistema."""
    if not os.path.isabs(path_str): return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(_to_long_path(path_str))
        if attrs == 0xFFFFFFFF: return False
        return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_OFFLINE))
    except (AttributeError, OSError, FileNotFoundError, ctypes.ArgumentError, Exception):
        return False 

@lru_cache(maxsize=2048)
def _is_reparse_point(path_str: str) -> bool:
    """Determina si un archivo es un punto de reparse (Junction o Symlink) vía WinAPI."""
    if not isinstance(path_str, str) or not path_str: return False
    if os.name != 'nt':
        return os.path.islink(path_str)
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(_to_long_path(path_str))
        if attrs == 0xFFFFFFFF: return False
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError, Exception):
        return False

@lru_cache(maxsize=2048)
def _is_encrypted_or_compressed(path_str: str) -> bool:
    """Verifica atributos NTFS de compresión o cifrado."""
    if os.name != 'nt': return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(_to_long_path(path_str))
        if attrs == 0xFFFFFFFF: return False
        return bool(attrs & (FILE_ATTRIBUTE_COMPRESSED | FILE_ATTRIBUTE_ENCRYPTED))
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError, Exception):
        return False

@lru_cache(maxsize=2048)
def _is_offline(path_str: str) -> bool:
    """Verifica si el archivo está marcado como offline (ej: placeholder de nube)."""
    if os.name != 'nt': return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(_to_long_path(path_str))
        if attrs == 0xFFFFFFFF: return False
        return bool(attrs & FILE_ATTRIBUTE_OFFLINE)
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError, Exception):
        return False

@lru_cache(maxsize=1024)
def _is_file_in_use(path_str: str) -> bool:
    """Verifica si un proceso está bloqueando el archivo mediante acceso exclusivo de WinAPI."""
    if os.name != 'nt' or not os.path.isabs(path_str):
        return False
    
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateFileW(_to_long_path(path_str), 0, 0, None, 3, 0x00000080, None)
    try:
        if handle == -1: 
            return True 
        return False
    finally:
        if handle != -1:
            kernel32.CloseHandle(handle)

@lru_cache(maxsize=128)
def _is_volume_readonly(path_str: str) -> bool:
    """Verifica mediante GetVolumeInformationW si el volumen de una ruta es de solo lectura."""
    if os.name != 'nt': return False
    root = os.path.splitdrive(path_str)[0] + "\\"
    flags = ctypes.c_ulong()
    try:
        if ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0):
            return bool(flags.value & 0x80000) # FILE_READ_ONLY_VOLUME
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError):
        return False
    return False

def _is_directory_junction(path: Path) -> bool:
    """Verifica si el path es un directorio con el flag de reparse point activo."""
    if os.name != 'nt': return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(_to_long_path(str(path)))
        if attrs == 0xFFFFFFFF: return False
        return bool(attrs & FILE_ATTRIBUTE_DIRECTORY and attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError, Exception): return False

def _is_kernel_managed(path: Path) -> bool:
    """Detecta archivos de paginación o hibernación bloqueados por el sistema operativo."""
    return path.name.lower() in ("pagefile.sys", "hiberfil.sys", "swapfile.sys")

def _is_sensitive_extension(path: Path) -> bool:
    """Verifica si la extensión del archivo está listada como crítica/ejecutable."""
    return path.suffix.lower() in SENSITIVE_EXTENSIONS

# Lista de validadores de integridad aplicada secuencialmente
_VALIDATORS: Final[list[_IntegrityCheck]] = [
    _IntegrityCheck(ProtectionReason.SYMLINK, lambda p, _: p.is_symlink()),
    _IntegrityCheck(ProtectionReason.REPARSE_POINT, lambda p, _: _is_reparse_point(str(p))),
    _IntegrityCheck(ProtectionReason.KERNEL_LOCKED, lambda p, _: _is_kernel_managed(p)),
    _IntegrityCheck(ProtectionReason.READ_ONLY, lambda _, st: not bool(st.st_mode & stat.S_IWRITE)),
    _IntegrityCheck(ProtectionReason.VOLUME_READ_ONLY, lambda p, _: _is_volume_readonly(str(p))),
    _IntegrityCheck(ProtectionReason.IN_USE, lambda p, _: _is_file_in_use(str(p))),
    _IntegrityCheck(ProtectionReason.SYSTEM_HIDDEN, lambda p, _: _is_system_or_hidden(str(p))),
    _IntegrityCheck(ProtectionReason.OFFLINE, lambda p, _: _is_offline(str(p))),
    _IntegrityCheck(ProtectionReason.ENCRYPTED_OR_COMPRESSED, lambda p, _: _is_encrypted_or_compressed(str(p))),
    _IntegrityCheck(ProtectionReason.HARD_LINK, lambda p, st: p.is_file() and st.st_nlink > 1),
    _IntegrityCheck(ProtectionReason.ADS, lambda p, _: _has_alternate_data_stream(p.name)),
    _IntegrityCheck(ProtectionReason.EMPTY_FILE, lambda p, st: p.is_file() and st.st_size == 0),
    _IntegrityCheck(ProtectionReason.EXCESSIVE_SIZE, lambda p, st: p.is_file() and st.st_size > MAX_FILE_SIZE),
    _IntegrityCheck(ProtectionReason.MOUNT_POINT, lambda p, _: os.path.ismount(p)),
    _IntegrityCheck(ProtectionReason.INVALID_TYPE, lambda _, st: not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode))),
]

_REASON_TO_CODE: Final[dict[ProtectionReason, SafetyValidationErrorCode]] = {
    ProtectionReason.SYMLINK: SafetyValidationErrorCode.REPARSE_POINT_DETECTED,
    ProtectionReason.HARD_LINK: SafetyValidationErrorCode.HARD_LINK_DETECTED,
    ProtectionReason.OFFLINE: SafetyValidationErrorCode.OFFLINE_FILE,
    ProtectionReason.ENCRYPTED_OR_COMPRESSED: SafetyValidationErrorCode.ENCRYPTED_OR_COMPRESSED,
    ProtectionReason.IN_USE: SafetyValidationErrorCode.FILE_IN_USE,
    ProtectionReason.REPARSE_POINT: SafetyValidationErrorCode.REPARSE_POINT_DETECTED,
    ProtectionReason.ADS: SafetyValidationErrorCode.ADS_DETECTED,
    ProtectionReason.VOLUME_READ_ONLY: SafetyValidationErrorCode.VOLUME_READ_ONLY,
    ProtectionReason.REMOTE_DRIVE: SafetyValidationErrorCode.REMOTE_DRIVE_DETECTED,
    ProtectionReason.REMOVABLE_DRIVE: SafetyValidationErrorCode.REMOVABLE_DRIVE_DETECTED,
    ProtectionReason.KERNEL_LOCKED: SafetyValidationErrorCode.KERNEL_LOCKED_FILE,
    ProtectionReason.ACCESS_WRITE: SafetyValidationErrorCode.WRITE_ACCESS_DENIED
}

def _check_file_integrity(path: Path) -> None:
    """
    Ejecuta una batería de reglas de integridad sobre el archivo mediante predicados.
    
    El proceso verifica atributos de sistema, estado de bloqueo, streams ADS y 
    restricciones de tamaño antes de confirmar la seguridad de la operación.
    Lanza UnsafePathError ante cualquier violación detectada.
    """
    try:
        file_stat = path.stat()
    except (PermissionError, OSError) as e:
        raise UnsafePathError(f"Acceso denegado a metadatos: {path.name}", SafetyValidationErrorCode.ACCESS_DENIED)
    
    if _is_directory_junction(path):
        raise UnsafePathError(f"Junction detectada: {path.name}", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        
    for rule in _VALIDATORS:
        try:
            if rule.predicate(path, file_stat):
                code = _REASON_TO_CODE.get(rule.reason, SafetyValidationErrorCode.GENERIC)
                raise UnsafePathError(f"Integridad comprometida: {rule.reason.value}", code)
        except (AttributeError, OSError, ctypes.ArgumentError, Exception):
            # Ignoramos fallos en chequeos puntuales de metadatos del kernel para mantener la robustez
            continue

@lru_cache(maxsize=2048)
def _is_readonly(path_str: str) -> bool:
    """Verifica el bit de modo POSIX/Windows para determinar si el archivo es de solo lectura."""
    try:
        path = Path(path_str)
        if not path.exists(): return True
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError, FileNotFoundError):
        return True

def _validate_access_permissions(path: Path) -> None:
    """Valida si el proceso tiene permisos básicos de lectura/escritura sobre la ruta."""
    try:
        if path.exists() and not os.access(path, os.R_OK):
            raise UnsafePathError("Permisos de lectura denegados.", SafetyValidationErrorCode.ACCESS_DENIED)
    except (OSError, PermissionError):
        raise UnsafePathError("Acceso al sistema de archivos denegado.", SafetyValidationErrorCode.ACCESS_DENIED)

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
    """Compara la ruta normalizada contra listas de directorios protegidos de forma eficiente."""
    path_norm = os.path.normpath(path_str).lower()
    
    # 1. Verificación O(1) contra raíces del sistema (ej: C:\Windows)
    if any(path_norm.startswith(root) for root in _SYSTEM_ROOT_PATHS_SET):
        return True
        
    # 2. Verificación jerárquica de componentes sin crear conjuntos temporales en el loop
    for part in path_norm.split(os.sep):
        if part in PROTECTED_DIR_NAMES:
            return True
            
    return False

@lru_cache(maxsize=2048)
def is_protected_path(path: PathLike) -> bool:
    """Verifica si la ruta se encuentra dentro de carpetas restringidas por el sistema."""
    if not path: return True
    try:
        p = normalize(path)
        return _is_system_path_cached(str(p)) or p == Path(p.anchor)
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
            
        return os.path.commonpath([c_path, p_path]) == str(p_path) if allow_equal else os.path.commonpath([c_path, p_path]) == str(p_path) and c_path != p_path
    except (ValueError, TypeError, OSError, RuntimeError): return False

@lru_cache(maxsize=2048)
def is_sensitive_file(path: PathLike) -> bool:
    """Determina si un archivo es sensible basándose puramente en su extensión."""
    if not path: return True
    try:
        return _is_sensitive_extension(Path(str(path)))
    except (TypeError, ValueError, OSError): return True 

def _validate_structural_safety(target_path: Path, path_string: str) -> None:
    """
    Realiza chequeos preventivos de la estructura del string de la ruta.
    
    Valida la ausencia de caracteres nulos, secuencias Unicode maliciosas, 
    nombres de dispositivos reservados (DOS legacy) y longitudes de ruta 
    fuera de los límites permitidos por el sistema.
    """
    if not isinstance(path_string, str):
        raise UnsafePathError("Ruta no es texto.", SafetyValidationErrorCode.GENERIC)
    if "\0" in path_string:
        raise UnsafePathError("Inyección de carácter nulo.", SafetyValidationErrorCode.NULL_CHAR)
    if _has_invalid_chars(path_string):
        raise UnsafePathError("Caracteres inválidos detectados.", SafetyValidationErrorCode.INVALID_CHARS)
    
    if unicodedata.normalize('NFKC', path_string) != path_string:
        raise UnsafePathError("Codificación de caracteres sospechosa.", SafetyValidationErrorCode.SUSPICIOUS_ENCODING)

    if _has_alternate_data_stream(path_string):
        raise UnsafePathError("Flujo de datos alternativo detectado.", SafetyValidationErrorCode.ADS_DETECTED)
    
    try:
        if target_path.exists() and not target_path.is_absolute():
            raise UnsafePathError("Ruta inconsistente con el sistema.", SafetyValidationErrorCode.GENERIC)

        if not target_path.parts or (len(target_path.parts) == 1 and target_path.parts[0] == os.sep):
             raise UnsafePathError("Ruta raíz no permitida.", SafetyValidationErrorCode.ROOT_ACCESS)

        for part in target_path.parts:
            if not part or part.strip() != part:
                raise UnsafePathError(f"Componente '{part}' con espacios envolventes.", SafetyValidationErrorCode.INVALID_CHARS)
            
            if part.endswith(('.', ' ')):
                raise UnsafePathError(f"Componente '{part}' termina en caracter inválido.", SafetyValidationErrorCode.INVALID_CHARS)
            
            if "  " in part:
                 raise UnsafePathError(f"Componente '{part}' con espacios excesivos.", SafetyValidationErrorCode.INVALID_CHARS)
            
            part_cleaned = part.split('.')[0]
            if _is_reserved_device_name(part_cleaned):
                raise UnsafePathError(f"Nombre reservado '{part}'.", SafetyValidationErrorCode.RESERVED_NAME)
                
    except (AttributeError, TypeError, ValueError):
        raise UnsafePathError("Estructura de ruta inválida.", SafetyValidationErrorCode.GENERIC)

    if path_string.startswith(("\\\\", "//")):
        raise UnsafePathError("Rutas UNC bloqueadas.", SafetyValidationErrorCode.UNC_PATH)

def _validate_boundary_conditions(target_path: Path, root_directory: Optional[PathLike]) -> None:
    """
    Valida las condiciones de borde geográficas y de entorno del sistema.
    
    Verifica que la ruta sea absoluta, esté dentro del contexto de usuario 
    permitido, no colisione con el directorio de ejecución de la aplicación, 
    y que la unidad destino no sea de tipo remoto o extraíble.
    """
    if not is_absolute_path_allowed(target_path):
        raise UnsafePathError("Solo se permiten rutas absolutas.", SafetyValidationErrorCode.RELATIVE_PATH_NOT_ALLOWED)
        
    if root_directory and not is_within_directory(target_path, root_directory, allow_equal=True):
        raise UnsafePathError("Fuera de alcance permitido.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    
    if is_protected_path(target_path):
        raise UnsafePathError("Ruta en directorio del sistema bloqueada.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)
    
    if os.name == 'nt':
        try:
            root = target_path.anchor
            if root:
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
                if drive_type == DRIVE_NO_ROOT_DIR:
                     raise UnsafePathError("Unidad inaccesible.", SafetyValidationErrorCode.IO_ERROR)
                if drive_type == DRIVE_REMOTE:
                     raise UnsafePathError("Unidad de red bloqueada.", SafetyValidationErrorCode.REMOTE_DRIVE_DETECTED)
                if drive_type == DRIVE_REMOVABLE:
                     raise UnsafePathError("Unidad extraíble bloqueada.", SafetyValidationErrorCode.REMOVABLE_DRIVE_DETECTED)
        except (OSError, AttributeError, ctypes.ArgumentError, Exception) as e:
             raise UnsafePathError(f"Fallo al consultar unidad: {e}", SafetyValidationErrorCode.IO_ERROR)

    try:
        app_root = Path(os.getcwd()).resolve()
        if target_path == app_root or app_root in target_path.parents:
            raise UnsafePathError("Modificación de App denegada.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    except (OSError, RuntimeError, ValueError):
        pass
        
    if is_drive_root(target_path):
        raise UnsafePathError("Acceso a raíz denegado.", SafetyValidationErrorCode.ROOT_ACCESS)
    if _is_reparse_point(str(target_path)):
        raise UnsafePathError("Nodo de reparse detectado.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)

def _validate_ntfs_reparse_redirection(path: Path) -> None:
    """Verifica si la ruta real difiere del path esperado tras resolver links/junctions."""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(_to_long_path(str(path)), 0, 0, None, 3, 0x02000000, None)
        if handle != -1:
            buf = ctypes.create_unicode_buffer(1024)
            if kernel32.GetFinalPathNameByHandleW(handle, buf, 1024, 0):
                final_path = Path(buf.value).resolve()
                # Verificar redundancia tras resolución y asegurar que el destino real no escape del padre
                if final_path.drive != path.resolve().drive:
                    raise UnsafePathError("Redirección de unidad detectada.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
                if not str(final_path).startswith(str(path.parent)):
                    raise UnsafePathError("Salida de carpeta permitida vía redirección.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ctypes.ArgumentError): 
        pass

def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> Path:
    """
    Valida rigurosamente si una ruta es segura para ser modificada.
    Lanza `UnsafePathError` si la ruta viola cualquier política de seguridad.
    """
    if path is None: 
        raise UnsafePathError("Ruta nula.", SafetyValidationErrorCode.GENERIC)
    
    try:
        p = normalize(path)
    except (ValueError, TypeError) as e:
        raise UnsafePathError(f"Ruta no normalizable: {e}", SafetyValidationErrorCode.GENERIC)
    
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError(f"Extensión bloqueada '{p.suffix}'.", SafetyValidationErrorCode.SENSITIVE_EXTENSION)

    _validate_structural_safety(p, str(p))
    _validate_boundary_conditions(p, base_dir)
    _validate_access_permissions(p)
    
    if p.exists() and not os.access(p, os.W_OK):
        raise UnsafePathError(f"Acceso de escritura denegado: {p.name}", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
    
    try:
        if p.exists():
            if os.name == 'nt':
                _validate_ntfs_reparse_redirection(p)
                
            _check_file_integrity(p)
        else:
            parent = p.parent
            if parent.exists() and is_protected_path(parent):
                raise UnsafePathError("Directorio contenedor restringido.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)
            
            if os.name == 'nt':
                anchor = getattr(p, 'anchor', None)
                if anchor:
                    drive_type = ctypes.windll.kernel32.GetDriveTypeW(anchor)
                    if drive_type in (DRIVE_REMOTE, DRIVE_REMOVABLE):
                        raise UnsafePathError("Unidad no apta para modificación.", SafetyValidationErrorCode.IO_ERROR)
    except (UnsafePathError):
        raise
    except (OSError, PermissionError, AttributeError, ctypes.ArgumentError) as e:
        raise UnsafePathError(f"Fallo durante validación de integridad: {e}", SafetyValidationErrorCode.IO_ERROR)
            
    return p

def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Predicado booleano que determina si una ruta es segura para manipulación."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, ValueError, TypeError, OSError, PermissionError): 
        return False

def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una lista de rutas, preservando solo aquellas que superan las pruebas de seguridad."""
    results = []
    for p in paths:
        if p is None: continue
        try:
            results.append(ensure_safe_to_modify(p, allow_sensitive=allow_sensitive))
        except (UnsafePathError, ValueError, TypeError, OSError, PermissionError):
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
            if p.is_symlink(): return f"'{p}' es un enlace simbólico."
            if _is_reparse_point(str(p)): return f"'{p}' es un punto de reparse (Junction/Symlink)."
            if os.path.ismount(p): return f"'{p}' es un punto de montaje."
            if _is_readonly(str(p)): return f"'{p}' es solo lectura."
            if _is_volume_readonly(str(p)): return f"'{p}' pertenece a un volumen de solo lectura."
            if _is_file_in_use(str(p)): return f"'{p}' en uso."
            if _is_encrypted_or_compressed(str(p)): return f"'{p}' archivo cifrado o comprimido."
            if _is_offline(str(p)): return f"'{p}' archivo offline/nube."
            if _is_system_or_hidden(str(p)): return f"'{p}' atributo oculto/sistema."
            if _has_alternate_data_stream(p.name): return f"'{p}' contiene ADS."
            if not (p.is_file() or p.is_dir()): return f"'{p}' tipo de objeto no soportado."
            if p.is_file() and p.stat().st_size == 0: return f"'{p}' archivo vacío."
            if p.is_file() and p.stat().st_size > MAX_FILE_SIZE: return f"'{p}' tamaño excesivo."
            if p.is_file() and p.stat().st_nlink > 1: return f"'{p}' detectado como hard link."
    except (OSError, FileNotFoundError, AttributeError):
        pass
    if is_sensitive_file(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
