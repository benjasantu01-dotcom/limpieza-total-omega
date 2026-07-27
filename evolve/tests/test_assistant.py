"""
test_assistant.py — ajustes y asistente, con la privacidad como contrato.

El test más importante de este archivo es el de privacidad: verifica que el
texto que sale del equipo no contenga rutas ni nombres de archivos. Está
escrito de forma genérica a propósito, así el día que alguien agregue un campo
nuevo al contexto del asistente y ese campo traiga una ruta, el test falla
antes de que el dato se filtre.

También se fija que el asistente NO pueda ejecutar acciones: no debe existir
ninguna llamada a borrar, mover ni aislar en todo el módulo. Un asistente que
aprieta botones es un asistente que puede equivocarse sobre archivos reales.
"""

import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import assistant  # noqa: E402
import settings  # noqa: E402

APP_DIR = Path(__file__).resolve().parents[2] / "app"


# ==========================================================================
# settings
# ==========================================================================

def test_defaults_are_complete_and_typed():
    for clave in ("tema", "acento", "confirmar_siempre", "asistente_activado",
                  "asistente_clave_api", "duplicados_tamano_minimo_kb"):
        assert clave in settings.DEFAULTS


def test_assistant_is_off_by_default():
    """Encenderlo manda datos a Google: lo decide el usuario, no el default."""
    assert settings.DEFAULTS["asistente_activado"] is False
    assert settings.DEFAULTS["asistente_clave_api"] == ""


def test_load_without_file_returns_defaults(tmp_path):
    assert settings.load(tmp_path) == settings.DEFAULTS


def test_save_then_load_keeps_the_values(tmp_path):
    settings.save({**settings.DEFAULTS, "tema": "claro", "top_archivos": 42}, tmp_path)
    recargado = settings.load(tmp_path)
    assert recargado["tema"] == "claro"
    assert recargado["top_archivos"] == 42


def test_save_creates_the_folder(tmp_path):
    destino = tmp_path / "sub" / "carpeta"
    assert settings.save(settings.DEFAULTS, destino) is not None
    assert (destino / settings.SETTINGS_FILE).is_file()


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    (tmp_path / settings.SETTINGS_FILE).write_text("{esto no es json", encoding="utf-8")
    assert settings.load(tmp_path) == settings.DEFAULTS


def test_unknown_keys_are_discarded(tmp_path):
    settings.save({"clave_inventada": "x", "tema": "claro"}, tmp_path)
    cargado = settings.load(tmp_path)
    assert "clave_inventada" not in cargado
    assert cargado["tema"] == "claro"


def test_invalid_theme_falls_back(tmp_path):
    assert settings.validate({"tema": "fucsia"})["tema"] == settings.DEFAULTS["tema"]
    assert settings.validate({"acento": "invisible"})["acento"] == settings.DEFAULTS["acento"]


def test_valid_themes_and_accents_are_accepted():
    for tema in settings.VALID_THEMES:
        assert settings.validate({"tema": tema})["tema"] == tema
    for acento in settings.VALID_ACCENTS:
        assert settings.validate({"acento": acento})["acento"] == acento


def test_numbers_are_clamped_to_their_range():
    assert settings.validate({"top_archivos": 99999})["top_archivos"] == 500
    assert settings.validate({"top_archivos": -5})["top_archivos"] == 1
    assert settings.validate({"duplicados_tamano_minimo_kb": -1})["duplicados_tamano_minimo_kb"] == 0


def test_text_numbers_are_accepted():
    # La interfaz entrega texto desde los campos de entrada.
    assert settings.validate({"top_archivos": "25"})["top_archivos"] == 25


def test_garbage_numbers_fall_back():
    assert settings.validate({"top_archivos": "muchos"})["top_archivos"] == \
        settings.DEFAULTS["top_archivos"]


def test_booleans_accept_the_usual_strings():
    assert settings.validate({"asistente_activado": "true"})["asistente_activado"] is True
    assert settings.validate({"asistente_activado": "sí"})["asistente_activado"] is True
    assert settings.validate({"asistente_activado": "no"})["asistente_activado"] is False


def test_validate_survives_anything():
    for basura in (None, "texto", 42, [], [1, 2]):
        assert settings.validate(basura) == settings.DEFAULTS


def test_a_protected_folder_is_never_remembered(tmp_path):
    """Una preferencia mal puesta no puede terminar en un borrado en el sistema."""
    peligrosa = str(tmp_path / "Windows" / "System32")
    resultado = settings.validate({"ultima_carpeta": peligrosa})
    assert resultado["ultima_carpeta"] == ""


def test_a_normal_folder_is_remembered(tmp_path):
    segura = str(tmp_path / "Descargas")
    assert settings.validate({"ultima_carpeta": segura})["ultima_carpeta"] == segura


def test_update_applies_partial_changes(tmp_path):
    settings.save({**settings.DEFAULTS, "tema": "claro"}, tmp_path)
    resultado = settings.update({"top_procesos": 7}, tmp_path)
    assert resultado["tema"] == "claro", "no debe perder lo que no se tocó"
    assert resultado["top_procesos"] == 7


def test_reset_returns_to_factory(tmp_path):
    settings.save({**settings.DEFAULTS, "tema": "claro", "top_archivos": 99}, tmp_path)
    assert settings.reset(tmp_path) == settings.DEFAULTS
    assert settings.load(tmp_path) == settings.DEFAULTS


def test_get_reads_a_single_value(tmp_path):
    settings.save({**settings.DEFAULTS, "top_procesos": 33}, tmp_path)
    assert settings.get("top_procesos", tmp_path) == 33
    assert settings.get("clave_que_no_existe", tmp_path) is None


def test_env_var_wins_over_the_config_file(tmp_path, monkeypatch):
    """La variable de entorno es el camino recomendado, así que tiene prioridad."""
    settings.save({**settings.DEFAULTS, "asistente_clave_api": "del-archivo"}, tmp_path)
    monkeypatch.setenv(settings.API_KEY_ENV_VAR, "del-entorno")
    assert settings.assistant_api_key(tmp_path) == "del-entorno"


def test_config_key_is_used_when_there_is_no_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv(settings.API_KEY_ENV_VAR, raising=False)
    settings.save({**settings.DEFAULTS, "asistente_clave_api": "del-archivo"}, tmp_path)
    assert settings.assistant_api_key(tmp_path) == "del-archivo"


def test_enabled_requires_both_the_switch_and_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv(settings.API_KEY_ENV_VAR, raising=False)

    settings.save({**settings.DEFAULTS, "asistente_activado": True}, tmp_path)
    assert settings.assistant_enabled(tmp_path) is False, "activado sin clave no alcanza"

    settings.save({**settings.DEFAULTS, "asistente_clave_api": "k"}, tmp_path)
    assert settings.assistant_enabled(tmp_path) is False, "una clave no autoriza por sí sola"

    settings.save({**settings.DEFAULTS, "asistente_activado": True,
                   "asistente_clave_api": "k"}, tmp_path)
    assert settings.assistant_enabled(tmp_path) is True


def test_describe_never_prints_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv(settings.API_KEY_ENV_VAR, raising=False)
    settings.save({**settings.DEFAULTS, "asistente_clave_api": "SECRETO-123"}, tmp_path)
    texto = "\n".join(settings.describe(tmp_path))
    assert "SECRETO-123" not in texto, "la clave nunca debe mostrarse en pantalla"
    assert "archivo de configuración" in texto


# ==========================================================================
# assistant: privacidad
# ==========================================================================

def _contexto_lleno():
    return assistant.SystemContext(
        score=61, grade="C", junk_mb=2400.0, suspicious_count=3,
        suspicious_warnings=1, memory_available_percent=11.0, memory_total_gb=16.0,
        disk_free_percent=6.0, duplicate_mb=900.0, startup_count=19,
        quarantined_count=2, browser_cache_mb=430.0, analyzed=True,
    )


def test_context_only_holds_numbers_and_short_strings():
    """Ningún campo del contexto puede ser una ruta o una lista de archivos."""
    for nombre, valor in vars(_contexto_lleno()).items():
        assert isinstance(valor, (int, float, bool, str)), \
            f"{nombre} no es un dato agregado"
        if isinstance(valor, str):
            assert len(valor) <= 8, f"{nombre} es un texto largo, podría traer una ruta"


def test_the_text_sent_out_contains_no_paths():
    """Lo que viaja a la API no puede tener separadores de ruta ni extensiones."""
    texto = assistant.context_as_text(_contexto_lleno())
    for marca in ("\\", "/", ".exe", ".dll", ".tmp", "C:", "Users", "AppData"):
        assert marca not in texto, f"el texto enviado contiene '{marca}'"


def test_build_context_ignores_non_numeric_extras():
    """Un extra con una ruta no puede colarse en el contexto."""
    contexto = assistant.build_context(
        ruta_secreta="C:/Users/benja/Documentos/secreto.txt",
        memory_total_gb=8.0,
    )
    assert not hasattr(contexto, "ruta_secreta")
    assert contexto.memory_total_gb == 8.0


def test_build_context_reads_fields_one_by_one():
    """Copia campo por campo, no el objeto entero, para no arrastrar datos."""
    class MetricasConRuta:
        junk_mb = 100.0
        suspicious_count = 2
        suspicious_warnings = 0
        memory_available_percent = 40.0
        disk_free_percent = 50.0
        duplicate_mb = 0.0
        startup_count = 5
        quarantined_count = 0
        archivo_secreto = "C:/Users/benja/clave.txt"

    contexto = assistant.build_context(metrics=MetricasConRuta())
    assert contexto.junk_mb == 100.0
    assert not hasattr(contexto, "archivo_secreto")
    assert "clave.txt" not in assistant.context_as_text(contexto)


def test_privacy_notice_explains_what_is_sent():
    for palabra in ("agregados", "Nunca", "rutas"):
        assert palabra in assistant.PRIVACY_NOTICE


def test_sensitive_keys_list_is_documented():
    assert len(assistant.SENSITIVE_KEYS_NEVER_SENT) >= 5
    assert any("ruta" in k for k in assistant.SENSITIVE_KEYS_NEVER_SENT)


# ==========================================================================
# assistant: el asistente no ejecuta acciones
# ==========================================================================

def test_assistant_module_cannot_delete_or_move_anything():
    """No debe existir ninguna llamada destructiva en todo el módulo."""
    arbol = ast.parse((APP_DIR / "assistant.py").read_text(encoding="utf-8"))
    prohibidos = {"unlink", "rmdir", "rmtree", "move", "remove", "rename",
                  "replace", "quarantine_file", "delete_reviewed", "purge_all",
                  "purge_item", "write_text", "write_bytes"}
    usados = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            if isinstance(nodo.func, ast.Name):
                usados.add(nodo.func.id)
            elif isinstance(nodo.func, ast.Attribute):
                usados.add(nodo.func.attr)
    assert not (usados & prohibidos), \
        f"el asistente solo aconseja, no ejecuta: {usados & prohibidos}"


def test_assistant_does_not_import_the_destructive_modules():
    arbol = ast.parse((APP_DIR / "assistant.py").read_text(encoding="utf-8"))
    importados = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            importados.update(a.name.split(".")[0] for a in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            importados.add(nodo.module.split(".")[0])
    assert "quarantine" not in importados
    assert "organizer" not in importados


# ==========================================================================
# assistant: el motor local tiene que servir solo
# ==========================================================================

def test_without_analysis_it_asks_you_to_run_one():
    respuesta = assistant.local_answer("¿qué hago?", assistant.SystemContext())
    assert "Analizar el sistema" in respuesta.text
    assert respuesta.source == "local"


def test_answers_are_never_empty():
    contexto = _contexto_lleno()
    for pregunta in assistant.SUGGESTED_QUESTIONS:
        respuesta = assistant.local_answer(pregunta, contexto)
        assert respuesta.text.strip(), f"sin respuesta para: {pregunta}"


def test_garbage_questions_still_get_an_answer():
    contexto = _contexto_lleno()
    for pregunta in ("", "   ", "asdkjhasd", None):
        assert assistant.local_answer(pregunta, contexto).text.strip()


def test_ram_question_debunks_the_ram_cleaner_myth():
    """La respuesta honesta sobre RAM es lo que diferencia esto de un limpiador falso."""
    respuesta = assistant.local_answer("¿por qué está lenta la ram?", _contexto_lleno())
    texto = respuesta.text.lower()
    assert "liberador de ram" in texto or "más lenta" in texto


def test_low_disk_is_reported_as_the_top_priority():
    contexto = _contexto_lleno()  # 6% libre
    respuesta = assistant.local_answer("¿qué arreglo primero?", contexto)
    assert "disco" in respuesta.text.lower()


def test_space_question_adds_up_what_can_be_recovered():
    respuesta = assistant.local_answer("¿cuánto espacio puedo recuperar?", _contexto_lleno())
    # 2400 basura + 900 duplicados + 430 caché
    assert "3730" in respuesta.text.replace(".", "")


def test_security_question_without_findings_is_reassuring():
    contexto = _contexto_lleno()
    contexto.suspicious_count = 0
    contexto.suspicious_warnings = 0
    respuesta = assistant.local_answer("¿es seguro?", contexto)
    assert "No hay archivos sospechosos" in respuesta.text


def test_security_question_with_findings_explains_they_are_signals():
    respuesta = assistant.local_answer("¿tengo virus?", _contexto_lleno())
    assert "señales" in respuesta.text.lower()


def test_a_healthy_system_gets_a_calm_answer():
    sano = assistant.SystemContext(
        score=98, grade="A", junk_mb=10, suspicious_count=0,
        memory_available_percent=55, disk_free_percent=60,
        startup_count=4, analyzed=True,
    )
    respuesta = assistant.local_answer("¿cómo estoy?", sano)
    assert "buen estado" in respuesta.text.lower()


def test_local_answer_always_says_it_did_not_send_anything():
    respuesta = assistant.local_answer("¿qué hago?", _contexto_lleno())
    assert "sin conexión" in respuesta.notice
    assert respuesta.is_online is False


def test_explain_area_covers_every_health_area():
    for area in ("basura", "seguridad", "memoria", "disco", "duplicados", "inicio"):
        assert len(assistant.explain_area(area)) > 40


def test_explain_area_on_unknown_input():
    assert "No tengo" in assistant.explain_area("inventada")
    assert "No tengo" in assistant.explain_area(None)


# ==========================================================================
# assistant: el motor en línea solo actúa si está autorizado
# ==========================================================================

def test_ask_stays_local_when_the_assistant_is_off(tmp_path, monkeypatch):
    """Sin autorización no puede haber ni una llamada de red."""
    monkeypatch.delenv(settings.API_KEY_ENV_VAR, raising=False)
    settings.save(settings.DEFAULTS, tmp_path)

    def prohibido(*a, **k):
        pytest.fail("se intentó salir a la red con el asistente desactivado")

    monkeypatch.setattr(assistant, "_call_gemini", prohibido)
    respuesta = assistant.ask("¿qué hago?", _contexto_lleno(), tmp_path)
    assert respuesta.source == "local"


def test_ask_uses_the_online_engine_when_authorized(tmp_path, monkeypatch):
    monkeypatch.setenv(settings.API_KEY_ENV_VAR, "clave")
    settings.save({**settings.DEFAULTS, "asistente_activado": True}, tmp_path)
    monkeypatch.setattr(assistant, "_call_gemini",
                        lambda *a, **k: "Respuesta del modelo")

    respuesta = assistant.ask("¿qué hago?", _contexto_lleno(), tmp_path)
    assert respuesta.source == "gemini"
    assert respuesta.text == "Respuesta del modelo"
    assert "agregados" in respuesta.notice


def test_online_failure_falls_back_to_local(tmp_path, monkeypatch):
    """Un problema de red no puede dejar al asistente sin contestar."""
    monkeypatch.setenv(settings.API_KEY_ENV_VAR, "clave")
    settings.save({**settings.DEFAULTS, "asistente_activado": True}, tmp_path)
    monkeypatch.setattr(assistant, "_call_gemini", lambda *a, **k: None)

    respuesta = assistant.ask("¿qué hago?", _contexto_lleno(), tmp_path)
    assert respuesta.source == "local"
    assert respuesta.text.strip()
    assert "motor local" in respuesta.notice


def test_metrics_are_withheld_when_the_user_says_no(tmp_path, monkeypatch):
    """Se puede usar el asistente sin mandar ni una métrica."""
    monkeypatch.setenv(settings.API_KEY_ENV_VAR, "clave")
    settings.save({**settings.DEFAULTS, "asistente_activado": True,
                   "asistente_enviar_metricas": False}, tmp_path)

    enviado = {}

    def espia(question, context_text, api_key, model):
        enviado["texto"] = context_text
        return "ok"

    monkeypatch.setattr(assistant, "_call_gemini", espia)
    assistant.ask("¿qué hago?", _contexto_lleno(), tmp_path)
    assert "2400" not in enviado["texto"]
    assert "no autorizó" in enviado["texto"]


def test_available_reflects_the_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv(settings.API_KEY_ENV_VAR, raising=False)
    settings.save(settings.DEFAULTS, tmp_path)
    assert assistant.available(tmp_path) is False

    monkeypatch.setenv(settings.API_KEY_ENV_VAR, "clave")
    settings.save({**settings.DEFAULTS, "asistente_activado": True}, tmp_path)
    assert assistant.available(tmp_path) is True


def test_system_prompt_forbids_claiming_actions():
    """El prompt tiene que prohibir que el modelo diga que hizo algo."""
    texto = assistant.SYSTEM_PROMPT.lower()
    assert "solo aconsej" in texto or "nunca digas que borraste" in texto
    assert "ram" in texto, "el prompt debe cubrir el mito del liberador de RAM"
