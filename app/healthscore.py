"""
healthscore.py — el panel que combina todos los módulos en un solo número.

Toma las mediciones de limpieza, seguridad, memoria, disco, duplicados y
arranque, y las convierte en un puntaje de 0 a 100 con una nota de A a F y
recomendaciones concretas.

DECISIÓN DE DISEÑO: `compute_score` es una función pura — recibe un objeto
con las métricas y no toca el disco ni el sistema. Eso permite testear
todos los casos límite (sistema impecable, sistema desastroso, datos
faltantes) sin necesitar una PC sucia de verdad. La recolección de datos
vive en los otros módulos; acá solo se puntúa.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Any
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

# Cuánto pesa cada área en el puntaje final. Suman 100 puntos totales.
WEIGHTS: Dict[str, int] = {
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
        """Normaliza los valores internos para evitar estados inválidos en los cálculos."""
        self.junk_mb = max(0.0, _to_float(self.junk_mb))
        self.suspicious_count = max(0, _to_int(self.suspicious_count))
        self.suspicious_warnings = max(0, _to_int(self.suspicious_warnings))
        self.memory_available_percent = _clamp(_to_float(self.memory_available_percent), 0.0, 100.0)
        self.disk_free_percent = _clamp(_to_float(self.disk_free_percent), 0.0, 100.0)
        self.duplicate_mb = max(0.0, _to_float(self.duplicate_mb))
        self.startup_count = max(0, _to_int(self.startup_count))
        self.quarantined_count = max(0, _to_int(self.quarantined_count))


@dataclass
class HealthResult:
    """Resultado final del cómputo: puntaje (0-100), nota (A-F) y consejos."""
    score: int
    grade: str
    breakdown: Dict[str, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Determina si el estado general es aceptable (>= 80/100)."""
        return self.score >= 80


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Acota un valor al rango [low, high] para estandarizar ratios."""
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return low
    return max(low, min(high, float(value)))


def _to_float(value: Any, default: float = 0.0) -> float:
    """Conversor seguro de tipos a float con manejo de errores."""
    try:
        val = float(value) if value is not None else default
        return val if math.isfinite(val) else default
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    """Conversor seguro de tipos a int con manejo de errores."""
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def score_junk(junk_mb: float) -> float:
    """Puntúa archivos basura (0.0 a 1.0): escala lineal donde 5000MB es el límite crítico."""
    return _clamp(1.0 - (junk_mb / 5000.0))


def score_security(suspicious_count: int, warnings: int = 0) -> float:
    """Puntúa seguridad (0.0 a 1.0): penaliza hallazgos (5%) y advertencias (25%)."""
    penalty = (suspicious_count * 0.05) + (warnings * 0.25)
    return _clamp(1.0 - penalty)


def score_memory(available_percent: float) -> float:
    """Puntúa RAM (0.0 a 1.0): el umbral óptimo de disponibilidad es 35%."""
    return _clamp(available_percent / 35.0)


def score_disk(free_percent: float) -> float:
    """Puntúa espacio (0.0 a 1.0): el umbral crítico es 25% de disco libre."""
    return _clamp(free_percent / 25.0)


def score_duplicates(duplicate_mb: float) -> float:
    """Puntúa duplicados (0.0 a 1.0): escala lineal con penalización máxima a 2000MB."""
    return _clamp(1.0 - (duplicate_mb / 2000.0))


def score_startup(startup_count: int) -> float:
    """Puntúa inicio (0.0 a 1.0): penalización lineal, el umbral de sobrecarga es 20 entradas."""
    return _clamp(1.0 - (startup_count / 20.0))


def grade_for_score(score: int) -> str:
    """Convierte el score 0-100 a escala escolar A-F."""
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 65: return "C"
    if score >= 50: return "D"
    return "F"


def _generate_recommendations(m: SystemMetrics, ratios: Dict[str, float]) -> List[str]:
    """Genera una lista de acciones correctivas basadas en ratios bajos por área."""
    recs: List[str] = []
    
    if ratios.get("seguridad", 1.0) < 0.9:
        recs.append(f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad; podés aislarlos en cuarentena sin borrarlos.")
    if ratios.get("disco", 1.0) < 0.6:
        recs.append(f"Queda {round(m.disk_free_percent, 1)}% de disco libre. Mirá el análisis de disco para ver qué ocupa más.")
    if ratios.get("memoria", 1.0) < 0.6:
        recs.append("Memoria disponible baja: cerrá programas que no uses. Ojo, 'liberar RAM' no sirve, cerrar procesos sí.")
    if ratios.get("basura", 1.0) < 0.8:
        recs.append(f"Hay unos {int(m.junk_mb)} MB de archivos temporales para revisar.")
    if ratios.get("duplicados", 1.0) < 0.8:
        recs.append(f"Podrías recuperar ~{int(m.duplicate_mb)} MB eliminando copias duplicadas.")
    if ratios.get("arranque", 1.0) < 0.6:
        recs.append(f"{m.startup_count} programas arrancan con Windows; desactivá los que no necesites desde el Administrador de tareas.")
    
    if m.quarantined_count > 0:
        recs.append(f"Tenés {m.quarantined_count} archivo(s) en cuarentena esperando tu decisión.")
    
    if not recs:
        recs.append("No hay nada urgente para hacer. El sistema está en buen estado.")
    return recs


def compute_score(metrics: SystemMetrics) -> HealthResult:
    """Calcula el HealthResult unificando todas las heurísticas y pesos definidos."""
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(0, "F", {}, ["Error: Datos de entrada con formato inválido."])

    try:
        metrics.validate()
        
        calculators = {
            "seguridad": lambda: score_security(metrics.suspicious_count, metrics.suspicious_warnings),
            "disco": lambda: score_disk(metrics.disk_free_percent),
            "memoria": lambda: score_memory(metrics.memory_available_percent),
            "basura": lambda: score_junk(metrics.junk_mb),
            "duplicados": lambda: score_duplicates(metrics.duplicate_mb),
            "arranque": lambda: score_startup(metrics.startup_count),
        }

        ratios: Dict[str, float] = {}
        for key in WEIGHTS:
            val = calculators[key]()
            ratios[key] = val if math.isfinite(val) else 0.0
            
        breakdown = {key: int(round(ratios[key] * WEIGHTS.get(key, 0))) for key in WEIGHTS}
        total = sum(breakdown.values())

    except (TypeError, ValueError, ZeroDivisionError, KeyError) as e:
        return HealthResult(0, "F", {}, [f"Error al procesar métricas: {str(e)}"])

    return HealthResult(
        score=max(0, min(100, total)),
        grade=grade_for_score(total),
        breakdown=breakdown,
        recommendations=_generate_recommendations(metrics, ratios),
    )


def summarize(result: HealthResult) -> List[str]:
    """Genera un reporte visual legible para mostrar en la interfaz o logs."""
    lines: List[str] = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    orden = sorted(result.breakdown.items(), key=lambda kv: kv[1] - WEIGHTS.get(kv[0], 0))
    for area, puntos in orden:
        maximo = WEIGHTS.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{'#' * puntos}{'.' * (maximo - puntos)}]")
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {rec}" for rec in result.recommendations])
    return lines
