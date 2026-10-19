"""
healthscore.py — El motor analítico de salud del sistema.

Este módulo implementa un motor de puntuación (scoring) basado en una arquitectura 
funcional de "Pipeline". La lógica separa la recolección de datos (SystemMetrics) 
de la evaluación (Scorer) y la generación de sugerencias (RecommendationRule).

El proceso sigue tres pasos:
1. Normalización: Convierte métricas crudas a un ratio [0.0, 1.0].
2. Ponderación: Aplica pesos configurables (WEIGHTS) al ratio normalizado.
3. Evaluación: Ejecuta reglas condicionales si el ratio cae bajo umbrales críticos.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Any, Final, NamedTuple, Annotated, Callable, TypeAlias, Protocol, Tuple
from enum import Enum
from pathlib import Path
import math
import logging
import string
from functools import lru_cache

ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Valor de salud normalizado entre 0.0 (crítico) y 1.0 (óptimo)"]
MetricKey: TypeAlias = str

# Constantes de rendimiento para validación
_ALLOWED_CHARS: Final[set[str]] = set(string.ascii_letters + string.digits + " .,:()-_%")
_BLOCKED_TERMS: Final[frozenset[str]] = frozenset(["system32", "windows", "admin", "c:\\", "system", "kernel", "path", "root", "drive"])
_MAX_MSG_LEN: Final[int] = 128

class Scorer(Protocol):
    """
    Protocolo funcional para transformar SystemMetrics en un ratio de salud [0.0, 1.0].
    
    Implementaciones deben ser puras (sin I/O), manejar valores atípicos y 
    garantizar consistencia en la salida (siempre devuelve float).
    """
    def __call__(self, metrics: SystemMetrics) -> NormalizedRatio: ...

class Grade(Enum):
    """Representación semántica de los rangos de puntaje (0-100)."""
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"

    @classmethod
    def from_score(cls, score: float | int) -> str:
        """
        Convierte una puntuación numérica a su categoría alfabética equivalente.
        
        Args:
            score: Valor numérico (int o float) a clasificar.
        Returns:
            String representando la nota (A-F). F si el input es inválido.
        """
        try:
            s = float(score)
        except (TypeError, ValueError):
            return cls.F.value
        if s >= 90: return cls.A.value
        if s >= 80: return cls.B.value
        if s >= 65: return cls.C.value
        if s >= 50: return cls.D.value
        return cls.F.value

class RecommendationRule(NamedTuple):
    """
    Regla de diagnóstico ejecutable para detectar anomalías en una métrica específica.
    
    Attributes:
        area: Identificador de la métrica analizada (ej: 'disco').
        threshold: Límite inferior de salud para activar la sugerencia.
        message_factory: Callable que recibe el estado actual y retorna un mensaje humano.
        check: Predicado (SystemMetrics, NormalizedRatio) -> bool para disparar la regla.
    """
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, NormalizedRatio], bool]

class PipelineEntry(NamedTuple):
    """
    Configuración de una unidad de análisis del motor de salud.
    
    Attributes:
        area: Nombre de la categoría de análisis.
        weight: Peso porcentual (0-100) en el puntaje total.
        scorer: Función normalizadora para convertir la métrica a un ratio [0, 1].
        rules: Colección de reglas de diagnóstico asociadas a esta área.
    """
    area: MetricKey
    weight: int
    scorer: Callable[[SystemMetrics], NormalizedRatio]
    rules: Tuple[RecommendationRule, ...]

__all__ = [
    "SystemMetrics",
    "HealthResult",
    "WEIGHTS",
    "compute_score",
    "grade_for_score",
    "score_junk",
    "score_security",
    "score_memory",
    "score_disk",
    "score_duplicates",
    "score_startup",
    "summarize",
]

# Umbrales críticos para la normalización de métricas
_LIMIT_JUNK_MB: Final[float] = 5000.0        # Límite superior de basura (MB) antes de puntaje 0
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0   # Límite superior de duplicados (MB) antes de puntaje 0
_LIMIT_STARTUP_COUNT: Final[int] = 20        # Cantidad máxima tolerada de programas de inicio
_LIMIT_RAM_PERCENT: Final[float] = 35.0      # Porcentaje de RAM libre base considerado "saludable"
_LIMIT_DISK_PERCENT: Final[float] = 25.0     # Porcentaje de espacio libre base considerado "saludable"

def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Recorta un valor numérico para que se encuentre dentro de un rango inclusivo."""
    if not math.isfinite(value): return min_val
    return max(min_val, min(max_val, value))

def create_linear_scorer(limit: float, inverse: bool = True) -> Callable[[float], NormalizedRatio]:
    """
    Fábrica de funciones normalizadoras.
    
    Genera un 'scorer' que asigna 1.0 a valores ideales y 0.0 cuando se alcanza 
    o supera el límite definido.
    """
    def scorer(val: float) -> NormalizedRatio:
        if not math.isfinite(val) or limit <= 0.0: return 0.0
        ratio = val / limit
        return _clamp(1.0 - ratio if inverse else ratio)
    return scorer

# Niveles de ratio de salud para disparar advertencias (0.0 a 1.0)
WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

# Pesos relativos de cada área en el score global (debe sumar 100)
WEIGHTS: Final[Dict[MetricKey, int]] = {
    "seguridad": 30,
    "disco": 20,
    "memoria": 18,
    "basura": 14,
    "duplicados": 10,
    "arranque": 8,
}

def _verify_pipeline_integrity(weights: Dict[str, int], pipeline: Tuple[PipelineEntry, ...]) -> None:
    """Valida la integridad estructural: pesos suman 100 y áreas coinciden."""
    if sum(weights.values()) != 100:
        raise ValueError("La suma de pesos en WEIGHTS debe ser estrictamente 100.")
    pipeline_areas = {e.area for e in pipeline}
    if set(weights.keys()) != pipeline_areas:
        raise ValueError("Inconsistencia entre áreas en WEIGHTS y _PIPELINE.")

_JUNK_SCORER = create_linear_scorer(_LIMIT_JUNK_MB, inverse=True)
_DUP_SCORER = create_linear_scorer(_LIMIT_DUPLICATE_MB, inverse=True)
_STARTUP_SCORER = create_linear_scorer(float(_LIMIT_STARTUP_COUNT), inverse=True)

# Funciones de factory para reglas del pipeline: generan mensajes basados en SystemMetrics
def _msg_seguridad(m: SystemMetrics) -> str: return f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad."
def _msg_disco(m: SystemMetrics) -> str: return f"Queda {m.disk_free_percent:.1f}% de disco libre."
def _msg_memoria(m: SystemMetrics) -> str: return "Memoria disponible baja: cerrá procesos innecesarios."
def _msg_basura(m: SystemMetrics) -> str: return f"Hay {m.junk_mb:.0f} MB de archivos temporales."
def _msg_duplicados(m: SystemMetrics) -> str: return f"Podrías recuperar {m.duplicate_mb:.0f} MB eliminando duplicados."
def _msg_arranque(m: SystemMetrics) -> str: return f"{m.startup_count} programas arrancan con Windows."

# Definición de reglas específicas para facilitar la lectura del pipeline
_RULES: Final[Dict[str, Tuple[RecommendationRule, ...]]] = {
    "seguridad": (RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, _msg_seguridad, lambda m, r: r < WARN_THRESHOLD_HIGH),),
    "disco": (RecommendationRule("disco", WARN_THRESHOLD_LOW, _msg_disco, lambda m, r: r < WARN_THRESHOLD_LOW),),
    "memoria": (RecommendationRule("memoria", WARN_THRESHOLD_LOW, _msg_memoria, lambda m, r: r < WARN_THRESHOLD_LOW),),
    "basura": (RecommendationRule("basura", WARN_THRESHOLD_MED, _msg_basura, lambda m, r: r < WARN_THRESHOLD_MED),),
    "duplicados": (RecommendationRule("duplicados", WARN_THRESHOLD_MED, _msg_duplicados, lambda m, r: r < WARN_THRESHOLD_MED),),
    "arranque": (RecommendationRule("arranque", WARN_THRESHOLD_LOW, _msg_arranque, lambda m, r: r < WARN_THRESHOLD_LOW),),
}

# Pipeline central de procesamiento
_PIPELINE: Final[Tuple[PipelineEntry, ...]] = (
    PipelineEntry("seguridad", 30, lambda m: score_security(m.suspicious_count, m.suspicious_warnings), _RULES["seguridad"]),
    PipelineEntry("disco", 20, lambda m: score_disk(m.disk_free_percent), _RULES["disco"]),
    PipelineEntry("memoria", 18, lambda m: score_memory(m.memory_available_percent), _RULES["memoria"]),
    PipelineEntry("basura", 14, lambda m: score_junk(m.junk_mb), _RULES["basura"]),
    PipelineEntry("duplicados", 10, lambda m: score_duplicates(m.duplicate_mb), _RULES["duplicados"]),
    PipelineEntry("arranque", 8, lambda m: score_startup(m.startup_count), _RULES["arranque"]),
)

_verify_pipeline_integrity(WEIGHTS, _PIPELINE)

def score_junk(junk_mb: float | int) -> NormalizedRatio: 
    """Calcula el score normalizado de archivos basura."""
    return _JUNK_SCORER(float(junk_mb) if isinstance(junk_mb, (int, float)) else 0.0)

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio: 
    """Calcula salud: Penaliza según hallazgos sospechosos y advertencias."""
    s_count = max(0, int(suspicious_count))
    w_count = max(0, int(warnings))
    penalization = (s_count * 0.05) + (w_count * 0.25)
    return _clamp(1.0 - penalization)

def score_memory(available_percent: float | int) -> NormalizedRatio: 
    """Calcula el score normalizado de memoria disponible."""
    val = float(available_percent) if isinstance(available_percent, (int, float)) else 0.0
    if _LIMIT_RAM_PERCENT <= 0.0 or not math.isfinite(val): return 0.0
    return _clamp(val / _LIMIT_RAM_PERCENT)

def score_disk(free_percent: float | int) -> NormalizedRatio: 
    """Calcula el score normalizado de espacio en disco."""
    val = float(free_percent) if isinstance(free_percent, (int, float)) else 0.0
    if _LIMIT_DISK_PERCENT <= 0.0 or not math.isfinite(val): return 0.0
    return _clamp(val / _LIMIT_DISK_PERCENT)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio: 
    """Calcula el score normalizado de duplicados."""
    return _DUP_SCORER(float(duplicate_mb) if isinstance(duplicate_mb, (int, float)) else 0.0)

def score_startup(startup_count: int | float) -> NormalizedRatio: 
    """Calcula el score normalizado de programas en inicio."""
    return _STARTUP_SCORER(float(startup_count) if isinstance(startup_count, (int, float)) else 0.0)

def _validate_numeric(value: Any, default: float, min_v: float, max_v: float) -> float:
    """Sanitiza métricas numéricas evitando valores fuera de rango o NaN."""
    try:
        if value is None or isinstance(value, bool):
            return default
        val = float(value)
        if math.isnan(val) or not math.isfinite(val) or val < min_v or val > max_v:
            return default
        return val
    except (ValueError, TypeError):
        return default

@dataclass
class SystemMetrics:
    """Contenedor de datos centralizado para las métricas del sistema."""
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    _FIELDS_TO_VALIDATE: Final[Tuple[str, ...]] = (
        "junk_mb", "suspicious_count", "suspicious_warnings", 
        "memory_available_percent", "disk_free_percent", 
        "duplicate_mb", "startup_count", "quarantined_count"
    )

    def __post_init__(self) -> None:
        """Inicializa métricas asegurando tipos correctos tras la creación."""
        for field_name in self._FIELDS_TO_VALIDATE:
            val = getattr(self, field_name)
            sanitized = _validate_numeric(val, 0.0, -1.0, 1e9)
            setattr(self, field_name, sanitized)
        self.validate()

    def safe_get(self, field_name: str, default: Any = 0) -> Any:
        """Obtiene un valor de métrica mediante introspección."""
        return getattr(self, field_name, default)

    def validate(self) -> None:
        """Asegura rangos aceptables para todas las métricas."""
        self.junk_mb = _validate_numeric(self.junk_mb, 0.0, 0.0, 1048576.0)
        self.duplicate_mb = _validate_numeric(self.duplicate_mb, 0.0, 0.0, 1048576.0)
        self.suspicious_count = int(_validate_numeric(self.suspicious_count, 0, 0, 5000))
        self.suspicious_warnings = int(_validate_numeric(self.suspicious_warnings, 0, 0, 5000))
        self.startup_count = int(_validate_numeric(self.startup_count, 0, 0, 1000))
        self.quarantined_count = int(_validate_numeric(self.quarantined_count, 0, 0, 1000))
        self.memory_available_percent = _validate_numeric(self.memory_available_percent, 100.0, 0.0, 100.0)
        self.disk_free_percent = _validate_numeric(self.disk_free_percent, 100.0, 0.0, 100.0)

    @property
    def is_finite(self) -> bool:
        """Valida que todos los campos críticos sean finitos."""
        return all(math.isfinite(float(getattr(self, f))) for f in self._FIELDS_TO_VALIDATE)

@dataclass
class HealthResult:
    """Resultado consolidado del análisis de salud."""
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool: 
        return 80 <= self.score <= 100

def grade_for_score(score: float | int) -> str: 
    """Wrapper para obtener la categoría de nota según el puntaje."""
    return Grade.from_score(score)

def _sanitize_msg(msg: str | Any) -> str:
    """Limpia cadenas de texto garantizando integridad defensiva."""
    if not isinstance(msg, str): return ""
    clean = "".join(c for c in msg if c in _ALLOWED_CHARS)
    lower_msg = clean.lower()
    for term in _BLOCKED_TERMS:
        if term in lower_msg:
            return ""
    return clean.strip()[:_MAX_MSG_LEN]

def _apply_rule_safely(metrics: SystemMetrics, rule: RecommendationRule) -> str:
    """Envoltorio seguro para ejecutar factories de reglas sin exponer errores."""
    try:
        raw_res = rule.message_factory(metrics)
        return _sanitize_msg(raw_res)
    except Exception: return ""

def _evaluate_rules(metrics: SystemMetrics, rules: Tuple[RecommendationRule, ...], normalized_ratio: NormalizedRatio, findings: List[str]) -> None:
    """Ejecuta reglas de diagnóstico y registra advertencias halladas."""
    if not math.isfinite(normalized_ratio): return
    for rule in rules:
        try:
            if rule.check(metrics, normalized_ratio):
                clean_msg = _apply_rule_safely(metrics, rule)
                if clean_msg: findings.append(clean_msg)
        except (Exception, ValueError, TypeError): continue

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Computa el puntaje global mediante el pipeline con validación de suma total."""
    if not isinstance(metrics, SystemMetrics) or not metrics.is_finite:
        return HealthResult(score=0, grade="F", breakdown={}, recommendations=["Error: Datos de sistema inválidos."])
        
    recommendations: List[str] = []
    metric_breakdown: Dict[MetricKey, int] = {}
    total_points = 0.0
    
    for entry in _PIPELINE:
        try:
            area_ratio = _clamp(entry.scorer(metrics))
            if entry.rules:
                _evaluate_rules(metrics, entry.rules, area_ratio, recommendations)
            
            points = area_ratio * entry.weight
            val_int = int(round(_clamp(points, 0.0, float(entry.weight))))
            metric_breakdown[entry.area] = val_int
            total_points += val_int
        except (Exception, ValueError, TypeError):
            metric_breakdown[entry.area] = 0
            
    if metrics.quarantined_count > 0:
        recommendations.append(f"Tenés {metrics.quarantined_count} archivo(s) en cuarentena.")
        
    final_score = int(round(_clamp(total_points, 0.0, 100.0)))
    return HealthResult(
        score=final_score,
        grade=grade_for_score(final_score),
        breakdown=metric_breakdown,
        recommendations=recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]
    )

@lru_cache(maxsize=128)
def _render_bar(points: int, max_val: int) -> str:
    """Crea una representación visual de barra para reportes con caché."""
    try:
        p = max(0, min(points, max_val))
        return "#" * p + "." * (max_val - p)
    except Exception:
        return "." * max_val

def summarize(result: HealthResult | None) -> List[str]:
    """Genera una lista de líneas legible para el informe de salud."""
    if not isinstance(result, HealthResult): 
        return ["Error: Informe de salud no disponible."]
    
    lines: List[str] = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for entry in _PIPELINE:
        p = result.breakdown.get(entry.area, 0)
        lines.append(f"  {entry.area.capitalize():<12} {p:>2}/{entry.weight:<2} [{_render_bar(p, entry.weight)}]")
    
    recs = []
    for r in result.recommendations:
        sanitized = _sanitize_msg(str(r))
        if sanitized:
            recs.append(sanitized)
    
    if not recs:
        recs = ["No hay advertencias activas."]
        
    lines.extend(("", "Recomendaciones:", *(f"  - {s}" for s in recs)))
    return lines
