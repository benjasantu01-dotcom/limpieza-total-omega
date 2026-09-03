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
from dataclasses import dataclass, field, fields
from typing import Dict, List, Any, Final, Tuple, TypeAlias, NamedTuple, Annotated, Callable
import math

# Tipos para mejorar la claridad en el flujo de datos
ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Un valor entre 0.0 y 1.0 representando salud"]
MetricKey: TypeAlias = str

class RecommendationRule(NamedTuple):
    """Define una condición bajo la cual mostrar una sugerencia al usuario."""
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

# Umbrales críticos que definen el punto de saturación o riesgo.
_LIMIT_JUNK_MB: Final[float] = 5000.0          
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0     
_LIMIT_STARTUP_COUNT: Final[int] = 20          
_LIMIT_RAM_PERCENT: Final[float] = 35.0        
_LIMIT_DISK_PERCENT: Final[float] = 25.0       

# Factores de normalización pre-calculados (inversos para evitar divisiones en tiempo de ejecución)
_INV_JUNK: Final[float] = 1.0 / _LIMIT_JUNK_MB
_INV_DUP: Final[float] = 1.0 / _LIMIT_DUPLICATE_MB
_INV_STARTUP: Final[float] = 1.0 / float(_LIMIT_STARTUP_COUNT)
_INV_RAM: Final[float] = 1.0 / _LIMIT_RAM_PERCENT
_INV_DISK: Final[float] = 1.0 / _LIMIT_DISK_PERCENT

# Niveles de severidad definidos como rangos de normalización
WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

# Pesos de importancia relativa para el cálculo del score final
WEIGHTS: Final[Dict[MetricKey, int]] = {
    "seguridad": 30,
    "disco": 20,
    "memoria": 18,
    "basura": 14,
    "duplicados": 10,
    "arranque": 8,
}

if sum(WEIGHTS.values()) != 100:
    raise ValueError("La suma de pesos en WEIGHTS debe ser estrictamente 100.")

_WEIGHT_ITEMS_INT: Final[List[Tuple[MetricKey, int]]] = list(WEIGHTS.items())

_RECOMMENDATION_RULES: Final[Tuple[RecommendationRule, ...]] = (
    RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, lambda m: f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad.", lambda m, r: r < WARN_THRESHOLD_HIGH),
    RecommendationRule("disco", WARN_THRESHOLD_LOW, lambda m: f"Queda {m.disk_free_percent:.1f}% de disco libre.", lambda m, r: r < WARN_THRESHOLD_LOW),
    RecommendationRule("memoria", WARN_THRESHOLD_LOW, lambda m: "Memoria disponible baja: cerrá procesos innecesarios.", lambda m, r: r < WARN_THRESHOLD_LOW),
    RecommendationRule("basura", WARN_THRESHOLD_MED, lambda m: f"Hay {m.junk_mb:.0f} MB de archivos temporales.", lambda m, r: r < WARN_THRESHOLD_MED),
    RecommendationRule("duplicados", WARN_THRESHOLD_MED, lambda m: f"Podrías recuperar {m.duplicate_mb:.0f} MB eliminando duplicados.", lambda m, r: r < WARN_THRESHOLD_MED),
    RecommendationRule("arranque", WARN_THRESHOLD_LOW, lambda m: f"{m.startup_count} programas arrancan con Windows.", lambda m, r: r < WARN_THRESHOLD_LOW),
)

# Diccionario pre-indexado para acceso O(1) a reglas por área
_RULES_BY_AREA: Final[Dict[MetricKey, List[RecommendationRule]]] = {}
for rule in _RECOMMENDATION_RULES:
    _RULES_BY_AREA.setdefault(rule.area, []).append(rule)

@dataclass
class SystemMetrics:
    """
    Contenedor de datos inmutable para el estado del sistema.
    Realiza saneamiento automático en __post_init__ para evitar valores fuera de rango.
    """
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Aplica límites físicos y sanitiza tipos de los campos de métrica."""
        self.junk_mb = max(0.0, _to_float(self.junk_mb))
        self.suspicious_count = max(0, _to_int(self.suspicious_count))
        self.suspicious_warnings = max(0, _to_int(self.suspicious_warnings))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent), 0.0, 100.0)
        self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
        self.startup_count = max(0, _to_int(self.startup_count))
        self.quarantined_count = max(0, _to_int(self.quarantined_count))

    def is_finite(self) -> bool:
        """Retorna True si todos los campos de datos contienen valores numéricos finitos."""
        return all(math.isfinite(getattr(self, f.name)) for f in fields(self) if isinstance(getattr(self, f.name), (int, float)))

@dataclass
class HealthResult:
    """Resultado final del análisis de salud, incluyendo desglose y recomendaciones."""
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Considera un sistema saludable si el puntaje global es 80 o superior."""
        return 80 <= self.score <= 100

def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    """Asegura que un valor numérico esté dentro de los límites [low, high]."""
    try:
        val = float(value)
        return max(low, min(high, val)) if math.isfinite(val) else low
    except (TypeError, ValueError):
        return low

def _to_float(value: Any, default: float = 0.0) -> float:
    """Conversión segura a float, descartando valores no numéricos o infinitos."""
    try:
        val = float(value)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError): return default

def _to_int(value: Any, default: int = 0) -> int:
    """Conversión segura a int, descartando valores no numéricos o infinitos."""
    try:
        val = float(value)
        return int(val) if math.isfinite(val) else default
    except (TypeError, ValueError): return default

def score_junk(junk_mb: float | int) -> NormalizedRatio:
    """Calcula salud de basura: 1.0 es 0MB (óptimo), disminuye hacia 0.0 a 5GB."""
    return _clamp(1.0 - (_to_float(junk_mb) * _INV_JUNK), 0.0, 1.0)

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio:
    """Puntúa seguridad penalizando hallazgos y advertencias (5% y 25% c/u)."""
    return _clamp(1.0 - ((_to_float(suspicious_count) * 0.05) + (_to_float(warnings) * 0.25)), 0.0, 1.0)

def score_memory(available_percent: float | int) -> NormalizedRatio:
    """Puntúa RAM disponible: mayor disponibilidad (hasta el límite) resulta en mayor salud."""
    return _clamp(_to_float(available_percent) * _INV_RAM, 0.0, 1.0)

def score_disk(free_percent: float | int) -> NormalizedRatio:
    """Puntúa espacio en disco: mayor porcentaje libre mejora la salud."""
    return _clamp(_to_float(free_percent) * _INV_DISK, 0.0, 1.0)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio:
    """Puntúa duplicados: menor peso de archivos duplicados mejora la salud."""
    return _clamp(1.0 - (_to_float(duplicate_mb) * _INV_DUP), 0.0, 1.0)

def score_startup(startup_count: int) -> NormalizedRatio:
    """Puntúa programas en inicio: menor cantidad implica mejor rendimiento."""
    return _clamp(1.0 - (_to_float(startup_count) * _INV_STARTUP), 0.0, 1.0)

def grade_for_score(score: float | int) -> str:
    """Mapea una calificación numérica (0-100) a una escala de letras (A-F)."""
    s = _to_float(score)
    if s >= 90: return "A"
    if s >= 80: return "B"
    if s >= 65: return "C"
    if s >= 50: return "D"
    return "F"

_SCORERS: Final[Dict[MetricKey, Callable[[SystemMetrics], NormalizedRatio]]] = {
    "seguridad": lambda m: score_security(m.suspicious_count, m.suspicious_warnings),
    "disco": lambda m: score_disk(m.disk_free_percent),
    "memoria": lambda m: score_memory(m.memory_available_percent),
    "basura": lambda m: score_junk(m.junk_mb),
    "duplicados": lambda m: score_duplicates(m.duplicate_mb),
    "arranque": lambda m: score_startup(m.startup_count)
}

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Procesa métricas mediante la pipeline definida para generar un HealthResult final."""
    if metrics is None or not isinstance(metrics, SystemMetrics) or not metrics.is_finite():
        return HealthResult(0, "F", {}, ["Error: Datos de sistema inválidos o corruptos."])
    
    try:
        metrics.validate()
        metric_breakdown: Dict[MetricKey, int] = {}
        total_pts: float = 0.0
        recommendations: List[str] = []
        
        for area, weight in _WEIGHT_ITEMS_INT:
            scorer = _SCORERS.get(area)
            if not scorer: continue
            ratio = scorer(metrics)
            pts = int(round(ratio * weight))
            metric_breakdown[area] = pts
            total_pts += float(pts)
            
            for rule in _RULES_BY_AREA.get(area, []):
                if rule.check(metrics, ratio):
                    recommendations.append(rule.message_factory(metrics))
        
        final_score = int(_clamp(total_pts, 0.0, 100.0))
        if metrics.quarantined_count > 0:
            recommendations.append(f"Tenés {metrics.quarantined_count} archivo(s) en cuarentena.")
        
        return HealthResult(
            score=final_score, 
            grade=grade_for_score(final_score), 
            breakdown=metric_breakdown, 
            recommendations=recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]
        )
    except (AttributeError, ValueError, TypeError):
        return HealthResult(0, "F", {}, ["Error inesperado al calcular métricas."])

def _render_bar(pts: int, maximo: int) -> str:
    """Genera una cadena de texto representando el progreso de los puntos."""
    puntos = max(0, min(pts, maximo))
    return ('#' * puntos) + ('.' * (maximo - puntos))

def summarize(result: HealthResult | None) -> List[str]:
    """Genera una lista de strings lista para renderizarse en una interfaz gráfica."""
    if not isinstance(result, HealthResult):
        return ["Error: Informe no disponible o formato inválido."]
    
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    breakdown = result.breakdown if isinstance(result.breakdown, dict) else {}
    
    for area, maximo in _WEIGHT_ITEMS_INT:
        puntos = breakdown.get(area, 0)
        bar = _render_bar(puntos, maximo)
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{bar}]")
    
    recs = result.recommendations if isinstance(result.recommendations, list) else []
    display_recs = recs if recs else ["No hay nada urgente para hacer. El sistema está en buen estado."]
    lines.extend(["", "Recomendaciones:", *[f"  - {r}" for r in display_recs]])
    return lines
