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
from typing import Dict, List

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
    """Acota un valor al rango [low, high]."""
    return max(low, min(high, value))


def score_junk(junk_mb: float) -> float:
    """Puntúa la basura. 0 MB es perfecto; 5 GB (5000 MB) es el umbral crítico."""
    return _clamp(1.0 - (max(0.0, float(junk_mb)) / 5000.0))


def score_security(suspicious_count: int, warnings: int = 0) -> float:
    """Puntúa los hallazgos de seguridad.
    
    Penalty logic: Cada ítem sospechoso reduce el score en 5%, mientras que
    cada advertencia lo hace en 25%, priorizando la detección de amenazas
    sobre la simple acumulación de logs.
    """
    penalty = max(0, int(suspicious_count)) * 0.05 + max(0, int(warnings)) * 0.25
    return _clamp(1.0 - penalty)


def score_memory(available_percent: float) -> float:
    """Puntúa la memoria por disponibilidad.
    
    El umbral de 35% se basa en el punto de saturación donde los sistemas 
    operativos modernos comienzan a recurrir a paginación agresiva, 
    degradando la experiencia del usuario.
    """
    return _clamp(max(0.0, float(available_percent)) / 35.0)


def score_disk(free_percent: float) -> float:
    """Puntúa el espacio libre en disco. 
    
    25% es el punto de referencia: debajo de esto, el sistema operativo 
    comienza a tener dificultades para la expansión de archivos temporales.
    """
    return _clamp(max(0.0, float(free_percent)) / 25.0)


def score_duplicates(duplicate_mb: float) -> float:
    """Puntúa espacio perdido. 2 GB (2000 MB) representa una pérdida ineficiente."""
    return _clamp(1.0 - (max(0.0, float(duplicate_mb)) / 2000.0))


def score_startup(startup_count: int) -> float:
    """Puntúa el arranque. 20 aplicaciones es el límite tolerable para 
    evitar tiempos de inicio excesivos.
    """
    return _clamp(1.0 - (max(0, int(startup_count)) / 20.0))


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
    """Calcula el puntaje de salud. Función pura: no toca el sistema."""
    if not isinstance(metrics, SystemMetrics):
        return HealthResult(0, "F", {}, ["Error: Datos de entrada inválidos."])

    ratios = {}
    try:
        ratios = {
            "seguridad": score_security(metrics.suspicious_count, metrics.suspicious_warnings),
            "disco": score_disk(metrics.disk_free_percent),
            "memoria": score_memory(metrics.memory_available_percent),
            "basura": score_junk(metrics.junk_mb),
            "duplicados": score_duplicates(metrics.duplicate_mb),
            "arranque": score_startup(metrics.startup_count),
        }
    except (ValueError, TypeError, ZeroDivisionError):
        return HealthResult(0, "F", {}, ["Error: Las métricas contienen valores corruptos."])

    breakdown = {area: int(round(ratio * WEIGHTS[area])) for area, ratio in ratios.items()}
    total = min(100, max(0, sum(breakdown.values())))

    recommendations: List[str] = []
    if ratios["seguridad"] < 0.9:
        recommendations.append(
            f"Revisá los {metrics.suspicious_count} hallazgo(s) de seguridad; "
            "podés aislarlos en cuarentena sin borrarlos."
        )
    if ratios["disco"] < 0.6:
        recommendations.append(
            f"Queda {round(float(metrics.disk_free_percent), 1)}% de disco libre. "
            "Mirá el análisis de disco para ver qué ocupa más."
        )
    if ratios["memoria"] < 0.6:
        recommendations.append(
            "Memoria disponible baja: cerrá programas que no uses. "
            "Ojo, 'liberar RAM' no sirve, cerrar procesos sí."
        )
    if ratios["basura"] < 0.8:
        recommendations.append(
            f"Hay unos {round(float(metrics.junk_mb))} MB de archivos temporales para revisar."
        )
    if ratios["duplicados"] < 0.8:
        recommendations.append(
            f"Podrías recuperar ~{round(float(metrics.duplicate_mb))} MB eliminando copias duplicadas."
        )
    if ratios["arranque"] < 0.6:
        recommendations.append(
            f"{metrics.startup_count} programas arrancan con Windows; "
            "desactivá los que no necesites desde el Administrador de tareas."
        )
    if metrics.quarantined_count:
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
    for area, puntos in sorted(result.breakdown.items(), key=lambda kv: kv[1] - WEIGHTS[kv[0]]):
        maximo = WEIGHTS[area]
        barra = "#" * puntos + "." * max(0, maximo - puntos)
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{barra}]")
    lines.extend(["", "Recomendaciones:"])
    for rec in result.recommendations:
        lines.append(f"  - {rec}")
    return lines
