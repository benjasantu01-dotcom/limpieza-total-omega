"""
healthscore.py — el panel que combina todos los módulos en un solo número.

Toma las mediciones de limpieza, seguridad, memoria, disco, duplicados y
arranque, y las convierte en un puntaje de 0 a 100 con una nota de A a F y
recomendaciones concretas.

DECISIÓN DE DISEÑO: `compute_score` es una función pura — recibe un objeto
con las mediciones y no toca el disco ni el sistema. Eso permite testear
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

# Cuánto pesa cada área en el puntaje final. Suman 100.
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
    """Mediciones crudas que alimentan el puntaje."""
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0

    def validate(self) -> None:
        """Asegura que todos los campos tengan valores numéricos finitos."""
        for field_name, field_type in self.__annotations__.items():
            val = getattr(self, field_name)
            if field_type is float:
                setattr(self, field_name, _to_float(val))
            elif field_type is int:
                setattr(self, field_name, _to_int(val))


@dataclass
class HealthResult:
    """Resultado del cálculo: puntaje, nota, desglose y recomendaciones."""
    score: int
    grade: str
    breakdown: Dict[str, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        return self.score >= 80


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Acota un valor al rango [low, high] y maneja NaN e infinito."""
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return low
    return max(low, min(high, float(value)))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        val = float(value) if value is not None else default
        return val if math.isfinite(val) else default
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def score_junk(junk_mb: float) -> float:
    return _clamp(1.0 - (max(0.0, _to_float(junk_mb)) / 5000.0))


def score_security(suspicious_count: int, warnings: int = 0) -> float:
    penalty = (max(0, _to_int(suspicious_count)) * 0.05) + (max(0, _to_int(warnings)) * 0.25)
    return _clamp(1.0 - penalty)


def score_memory(available_percent: float) -> float:
    val = _to_float(available_percent)
    return _clamp(max(0.0, val) / 35.0)


def score_disk(free_percent: float) -> float:
    val = _to_float(free_percent)
    return _clamp(max(0.0, val) / 25.0)


def score_duplicates(duplicate_mb: float) -> float:
    return _clamp(1.0 - (max(0.0, _to_float(duplicate_mb)) / 2000.0))


def score_startup(startup_count: int) -> float:
    return _clamp(1.0 - (max(0, _to_int(startup_count)) / 20.0))


def grade_for_score(score: int) -> str:
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 65: return "C"
    if score >= 50: return "D"
    return "F"


def _generate_recommendations(m: SystemMetrics, ratios: Dict[str, float]) -> List[str]:
    recs: List[str] = []
    if ratios.get("seguridad", 1.0) < 0.9:
        recs.append(f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad; podés aislarlos en cuarentena sin borrarlos.")
    if ratios.get("disco", 1.0) < 0.6:
        recs.append(f"Queda {round(m.disk_free_percent, 1)}% de disco libre. Mirá el análisis de disco para ver qué ocupa más.")
    if ratios.get("memoria", 1.0) < 0.6:
        recs.append("Memoria disponible baja: cerrá programas que no uses. Ojo, 'liberar RAM' no sirve, cerrar procesos sí.")
    if ratios.get("basura", 1.0) < 0.8:
        recs.append(f"Hay unos {round(m.junk_mb)} MB de archivos temporales para revisar.")
    if ratios.get("duplicados", 1.0) < 0.8:
        recs.append(f"Podrías recuperar ~{round(m.duplicate_mb)} MB eliminando copias duplicadas.")
    if ratios.get("arranque", 1.0) < 0.6:
        recs.append(f"{m.startup_count} programas arrancan con Windows; desactivá los que no necesites desde el Administrador de tareas.")
    if getattr(m, 'quarantined_count', 0) > 0:
        recs.append(f"Tenés {m.quarantined_count} archivo(s) en cuarentena esperando tu decisión.")
    if not recs:
        recs.append("No hay nada urgente para hacer. El sistema está en buen estado.")
    return recs


def compute_score(metrics: SystemMetrics) -> HealthResult:
    if metrics is None or not isinstance(metrics, SystemMetrics):
        return HealthResult(0, "F", {}, ["Error: Datos de entrada nulos o inválidos."])

    try:
        metrics.validate()
        
        ratios = {
            "seguridad": score_security(metrics.suspicious_count, metrics.suspicious_warnings),
            "disco": score_disk(metrics.disk_free_percent),
            "memoria": score_memory(metrics.memory_available_percent),
            "basura": score_junk(metrics.junk_mb),
            "duplicados": score_duplicates(metrics.duplicate_mb),
            "arranque": score_startup(metrics.startup_count),
        }

        breakdown = {k: int(round(ratios[k] * WEIGHTS[k])) for k in WEIGHTS}
        total = sum(breakdown.values())

    except Exception:
        return HealthResult(0, "F", {}, ["Error inesperado al calcular las métricas."])

    return HealthResult(
        score=max(0, min(100, total)),
        grade=grade_for_score(total),
        breakdown=breakdown,
        recommendations=_generate_recommendations(metrics, ratios),
    )


def summarize(result: HealthResult) -> List[str]:
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    orden = sorted(result.breakdown.items(), key=lambda kv: kv[1] - WEIGHTS.get(kv[0], 0))
    for area, puntos in orden:
        maximo = WEIGHTS.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{'#' * puntos}{'.' * (maximo - puntos)}]")
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {rec}" for rec in result.recommendations])
    return lines
