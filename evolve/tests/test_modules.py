"""
test_modules.py — tests de los módulos de análisis y presentación.

Todos los módulos que se prueban acá son de SOLO LECTURA (memoria, disco,
duplicados, navegadores, arranque) o puros (healthscore, reporting,
branding). Por eso los tests pueden ser exhaustivos sin riesgo.

Los módulos están escritos para poder testearse en Linux: las funciones que
interpretan datos del sistema reciben el texto crudo por parámetro, y las
que recorren carpetas aceptan la base por parámetro. Así el bucle autónomo
tiene cobertura real sobre lo que escribe, en vez de tests que se saltean
por no estar en Windows.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import branding  # noqa: E402
import browser  # noqa: E402
import diskreport  # noqa: E402
import duplicates  # noqa: E402
import healthscore  # noqa: E402
import logrotate  # noqa: E402
import memory  # noqa: E402
import reporting  # noqa: E402
import startup  # noqa: E402


# ==========================================================================
# branding
# ==========================================================================

class _CanvasFalso:
    """Canvas de mentira: registra las llamadas en vez de dibujar.

    Permite testear todo el dibujo (logo, degradado, medidor circular) sin
    pantalla, que es justo lo que no hay en GitHub Actions.
    """

    def __init__(self):
        self.llamadas = []
        self.colores = []

    def _registrar(self, tipo, kwargs):
        self.llamadas.append(tipo)
        for clave in ("fill", "outline"):
            valor = kwargs.get(clave)
            if isinstance(valor, str) and valor.startswith("#"):
                self.colores.append(valor)

    def create_polygon(self, *a, **k):
        self._registrar("polygon", k)

    def create_line(self, *a, **k):
        self._registrar("line", k)

    def create_text(self, *a, **k):
        self._registrar("text", k)

    def create_oval(self, *a, **k):
        self._registrar("oval", k)

    def create_rectangle(self, *a, **k):
        self._registrar("rectangle", k)

    def create_arc(self, *a, **k):
        self._registrar("arc", k)


def test_branding_has_name_and_version():
    assert branding.APP_NAME
    assert branding.APP_VERSION.count(".") == 2
    assert branding.APP_NAME in branding.app_title()


def test_logo_svg_is_valid_svg_markup():
    svg = branding.logo_svg()
    assert svg.strip().startswith("<svg")
    assert svg.strip().endswith("</svg>")
    assert "viewBox" in svg


def test_logo_svg_respects_requested_size():
    assert 'width="256"' in branding.logo_svg(256)


def test_save_logo_svg_writes_the_file(tmp_path):
    destino = branding.save_logo_svg(tmp_path / "iconos" / "logo.svg")
    assert destino.is_file()
    assert destino.read_text(encoding="utf-8").startswith("<svg")


def test_draw_logo_paints_on_the_canvas_without_a_display():
    canvas = _CanvasFalso()
    branding.draw_logo(canvas, size=48)
    assert "polygon" in canvas.llamadas
    assert "text" in canvas.llamadas


def test_palette_has_every_key_the_ui_uses():
    for clave in ("background", "surface", "surface_alt", "accent", "accent_hover",
                  "danger", "danger_hover", "text", "text_muted", "border"):
        assert clave in branding.PALETTE


def test_colors_are_valid_hex():
    for nombre, valor in branding.PALETTE.items():
        assert valor.startswith("#") and len(valor) == 7, f"{nombre} no es hex válido"


def test_unknown_color_falls_back_instead_of_crashing():
    assert branding.color("no-existe") == "#808080"
    assert branding.font_size("no-existe") == branding.FONT_SIZES["body"]


def test_severity_helpers_cover_all_levels():
    for nivel in ("ok", "info", "warning", "danger"):
        assert branding.severity_color(nivel).startswith("#")
        assert branding.severity_label(nivel)


def test_grade_color_covers_a_to_f():
    for nota in ("A", "B", "C", "D", "F"):
        assert branding.grade_color(nota).startswith("#")


def test_logo_ascii_is_not_empty():
    assert "Omega" in branding.logo_ascii()


# -- diseño visual: iconos, barras, degradados y medidor ---------------------

def test_every_tab_has_an_icon():
    for seccion in ("Salud", "Limpieza", "Seguridad", "Cuarentena", "Memoria",
                    "Disco", "Duplicados", "Navegadores", "Inicio", "Informe"):
        assert seccion in branding.ICONS, f"la pestaña {seccion} no tiene ícono"
        assert branding.icon(seccion) != "\u2022"


def test_unknown_section_gets_a_neutral_bullet():
    assert branding.icon("No existe") == "\u2022"
    assert branding.icon(None) == "\u2022"


def test_tab_label_puts_the_icon_before_the_name():
    etiqueta = branding.tab_label("Salud")
    assert etiqueta.startswith(branding.ICONS["Salud"])
    assert "Salud" in etiqueta


def test_severity_icons_are_distinct():
    marcas = {branding.severity_icon(n) for n in ("ok", "info", "warning", "danger")}
    assert len(marcas) == 4


def test_score_color_changes_with_the_score():
    assert branding.score_color(95) == branding.PALETTE["success"]
    assert branding.score_color(85) == branding.PALETTE["info"]
    assert branding.score_color(70) == branding.PALETTE["warning"]
    assert branding.score_color(10) == branding.PALETTE["danger"]


def test_score_color_survives_garbage():
    assert branding.score_color(None) == branding.PALETTE["text_muted"]
    assert branding.score_color("mucho") == branding.PALETTE["text_muted"]


def test_text_bar_length_is_exact():
    assert len(branding.bar(50, width=20)) == 20
    assert len(branding.bar(0, width=8)) == 8


def test_text_bar_reflects_the_percentage():
    assert branding.bar(0, 10).count("\u2588") == 0
    assert branding.bar(100, 10).count("\u2588") == 10
    assert branding.bar(50, 10).count("\u2588") == 5


def test_text_bar_clamps_and_tolerates_garbage():
    assert branding.bar(-40, 10).count("\u2588") == 0
    assert branding.bar(9999, 10).count("\u2588") == 10
    assert len(branding.bar(None, 10)) == 10
    assert len(branding.bar("hola", 10)) == 10


def test_blend_returns_the_endpoints_and_the_middle():
    assert branding.blend("#000000", "#ffffff", 0.0) == "#000000"
    assert branding.blend("#000000", "#ffffff", 1.0) == "#ffffff"
    medio = branding.blend("#000000", "#ffffff", 0.5)
    assert medio in ("#7f7f7f", "#808080")


def test_blend_clamps_out_of_range_ratios():
    assert branding.blend("#000000", "#ffffff", -5) == "#000000"
    assert branding.blend("#000000", "#ffffff", 99) == "#ffffff"


def test_blend_on_invalid_color_does_not_crash():
    assert branding.blend("no-es-color", "#ffffff", 0.5).startswith("#")


def test_gradient_produces_the_requested_amount_of_colors():
    for cantidad in (1, 2, 7, 300):
        colores = branding.gradient_colors(cantidad)
        assert len(colores) == cantidad
        assert all(c.startswith("#") and len(c) == 7 for c in colores)


def test_gradient_starts_and_ends_on_its_stops():
    colores = branding.gradient_colors(50)
    assert colores[0].lower() == branding.GRADIENT_STOPS[0].lower()
    assert colores[-1].lower() == branding.GRADIENT_STOPS[-1].lower()


def test_gradient_actually_changes_color():
    colores = branding.gradient_colors(40)
    assert len(set(colores)) > 10, "un degradado con un solo tono no es un degradado"


def test_gradient_bar_paints_one_line_per_pixel():
    canvas = _CanvasFalso()
    branding.draw_gradient_bar(canvas, width=60)
    assert canvas.llamadas.count("line") == 60


def test_gradient_bar_ignores_invalid_sizes():
    canvas = _CanvasFalso()
    branding.draw_gradient_bar(canvas, width="ancho")
    branding.draw_gradient_bar(None, width=10)
    assert canvas.llamadas == []


def test_ring_draws_track_and_progress():
    canvas = _CanvasFalso()
    branding.draw_ring(canvas, 75, size=120)
    assert canvas.llamadas.count("arc") == 2, "hacen falta pista y avance"


def test_ring_at_zero_draws_only_the_track():
    canvas = _CanvasFalso()
    branding.draw_ring(canvas, 0, size=120)
    assert canvas.llamadas.count("arc") == 1


def test_ring_uses_the_score_color():
    canvas = _CanvasFalso()
    branding.draw_ring(canvas, 95, size=120)
    assert branding.PALETTE["success"] in canvas.colores


def test_ring_ignores_garbage_percent_and_missing_canvas():
    canvas = _CanvasFalso()
    branding.draw_ring(canvas, "mucho", size=120)
    branding.draw_ring(None, 50)
    assert canvas.llamadas == [], "un porcentaje inválido no debe dibujar nada"


def test_ring_clamps_absurd_sizes_instead_of_failing():
    # Un tamaño negativo se lleva al mínimo dibujable: es preferible un anillo
    # chico a una pestaña vacía sin explicación.
    canvas = _CanvasFalso()
    branding.draw_ring(canvas, 50, size=-10)
    assert canvas.llamadas.count("arc") == 2


def test_logo_draws_a_gradient_and_a_halo():
    canvas = _CanvasFalso()
    branding.draw_logo(canvas, size=72)
    assert "oval" in canvas.llamadas, "falta el halo detrás del escudo"
    assert "rectangle" in canvas.llamadas, "falta el degradado del escudo"
    assert "text" in canvas.llamadas


def test_palette_offers_more_than_one_accent():
    # Un solo acento es lo que hacía que la interfaz se viera apagada.
    for clave in ("accent", "accent2", "accent3", "success", "info", "warning"):
        assert clave in branding.PALETTE


# ==========================================================================
# memory
# ==========================================================================

_MEMINFO = """MemTotal:       16384000 kB
MemFree:         2048000 kB
MemAvailable:    8192000 kB
Buffers:          128000 kB
Cached:          4096000 kB
"""


def test_format_bytes_scales_units():
    assert memory.format_bytes(512) == "512 B"
    assert memory.format_bytes(1024) == "1.0 KB"
    assert memory.format_bytes(1024 ** 2) == "1.0 MB"
    assert memory.format_bytes(1024 ** 3) == "1.0 GB"


def test_format_bytes_handles_garbage_input():
    assert memory.format_bytes("no soy un número") == "0 B"
    assert memory.format_bytes(-5) == "0 B"


def test_parse_meminfo_reads_total_and_available():
    snap = memory.parse_linux_meminfo(_MEMINFO)
    assert snap.total == 16384000 * 1024
    assert snap.available == 8192000 * 1024
    assert snap.cached == 4096000 * 1024


def test_meminfo_prefers_available_over_free():
    # MemAvailable es la cifra correcta; MemFree subestima porque ignora caché.
    snap = memory.parse_linux_meminfo(_MEMINFO)
    assert snap.available != 2048000 * 1024


def test_meminfo_falls_back_to_free_when_available_is_missing():
    snap = memory.parse_linux_meminfo("MemTotal: 1000 kB\nMemFree: 400 kB\n")
    assert snap.available == 400 * 1024


def test_snapshot_computes_used_and_percentages():
    snap = memory.MemorySnapshot(total=1000, available=250)
    assert snap.used == 750
    assert snap.used_percent == 75.0
    assert snap.available_percent == 25.0


def test_snapshot_with_zero_total_does_not_divide_by_zero():
    snap = memory.MemorySnapshot(total=0, available=0)
    assert snap.used_percent == 0.0
    assert snap.available_percent == 0.0


def test_parse_process_csv_sorts_by_consumption():
    csv = (
        '"Name","Id","WorkingSet"\n'
        '"chico","10","1048576"\n'
        '"grande","11","104857600"\n'
        '"medio","12","10485760"\n'
    )
    procesos = memory.parse_windows_process_csv(csv)
    assert [p.name for p in procesos] == ["grande", "medio", "chico"]
    assert procesos[0].pid == 11


def test_parse_process_csv_skips_broken_lines():
    csv = '"Name","Id","WorkingSet"\n"ok","1","1024"\nlinea basura\n"malo","x","y"\n'
    procesos = memory.parse_windows_process_csv(csv)
    assert len(procesos) == 1
    assert procesos[0].name == "ok"


def test_parse_process_csv_on_empty_input():
    assert memory.parse_windows_process_csv("") == []


def test_process_working_set_in_mb():
    assert memory.ProcessMemory("x", 1, 1024 * 1024 * 5).working_set_mb == 5.0


def test_pressure_level_thresholds():
    assert memory.pressure_level(memory.MemorySnapshot(100, 50)) == "ok"
    assert memory.pressure_level(memory.MemorySnapshot(100, 25)) == "info"
    assert memory.pressure_level(memory.MemorySnapshot(100, 15)) == "warning"
    assert memory.pressure_level(memory.MemorySnapshot(100, 5)) == "danger"


def test_pressure_level_without_data_is_informative():
    assert memory.pressure_level(memory.MemorySnapshot(0, 0)) == "info"


def test_diagnose_explains_that_free_ram_is_not_the_goal():
    lineas = memory.diagnose(memory.MemorySnapshot(total=1000, available=500))
    texto = " ".join(lineas).lower()
    assert "memoria total" in texto
    # El mensaje honesto tiene que estar: es la diferencia con un limpiador falso.
    assert "liberar" in texto or "caché" in texto


def test_diagnose_on_critical_memory_suggests_closing_processes():
    lineas = memory.diagnose(memory.MemorySnapshot(total=1000, available=20))
    assert "crítico" in " ".join(lineas).lower()


def test_diagnose_without_data_does_not_crash():
    assert memory.diagnose(memory.MemorySnapshot(0, 0))


def test_diagnose_lists_top_processes():
    procesos = [memory.ProcessMemory("pesado", 99, 1024 * 1024 * 300)]
    texto = " ".join(memory.diagnose(memory.MemorySnapshot(1000, 100), procesos))
    assert "pesado" in texto


def test_trim_warning_is_honest_about_the_tradeoff():
    assert "NO acelera" in memory.TRIM_WARNING


def test_read_snapshot_never_crashes():
    snap = memory.read_snapshot()
    assert snap.total >= 0 and snap.available >= 0


def test_top_processes_returns_a_list():
    assert isinstance(memory.top_memory_processes(limit=3), list)


def test_trim_on_impossible_pid_fails_safely():
    ok, mensaje = memory.trim_working_set(999999)
    assert ok is False
    assert isinstance(mensaje, str) and mensaje


# ==========================================================================
# duplicates
# ==========================================================================

def test_finds_identical_files(tmp_path):
    (tmp_path / "a.txt").write_text("mismo contenido" * 100)
    (tmp_path / "b.txt").write_text("mismo contenido" * 100)
    grupos = duplicates.find_duplicates([tmp_path], min_size=1)
    assert len(grupos) == 1
    assert grupos[0].count == 2


def test_ignores_files_with_different_content(tmp_path):
    (tmp_path / "a.txt").write_text("uno" * 100)
    (tmp_path / "b.txt").write_text("dos" * 100)
    assert duplicates.find_duplicates([tmp_path], min_size=1) == []


def test_finds_duplicates_across_subfolders(tmp_path):
    for sub in ("x", "y"):
        carpeta = tmp_path / sub
        carpeta.mkdir()
        (carpeta / "igual.dat").write_bytes(b"A" * 5000)
    grupos = duplicates.find_duplicates([tmp_path], min_size=1)
    assert len(grupos) == 1
    assert grupos[0].count == 2


def test_min_size_filters_out_tiny_files(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    assert duplicates.find_duplicates([tmp_path], min_size=1024) == []


def test_never_scans_system_folders(tmp_path):
    sistema = tmp_path / "Windows"
    sistema.mkdir()
    (sistema / "a.dat").write_bytes(b"Z" * 3000)
    (sistema / "b.dat").write_bytes(b"Z" * 3000)
    assert duplicates.find_duplicates([tmp_path], min_size=1) == []


def test_wasted_bytes_counts_all_copies_but_one():
    grupo = duplicates.DuplicateGroup("abc", 1000, [Path("a"), Path("b"), Path("c")])
    assert grupo.wasted_bytes == 2000
    assert grupo.count == 3


def test_reclaimable_bytes_sums_every_group():
    grupos = [
        duplicates.DuplicateGroup("a", 100, [Path("1"), Path("2")]),
        duplicates.DuplicateGroup("b", 50, [Path("3"), Path("4"), Path("5")]),
    ]
    assert duplicates.reclaimable_bytes(grupos) == 100 + 100


def test_group_by_size_separates_by_exact_size(tmp_path):
    (tmp_path / "chico.txt").write_text("12345")
    (tmp_path / "otro-chico.txt").write_text("abcde")
    (tmp_path / "grande.txt").write_text("1234567890")
    grupos = duplicates.group_by_size(tmp_path.iterdir())
    assert sorted(len(v) for v in grupos.values()) == [1, 2]


def test_hash_of_identical_content_matches(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"contenido")
    b.write_bytes(b"contenido")
    assert duplicates.hash_file(a) == duplicates.hash_file(b)


def test_hash_of_missing_file_returns_none(tmp_path):
    assert duplicates.hash_file(tmp_path / "no-existe") is None
    assert duplicates.partial_hash(tmp_path / "no-existe") is None


def test_partial_hash_only_reads_the_beginning(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"X" * 100 + b"final-uno")
    b.write_bytes(b"X" * 100 + b"final-dos")
    assert duplicates.partial_hash(a, read_bytes=50) == duplicates.partial_hash(b, read_bytes=50)
    assert duplicates.hash_file(a) != duplicates.hash_file(b)


def test_suggest_keeper_prefers_the_oldest_copy(tmp_path):
    import os
    viejo, nuevo = tmp_path / "viejo.txt", tmp_path / "nuevo.txt"
    viejo.write_text("igual")
    nuevo.write_text("igual")
    os.utime(viejo, (1000000, 1000000))
    grupo = duplicates.DuplicateGroup("x", 5, [nuevo, viejo])
    assert duplicates.suggest_keeper(grupo) == viejo


def test_suggest_keeper_on_empty_group_returns_none():
    assert duplicates.suggest_keeper(duplicates.DuplicateGroup("x", 0, [])) is None


def test_format_group_marks_which_copy_to_keep(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("igual")
    b.write_text("igual")
    lineas = duplicates.format_group(duplicates.DuplicateGroup("x", 5, [a, b]))
    texto = "\n".join(lineas)
    assert "conservar" in texto and "duplicado" in texto


def test_missing_directory_does_not_crash(tmp_path):
    assert duplicates.find_duplicates([tmp_path / "no-existe"]) == []


# ==========================================================================
# diskreport
# ==========================================================================

def test_format_size_scales_units():
    assert diskreport.format_size(0) == "0 B"
    assert diskreport.format_size(1536) == "1.5 KB"
    assert diskreport.format_size(1024 ** 3) == "1.0 GB"


def test_format_size_handles_garbage():
    assert diskreport.format_size(None) == "0 B"


def test_drive_usage_reads_a_real_path(tmp_path):
    uso = diskreport.drive_usage(tmp_path)
    assert uso is not None
    assert uso.total > 0
    assert 0 <= uso.used_percent <= 100


def test_drive_usage_on_invalid_path_returns_none(tmp_path):
    assert diskreport.drive_usage(tmp_path / "no-existe-para-nada") is None


def test_almost_full_flag():
    assert diskreport.DriveUsage("X", total=1000, used=950, free=50).is_almost_full
    assert not diskreport.DriveUsage("X", total=1000, used=500, free=500).is_almost_full


def test_all_drives_usage_returns_a_list():
    assert isinstance(diskreport.all_drives_usage(), list)


def test_walk_files_finds_everything_recursively(tmp_path):
    (tmp_path / "raiz.txt").write_text("12345")
    sub = tmp_path / "sub" / "mas"
    sub.mkdir(parents=True)
    (sub / "hondo.txt").write_text("1234567890")
    encontrados = {p.name: s for p, s in diskreport.walk_files(tmp_path)}
    assert encontrados == {"raiz.txt": 5, "hondo.txt": 10}


def test_walk_files_skips_system_folders(tmp_path):
    (tmp_path / "normal.txt").write_text("x")
    sistema = tmp_path / "Program Files"
    sistema.mkdir()
    (sistema / "oculto.txt").write_text("y")
    nombres = {p.name for p, _ in diskreport.walk_files(tmp_path)}
    assert nombres == {"normal.txt"}


def test_largest_files_sorted_descending(tmp_path):
    (tmp_path / "chico").write_bytes(b"a" * 10)
    (tmp_path / "grande").write_bytes(b"a" * 1000)
    (tmp_path / "medio").write_bytes(b"a" * 100)
    top = diskreport.largest_files(tmp_path, limit=3)
    assert [e.path.name for e in top] == ["grande", "medio", "chico"]


def test_largest_files_respects_the_limit(tmp_path):
    for i in range(6):
        (tmp_path / f"f{i}").write_bytes(b"a" * (i + 1) * 10)
    assert len(diskreport.largest_files(tmp_path, limit=2)) == 2


def test_usage_by_extension_groups_and_counts(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"a" * 100)
    (tmp_path / "b.jpg").write_bytes(b"a" * 200)
    (tmp_path / "c.txt").write_bytes(b"a" * 50)
    uso = {u.extension: u for u in diskreport.usage_by_extension(tmp_path)}
    assert uso[".jpg"].size_bytes == 300
    assert uso[".jpg"].count == 2
    assert uso[".txt"].count == 1


def test_usage_by_extension_labels_files_without_extension(tmp_path):
    (tmp_path / "sinextension").write_bytes(b"a" * 10)
    assert any(u.extension == "(sin extensión)" for u in diskreport.usage_by_extension(tmp_path))


def test_largest_folders_ranks_subfolders(tmp_path):
    for nombre, tamano in (("chica", 10), ("grande", 5000), ("media", 500)):
        carpeta = tmp_path / nombre
        carpeta.mkdir()
        (carpeta / "archivo").write_bytes(b"a" * tamano)
    ranking = diskreport.largest_folders(tmp_path)
    assert [f.path.name for f in ranking] == ["grande", "media", "chica"]


def test_largest_folders_on_a_file_returns_empty(tmp_path):
    archivo = tmp_path / "x.txt"
    archivo.write_text("x")
    assert diskreport.largest_folders(archivo) == []


def test_total_size_counts_bytes_and_files(tmp_path):
    (tmp_path / "a").write_bytes(b"a" * 100)
    (tmp_path / "b").write_bytes(b"a" * 200)
    assert diskreport.total_size(tmp_path) == (300, 2)


def test_summarize_mentions_the_folder_and_totals(tmp_path):
    (tmp_path / "x.log").write_bytes(b"a" * 1000)
    texto = "\n".join(diskreport.summarize(tmp_path))
    assert str(tmp_path) in texto
    assert ".log" in texto


# ==========================================================================
# startup
# ==========================================================================

def test_parse_registry_csv_reads_entries():
    csv = '"Name","Value"\n"MiApp","C:\\\\Apps\\\\mi.exe --silent"\n"Otra","D:\\\\otra.exe"\n'
    entradas = startup.parse_registry_csv(csv, source="HKCU")
    assert [e.name for e in entradas] == ["MiApp", "Otra"]
    assert entradas[0].source == "HKCU"


def test_parse_registry_csv_skips_powershell_noise():
    csv = '"Name","Value"\n"PSPath","algo"\n"Real","C:\\\\r.exe"\n'
    assert [e.name for e in startup.parse_registry_csv(csv)] == ["Real"]


def test_parse_registry_csv_on_empty_input():
    assert startup.parse_registry_csv("") == []


def test_executable_extracted_from_quoted_command():
    entrada = startup.StartupEntry("X", '"C:\\Program Files\\App\\app.exe" /min', "reg")
    assert entrada.executable == "C:\\Program Files\\App\\app.exe"


def test_executable_extracted_from_unquoted_command():
    assert startup.StartupEntry("X", "/usr/bin/app --flag", "reg").executable == "/usr/bin/app"


def test_executable_on_empty_command():
    assert startup.StartupEntry("X", "", "reg").executable == ""


def test_entries_from_folders_reads_injected_folders(tmp_path):
    carpeta = tmp_path / "Inicio"
    carpeta.mkdir()
    (carpeta / "MiPrograma.lnk").write_text("x")
    (carpeta / "Otro.lnk").write_text("y")
    entradas = startup.entries_from_folders([carpeta])
    assert {e.name for e in entradas} == {"MiPrograma", "Otro"}
    assert all(e.source == "carpeta" for e in entradas)


def test_entries_from_folders_ignores_desktop_ini(tmp_path):
    carpeta = tmp_path / "Inicio"
    carpeta.mkdir()
    (carpeta / "desktop.ini").write_text("x")
    assert startup.entries_from_folders([carpeta]) == []


def test_entries_from_folders_on_missing_folder(tmp_path):
    assert startup.entries_from_folders([tmp_path / "no-existe"]) == []


def test_impact_scales_with_the_number_of_programs():
    def entradas(n):
        return [startup.StartupEntry(f"p{i}", "c", "carpeta") for i in range(n)]
    assert startup.estimate_impact(entradas(0)) == "ok"
    assert startup.estimate_impact(entradas(4)) == "ok"
    assert startup.estimate_impact(entradas(8)) == "info"
    assert startup.estimate_impact(entradas(14)) == "warning"
    assert startup.estimate_impact(entradas(25)) == "danger"


def test_summarize_tells_the_user_how_to_disable_safely():
    entradas = [startup.StartupEntry("App", "c:\\app.exe", "carpeta")]
    texto = "\n".join(startup.summarize(entradas))
    assert "Administrador de tareas" in texto
    assert "App" in texto


def test_summarize_with_no_entries():
    assert "0" in "\n".join(startup.summarize([]))


def test_how_to_disable_explains_why_the_app_does_not_touch_the_registry():
    assert "registro" in startup.HOW_TO_DISABLE.lower()


def test_registry_keys_cover_user_and_machine():
    juntas = " ".join(startup.REGISTRY_RUN_KEYS)
    assert "HKCU" in juntas and "HKLM" in juntas


def test_list_startup_entries_returns_a_list():
    assert isinstance(startup.list_startup_entries(), list)


# ==========================================================================
# browser
# ==========================================================================

def test_detect_profiles_finds_injected_cache_folders(tmp_path):
    cache = tmp_path / "Navegador" / "Default" / "Cache"
    cache.mkdir(parents=True)
    (cache / "dato.bin").write_bytes(b"a" * 500)

    encontrados = browser.detect_profiles(
        bases=[tmp_path],
        cache_paths={"Navegador Falso": r"Navegador\Default\Cache"},
    )
    assert len(encontrados) == 1
    assert encontrados[0].browser == "Navegador Falso"
    assert encontrados[0].size_bytes == 500


def test_detect_profiles_ignores_missing_folders(tmp_path):
    assert browser.detect_profiles(
        bases=[tmp_path], cache_paths={"X": r"no\existe\Cache"}
    ) == []


def test_detect_profiles_never_reports_user_data_folders(tmp_path):
    # Aunque se le pida explícitamente, no debe reportar carpetas de datos.
    peligrosa = tmp_path / "Perfil" / "Cookies"
    peligrosa.mkdir(parents=True)
    (peligrosa / "x").write_text("secreto")
    assert browser.detect_profiles(
        bases=[tmp_path], cache_paths={"Chrome": r"Perfil\Cookies"}
    ) == []


def test_never_touch_covers_credentials_and_bookmarks():
    for nombre in ("login data", "cookies", "bookmarks", "history"):
        assert nombre in browser.NEVER_TOUCH


def test_known_browsers_are_covered():
    juntos = " ".join(browser.BROWSER_CACHE_PATHS)
    for nombre in ("Chrome", "Edge", "Brave", "Opera"):
        assert nombre in juntos


def test_cache_paths_only_point_at_cache_folders():
    # Cada ruta configurada tiene que terminar en algo con "cache": es lo que
    # garantiza que no se liste una carpeta de datos del usuario.
    for navegador, ruta in browser.BROWSER_CACHE_PATHS.items():
        assert "cache" in ruta.lower(), f"{navegador} apunta a algo que no es caché"


def test_directory_size_adds_up_recursively(tmp_path):
    (tmp_path / "a").write_bytes(b"a" * 100)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b").write_bytes(b"a" * 200)
    assert browser.directory_size(tmp_path) == 300


def test_directory_size_of_missing_folder_is_zero(tmp_path):
    assert browser.directory_size(tmp_path / "no-existe") == 0


def test_total_cache_bytes_sums_detected_caches(tmp_path):
    caches = [
        browser.BrowserCache("A", tmp_path, 100),
        browser.BrowserCache("B", tmp_path, 250),
    ]
    assert browser.total_cache_bytes(caches) == 350


def test_summarize_includes_the_safety_note(tmp_path):
    caches = [browser.BrowserCache("Chrome", tmp_path / "Cache", 1024 * 1024)]
    texto = "\n".join(browser.summarize(caches))
    assert "Chrome" in texto
    assert "contraseñas" in texto


def test_summarize_with_no_caches():
    assert "No se detectaron" in "\n".join(browser.summarize([]))


# ==========================================================================
# healthscore
# ==========================================================================

def test_weights_add_up_to_one_hundred():
    assert sum(healthscore.WEIGHTS.values()) == 100


def test_security_weighs_more_than_junk():
    # Un archivo sospechoso importa más que unos MB de basura.
    assert healthscore.WEIGHTS["seguridad"] > healthscore.WEIGHTS["basura"]


def test_a_clean_system_scores_high():
    resultado = healthscore.compute_score(healthscore.SystemMetrics())
    assert resultado.score >= 95
    assert resultado.grade == "A"
    assert resultado.is_healthy


def test_a_broken_system_scores_low():
    resultado = healthscore.compute_score(healthscore.SystemMetrics(
        junk_mb=9000,
        suspicious_count=40,
        suspicious_warnings=10,
        memory_available_percent=2,
        disk_free_percent=1,
        duplicate_mb=5000,
        startup_count=40,
    ))
    assert resultado.score <= 20
    assert resultado.grade == "F"
    assert not resultado.is_healthy


def test_score_never_leaves_the_zero_to_hundred_range():
    for metrics in (
        healthscore.SystemMetrics(),
        healthscore.SystemMetrics(junk_mb=-500, disk_free_percent=999),
        healthscore.SystemMetrics(suspicious_count=-3, startup_count=-10),
        healthscore.SystemMetrics(memory_available_percent=1000),
    ):
        resultado = healthscore.compute_score(metrics)
        assert 0 <= resultado.score <= 100


def test_breakdown_covers_every_weighted_area():
    resultado = healthscore.compute_score(healthscore.SystemMetrics())
    assert set(resultado.breakdown) == set(healthscore.WEIGHTS)
    for area, puntos in resultado.breakdown.items():
        assert 0 <= puntos <= healthscore.WEIGHTS[area]


def test_grade_boundaries():
    assert healthscore.grade_for_score(100) == "A"
    assert healthscore.grade_for_score(90) == "A"
    assert healthscore.grade_for_score(89) == "B"
    assert healthscore.grade_for_score(80) == "B"
    assert healthscore.grade_for_score(79) == "C"
    assert healthscore.grade_for_score(65) == "C"
    assert healthscore.grade_for_score(64) == "D"
    assert healthscore.grade_for_score(50) == "D"
    assert healthscore.grade_for_score(49) == "F"
    assert healthscore.grade_for_score(0) == "F"


def test_memory_score_does_not_reward_excess_free_ram():
    # 35% disponible ya es el máximo: tener 90% libre no da más puntos,
    # porque RAM libre de sobra no mejora el rendimiento.
    assert healthscore.score_memory(35) == healthscore.score_memory(90) == 1.0


def test_individual_scores_stay_between_zero_and_one():
    for valor in (-100, 0, 50, 100, 99999):
        for fn in (healthscore.score_junk, healthscore.score_memory,
                   healthscore.score_disk, healthscore.score_duplicates):
            assert 0.0 <= fn(valor) <= 1.0
        assert 0.0 <= healthscore.score_startup(int(valor)) <= 1.0
        assert 0.0 <= healthscore.score_security(int(valor)) <= 1.0


def test_warnings_hurt_more_than_informational_findings():
    solo_info = healthscore.score_security(4, warnings=0)
    con_warnings = healthscore.score_security(4, warnings=4)
    assert con_warnings < solo_info


def test_a_healthy_system_still_gets_a_recommendation():
    resultado = healthscore.compute_score(healthscore.SystemMetrics())
    assert resultado.recommendations
    assert "buen estado" in " ".join(resultado.recommendations)


def test_low_disk_produces_a_specific_recommendation():
    resultado = healthscore.compute_score(healthscore.SystemMetrics(disk_free_percent=3))
    assert any("disco" in r.lower() for r in resultado.recommendations)


def test_quarantined_files_are_mentioned():
    resultado = healthscore.compute_score(healthscore.SystemMetrics(quarantined_count=3))
    assert any("cuarentena" in r.lower() for r in resultado.recommendations)


def test_compute_score_is_pure_and_repeatable():
    metrics = healthscore.SystemMetrics(junk_mb=500, suspicious_count=2, startup_count=7)
    primero = healthscore.compute_score(metrics)
    segundo = healthscore.compute_score(metrics)
    assert primero.score == segundo.score
    assert primero.breakdown == segundo.breakdown


def test_summarize_shows_score_grade_and_recommendations():
    resultado = healthscore.compute_score(healthscore.SystemMetrics(junk_mb=3000))
    texto = "\n".join(healthscore.summarize(resultado))
    assert "Salud del sistema" in texto
    assert "Recomendaciones" in texto


# ==========================================================================
# reporting
# ==========================================================================

def test_report_includes_branding_and_sections():
    texto = reporting.build_report({"salud": ["100/100"], "memoria": ["8 GB"]})
    assert branding.APP_NAME in texto
    assert "Salud general del sistema" in texto
    assert "100/100" in texto


def test_report_skips_sections_that_were_not_run():
    texto = reporting.build_report({"memoria": ["dato"]})
    assert "Memoria RAM" in texto
    assert "Uso de disco" not in texto


def test_empty_report_says_so():
    assert "Todavía no se ejecutó" in reporting.build_report({})


def test_section_underline_matches_the_title_length():
    lineas = reporting.section("Título", ["contenido"])
    assert len(lineas[1]) == len("Título")


def test_section_with_no_content_shows_a_placeholder():
    assert "(sin datos)" in reporting.section("T", [])


def test_markdown_report_uses_headers_and_code_blocks():
    md = reporting.build_markdown({"disco": ["C: 50%"]})
    assert md.startswith(f"# {branding.APP_NAME}")
    assert "## Uso de disco" in md
    assert md.count("```") == 2


def test_save_report_writes_text_file(tmp_path):
    ruta = reporting.save_report({"salud": ["ok"]}, tmp_path / "sub" / "informe.txt")
    assert ruta.is_file()
    assert branding.APP_NAME in ruta.read_text(encoding="utf-8")


def test_save_report_writes_markdown(tmp_path):
    ruta = reporting.save_report({"salud": ["ok"]}, tmp_path / "i.md", as_markdown=True)
    assert ruta.read_text(encoding="utf-8").startswith("#")


def test_report_sections_and_titles_are_in_sync():
    for clave in reporting.REPORT_SECTIONS:
        assert clave in reporting.SECTION_TITLES, f"falta el título de {clave}"


def test_quick_summary_lists_what_was_run():
    resumen = reporting.quick_summary({"salud": ["a"], "memoria": ["b", "c"]})
    assert "salud" in resumen and "memoria" in resumen


def test_quick_summary_on_empty_data():
    assert "Sin análisis" in reporting.quick_summary({})


# ==========================================================================
# logrotate
# ==========================================================================

def test_small_log_is_not_rotated(tmp_path):
    log = tmp_path / "evolve_log.md"
    log.write_text("línea\n" * 10, encoding="utf-8")
    resultado = logrotate.rotate_text_log(log, max_bytes=10_000)
    assert resultado["rotated"] is False
    assert log.read_text(encoding="utf-8").count("línea") == 10


def test_big_log_is_trimmed_and_archived(tmp_path):
    log = tmp_path / "evolve_log.md"
    archivo_historico = tmp_path / "archive"
    log.write_text("".join(f"línea {i}\n" for i in range(2000)), encoding="utf-8")

    resultado = logrotate.rotate_text_log(
        log, max_bytes=100, keep_lines=50, archive_dir=archivo_historico
    )

    assert resultado["rotated"] is True
    assert resultado["archived_lines"] == 1950
    contenido = log.read_text(encoding="utf-8")
    assert "línea 1999" in contenido, "lo más reciente tiene que quedar"
    assert "línea 0" not in contenido, "lo viejo tiene que salir del archivo activo"
    assert "línea 0" in Path(resultado["archive"]).read_text(encoding="utf-8"), \
        "lo viejo no se pierde, se archiva"


def test_rotated_log_explains_where_the_history_went(tmp_path):
    log = tmp_path / "evolve_log.md"
    log.write_text("".join(f"l{i}\n" for i in range(500)), encoding="utf-8")
    logrotate.rotate_text_log(log, max_bytes=10, keep_lines=10, archive_dir=tmp_path / "arch")
    assert "rotado" in log.read_text(encoding="utf-8")


def test_missing_log_does_not_crash(tmp_path):
    resultado = logrotate.rotate_text_log(tmp_path / "no-existe.md")
    assert resultado["rotated"] is False
    assert resultado["reason"] == "no existe"


def test_jsonl_keeps_the_most_recent_records(tmp_path):
    metrics = tmp_path / "metrics.jsonl"
    metrics.write_text("".join(f'{{"i": {i}}}\n' for i in range(300)), encoding="utf-8")

    resultado = logrotate.rotate_jsonl(metrics, keep_records=20, archive_dir=tmp_path / "arch")

    assert resultado["rotated"] is True
    assert resultado["archived_records"] == 280
    lineas = [l for l in metrics.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lineas) == 20
    assert '"i": 299' in lineas[-1]


def test_short_jsonl_is_left_alone(tmp_path):
    metrics = tmp_path / "metrics.jsonl"
    metrics.write_text('{"a": 1}\n', encoding="utf-8")
    assert logrotate.rotate_jsonl(metrics, keep_records=100)["rotated"] is False


def test_jsonl_rotation_keeps_valid_json_lines(tmp_path):
    import json
    metrics = tmp_path / "metrics.jsonl"
    metrics.write_text("".join(f'{{"n": {i}}}\n' for i in range(50)), encoding="utf-8")
    logrotate.rotate_jsonl(metrics, keep_records=5, archive_dir=tmp_path / "arch")
    for linea in metrics.read_text(encoding="utf-8").splitlines():
        if linea.strip():
            assert json.loads(linea)["n"] >= 45


def test_prune_keeps_the_newest_archives(tmp_path):
    import os
    archivo = tmp_path / "archive"
    archivo.mkdir()
    for i in range(10):
        f = archivo / f"viejo-{i}.md"
        f.write_text("x")
        os.utime(f, (1_000_000 + i * 100, 1_000_000 + i * 100))

    borrados = logrotate.prune_archives(archivo, keep_files=4)

    assert borrados == 6
    quedan = sorted(f.name for f in archivo.iterdir())
    assert quedan == ["viejo-6.md", "viejo-7.md", "viejo-8.md", "viejo-9.md"]


def test_prune_does_nothing_when_under_the_limit(tmp_path):
    archivo = tmp_path / "archive"
    archivo.mkdir()
    (archivo / "uno.md").write_text("x")
    assert logrotate.prune_archives(archivo, keep_files=5) == 0


def test_prune_on_missing_folder_returns_zero(tmp_path):
    assert logrotate.prune_archives(tmp_path / "no-existe") == 0


def test_rotate_all_handles_a_project_without_logs(tmp_path):
    resultado = logrotate.rotate_all(tmp_path)
    assert resultado["log"]["rotated"] is False
    assert resultado["metrics"]["rotated"] is False
    assert Path(resultado["archive_dir"]).is_dir()


def test_rotate_all_rotates_both_files(tmp_path):
    (tmp_path / "evolve").mkdir()
    # Las líneas se rellenan a propósito para pasar el umbral real de
    # MAX_LOG_BYTES: si el archivo no pesa lo suficiente, no se rota, y el
    # test estaría midiendo otra cosa.
    relleno = "x" * 120
    (tmp_path / "evolve_log.md").write_text(
        "".join(f"l{i} {relleno}\n" for i in range(3000)), encoding="utf-8")
    (tmp_path / "evolve" / "metrics.jsonl").write_text(
        "".join(f'{{"i": {i}}}\n' for i in range(2000)), encoding="utf-8")

    resultado = logrotate.rotate_all(tmp_path)

    assert resultado["log"]["rotated"] is True
    assert resultado["metrics"]["rotated"] is True
    assert "archivadas" in logrotate.summarize(resultado)


def test_summarize_when_there_is_nothing_to_rotate():
    vacio = {"log": {"rotated": False}, "metrics": {"rotated": False}, "pruned": 0}
    assert "nada para rotar" in logrotate.summarize(vacio)


def test_containment_check_blocks_escaping_the_archive(tmp_path):
    dentro = tmp_path / "archive" / "x.md"
    (tmp_path / "archive").mkdir()
    dentro.write_text("x")
    assert logrotate._is_within(dentro, tmp_path / "archive")
    assert not logrotate._is_within(tmp_path / "afuera.md", tmp_path / "archive")
