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
import errno
from enum import Enum, auto, IntEnum
from pathlib import Path
from typing import Union, Iterable, TypeAlias, Final, NamedTuple, Callable, Optional, TypedDict
from functools import lru_cache
import unicodedata

# Constantes de error de Win32 API
ERROR_SHARING_VIOLATION: Final[int] = 32
ERROR_FILE_NOT_FOUND: Final[int] = 2
ERROR_ACCESS_DENIED: Final[int] = 5
ERROR_INSUFFICIENT_BUFFER: Final[int] = 122
ERROR_INVALID_NAME: Final[int] = 123
INVALID_FILE_ATTRIBUTES: Final[int] = 0xFFFFFFFF
LONG_PATH_PREFIX: Final[str] = "\\\\?\\"

PathLike: TypeAlias = Union[str, os.PathLike]
# Predicado de violación: retorna True si se detecta una condición insegura
ViolationPredicate: TypeAlias = Callable[[Path, os.stat_result, "SecurityDescriptor"], bool]

# Caché global de integridad para evitar E/S redundante
_SECURITY_CACHE: Final[dict[tuple[str, int, int], bool]] = {}
_STRUCTURAL_CACHE: Final[dict[str, bool]] = {}

class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    """Estructura de bajo nivel que mapea la API GetFileInformationByHandle de Windows."""
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", ctypes.c_uint64),
        ("ftLastAccessTime", ctypes.c_uint64),
        ("ftLastWriteTime", ctypes.c_uint64),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]

class SafetyState(NamedTuple):
    """Contexto inmutable que encapsula el estado crítico de un archivo para validación."""
    path: Path
    descriptor: SecurityDescriptor
    stat_result: os.stat_result
    file_id: tuple[int, int, int, int]

def _get_file_id(path_str: str) -> tuple[int, int, int, int]:
    """
    Genera un identificador único (ID de volumen + índice de nodo) para detectar 
    si un archivo cambió bajo la mano (mitigación TOCTOU) utilizando la API de Windows.
    """
    if os.name != 'nt' or not path_str: return (0, 0, 0, 0)
    info = BY_HANDLE_FILE_INFORMATION()
    kernel32 = ctypes.windll.kernel32
    try:
        # Abre con acceso nulo para obtener metadata sin bloquear ni modificar el archivo
        handle = kernel32.CreateFileW(_to_long_path(path_str), 0, 0x07, None, 3, 0x00000080, None)
        if handle == -1: # INVALID_HANDLE_VALUE
            return (0, 0, 0, 0)
        try:
            if kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
                is_dir = bool(info.dwFileAttributes & Win32Attr.DIRECTORY)
                return (info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow, int(is_dir))
        finally:
            kernel32.CloseHandle(handle)
    except (ctypes.ArgumentError, OSError, ValueError):
        return (0, 0, 0, 0)
    return (0, 0, 0, 0)

class SafetyAction(Enum):
    """Niveles de criticidad para determinar el rigor en la validación de acceso."""
    READ = auto()
    MODIFY = auto()

class Win32Attr(IntEnum):
    """Mapeo de bits para banderas de atributos de archivos en Windows (Win32)."""
    READONLY: int = 0x01
    HIDDEN: int = 0x02
    SYSTEM: int = 0x04
    DIRECTORY: int = 0x10
    TEMPORARY: int = 0x100
    SPARSE_FILE: int = 0x200
    REPARSE_POINT: int = 0x400
    COMPRESSED: int = 0x800
    OFFLINE: int = 0x1000
    ENCRYPTED: int = 0x4000

class SecurityDescriptor(NamedTuple):
    """Metadatos de seguridad analizados para una ruta específica."""
    attrs: int
    is_protected_system: bool
    is_in_use: bool
    is_readonly: bool
    is_reparse: bool
    
    def has_flag(self, flag: Win32Attr) -> bool:
        """Verifica la presencia de un atributo específico mediante operación AND binaria."""
        return bool(self.attrs & flag)

    @classmethod
    def from_path(cls, path_str: str) -> SecurityDescriptor:
        """Extrae atributos desde el SO, calculando flags de riesgo derivados."""
        if not path_str:
            return cls(0, True, True, True, True)
        try:
            attrs = _get_file_attrs(path_str)
            if attrs == INVALID_FILE_ATTRIBUTES:
                return cls(0, True, True, True, True)
                
            risk_flags = (Win32Attr.HIDDEN | Win32Attr.SYSTEM | Win32Attr.OFFLINE | 
                          Win32Attr.TEMPORARY | Win32Attr.REPARSE_POINT)
            is_protected = bool(attrs & risk_flags)
            is_in_use = is_protected or _is_file_locked_by_other_process(path_str)
            
            return cls(
                attrs=attrs,
                is_protected_system=is_protected,
                is_in_use=is_in_use,
                is_readonly=bool(attrs & Win32Attr.READONLY),
                is_reparse=bool(attrs & Win32Attr.REPARSE_POINT)
            )
        except (OSError, ValueError):
            return cls(0, True, True, True, True)

class FileMetadata(TypedDict):
    """Estructura de intercambio de metadatos de seguridad entre módulos."""
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

MAX_PATH_LENGTH: Final[int] = 260
MAX_FILENAME_LENGTH: Final[int] = 255
MAX_FILE_SIZE: Final[int] = 2 * 1024 * 1024 * 1024
MAX_DIRECTORY_DEPTH: Final[int] = 32
MAX_REPARSE_DEPTH: Final[int] = 4

DRIVE_UNKNOWN: Final[int] = 0
DRIVE_NO_ROOT_DIR: Final[int] = 1
DRIVE_REMOVABLE: Final[int] = 2 
DRIVE_FIXED: Final[int] = 3     
DRIVE_REMOTE: Final[int] = 4    
DRIVE_CDROM: Final[int] = 5     
DRIVE_RAMDISK: Final[int] = 6   

@lru_cache(maxsize=8192)
def _to_long_path(path_str: str) -> str:
    """
    Transforma rutas a formato extendido UNC (\\?\C:\...) para permitir el 
    acceso a rutas que exceden los 260 caracteres típicos de Win32.
    """
    if not isinstance(path_str, str) or not path_str:
        return ""
    if os.name == 'nt':
        if path_str.startswith(LONG_PATH_PREFIX):
            return path_str
        if path_str.startswith("\\\\"):
            return "\\\\?\\UNC" + path_str[1:]
        try:
            abs_path = os.path.abspath(path_str)
            return LONG_PATH_PREFIX + abs_path
        except (OSError, ValueError):
            return path_str
    return path_str

def _is_path_too_long(path_str: str) -> bool:
    """Valida el cumplimiento del límite de longitud de ruta de Windows."""
    return len(path_str) > MAX_PATH_LENGTH

@lru_cache(maxsize=4096)
def _get_file_attrs(path_str: Optional[str]) -> int:
    """Consulta atributos de archivo directamente mediante GetFileAttributesW."""
    if not isinstance(path_str, str) or not path_str:
        return 0
    # Validación lógica de absolutismo evitando llamada a sistema de archivos
    if not (path_str.startswith(LONG_PATH_PREFIX) or len(path_str) > 2 and path_str[1:3] == ':\\'):
        return 0
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(_to_long_path(path_str))
        if attrs == INVALID_FILE_ATTRIBUTES:
            err = ctypes.windll.kernel32.GetLastError()
            if err == ERROR_ACCESS_DENIED:
                return Win32Attr.SYSTEM
            return INVALID_FILE_ATTRIBUTES
        return attrs
    except (AttributeError, ctypes.ArgumentError, OSError):
        return 0

class SafetyValidationErrorCode(IntEnum):
    """Categorización de errores para auditoría de fallos en la validación."""
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
    REMOVABLE_DRIVE_DETECTED = 22
    KERNEL_LOCKED_FILE = 23
    WRITE_ACCESS_DENIED = 24
    MOUNT_POINT_DETECTED = 25
    TOCTOU_VIOLATION = 26
    SPARSE_FILE_DETECTED = 27
    DEVICE_FILE_DETECTED = 28
    EMPTY_FILE = 29
    VOLUME_RESTRICTED = 30
    PATH_TOO_DEEP = 31
    SYSTEM_OWNER_PROTECTION = 32
    VIRTUAL_DRIVE_DETECTED = 33
    CIRCULAR_REFERENCE = 35
    ADMIN_PRIVILEGE_VIOLATION = 36
    VOLUME_SHADOW_COPY = 38

class UnsafePathError(Exception):
    """Excepción base para violaciones de política de seguridad en el sistema de archivos."""
    def __init__(self, message: str, code: SafetyValidationErrorCode = SafetyValidationErrorCode.GENERIC):
        super().__init__(f"[{code.name}] {message}")
        self.code = code

class ProtectionReason(Enum):
    """Lista exhaustiva de motivos por los que una ruta es catalogada como insegura."""
    INACCESSIBLE = auto()
    REPARSE_POINT = auto()
    READ_ONLY = auto()
    IN_USE = auto()
    SYSTEM_HIDDEN = auto()
    HARD_LINK = auto()
    SYMLINK = auto()
    ADS = auto()
    EMPTY_FILE = auto()
    EXCESSIVE_DEPTH = auto()
    MOUNT_POINT = auto()
    EXCESSIVE_SIZE = auto()
    INVALID_TYPE = auto()
    OFFLINE = auto()
    ENCRYPTED_OR_COMPRESSED = auto()
    VOLUME_READ_ONLY = auto()
    REMOTE_DRIVE = auto()
    REMOVABLE_DRIVE = auto()
    KERNEL_LOCKED = auto()
    ACCESS_WRITE = auto()
    TOCTOU_VIOLATION = auto()
    SPARSE_FILE = auto()
    DEVICE_FILE = auto()
    VOLUME_RESTRICTED = auto()
    SYSTEM_OWNER = auto()
    VIRTUAL_DRIVE = auto()
    CIRCULAR_REF = auto()
    ADMIN_PRIVILEGE = auto()
    SHADOW_COPY = auto()

class ValidationContext(Enum):
    """Contexto de ejecución de la validación (Estructural o Integridad)."""
    STRUCTURAL = auto()
    INTEGRITY = auto()

PROTECTED_DIR_NAMES: Final[set[str]] = {
    "windows", "winnt", "system32", "syswow64", "system", "boot",
    "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery",
    "perflogs", "msocache", "$windows.~bt", "$windows.~ws",
    "windowsapps", "assembly", "winsxs", "drivers", "drivestore",
    ".ssh", ".gnupg", "microsoft\\crypto", "protect",
    "bin", "sbin", "usr", "etc", "var", "lib", "lib64", "proc", "sys",
    "dev", "root", "library", "applications",
    "config.msi", "installer",
}

SENSITIVE_EXTENSIONS: Final[set[str]] = {
    ".sys", ".dll", ".exe", ".msi", ".drv", ".ocx", ".cpl", ".efi",
    ".reg", ".pol", ".key", ".pem", ".pfx", ".p12", ".crt", ".cer",
}

_SYSTEM_ROOT_PATHS: Final[set[str]] = {
    os.path.normcase(os.environ[v]) for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    if os.environ.get(v)
}

_RESERVED_NAMES_PATTERN: Final[re.Pattern] = re.compile(
    r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$', re.IGNORECASE
)

_KERNEL_BLOCKED_FILES: Final[frozenset[str]] = frozenset(
    ("pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log.tmp", "memory.dmp")
)

_KERNEL_BLOCKED_DIRS: Final[frozenset[str]] = frozenset(("config.msi", "installer"))

class _IntegrityCheck(NamedTuple):
    """Regla de validación abstracta para la integridad de objetos del filesystem."""
    reason: ProtectionReason
    predicate: ViolationPredicate

class _CheckResult(NamedTuple):
    is_safe: bool
    reason: Optional[ProtectionReason] = None

@lru_cache(maxsize=1)
def is_running_as_admin() -> bool:
    """Verifica si el proceso actual opera con privilegios elevados en Windows."""
    if os.name != 'nt':
        try: return os.geteuid() == 0
        except (AttributeError, OSError): return False
    try:
        shell32 = ctypes.windll.shell32
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False

@lru_cache(maxsize=128)
def _is_virtual_drive(path_str: str) -> bool:
    """Detecta si la unidad es un mapeo lógico tipo SUBST o dispositivo virtual."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    drive = os.path.splitdrive(path_str)[0]
    if not drive: return False
    try:
        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_unicode_buffer(512)
        res = kernel32.QueryDosDeviceW(drive, buf, 512)
        if res > 0:
            device_path = buf.value
            return device_path.startswith("\\DosDevices\\") or device_path.startswith("\\??\\")
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False 
    return False

def _is_volume_shadow_copy(path_str: str) -> bool:
    """Detecta si la ruta pertenece a un Volumen de Instantánea (VSS) de Windows."""
    if os.name != 'nt' or not path_str or not path_str.startswith("\\\\?\\"): return False
    # Las instantáneas de volumen suelen tener una estructura de nombre específica en el namespace global
    return "\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy" in path_str.upper()

def _is_file_owned_by_system(path_str: str) -> bool:
    """Verifica si el archivo pertenece a SYSTEM o TrustedInstaller mediante sus SID."""
    if os.name != 'nt' or not isinstance(path_str, str) or not os.path.exists(_to_long_path(path_str)): return False
    try:
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        sid_ptr = ctypes.c_void_p()
        res = advapi32.GetNamedSecurityInfoW(
            _to_long_path(path_str), 1, 1, ctypes.byref(sid_ptr), None, None, None, None
        )
        if res == 0 and sid_ptr:
            try:
                sid_str = ctypes.create_unicode_buffer(128)
                if advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str)):
                    sid_val = sid_str.value
                    return sid_val.startswith("S-1-5-18") or sid_val.startswith("S-1-5-80")
            finally:
                kernel32.LocalFree(sid_ptr)
    except (AttributeError, OSError, ctypes.ArgumentError):
        pass
    return False

def _has_invalid_chars(path_str: Optional[str]) -> bool:
    """Filtra caracteres prohibidos en el sistema y secuencias BIDI maliciosas."""
    if not isinstance(path_str, str) or not path_str: return True
    return bool(re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E\u206A-\u206F]', path_str))

def _is_path_empty_or_whitespace(path_str: str) -> bool:
    """Valida que la cadena no sea nula o solo espacios."""
    return not path_str or path_str.strip() == ""

def _is_unc_path(path_str: str) -> bool:
    """Determina si la ruta apunta a un recurso compartido UNC (Red)."""
    if not isinstance(path_str, str): return False
    return path_str.startswith(("\\\\", "//"))

def _is_unc_path_accessible(path_str: str) -> bool:
    """Valida la conectividad básica con un recurso compartido de red."""
    if not _is_unc_path(path_str) or os.name != 'nt': return False
    try:
        return ctypes.windll.kernel32.GetDiskFreeSpaceW(path_str, None, None, None, None) != 0
    except (OSError, ctypes.ArgumentError): return False

@lru_cache(maxsize=128)
def _is_reserved_device_name(name: str) -> bool:
    """Identifica nombres reservados por el kernel (ej. CON, LPT1)."""
    if not name: return False
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))

def _is_device_file(path: Path) -> bool:
    """Identifica punteros a archivos de dispositivo del sistema."""
    path_str = str(path).upper()
    return path_str.startswith(("\\\\.\\", "//./"))

@lru_cache(maxsize=512)
def _has_alternate_data_stream(path_name: str) -> bool:
    """Detecta flujos alternativos NTFS (ADS) que pueden ocultar contenido malicioso."""
    return ":" in path_name and len(path_name.split(":")) > 2

def _is_system_directory_junction(path_str: str) -> bool:
    """Determina si una ruta es un punto de unión de sistema (reparse point)."""
    if os.name != 'nt' or not isinstance(path_str, str): return False
    attrs = _get_file_attrs(path_str)
    if attrs == INVALID_FILE_ATTRIBUTES: return False
    return bool(attrs & Win32Attr.REPARSE_POINT)

@lru_cache(maxsize=1024)
def _is_file_locked_by_other_process(path_str: str) -> bool:
    """Verifica si un archivo está bloqueado por otro proceso usando reintentos."""
    if not isinstance(path_str, str) or os.name != 'nt' or not (path_str.startswith(LONG_PATH_PREFIX) or len(path_str) > 2 and path_str[1:3] == ':\\'): return False
    if not os.path.exists(path_str): return False
    
    kernel32 = ctypes.windll.kernel32
    path_long = _to_long_path(path_str)
    
    for attempt in range(3):
        # Intenta abrir con acceso 0 para metadata sin bloquear
        handle = kernel32.CreateFileW(path_long, 0, 0, None, 3, 0x00000080, None)
        if handle != -1:
            kernel32.CloseHandle(handle)
            return False
        
        err = kernel32.GetLastError()
        if err == ERROR_SHARING_VIOLATION:
            time.sleep(0.05 * (2 ** attempt))
            continue
        return True
    return True

def _get_security_descriptor(path: Path) -> SecurityDescriptor:
    """Extrae metadatos de seguridad para un objeto path."""
    return SecurityDescriptor.from_path(str(path))

def _is_file_in_use_by_system(path_str: str) -> bool:
    """Verifica si el archivo está cargado en memoria por algún proceso (GetModuleHandle)."""
    if os.name != 'nt' or not isinstance(path_str, str): return False
    try:
        h_module = ctypes.windll.kernel32.GetModuleHandleW(path_str)
        return h_module != 0
    except (OSError, ctypes.ArgumentError): return False

@lru_cache(maxsize=128)
def _is_volume_readonly(path_str: Optional[str]) -> bool:
    """Consulta al SO si el volumen que contiene la ruta es de solo lectura."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str or not (path_str.startswith(LONG_PATH_PREFIX) or len(path_str) > 2 and path_str[1:3] == ':\\'): return False
    if _is_unc_path(path_str): return False
    try:
        drive = os.path.splitdrive(path_str)[0]
        if not drive or not os.path.exists(drive + "\\"): return False
        flags = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetVolumeInformationW(drive + "\\", None, 0, None, None, ctypes.byref(flags), None, 0) != 0:
            return bool(flags.value & 0x80000)
    except (AttributeError, OSError, ctypes.ArgumentError): pass
    return False

@lru_cache(maxsize=128)
def _is_volume_busy_or_invalid(path_str: Optional[str]) -> bool:
    """Valida la integridad y disponibilidad operativa del volumen raíz."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str: return False
    try:
        drive = os.path.splitdrive(path_str)[0] + "\\"
        kernel32 = ctypes.windll.kernel32
        if kernel32.GetVolumeInformationW(drive, None, 0, None, None, None, None, 0) == 0:
            return True
        return False
    except (OSError, ctypes.ArgumentError): return True

@lru_cache(maxsize=128)
def _get_drive_type(root: str) -> int:
    """Retorna tipo de unidad lógico según la API Win32."""
    try: return ctypes.windll.kernel32.GetDriveTypeW(root)
    except (OSError, AttributeError, ctypes.ArgumentError): return DRIVE_UNKNOWN

@lru_cache(maxsize=128)
def _is_volume_removable_media(path_str: Optional[str]) -> bool:
    """Identifica si el archivo reside en almacenamiento extraíble (USB/SD)."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str or not (path_str.startswith(LONG_PATH_PREFIX) or len(path_str) > 2 and path_str[1:3] == ':\\'): return False
    try:
        root = os.path.splitdrive(path_str)[0] + "\\"
        return _get_drive_type(root) == DRIVE_REMOVABLE
    except (AttributeError, OSError, ctypes.ArgumentError): pass
    return False

@lru_cache(maxsize=128)
def _is_volume_compressed_or_encrypted(path_str: Optional[str]) -> bool:
    """Verifica si el volumen tiene restricciones nativas de cifrado/compresión."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str or not (path_str.startswith(LONG_PATH_PREFIX) or len(path_str) > 2 and path_str[1:3] == ':\\'): return False
    try:
        root = os.path.splitdrive(path_str)[0] + "\\"
        if not os.path.exists(root): return False
        flags = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0) != 0:
            return bool(flags.value & (0x10 | 0x20000))
    except (AttributeError, OSError, ctypes.ArgumentError): pass
    return False

@lru_cache(maxsize=1024)
def _is_kernel_managed(path_str: str) -> bool:
    """Identifica rutas protegidas que forman parte de la gestión exclusiva del SO."""
    if not path_str: return False
    p_lower = path_str.lower()
    if any(p_lower.endswith(f) for f in _KERNEL_BLOCKED_FILES): return True
    if os.name == 'nt':
        local_app_data = os.environ.get("LOCALAPPDATA", "").lower()
        app_data = os.environ.get("APPDATA", "").lower()
        if (local_app_data and p_lower.startswith(local_app_data)) or \
           (app_data and p_lower.startswith(app_data)): return True
    return any(d in p_lower for d in _KERNEL_BLOCKED_DIRS)

@lru_cache(maxsize=1024)
def _is_sensitive_extension(ext: str) -> bool:
    """Determina si la extensión de un archivo sugiere un componente crítico del sistema."""
    return ext.lower() in SENSITIVE_EXTENSIONS

def _rule(reason: ProtectionReason, predicate: ViolationPredicate) -> _IntegrityCheck:
    """Constructor helper para definir reglas de seguridad."""
    return _IntegrityCheck(reason, predicate)

def _validate_junction_depth(path: Path) -> None:
    """Limita la profundidad de resolución de enlaces para prevenir ataques de recursión."""
    count = 0
    curr = path
    while curr.is_symlink() or _is_system_directory_junction(str(curr)):
        count += 1
        if count > MAX_REPARSE_DEPTH:
            raise UnsafePathError("Profundidad de reparse point excesiva.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        try:
            curr = _get_final_path_normalized(curr)
        except Exception:
            break

def _validate_drive_restrictions(target_path: Path) -> None:
    """Valida que la unidad destino cumpla con los requisitos operativos de la app."""
    anchor = target_path.anchor
    if not anchor: return
    
    try:
        path_s = str(target_path)
        if _is_volume_shadow_copy(path_s):
            raise UnsafePathError("Volumen de instantánea bloqueado.", SafetyValidationErrorCode.VOLUME_SHADOW_COPY)
        if _is_volume_busy_or_invalid(path_s):
             raise UnsafePathError("Volumen inaccesible.", SafetyValidationErrorCode.IO_ERROR)
        drive_type = _get_drive_type(anchor)
        if drive_type == DRIVE_NO_ROOT_DIR:
            raise UnsafePathError("Unidad inaccesible.", SafetyValidationErrorCode.IO_ERROR)
        if drive_type == DRIVE_REMOTE:
            raise UnsafePathError("Unidad de red bloqueada.", SafetyValidationErrorCode.IO_ERROR)
        if drive_type == DRIVE_REMOVABLE:
            raise UnsafePathError("Unidad extraíble bloqueada.", SafetyValidationErrorCode.REMOVABLE_DRIVE_DETECTED)
        if drive_type == DRIVE_CDROM or _is_volume_readonly(path_s):
            raise UnsafePathError("Volumen de solo lectura.", SafetyValidationErrorCode.VOLUME_READ_ONLY)
        if _is_volume_compressed_or_encrypted(path_s):
            raise UnsafePathError("Volumen cifrado o comprimido.", SafetyValidationErrorCode.VOLUME_RESTRICTED)
        if _is_virtual_drive(path_s):
            raise UnsafePathError("Unidad virtual bloqueada.", SafetyValidationErrorCode.VIRTUAL_DRIVE_DETECTED)
    except (AttributeError, OSError, ctypes.ArgumentError):
        pass

# Predicados de integridad encapsulados: lógica booleana sobre el estado de un objeto de archivo.
@lru_cache(maxsize=2048)
def _check_symlink(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return p.is_symlink()
@lru_cache(maxsize=2048)
def _check_reparse(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return sd.is_reparse
@lru_cache(maxsize=2048)
def _check_kernel_locked(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return _is_kernel_managed(str(p))
@lru_cache(maxsize=2048)
def _check_read_only(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return not bool(st.st_mode & stat.S_IWRITE) or sd.is_readonly
@lru_cache(maxsize=2048)
def _check_vol_read_only(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return _is_volume_readonly(str(p))
@lru_cache(maxsize=2048)
def _check_vol_removable(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return _is_volume_removable_media(str(p))
@lru_cache(maxsize=2048)
def _check_vol_restricted(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return _is_volume_compressed_or_encrypted(str(p))
@lru_cache(maxsize=2048)
def _check_in_use(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return sd.is_in_use
@lru_cache(maxsize=2048)
def _check_sys_hidden(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return sd.is_protected_system
@lru_cache(maxsize=2048)
def _check_offline(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return sd.has_flag(Win32Attr.OFFLINE)
@lru_cache(maxsize=2048)
def _check_enc_comp(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return sd.has_flag(Win32Attr.COMPRESSED) or sd.has_flag(Win32Attr.ENCRYPTED)
@lru_cache(maxsize=2048)
def _check_sparse(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return sd.has_flag(Win32Attr.SPARSE_FILE)
@lru_cache(maxsize=2048)
def _check_hard_link(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return p.is_file() and st.st_nlink > 1
@lru_cache(maxsize=2048)
def _check_ads(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return _has_alternate_data_stream(p.name)
@lru_cache(maxsize=2048)
def _check_empty(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return p.is_file() and st.st_size == 0
@lru_cache(maxsize=2048)
def _check_size(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return p.is_file() and st.st_size > MAX_FILE_SIZE
@lru_cache(maxsize=2048)
def _check_mount(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return os.path.ismount(p)
@lru_cache(maxsize=2048)
def _check_type(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode))
@lru_cache(maxsize=2048)
def _check_owner(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return _is_file_owned_by_system(str(p))
@lru_cache(maxsize=2048)
def _check_virtual(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return _is_virtual_drive(str(p))
@lru_cache(maxsize=2048)
def _check_shadow(p: Path, st: os.stat_result, sd: SecurityDescriptor) -> bool: return _is_volume_shadow_copy(str(p))

_VALIDATORS: Final[list[_IntegrityCheck]] = [
    _rule(ProtectionReason.SYMLINK, _check_symlink),
    _rule(ProtectionReason.REPARSE_POINT, _check_reparse),
    _rule(ProtectionReason.KERNEL_LOCKED, _check_kernel_locked),
    _rule(ProtectionReason.READ_ONLY, _check_read_only),
    _rule(ProtectionReason.VOLUME_READ_ONLY, _check_vol_read_only),
    _rule(ProtectionReason.REMOVABLE_DRIVE, _check_vol_removable),
    _rule(ProtectionReason.VOLUME_RESTRICTED, _check_vol_restricted),
    _rule(ProtectionReason.IN_USE, _check_in_use),
    _rule(ProtectionReason.SYSTEM_HIDDEN, _check_sys_hidden),
    _rule(ProtectionReason.OFFLINE, _check_offline),
    _rule(ProtectionReason.ENCRYPTED_OR_COMPRESSED, _check_enc_comp),
    _rule(ProtectionReason.SPARSE_FILE, _check_sparse),
    _rule(ProtectionReason.HARD_LINK, _check_hard_link),
    _rule(ProtectionReason.ADS, _check_ads),
    _rule(ProtectionReason.EMPTY_FILE, _check_empty),
    _rule(ProtectionReason.EXCESSIVE_SIZE, _check_size),
    _rule(ProtectionReason.MOUNT_POINT, _check_mount),
    _rule(ProtectionReason.INVALID_TYPE, _check_type),
    _rule(ProtectionReason.SYSTEM_OWNER, _check_owner),
    _rule(ProtectionReason.VIRTUAL_DRIVE, _check_virtual),
    _rule(ProtectionReason.SHADOW_COPY, _check_shadow),
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
    ProtectionReason.REMOVABLE_DRIVE: SafetyValidationErrorCode.REMOVABLE_DRIVE_DETECTED,
    ProtectionReason.VOLUME_RESTRICTED: SafetyValidationErrorCode.VOLUME_RESTRICTED,
    ProtectionReason.KERNEL_LOCKED: SafetyValidationErrorCode.KERNEL_LOCKED_FILE,
    ProtectionReason.ACCESS_WRITE: SafetyValidationErrorCode.WRITE_ACCESS_DENIED,
    ProtectionReason.MOUNT_POINT: SafetyValidationErrorCode.MOUNT_POINT_DETECTED,
    ProtectionReason.SPARSE_FILE: SafetyValidationErrorCode.SPARSE_FILE_DETECTED,
    ProtectionReason.EMPTY_FILE: SafetyValidationErrorCode.EMPTY_FILE,
    ProtectionReason.READ_ONLY: SafetyValidationErrorCode.WRITE_ACCESS_DENIED,
    ProtectionReason.SYSTEM_OWNER: SafetyValidationErrorCode.SYSTEM_OWNER_PROTECTION,
    ProtectionReason.VIRTUAL_DRIVE: SafetyValidationErrorCode.VIRTUAL_DRIVE_DETECTED,
    ProtectionReason.SHADOW_COPY: SafetyValidationErrorCode.VOLUME_SHADOW_COPY,
}

def _evaluate_security_rules(state: SafetyState) -> None:
    """
    Ejecuta el conjunto de reglas de integridad sobre la ruta y lanza UnsafePathError.
    Utiliza un caché de integridad para optimizar el rendimiento.
    """
    if not state.path.exists():
         raise UnsafePathError("Archivo eliminado antes de la validación.", SafetyValidationErrorCode.IO_ERROR)
    
    # La clave de caché garantiza que no se valide innecesariamente un archivo inmutable
    cache_key = (str(state.path), int(state.stat_result.st_mtime), getattr(state.stat_result, 'st_ino', 0))
    if cache_key in _SECURITY_CACHE:
        return

    for rule in _VALIDATORS:
        if rule.predicate(state.path, state.stat_result, state.descriptor):
            code = _REASON_TO_CODE.get(rule.reason, SafetyValidationErrorCode.GENERIC)
            raise UnsafePathError(f"Integridad comprometida: {rule.reason.name}", code)
    
    if len(_SECURITY_CACHE) > 4096: _SECURITY_CACHE.clear()
    _SECURITY_CACHE[cache_key] = True

def _get_path_stat_robust(path: Path) -> os.stat_result:
    """
    Obtiene los atributos de estado (stat) de una ruta.
    Implementa reintentos para manejar bloqueos transitorios provocados por 
    procesos de segundo plano (indexadores, antivirus).
    """
    if not isinstance(path, Path):
        raise UnsafePathError("Tipo de objeto de ruta inválido", SafetyValidationErrorCode.GENERIC)
    
    if not path.exists():
        raise UnsafePathError(f"Archivo inexistente: {path.name}", SafetyValidationErrorCode.IO_ERROR)
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if _is_device_file(path):
                raise UnsafePathError(f"Acceso a dispositivo bloqueado: {path.name}", SafetyValidationErrorCode.DEVICE_FILE_DETECTED)
                
            if os.name == 'nt':
                kernel32 = ctypes.windll.kernel32
                # Intenta abrir con acceso GENERIC_READ para verificar acceso y compartición
                handle = kernel32.CreateFileW(
                    _to_long_path(str(path)), 0x80000000, 0x01, None, 3, 0x00000080, None
                )
                if handle == -1:
                    err = kernel32.GetLastError()
                    if err == ERROR_SHARING_VIOLATION:
                        time.sleep(0.05 * (2 ** attempt))
                        continue
                    if err == ERROR_ACCESS_DENIED:
                        raise UnsafePathError(f"Acceso denegado: {path.name}", SafetyValidationErrorCode.ACCESS_DENIED)
                else:
                    kernel32.CloseHandle(handle)
            
            return path.stat()
        except (OSError, PermissionError) as e:
            if getattr(e, 'errno', 0) in (errno.ETIMEDOUT, errno.EBUSY, errno.EAGAIN) or getattr(e, 'winerror', 0) == ERROR_SHARING_VIOLATION:
                if attempt < max_retries - 1:
                     time.sleep(0.05 * (2 ** attempt))
                     continue
                raise UnsafePathError(f"Archivo bloqueado tras reintentos: {path.name}", SafetyValidationErrorCode.FILE_IN_USE)
            raise UnsafePathError(f"Acceso fallido: {path.name}", SafetyValidationErrorCode.IO_ERROR)
    raise UnsafePathError(f"Error tras reintentos leyendo metadatos: {path.name}", SafetyValidationErrorCode.IO_ERROR)

def _check_file_integrity(state: SafetyState) -> None:
    """
    Verifica que el archivo no haya cambiado entre validaciones (mitigación TOCTOU).
    Compara el file_id actual contra el registrado originalmente en 'state'.
    """
    if not state.path.exists():
        raise UnsafePathError("El archivo ya no existe (TOCTOU).", SafetyValidationErrorCode.IO_ERROR)
    
    try:
        current_stat = _get_path_stat_robust(state.path)
        current_id = _get_file_id(str(state.path))
        
        if state.file_id != (0, 0, 0, 0) and current_id != state.file_id:
            raise UnsafePathError(f"Manipulación de identidad detectada (TOCTOU): {state.path.name}", SafetyValidationErrorCode.TOCTOU_VIOLATION)
        
        if stat.S_ISREG(state.stat_result.st_mode) != stat.S_ISREG(current_stat.st_mode):
            raise UnsafePathError(f"Cambio de tipo detectado (TOCTOU): {state.path.name}", SafetyValidationErrorCode.TOCTOU_VIOLATION)
        
        _evaluate_security_rules(state)
    except (OSError, PermissionError, UnsafePathError) as e:
        if isinstance(e, UnsafePathError): raise
        raise UnsafePathError(f"Fallo durante integridad: {e}", SafetyValidationErrorCode.IO_ERROR)

@lru_cache(maxsize=2048)
def _is_readonly(path_str: str) -> bool:
    """Comprueba permisos efectivos de escritura a nivel de SO."""
    if not path_str: return True
    try:
        path = Path(path_str)
        if not path.exists(): return True
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError, FileNotFoundError):
        return True

def _validate_access_permissions(path: Path) -> None:
    """Verifica permisos de lectura/escritura (R/W) efectivos para el proceso actual."""
    try:
        if not path.exists():
            raise UnsafePathError("Archivo inexistente, no se pueden validar permisos.", SafetyValidationErrorCode.ACCESS_DENIED)
        if not os.access(path, os.R_OK):
            raise UnsafePathError("Permisos de lectura denegados.", SafetyValidationErrorCode.ACCESS_DENIED)
        if not os.access(path, os.W_OK):
            raise UnsafePathError("Permisos de escritura denegados.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
    except (OSError, PermissionError) as e:
        raise UnsafePathError(f"Error de acceso: {e}", SafetyValidationErrorCode.ACCESS_DENIED)

def _validate_volume_integrity(path: Path) -> None:
    """Asegura que el volumen contenga capacidades operativas válidas para el proceso."""
    if os.name != 'nt': return
    try:
        path_s = str(path)
        if _is_volume_shadow_copy(path_s):
            raise UnsafePathError(f"Volumen de instantánea bloqueado: {path.anchor}", SafetyValidationErrorCode.VOLUME_SHADOW_COPY)
        if _is_volume_readonly(path_s) or _is_volume_compressed_or_encrypted(path_s) or \
           _is_virtual_drive(path_s) or _is_volume_busy_or_invalid(path_s):
            raise UnsafePathError(f"Volumen restringido/inaccesible: {path.anchor}", SafetyValidationErrorCode.VOLUME_RESTRICTED)
        
        # Validación extra contra Mount Point Hijacking: si es punto de montaje, verificar integridad
        if os.path.ismount(path):
            if not _get_final_path_normalized(path).drive == path.drive:
                 raise UnsafePathError(f"Punto de montaje en volumen externo: {path}", SafetyValidationErrorCode.MOUNT_POINT_DETECTED)
            raise UnsafePathError(f"Punto de montaje bloqueado: {path}", SafetyValidationErrorCode.MOUNT_POINT_DETECTED)
        _validate_drive_restrictions(path)
    except (OSError, PermissionError, AttributeError):
        raise UnsafePathError("Error validando integridad de volumen.", SafetyValidationErrorCode.IO_ERROR)

@lru_cache(maxsize=4096)
def _normalize_resolved(path_str: str) -> Path:
    """Normaliza y resuelve rutas absolutas de forma segura."""
    try:
        return Path(os.path.abspath(path_str)).resolve(strict=False)
    except (OSError, RuntimeError, PermissionError):
        return Path(path_str).absolute()

@lru_cache(maxsize=4096)
def normalize(path: PathLike) -> Path:
    """
    Normaliza y valida rutas, detectando inyecciones, traversal y caracteres prohibidos.
    """
    if path is None: raise UnsafePathError("Ruta nula recibida.", SafetyValidationErrorCode.GENERIC)
    path_str = str(path).strip()
    
    if os.name == 'nt' and _is_path_too_long(path_str):
        if not path_str.startswith(LONG_PATH_PREFIX):
             raise UnsafePathError("Ruta demasiado larga.", SafetyValidationErrorCode.PATH_TOO_LONG)

    if _is_path_empty_or_whitespace(path_str): raise UnsafePathError("Entrada de ruta vacía o inválida.", SafetyValidationErrorCode.GENERIC)
    if unicodedata.normalize('NFKC', path_str) != path_str:
         raise UnsafePathError("Ruta contiene secuencias Unicode sospechosas.", SafetyValidationErrorCode.SUSPICIOUS_ENCODING)
    try:
        p = Path(path_str)
        anchor_path = Path(p.anchor)
        parts = p.parts
        current_subpath = anchor_path
        
        if len(parts) > MAX_DIRECTORY_DEPTH:
            raise UnsafePathError("Profundidad de ruta excesiva.", SafetyValidationErrorCode.PATH_TOO_DEEP)

        for part in parts:
            if part in (os.sep, os.altsep): continue
            current_subpath = current_subpath / part
            if current_subpath.exists() and _is_system_directory_junction(str(current_subpath)):
                raise UnsafePathError("Segmento de ruta contiene punto de reparse.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        
        if _is_device_file(p): raise UnsafePathError("Acceso a dispositivo bloqueado.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED)
        if _has_alternate_data_stream(p.name): raise UnsafePathError("Flujo de datos alternativo detectado.", SafetyValidationErrorCode.ADS_DETECTED)
        
        resolved = _normalize_resolved(path_str)
        if ".." in parts: raise UnsafePathError("Path traversal detectado.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
        return resolved
    except (OSError, PermissionError, RuntimeError, ValueError, TypeError) as e:
        raise UnsafePathError(f"Error al normalizar ruta {path_str}: {e}", SafetyValidationErrorCode.IO_ERROR)

def is_absolute_path_allowed(path: PathLike) -> bool:
    """Verifica si el path es absoluto."""
    try: return Path(path).is_absolute()
    except (TypeError, ValueError): return False

def is_drive_root(path: PathLike) -> bool:
    """Verifica si la ruta corresponde a la raíz de una unidad lógica."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (UnsafePathError, TypeError, OSError): return True

@lru_cache(maxsize=1024)
def is_protected_path(path: PathLike) -> bool:
    """Determina si la ruta reside en una zona protegida por el SO."""
    if not isinstance(path, (str, Path)) or not path: return True
    
    try:
        norm_str = os.path.normcase(os.path.abspath(str(path)))
        parts = Path(norm_str).parts
        
        if any(norm_str.startswith(r) for r in _SYSTEM_ROOT_PATHS):
            return True
            
        if not PROTECTED_DIR_NAMES.isdisjoint(part.lower() for part in parts):
            return True
        
        attrs = _get_file_attrs(norm_str)
        if attrs != INVALID_FILE_ATTRIBUTES and (attrs & (Win32Attr.SYSTEM | Win32Attr.HIDDEN | Win32Attr.REPARSE_POINT)):
            return True
            
        return len(parts) <= 1
    except (OSError, AttributeError, ValueError):
        return True

def _get_final_path_normalized(p: Path) -> Path:
    """
    Resuelve el path final en Windows usando Handles para saltar puntos de unión
    (junctions) y enlaces simbólicos.
    """
    if os.name != 'nt':
        return p.resolve()
    
    kernel32 = ctypes.windll.kernel32
    try:
        p.lstat()
    except (OSError, FileNotFoundError, PermissionError):
        return p.resolve(strict=False)

    handle = kernel32.CreateFileW(
        _to_long_path(str(p)), 0, 0, None, 3, 0x02000000, None
    )
    if handle == -1:
        return p.resolve(strict=False)
    
    try:
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.GetFinalPathNameByHandleW(handle, buf, 1024, 0) > 0:
            return Path(buf.value)
    finally:
        kernel32.CloseHandle(handle)
    return p.resolve(strict=False)

@lru_cache(maxsize=4096)
def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Verifica contención de rutas evitando escapes (jailbreak) mediante resolución de enlaces."""
    if child is None or parent is None: return False
    try:
        c_path = normalize(child)
        p_path = normalize(parent)
        if is_drive_root(c_path) or is_protected_path(str(c_path)): return False
        
        final_c = _get_final_path_normalized(c_path)
        final_p = _get_final_path_normalized(p_path)
        
        return final_c.is_relative_to(final_p) if allow_equal else (final_c.is_relative_to(final_p) and final_c != final_p)
    except (UnsafePathError, TypeError, OSError, RuntimeError, ValueError): return False

@lru_cache(maxsize=2048)
def is_sensitive_file(path: PathLike) -> bool:
    """Determina si un archivo es sensible basado en su extensión."""
    if not path: return True
    try: 
        p_str = str(path)
        return _is_sensitive_extension(os.path.splitext(p_str)[1])
    except (TypeError, ValueError, OSError): return True 

def _validate_structural_safety(target_path: Path, path_string: str) -> None:
    """Validación sintáctica para prevenir inyecciones y accesos a namespaces peligrosos."""
    if path_string in _STRUCTURAL_CACHE:
        return

    if not isinstance(path_string, str):
        raise UnsafePathError("Ruta no es texto.", SafetyValidationErrorCode.GENERIC)
    
    if re.search(r'[\u200E\u200F\u202A-\u202E\u206A-\u206F]', path_string):
        raise UnsafePathError("Secuencias de escape visual detectadas.", SafetyValidationErrorCode.INVALID_CHARS)
    
    if path_string.startswith(("\\\\.\\", "//./", LONG_PATH_PREFIX)) and not path_string.startswith(LONG_PATH_PREFIX):
        raise UnsafePathError("Acceso a Namespace de dispositivos prohibido.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED)
    if _is_unc_path(path_string) and not _is_unc_path_accessible(path_string):
        raise UnsafePathError("Rutas UNC/Red bloqueadas o inaccesibles.", SafetyValidationErrorCode.UNC_PATH)
    if ".." in path_string.split(os.sep):
        raise UnsafePathError("Path traversal detectado.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    if _is_path_too_long(path_string):
        raise UnsafePathError("Ruta demasiado larga.", SafetyValidationErrorCode.PATH_TOO_LONG)
    if "\0" in path_string:
        raise UnsafePathError("Inyección de carácter nulo.", SafetyValidationErrorCode.NULL_CHAR)
    if _has_invalid_chars(path_string):
        raise UnsafePathError("Caracteres inválidos detectados.", SafetyValidationErrorCode.INVALID_CHARS)
    if unicodedata.normalize('NFKC', path_string) != path_string:
        raise UnsafePathError("Codificación de caracteres sospechosa.", SafetyValidationErrorCode.SUSPICIOUS_ENCODING)
    if _has_alternate_data_stream(target_path.name):
        raise UnsafePathError("Flujo de datos alternativo detectado.", SafetyValidationErrorCode.ADS_DETECTED)
    if _is_device_file(target_path):
        raise UnsafePathError("Acceso a dispositivo bloqueado.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED)
    if _RESERVED_NAMES_PATTERN.fullmatch(target_path.name):
        raise UnsafePathError("Nombre de dispositivo reservado detectado.", SafetyValidationErrorCode.RESERVED_NAME)

    try:
        t_parts = target_path.parts
        if target_path.exists() and not target_path.is_absolute():
            raise UnsafePathError("Ruta inconsistente con el sistema.", SafetyValidationErrorCode.GENERIC)
        if not t_parts or (len(t_parts) == 1 and t_parts[0] == os.sep):
             raise UnsafePathError("Ruta raíz no permitida.", SafetyValidationErrorCode.ROOT_ACCESS)
        if len(t_parts) == 1 and target_path == Path(target_path.anchor):
             raise UnsafePathError("Operación sobre raíz de unidad prohibida.", SafetyValidationErrorCode.ROOT_ACCESS)
        if len(t_parts) > MAX_DIRECTORY_DEPTH:
             raise UnsafePathError("Profundidad de directorio excesiva.", SafetyValidationErrorCode.PATH_TOO_DEEP)
    except (AttributeError, TypeError, ValueError):
        raise UnsafePathError("Estructura de ruta inválida.", SafetyValidationErrorCode.GENERIC)

    if len(_STRUCTURAL_CACHE) > 2048: _STRUCTURAL_CACHE.clear()
    _STRUCTURAL_CACHE[path_string] = True

def _validate_preconditions(target_path: Path, base_dir: Optional[PathLike]) -> None:
    """Verificaciones de seguridad previas a cualquier operación de escritura."""
    if not is_absolute_path_allowed(target_path):
        raise UnsafePathError("Solo se permiten rutas absolutas.", SafetyValidationErrorCode.RELATIVE_PATH_NOT_ALLOWED)
    
    if is_running_as_admin():
        if _is_file_owned_by_system(str(target_path)) or _is_kernel_managed(str(target_path)):
            raise UnsafePathError("Operación denegada en modo privilegiado.", SafetyValidationErrorCode.ADMIN_PRIVILEGE_VIOLATION)

    try:
        attrs = _get_file_attrs(str(target_path))
        if attrs != INVALID_FILE_ATTRIBUTES:
            if attrs & (Win32Attr.SYSTEM | Win32Attr.HIDDEN):
                raise UnsafePathError("Protección defensiva: Archivo de sistema oculto.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)
            if attrs & Win32Attr.OFFLINE:
                raise UnsafePathError("Protección defensiva: Archivo offline.", SafetyValidationErrorCode.OFFLINE_FILE)
            if attrs & Win32Attr.REPARSE_POINT:
                raise UnsafePathError("Protección defensiva: Redirección no autorizada.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        
        if target_path.parent.exists() and not os.access(target_path.parent, os.W_OK):
            raise UnsafePathError("Directorio contenedor sin permisos de escritura.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
        
        if os.name == 'nt' and target_path.parent.exists() and _is_volume_readonly(str(target_path.parent)):
            raise UnsafePathError("Directorio contenedor en volumen de solo lectura.", SafetyValidationErrorCode.VOLUME_READ_ONLY)

        if _is_kernel_managed(str(target_path)):
            raise UnsafePathError(f"Archivo de sistema crítico: {target_path.name}", SafetyValidationErrorCode.KERNEL_LOCKED_FILE)
        
        _validate_volume_integrity(target_path)
    except (OSError, PermissionError):
        raise UnsafePathError("Error accediendo a precondiciones.", SafetyValidationErrorCode.IO_ERROR)

def _validate_boundary_conditions(target_path: Path, root_directory: Optional[PathLike]) -> None:
    """Valida los límites del alcance operativo permitido."""
    if root_directory:
        try:
            rd_path = Path(root_directory)
            if not rd_path.is_absolute():
                raise UnsafePathError("El directorio base debe ser absoluto.", SafetyValidationErrorCode.RELATIVE_PATH_NOT_ALLOWED)
            if not is_within_directory(target_path, rd_path, allow_equal=True):
                raise UnsafePathError("Fuera de alcance permitido.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
        except (TypeError, ValueError, OSError):
            raise UnsafePathError("Directorio base inválido.", SafetyValidationErrorCode.GENERIC)

    if is_protected_path(str(target_path)):
        raise UnsafePathError("Ruta en directorio del sistema bloqueada.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)
    
    try:
        app_root = _normalize_resolved(os.getcwd())
        if target_path == app_root or app_root in target_path.parents:
            raise UnsafePathError("Modificación de App denegada.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
    except (OSError, RuntimeError, ValueError): pass
    if is_drive_root(target_path):
        raise UnsafePathError("Acceso a raíz denegado.", SafetyValidationErrorCode.ROOT_ACCESS)

def _validate_ntfs_reparse_redirection(path: Path, base_dir: Optional[Path]) -> None:
    """Verifica la seguridad de las redirecciones mediante puntos de unión NTFS."""
    for p in [path] + list(path.parents):
        if _is_system_directory_junction(str(p)):
            _validate_junction_depth(p)
            final_path = _get_final_path_normalized(p)
            if base_dir and not final_path.is_relative_to(base_dir):
                raise UnsafePathError(f"Redirección insegura en reparse point: {p}", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)

def _validate_path_components(path: Path) -> None:
    """Verifica recursión circular y spoofing en componentes de ruta."""
    try:
        resolved_path = path.resolve()
    except (OSError, RuntimeError, PermissionError):
        resolved_path = path

    anchor = Path(resolved_path.anchor)
    parts = resolved_path.parts
    visited: set[Path] = set()

    if len(parts) > MAX_DIRECTORY_DEPTH:
        raise UnsafePathError("Ruta demasiado profunda.", SafetyValidationErrorCode.PATH_TOO_DEEP)

    for i in range(len(parts)):
        current_check = anchor.joinpath(*parts[:i+1])
        if "~" in current_check.name or re.search(r'[\u200E\u200F\u202A-\u202E]', current_check.name):
            raise UnsafePathError("Ruta con spoofing de nombres detectada.", SafetyValidationErrorCode.SHORT_NAME_SPOOFING)
        
        if current_check.exists():
            try:
                resolved = current_check.resolve()
                if resolved in visited:
                    raise UnsafePathError(f"Referencia circular detectada: {current_check}", SafetyValidationErrorCode.CIRCULAR_REFERENCE)
                visited.add(resolved)
                
                if _is_system_directory_junction(str(current_check)):
                    if _get_final_path_normalized(current_check) is None:
                        raise UnsafePathError(f"Punto de reparse inaccesible: {current_check}", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
            except (OSError, RuntimeError, PermissionError):
                continue
    
    _validate_no_reparse_traversal(path)

def _validate_no_reparse_traversal(path: Path) -> None:
    """Impide bypass de jail mediante redirecciones en niveles intermedios de la ruta."""
    try:
        current = Path(path.anchor)
        depth = 0
        for part in path.parts[1:]:
            current = current / part
            if current.is_symlink() or _is_system_directory_junction(str(current)):
                _validate_junction_depth(current)
                target = _get_final_path_normalized(current)
                if not target.is_relative_to(Path(path.anchor)):
                    raise UnsafePathError(f"Escape inseguro detectado: {current}", SafetyValidationErrorCode.OUT_OF_BOUNDS)
            depth += 1
            if depth > MAX_DIRECTORY_DEPTH:
                raise UnsafePathError("Profundidad de redirección excesiva.", SafetyValidationErrorCode.PATH_TOO_DEEP)
    except (Exception, UnsafePathError) as e:
        if isinstance(e, UnsafePathError): raise
        pass

def _validate_file_operations_security(p: Path, allow_sensitive: bool, base: Optional[Path]) -> None:
    """Valida la integridad de archivos antes de operaciones de escritura."""
    if not p.exists():
        raise UnsafePathError(f"El archivo fue eliminado: {p.name}", SafetyValidationErrorCode.ACCESS_DENIED)
    
    _validate_access_permissions(p)
    if _is_file_in_use_by_system(str(p)):
        raise UnsafePathError(f"Archivo en uso por el sistema: {p.name}", SafetyValidationErrorCode.FILE_IN_USE)
    
    state = SafetyState(
        path=p,
        descriptor=_get_security_descriptor(p),
        stat_result=_get_path_stat_robust(p),
        file_id=_get_file_id(str(p))
    )
    
    if os.name == 'nt': 
        _validate_ntfs_reparse_redirection(p, base)
    _check_file_integrity(state)

def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> Path:
    """
    Realiza la validación jerárquica de una ruta para asegurar que sea segura para su modificación.
    
    El flujo se divide en:
    1. Normalización, análisis de namespace y estructura.
    2. Verificaciones de límites de alcance y restricciones de volumen.
    3. Verificación de permisos y precondiciones de sistema.
    4. Integridad en disco (mitigación TOCTOU).
    """
    if path is None:
        raise UnsafePathError("Ruta nula recibida.", SafetyValidationErrorCode.GENERIC)
        
    p = normalize(str(path).strip())
    
    # 1. Análisis Estructural
    if p.exists() and not (p.is_file() or p.is_dir()):
        raise UnsafePathError(f"Tipo de archivo no soportado: {p.name}", SafetyValidationErrorCode.INVALID_TYPE)
    if p.name.lower() in _KERNEL_BLOCKED_FILES:
        raise UnsafePathError(f"Modificación bloqueada de archivo crítico: {p.name}", SafetyValidationErrorCode.KERNEL_LOCKED_FILE)
    
    base = None
    if base_dir:
        try: base = _normalize_resolved(str(base_dir))
        except (TypeError, ValueError, OSError) as e:
            raise UnsafePathError(f"Directorio base inválido: {e}", SafetyValidationErrorCode.IO_ERROR)
    
    if os.name == 'nt': _validate_ntfs_reparse_redirection(p, base)
    _validate_path_components(p)
    _validate_structural_safety(p, str(p))
    
    # 2. Límites y Volúmenes
    _validate_boundary_conditions(p, base_dir)
    _validate_preconditions(p, base_dir)
    
    # 3. Sensibilidad y Seguridad de Operación
    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError(f"Extensión bloqueada '{p.suffix}'.", SafetyValidationErrorCode.SENSITIVE_EXTENSION)
            
    # 4. Integridad (TOCTOU)
    if p.exists():
        _validate_file_operations_security(p, allow_sensitive, base)
    else:
        if p.parent.exists() and not os.access(p.parent, os.W_OK):
            raise UnsafePathError("Directorio contenedor sin permisos de escritura.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
    
    return p

def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> bool:
    """Verificación booleana de seguridad de ruta (wrapper de ensure_safe_to_modify)."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive, base_dir=base_dir)
        return True
    except (UnsafePathError, ValueError, TypeError, OSError, PermissionError, AttributeError):
        return False

def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> list[Path]:
    """Filtra una colección de rutas, retornando solo aquellas que superan las validaciones."""
    results: list[Path] = []
    if paths is None: return results
    for p in paths:
        if p is None: continue
        try: results.append(ensure_safe_to_modify(p, allow_sensitive=allow_sensitive, base_dir=base_dir))
        except (UnsafePathError, ValueError, TypeError, OSError, PermissionError, AttributeError): continue
    return results

def describe_protection(path: PathLike) -> str:
    """Retorna un diagnóstico descriptivo sobre el estado de protección de una ruta."""
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
            sd = _get_security_descriptor(p)
            diagnostics = {
                "enlace simbólico": p.is_symlink(),
                "punto de reparse": sd.is_reparse,
                "punto de montaje": os.path.ismount(p),
                "unidad virtual mapeada": _is_virtual_drive(str(p)),
                "atributo de solo lectura": sd.is_readonly,
                "solo lectura (permisos)": _is_readonly(str(p)),
                "volumen de solo lectura": _is_volume_readonly(str(p)),
                "volumen extraíble": _is_volume_removable_media(str(p)),
                "volumen cifrado/comprimido": _is_volume_compressed_or_encrypted(str(p)),
                "archivo en uso": sd.is_in_use,
                "archivo cifrado o comprimido": sd.has_flag(Win32Attr.COMPRESSED) or sd.has_flag(Win32Attr.ENCRYPTED),
                "archivo disperso (sparse)": sd.has_flag(Win32Attr.SPARSE_FILE),
                "archivo offline/nube": sd.has_flag(Win32Attr.OFFLINE),
                "atributo oculto/sistema": sd.is_protected_system,
                "contiene ADS": _has_alternate_data_stream(p.name),
                "tamaño excesivo": p.is_file() and p.stat().st_size > MAX_FILE_SIZE,
                "detectado como hard link": p.is_file() and p.stat().st_nlink > 1,
                "volumen de instantánea (VSS)": _is_volume_shadow_copy(str(p))
            }
            for reason, is_active in diagnostics.items():
                if is_active: return f"'{p}' es {reason}."
            
            if not (p.is_file() or p.is_dir()): return f"'{p}' tipo de objeto no soportado."
            if p.is_file() and p.stat().st_size == 0: return f"'{p}' archivo vacío (potencialmente crítico)."
            
    except (OSError, FileNotFoundError, AttributeError): pass
    
    if is_sensitive_file(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
