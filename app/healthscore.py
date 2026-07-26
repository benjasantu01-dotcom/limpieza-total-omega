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
# Seguridad pesa más que limpieza a propósito: un archivo sospechoso es un
# problema real, unos MB de basura no.
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
    """Mediciones crudas que alimentan el puntaje.

    Todos los campos tienen valor por defecto para que un panel se pueda
    calcular aunque falte un módulo por analizar todavía.
    """
    junk_mb: float = 0.0
    suspicious_count: int = 0
    suspicious_warnings: int = 0
    memory_available_percent: float = 100.0
    disk_free_percent: float = 100.0
    duplicate_mb: float = 0.0
    startup_count: int = 0
    quarantined_count: int = 0


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
    """Convierte de forma segura a float evitando excepciones por None."""
    try:
        val = float(value) if value is not None else default
        return val if math.isfinite(val) else default
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    """Convierte de forma segura a int evitando excepciones por None."""
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def score_junk(junk_mb: float) -> float:
    """Puntúa la basura. Escala lineal donde 0 MB es 100% y 5000 MB es 0%."""
    val = max(0.0, _to_float(junk_mb))
    return _clamp(1.0 - (val / 5000.0))


def score_security(suspicious_count: int, warnings: int = 0) -> float:
    """Puntúa la seguridad penalizando hallazgos.
    
    Cada ítem sospechoso reduce el score en 5% y cada advertencia en 25%.
    La suma de penalizaciones se resta de la base 1.0.
    """
    s_count = max(0, _to_int(suspicious_count))
    w_count = max(0, _to_int(warnings))
    penalty = (s_count * 0.05) + (w_count * 0.25)
    return _clamp(1.0 - penalty)


def score_memory(available_percent: float) -> float:
    """Puntúa la RAM disponible.
    
    Se espera un 35% de margen operativo saludable. Valores mayores a 35%
    otorgan el puntaje máximo tras el acotado.
    """
    val = max(0.0, _to_float(available_percent, 0.0))
    return _clamp(val / 35.0)


def score_disk(free_percent: float) -> float:
    """Puntúa el espacio libre.
    
    El umbral de eficiencia es 25% de espacio libre total. Menos de eso
    comienza a reducir proporcionalmente el puntaje.
    """
    val = max(0.0, _to_float(free_percent, 0.0))
    return _clamp(val / 25.0)


def score_duplicates(duplicate_mb: float) -> float:
    """Puntúa ineficiencia por duplicados. 2000 MB (2 GB) marca el 0% de score."""
    val = max(0.0, _to_float(duplicate_mb))
    return _clamp(1.0 - (val / 2000.0))


def score_startup(startup_count: int) -> float:
    """Puntúa el arranque. 20 aplicaciones es el máximo tolerable antes de 0%."""
    s_count = max(0, _to_int(startup_count))
    return _clamp(1.0 - (s_count / 20.0))


def grade_for_score(score: int) -> str:
    """Convierte el puntaje 0-100 en una nota de A a F."""
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 65:
        return "C"
    if score >= 50:
        return "D"
    return "F"


def compute_score(metrics: SystemMetrics) -> HealthResult:
    """Calcula el puntaje de salud total combinando métricas ponderadas."""
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(0, "F", {}, ["Error: Datos de entrada inválidos."])

    try:
        # Pre-calculamos los ratios para las recomendaciones
        r_sec = score_security(metrics.suspicious_count, metrics.suspicious_warnings)
        r_dis = score_disk(metrics.disk_free_percent)
        r_mem = score_memory(metrics.memory_available_percent)
        r_jun = score_junk(metrics.junk_mb)
        r_dup = score_duplicates(metrics.duplicate_mb)
        r_sta = score_startup(metrics.startup_count)

        breakdown: Dict[str, int] = {
            "seguridad": int(round(_clamp(r_sec) * WEIGHTS["seguridad"])),
            "disco": int(round(_clamp(r_dis) * WEIGHTS["disco"])),
            "memoria": int(round(_clamp(r_mem) * WEIGHTS["memoria"])),
            "basura": int(round(_clamp(r_jun) * WEIGHTS["basura"])),
            "duplicados": int(round(_clamp(r_dup) * WEIGHTS["duplicados"])),
            "arranque": int(round(_clamp(r_sta) * WEIGHTS["arranque"])),
        }
            
    except Exception:
        return HealthResult(0, "F", {}, ["Error inesperado al calcular las métricas."])

    total = min(100, max(0, sum(breakdown.values())))

    recommendations: List[str] = []
    if r_sec < 0.9:
        recommendations.append(
            f"Revisá los {_to_int(metrics.suspicious_count)} hallazgo(s) de seguridad; "
            "podés aislarlos en cuarentena sin borrarlos."
        )
    if r_dis < 0.6:
        recommendations.append(
            f"Queda {round(_to_float(metrics.disk_free_percent), 1)}% de disco libre. "
            "Mirá el análisis de disco para ver qué ocupa más."
        )
    if r_mem < 0.6:
        recommendations.append(
            "Memoria disponible baja: cerrá programas que no uses. "
            "Ojo, 'liberar RAM' no sirve, cerrar procesos sí."
        )
    if r_jun < 0.8:
        recommendations.append(
            f"Hay unos {round(_to_float(metrics.junk_mb))} MB de archivos temporales para revisar."
        )
    if r_dup < 0.8:
        recommendations.append(
            f"Podrías recuperar ~{round(_to_float(metrics.duplicate_mb))} MB eliminando copias duplicadas."
        )
    if r_sta < 0.6:
        recommendations.append(
            f"{_to_int(metrics.startup_count)} programas arrancan con Windows; "
            "desactivá los que no necesites desde el Administrador de tareas."
        )
    if _to_int(metrics.quarantined_count) > 0:
        recommendations.append(
            f"Tenés {metrics.quarantined_count} archivo(s) en cuarentena esperando tu decisión."
        )
    if not recommendations:
        recommendations.append("No hay nada urgente para hacer. El sistema está en buen estado.")

    return HealthResult(
        score=total,
        grade=grade_for_score(total),
        breakdown=breakdown,
        recommendations=recommendations,
    )


def summarize(result: HealthResult) -> List[str]:
    """Resumen legible del puntaje, con desglose y recomendaciones."""
    lines = [
        f"Salud del sistema: {result.score}/100  (nota {result.grade})",
        "",
        "Desglose por área:",
    ]
    # Ordenar por el delta entre el puntaje obtenido y el máximo posible (prioriza problemas)
    orden = sorted(result.breakdown.items(), key=lambda kv: kv[1] - WEIGHTS.get(kv[0], 0))
    for area, puntos in orden:
        maximo = WEIGHTS.get(area, 0)
        barra = f"{'#' * puntos}{'.' * (maximo - puntos)}"
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{barra}]")
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {rec}" for rec in result.recommendations])
    return lines
