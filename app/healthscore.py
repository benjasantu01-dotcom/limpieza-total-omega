"""
healthscore.py — el panel que combina todos los módulos en un solo número.

Toma las mediciones de limpieza, seguridad, memoria, disco, duplicados y
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
from typing import Dict, List, Any, Final, Tuple
import math

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

# Umbrales críticos para la normalización: definen el punto de saturación (ratio 0.0).
# Un valor igual o superior a estos límites en el caso de basura/duplicados
# o igual a cero en porcentajes, penaliza el puntaje al mínimo posible.
JUNK_LIMIT_MB: Final[float] = 5000.0          
DUPLICATE_LIMIT_MB: Final[float] = 2000.0     
STARTUP_LIMIT_COUNT: Final[int] = 20          
RAM_IDEAL_PERCENT: Final[float] = 35.0        
DISK_IDEAL_PERCENT: Final[float] = 25.0       

# Umbrales para disparar recomendaciones (ratios de 0.0 a 1.0).
# Se usan en _generate_recommendations para filtrar sugerencias relevantes 
# basándose en qué tan lejos está el sistema de su estado óptimo.
WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

# Peso relativo de cada área en el puntaje total (sumatoria debe ser 100).
WEIGHTS: Final[Dict[str, int]] = {
    "seguridad": 30,
    "disco": 20,
    "memoria": 18,
    "basura": 14,
    "duplicados": 10,
    "arranque": 8,
}


@dataclass
class SystemMetrics:
    """Contenedor de datos crudos (métricas) provenientes de los diversos módulos."""
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    def validate(self) -> None:
        """Normaliza y asegura que los valores de entrada estén en rangos válidos."""
        self.junk_mb = max(0.0, _to_float(self.junk_mb))
        self.suspicious_count = max(0, _to_int(self.suspicious_count))
        self.suspicious_warnings = max(0, _to_int(self.suspicious_warnings))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent), 0.0, 100.0)
        self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
        self.startup_count = max(0, _to_int(self.startup_count))
        self.quarantined_count = max(0, _to_int(self.quarantined_count))

    def is_finite(self) -> bool:
        """Valida que los datos de entrada sean números procesables (no NaN o Inf)."""
        return (math.isfinite(self.junk_mb) and 
                math.isfinite(self.memory_available_percent) and 
                math.isfinite(self.disk_free_percent) and 
                math.isfinite(self.duplicate_mb) and
                math.isfinite(float(self.suspicious_count)) and
                math.isfinite(float(self.startup_count)))


@dataclass
class HealthResult:
    """Resultado final: puntaje (0-100), nota cualitativa y recomendaciones."""
    score: int
    grade: str
    breakdown: Dict[str, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Retorna True si el estado del sistema es aceptable (>= 80/100)."""
        return self.score >= 80


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Acota un valor numérico al rango [low, high] y valida finitud."""
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return low
    return max(low, min(high, float(value)))


def _to_float(value: Any, default: float = 0.0) -> float:
    """Convierte entrada a float; retorna default en caso de error o valores no finitos."""
    try:
        val = float(value) if value is not None else default
        return val if math.isfinite(val) else default
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    """Convierte entrada a int; valida finitud mediante conversión intermedia."""
    try:
        val = int(float(value)) if value is not None else default
        return val if math.isfinite(float(val)) else default
    except (TypeError, ValueError):
        return default


def score_junk(junk_mb: float) -> float:
    """Calcula ratio [0, 1] donde 1 es 'cero basura' y 0 es 'mínimo aceptable'."""
    val = _to_float(junk_mb)
    if JUNK_LIMIT_MB <= 0: return 0.0
    return _clamp(1.0 - (val / JUNK_LIMIT_MB))


def score_security(suspicious_count: int, warnings: int = 0) -> float:
    """Calcula ratio [0, 1] penalizando hallazgos según un factor de riesgo."""
    s = max(0.0, _to_float(suspicious_count))
    w = max(0.0, _to_float(warnings))
    penalty: float = s * 0.05 + w * 0.25
    return _clamp(1.0 - penalty, 0.0, 1.0)


def score_memory(available_percent: float) -> float:
    """Calcula ratio [0, 1] basado en la proporción de RAM disponible."""
    val = _clamp(_to_float(available_percent), 0.0, 100.0)
    if RAM_IDEAL_PERCENT <= 0: return 0.0
    return _clamp(val / RAM_IDEAL_PERCENT)


def score_disk(free_percent: float) -> float:
    """Calcula ratio [0, 1] basado en la proporción de disco libre disponible."""
    val = _to_float(free_percent)
    if DISK_IDEAL_PERCENT <= 0: return 0.0
    return _clamp(val / DISK_IDEAL_PERCENT)


def score_duplicates(duplicate_mb: float) -> float:
    """Calcula ratio [0, 1] basado en el peso de archivos redundantes encontrados."""
    val = _to_float(duplicate_mb)
    if DUPLICATE_LIMIT_MB <= 0: return 0.0
    return _clamp(1.0 - (val / DUPLICATE_LIMIT_MB))


def score_startup(startup_count: int) -> float:
    """Calcula ratio [0, 1] inversamente proporcional al número de programas en inicio."""
    count = _to_int(startup_count)
    if STARTUP_LIMIT_COUNT <= 0: return 0.0
    return _clamp(1.0 - (float(count) / STARTUP_LIMIT_COUNT))


def grade_for_score(score: int) -> str:
    """Asigna una calificación escolar (A-F) basada en un puntaje [0-100]."""
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 65: return "C"
    if score >= 50: return "D"
    return "F"


def _generate_recommendations(m: SystemMetrics, ratios: Dict[str, float]) -> List[str]:
    """Genera acciones correctivas si los ratios individuales caen por debajo de los umbrales."""
    recs: List[str] = []
    
    if ratios.get("seguridad", 1.0) < WARN_THRESHOLD_HIGH:
        recs.append(f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad; podés aislarlos en cuarentena sin borrarlos.")
    if ratios.get("disco", 1.0) < WARN_THRESHOLD_LOW:
        recs.append(f"Queda {m.disk_free_percent:.1f}% de disco libre. Mirá el análisis de disco para ver qué ocupa más.")
    if ratios.get("memoria", 1.0) < WARN_THRESHOLD_LOW:
        recs.append("Memoria disponible baja: cerrá programas que no uses. Ojo, 'liberar RAM' no sirve, cerrar procesos sí.")
    if ratios.get("basura", 1.0) < WARN_THRESHOLD_MED:
        recs.append(f"Hay unos {int(m.junk_mb)} MB de archivos temporales para revisar.")
    if ratios.get("duplicados", 1.0) < WARN_THRESHOLD_MED:
        recs.append(f"Podrías recuperar ~{int(m.duplicate_mb)} MB eliminando copias duplicadas.")
    if ratios.get("arranque", 1.0) < WARN_THRESHOLD_LOW:
        recs.append(f"{m.startup_count} programas arrancan con Windows; desactivá los que no necesites desde el Administrador de tareas.")
    
    if m.quarantined_count > 0:
        recs.append(f"Tenés {m.quarantined_count} archivo(s) en cuarentena esperando tu decisión.")
    
    if not recs:
        recs.append("No hay nada urgente para hacer. El sistema está en buen estado.")
    return recs


def compute_score(metrics: SystemMetrics) -> HealthResult:
    """Procesa métricas y devuelve un objeto HealthResult con el puntaje final calculado."""
    if metrics is None or not isinstance(metrics, SystemMetrics):
        return HealthResult(0, "F", {}, ["Error: Instancia de métricas nula o no válida."])
    
    metrics.validate()
    if not metrics.is_finite():
        return HealthResult(0, "F", {}, ["Error: Métricas contienen datos no procesables."])
    
    # Validamos pesos para evitar divisiones por cero o pesos corruptos
    valid_weights = {k: max(0, int(v)) for k, v in WEIGHTS.items()}
    total_weights = sum(valid_weights.values())
    if total_weights <= 0:
        return HealthResult(0, "F", {}, ["Error: Configuración de pesos inválida."])

    ratios = {
        "seguridad": score_security(metrics.suspicious_count, metrics.suspicious_warnings),
        "disco": score_disk(metrics.disk_free_percent),
        "memoria": score_memory(metrics.memory_available_percent),
        "basura": score_junk(metrics.junk_mb),
        "duplicados": score_duplicates(metrics.duplicate_mb),
        "arranque": score_startup(metrics.startup_count)
    }

    # Calculamos puntos ponderados, asegurando que cada ratio esté estrictamente en [0.0, 1.0]
    breakdown = {
        area: int((_clamp(ratios.get(area, 0.0)) * valid_weights.get(area, 0) * 100 / total_weights) + 0.5) 
        for area in valid_weights
    }
    total_score = _clamp(sum(breakdown.values()), 0.0, 100.0)

    return HealthResult(
        score=int(total_score),
        grade=grade_for_score(int(total_score)),
        breakdown=breakdown,
        recommendations=_generate_recommendations(metrics, ratios),
    )


def _sort_by_performance_delta(item: Tuple[str, int]) -> int:
    """Calcula la desviación respecto al peso ideal para ordenar áreas prioritarias."""
    area, puntos = item
    return puntos - WEIGHTS.get(area, 0)


def summarize(result: HealthResult) -> List[str]:
    """Genera una representación visual y textual detallada del resultado de salud."""
    lines: List[str] = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    
    for area, puntos in sorted(result.breakdown.items(), key=_sort_by_performance_delta):
        maximo: int = WEIGHTS.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{'#' * puntos}{'.' * (maximo - puntos)}]")
    
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {rec}" for rec in result.recommendations])
    return lines
