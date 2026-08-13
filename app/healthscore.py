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
from typing import Dict, List, Any, Final, Tuple, TypeAlias, NamedTuple
import math

# Tipos para mejorar la claridad en el flujo de datos
ScoreMap: TypeAlias = Dict[str, float]

class RecommendationRule(NamedTuple):
    area: str
    threshold: float
    message_format: str

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

# --- UMBRALES DE NORMALIZACIÓN (referencias constantes para cálculo) ---
_LIMIT_JUNK_MB: Final[float] = 5000.0          
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0     
_LIMIT_STARTUP_COUNT: Final[int] = 20          
_LIMIT_RAM_PERCENT: Final[float] = 35.0        
_LIMIT_DISK_PERCENT: Final[float] = 25.0       

# --- UMBRALES DE ADVERTENCIA (ratios de 0.0 a 1.0) ---
WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

# --- PESOS DE CALIFICACIÓN (base para cálculo de puntaje) ---
WEIGHTS: Final[Dict[str, int]] = {
    "seguridad": 30,
    "disco": 20,
    "memoria": 18,
    "basura": 14,
    "duplicados": 10,
    "arranque": 8,
}

_TOTAL_WEIGHTS: Final[float] = float(sum(WEIGHTS.values()))
_WEIGHT_FACTORS: Final[Dict[str, float]] = {
    k: (w * 100.0 / _TOTAL_WEIGHTS) if _TOTAL_WEIGHTS > 0 else 0.0 
    for k, w in WEIGHTS.items()
}
_WEIGHT_ITEMS: Final[List[Tuple[str, float]]] = [(k, _WEIGHT_FACTORS[k]) for k in WEIGHTS]

# Pre-computo de metadatos para optimizar recomendaciones
_RECOMMENDATION_RULES: Final[Tuple[RecommendationRule, ...]] = (
    RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, "Revisá los {} hallazgo(s) de seguridad."),
    RecommendationRule("disco", WARN_THRESHOLD_LOW, "Queda {:.1f}% de disco libre."),
    RecommendationRule("memoria", WARN_THRESHOLD_LOW, "Memoria disponible baja: cerrá procesos innecesarios."),
    RecommendationRule("basura", WARN_THRESHOLD_MED, "Hay {:.0f} MB de archivos temporales."),
    RecommendationRule("duplicados", WARN_THRESHOLD_MED, "Podrías recuperar {:.0f} MB eliminando duplicados."),
    RecommendationRule("arranque", WARN_THRESHOLD_LOW, "{} programas arrancan con Windows."),
)

def _validate_weights() -> bool:
    """Verifica la consistencia matemática de la configuración de pesos."""
    if not math.isfinite(_TOTAL_WEIGHTS) or _TOTAL_WEIGHTS <= 0: return False
    return all(isinstance(w, int) and w >= 0 for w in WEIGHTS.values())


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
        return all(math.isfinite(v) for v in (
            self.junk_mb, self.suspicious_count, self.suspicious_warnings,
            self.memory_available_percent, self.disk_free_percent, 
            self.duplicate_mb, self.startup_count, self.quarantined_count
        ))


@dataclass
class HealthResult:
    score: int
    grade: str
    breakdown: Dict[str, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        return 0 <= self.score <= 100 and self.score >= 80


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value): return low
    return max(low, min(high, value))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None: return default
        val = float(value)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError): return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None: return default
        return int(float(value))
    except (TypeError, ValueError): return default


def score_junk(junk_mb: float | int) -> float:
    val = max(0.0, _to_float(junk_mb))
    return 0.0 if _LIMIT_JUNK_MB <= 0.0 else _clamp(1.0 - (val / _LIMIT_JUNK_MB), 0.0, 1.0)


def score_security(suspicious_count: int, warnings: int = 0) -> float:
    count = max(0, _to_int(suspicious_count))
    warns = max(0, _to_int(warnings))
    penalty = (count * 0.05) + (warns * 0.25)
    return _clamp(1.0 - penalty, 0.0, 1.0)


def score_memory(available_percent: float | int) -> float:
    val = _clamp(_to_float(available_percent), 0.0, 100.0)
    if _LIMIT_RAM_PERCENT <= 0.0: return 0.0
    return _clamp(val / _LIMIT_RAM_PERCENT, 0.0, 1.0)


def score_disk(free_percent: float | int) -> float:
    val = _clamp(_to_float(free_percent), 0.0, 100.0)
    if _LIMIT_DISK_PERCENT <= 0.0: return 0.0
    return _clamp(val / _LIMIT_DISK_PERCENT, 0.0, 1.0)


def score_duplicates(duplicate_mb: float | int) -> float:
    val = max(0.0, _to_float(duplicate_mb))
    return 0.0 if _LIMIT_DUPLICATE_MB <= 0.0 else _clamp(1.0 - (val / _LIMIT_DUPLICATE_MB), 0.0, 1.0)


def score_startup(startup_count: int) -> float:
    val = max(0, _to_int(startup_count))
    return 0.0 if _LIMIT_STARTUP_COUNT <= 0 else _clamp(1.0 - (val / _LIMIT_STARTUP_COUNT), 0.0, 1.0)


def grade_for_score(score: float | int) -> str:
    s = _clamp(_to_float(score), 0.0, 100.0)
    if s >= 90: return "A"
    if s >= 80: return "B"
    if s >= 65: return "C"
    if s >= 50: return "D"
    return "F"


def _generate_recommendations(metrics: SystemMetrics, ratios: ScoreMap) -> List[str]:
    if not metrics.is_finite():
        return ["Error: Datos de entrada corruptos, análisis no disponible."]
        
    recommendations: List[str] = []
    vals = {
        "seguridad": metrics.suspicious_count,
        "disco": metrics.disk_free_percent,
        "memoria": metrics.memory_available_percent,
        "basura": metrics.junk_mb,
        "duplicados": metrics.duplicate_mb,
        "arranque": metrics.startup_count
    }

    for rule in _RECOMMENDATION_RULES:
        if rule.area in ratios and rule.area in vals:
            if ratios[rule.area] < rule.threshold:
                try:
                    recommendations.append(rule.message_format.format(vals[rule.area]))
                except (ValueError, KeyError):
                    continue
    
    if metrics.quarantined_count > 0:
        recommendations.append(f"Tenés {metrics.quarantined_count} archivo(s) en cuarentena.")
    
    return recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]


def compute_score(metrics: SystemMetrics) -> HealthResult:
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(0, "F", {}, ["Error: Instancia de métricas inválida."])
    
    metrics.validate()
    if not metrics.is_finite() or not _validate_weights():
        return HealthResult(0, "F", {}, ["Error: Datos o configuración inestables."])

    ratios: ScoreMap = {
        "seguridad": score_security(metrics.suspicious_count, metrics.suspicious_warnings),
        "disco": score_disk(metrics.disk_free_percent),
        "memoria": score_memory(metrics.memory_available_percent),
        "basura": score_junk(metrics.junk_mb),
        "duplicados": score_duplicates(metrics.duplicate_mb),
        "arranque": score_startup(metrics.startup_count)
    }
    
    breakdown = {area: int(round(ratios[area] * factor)) for area, factor in _WEIGHT_ITEMS}
    final_score = int(round(_clamp(sum(breakdown.values()), 0.0, 100.0)))
    
    return HealthResult(
        score=final_score,
        grade=grade_for_score(final_score),
        breakdown=breakdown,
        recommendations=_generate_recommendations(metrics, ratios),
    )


def summarize(result: HealthResult) -> List[str]:
    if not isinstance(result, HealthResult): return ["Error: Formato inválido."]

    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, factor in _WEIGHT_ITEMS:
        puntos = result.breakdown.get(area, 0)
        maximo = int(round(factor))
        visual = f"[{'#' * puntos}{'.' * (max(0, maximo - puntos))}]"
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} {visual}")
    
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {r}" for r in result.recommendations] if result.recommendations else ["  - Ninguna."])
    return lines
