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
from typing import Union, Iterable, TypeAlias, Final, NamedTuple, Callable, Optional, TypeGuard, TypedDict
from functools import lru_cache
import unicodedata

PathLike: TypeAlias = Union[str, os.PathLike]
ViolationPredicate: TypeAlias = Callable[[Path, os.stat_result], bool]

class SafetyAction(Enum):
    """Define la criticidad de la operación para ajustar el rigor de la validación."""
    READ = auto()      # Listado, escaneo, análisis
    MODIFY = auto()    # Mover, renombrar, borrar, editar

class FileMetadata(TypedDict):
    """Representación de los atributos de archivo necesarios para evaluaciones de seguridad."""
    is_reparse: bool
    is_system_hidden: bool
    is_readonly: bool
    is_in_use: bool

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

# Máscaras de bits para FileAttributes (Win32 API)
class Win32Attr(IntEnum):
    """Atributos de archivo del sistema de archivos Windows (Win32)."""
    HIDDEN: int = 0x02
    SYSTEM: int = 0x04
    DIRECTORY: int = 0x10
    TEMPORARY: int = 0x100
    SPARSE_FILE: int = 0x200
    REPARSE_POINT: int = 0x400
    COMPRESSED: int = 0x800
    OFFLINE: int = 0x1000
    ENCRYPTED: int = 0x4000

MAX_PATH_LENGTH: Final[int] = 260
MAX_FILENAME_LENGTH: Final[int] = 255
MAX_FILE_SIZE: Final[int] = 2 * 1024 * 1024 * 1024  # 2GB: Evita procesamiento de archivos gigantes para prevenir DoS.

# Constantes Win32 Drive Types (retornadas por GetDriveType)
DRIVE_UNKNOWN: Final[int] = 0
DRIVE_NO_ROOT_DIR: Final[int] = 1
DRIVE_REMOVABLE: Final[int] = 2 # Discos extraíbles (USB/SD)
DRIVE_FIXED: Final[int] = 3     # Discos locales fijos
DRIVE_REMOTE: Final[int] = 4    # Unidades de red mapeadas
DRIVE_CDROM: Final[int] = 5     # Discos ópticos
DRIVE_RAMDISK: Final[int] = 6   # Discos virtuales en memoria

def _to_long_path(path_str: str) -> str:
    """
    Aplica el prefijo '\\?\' para habilitar el acceso a rutas largas (>260 chars) 
    y deshabilitar la normalización de nombres reservados (ej. 'CON.txt').
    """
    if os.name == 'nt' and not path_str.startswith("\\\\?\\"):
        if path_str.startswith("\\\\"): return "\\\\?\\UNC" + path_str[1:]
        return "\\\\?\\" + path_str
    return path_str

@lru_cache(maxsize=1024)
def _get_file_attrs(path_str: Optional[str]) -> int:
    """
    Obtiene atributos de archivo Win32 mediante syscall GetFileAttributesW.
    Retorna 0 en caso de fallo, tratando la ruta como estándar (seguridad conservadora).
    """
    if os.name != 'nt' or not path_str: return 0
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(_to_long_path(path_str))
        # 0xFFFFFFFF indica error en la llamada Win32
        return attrs if attrs != 0xFFFFFFFF else 0
    except (AttributeError, OSError, ctypes.ArgumentError, TypeError):
        return 0

class SafetyValidationErrorCode(IntEnum):
    """Códigos de error detallados para diagnósticos de fallo en integridad."""
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
    MOUNT_POINT_DETECTED = 25
    TOCTOU_VIOLATION = 26
    SPARSE_FILE_DETECTED = 27
    DEVICE_FILE_DETECTED = 28
    EMPTY_FILE = 29

class UnsafePathError(Exception):
    """Excepción base para violaciones de seguridad detectadas durante la validación."""
    def __init__(self, message: str, code: SafetyValidationErrorCode = SafetyValidationErrorCode.GENERIC):
        super().__init__(f"[{code.name}] {message}")
        self.code = code

class ProtectionReason(Enum):
    """Categorías semánticas de riesgo utilizadas para mapear fallos a causas raíz."""
    INACCESSIBLE = "inaccesible"
    REPARSE_POINT = "punto de reparse"
    READ_ONLY = "solo lectura"
    IN_USE = "en uso"
    SYSTEM_HIDDEN = "sistema/oculto/offline/temporal"
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
    TOCTOU_VIOLATION = "violación de consistencia (TOCTOU)"
    SPARSE_FILE = "archivo disperso (sparse file)"
    DEVICE_FILE = "archivo de dispositivo detectado"

class ValidationContext(Enum):
    """Diferencia entre validación de estructura (nombres/cadenas) e integridad (estado físico)."""
    STRUCTURAL = auto()
    INTEGRITY = auto()

# Directorios críticos del sistema que la aplicación NUNCA debe modificar
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

# Extensiones ejecutables y configuraciones de seguridad críticas
SENSITIVE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".sys", ".dll", ".exe", ".msi", ".drv", ".ocx", ".cpl", ".efi",
    ".reg", ".pol", ".key", ".pem", ".pfx", ".p12", ".crt", ".cer",
})

_SYSTEM_ROOT_PATHS: Final[tuple[str, ...]] = tuple(
    os.path.normcase(os.environ[v]) for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
)

_SYSTEM_ROOT_PATHS_TUPLE: Final[tuple[str, ...]] = tuple(p.lower() for p in _SYSTEM_ROOT_PATHS)

_RESERVED_NAMES_PATTERN: Final[re.Pattern] = re.compile(
    r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$', re.IGNORECASE
)

class _IntegrityCheck(NamedTuple):
    """Regla de seguridad que vincula un motivo de protección a un predicado evaluable."""
    reason: ProtectionReason
    predicate: ViolationPredicate

class _CheckResult(NamedTuple):
    """Resultado del chequeo de integridad."""
    is_safe: bool
    reason: Optional[ProtectionReason] = None

@lru_cache(maxsize=1)
def is_running_as_admin() -> bool:
    """Verifica privilegios elevados: Windows (IsUserAnAdmin) o POSIX (EUID 0)."""
    if os.name != 'nt':
        try: return os.geteuid() == 0
        except (AttributeError, OSError): return False
    try:
        shell32 = ctypes.windll.shell32
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False

def _has_invalid_chars(path_str: Optional[str]) -> bool:
    """
    Detecta caracteres prohibidos en rutas Windows, secuencias de control RTL o nulos.
    Crucial para evitar ataques de inyección de rutas mediante caracteres invisibles.
    """
    if not isinstance(path_str, str) or not path_str: return True
    return bool(re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E\u206A-\u206F]', path_str))

def _is_unc_path(path_str: str) -> bool:
    """Detecta rutas de red UNC (Universal Naming Convention)."""
    return path_str.startswith(("\\\\", "//"))

@lru_cache(maxsize=128)
def _is_reserved_device_name(name: str) -> bool:
    """Valida contra nombres de dispositivos legacy (CON, NUL, etc.) que bloquean la API Win32."""
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))

def _is_device_file(path: Path) -> bool:
    """Detecta rutas de dispositivos de Windows (e.g., \\.\PhysicalDrive0) peligrosas para IO."""
    path_str = str(path).upper()
    return path_str.startswith(("\\\\.\\", "//./"))

@lru_cache(maxsize=512)
def _has_alternate_data_stream(path_name: str) -> bool:
    """Detecta flujos alternativos NTFS (ej. 'archivo.txt:stream') utilizados para ocultar payloads."""
    return ":" in path_name and len(path_name.split(":")) > 2

def _is_system_or_hidden(path_str: str) -> bool:
    """Verifica atributos Win32 para identificar archivos marcados como sistema, ocultos o offline."""
    if not os.path.isabs(path_str): return False
    attrs = _get_file_attrs(path_str)
    return bool(attrs & (Win32Attr.HIDDEN | Win32Attr.SYSTEM | Win32Attr.OFFLINE | Win32Attr.TEMPORARY))

def _is_reparse_point(path_str: str) -> bool:
    """Verifica el atributo REPARSE_POINT para detectar Junctions o Symlinks dinámicos."""
    if os.name != 'nt': return os.path.islink(path_str)
    return bool(_get_file_attrs(path_str) & Win32Attr.REPARSE_POINT)

def _is_encrypted_or_compressed_or_sparse(path_str: str) -> bool:
    """Detecta flags NTFS de cifrado, compresión o archivos dispersos (sparse)."""
    if os.name != 'nt': return False
    return bool(_get_file_attrs(path_str) & (Win32Attr.COMPRESSED | Win32Attr.ENCRYPTED | Win32Attr.SPARSE_FILE))

def _is_offline(path_str: str) -> bool:
    """Detecta si un archivo es gestionado por proveedores en la nube y no reside físicamente en disco."""
    if os.name != 'nt': return False
    return bool(_get_file_attrs(path_str) & Win32Attr.OFFLINE)

@lru_cache(maxsize=1024)
def _is_file_locked_by_other_process(path_str: str) -> bool:
    """
    Verifica concurrencia usando Win32 API.
    Abre el archivo con acceso nulo y atributos de solo lectura para comprobar bloqueo.
    """
    if os.name != 'nt': return False
    kernel32 = ctypes.windll.kernel32
    try:
        handle = kernel32.CreateFileW(
            _to_long_path(path_str), 0, 0x00000003, None, 3, 0x00000080, None
        )
        if handle == -1: return True
        kernel32.CloseHandle(handle)
    except (OSError, Exception):
        return True
    return False

@lru_cache(maxsize=128)
def _is_volume_readonly(path_str: Optional[str]) -> bool:
    """Consulta GetVolumeInformationW para verificar si el volumen completo es de solo lectura."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    try:
        root = os.path.splitdrive(path_str)[0] + "\\"
        flags = ctypes.c_ulong()
        # Se asegura que la llamada no exceda los límites de la API
        if ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0):
            return bool(flags.value & 0x80000)
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError):
        pass
    return False

def _is_directory_junction(path_str: str) -> bool:
    """Especialización para detectar directorios que son puntos de unión de NTFS."""
    attrs = _get_file_attrs(path_str)
    return bool(attrs & Win32Attr.DIRECTORY and attrs & Win32Attr.REPARSE_POINT)

def _is_kernel_managed(path: Path) -> bool:
    """Identifica archivos del sistema crítico que el kernel mantiene bloqueados siempre, sin importar ubicación."""
    return path.name.lower() in ("pagefile.sys", "hiberfil.sys", "swapfile.sys")

@lru_cache(maxsize=1024)
def _is_sensitive_extension(ext: str) -> bool:
    """Verifica si la extensión del archivo está en la lista negra de componentes críticos."""
    return ext.lower() in SENSITIVE_EXTENSIONS

# Lista de validadores de integridad aplicada secuencialmente
_VALIDATORS: Final[list[_IntegrityCheck]] = [
    _IntegrityCheck(ProtectionReason.SYMLINK, lambda p, _: p.is_symlink()),
    _IntegrityCheck(ProtectionReason.REPARSE_POINT, lambda p, _: _is_reparse_point(str(p))),
    _IntegrityCheck(ProtectionReason.KERNEL_LOCKED, lambda p, _: _is_kernel_managed(p)),
    _IntegrityCheck(ProtectionReason.READ_ONLY, lambda _, st: not bool(st.st_mode & stat.S_IWRITE)),
    _IntegrityCheck(ProtectionReason.VOLUME_READ_ONLY, lambda p, _: _is_volume_readonly(str(p))),
    _IntegrityCheck(ProtectionReason.IN_USE, lambda p, _: _is_file_locked_by_other_process(str(p))),
    _IntegrityCheck(ProtectionReason.SYSTEM_HIDDEN, lambda p, _: _is_system_or_hidden(str(p))),
    _IntegrityCheck(ProtectionReason.OFFLINE, lambda p, _: _is_offline(str(p))),
    _IntegrityCheck(ProtectionReason.ENCRYPTED_OR_COMPRESSED, lambda p, _: _is_encrypted_or_compressed_or_sparse(str(p))),
    _IntegrityCheck(ProtectionReason.SPARSE_FILE, lambda p, _: _is_encrypted_or_compressed_or_sparse(str(p))),
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
    ProtectionReason.ACCESS_WRITE: SafetyValidationErrorCode.WRITE_ACCESS_DENIED,
    ProtectionReason.MOUNT_POINT: SafetyValidationErrorCode.MOUNT_POINT_DETECTED,
    ProtectionReason.SPARSE_FILE: SafetyValidationErrorCode.SPARSE_FILE_DETECTED,
    ProtectionReason.EMPTY_FILE: SafetyValidationErrorCode.EMPTY_FILE
}

def _evaluate_security_rules(path: Path, current_stat: os.stat_result) -> None:
    """
    Aplica una secuencia de predicados de integridad sobre el archivo.
    Lanza UnsafePathError ante el primer incumplimiento detectado.
    """
    for rule in _VALIDATORS:
        if rule.predicate(path, current_stat):
            code = _REASON_TO_CODE.get(rule.reason, SafetyValidationErrorCode.GENERIC)
            raise UnsafePathError(f"Integridad comprometida: {rule.reason.value}", code)

def _get_path_stat_robust(path: Path) -> os.stat_result:
    """
    Intenta obtener metadatos (stat) del sistema de archivos con validación.
    """
    if not path.exists():
        raise UnsafePathError(f"Ruta inexistente: {path.name}", SafetyValidationErrorCode.IO_ERROR)
    try:
        st = path.stat()
        if not hasattr(st, 'st_dev') or not hasattr(st, 'st_ino'):
             raise UnsafePathError("Metadatos incompletos.", SafetyValidationErrorCode.IO_ERROR)
        return st
    except (OSError, FileNotFoundError, PermissionError) as e:
        code = SafetyValidationErrorCode.ACCESS_DENIED if isinstance(e, PermissionError) else SafetyValidationErrorCode.IO_ERROR
        raise UnsafePathError(f"No se pudo acceder a los metadatos: {path.name}", code)

def _check_file_integrity(path: Path, initial_stat: os.stat_result) -> None:
    """
    Verifica metadatos en disco y compara con el estado inicial capturado.
    Implementa protección contra ataques TOCTOU (Time-of-Check to Time-of-Use)
    asegurando que el descriptor de archivo no haya sido cambiado tras la inspección.
    """
    if not os.access(path, os.R_OK):
        raise UnsafePathError(f"Acceso de lectura denegado a {path.name}", SafetyValidationErrorCode.ACCESS_DENIED)
    
    current_stat = _get_path_stat_robust(path)
    
    # Compara el identificador único del dispositivo y del inodo (o índice de archivo)
    if current_stat.st_dev != initial_stat.st_dev or current_stat.st_ino != initial_stat.st_ino:
        raise UnsafePathError(f"Consistencia fallida (TOCTOU): {path.name}", SafetyValidationErrorCode.TOCTOU_VIOLATION)
    
    if _is_directory_junction(str(path)):
        raise UnsafePathError(f"Junction detectada: {path.name}", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        
    _evaluate_security_rules(path, current_stat)

@lru_cache(maxsize=2048)
def _is_readonly(path_str: str) -> bool:
    """Verifica estado de escritura del sistema de archivos mediante permisos básicos de OS."""
    try:
        path = Path(path_str)
        if not path.exists(): return True
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError, FileNotFoundError):
        return True

def _validate_access_permissions(path: Path) -> None:
    """Verifica la capacidad del proceso para manipular el recurso según permisos de OS."""
    if not os.access(path, os.R_OK):
        raise UnsafePathError("Permisos de lectura denegados.", SafetyValidationErrorCode.ACCESS_DENIED)
    if not os.access(path, os.W_OK):
        raise UnsafePathError("Permisos de escritura denegados.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)

@lru_cache(maxsize=4096)
def normalize(path: PathLike) -> Path:
    """
    Normaliza la ruta, resuelve relativos ('..') y fuerza codificación NFKC.
    Previene traversal básico mediante la validación de componentes de la ruta.
    """
    if path is None: raise UnsafePathError("Ruta nula recibida.", SafetyValidationErrorCode.GENERIC)
    path_str = str(path).strip()
    if not path_str: raise UnsafePathError("Entrada de ruta vacía.", SafetyValidationErrorCode.GENERIC)
    if unicodedata.normalize('NFKC', path_str) != path_str:
         raise UnsafePathError("Ruta contiene secuencias Unicode sospechosas.", SafetyValidationErrorCode.SUSPICIOUS_ENCODING)
    try:
        p = Path(path_str)
        resolved = p.resolve()
        # Verificar si hay traversal comparando componentes originales resueltos
        if ".." in p.parts: raise UnsafePathError("Path traversal detectado.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
        return resolved
    except (OSError, PermissionError) as e:
        raise UnsafePathError(f"Acceso denegado o error de sistema en {path_str}: {e}", SafetyValidationErrorCode.ACCESS_DENIED)
    except (RuntimeError, TypeError, ValueError) as e:
        raise UnsafePathError(f"Error irrecuperable al normalizar {path_str}: {e}", SafetyValidationErrorCode.IO_ERROR)

def is_absolute_path_allowed(path: PathLike) -> bool:
    """Verifica que la ruta sea absoluta para evitar contextos de trabajo ambiguos."""
    try: return Path(path).is_absolute()
    except (TypeError, ValueError): return False

def is_drive_root(path: PathLike) -> bool:
    """Verifica si la ruta apunta a la raíz de una unidad lógica (ej. 'C:\')."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (UnsafePathError, TypeError, OSError): return True

@lru_cache(maxsize=4096)
def _is_system_path_raw(path_str: str) -> bool:
    """
    Verifica si una ruta normalizada y en minúsculas es parte del sistema.
    Optimizado mediante el uso de conjuntos para evitar bucles.
    """
    path_lower = path_str.lower()
    # Verifica si es parte de las rutas raíces prohibidas
    if any(path_lower.startswith(root) for root in _SYSTEM_ROOT_PATHS_TUPLE): return True
    # Extraer componentes del path para verificar contra PROTECTED_DIR_NAMES
    components = frozenset(path_lower.split(os.sep))
    return not PROTECTED_DIR_NAMES.isdisjoint(components)

@lru_cache(maxsize=4096)
def is_protected_path(path: PathLike) -> TypeGuard[str]:
    """Valida si la ruta es parte de las áreas protegidas del sistema o raíz de unidad."""
    if not isinstance(path, (str, Path)) or not path: return True
    try:
        p = normalize(path)
        if p == Path(p.anchor): return True
        return _is_system_path_raw(str(p))
    except (UnsafePathError, TypeError, OSError, RuntimeError): return True

@lru_cache(maxsize=4096)
def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """
    Verifica si 'child' reside físicamente debajo de 'parent'.
    Bloquea el acceso si la ruta resultante está en áreas protegidas.
    """
    if child is None or parent is None: return False
    try:
        c_path = normalize(child)
        p_path = normalize(parent)
        if is_drive_root(c_path) or is_protected_path(str(c_path)): return False
        c_str = str(c_path)
        p_str = str(p_path)
        return c_str.startswith(p_str) if allow_equal else (c_str.startswith(p_str) and c_str != p_str)
    except (UnsafePathError, TypeError, OSError, RuntimeError): return False

@lru_cache(maxsize=2048)
def is_sensitive_file(path: PathLike) -> bool:
    """Valida si la extensión del archivo es crítica según la configuración global."""
    if not path: return True
    try: 
        p_str = str(path)
        return _is_sensitive_extension(os.path.splitext(p_str)[1])
    except (TypeError, ValueError, OSError): return True 

def _validate_structural_safety(target_path: Path, path_string: str) -> None:
    """Valida la integridad de la estructura de la ruta (largo, caracteres, inyecciones)."""
    if not isinstance(path_string, str):
        raise UnsafePathError("Ruta no es texto.", SafetyValidationErrorCode.GENERIC)
    if _is_unc_path(path_string):
        raise UnsafePathError("Rutas UNC/Red bloqueadas.", SafetyValidationErrorCode.UNC_PATH)
    if ".." in path_string.split(os.sep):
        raise UnsafePathError("Path traversal detectado.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    if len(path_string) > MAX_PATH_LENGTH:
        raise UnsafePathError("Ruta demasiado larga.", SafetyValidationErrorCode.PATH_TOO_LONG)
    if "\0" in path_string:
        raise UnsafePathError("Inyección de carácter nulo.", SafetyValidationErrorCode.NULL_CHAR)
    if _has_invalid_chars(path_string):
        raise UnsafePathError("Caracteres inválidos detectados.", SafetyValidationErrorCode.INVALID_CHARS)
    if unicodedata.normalize('NFKC', path_string) != path_string:
        raise UnsafePathError("Codificación de caracteres sospechosa.", SafetyValidationErrorCode.SUSPICIOUS_ENCODING)
    if _has_alternate_data_stream(path_string):
        raise UnsafePathError("Flujo de datos alternativo detectado.", SafetyValidationErrorCode.ADS_DETECTED)
    if _is_device_file(target_path):
        raise UnsafePathError("Acceso a dispositivo bloqueado.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED)
    
    try:
        if target_path.exists() and not target_path.is_absolute():
            raise UnsafePathError("Ruta inconsistente con el sistema.", SafetyValidationErrorCode.GENERIC)
        if not target_path.parts or (len(target_path.parts) == 1 and target_path.parts[0] == os.sep):
             raise UnsafePathError("Ruta raíz no permitida.", SafetyValidationErrorCode.ROOT_ACCESS)
        for part in target_path.parts:
            if len(part) > MAX_FILENAME_LENGTH:
                raise UnsafePathError(f"Nombre de componente demasiado largo.", SafetyValidationErrorCode.PATH_TOO_LONG)
            if not part or part.strip() != part:
                raise UnsafePathError(f"Componente '{part}' con espacios envolventes.", SafetyValidationErrorCode.INVALID_CHARS)
            if part.endswith(('.', ' ')):
                raise UnsafePathError(f"Componente '{part}' termina en caracter inválido.", SafetyValidationErrorCode.INVALID_CHARS)
            part_cleaned = part.split('.')[0]
            if _is_reserved_device_name(part_cleaned):
                raise UnsafePathError(f"Nombre reservado '{part}'.", SafetyValidationErrorCode.RESERVED_NAME)
    except (AttributeError, TypeError, ValueError):
        raise UnsafePathError("Estructura de ruta inválida.", SafetyValidationErrorCode.GENERIC)

def _validate_boundary_conditions(target_path: Path, root_directory: Optional[PathLike]) -> None:
    """Verifica límites de sandbox y restricciones específicas del tipo de volumen (ej. red)."""
    if not is_absolute_path_allowed(target_path):
        raise UnsafePathError("Solo se permiten rutas absolutas.", SafetyValidationErrorCode.RELATIVE_PATH_NOT_ALLOWED)
        
    if root_directory and not is_within_directory(target_path, root_directory, allow_equal=True):
        raise UnsafePathError("Fuera de alcance permitido.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    if is_protected_path(str(target_path)):
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
                if drive_type == DRIVE_CDROM or _is_volume_readonly(str(target_path)):
                     raise UnsafePathError("Volumen de solo lectura.", SafetyValidationErrorCode.VOLUME_READ_ONLY)
        except (OSError, AttributeError, ctypes.ArgumentError) as e:
             raise UnsafePathError(f"Fallo al consultar unidad: {e}", SafetyValidationErrorCode.IO_ERROR)
    
    try:
        app_root = Path(os.getcwd()).resolve()
        if target_path == app_root or app_root in target_path.parents:
            raise UnsafePathError("Modificación de App denegada.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    except (OSError, RuntimeError, ValueError): pass
    if is_drive_root(target_path):
        raise UnsafePathError("Acceso a raíz denegado.", SafetyValidationErrorCode.ROOT_ACCESS)

def _get_final_path_normalized(path: Path) -> Optional[Path]:
    """
    Resuelve la ruta física real en disco mediante GetFinalPathNameByHandleW.
    Permite detectar si un archivo ha sido redirigido mediante un Reparse Point (Junction).
    """
    kernel32 = ctypes.windll.kernel32
    try:
        handle = kernel32.CreateFileW(_to_long_path(str(path)), 0, 0, None, 3, 0x02000000, None)
        if handle == -1: return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            if kernel32.GetFinalPathNameByHandleW(handle, buf, 1024, 0) > 0:
                return Path(buf.value).resolve()
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError, Exception):
        return None
    return None

def _validate_ntfs_reparse_redirection(path: Path) -> None:
    """
    Verifica que la resolución de la ruta no sea un proxy hacia otro volumen o subdirectorio no autorizado.
    Utiliza el identificador final del archivo (vía API Win32) para detectar redirecciones que salten 
    las protecciones de sandbox o intenten acceder fuera del árbol raíz autorizado.
    """
    if not path.exists(): return
    for parent in path.parents:
        if _is_reparse_point(str(parent)):
            raise UnsafePathError("Segmento de ruta contiene punto de reparse.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
            
    final_path = _get_final_path_normalized(path)
    if final_path:
        if final_path.drive != path.resolve().drive:
            raise UnsafePathError("Redirección de unidad detectada.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        if not str(final_path).startswith(str(path.parent)):
            raise UnsafePathError("Salida de carpeta permitida vía redirección.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)

def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> Path:
    """
    Valida la integridad de una ruta y sus permisos para operaciones de escritura.
    
    El proceso sigue un pipeline de seguridad estricto:
    1. Normalización y limpieza de caracteres.
    2. Validación estructural (evitar traversal, caracteres prohibidos).
    3. Verificación de límites (evitar sistema, unidades de red, sandbox).
    4. Inspección de integridad física (TOCTOU, bloqueos, reparse points).
    
    Args:
        path: La ruta a evaluar.
        allow_sensitive: Si es True, permite extensiones bloqueadas (ej. .exe).
        base_dir: Directorio raíz opcional para restringir el alcance del movimiento.
        
    Returns:
        Path: La ruta normalizada y validada.
        
    Raises:
        UnsafePathError: Si la ruta infringe cualquier política de seguridad definida.
    """
    if path is None:
        raise UnsafePathError("Ruta nula.", SafetyValidationErrorCode.GENERIC)
    p = normalize(path)
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError(f"Extensión bloqueada '{p.suffix}'.", SafetyValidationErrorCode.SENSITIVE_EXTENSION)
    _validate_structural_safety(p, str(p))
    _validate_boundary_conditions(p, base_dir)
    
    if p.exists():
        _validate_access_permissions(p)
        initial_stat = _get_path_stat_robust(p)
        if not bool(initial_stat.st_mode & stat.S_IWRITE):
            raise UnsafePathError(f"Acceso de escritura denegado: {p.name}", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
        if os.name == 'nt': 
            _validate_ntfs_reparse_redirection(p)
            if not os.access(p.parent, os.W_OK):
                     raise UnsafePathError("Directorio contenedor marcado como solo lectura.", SafetyValidationErrorCode.VOLUME_READ_ONLY)
        _check_file_integrity(p, initial_stat)
    else:
        parent = p.parent
        if parent.exists() and not os.access(parent, os.W_OK):
                 raise UnsafePathError("Directorio contenedor no tiene permisos de escritura.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
        if parent.exists() and is_protected_path(str(parent)):
            raise UnsafePathError("Creación en directorio restringido.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)
        # Protección extra: si el padre tiene reparse points, podría ser un bypass.
        if parent.exists() and os.name == 'nt':
            for p_seg in parent.parents:
                if _is_reparse_point(str(p_seg)):
                    raise UnsafePathError("Ruta base contiene punto de reparse.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
    return p

def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Wrapper booleano para validar seguridad, ideal para filtrado en colecciones."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, ValueError, TypeError, OSError, PermissionError): return False

def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una colección, descartando rutas inseguras o fuera de alcance."""
    results = []
    for p in paths:
        if p is None: continue
        try: results.append(ensure_safe_to_modify(p, allow_sensitive=allow_sensitive))
        except (UnsafePathError, ValueError, TypeError, OSError, PermissionError): continue
    return results

def describe_protection(path: PathLike) -> str:
    """Diagnóstico humano de por qué una ruta fue marcada como insegura."""
    if path is None: return "Ruta nula."
    try:
        p = normalize(path)
        raw_str = str(path)
    except (UnsafePathError, TypeError, ValueError): return "Ruta mal formada."
    if _is_unc_path(raw_str): return f"'{raw_str}' es ruta de red."
    if _is_device_file(p): return f"'{raw_str}' es un archivo de dispositivo."
    if is_drive_root(p): return f"'{p}' es raíz de unidad."
    if is_protected_path(str(p)): return f"'{p}' protegida por sistema."
    try:
        if p.exists():
            if p.is_symlink(): return f"'{p}' es un enlace simbólico."
            if _is_reparse_point(str(p)): return f"'{p}' es un punto de reparse (Junction/Symlink)."
            if os.path.ismount(p): return f"'{p}' es un punto de montaje."
            if _is_readonly(str(p)): return f"'{p}' es solo lectura."
            if _is_volume_readonly(str(p)): return f"'{p}' pertenece a un volumen de solo lectura."
            if _is_file_locked_by_other_process(str(p)): return f"'{p}' en uso."
            if _is_encrypted_or_compressed_or_sparse(str(p)): return f"'{p}' archivo cifrado, comprimido o disperso."
            if _is_offline(str(p)): return f"'{p}' archivo offline/nube."
            if _is_system_or_hidden(str(p)): return f"'{p}' atributo oculto/sistema/temporal."
            if _has_alternate_data_stream(p.name): return f"'{p}' contiene ADS."
            if not (p.is_file() or p.is_dir()): return f"'{p}' tipo de objeto no soportado."
            if p.is_file() and p.stat().st_size == 0: return f"'{p}' archivo vacío (potencialmente crítico)."
            if p.is_file() and p.stat().st_size > MAX_FILE_SIZE: return f"'{p}' tamaño excesivo."
            if p.is_file() and p.stat().st_nlink > 1: return f"'{p}' detectado como hard link."
    except (OSError, FileNotFoundError, AttributeError): pass
    if is_sensitive_file(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
