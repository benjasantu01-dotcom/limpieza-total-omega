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
    
    Attributes:
        area: Categoría del sistema (ej: 'seguridad').
        threshold: Ratio debajo del cual se considera necesario recomendar.
        message_format: Plantilla del mensaje al usuario.
        expected_args: Cantidad de valores numéricos que inyecta la plantilla.
        metric_attr: Nombre del atributo en SystemMetrics a evaluar.
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
# Constantes físicas utilizadas para convertir magnitudes absolutas a ratios de 0 a 1.
_LIMIT_JUNK_MB: Final[float] = 5000.0          
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0     
_LIMIT_STARTUP_COUNT: Final[int] = 20          
_LIMIT_RAM_PERCENT: Final[float] = 35.0        
_LIMIT_DISK_PERCENT: Final[float] = 25.0       

# --- UMBRALES DE ADVERTENCIA (ratios de 0.0 a 1.0) ---
# Definen el punto de corte para disparar recomendaciones en la interfaz.
WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

# --- PESOS DE CALIFICACIÓN ---
# Pesos relativos definen cuánto impacta cada área sobre el puntaje final (100 puntos máx).
WEIGHTS: Final[Dict[MetricKey, int]] = {
    "seguridad": 30,
    "disco": 20,
    "memoria": 18,
    "basura": 14,
    "duplicados": 10,
    "arranque": 8,
}

_TOTAL_WEIGHTS: Final[float] = float(sum(WEIGHTS.values()))
_WEIGHT_FACTORS: Final[Dict[MetricKey, float]] = {
    k: (w * 100.0 / _TOTAL_WEIGHTS) if _TOTAL_WEIGHTS > 0 else 0.0 
    for k, w in WEIGHTS.items()
}
_WEIGHT_ITEMS: Final[List[Tuple[MetricKey, float]]] = [(k, _WEIGHT_FACTORS[k]) for k in WEIGHTS]

# Pre-computo de metadatos para optimizar recomendaciones en la UI
_RECOMMENDATION_RULES: Final[Tuple[RecommendationRule, ...]] = (
    RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, "Revisá los {} hallazgo(s) de seguridad.", 1, "suspicious_count"),
    RecommendationRule("disco", WARN_THRESHOLD_LOW, "Queda {:.1f}% de disco libre.", 1, "disk_free_percent"),
    RecommendationRule("memoria", WARN_THRESHOLD_LOW, "Memoria disponible baja: cerrá procesos innecesarios.", 0, "memory_available_percent"),
    RecommendationRule("basura", WARN_THRESHOLD_MED, "Hay {:.0f} MB de archivos temporales.", 1, "junk_mb"),
    RecommendationRule("duplicados", WARN_THRESHOLD_MED, "Podrías recuperar {:.0f} MB eliminando duplicados.", 1, "duplicate_mb"),
    RecommendationRule("arranque", WARN_THRESHOLD_LOW, "{} programas arrancan con Windows.", 1, "startup_count"),
)

def _validate_integrity() -> bool:
    """Verifica que la configuración de pesos sea matemáticamente coherente."""
    return math.isfinite(_TOTAL_WEIGHTS) and _TOTAL_WEIGHTS > 0 and all(isinstance(w, int) and w >= 0 for w in WEIGHTS.values())


@dataclass
class SystemMetrics:
    """Contenedor de datos crudos recolectados del sistema antes de la normalización."""
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    def validate(self) -> None:
        """Asegura la integridad de los datos normalizando tipos y acotando rangos."""
        self.junk_mb = max(0.0, _to_float(self.junk_mb))
        self.suspicious_count = max(0, _to_int(self.suspicious_count))
        self.suspicious_warnings = max(0, _to_int(self.suspicious_warnings))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent), 0.0, 100.0)
        self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
        self.startup_count = max(0, _to_int(self.startup_count))
        self.quarantined_count = max(0, _to_int(self.quarantined_count))

    def is_finite(self) -> bool:
        """Verifica que todas las métricas sean valores numéricos reales."""
        attrs = (self.junk_mb, self.suspicious_count, self.suspicious_warnings, 
                 self.memory_available_percent, self.disk_free_percent, 
                 self.duplicate_mb, self.startup_count, self.quarantined_count)
        return all(math.isfinite(float(a)) for a in attrs)


@dataclass
class HealthResult:
    """Estructura de datos para el resultado final consumido por la interfaz."""
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Determina si la salud general excede el umbral del 80%."""
        return 80 <= self.score <= 100


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Fuerza un valor a mantenerse dentro del rango [low, high]."""
    return max(low, min(high, value)) if math.isfinite(value) else low


def _to_float(value: Any, default: float = 0.0) -> float:
    """Conversión segura a float con validación de finitud."""
    try:
        val = float(value)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError): return default


def _to_int(value: Any, default: int = 0) -> int:
    """Conversión segura a entero con validación de finitud."""
    try:
        val = float(value)
        return int(val) if math.isfinite(val) else default
    except (TypeError, ValueError): return default


def score_junk(junk_mb: float | int) -> NormalizedRatio:
    """Calcula ratio (0.0-1.0) comparando basura vs umbral máximo."""
    return 0.0 if _LIMIT_JUNK_MB <= 0.0 else _clamp(1.0 - (max(0.0, _to_float(junk_mb)) / _LIMIT_JUNK_MB), 0.0, 1.0)


def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio:
    """Calcula ratio penalizando hallazgos de seguridad (5% por hallazgo, 25% por advertencia)."""
    return _clamp(1.0 - ((max(0, _to_int(suspicious_count)) * 0.05) + (max(0, _to_int(warnings)) * 0.25)), 0.0, 1.0)


def score_memory(available_percent: float | int) -> NormalizedRatio:
    """Calcula ratio basado en la memoria disponible frente al umbral crítico."""
    return (_clamp(_to_float(available_percent) / _LIMIT_RAM_PERCENT, 0.0, 1.0) 
            if _LIMIT_RAM_PERCENT > 0 else 0.0)


def score_disk(free_percent: float | int) -> NormalizedRatio:
    """Calcula ratio basado en el espacio libre vs límite definido."""
    return (_clamp(_to_float(free_percent) / _LIMIT_DISK_PERCENT, 0.0, 1.0) 
            if _LIMIT_DISK_PERCENT > 0 else 0.0)


def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio:
    """Calcula ratio basado en el volumen de duplicados."""
    return 0.0 if _LIMIT_DUPLICATE_MB <= 0.0 else _clamp(1.0 - (max(0.0, _to_float(duplicate_mb)) / _LIMIT_DUPLICATE_MB), 0.0, 1.0)


def score_startup(startup_count: int) -> NormalizedRatio:
    """Calcula ratio inversamente proporcional al conteo de ítems de arranque."""
    return 0.0 if _LIMIT_STARTUP_COUNT <= 0 else _clamp(1.0 - (max(0, _to_int(startup_count)) / _LIMIT_STARTUP_COUNT), 0.0, 1.0)


def grade_for_score(score: float | int) -> str:
    """Mapea puntaje (0-100) a escala A-F."""
    s = _clamp(_to_float(score), 0.0, 100.0)
    if s >= 90: return "A"
    if s >= 80: return "B"
    if s >= 65: return "C"
    if s >= 50: return "D"
    return "F"


def _calculate_breakdown(ratios: ScoreMap) -> Dict[MetricKey, int]:
    """Distribuye los puntos ponderados según cada área."""
    return {area: int(round(_clamp(ratios.get(area, 0.0), 0.0, 1.0) * factor)) for area, factor in _WEIGHT_ITEMS}


def _generate_recommendations(metrics: SystemMetrics, ratios: ScoreMap) -> List[str]:
    """Genera lista de sugerencias basada en umbrales de salud superados."""
    recommendations: List[str] = []
    
    for rule in _RECOMMENDATION_RULES:
        if _clamp(ratios.get(rule.area, 1.0), 0.0, 1.0) < rule.threshold:
            val = getattr(metrics, rule.metric_attr, None)
            if val is not None and isinstance(val, (int, float)) and math.isfinite(float(val)):
                if rule.expected_args > 0:
                    try:
                        recommendations.append(rule.message_format.format(float(val)))
                    except (ValueError, IndexError, TypeError, KeyError):
                        continue
                else:
                    recommendations.append(rule.message_format)
    
    if isinstance(metrics.quarantined_count, int) and metrics.quarantined_count > 0:
        recommendations.append(f"Tenés {metrics.quarantined_count} archivo(s) en cuarentena.")
    
    return recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]


def compute_score(metrics: SystemMetrics) -> HealthResult:
    """Orquestador principal: normaliza métricas, calcula breakdown y genera resultados."""
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
    """Serializa el objeto HealthResult a una lista de líneas para reporte textual."""
    if not isinstance(result, HealthResult): return ["Error: Formato inválido."]
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, factor in _WEIGHT_ITEMS:
        puntos = result.breakdown.get(area, 0)
        maximo = int(round(factor))
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{'#' * puntos}{'.' * (max(0, maximo - puntos))}]")
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {r}" for r in result.recommendations] if result.recommendations else ["  - Ninguna."])
    return lines
