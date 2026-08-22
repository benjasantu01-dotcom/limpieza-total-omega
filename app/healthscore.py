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

# Umbrales críticos para la lógica de scoring
_LIMIT_JUNK_MB: Final[float] = 5000.0          
_LIMIT_DUPLICATE_MB: Final[float] = 2000.0     
_LIMIT_STARTUP_COUNT: Final[int] = 20          
_LIMIT_RAM_PERCENT: Final[float] = 35.0        
_LIMIT_DISK_PERCENT: Final[float] = 25.0       

# Umbrales de advertencia (sensibilidad del reporte)
WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

WEIGHTS: Final[Dict[MetricKey, int]] = {
    "seguridad": 30,
    "disco": 20,
    "memoria": 18,
    "basura": 14,
    "duplicados": 10,
    "arranque": 8,
}

_WEIGHT_ITEMS_INT: Final[List[Tuple[MetricKey, int]]] = list(WEIGHTS.items())

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
    """Contenedor de datos crudos recolectados del sistema para su normalización."""
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
        """Asegura la integridad de tipos y rangos de las métricas recibidas."""
        self.junk_mb = max(0.0, _to_float(self.junk_mb))
        self.suspicious_count = max(0, _to_int(self.suspicious_count))
        self.suspicious_warnings = max(0, _to_int(self.suspicious_warnings))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent), 0.0, 100.0)
        self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
        self.startup_count = max(0, _to_int(self.startup_count))
        self.quarantined_count = max(0, _to_int(self.quarantined_count))

    def is_finite(self) -> bool:
        """Verifica que todas las métricas sean valores numéricos finitos y no nulos."""
        for val in self.__dict__.values():
            if val is None: return False
            if isinstance(val, (int, float)) and not math.isfinite(float(val)):
                return False
        return True

@dataclass
class HealthResult:
    """Resultado procesado del cálculo de salud, listo para visualización."""
    score: int
    grade: str
    breakdown: Dict[MetricKey, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Determina si el estado general es considerado óptimo (80+)."""
        return 80 <= self.score <= 100

def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Fuerza un valor dentro de un rango cerrado [low, high]."""
    return max(low, min(high, value)) if math.isfinite(value) else low

def _to_float(value: Any, default: float = 0.0) -> float:
    """Convierte entrada a float, retornando default si es inválida o no numérica."""
    try:
        val = float(value)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError): return default

def _to_int(value: Any, default: int = 0) -> int:
    """Convierte entrada a int, retornando default si es inválida o no numérica."""
    try:
        val = float(value)
        return int(val) if math.isfinite(val) else default
    except (TypeError, ValueError): return default

def score_junk(junk_mb: float | int) -> NormalizedRatio:
    """Calcula salud: penaliza linealmente el exceso de basura hasta llegar a _LIMIT_JUNK_MB (salud 0)."""
    return 0.0 if _LIMIT_JUNK_MB <= 0.0 else _clamp(1.0 - (max(0.0, _to_float(junk_mb)) / _LIMIT_JUNK_MB), 0.0, 1.0)

def score_security(suspicious_count: int, warnings: int = 0) -> NormalizedRatio:
    """Calcula salud: penaliza 5% por hallazgo estándar y 25% por cada advertencia crítica."""
    return _clamp(1.0 - ((max(0, _to_int(suspicious_count)) * 0.05) + (max(0, _to_int(warnings)) * 0.25)), 0.0, 1.0)

def score_memory(available_percent: float | int) -> NormalizedRatio:
    """Calcula salud: normaliza el porcentaje libre respecto al umbral definido. >100% retorna 1.0."""
    limit = max(0.1, float(_LIMIT_RAM_PERCENT))
    return _clamp(_to_float(available_percent) / limit, 0.0, 1.0)

def score_disk(free_percent: float | int) -> NormalizedRatio:
    """Calcula salud: normaliza el porcentaje libre respecto al umbral crítico definido."""
    limit = max(0.1, float(_LIMIT_DISK_PERCENT))
    return _clamp(_to_float(free_percent) / limit, 0.0, 1.0)

def score_duplicates(duplicate_mb: float | int) -> NormalizedRatio:
    """Calcula salud: penaliza la ocupación de espacio por duplicados respecto al límite tolerable."""
    return 0.0 if _LIMIT_DUPLICATE_MB <= 0.0 else _clamp(1.0 - (max(0.0, _to_float(duplicate_mb)) / _LIMIT_DUPLICATE_MB), 0.0, 1.0)

def score_startup(startup_count: int) -> NormalizedRatio:
    """Calcula salud: penaliza el exceso de procesos de inicio. 0 procesos = 100% de salud."""
    return 0.0 if _LIMIT_STARTUP_COUNT <= 0 else _clamp(1.0 - (max(0, _to_int(startup_count)) / _LIMIT_STARTUP_COUNT), 0.0, 1.0)

def grade_for_score(score: float | int) -> str:
    """Convierte puntaje numérico (0-100) a nota cualitativa (A-F)."""
    s = _clamp(_to_float(score), 0.0, 100.0)
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

def compute_score(metrics: SystemMetrics) -> HealthResult:
    """Orquesta el cálculo de salud: normaliza áreas, aplica pesos y genera recomendaciones."""
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(0, "F", {}, ["Error: Tipo de métricas incorrecto."])
    
    # Validar que los límites globales no causen divisiones por cero y que los datos sean seguros
    safe_limits = [
        _LIMIT_JUNK_MB > 0, _LIMIT_RAM_PERCENT > 0, 
        _LIMIT_DISK_PERCENT > 0, _LIMIT_DUPLICATE_MB > 0, 
        _LIMIT_STARTUP_COUNT > 0
    ]
    if not all(safe_limits):
        return HealthResult(0, "F", {}, ["Error: Umbrales de sistema mal configurados."])

    try:
        metrics.validate()
        if not metrics.is_finite():
            return HealthResult(0, "F", {}, ["Error: Métricas de entrada no finitas."])
    except Exception:
        return HealthResult(0, "F", {}, ["Error: Fallo al validar métricas."])

    breakdown: Dict[MetricKey, int] = {}
    ratios: Dict[MetricKey, float] = {}
    final_score: float = 0.0
    
    for area, weight in _WEIGHT_ITEMS_INT:
        scorer = _SCORERS.get(area)
        if scorer is None: continue
        
        try:
            ratio = scorer(metrics)
        except Exception:
            ratio = 0.0
        
        ratios[area] = ratio
        puntos = round(ratio * weight)
        breakdown[area] = int(_clamp(float(puntos), 0.0, float(weight)))
        final_score += breakdown[area]
    
    recommendations = []
    for r in _RECOMMENDATION_RULES:
        if r.check(metrics, ratios.get(r.area, 0.0)):
            recommendations.append(r.message_factory(metrics))
            
    if metrics.quarantined_count > 0:
        recommendations.append(f"Tenés {metrics.quarantined_count} archivo(s) en cuarentena.")
    
    score_int = int(_clamp(final_score, 0.0, 100.0))
    return HealthResult(score_int, grade_for_score(score_int), breakdown, recommendations or ["No hay nada urgente para hacer. El sistema está en buen estado."])

def summarize(result: HealthResult) -> List[str]:
    """Genera una representación textual (lista de strings) formateada para la interfaz de usuario."""
    if not isinstance(result, HealthResult): return ["Error: Formato inválido."]
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, maximo in _WEIGHT_ITEMS_INT:
        puntos = result.breakdown.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{'#' * puntos}{'.' * (max(0, maximo - puntos))}]")
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {r}" for r in result.recommendations] if result.recommendations else ["  - Ninguna."])
    return lines
