"""
healthscore.py — El motor analítico de salud del sistema.

Este módulo implementa un motor de puntuación (scoring) de arquitectura funcional.
Toma un objeto 'SystemMetrics' y, mediante un pipeline de normalización y 
ponderación, lo transforma en un 'HealthResult' comprensible para el usuario.

DISEÑO:
- `compute_score` es una función pura: no tiene efectos secundarios, facilitando
  la verificabilidad y los tests unitarios.
- El pipeline utiliza una estrategia de 'Clamping' para asegurar que cualquier
  entrada de métrica, sin importar su origen, resulte en un valor entre 0 y 1.
- La extensión del puntaje a nuevos módulos se realiza agregando una entrada 
  en el diccionario `_SCORERS` y ajustando la constante `WEIGHTS`.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Any, Final, Tuple, TypeAlias, NamedTuple, Annotated, Callable
import math

# Tipos semánticos para mejorar la legibilidad de la firma de funciones.
ScoreMap: TypeAlias = Dict[str, float]
NormalizedRatio: TypeAlias = Annotated[float, "Un valor entre 0.0 y 1.0 representando salud"]
MetricKey: TypeAlias = str

class RecommendationRule(NamedTuple):
    """
    Define una regla de inferencia para generar recomendaciones.
    
    Attributes:
        area: Identificador del módulo afectado.
        threshold: Valor límite de ratio bajo el cual la regla se activa.
        message_factory: Callable que recibe métricas para generar un string.
        check: Función booleana para evaluar si se cumplen las condiciones.
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

# Umbrales críticos que definen el punto de saturación o riesgo por módulo.
_LIMIT_JUNK_MB: Final[float] = 5000.0          
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0     
_LIMIT_STARTUP_COUNT: Final[int] = 20          
_LIMIT_RAM_PERCENT: Final[float] = 35.0        
_LIMIT_DISK_PERCENT: Final[float] = 25.0       

# Factores de normalización: escalan valores brutos a un ratio [0.0, 1.0].
_INV_JUNK: Final[float] = 1.0 / _LIMIT_JUNK_MB if _LIMIT_JUNK_MB > 0 else 0.0
_INV_DUP: Final[float] = 1.0 / _LIMIT_DUPLICATE_MB if _LIMIT_DUPLICATE_MB > 0 else 0.0
_INV_STARTUP: Final[float] = 1.0 / float(_LIMIT_STARTUP_COUNT) if _LIMIT_STARTUP_COUNT > 0 else 0.0

# Niveles de severidad para activar reglas de recomendación (heurística).
WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

# Pesos de importancia relativa para el cálculo del score final (suma total = 100).
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

def score_junk(junk_mb: float | int) -> NormalizedRatio:
    """Transforma el volumen de basura en un ratio de salud (0.0 a 1.0)."""
    return _clamp(1.0 - (_to_float(junk_mb) * _INV_JUNK))

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio:
    """Calcula el ratio de seguridad penalizando eventos detectados."""
    return _clamp(1.0 - ((_to_float(suspicious_count) * 0.05) + (_to_float(warnings) * 0.25)))

def score_memory(available_percent: float | int) -> NormalizedRatio:
    """Evalúa la salud de la memoria RAM según el margen de disponibilidad."""
    val = _to_float(available_percent)
    return _clamp(val / _LIMIT_RAM_PERCENT) if _LIMIT_RAM_PERCENT > 0 else 1.0

def score_disk(free_percent: float | int) -> NormalizedRatio:
    """Evalúa la salud del disco según el porcentaje de espacio libre disponible."""
    val = _to_float(free_percent)
    return _clamp(val / _LIMIT_DISK_PERCENT) if _LIMIT_DISK_PERCENT > 0 else 1.0

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio:
    """Calcula el ratio de duplicados basado en el espacio desperdiciado."""
    return _clamp(1.0 - (_to_float(duplicate_mb) * _INV_DUP))

def score_startup(startup_count: int) -> NormalizedRatio:
    """Evalúa la carga de inicio según la cantidad de programas registrados."""
    return _clamp(1.0 - (_to_float(startup_count) * _INV_STARTUP))

_SCORERS: Final[Dict[MetricKey, Callable[[SystemMetrics], NormalizedRatio]]] = {
    "seguridad": lambda m: score_security(m.suspicious_count, m.suspicious_warnings),
    "disco": lambda m: score_disk(m.disk_free_percent),
    "memoria": lambda m: score_memory(m.memory_available_percent),
    "basura": lambda m: score_junk(m.junk_mb),
    "duplicados": lambda m: score_duplicates(m.duplicate_mb),
    "arranque": lambda m: score_startup(m.startup_count)
}

_RECOMMENDATION_RULES: Final[Tuple[RecommendationRule, ...]] = (
    RecommendationRule("seguridad", WARN_THRESHOLD_HIGH, lambda m: f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad.", lambda m, r: r < WARN_THRESHOLD_HIGH),
    RecommendationRule("disco", WARN_THRESHOLD_LOW, lambda m: f"Queda {m.disk_free_percent:.1f}% de disco libre.", lambda m, r: r < WARN_THRESHOLD_LOW),
    RecommendationRule("memoria", WARN_THRESHOLD_LOW, lambda m: "Memoria disponible baja: cerrá procesos innecesarios.", lambda m, r: r < WARN_THRESHOLD_LOW),
    RecommendationRule("basura", WARN_THRESHOLD_MED, lambda m: f"Hay {m.junk_mb:.0f} MB de archivos temporales.", lambda m, r: r < WARN_THRESHOLD_MED),
    RecommendationRule("duplicados", WARN_THRESHOLD_MED, lambda m: f"Podrías recuperar {m.duplicate_mb:.0f} MB eliminando duplicados.", lambda m, r: r < WARN_THRESHOLD_MED),
    RecommendationRule("arranque", WARN_THRESHOLD_LOW, lambda m: f"{m.startup_count} programas arrancan con Windows.", lambda m, r: r < WARN_THRESHOLD_LOW),
)

_RULES_BY_AREA: Final[Dict[MetricKey, List[RecommendationRule]]] = {}
for rule in _RECOMMENDATION_RULES:
    _RULES_BY_AREA.setdefault(rule.area, []).append(rule)

_OPTIMIZED_PIPELINE: Final[List[Tuple[MetricKey, int, Callable[[SystemMetrics], NormalizedRatio], List[RecommendationRule]]]] = [
    (area, weight, _SCORERS[area], _RULES_BY_AREA.get(area, [])) for area, weight in _WEIGHT_ITEMS_INT
]

@dataclass
class SystemMetrics:
    """Contenedor inmutable y validado de métricas extraídas del sistema."""
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
        """Aplica normalización defensiva para asegurar integridad de datos."""
        self.junk_mb = max(0.0, _to_float(self.junk_mb))
        self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
        self.suspicious_count = int(max(0, _to_float(self.suspicious_count)))
        self.suspicious_warnings = int(max(0, _to_float(self.suspicious_warnings)))
        self.startup_count = int(max(0, _to_float(self.startup_count)))
        self.quarantined_count = int(max(0, _to_float(self.quarantined_count)))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent), 0.0, 100.0)

    def is_finite(self) -> bool:
        """Verifica que todos los atributos numéricos sean matemáticamente finitos."""
        return (math.isfinite(self.junk_mb) and math.isfinite(self.suspicious_count) and
                math.isfinite(self.suspicious_warnings) and math.isfinite(self.memory_available_percent) and
                math.isfinite(self.disk_free_percent) and math.isfinite(self.duplicate_mb) and
                math.isfinite(self.startup_count) and math.isfinite(self.quarantined_count))

@dataclass
class HealthResult:
    """Resultado del cálculo de salud con desglose y recomendaciones."""
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Determina si la salud general se encuentra en un estado óptimo."""
        return 80 <= self.score <= 100

def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Limita un valor a un rango específico [low, high]."""
    return max(low, min(high, value))

def _to_float(value: Any, default: float = 0.0) -> float:
    """Conversor seguro de tipos a float con validación de finitud."""
    try:
        val = float(value)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError): return default

def grade_for_score(score: float | int) -> str:
    """Clasifica el puntaje (0-100) según una escala de letras."""
    s = _to_float(score)
    if s >= 90: return "A"
    if s >= 80: return "B"
    if s >= 65: return "C"
    if s >= 50: return "D"
    return "F"

def _evaluate_rules(metrics: SystemMetrics, rules: List[RecommendationRule], ratio: float) -> List[str]:
    """Filtra y ejecuta recomendaciones basadas en el estado del sistema."""
    findings = []
    for rule in rules:
        if rule.check(metrics, ratio):
            try:
                msg = rule.message_factory(metrics)
                if isinstance(msg, str) and msg.strip():
                    findings.append(msg.strip())
            except Exception:
                continue
    return findings

def compute_score(metrics: SystemMetrics | None) -> HealthResult:
    """Ejecuta el pipeline de evaluación completo sobre las métricas provistas."""
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(0, "F", {}, ["Error: Tipo de entrada de métricas inválido."])
    
    metrics.validate()
    if not metrics.is_finite():
        return HealthResult(0, "F", {}, ["Error: Datos de sistema corruptos."])
    
    metric_breakdown: Dict[MetricKey, int] = {}
    total_pts: float = 0.0
    recommendations: List[str] = []
    
    for area, weight, scorer, rules in _OPTIMIZED_PIPELINE:
        try:
            ratio = scorer(metrics)
            pts = int(round(ratio * weight))
            metric_breakdown[area] = pts
            total_pts += float(pts)
            if rules:
                recommendations.extend(_evaluate_rules(metrics, rules, ratio))
        except Exception:
            metric_breakdown[area] = 0
            continue
    
    final_score = int(_clamp(total_pts, 0.0, 100.0))
    if metrics.quarantined_count > 0:
        recommendations.append(f"Tenés {metrics.quarantined_count} archivo(s) en cuarentena.")
    
    return HealthResult(
        score=final_score, 
        grade=grade_for_score(final_score), 
        breakdown=metric_breakdown, 
        recommendations=recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."]
    )

def _render_bar(pts: int, maximo: int) -> str:
    """Renderiza una barra de progreso textual simple."""
    if maximo <= 0: return ""
    puntos = max(0, min(pts, maximo))
    return ('#' * puntos) + ('.' * (maximo - puntos))

def summarize(result: HealthResult | None) -> List[str]:
    """Serializa un HealthResult en una lista de líneas legible para el usuario."""
    if not isinstance(result, HealthResult):
        return ["Error: Informe no disponible o formato inválido."]
    
    lines: List[str] = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    
    for area, maximo in _WEIGHT_ITEMS_INT:
        puntos = result.breakdown.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{_render_bar(puntos, maximo)}]")
    
    lines.extend(["", "Recomendaciones:", *[f"  - {r}" for r in result.recommendations]])
    return lines
