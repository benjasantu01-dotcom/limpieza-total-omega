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
from typing import Dict, List, Any, Final, Tuple, TypeAlias, NamedTuple, Annotated, Callable
import math

# Tipos para mejorar la claridad en el flujo de datos
ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Un valor entre 0.0 y 1.0 representando salud"]
MetricKey: TypeAlias = str

class RecommendationRule(NamedTuple):
    """
    Regla de negocio que evalúa si una métrica requiere una recomendación al usuario.
    
    Attributes:
        area: La clave de la métrica (de `WEIGHTS`) a evaluar.
        threshold: Ratio crítico por debajo del cual se dispara la advertencia.
        message_factory: Función que genera un mensaje humano según el contexto de SystemMetrics.
        check: Predicado que compara el ratio actual contra el umbral.
    """
    area: MetricKey
    threshold: float
    message_factory: Callable[[SystemMetrics], str]
    check: Callable[[SystemMetrics, float], bool]

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
# Define la escala de referencia: para cada métrica, el límite representa 
# el valor óptimo de tolerancia antes de empezar a degradar el puntaje.
_LIMIT_JUNK_MB: Final[float] = 5000.0          
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0     
_LIMIT_STARTUP_COUNT: Final[int] = 20          
_LIMIT_RAM_PERCENT: Final[float] = 35.0        
_LIMIT_DISK_PERCENT: Final[float] = 25.0       

# --- UMBRALES DE ADVERTENCIA ---
# Definen cuándo una métrica se considera 'degradada' para disparar una recomendación.
WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

# --- PESOS DE CALIFICACIÓN ---
# Determinan la importancia relativa de cada categoría en la sumatoria final de 100 puntos.
WEIGHTS: Final[Dict[MetricKey, int]] = {
    "seguridad": 30,
    "disco": 20,
    "memoria": 18,
    "basura": 14,
    "duplicados": 10,
    "arranque": 8,
}

def _validate_integrity() -> bool:
    """Verifica que la suma de pesos sea exactamente 100 para garantizar la escala 0-100."""
    return math.isfinite(sum(WEIGHTS.values())) and sum(WEIGHTS.values()) == 100 and all(isinstance(w, int) and w >= 0 for w in WEIGHTS.values())

_WEIGHT_ITEMS_INT: Final[List[Tuple[MetricKey, int]]] = list(WEIGHTS.items())
_IS_INTEGRITY_VALID: Final[bool] = _validate_integrity()

_RECOMMENDATION_RULES: Final[Tuple[RecommendationRule, ...]] = (
    RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, lambda m: f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad.", lambda m, r: r < WARN_THRESHOLD_HIGH),
    RecommendationRule("disco", WARN_THRESHOLD_LOW, lambda m: f"Queda {m.disk_free_percent:.1f}% de disco libre.", lambda m, r: r < WARN_THRESHOLD_LOW),
    RecommendationRule("memoria", WARN_THRESHOLD_LOW, lambda m: "Memoria disponible baja: cerrá procesos innecesarios.", lambda m, r: r < WARN_THRESHOLD_LOW),
    RecommendationRule("basura", WARN_THRESHOLD_MED, lambda m: f"Hay {m.junk_mb:.0f} MB de archivos temporales.", lambda m, r: r < WARN_THRESHOLD_MED),
    RecommendationRule("duplicados", WARN_THRESHOLD_MED, lambda m: f"Podrías recuperar {m.duplicate_mb:.0f} MB eliminando duplicados.", lambda m, r: r < WARN_THRESHOLD_MED),
    RecommendationRule("arranque", WARN_THRESHOLD_LOW, lambda m: f"{m.startup_count} programas arrancan con Windows.", lambda m, r: r < WARN_THRESHOLD_LOW),
)

@dataclass
class SystemMetrics:
    """Contenedor de datos crudos recolectados por otros módulos para su evaluación."""
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    def validate(self) -> None:
        """Aplica saneamiento de tipos y rangos para evitar desbordes en los cálculos."""
        self.junk_mb = max(0.0, _to_float(self.junk_mb))
        self.suspicious_count = max(0, _to_int(self.suspicious_count))
        self.suspicious_warnings = max(0, _to_int(self.suspicious_warnings))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent), 0.0, 100.0)
        self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
        self.startup_count = max(0, _to_int(self.startup_count))
        self.quarantined_count = max(0, _to_int(self.quarantined_count))

    def is_finite(self) -> bool:
        """Comprueba que todas las métricas sean valores numéricos finitos."""
        return all(math.isfinite(float(getattr(self, a))) for a in self.__dataclass_fields__)

@dataclass
class HealthResult:
    """Resultado final del proceso de evaluación, listo para ser consumido por la UI."""
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Determina si el sistema tiene un puntaje aceptable (80+)."""
        return 80 <= self.score <= 100

def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Asegura que un valor esté contenido en un rango determinado."""
    return max(low, min(high, value)) if math.isfinite(value) else low

def _to_float(value: Any, default: float = 0.0) -> float:
    """Conversión segura a float evitando excepciones y valores no finitos."""
    try:
        val = float(value)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError): return default

def _to_int(value: Any, default: int = 0) -> int:
    """Conversión segura a int con redondeo previo desde float."""
    try:
        val = float(value)
        return int(val) if math.isfinite(val) else default
    except (TypeError, ValueError): return default

def score_junk(junk_mb: float | int) -> NormalizedRatio:
    """Calcula el ratio (0.0-1.0) normalizando según _LIMIT_JUNK_MB."""
    return 0.0 if _LIMIT_JUNK_MB <= 0.0 else _clamp(1.0 - (max(0.0, _to_float(junk_mb)) / _LIMIT_JUNK_MB), 0.0, 1.0)

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio:
    """Calcula el ratio de seguridad aplicando penalizaciones fijas por incidencia."""
    s_count = max(0, _to_int(suspicious_count))
    s_warn = max(0, _to_int(warnings))
    return _clamp(1.0 - ((s_count * 0.05) + (s_warn * 0.25)), 0.0, 1.0)

def score_memory(available_percent: float | int) -> NormalizedRatio:
    """Calcula el ratio de salud de memoria respecto al umbral definido."""
    return 0.0 if _LIMIT_RAM_PERCENT <= 0 else _clamp(_to_float(available_percent) / _LIMIT_RAM_PERCENT, 0.0, 1.0)

def score_disk(free_percent: float | int) -> NormalizedRatio:
    """Calcula el ratio de salud de disco respecto al umbral definido."""
    return 0.0 if _LIMIT_DISK_PERCENT <= 0 else _clamp(_to_float(free_percent) / _LIMIT_DISK_PERCENT, 0.0, 1.0)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio:
    """Calcula el ratio de salud normalizando respecto a _LIMIT_DUPLICATE_MB."""
    return 0.0 if _LIMIT_DUPLICATE_MB <= 0.0 else _clamp(1.0 - (max(0.0, _to_float(duplicate_mb)) / _LIMIT_DUPLICATE_MB), 0.0, 1.0)

def score_startup(startup_count: int) -> NormalizedRatio:
    """Calcula el ratio de salud de arranque normalizando respecto a _LIMIT_STARTUP_COUNT."""
    return 0.0 if _LIMIT_STARTUP_COUNT <= 0 else _clamp(1.0 - (max(0, _to_int(startup_count)) / _LIMIT_STARTUP_COUNT), 0.0, 1.0)

def grade_for_score(score: float | int) -> str:
    """Asigna una letra de calificación (A-F) basada en un puntaje numérico (0-100)."""
    s = _clamp(_to_float(score), 0.0, 100.0)
    if s >= 90: return "A"
    if s >= 80: return "B"
    if s >= 65: return "C"
    if s >= 50: return "D"
    return "F"

def compute_score(metrics: SystemMetrics) -> HealthResult:
    """
    Procesa las métricas del sistema para generar un informe de salud completo.
    Coordina la normalización, aplicación de pesos y generación de recomendaciones.
    """
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(0, "F", {}, ["Error: Entrada no válida."])
    
    if not _IS_INTEGRITY_VALID:
        return HealthResult(0, "F", {}, ["Error: Configuración del sistema de evaluación inestable."])
    
    try:
        metrics.validate()
        if not metrics.is_finite():
            raise ValueError("Datos no finitos detectados")
    except (ValueError, TypeError, AttributeError):
        return HealthResult(0, "F", {}, ["Error: Datos de entrada corruptos o incompletos."])

    ratios: ScoreMap = {
        "seguridad": score_security(metrics.suspicious_count, metrics.suspicious_warnings),
        "disco": score_disk(metrics.disk_free_percent),
        "memoria": score_memory(metrics.memory_available_percent),
        "basura": score_junk(metrics.junk_mb),
        "duplicados": score_duplicates(metrics.duplicate_mb),
        "arranque": score_startup(metrics.startup_count)
    }
    
    breakdown: Dict[MetricKey, int] = {}
    final_score: int = 0
    for area, weight in _WEIGHT_ITEMS_INT:
        ratio = ratios.get(area, 0.0)
        puntos = int(round(_clamp(ratio, 0.0, 1.0) * weight))
        breakdown[area] = puntos
        final_score += puntos
    
    # Asegurar que el puntaje final se mantenga en rango 0-100
    final_score = int(_clamp(float(final_score), 0.0, 100.0))
    
    # Uso de get con default para evitar errores si una regla referencia un área inexistente
    recommendations: List[str] = [
        rule.message_factory(metrics) 
        for rule in _RECOMMENDATION_RULES 
        if rule.check(metrics, ratios.get(rule.area, 0.0))
    ]
    
    if metrics.quarantined_count > 0:
        recommendations.append(f"Tenés {metrics.quarantined_count} archivo(s) en cuarentena.")
    
    if not recommendations:
        recommendations = ["No hay nada urgente para hacer. El sistema está en buen estado."]

    return HealthResult(final_score, grade_for_score(final_score), breakdown, recommendations)

def summarize(result: HealthResult) -> List[str]:
    """Genera una representación visual en texto para el reporte final."""
    if not isinstance(result, HealthResult): return ["Error: Formato inválido."]
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, maximo in _WEIGHT_ITEMS_INT:
        puntos = result.breakdown.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{'#' * puntos}{'.' * (max(0, maximo - puntos))}]")
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {r}" for r in result.recommendations] if result.recommendations else ["  - Ninguna."])
    return lines
