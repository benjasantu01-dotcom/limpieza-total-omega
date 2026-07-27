"""
test_retries.py — que un fallo temporal de Google no desperdicie una iteración.

POR QUÉ EXISTE
--------------
Medido sobre 373 iteraciones reales: 96 (26%) terminaron en "sin respuesta de
la IA". Al leer el log, la mayoría eran errores 503 (Service Unavailable) de
Google, que son temporales y se resuelven esperando unos segundos. Pero el
código los atrapaba con `except requests.RequestException` y devolvía None de
inmediato, tirando la iteración completa.

Cada iteración perdida es una mejora menos por día. Estos tests fijan el
comportamiento correcto: reintentar lo temporal, no insistir con lo que no se
arregla reintentando.

El servidor se simula con un doble de `requests.post`, así los tests corren en
milisegundos y sin red. `time.sleep` también se anula, si no la espera con
retroceso haría que la suite tardara medio minuto.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import evolve  # noqa: E402


class RespuestaFalsa:
    """Imita lo justo de una respuesta de `requests` que usa call_gemini."""

    def __init__(self, status_code: int, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.text = text or "{}"
        self.headers = headers or {}

    def json(self):
        return {
            "candidates": [{"content": {"parts": [{"text": "RESPUESTA OK"}]}}]
        }

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


@pytest.fixture
def sin_esperas(monkeypatch):
    """Anula los sleep y garantiza que exista una API key falsa."""
    esperas = []
    monkeypatch.setattr(evolve.time, "sleep", lambda s: esperas.append(s))
    monkeypatch.setattr(evolve, "GEMINI_API_KEY", "clave-de-prueba")
    monkeypatch.setattr(evolve, "log", lambda *a, **k: None)
    return esperas


def responder(monkeypatch, secuencia):
    """Hace que cada llamada devuelva el siguiente elemento de `secuencia`."""
    llamadas = {"n": 0}

    def falso_post(*args, **kwargs):
        indice = min(llamadas["n"], len(secuencia) - 1)
        llamadas["n"] += 1
        item = secuencia[indice]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(evolve.requests, "post", falso_post)
    return llamadas


# --------------------------------------------------------------------------
# Lo que se arregla reintentando
# --------------------------------------------------------------------------

def test_recovers_after_a_temporary_503(monkeypatch, sin_esperas):
    """Un 503 seguido de éxito tiene que terminar en éxito.

    Este es exactamente el caso que antes tiraba la iteración a la basura.
    """
    llamadas = responder(monkeypatch, [
        RespuestaFalsa(503),
        RespuestaFalsa(200),
    ])
    assert evolve.call_gemini("prompt") == "RESPUESTA OK"
    assert llamadas["n"] == 2


def test_recovers_after_several_503s(monkeypatch, sin_esperas):
    llamadas = responder(monkeypatch, [
        RespuestaFalsa(503), RespuestaFalsa(500), RespuestaFalsa(200),
    ])
    assert evolve.call_gemini("prompt") == "RESPUESTA OK"
    assert llamadas["n"] == 3


def test_server_error_waits_longer_each_time(monkeypatch, sin_esperas):
    """La espera crece: no conviene martillar un servidor que está caído."""
    responder(monkeypatch, [RespuestaFalsa(503), RespuestaFalsa(503), RespuestaFalsa(200)])
    evolve.call_gemini("prompt")
    assert sin_esperas == sorted(sin_esperas), "las esperas deben ir creciendo"
    assert len(sin_esperas) == 2


def test_gives_up_after_the_retry_limit(monkeypatch, sin_esperas):
    """No se reintenta para siempre: hay corridas con tiempo límite."""
    llamadas = responder(monkeypatch, [RespuestaFalsa(503)])
    assert evolve.call_gemini("prompt") is None
    assert llamadas["n"] == evolve.MAX_RETRIES_ON_SERVER_ERROR + 1


def test_recovers_after_a_timeout(monkeypatch, sin_esperas):
    import requests
    llamadas = responder(monkeypatch, [
        requests.Timeout("tardó demasiado"),
        RespuestaFalsa(200),
    ])
    assert evolve.call_gemini("prompt") == "RESPUESTA OK"
    assert llamadas["n"] == 2


def test_recovers_after_a_connection_drop(monkeypatch, sin_esperas):
    import requests
    responder(monkeypatch, [
        requests.ConnectionError("se cortó la red"),
        RespuestaFalsa(200),
    ])
    assert evolve.call_gemini("prompt") == "RESPUESTA OK"


def test_network_errors_also_have_a_limit(monkeypatch, sin_esperas):
    import requests
    llamadas = responder(monkeypatch, [requests.ConnectionError("sin red")])
    assert evolve.call_gemini("prompt") is None
    assert llamadas["n"] == evolve.MAX_RETRIES_ON_SERVER_ERROR + 1


def test_rate_limit_is_retried(monkeypatch, sin_esperas):
    llamadas = responder(monkeypatch, [
        RespuestaFalsa(429, headers={"Retry-After": "5"}),
        RespuestaFalsa(200),
    ])
    assert evolve.call_gemini("prompt") == "RESPUESTA OK"
    assert llamadas["n"] == 2


# --------------------------------------------------------------------------
# Lo que NO se arregla reintentando
# --------------------------------------------------------------------------

def test_daily_quota_exhausted_stops_immediately(monkeypatch, sin_esperas):
    """Si la cuota diaria se agotó, insistir solo quema tiempo de la corrida."""
    espera_larga = str(evolve.QUOTA_EXHAUSTED_WAIT_THRESHOLD + 60)
    llamadas = responder(monkeypatch, [
        RespuestaFalsa(429, headers={"Retry-After": espera_larga}),
    ])
    assert evolve.call_gemini("prompt") == "QUOTA_EXHAUSTED"
    assert llamadas["n"] == 1, "no debe reintentar una cuota diaria agotada"


def test_client_errors_are_not_retried(monkeypatch, sin_esperas):
    """Un 400 es culpa del pedido: reintentarlo da el mismo 400."""
    llamadas = responder(monkeypatch, [RespuestaFalsa(400)])
    assert evolve.call_gemini("prompt") is None
    assert llamadas["n"] == 1


def test_forbidden_is_not_retried(monkeypatch, sin_esperas):
    llamadas = responder(monkeypatch, [RespuestaFalsa(403)])
    assert evolve.call_gemini("prompt") is None
    assert llamadas["n"] == 1


def test_missing_api_key_fails_without_calling_the_network(monkeypatch, sin_esperas):
    monkeypatch.setattr(evolve, "GEMINI_API_KEY", "")
    llamadas = responder(monkeypatch, [RespuestaFalsa(200)])
    assert evolve.call_gemini("prompt") is None
    assert llamadas["n"] == 0


def test_empty_response_is_not_accepted(monkeypatch, sin_esperas):
    """Una respuesta sin texto no sirve como mejora."""
    class SinTexto(RespuestaFalsa):
        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "   "}]}}]}

    responder(monkeypatch, [SinTexto(200)])
    assert evolve.call_gemini("prompt") is None


def test_malformed_response_does_not_crash_the_loop(monkeypatch, sin_esperas):
    class Rara(RespuestaFalsa):
        def json(self):
            return {"algo": "inesperado"}

    responder(monkeypatch, [Rara(200)])
    assert evolve.call_gemini("prompt") is None


def test_retry_totals_stay_bounded(monkeypatch, sin_esperas):
    """Ninguna combinación de fallas puede colgar la corrida indefinidamente."""
    llamadas = responder(monkeypatch, [
        RespuestaFalsa(503),
        RespuestaFalsa(429, headers={"Retry-After": "5"}),
        RespuestaFalsa(503),
        RespuestaFalsa(429, headers={"Retry-After": "5"}),
        RespuestaFalsa(503),
        RespuestaFalsa(503),
        RespuestaFalsa(503),
    ])
    assert evolve.call_gemini("prompt") is None
    tope = evolve.MAX_RETRIES_ON_RATE_LIMIT + evolve.MAX_RETRIES_ON_SERVER_ERROR + 2
    assert llamadas["n"] <= tope
