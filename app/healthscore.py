"""
healthscore.py — El motor analítico de salud del sistema.

Implementa un motor de puntuación (scoring) basado en una arquitectura funcional de 
"Pipeline". La lógica separa la recolección de datos (SystemMetrics) de la 
evaluación (Scorer) y la generación de sugerencias (RecommendationRule).

El proceso de cálculo sigue tres pasos fundamentales:
1. Normalización: Conversión de métricas crudas (ej: MB, %) a un ratio [0.0, 1.0].
2. Ponderación: Aplicación de pesos definidos en WEIGHTS al ratio normalizado.
3. Evaluación: Ejecución de reglas condicionales para emitir recomendaciones.

Seguridad: Todas las entradas son validadas contra inyecciones y términos sensibles.
Los cálculos utilizan decoradores para asegurar que solo se procesen métricas permitidas.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Any, Final, NamedTuple, Annotated, Callable, TypeAlias, Protocol, Tuple
from types import MappingProxyType
from enum import Enum
from pathlib import Path
import math
import string
import sys
import unicodedata
from functools import lru_cache, wraps

# Tipos definidos para mejorar la legibilidad de las firmas de funciones
ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Rango [0.0 (crítico), 1.0 (óptimo)]"]
MetricKey: TypeAlias = str

# --- Constantes y Límites ---
_ALLOWED_CHARS: Final[set[str]] = set(string.ascii_letters + string.digits + " .,:()-_%")
_BLOCKED_TERMS: Final[frozenset[str]] = frozenset([
    "system32", "windows", "admin", "c:\\", "system", "kernel", "path", 
    "root", "drive", "user", "profile", "appdata", "programdata", "exe", "dll",
    "boot", "users", "admin", "secret", "private"
])
_MAX_MSG_LEN: Final[int] = 128

# Define los límites físicos para validar que las métricas crudas sean coherentes
_METRIC_LIMITS: Final[MappingProxyType[str, Tuple[float, float]]] = MappingProxyType({
    "junk_mb": (0.0, 1048576.0),
    "duplicate_mb": (0.0, 1048576.0),
    "suspicious_count": (0.0, 5000.0),
    "suspicious_warnings": (0.0, 5000.0),
    "startup_count": (0.0, 1000.0),
    "memory_available_percent": (0.0, 100.0),
    "disk_free_percent": (0.0, 100.0),
    "quarantined_count": (0.0, 10000.0)
})

_VALIDATION_KEYS: Final[Tuple[str, ...]] = (
    "junk_mb", "suspicious_count", "suspicious_warnings", 
    "memory_available_percent", "disk_free_percent", 
    "duplicate_mb", "startup_count"
)
_ALL_METRICS: Final[frozenset[str]] = frozenset(_VALIDATION_KEYS + ("quarantined_count",))

# --- Utilidades de Validación ---

def _is_finite_numeric(val: Any) -> bool:
    """Valida si un valor es un número real finito, descartando booleanos."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    return math.isfinite(float(val))

def _validate_numeric(value: Any, default: float, min_val: float, max_val: float) -> float:
    """Asegura que un valor sea numérico, finito y contenido en un rango dado."""
    try:
        if value is None or not _is_finite_numeric(value): 
            return float(default)
        return float(max(min_val, min(max_val, float(value))))
    except (ValueError, TypeError):
        return float(default)

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Ajusta un valor numérico al rango [min_val, max_val]."""
    if not _is_finite_numeric(value): return float(min_val)
    return float(max(min_val, min(max_val, float(value))))

def _is_potentially_unsafe(text: str) -> bool:
    """Detecta caracteres de control o secuencias de escape potencialmente maliciosas."""
    return any(unicodedata.category(c).startswith('C') for c in text)

# --- Decoradores de Seguridad ---

def _guard_metric_access(func: Callable[[Any, str, Any], Any]) -> Callable[[Any, str, Any], Any]:
    """Decorador para restringir el acceso a métricas definidas en _ALL_METRICS."""
    @wraps(func)
    def wrapper(instance: Any, field_name: str, default: Any = 0.0) -> Any:
        if field_name not in _ALL_METRICS:
            return default
        return func(instance, field_name, default)
    return wrapper

def _is_sensitive_input(data: Any) -> bool:
    """Detecta si un mensaje contiene términos bloqueados para prevenir fugas de datos."""
    if not isinstance(data, str): return False
    d_low = data.lower()
    return any(term in d_low for term in _BLOCKED_TERMS)

def _sanitize_msg(msg: str | Any) -> str:
    """Limpia y valida mensajes contra inyecciones y caracteres no permitidos."""
    if not isinstance(msg, str) or _is_potentially_unsafe(msg): return ""
    normalized = unicodedata.normalize("NFKC", msg)
    if len(normalized) > _MAX_MSG_LEN: normalized = normalized[:_MAX_MSG_LEN]
    if _is_sensitive_input(normalized): return ""
    
    clean = "".join(char for char in normalized if char in _ALLOWED_CHARS)
    return clean.strip()

# --- Estructuras y Protocolos ---

class Scorer(Protocol):
    """
    Protocolo de interfaz para funciones que transforman métricas en un ratio [0.0, 1.0].
    """
    def __call__(self, metrics: SystemMetrics) -> NormalizedRatio: ...

class Grade(Enum):
    """Enumeración de calificaciones asociadas a rangos de salud."""
    A = "A"; B = "B"; C = "C"; D = "D"; F = "F"

    @staticmethod
    def _get_letter(score: float) -> str:
        """Determina la letra basándose en umbrales prefijados."""
        if score >= 90: return Grade.A.value
        if score >= 80: return Grade.B.value
        if score >= 65: return Grade.C.value
        if score >= 50: return Grade.D.value
        return Grade.F.value

    @classmethod
    def from_score(cls, score: float | int | None) -> str:
        """Mapea una puntuación numérica (0-100) a una calificación de letra."""
        if score is None or not _is_finite_numeric(score):
            return cls.F.value
        return cls._get_letter(float(score))

class RecommendationRule(NamedTuple):
    """
    Lógica de negocio para emitir una advertencia.
    area: Categoría de la métrica.
    threshold: Ratio límite para disparar la regla.
    message_factory: Función que genera el texto de la recomendación.
    check: Predicado que compara el ratio actual contra el umbral.
    """
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, NormalizedRatio], bool]

class PipelineEntry(NamedTuple):
    """
    Configuración completa para una etapa del motor de cálculo de salud.
    """
    area: MetricKey
    weight: float
    scorer: Callable[[SystemMetrics], NormalizedRatio]
    rules: Tuple[RecommendationRule, ...]
    formatted_label: str = ""

# --- Constantes de Configuración ---

_LIMIT_JUNK_MB: Final[float] = 5000.0        
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0   
_LIMIT_STARTUP_COUNT: Final[int] = 20        
_LIMIT_RAM_PERCENT: Final[float] = 35.0      
_LIMIT_DISK_PERCENT: Final[float] = 25.0     

WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

WEIGHTS: Final[MappingProxyType[str, int]] = MappingProxyType({
    "seguridad": 30, "disco": 20, "memoria": 18, "basura": 14, "duplicados": 10, "arranque": 8,
})

# --- Factorías de Scorers ---

def create_linear_scorer(limit: float, inverse: bool = True) -> Callable[[float], NormalizedRatio]:
    """
    Fabrica un scorer lineal.
    inverse=True: menor valor de entrada (ej: basura) es mejor (ratio alto).
    inverse=False: mayor valor de entrada (ej: espacio libre) es mejor (ratio alto).
    """
    @lru_cache(maxsize=32)
    def scorer(val: float) -> NormalizedRatio:
        if not _is_finite_numeric(limit) or limit <= 0.0 or not _is_finite_numeric(val): 
            return 0.0
        try:
            ratio = float(val) / float(limit)
            return _clamp(1.0 - ratio if inverse else ratio)
        except (ZeroDivisionError, ValueError):
            return 0.0
    return scorer

_JUNK_SCORER: Final = create_linear_scorer(_LIMIT_JUNK_MB, inverse=True)
_DUP_SCORER: Final = create_linear_scorer(_LIMIT_DUPLICATE_MB, inverse=True)
_STARTUP_SCORER: Final = create_linear_scorer(float(_LIMIT_STARTUP_COUNT), inverse=True)

# --- Funciones de Recomendación (Reglas de Negocio) ---

def _msg_security(m: SystemMetrics) -> str: return _sanitize_msg(f"Riesgos de seguridad detectados: {m.safe_get('suspicious_count')}.")
def _msg_disk(m: SystemMetrics) -> str: return _sanitize_msg(f"Espacio libre en disco bajo: {m.safe_get('disk_free_percent'):.1f} porciento.")
def _msg_memory(m: SystemMetrics) -> str: return _sanitize_msg("Memoria disponible baja, optimice sus procesos.")
def _msg_junk(m: SystemMetrics) -> str: return _sanitize_msg(f"Acumulacion de archivos temporales: {m.safe_get('junk_mb'):.0f} MB.")
def _msg_duplicates(m: SystemMetrics) -> str: return _sanitize_msg(f"Recuperacion posible: {m.safe_get('duplicate_mb'):.0f} MB en duplicados.")
def _msg_startup(m: SystemMetrics) -> str: return _sanitize_msg(f"Exceso de programas en arranque: {m.safe_get('startup_count')} entradas.")

def _check_low(m: SystemMetrics, r: NormalizedRatio) -> bool: return r < WARN_THRESHOLD_LOW
def _check_med(m: SystemMetrics, r: NormalizedRatio) -> bool: return r < WARN_THRESHOLD_MED
def _check_high(m: SystemMetrics, r: NormalizedRatio) -> bool: return r < WARN_THRESHOLD_HIGH

# --- Definición de Pipeline ---

PIPELINE_DEFINITIONS: Final[Tuple[PipelineEntry, ...]] = (
    PipelineEntry("seguridad", 30.0, lambda m: score_security(m.safe_get('suspicious_count'), m.safe_get('suspicious_warnings')), (RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, _msg_security, _check_high),), "Seguridad  "),
    PipelineEntry("disco", 20.0, lambda m: score_disk(m.safe_get('disk_free_percent')), (RecommendationRule("disco", WARN_THRESHOLD_LOW, _msg_disk, _check_low),), "Disco      "),
    PipelineEntry("memoria", 18.0, lambda m: score_memory(m.safe_get('memory_available_percent')), (RecommendationRule("memoria", WARN_THRESHOLD_LOW, _msg_memory, _check_low),), "Memoria    "),
    PipelineEntry("basura", 14.0, lambda m: score_junk(m.safe_get('junk_mb')), (RecommendationRule("basura", WARN_THRESHOLD_MED, _msg_junk, _check_med),), "Basura     "),
    PipelineEntry("duplicados", 10.0, lambda m: score_duplicates(m.safe_get('duplicate_mb')), (RecommendationRule("duplicados", WARN_THRESHOLD_MED, _msg_duplicates, _check_med),), "Duplicados "),
    PipelineEntry("arranque", 8.0, lambda m: score_startup(m.safe_get('startup_count')), (RecommendationRule("arranque", WARN_THRESHOLD_LOW, _msg_startup, _check_low),), "Arranque   "),
)

_PIPELINE_WEIGHTS_TOTAL: Final[float] = float(sum(p.weight for p in PIPELINE_DEFINITIONS))

# --- Funciones de Cálculo ---

@lru_cache(maxsize=16)
def score_junk(junk_mb: float | int) -> NormalizedRatio: 
    """Calcula el ratio de salud (0.0-1.0) para el volumen de archivos basura encontrados."""
    return _JUNK_SCORER(float(junk_mb))

@lru_cache(maxsize=16)
def score_security(suspicious_count: int | float, warnings: int | float = 0) -> NormalizedRatio: 
    """Puntúa el nivel de seguridad penalizando amenazas y advertencias de Defender."""
    count = _validate_numeric(suspicious_count, 0.0, 0.0, 5000.0)
    warn = _validate_numeric(warnings, 0.0, 0.0, 5000.0)
    penalty = (count * 0.05) + (warn * 0.25)
    return _clamp(1.0 - penalty)

@lru_cache(maxsize=16)
def score_memory(available_percent: float | int) -> NormalizedRatio: 
    """Normaliza el porcentaje de RAM disponible respecto al umbral crítico definido."""
    if _LIMIT_RAM_PERCENT <= 0: return 0.0
    return _clamp(float(available_percent) / _LIMIT_RAM_PERCENT)

@lru_cache(maxsize=16)
def score_disk(free_percent: float | int) -> NormalizedRatio: 
    """Normaliza el espacio libre en disco respecto al umbral de seguridad definido."""
    if _LIMIT_DISK_PERCENT <= 0: return 0.0
    return _clamp(float(free_percent) / _LIMIT_DISK_PERCENT)

@lru_cache(maxsize=16)
def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio: 
    """Calcula el ratio de salud considerando la redundancia de datos por duplicación."""
    return _DUP_SCORER(float(duplicate_mb))

@lru_cache(maxsize=16)
def score_startup(startup_count: int | float) -> NormalizedRatio: 
    """Puntúa el impacto en el rendimiento al inicio basado en la cantidad de entradas."""
    return _STARTUP_SCORER(float(startup_count))

# --- Modelos de Datos ---

@dataclass
class SystemMetrics:
    """
    Contenedor centralizado para métricas del sistema.
    
    Asegura que todos los campos numéricos se mantengan dentro de los límites
    definidos en _METRIC_LIMITS mediante validación en __post_init__ y 
    durante la asignación de atributos.
    """
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0
    _is_valid: bool = field(init=False, default=True)

    def __post_init__(self) -> None: 
        """Valida y ajusta todos los campos numéricos al inicializar el objeto."""
        for f in _ALL_METRICS:
            val = self.__dict__.get(f, 0.0)
            self._set_validated_field(f, val)
        
        valid = all(_is_finite_numeric(self.__dict__.get(f)) for f in _ALL_METRICS)
        object.__setattr__(self, "_is_valid", valid)

    def _set_validated_field(self, name: str, value: Any) -> None:
        """Ajusta un campo interno basándose en límites predefinidos."""
        limits = _METRIC_LIMITS.get(name, (0.0, 1e9))
        if value is None or isinstance(value, bool):
            clean_val = 0.0
        else:
            clean_val = _validate_numeric(value, 0.0, *limits)
        object.__setattr__(self, name, clean_val)

    def __setattr__(self, name: str, value: Any) -> None:
        """
        Intercepta asignaciones para garantizar integridad de datos.
        
        Solo permite modificar campos definidos en _ALL_METRICS y campos
        internos (prefijados con '_'). Lanza AttributeError ante cualquier otro intento.
        """
        if name in _ALL_METRICS:
            self._set_validated_field(name, value)
        elif name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            raise AttributeError(f"Campo no permitido en SystemMetrics: {name}")

    @_guard_metric_access
    def safe_get(self, field_name: str, default: Any = 0.0) -> Any: 
        """Retorna de forma segura el valor de una métrica dada."""
        return self.__dict__.get(field_name, default)

    def get_clamped_metric(self, field_name: str) -> float:
        """Retorna el valor de una métrica tras asegurar límites de rango."""
        limits = _METRIC_LIMITS.get(field_name, (0.0, 0.0))
        val = self.__dict__.get(field_name, 0.0)
        return _validate_numeric(val, 0.0, *limits)

    def validate(self) -> None:
        """Re-ejecuta la validación de integridad sobre los campos del objeto."""
        for f in _ALL_METRICS:
            self._set_validated_field(f, self.__dict__.get(f, 0.0))
        
        valid = all(_is_finite_numeric(self.__dict__.get(f)) for f in _ALL_METRICS)
        object.__setattr__(self, "_is_valid", valid)

    @property
    def is_finite(self) -> bool: 
        """Retorna True si todas las métricas almacenadas son numéricas y finitas."""
        return self._is_valid

@dataclass
class HealthResult:
    """Estructura que encapsula el resultado final del análisis de salud."""
    score: int = 0
    grade: str = "F"
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=lambda: ["Error: Datos no inicializados."])

    @property
    def is_healthy(self) -> bool: return 80 <= self.score <= 100

def grade_for_score(score: float | int) -> str: return Grade.from_score(score)

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Ejecuta el pipeline funcional de evaluación de salud del sistema."""
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(score=0, grade="F", breakdown={}, recommendations=["Error: Formato de datos no válido."])
    
    metrics.validate()
    if not metrics.is_finite:
        return HealthResult(score=0, grade="F", breakdown={}, recommendations=["Error: Datos de entrada corruptos."])
    
    if _PIPELINE_WEIGHTS_TOTAL <= 0:
        return HealthResult(0, "F", {}, ["Error: Pipeline sin definir."])

    metric_breakdown: Dict[MetricKey, int] = {}
    total: float = 0.0
    recs: List[str] = []
    
    for entry in PIPELINE_DEFINITIONS:
        try:
            ratio = entry.scorer(metrics)
            if not _is_finite_numeric(ratio): continue
            
            p = ratio * entry.weight
            metric_breakdown[entry.area] = int(p)
            total += p
            
            for rule in entry.rules:
                if rule.check(metrics, ratio):
                    msg = rule.message_factory(metrics)
                    if msg: recs.append(msg)
        except Exception:
            continue
    
    if metrics.safe_get("quarantined_count") > 0:
        msg = _sanitize_msg("Existen elementos en cuarentena.")
        if msg: recs.append(msg)
    
    final_score = int(_clamp((total * 100.0) / _PIPELINE_WEIGHTS_TOTAL, 0.0, 100.0))
        
    return HealthResult(final_score, grade_for_score(final_score), metric_breakdown, recs or ["El sistema está en buen estado."])

def _render_bar(points: int, max_val: int) -> str:
    """Genera una representación visual en barra de texto para el reporte."""
    try:
        if not isinstance(points, int) or not isinstance(max_val, int) or max_val <= 0: return ""
        p = max(0, min(points, max_val))
        return "#" * p + "." * (max_val - p)
    except (ValueError, TypeError): return ""

def summarize(result: HealthResult | None) -> List[str]:
    """Genera un reporte legible en formato de lista de strings para la interfaz."""
    if not isinstance(result, HealthResult): return ["Error: Informe no disponible."]
    
    lines: List[str] = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    bd = result.breakdown
    for e in PIPELINE_DEFINITIONS:
        val = bd.get(e.area, 0)
        lines.append(f"  {e.formatted_label} {val:>2}/{int(e.weight):<2} [{_render_bar(val, int(e.weight))}]")
    
    lines.extend(("", "Recomendaciones:"))
    if not result.recommendations:
        lines.append("  - El sistema está en buen estado.")
    else:
        lines.extend([f"  - {s}" for s in result.recommendations if isinstance(s, str) and s])
        
    return lines
