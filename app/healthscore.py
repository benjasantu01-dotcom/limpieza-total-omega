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
    """Define una condición de advertencia basada en umbrales de métricas."""
    area: str
    threshold: float
    message_format: str
    expected_args: int = 1

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
    RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, "Revisá los {} hallazgo(s) de seguridad.", 1),
    RecommendationRule("disco", WARN_THRESHOLD_LOW, "Queda {:.1f}% de disco libre.", 1),
    RecommendationRule("memoria", WARN_THRESHOLD_LOW, "Memoria disponible baja: cerrá procesos innecesarios.", 0),
    RecommendationRule("basura", WARN_THRESHOLD_MED, "Hay {:.0f} MB de archivos temporales.", 1),
    RecommendationRule("duplicados", WARN_THRESHOLD_MED, "Podrías recuperar {:.0f} MB eliminando duplicados.", 1),
    RecommendationRule("arranque", WARN_THRESHOLD_LOW, "{} programas arrancan con Windows.", 1),
)

def _validate_weights() -> bool:
    """Verifica la integridad de los pesos definidos para evitar divisiones por cero."""
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
        """Asegura que los valores de entrada estén dentro de rangos lógicos."""
        self.junk_mb = max(0.0, _to_float(self.junk_mb))
        self.suspicious_count = max(0, _to_int(self.suspicious_count))
        self.suspicious_warnings = max(0, _to_int(self.suspicious_warnings))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent), 0.0, 100.0)
        self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
        self.startup_count = max(0, _to_int(self.startup_count))
        self.quarantined_count = max(0, _to_int(self.quarantined_count))

    def is_finite(self) -> bool:
        """Verifica que todas las métricas contengan números finitos válidos."""
        return math.isfinite(self.junk_mb + self.suspicious_count + self.suspicious_warnings + 
                             self.memory_available_percent + self.disk_free_percent + 
                             self.duplicate_mb + self.startup_count + self.quarantined_count)


@dataclass
class HealthResult:
    score: int
    grade: str
    breakdown: Dict[str, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        return 80 <= self.score <= 100


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Restringe un valor numérico a un rango acotado [low, high]."""
    return max(low, min(high, value)) if math.isfinite(value) else low


def _to_float(value: Any, default: float = 0.0) -> float:
    """Conversión segura a float manejando excepciones de tipo."""
    try:
        val = float(value)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError): return default


def _to_int(value: Any, default: int = 0) -> int:
    """Conversión segura a int manejando excepciones de tipo."""
    try:
        return int(float(value))
    except (TypeError, ValueError): return default


def score_junk(junk_mb: float | int) -> float:
    """Normaliza el volumen de basura a un ratio (1.0 = impecable, 0.0 = lleno)."""
    return 0.0 if _LIMIT_JUNK_MB <= 0.0 else _clamp(1.0 - (max(0.0, _to_float(junk_mb)) / _LIMIT_JUNK_MB), 0.0, 1.0)


def score_security(suspicious_count: int, warnings: int = 0) -> float:
    """Calcula el ratio de seguridad penalizando amenazas (fuerte) y advertencias (leve)."""
    return _clamp(1.0 - ((max(0, _to_int(suspicious_count)) * 0.05) + (max(0, _to_int(warnings)) * 0.25)), 0.0, 1.0)


def score_memory(available_percent: float | int) -> float:
    """Normaliza la disponibilidad de RAM basándose en el límite definido."""
    return _clamp(_to_float(available_percent) / _LIMIT_RAM_PERCENT, 0.0, 1.0) if _LIMIT_RAM_PERCENT > 0 else 0.0


def score_disk(free_percent: float | int) -> float:
    """Normaliza el espacio en disco libre relativo al umbral mínimo deseado."""
    return _clamp(_to_float(free_percent) / _LIMIT_DISK_PERCENT, 0.0, 1.0) if _LIMIT_DISK_PERCENT > 0 else 0.0


def score_duplicates(duplicate_mb: float | int) -> float:
    """Calcula el ratio de salud referente a archivos duplicados."""
    return 0.0 if _LIMIT_DUPLICATE_MB <= 0.0 else _clamp(1.0 - (max(0.0, _to_float(duplicate_mb)) / _LIMIT_DUPLICATE_MB), 0.0, 1.0)


def score_startup(startup_count: int) -> float:
    """Calcula el ratio de salud de los elementos en inicio automático."""
    return 0.0 if _LIMIT_STARTUP_COUNT <= 0 else _clamp(1.0 - (max(0, _to_int(startup_count)) / _LIMIT_STARTUP_COUNT), 0.0, 1.0)


def grade_for_score(score: float | int) -> str:
    """Convierte un puntaje numérico (0-100) en una calificación alfabética."""
    s = _clamp(_to_float(score), 0.0, 100.0)
    if s >= 90: return "A"
    if s >= 80: return "B"
    if s >= 65: return "C"
    if s >= 50: return "D"
    return "F"


def _generate_recommendations(metrics: SystemMetrics, ratios: ScoreMap) -> List[str]:
    """Genera una lista de textos explicativos según las violaciones de umbrales."""
    if not isinstance(metrics, SystemMetrics) or not metrics.is_finite():
        return ["Error: Datos de entrada corruptos, análisis no disponible."]
        
    recommendations: List[str] = []
    # Diccionario de acceso rápido para mapear área a la métrica cruda relevante
    valor_metricas: Dict[str, float | int] = {
        "seguridad": metrics.suspicious_count, 
        "disco": metrics.disk_free_percent, 
        "memoria": metrics.memory_available_percent, 
        "basura": metrics.junk_mb, 
        "duplicados": metrics.duplicate_mb, 
        "arranque": metrics.startup_count
    }

    for rule in _RECOMMENDATION_RULES:
        ratio = ratios.get(rule.area)
        if ratio is not None and math.isfinite(ratio) and ratio < rule.threshold:
            val = valor_metricas.get(rule.area)
            if val is None or not math.isfinite(float(val)):
                continue
            try:
                msg = rule.message_format.format(val) if rule.expected_args > 0 else rule.message_format
                recommendations.append(msg)
            except (ValueError, IndexError, KeyError, TypeError):
                continue
    
    if metrics.quarantined_count > 0:
        recommendations.append(f"Tenés {metrics.quarantined_count} archivo(s) en cuarentena.")
    
    return recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]


def compute_score(metrics: SystemMetrics) -> HealthResult:
    """
    Función principal de cálculo: toma métricas crudas, las normaliza a ratios,
    aplica los pesos configurados y consolida un puntaje total de 0 a 100.
    """
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(0, "F", {}, ["Error: Instancia de métricas inválida."])
    
    metrics.validate()
    if not metrics.is_finite() or not _validate_weights():
        return HealthResult(0, "F", {}, ["Error: Datos o configuración inestables."])

    ratios = {
        "seguridad": score_security(metrics.suspicious_count, metrics.suspicious_warnings),
        "disco": score_disk(metrics.disk_free_percent),
        "memoria": score_memory(metrics.memory_available_percent),
        "basura": score_junk(metrics.junk_mb),
        "duplicados": score_duplicates(metrics.duplicate_mb),
        "arranque": score_startup(metrics.startup_count)
    }
    
    # Calcular desglose ponderado redondeado al entero más cercano
    breakdown = {area: int(round(_clamp(ratios[area], 0.0, 1.0) * factor)) for area, factor in _WEIGHT_ITEMS}
    final_score = int(round(_clamp(float(sum(breakdown.values())), 0.0, 100.0)))
    
    return HealthResult(final_score, grade_for_score(final_score), breakdown, _generate_recommendations(metrics, ratios))


def summarize(result: HealthResult) -> List[str]:
    """Genera un reporte textual formateado del resultado del análisis."""
    if not isinstance(result, HealthResult): return ["Error: Formato inválido."]
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, factor in _WEIGHT_ITEMS:
        puntos = result.breakdown.get(area, 0)
        maximo = int(round(factor))
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{'#' * puntos}{'.' * (max(0, maximo - puntos))}]")
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {r}" for r in result.recommendations] if result.recommendations else ["  - Ninguna."])
    return lines
