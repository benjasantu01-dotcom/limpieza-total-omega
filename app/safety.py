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
ViolationPredicate: TypeAlias = Callable[[Path, os.stat_result, "SecurityDescriptor"], bool]

class SafetyAction(Enum):
    """Define la criticidad de la operación para ajustar el rigor de la validación."""
    READ = auto()      # Listado, escaneo, análisis
    MODIFY = auto()    # Mover, renombrar, borrar, editar

class SecurityDescriptor(NamedTuple):
    """
    Estado consolidado de un archivo tras consultar metadatos del sistema.
    
    Attributes:
        attrs: Máscara de bits con atributos de Win32 (GetFileAttributesW).
        is_protected_system: Indica si el archivo tiene atributos de sistema, oculto o temporal.
        is_in_use: Indica si el archivo posee un bloqueo de escritura por otro proceso.
    """
    attrs: int
    is_protected_system: bool
    is_in_use: bool
    
    def has_flag(self, flag: Win32Attr) -> bool:
        """Verifica si un atributo específico está presente en el descriptor."""
        return bool(self.attrs & flag)

class FileMetadata(TypedDict):
    """
    Representación de los atributos clave para auditoría de integridad.
    
    Utilizado por módulos externos para decidir si un archivo cumple
    con los criterios de limpieza o escaneo seguros.
    """
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

@lru_cache(maxsize=4096)
def _to_long_path(path_str: str) -> str:
    """
    Transforma la ruta al formato '\\?\' para permitir el acceso a rutas que exceden 
    los 260 caracteres (MAX_PATH) en Windows.
    """
    if os.name == 'nt' and not path_str.startswith("\\\\?\\"):
        if path_str.startswith("\\\\"): return "\\\\?\\UNC" + path_str[1:]
        return "\\\\?\\" + path_str
    return path_str

def _is_path_too_long(path_str: str) -> bool:
    """Verifica si la longitud de la cadena excede el estándar MAX_PATH."""
    return len(path_str) > MAX_PATH_LENGTH

@lru_cache(maxsize=2048)
def _get_file_attrs(path_str: Optional[str]) -> int:
    """
    Consulta los atributos de archivo mediante la API Win32 GetFileAttributesW.
    Permite detectar flags de sistema, ocultos o puntos de reparse.
    """
    if os.name != 'nt' or not path_str or _is_path_too_long(path_str): return 0
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(_to_long_path(path_str))
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
    VOLUME_RESTRICTED = 30

class UnsafePathError(Exception):
    """Excepción lanzada cuando una ruta no supera los filtros de seguridad."""
    def __init__(self, message: str, code: SafetyValidationErrorCode = SafetyValidationErrorCode.GENERIC):
        super().__init__(f"[{code.name}] {message}")
        self.code = code

class ProtectionReason(Enum):
    """Categorías de riesgo utilizadas para clasificar violaciones de seguridad."""
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

class ValidationContext(Enum):
    """Contexto de la validación: Estructural (nombres) o Integridad (disco)."""
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
    """Verifica si el proceso actual cuenta con privilegios elevados (Administrador)."""
    if os.name != 'nt':
        try: return os.geteuid() == 0
        except (AttributeError, OSError): return False
    try:
        shell32 = ctypes.windll.shell32
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False

def _has_invalid_chars(path_str: Optional[str]) -> bool:
    """Detecta caracteres de control invisibles o prohibidos en nombres de rutas de Windows."""
    if not isinstance(path_str, str) or not path_str: return True
    return bool(re.search(r'[\u0000-\u001F\u007F-\u009F\u200E\u200F\u202A-\u202E\u206A-\u206F]', path_str))

def _is_path_empty_or_whitespace(path_str: str) -> bool:
    """Valida si la cadena está vacía o solo contiene espacios en blanco."""
    return not path_str or path_str.isspace()

def _is_unc_path(path_str: str) -> bool:
    """Verifica si la ruta apunta a un recurso de red mediante formato UNC (\\servidor\recurso)."""
    return path_str.startswith(("\\\\", "//"))

@lru_cache(maxsize=128)
def _is_reserved_device_name(name: str) -> bool:
    """Verifica si el nombre corresponde a un dispositivo reservado (ej. NUL, CON) que puede causar bloqueos."""
    return bool(_RESERVED_NAMES_PATTERN.fullmatch(name))

def _is_device_file(path: Path) -> bool:
    """Detecta si la ruta es un acceso directo a un dispositivo (NT Device Path)."""
    path_str = str(path).upper()
    return path_str.startswith(("\\\\.\\", "//./"))

@lru_cache(maxsize=512)
def _has_alternate_data_stream(path_name: str) -> bool:
    """Detecta flujos de datos alternativos (ADS) NTFS, comúnmente utilizados para ocultar payloads."""
    return ":" in path_name and len(path_name.split(":")) > 2

@lru_cache(maxsize=1024)
def _get_security_descriptor_cached(path_str: str, mtime: float) -> SecurityDescriptor:
    """Versión cacheada del descriptor de seguridad vinculada al path y su timestamp de modificación."""
    attrs = _get_file_attrs(path_str)
    in_use = False
    try:
        in_use = _is_file_locked_by_other_process(path_str)
    except (OSError, AttributeError, ctypes.ArgumentError):
        in_use = True
    return SecurityDescriptor(
        attrs=attrs,
        is_protected_system=bool(attrs & (Win32Attr.HIDDEN | Win32Attr.SYSTEM | Win32Attr.OFFLINE | Win32Attr.TEMPORARY)),
        is_in_use=in_use
    )

def _get_security_descriptor(path: Path) -> SecurityDescriptor:
    """
    Construye un descriptor de seguridad para evaluar el archivo en un instante dado.
    Utiliza el mtime del archivo para invalidar el cache si el archivo cambia.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return _get_security_descriptor_cached(str(path), mtime)

@lru_cache(maxsize=1024)
def _is_file_locked_by_other_process(path_str: str) -> bool:
    """
    Verifica si un archivo está en uso exclusivo mediante la API CreateFile.
    Si el handle falla con sharing violation, se considera el archivo bloqueado.
    """
    if not isinstance(path_str, str) or os.name != 'nt' or _is_path_too_long(path_str): return False
    kernel32 = ctypes.windll.kernel32
    try:
        handle = kernel32.CreateFileW(
            _to_long_path(path_str), 0, 0x00000003, None, 3, 0x00000080, None
        )
        if handle == -1: return True
        kernel32.CloseHandle(handle)
        return False
    except (OSError, ctypes.ArgumentError, AttributeError):
        return True

def _is_file_in_use_by_system(path_str: str) -> bool:
    """Verifica si el archivo está siendo referenciado por módulos cargados del sistema."""
    if os.name != 'nt': return False
    try:
        h_module = ctypes.windll.kernel32.GetModuleHandleW(path_str)
        return h_module != 0
    except (OSError, ctypes.ArgumentError): return False

@lru_cache(maxsize=128)
def _is_volume_readonly(path_str: Optional[str]) -> bool:
    """Consulta los atributos del volumen para verificar si el archivo reside en un medio de solo lectura."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str or _is_path_too_long(path_str): return False
    try:
        drive_path = os.path.splitdrive(path_str)[0]
        if not drive_path: return False
        root = drive_path + "\\"
        flags = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0) != 0:
            return bool(flags.value & 0x80000)
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError):
        pass
    return False

@lru_cache(maxsize=128)
def _is_volume_compressed_or_encrypted(path_str: Optional[str]) -> bool:
    """Verifica si el volumen está comprimido o cifrado (BitLocker), restringiendo modificaciones."""
    if os.name != 'nt' or not isinstance(path_str, str) or not path_str or _is_path_too_long(path_str): return False
    try:
        drive_path = os.path.splitdrive(path_str)[0]
        if not drive_path: return False
        root = drive_path + "\\"
        flags = ctypes.c_ulong()
        # FILE_FILE_COMPRESSION (0x10) | FILE_SUPPORTS_ENCRYPTION (0x20000)
        if ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0) != 0:
            return bool(flags.value & (0x10 | 0x20000))
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError):
        pass
    return False

def _is_directory_junction(path_str: str) -> bool:
    """Verifica específicamente si una ruta es una carpeta tipo Junction de NTFS."""
    attrs = _get_file_attrs(path_str)
    return bool(attrs & Win32Attr.DIRECTORY and attrs & Win32Attr.REPARSE_POINT)

def _is_kernel_managed(path: Path) -> bool:
    """Identifica archivos del núcleo bloqueados permanentemente (ej. pagefile.sys), evitando su manipulación."""
    return path.name.lower() in ("pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log.tmp")

@lru_cache(maxsize=1024)
def _is_sensitive_extension(ext: str) -> bool:
    """Valida si la extensión está en la lista de archivos cuya modificación implica riesgo crítico."""
    return ext.lower() in SENSITIVE_EXTENSIONS

def _rule(reason: ProtectionReason, predicate: ViolationPredicate) -> _IntegrityCheck:
    """Helper de fábrica para definir una nueva regla de integridad."""
    return _IntegrityCheck(reason, predicate)

# Lista de validadores de integridad aplicada secuencialmente
_VALIDATORS: Final[list[_IntegrityCheck]] = [
    _rule(ProtectionReason.SYMLINK, lambda p, _, __: p.is_symlink()),
    _rule(ProtectionReason.REPARSE_POINT, lambda p, _, __: _is_directory_junction(str(p))),
    _rule(ProtectionReason.KERNEL_LOCKED, lambda p, _, __: _is_kernel_managed(p)),
    _rule(ProtectionReason.READ_ONLY, lambda _, st, __: not bool(st.st_mode & stat.S_IWRITE)),
    _rule(ProtectionReason.VOLUME_READ_ONLY, lambda p, _, __: _is_volume_readonly(str(p))),
    _rule(ProtectionReason.VOLUME_RESTRICTED, lambda p, _, __: _is_volume_compressed_or_encrypted(str(p))),
    _rule(ProtectionReason.IN_USE, lambda _, __, sd: sd.is_in_use),
    _rule(ProtectionReason.SYSTEM_HIDDEN, lambda _, __, sd: sd.is_protected_system),
    _rule(ProtectionReason.OFFLINE, lambda _, __, sd: sd.has_flag(Win32Attr.OFFLINE)),
    _rule(ProtectionReason.ENCRYPTED_OR_COMPRESSED, lambda _, __, sd: bool(sd.attrs & (Win32Attr.COMPRESSED | Win32Attr.ENCRYPTED))),
    _rule(ProtectionReason.SPARSE_FILE, lambda _, __, sd: sd.has_flag(Win32Attr.SPARSE_FILE)),
    _rule(ProtectionReason.HARD_LINK, lambda p, st, __: p.is_file() and st.st_nlink > 1),
    _rule(ProtectionReason.ADS, lambda p, _, __: _has_alternate_data_stream(p.name)),
    _rule(ProtectionReason.EMPTY_FILE, lambda p, st, __: p.is_file() and st.st_size == 0),
    _rule(ProtectionReason.EXCESSIVE_SIZE, lambda p, st, __: p.is_file() and st.st_size > MAX_FILE_SIZE),
    _rule(ProtectionReason.MOUNT_POINT, lambda p, _, __: os.path.ismount(p)),
    _rule(ProtectionReason.INVALID_TYPE, lambda _, st, __: not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode))),
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
    ProtectionReason.VOLUME_RESTRICTED: SafetyValidationErrorCode.VOLUME_RESTRICTED,
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
    Ejecuta el conjunto de reglas de integridad sobre un archivo dado.
    Si cualquier regla falla, se aborta la operación con una excepción.
    """
    sd = _get_security_descriptor(path)
    for rule in _VALIDATORS:
        if rule.predicate(path, current_stat, sd):
            code = _REASON_TO_CODE.get(rule.reason, SafetyValidationErrorCode.GENERIC)
            raise UnsafePathError(f"Integridad comprometida: {rule.reason.name}", code)

def _get_path_stat_robust(path: Path) -> os.stat_result:
    """
    Obtiene los metadatos de un archivo de manera segura, bloqueando el acceso a archivos de dispositivo.
    Valida que el archivo no sea un dispositivo o un punto de reparse antes de realizar la consulta stat().
    """
    if not isinstance(path, Path):
        raise UnsafePathError("Tipo de objeto de ruta inválido", SafetyValidationErrorCode.GENERIC)
    if _is_device_file(path):
        raise UnsafePathError(f"Acceso a dispositivo bloqueado: {path.name}", SafetyValidationErrorCode.DEVICE_FILE_DETECTED)
    if _is_directory_junction(str(path)):
        raise UnsafePathError(f"Punto de reparse detectado durante acceso estático: {path.name}", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
    try:
        return path.stat()
    except (PermissionError, OSError) as e:
        if getattr(e, 'winerror', 0) == 32:
             raise UnsafePathError(f"Archivo bloqueado por otro proceso: {path.name}", SafetyValidationErrorCode.FILE_IN_USE)
        raise UnsafePathError(f"Acceso fallido: {path.name}", SafetyValidationErrorCode.ACCESS_DENIED)
    except (ValueError, TypeError) as e:
        raise UnsafePathError(f"Error al leer metadatos de {path.name}: {e}", SafetyValidationErrorCode.IO_ERROR)

def _check_file_integrity(path: Path, initial_stat: os.stat_result) -> None:
    """
    Previene ataques Time-of-Check to Time-of-Use (TOCTOU).
    Verifica que el archivo no haya sido reemplazado y pertenezca a un volumen local permitido
    comparando los identificadores de dispositivo e inodo contra el estado inicial.
    """
    if not path.exists():
        raise UnsafePathError("El archivo ya no existe (TOCTOU).", SafetyValidationErrorCode.IO_ERROR)
        
    if os.name == 'nt':
        root = path.anchor
        if root:
            try:
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
                if drive_type not in (DRIVE_FIXED, DRIVE_RAMDISK):
                    raise UnsafePathError(f"Volumen no compatible/remoto: {root}", SafetyValidationErrorCode.IO_ERROR)
            except (AttributeError, ctypes.ArgumentError):
                pass

    if not os.access(path, os.R_OK):
        raise UnsafePathError(f"Acceso de lectura denegado a {path.name}", SafetyValidationErrorCode.ACCESS_DENIED)
    
    current_stat = _get_path_stat_robust(path)
    
    if current_stat.st_dev != initial_stat.st_dev or current_stat.st_ino != initial_stat.st_ino:
        raise UnsafePathError(f"Consistencia fallida (TOCTOU): {path.name}", SafetyValidationErrorCode.TOCTOU_VIOLATION)
    
    if _is_directory_junction(str(path)):
        raise UnsafePathError(f"Junction detectada: {path.name}", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        
    _evaluate_security_rules(path, current_stat)

@lru_cache(maxsize=2048)
def _is_readonly(path_str: str) -> bool:
    """Determina si un archivo es de solo lectura mediante el estado de sus permisos en el sistema."""
    try:
        path = Path(path_str)
        if not path.exists(): return True
        return not bool(path.stat().st_mode & stat.S_IWRITE)
    except (OSError, PermissionError, FileNotFoundError):
        return True

def _validate_access_permissions(path: Path) -> None:
    """Valida los permisos de lectura y escritura del usuario actual sobre el archivo."""
    try:
        if not os.access(path, os.R_OK):
            raise UnsafePathError("Permisos de lectura denegados.", SafetyValidationErrorCode.ACCESS_DENIED)
        if not os.access(path, os.W_OK):
            raise UnsafePathError("Permisos de escritura denegados.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
    except PermissionError:
        raise UnsafePathError("Acceso al archivo denegado por el sistema.", SafetyValidationErrorCode.ACCESS_DENIED)

@lru_cache(maxsize=4096)
def normalize(path: PathLike) -> Path:
    """
    Normaliza una ruta y valida componentes para prevenir ataques de Path Traversal.
    Aplica normalización NFKC para prevenir bypasses por caracteres equivalentes Unicode.
    """
    if path is None: raise UnsafePathError("Ruta nula recibida.", SafetyValidationErrorCode.GENERIC)
    path_str = str(path).strip()
    if _is_path_empty_or_whitespace(path_str): raise UnsafePathError("Entrada de ruta vacía o inválida.", SafetyValidationErrorCode.GENERIC)
    if _is_path_too_long(path_str):
        raise UnsafePathError("Ruta demasiado larga.", SafetyValidationErrorCode.PATH_TOO_LONG)
    if unicodedata.normalize('NFKC', path_str) != path_str:
         raise UnsafePathError("Ruta contiene secuencias Unicode sospechosas.", SafetyValidationErrorCode.SUSPICIOUS_ENCODING)
    try:
        p = Path(path_str)
        current_subpath = Path(p.anchor)
        for part in p.parts:
            if part in (os.sep, os.altsep): continue
            current_subpath = current_subpath / part
            if os.path.exists(str(current_subpath)) and _is_directory_junction(str(current_subpath)):
                raise UnsafePathError("Segmento de ruta contiene punto de reparse.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        
        if _is_device_file(p): raise UnsafePathError("Acceso a dispositivo bloqueado.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED)
        if _has_alternate_data_stream(p.name): raise UnsafePathError("Flujo de datos alternativo detectado.", SafetyValidationErrorCode.ADS_DETECTED)
        
        resolved = p.resolve()
        if ".." in p.parts: raise UnsafePathError("Path traversal detectado.", SafetyValidationErrorCode.OUT_OF_BOUNDS)
        return resolved
    except (OSError, PermissionError, RuntimeError, ValueError, TypeError) as e:
        raise UnsafePathError(f"Error al normalizar ruta {path_str}: {e}", SafetyValidationErrorCode.IO_ERROR)

def is_absolute_path_allowed(path: PathLike) -> bool:
    """Verifica si la ruta provista cumple con el requisito de ser absoluta para garantizar la trazabilidad."""
    try: return Path(path).is_absolute()
    except (TypeError, ValueError): return False

def is_drive_root(path: PathLike) -> bool:
    """Verifica si la ruta apunta a la raíz de un volumen físico para prevenir borrados accidentales de disco."""
    try:
        p = normalize(path)
        return p == Path(p.anchor)
    except (UnsafePathError, TypeError, OSError): return True

@lru_cache(maxsize=4096)
def _is_system_path_raw(path_str: str) -> bool:
    """Comprueba si una ruta pertenece a directorios críticos del sistema basándose en nombres protegidos."""
    path_lower = path_str.lower()
    if any(path_lower.startswith(root) for root in _SYSTEM_ROOT_PATHS_TUPLE):
        return True
    
    # Búsqueda eficiente basada en la intersección de partes del path con el conjunto de nombres protegidos
    parts_set = set(path_lower.split(os.sep))
    return bool(parts_set & PROTECTED_DIR_NAMES)

@lru_cache(maxsize=4096)
def is_protected_path(path: PathLike) -> bool:
    """Valida si la ruta está marcada como protegida contra modificaciones del usuario."""
    if not isinstance(path, (str, Path)) or not path: return True
    try:
        p_str = str(path)
        p = normalize(p_str)
        if p == Path(p.anchor): return True
        return _is_system_path_raw(str(p))
    except (UnsafePathError, TypeError, OSError, RuntimeError, ValueError): return True

@lru_cache(maxsize=4096)
def is_within_directory(child: PathLike, parent: PathLike, allow_equal: bool = False) -> bool:
    """Valida que una ruta resida dentro del sandbox permitido (directorio padre)."""
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
    """Identifica si la extensión del archivo es crítica, limitando la exposición a cambios accidentales."""
    if not path: return True
    try: 
        p_str = str(path)
        return _is_sensitive_extension(os.path.splitext(p_str)[1])
    except (TypeError, ValueError, OSError): return True 

def _validate_structural_safety(target_path: Path, path_string: str) -> None:
    """
    Realiza validaciones sobre la estructura de la cadena de texto para prevenir inyecciones
    o bypasses mediante rutas mal formadas, nombres reservados o caracteres inválidos.
    """
    if not isinstance(path_string, str):
        raise UnsafePathError("Ruta no es texto.", SafetyValidationErrorCode.GENERIC)
    if _is_unc_path(path_string):
        raise UnsafePathError("Rutas UNC/Red bloqueadas.", SafetyValidationErrorCode.UNC_PATH)
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
    if _has_alternate_data_stream(path_string):
        raise UnsafePathError("Flujo de datos alternativo detectado.", SafetyValidationErrorCode.ADS_DETECTED)
    if _is_device_file(target_path):
        raise UnsafePathError("Acceso a dispositivo bloqueado.", SafetyValidationErrorCode.DEVICE_FILE_DETECTED)
    
    if "/" in path_string and os.altsep == "/":
        if path_string.replace("/", "\\") != path_string.replace("\\", "\\"):
            raise UnsafePathError("Ruta mal formada con separadores inconsistentes.", SafetyValidationErrorCode.INVALID_CHARS)
    
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
    """Verifica límites del Sandbox y tipo de medio, denegando acceso a unidades extraíbles o remotas."""
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
                if _is_volume_compressed_or_encrypted(str(target_path)):
                     raise UnsafePathError("Volumen cifrado o comprimido.", SafetyValidationErrorCode.VOLUME_RESTRICTED)
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
    """Resuelve la ruta física real de un archivo, expandiendo enlaces y puntos de unión mediante el handle."""
    if _is_path_too_long(str(path)): return None
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
    except (OSError, AttributeError, ctypes.ArgumentError):
        return None
    return None

def _validate_ntfs_reparse_redirection(path: Path) -> None:
    """Verifica que las redirecciones NTFS (Junctions) no apunten fuera de la jerarquía permitida."""
    if not path.exists(): return
    for parent in path.parents:
        if _is_directory_junction(str(parent)):
            raise UnsafePathError("Segmento de ruta contiene punto de reparse.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
            
    final_path = _get_final_path_normalized(path)
    if final_path:
        if final_path.drive != path.resolve().drive:
            raise UnsafePathError("Redirección de unidad detectada.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        if not str(final_path).startswith(str(path.parent)):
            raise UnsafePathError("Salida de carpeta permitida vía redirección.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)

def ensure_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False, base_dir: Optional[PathLike] = None) -> Path:
    """
    Función de entrada principal para validar si es seguro aplicar una modificación.
    Combina validaciones de estructura, límites, permisos y metadatos del sistema de archivos.
    """
    if path is None:
        raise UnsafePathError("Ruta nula.", SafetyValidationErrorCode.GENERIC)
    
    p = normalize(path)
    
    # Pre-check de archivos críticos de kernel antes de cualquier operación
    if _is_kernel_managed(p):
        raise UnsafePathError(f"Archivo de sistema crítico: {p.name}", SafetyValidationErrorCode.KERNEL_LOCKED_FILE)
    
    if os.name == 'nt' and (_is_volume_readonly(str(p)) or _is_volume_compressed_or_encrypted(str(p))):
        raise UnsafePathError(f"Volumen restringido/solo lectura: {p.anchor}", SafetyValidationErrorCode.VOLUME_READ_ONLY)

    if not allow_sensitive and is_sensitive_file(p):
        raise UnsafePathError(f"Extensión bloqueada '{p.suffix}'.", SafetyValidationErrorCode.SENSITIVE_EXTENSION)
        
    _validate_structural_safety(p, str(p))
    _validate_boundary_conditions(p, base_dir)
    
    if p.exists():
        _validate_access_permissions(p)
        if _is_file_in_use_by_system(str(p)):
             raise UnsafePathError(f"Archivo en uso por el sistema: {p.name}", SafetyValidationErrorCode.FILE_IN_USE)
        initial_stat = _get_path_stat_robust(p)
        if not bool(initial_stat.st_mode & stat.S_IWRITE):
            raise UnsafePathError(f"Acceso de escritura denegado: {p.name}", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
        if initial_stat.st_nlink > 1 and is_protected_path(str(p.resolve())):
            raise UnsafePathError("Modificación denegada: hard link hacia sistema.", SafetyValidationErrorCode.HARD_LINK_DETECTED)
        if os.name == 'nt': 
            _validate_ntfs_reparse_redirection(p)
            try:
                if not os.access(p.parent, os.W_OK):
                     raise UnsafePathError("Directorio contenedor marcado como solo lectura.", SafetyValidationErrorCode.VOLUME_READ_ONLY)
            except OSError:
                pass
        _check_file_integrity(p, initial_stat)
    else:
        try:
            parent = p.parent
            if parent.exists():
                if not os.access(parent, os.W_OK):
                     raise UnsafePathError("Directorio contenedor no tiene permisos de escritura.", SafetyValidationErrorCode.WRITE_ACCESS_DENIED)
                if is_protected_path(str(parent)):
                    raise UnsafePathError("Creación en directorio restringido.", SafetyValidationErrorCode.PROTECTED_SYSTEM_PATH)
                if os.name == 'nt' and _is_file_locked_by_other_process(str(parent)):
                    raise UnsafePathError("Directorio contenedor bloqueado por otro proceso.", SafetyValidationErrorCode.FILE_IN_USE)
                for p_seg in parent.parents:
                    if _is_directory_junction(str(p_seg)):
                        raise UnsafePathError("Ruta base contiene punto de reparse.", SafetyValidationErrorCode.REPARSE_POINT_DETECTED)
        except (OSError, RuntimeError):
            pass
    return p

def is_safe_to_modify(path: PathLike, *, allow_sensitive: bool = False) -> bool:
    """Wrapper booleano para validar seguridad, diseñado para uso seguro en bucles de filtrado."""
    try:
        ensure_safe_to_modify(path, allow_sensitive=allow_sensitive)
        return True
    except (UnsafePathError, ValueError, TypeError, OSError, PermissionError): return False

def filter_safe_paths(paths: Iterable[PathLike], *, allow_sensitive: bool = False) -> list[Path]:
    """Filtra una colección de rutas, devolviendo solo las que cumplen con las garantías de seguridad."""
    results = []
    for p in paths:
        if p is None: continue
        try: results.append(ensure_safe_to_modify(p, allow_sensitive=allow_sensitive))
        except (UnsafePathError, ValueError, TypeError, OSError, PermissionError): continue
    return results

def describe_protection(path: PathLike) -> str:
    """Retorna una descripción legible de la causa por la que una ruta es considerada insegura."""
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
            if _is_directory_junction(str(p)): return f"'{p}' es un punto de reparse (Junction/Symlink)."
            if os.path.ismount(p): return f"'{p}' es un punto de montaje."
            if _is_readonly(str(p)): return f"'{p}' es solo lectura."
            if _is_volume_readonly(str(p)): return f"'{p}' pertenece a un volumen de solo lectura."
            if _is_volume_compressed_or_encrypted(str(p)): return f"'{p}' pertenece a un volumen cifrado/comprimido."
            if _is_file_locked_by_other_process(str(p)): return f"'{p}' en uso."
            if _get_security_descriptor(p).attrs & (Win32Attr.COMPRESSED | Win32Attr.ENCRYPTED): return f"'{p}' archivo cifrado o comprimido."
            if _get_security_descriptor(p).has_flag(Win32Attr.SPARSE_FILE): return f"'{p}' archivo disperso (sparse)."
            if _get_security_descriptor(p).has_flag(Win32Attr.OFFLINE): return f"'{p}' archivo offline/nube."
            if _get_security_descriptor(p).is_protected_system: return f"'{p}' atributo oculto/sistema/temporal."
            if _has_alternate_data_stream(p.name): return f"'{p}' contiene ADS."
            if not (p.is_file() or p.is_dir()): return f"'{p}' tipo de objeto no soportado."
            if p.is_file() and p.stat().st_size == 0: return f"'{p}' archivo vacío (potencialmente crítico)."
            if p.is_file() and p.stat().st_size > MAX_FILE_SIZE: return f"'{p}' tamaño excesivo."
            if p.is_file() and p.stat().st_nlink > 1: return f"'{p}' detectado como hard link."
    except (OSError, FileNotFoundError, AttributeError): pass
    if is_sensitive_file(p): return f"'{p.name}' extensión sensible."
    return f"'{p}' es candidata a modificación."
