"""
healthscore.py — el panel que combina todos los módulos en un solo número.

Toma las métricas de limpieza, seguridad, memoria, disco, duplicados y
arranque, y las convierte en un puntaje de 0 a 100 con una nota de A a F y
recomendaciones concretas.

DECISIÓN DE DISEÑO: `compute_score` es una función pura — recibe un objeto
con las métricas y no toca el disco ni el sistema. Eso permite testear
todos los casos límite (sistema impecable, sistema desastre, datos
faltantes) sin necesitar una PC sucia de verdad. La recolección de datos
vive en los otros módulos; acá solo se puntúa.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Any, Final, Tuple, TypeAlias, NamedTuple, Annotated
import math

# Tipos para mejorar la claridad en el flujo de datos
ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Un valor entre 0.0 y 1.0 representando salud"]
MetricKey: TypeAlias = str

class RecommendationRule(NamedTuple):
    """
    Define una condición de advertencia basada en umbrales de métricas.
    """
    area: MetricKey
    threshold: float
    message_format: str
    expected_args: int = 1
    metric_attr: str = ""

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

# --- UMBRALES DE NORMALIZACIÓN ---
_LIMIT_JUNK_MB: Final[float] = 5000.0          
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0     
_LIMIT_STARTUP_COUNT: Final[int] = 20          
_LIMIT_RAM_PERCENT: Final[float] = 35.0        
_LIMIT_DISK_PERCENT: Final[float] = 25.0       

# --- UMBRALES DE ADVERTENCIA ---
WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

# --- PESOS DE CALIFICACIÓN ---
WEIGHTS: Final[Dict[MetricKey, int]] = {
    "seguridad": 30,
    "disco": 20,
    "memoria": 18,
    "basura": 14,
    "duplicados": 10,
    "arranque": 8,
}

# Validación defensiva de constantes para asegurar consistencia del sistema de puntaje
def _safe_sum_weights() -> float:
    return float(sum(max(0, w) for w in WEIGHTS.values()))

_TOTAL_WEIGHTS: Final[float] = _safe_sum_weights()
_WEIGHT_FACTORS: Final[Dict[MetricKey, float]] = {
    k: (max(0, w) * 100.0 / _TOTAL_WEIGHTS) if _TOTAL_WEIGHTS > 0 else 0.0 
    for k, w in WEIGHTS.items()
}
_WEIGHT_ITEMS: Final[List[Tuple[MetricKey, float]]] = [(k, _WEIGHT_FACTORS[k]) for k in WEIGHTS]
_WEIGHT_ITEMS_INT: Final[List[Tuple[MetricKey, int]]] = [(k, int(round(_WEIGHT_FACTORS[k]))) for k in WEIGHTS]

_RECOMMENDATION_RULES: Final[Tuple[RecommendationRule, ...]] = (
    RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, "Revisá los {} hallazgo(s) de seguridad.", 1, "suspicious_count"),
    RecommendationRule("disco", WARN_THRESHOLD_LOW, "Queda {:.1f}% de disco libre.", 1, "disk_free_percent"),
    RecommendationRule("memoria", WARN_THRESHOLD_LOW, "Memoria disponible baja: cerrá procesos innecesarios.", 0, "memory_available_percent"),
    RecommendationRule("basura", WARN_THRESHOLD_MED, "Hay {:.0f} MB de archivos temporales.", 1, "junk_mb"),
    RecommendationRule("duplicados", WARN_THRESHOLD_MED, "Podrías recuperar {:.0f} MB eliminando duplicados.", 1, "duplicate_mb"),
    RecommendationRule("arranque", WARN_THRESHOLD_LOW, "{} programas arrancan con Windows.", 1, "startup_count"),
)

def _validate_integrity() -> bool:
    return math.isfinite(_TOTAL_WEIGHTS) and _TOTAL_WEIGHTS > 0 and all(isinstance(w, int) and w >= 0 for w in WEIGHTS.values())

@dataclass
class SystemMetrics:
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    def validate(self) -> None:
        self.junk_mb = max(0.0, _to_float(self.junk_mb))
        self.suspicious_count = max(0, _to_int(self.suspicious_count))
        self.suspicious_warnings = max(0, _to_int(self.suspicious_warnings))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent), 0.0, 100.0)
        self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
        self.startup_count = max(0, _to_int(self.startup_count))
        self.quarantined_count = max(0, _to_int(self.quarantined_count))

    def is_finite(self) -> bool:
        return all(math.isfinite(float(getattr(self, a))) for a in self.__dataclass_fields__)

@dataclass
class HealthResult:
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        return 80 <= self.score <= 100

def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value)) if math.isfinite(value) else low

def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        val = float(value)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError): return default

def _to_int(value: Any, default: int = 0) -> int:
    try:
        val = float(value)
        return int(val) if math.isfinite(val) else default
    except (TypeError, ValueError): return default

def score_junk(junk_mb: float | int) -> NormalizedRatio:
    return 0.0 if _LIMIT_JUNK_MB <= 0.0 else _clamp(1.0 - (max(0.0, _to_float(junk_mb)) / _LIMIT_JUNK_MB), 0.0, 1.0)

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio:
    return _clamp(1.0 - ((max(0, _to_int(suspicious_count)) * 0.05) + (max(0, _to_int(warnings)) * 0.25)), 0.0, 1.0)

def score_memory(available_percent: float | int) -> NormalizedRatio:
    return (_clamp(_to_float(available_percent) / _LIMIT_RAM_PERCENT, 0.0, 1.0) if _LIMIT_RAM_PERCENT > 0 else 0.0)

def score_disk(free_percent: float | int) -> NormalizedRatio:
    return (_clamp(_to_float(free_percent) / _LIMIT_DISK_PERCENT, 0.0, 1.0) if _LIMIT_DISK_PERCENT > 0 else 0.0)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio:
    return 0.0 if _LIMIT_DUPLICATE_MB <= 0.0 else _clamp(1.0 - (max(0.0, _to_float(duplicate_mb)) / _LIMIT_DUPLICATE_MB), 0.0, 1.0)

def score_startup(startup_count: int) -> NormalizedRatio:
    return 0.0 if _LIMIT_STARTUP_COUNT <= 0 else _clamp(1.0 - (max(0, _to_int(startup_count)) / _LIMIT_STARTUP_COUNT), 0.0, 1.0)

def grade_for_score(score: float | int) -> str:
    s = _clamp(_to_float(score), 0.0, 100.0)
    if s >= 90: return "A"
    if s >= 80: return "B"
    if s >= 65: return "C"
    if s >= 50: return "D"
    return "F"

def _calculate_breakdown(ratios: ScoreMap) -> Dict[MetricKey, int]:
    return {area: int(round(_clamp(ratios.get(area, 0.0) or 0.0, 0.0, 1.0) * factor)) for area, factor in _WEIGHT_ITEMS}

def _generate_recommendations(metrics: SystemMetrics, ratios: ScoreMap) -> List[str]:
    recommendations: List[str] = []
    for rule in _RECOMMENDATION_RULES:
        if _clamp(ratios.get(rule.area, 1.0), 0.0, 1.0) < rule.threshold:
            val = getattr(metrics, rule.metric_attr, None) if rule.metric_attr else None
            if rule.expected_args > 0 and val is not None:
                try: recommendations.append(rule.message_format.format(val))
                except (ValueError, IndexError, TypeError, KeyError): continue
            elif rule.expected_args == 0:
                recommendations.append(rule.message_format)
    
    if metrics.quarantined_count > 0:
        recommendations.append(f"Tenés {metrics.quarantined_count} archivo(s) en cuarentena.")
    
    return recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]

def compute_score(metrics: SystemMetrics) -> HealthResult:
    if not isinstance(metrics, SystemMetrics) or not _validate_integrity():
        return HealthResult(0, "F", {}, ["Error: Sistema de evaluación inestable."])
    
    metrics.validate()
    if not metrics.is_finite():
        return HealthResult(0, "F", {}, ["Error: Datos de entrada corruptos."])

    ratios: ScoreMap = {
        "seguridad": score_security(metrics.suspicious_count, metrics.suspicious_warnings),
        "disco": score_disk(metrics.disk_free_percent),
        "memoria": score_memory(metrics.memory_available_percent),
        "basura": score_junk(metrics.junk_mb),
        "duplicados": score_duplicates(metrics.duplicate_mb),
        "arranque": score_startup(metrics.startup_count)
    }
    
    breakdown = _calculate_breakdown(ratios)
    final_score = int(round(_clamp(float(sum(breakdown.values())), 0.0, 100.0)))
    
    return HealthResult(final_score, grade_for_score(final_score), breakdown, _generate_recommendations(metrics, ratios))

def summarize(result: HealthResult) -> List[str]:
    if not isinstance(result, HealthResult): return ["Error: Formato inválido."]
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, maximo in _WEIGHT_ITEMS_INT:
        puntos = result.breakdown.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{'#' * puntos}{'.' * (max(0, maximo - puntos))}]")
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {r}" for r in result.recommendations] if result.recommendations else ["  - Ninguna."])
    return lines
