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
from typing import Dict, List, Any, Final, Tuple, TypeAlias
import math

# Tipos para mejorar la claridad en el flujo de datos
ScoreMap: TypeAlias = Dict[str, float]

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
JUNK_LIMIT_MB: Final[float] = 5000.0          
DUPLICATE_LIMIT_MB: Final[float] = 2000.0     
STARTUP_LIMIT_COUNT: Final[int] = 20          
RAM_IDEAL_PERCENT: Final[float] = 35.0        
DISK_IDEAL_PERCENT: Final[float] = 25.0       

# --- UMBRALES DE ADVERTENCIA (ratios de 0.0 a 1.0) ---
WARN_THRESHOLD_HIGH: Final[float] = 0.9
WARN_THRESHOLD_MED: Final[float] = 0.8
WARN_THRESHOLD_LOW: Final[float] = 0.6

# --- PESOS DE CALIFICACIÓN ---
# Define la importancia relativa de cada módulo en el puntaje global (suma 100).
WEIGHTS: Final[Dict[str, int]] = {
    "seguridad": 30,
    "disco": 20,
    "memoria": 18,
    "basura": 14,
    "duplicados": 10,
    "arranque": 8,
}

_TOTAL_WEIGHTS: Final[int] = sum(WEIGHTS.values())
_WEIGHT_ITEMS: Final[List[Tuple[str, int]]] = list(WEIGHTS.items())


def _validate_weights() -> bool:
    """Valida que los pesos definidos en WEIGHTS sean enteros positivos."""
    return _TOTAL_WEIGHTS > 0 and all(isinstance(w, int) and w >= 0 for w in WEIGHTS.values())


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
        """Normaliza y asegura que los valores de las métricas sean numéricos finitos."""
        self.junk_mb = max(0.0, _to_float(self.junk_mb))
        self.suspicious_count = max(0, _to_int(self.suspicious_count))
        self.suspicious_warnings = max(0, _to_int(self.suspicious_warnings))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent), 0.0, 100.0)
        self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
        self.startup_count = max(0, _to_int(self.startup_count))
        self.quarantined_count = max(0, _to_int(self.quarantined_count))

    def is_finite(self) -> bool:
        """Verifica que todos los campos numéricos sean números reales finitos."""
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
        """Determina si el estado general del sistema es satisfactorio (>= 80)."""
        return math.isfinite(self.score) and self.score >= 80


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Restringe un valor al intervalo cerrado [low, high]. Retorna 'low' en caso de NaN/inf."""
    if not math.isfinite(value): return low
    return max(low, min(high, value))


def _to_float(value: Any, default: float = 0.0) -> float:
    """Intenta convertir un valor arbitrario a float. Retorna 'default' si falla o no es finito."""
    try:
        val = float(value)
        return val if math.isfinite(val) else default
    except (TypeError, ValueError): return default


def _to_int(value: Any, default: int = 0) -> int:
    """Intenta convertir un valor arbitrario a int. Retorna 'default' ante excepciones de tipo."""
    try: return int(float(value))
    except (TypeError, ValueError): return default


def score_junk(junk_mb: float) -> float:
    """Ratio (0.0-1.0): 1.0 implica limpieza total; disminuye a medida que la basura aumenta."""
    return 1.0 if JUNK_LIMIT_MB <= 0.0 else _clamp(1.0 - (_to_float(junk_mb) / JUNK_LIMIT_MB))


def score_security(suspicious_count: int, warnings: int = 0) -> float:
    """
    Ratio (0.0-1.0): Calcula penalización basada en hallazgos (5% c/u) y advertencias (25% c/u).
    """
    count = max(0, _to_int(suspicious_count))
    warn = max(0, _to_int(warnings))
    penalty = (count * 0.05) + (warn * 0.25)
    return _clamp(1.0 - penalty, 0.0, 1.0)


def score_memory(available_percent: float) -> float:
    """Ratio (0.0-1.0): 1.0 si la memoria disponible alcanza o supera el umbral ideal."""
    if RAM_IDEAL_PERCENT <= 0.0: return 0.0
    val = _to_float(available_percent)
    return _clamp(val / RAM_IDEAL_PERCENT, 0.0, 1.0)


def score_disk(free_percent: float) -> float:
    """Ratio (0.0-1.0): 1.0 si el espacio libre alcanza o supera el umbral ideal."""
    return 0.0 if DISK_IDEAL_PERCENT <= 0.0 else _clamp(_to_float(free_percent) / DISK_IDEAL_PERCENT, 0.0, 1.0)


def score_duplicates(duplicate_mb: float) -> float:
    """Ratio (0.0-1.0): 1.0 si no existen duplicados significativos según el límite definido."""
    return 1.0 if DUPLICATE_LIMIT_MB <= 0.0 else _clamp(1.0 - (_to_float(duplicate_mb) / DUPLICATE_LIMIT_MB), 0.0, 1.0)


def score_startup(startup_count: int) -> float:
    """Ratio (0.0-1.0): 1.0 si no hay programas al inicio, penaliza linealmente hasta el límite."""
    val = float(max(0, _to_int(startup_count)))
    return 1.0 if STARTUP_LIMIT_COUNT <= 0 else _clamp(1.0 - (val / STARTUP_LIMIT_COUNT), 0.0, 1.0)


def grade_for_score(score: int) -> str:
    """Mapea un puntaje entero (0-100) a una calificación cualitativa (A-F)."""
    s = int(_clamp(float(score), 0.0, 100.0))
    if s >= 90: return "A"
    if s >= 80: return "B"
    if s >= 65: return "C"
    if s >= 50: return "D"
    return "F"


def _generate_recommendations(m: SystemMetrics, ratios: ScoreMap) -> List[str]:
    """Genera recomendaciones basadas en los ratios de cada área evaluada."""
    recs: List[str] = []
    if ratios.get("seguridad", 1.0) < WARN_THRESHOLD_HIGH:
        recs.append(f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad; podés aislarlos en cuarentena.")
    if ratios.get("disco", 1.0) < WARN_THRESHOLD_LOW:
        recs.append(f"Queda {m.disk_free_percent:.1f}% de disco libre.")
    if ratios.get("memoria", 1.0) < WARN_THRESHOLD_LOW:
        recs.append("Memoria disponible baja: cerrá procesos innecesarios.")
    if ratios.get("basura", 1.0) < WARN_THRESHOLD_MED:
        recs.append(f"Hay {int(m.junk_mb)} MB de archivos temporales.")
    if ratios.get("duplicados", 1.0) < WARN_THRESHOLD_MED:
        recs.append(f"Podrías recuperar {int(m.duplicate_mb)} MB eliminando duplicados.")
    if ratios.get("arranque", 1.0) < WARN_THRESHOLD_LOW:
        recs.append(f"{m.startup_count} programas arrancan con Windows.")
    
    if m.quarantined_count > 0:
        recs.append(f"Tenés {m.quarantined_count} archivo(s) en cuarentena.")
    
    return recs or ["No hay nada urgente para hacer. El sistema está en buen estado."]


def compute_score(metrics: SystemMetrics) -> HealthResult:
    """
    Ejecuta el cálculo ponderado de salud integral del sistema.
    Normaliza métricas, aplica pesos definidos en `WEIGHTS` y clasifica el estado.
    """
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
    
    if not all(math.isfinite(ratios.get(k, float('nan'))) for k in WEIGHTS):
        return HealthResult(0, "F", {}, ["Error: Cálculo de salud fallido."])

    breakdown: Dict[str, int] = {}
    total_score: float = 0.0
    
    for area, weight in _WEIGHT_ITEMS:
        ratio = ratios.get(area, 0.0)
        # Ponderación defensiva: asegurar que el valor intermedio sea numérico
        score_val = (ratio * weight * 100.0) / _TOTAL_WEIGHTS
        if not math.isfinite(score_val):
            score_val = 0.0
        breakdown[area] = round(score_val)
        total_score += score_val

    if not math.isfinite(total_score):
        total_score = 0.0

    final_score = int(_clamp(round(total_score), 0.0, 100.0))
    return HealthResult(
        score=final_score,
        grade=grade_for_score(final_score),
        breakdown=breakdown,
        recommendations=_generate_recommendations(metrics, ratios),
    )


def summarize(result: HealthResult) -> List[str]:
    """Genera una representación textual formateada de los resultados del análisis."""
    if not isinstance(result, HealthResult): return ["Error: Formato inválido."]

    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    for area, maximo in _WEIGHT_ITEMS:
        puntos = result.breakdown.get(area, 0)
        visual = f"[{'#' * puntos}{'.' * (max(0, maximo - puntos))}]"
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} {visual}")
    
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {r}" for r in result.recommendations] if result.recommendations else ["  - Ninguna."])
    return lines
