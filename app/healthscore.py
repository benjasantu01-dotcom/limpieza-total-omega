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
        """Normaliza los tipos de datos internos para evitar errores en cálculos matemáticos."""
        for field_name, field_type in self.__annotations__.items():
            try:
                val = getattr(self, field_name, None)
                if field_type is float:
                    setattr(self, field_name, _to_float(val))
                elif field_type is int:
                    setattr(self, field_name, _to_int(val))
            except AttributeError:
                continue


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
    """Acota un valor al rango [0.0, 1.0] para estandarizar ratios de rendimiento."""
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
    """Puntúa archivos temporales: castigo lineal. 0MB=1.0, 5000MB=0.0."""
    return _clamp(1.0 - (max(0.0, _to_float(junk_mb)) / 5000.0))


def score_security(suspicious_count: int, warnings: int = 0) -> float:
    """Puntúa seguridad: cada hallazgo reduce el score (5% c/u) y advertencia (25% c/u)."""
    penalty = (max(0, _to_int(suspicious_count)) * 0.05) + (max(0, _to_int(warnings)) * 0.25)
    return _clamp(1.0 - penalty)


def score_memory(available_percent: float) -> float:
    """Puntúa disponibilidad de RAM. 35% de margen libre se considera nivel óptimo."""
    val = _to_float(available_percent)
    return _clamp(max(0.0, val) / 35.0)


def score_disk(free_percent: float) -> float:
    """Puntúa espacio en disco. 25% de espacio libre es el umbral de salud deseado."""
    val = _to_float(free_percent)
    return _clamp(max(0.0, val) / 25.0)


def score_duplicates(duplicate_mb: float) -> float:
    """Puntúa duplicados: penalización hasta alcanzar el umbral de 2GB (2000MB)."""
    return _clamp(1.0 - (max(0.0, _to_float(duplicate_mb)) / 2000.0))


def score_startup(startup_count: int) -> float:
    """Puntúa programas de inicio: penalización creciente hasta 20 entradas."""
    return _clamp(1.0 - (max(0, _to_int(startup_count)) / 20.0))


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
    
    # Usar .get con default 1.0 para evitar KeyError si el diccionario está incompleto
    if ratios.get("seguridad", 1.0) < 0.9:
        recs.append(f"Revisá los {m.suspicious_count} hallazgo(s) de seguridad; podés aislarlos en cuarentena sin borrarlos.")
    if ratios.get("disco", 1.0) < 0.6:
        recs.append(f"Queda {round(float(m.disk_free_percent), 1)}% de disco libre. Mirá el análisis de disco para ver qué ocupa más.")
    if ratios.get("memoria", 1.0) < 0.6:
        recs.append("Memoria disponible baja: cerrá programas que no uses. Ojo, 'liberar RAM' no sirve, cerrar procesos sí.")
    if ratios.get("basura", 1.0) < 0.8:
        recs.append(f"Hay unos {round(float(m.junk_mb))} MB de archivos temporales para revisar.")
    if ratios.get("duplicados", 1.0) < 0.8:
        recs.append(f"Podrías recuperar ~{round(float(m.duplicate_mb))} MB eliminando copias duplicadas.")
    if ratios.get("arranque", 1.0) < 0.6:
        recs.append(f"{m.startup_count} programas arrancan con Windows; desactivá los que no necesites desde el Administrador de tareas.")
    
    if getattr(m, 'quarantined_count', 0) > 0:
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
        
        ratios = {
            "seguridad": score_security(metrics.suspicious_count, metrics.suspicious_warnings),
            "disco": score_disk(metrics.disk_free_percent),
            "memoria": score_memory(metrics.memory_available_percent),
            "basura": score_junk(metrics.junk_mb),
            "duplicados": score_duplicates(metrics.duplicate_mb),
            "arranque": score_startup(metrics.startup_count),
        }

        # Aplica los pesos configurados en la constante global WEIGHTS
        breakdown = {k: int(round(ratios.get(k, 0.0) * WEIGHTS.get(k, 0))) for k in WEIGHTS}
        total = sum(breakdown.values())

    except (TypeError, ValueError, ZeroDivisionError) as e:
        return HealthResult(0, "F", {}, [f"Error al procesar métricas: {str(e)}"])

    return HealthResult(
        score=max(0, min(100, total)),
        grade=grade_for_score(total),
        breakdown=breakdown,
        recommendations=_generate_recommendations(metrics, ratios),
    )


def summarize(result: HealthResult) -> List[str]:
    """Genera un reporte visual legible para mostrar en la interfaz o logs."""
    lines = [f"Salud del sistema: {result.score}/100  (nota {result.grade})", "", "Desglose por área:"]
    # Ordena por áreas con mayor desviación negativa respecto a su peso ideal
    orden = sorted(result.breakdown.items(), key=lambda kv: kv[1] - WEIGHTS.get(kv[0], 0))
    for area, puntos in orden:
        maximo = WEIGHTS.get(area, 0)
        lines.append(f"  {area.capitalize():<12} {puntos:>2}/{maximo:<2} [{'#' * puntos}{'.' * (maximo - puntos)}]")
    lines.extend(["", "Recomendaciones:"])
    lines.extend([f"  - {rec}" for rec in result.recommendations])
    return lines
