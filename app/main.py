"""
main.py — Limpieza Total Omega
Interfaz de escritorio para Windows 11: limpieza, seguridad, memoria, disco,
duplicados, navegadores, arranque y cuarentena, todo en una sola ventana con
pestañas.

REGLA QUE ATRAVIESA TODA ESTA INTERFAZ
--------------------------------------
Nada se borra sin que el usuario lo pida de forma explícita, y toda acción
destructiva pasa por dos filtros antes de tocar el disco:
  1. Un diálogo de confirmación que dice exactamente qué va a pasar.
  2. La validación de `safety.py`, que bloquea rutas de sistema.

Los análisis (memoria, disco, duplicados, navegadores, arranque) son de
solo lectura: informan, no modifican. Lo sospechoso se aísla en cuarentena
(reversible), no se elimina.

CRITERIO DE DISEÑO
------------------
Los estilos no se escriben acá: todo color, tamaño e ícono sale de
`branding.py`. Así el bucle autónomo puede rediseñar la app entera sin tocar
una sola línea de lógica, y no hay forma de que un color quede desalineado
entre pestañas.

El panel de Salud usa un medidor circular y barras por área en vez de solo
texto, porque el objetivo es que el estado del sistema se entienda sin leer.

RENDIMIENTO
------------------
Los análisis del panel de Salud se consolidan en una única ejecución asíncrona
para minimizar el overhead de hilos y garantizar la coherencia de los datos
que consume el asistente. El estado de análisis pesados se cachea por sesión.
Se emplea invalidación selectiva para evitar procesado redundante en disco.
Se implementa TTL (Time-To-Live) y política LRU para gestión eficiente de memoria.
Se optimizan eventos de redibujo UI para evitar cálculos innecesarios.

Instalar dependencias:
    pip install customtkinter

Ejecutar:
    python main.py
"""

import concurrent.futures
import logging
import os
import time
import tkinter as tk
from collections import OrderedDict
from tkinter import filedialog, messagebox
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any, Callable, Union

import customtkinter as ctk

import assistant
import branding
import browser
import diskreport
import duplicates as duplicates_mod
import healthscore
import memory as memory_mod
import quarantine
import reporting
import safety
import settings as settings_mod
import startup as startup_mod
from organizer import (
    scan_for_junk,
    sort_junk,
    stage_for_review,
    delete_reviewed,
    list_available_drives,
)
from scanner import scan_directory, run_windows_defender_quick_scan

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# Nombres de las pestañas. En un solo lugar para que agregar una sección
# nueva no implique buscar strings sueltos por todo el archivo.
TABS = (
    "Salud",
    "Limpieza",
    "Seguridad",
    "Cuarentena",
    "Memoria",
    "Disco",
    "Duplicados",
    "Navegadores",
    "Inicio",
    "Informe",
    "Asistente",
    "Ajustes",
)

# Áreas del puntaje de salud, con su etiqueta para la interfaz.
HEALTH_AREAS = (
    ("basura", "Archivos basura"),
    ("seguridad", "Seguridad"),
    ("memoria", "Memoria RAM"),
    ("disco", "Espacio en disco"),
    ("duplicados", "Duplicados"),
    ("inicio", "Programas de inicio"),
)


class LimpiezaTotalOmegaApp(ctk.CTk):
    """Orquesta la interfaz gráfica, el estado de la sesión y la delegación de 
    tareas intensivas en E/S a un pool de hilos de ejecución.
    """

    def __init__(self):
        super().__init__()
        self.tabs: Dict[str, ctk.CTkFrame] = {}
        # Asegurar estado limpio antes de intentar inicializar la GUI
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        try:
            self._validate_environment()
            self._init_window_properties()
            self._init_state()
            self._build_layout()
        except Exception as e:
            logging.critical("Error fatal al inicializar la aplicación: %s", e)
            raise

    # ------------------------------------------------------------------
    # Inicialización
    # ------------------------------------------------------------------

    def _validate_environment(self) -> None:
        """Verifica que el entorno de ejecución sea apto antes de iniciar la GUI."""
        home = os.path.expanduser("~")
        if not os.path.exists(home) or not os.access(home, os.R_OK):
            raise OSError(f"Directorio de usuario inaccesible: {home}")

    def _init_window_properties(self) -> None:
        """Establece la geometría inicial, título y colores globales desde el branding."""
        self.title(branding.app_title())
        try:
            self.geometry("1120x780")
            self.minsize(980, 680)
        except Exception as e:
            logging.warning("No se pudo configurar la geometría inicial: %s", e)
        try:
            bg_color = branding.color("background")
            if bg_color:
                self.configure(fg_color=bg_color)
        except Exception as e:
            logging.error("Fallo al aplicar colores de branding: %s", e)

    def _init_state(self) -> None:
        """Configura los contenedores de datos, el caché de sesión, la carga de 
        ajustes y el pool de hilos para procesos asíncronos.
        """
        self._cache: OrderedDict[str, Tuple[Any, float]] = OrderedDict()
        self._cache_ttl = 300
        self._cache_max_size = 20
        
        self._last_health_state: Optional[Tuple] = None
        self.scan_target: Optional[str] = None
        self.analysis_folder: Optional[str] = None
        self.report_data: Dict[str, List[str]] = {}
        self.assistant_context = assistant.SystemContext()
        
        try:
            raw_settings = settings_mod.load()
            self.settings = raw_settings if isinstance(raw_settings, dict) else settings_mod.reset()
        except Exception as e:
            logging.error("Fallo al cargar ajustes, reseteando a valores por defecto: %s", e)
            self.settings = settings_mod.reset()
            
        self.setting_vars: Dict[str, Any] = {}
        self.outputs: Dict[str, ctk.CTkTextbox] = {}
        self.cards: Dict[str, ctk.CTkLabel] = {}
        self.area_bars: Dict[str, Tuple[ctk.CTkProgressBar, ctk.CTkLabel]] = {}
        
        self._tasks_running = 0
        if self._executor: self._executor.shutdown(wait=False)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        self._debounce_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Factorías de Componentes UI (Modularización)
    # ------------------------------------------------------------------

    def _create_styled_label(self, parent: Any, text: str, style: str, **kwargs) -> ctk.CTkLabel:
        """Crea una etiqueta con tipografía y color extraídos de la marca."""
        font_config = {"size": branding.font_size(style)}
        if style == "title": font_config["weight"] = "bold"
        if style == "caption": font_config["weight"] = "bold"
        
        color_map = {
            "title": "text", "body": "text_muted", "caption": "text_dim", "accent": "accent"
        }
        
        return ctk.CTkLabel(
            parent, text=text,
            text_color=branding.color(color_map.get(style, "text")),
            font=ctk.CTkFont(**font_config),
            **kwargs
        )

    def _make_output(self, tab_name: str, parent: ctk.CTk) -> ctk.CTkTextbox:
        """Genera un área de texto para logs en la pestaña indicada."""
        box = ctk.CTkTextbox(
            parent,
            fg_color=branding.color("card"),
            text_color=branding.color("text"),
            border_color=branding.color("border"),
            border_width=1,
            corner_radius=10,
            font=ctk.CTkFont(family="Consolas", size=branding.font_size("mono")),
        )
        box.pack(fill="both", expand=True, padx=12, pady=12)
        self.outputs[tab_name] = box
        return box

    def _button_row(self, parent: ctk.CTk) -> ctk.CTkFrame:
        """Genera una fila horizontal para alinear botones de acción."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(12, 0))
        return row

    def _action(self, parent: ctk.CTk, text: str, command: Callable, 
                danger: bool = False, column: int = 0, secondary: bool = False) -> ctk.CTkButton:
        """Crea un botón estilizado según la importancia de la acción."""
        if danger:
            fondo, hover, texto = ("danger", "danger_hover", "text")
        elif secondary:
            fondo, hover, texto = ("accent2", "accent2_hover", "text")
        else:
            fondo, hover, texto = ("accent", "accent_hover", "background")

        button = ctk.CTkButton(
            parent, text=text, command=command,
            fg_color=branding.color(fondo),
            hover_color=branding.color(hover),
            text_color=branding.color(texto),
            font=ctk.CTkFont(size=branding.font_size("body"), weight="bold"),
            width=190, height=36, corner_radius=9,
        )
        button.grid(row=0, column=column, padx=6, pady=4, sticky="w")
        return button

    def _hint(self, parent: ctk.CTk, text: str) -> None:
        """Crea una etiqueta descriptiva de menor importancia."""
        self._create_styled_label(
            parent, text, "caption",
            wraplength=1010, justify="left"
        ).pack(fill="x", padx=14, pady=(10, 0))

    def _menu(self, parent: ctk.CTk, values: List[str], variable: tk.StringVar, 
              command: Optional[Callable] = None, width: int = 190) -> ctk.CTkOptionMenu:
        """Genera un menú desplegable con colores de marca."""
        return ctk.CTkOptionMenu(
            parent, values=values, variable=variable, command=command, width=width,
            fg_color=branding.color("surface_alt"),
            button_color=branding.color("accent2"),
            button_hover_color=branding.color("accent2_hover"),
            text_color=branding.color("text"),
            dropdown_fg_color=branding.color("surface_alt"),
            dropdown_text_color=branding.color("text"),
            dropdown_hover_color=branding.color("surface_hover"),
            corner_radius=9,
        )

    def _entry(self, parent: ctk.CTk, placeholder: str, width: int = 200) -> ctk.CTkEntry:
        """Genera un campo de entrada de texto estandarizado."""
        return ctk.CTkEntry(
            parent, width=width, placeholder_text=placeholder,
            fg_color=branding.color("card"),
            border_color=branding.color("border"),
            text_color=branding.color("text"),
            corner_radius=9,
        )

    # ------------------------------------------------------------------
    # Métodos de Construcción de Layout Principal
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        """Estructura las secciones principales de la interfaz."""
        self._build_header()
        self._build_tabs_container()
        self._build_footer()

    def _tab_factory(self, name: str) -> None:
        """Mapea el nombre de la pestaña a su método de construcción UI."""
        # Se agrupan los métodos de construcción por lógica de dominio
        constructors: Dict[str, Callable] = {
            "Salud": self._build_tab_salud,
            "Limpieza": self._build_tab_limpieza,
            "Seguridad": self._build_tab_seguridad,
            "Cuarentena": self._build_tab_cuarentena,
            "Memoria": self._build_tab_memoria,
            "Disco": self._build_tab_disco,
            "Duplicados": self._build_tab_duplicados,
            "Navegadores": self._build_tab_navegadores,
            "Inicio": self._build_tab_inicio,
            "Informe": self._build_tab_informe,
            "Asistente": self._build_tab_asistente,
            "Ajustes": self._build_tab_ajustes,
        }
        constructor = constructors.get(name)
        if constructor:
            constructor()

    def _build_tabs_container(self) -> None:
        """Inicializa el contenedor de pestañas y sus paneles correspondientes."""
        self.tabview = ctk.CTkTabview(
            self,
            fg_color=branding.color("surface"),
            segmented_button_fg_color=branding.color("surface_alt"),
            segmented_button_selected_color=branding.color("accent"),
            segmented_button_selected_hover_color=branding.color("accent_hover"),
            segmented_button_unselected_color=branding.color("surface_alt"),
            segmented_button_unselected_hover_color=branding.color("surface_hover"),
            text_color=branding.color("text"),
            corner_radius=12,
            border_width=1,
            border_color=branding.color("border"),
        )
        self.tabview.pack(fill="both", expand=True, padx=18, pady=(4, 8))

        for name in TABS:
            try:
                label = branding.tab_label(name)
                frame = self.tabview.add(label)
                if frame:
                    self.tabs[name] = frame
                    self._tab_factory(name)
                else:
                    logging.warning("No se pudo obtener frame para la pestaña: %s", name)
            except Exception as e:
                logging.error("Error al construir la pestaña %s: %s", name, e)

    def _build_header(self) -> None:
        """Dibuja el encabezado con logo, nombre, tagline y versión."""
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(16, 0))

        canvas = tk.Canvas(
            header, width=72, height=72,
            bg=branding.color("background"),
            highlightthickness=0, bd=0,
        )
        canvas.grid(row=0, column=0, rowspan=2, padx=(0, 16))
        try:
            branding.draw_logo(canvas, size=72)
        except Exception as e:
            logging.error("Error al dibujar logo: %s", e)

        self._create_styled_label(header, branding.APP_NAME, "title").grid(row=0, column=1, sticky="sw")
        self._create_styled_label(header, branding.APP_TAGLINE, "body").grid(row=1, column=1, sticky="nw")

        self._create_styled_label(
            header, f"  v{branding.APP_VERSION}  ", "caption",
            fg_color=branding.color("accent"), corner_radius=9
        ).grid(row=0, column=2, sticky="e", padx=(16, 0))
        header.grid_columnconfigure(2, weight=1)

        franja = tk.Canvas(self, height=3, bg=branding.color("background"),
                           highlightthickness=0, bd=0)
        franja.pack(fill="x", padx=18, pady=(12, 6))

        def on_resize(event):
            if self._debounce_id:
                self.after_cancel(self._debounce_id)
            self._debounce_id = self.after(100, lambda: (franja.delete("all"), branding.draw_gradient_bar(franja, event.width, 3)))

        franja.bind("<Configure>", on_resize)

    def _build_footer(self) -> None:
        """Crea la barra de estado y el indicador de actividad."""
        pie = ctk.CTkFrame(self, fg_color="transparent")
        pie.pack(fill="x", padx=18, pady=(0, 12))

        self.status = self._create_styled_label(pie, "Listo. Nada se borra sin tu confirmación.", "caption")
        self.status.pack(side="left")

        self.activity = ctk.CTkProgressBar(
            pie, width=170, height=6, mode="indeterminate",
            fg_color=branding.color("surface_alt"),
            progress_color=branding.color("accent"),
        )
        self.activity.pack(side="right")
        self.activity.pack_forget()

    # ------------------------------------------------------------------
    # Construcción de Pestañas Específicas
    # ------------------------------------------------------------------

    def _build_tab_salud(self) -> None:
        """Construye el dashboard de salud con métricas globales."""
        tab = self.tabs["Salud"]
        row = self._button_row(tab)
        self._action(row, "Analizar el sistema", self.on_full_analysis, column=0)
        self._action(row, "Limpiar panel", lambda: self.clear("Salud"),
                     secondary=True, column=1)

        card_container = ctk.CTkFrame(tab, fg_color="transparent")
        card_container.pack(fill="x", padx=12, pady=(14, 0))
        self._build_health_metrics_row(card_container)

        center_container = ctk.CTkFrame(tab, fg_color="transparent")
        center_container.pack(fill="x", padx=12, pady=(16, 0))
        center_container.grid_columnconfigure(1, weight=1)

        self.gauge = tk.Canvas(
            center_container, width=176, height=176,
            bg=branding.color("surface"), highlightthickness=0, bd=0,
        )
        self.gauge.grid(row=0, column=0, padx=(4, 22))
        self._draw_gauge(0, "-")

        self._build_health_area_bars(center_container)

        self._hint(tab, "Combina limpieza, seguridad, memoria, disco y arranque en un solo "
                        "puntaje. Es un análisis de solo lectura: no modifica nada.")
        self._make_output("Salud", tab)

    def _build_health_metrics_row(self, container: ctk.CTkFrame) -> None:
        """Crea las tarjetas de métricas numéricas superiores."""
        metrics = (("basura", "Basura"), ("sospechosos", "Sospechosos"),
                   ("ram", "RAM libre"), ("disco", "Disco libre"))
        for i, (clave, titulo) in enumerate(metrics):
            container.grid_columnconfigure(i, weight=1)
            self.cards[clave] = self._metric_card(container, titulo, i)

    def _build_health_area_bars(self, parent: ctk.CTk) -> None:
        """Genera el desglose de barras para cada categoría de salud."""
        area_container = ctk.CTkFrame(parent, fg_color="transparent")
        area_container.grid(row=0, column=1, sticky="ew")
        area_container.grid_columnconfigure(1, weight=1)
        for fila, (clave, etiqueta) in enumerate(HEALTH_AREAS):
            self._build_single_health_bar(area_container, clave, etiqueta, fila)

    def _build_single_health_bar(self, container: ctk.CTkFrame, clave: str, etiqueta: str, fila: int) -> None:
        """Construye una fila individual de barra de progreso y su valor."""
        self._create_styled_label(container, etiqueta, "body", anchor="w", width=150).grid(row=fila, column=0, sticky="w", pady=4)
        
        barra = ctk.CTkProgressBar(
            container, height=9, corner_radius=5,
            fg_color=branding.color("surface_alt"),
            progress_color=branding.color("accent"),
        )
        barra.grid(row=fila, column=1, sticky="ew", padx=10, pady=4)
        barra.set(0)
        
        valor_label = self._create_styled_label(container, "-", "caption", width=64, anchor="e")
        valor_label.grid(row=fila, column=2, sticky="e", pady=4)
        self.area_bars[clave] = (barra, valor_label)

    def _metric_card(self, parent: ctk.CTk, title: str, column: int) -> ctk.CTkLabel:
        """Crea una tarjeta decorativa de métrica."""
        tarjeta = ctk.CTkFrame(
            parent, fg_color=branding.color("card"), corner_radius=12,
            border_width=1, border_color=branding.color("border"),
        )
        tarjeta.grid(row=0, column=column, padx=6, sticky="ew")

        valor_label = self._create_styled_label(tarjeta, "-", "accent")
        valor_label.pack(pady=(14, 0))
        self._create_styled_label(tarjeta, title.upper(), "caption").pack(pady=(0, 14))
        return valor_label

    def _draw_gauge(self, score: int, grade: str) -> None:
        """Dibuja el gauge circular de puntaje en el canvas."""
        def update_canvas():
            if not hasattr(self, 'gauge') or not self.gauge.winfo_exists(): return
            self.gauge.delete("all")
            branding.draw_ring(self.gauge, score, size=176, thickness=15)
            color_nota = branding.grade_color(grade) if grade != "-" else branding.color("text_dim")
            self.gauge.create_text(
                88, 78, text=str(score), fill=branding.score_color(score),
                font=("Segoe UI", branding.font_size("display"), "bold"),
            )
            self.gauge.create_text(
                88, 116, text=f"nota {grade}", fill=color_nota,
                font=("Segoe UI", branding.font_size("body"), "bold"),
            )
        self.after(0, update_canvas)

    def _build_tab_limpieza(self) -> None:
        """Construye el panel de gestión de archivos basura."""
        tab = self.tabs["Limpieza"]
        row = self._button_row(tab)
        self._action(row, "Buscar basura", self.on_scan_junk, column=0)
        self._action(row, "Mover a revisión", self.on_stage, secondary=True, column=1)
        self._action(row, "Vaciar revisados", self.on_delete_reviewed, danger=True, column=2)

        options_container = ctk.CTkFrame(tab, fg_color="transparent")
        options_container.pack(fill="x", padx=12, pady=(12, 0))

        self._create_styled_label(options_container, "Buscar en:", "body").grid(row=0, column=0, padx=(0, 8))
        drive_options = ["Por defecto (Temp + Descargas)"] + list_available_drives() + ["Elegir carpeta..."]
        self.target_choice = ctk.StringVar(value=drive_options[0])
        self._menu(options_container, drive_options, self.target_choice,
                   self.on_target_choice_changed, width=240).grid(row=0, column=1, padx=4)

        self.target_label = self._create_styled_label(options_container, "", "accent")
        self.target_label.grid(row=0, column=2, padx=10)

        self._create_styled_label(options_container, "Ordenar por:", "body").grid(row=0, column=3, padx=(20, 8))
        self.sort_by = ctk.StringVar(value="size")
        self._menu(options_container, ["size", "date"], self.sort_by,
                   lambda _: self.refresh_list(), width=110).grid(row=0, column=4, padx=4)

        self._make_output("Limpieza", tab)

    def _build_tab_seguridad(self) -> None:
        """Construye la interfaz de escaneo de seguridad."""
        tab = self.tabs["Seguridad"]
        row = self._button_row(tab)
        self._action(row, "Escaneo heurístico", self.on_heuristic_scan, column=0)
        self._action(row, "Elegir carpeta y escanear", self.on_heuristic_scan_folder,
                     secondary=True, column=1)
        self._action(row, "Aislar hallazgos", self.on_quarantine_findings,
                     danger=True, column=2)
        self._action(row, "Windows Defender", self.on_defender_scan,
                     secondary=True, column=3)
        self._make_output("Seguridad", tab)

    def _build_tab_cuarentena(self) -> None:
        """Construye la gestión de archivos aislados."""
        tab = self.tabs["Cuarentena"]
        row = self._button_row(tab)
        self._action(row, "Ver cuarentena", self.on_list_quarantine, column=0)
        self._action(row, "Restaurar por ID", self.on_restore_quarantine,
                     secondary=True, column=1)
        self._action(row, "Vaciar cuarentena", self.on_purge_quarantine,
                     danger=True, column=2)

        id_container = ctk.CTkFrame(tab, fg_color="transparent")
        id_container.pack(fill="x", padx=12, pady=(12, 0))
        self._create_styled_label(id_container, "ID a restaurar:", "body").grid(row=0, column=0, padx=(0, 8))
        self.quarantine_id = self._entry(id_container, "pegá el ID que ves en la lista", 240)
        self.quarantine_id.grid(row=0, column=1, padx=4)
        self._make_output("Cuarentena", tab)

    def _build_tab_memoria(self) -> None:
        """Construye la interfaz de diagnóstico de procesos."""
        tab = self.tabs["Memoria"]
        row = self._button_row(tab)
        self._action(row, "Diagnóstico de RAM", self.on_memory_report, column=0)
        self._action(row, "Procesos que más consumen", self.on_memory_processes,
                     secondary=True, column=1)
        self._action(row, "Liberar working set (PID)", self.on_trim_process,
                     danger=True, column=2)

        pid_container = ctk.CTkFrame(tab, fg_color="transparent")
        pid_container.pack(fill="x", padx=12, pady=(12, 0))
        self._create_styled_label(pid_container, "PID:", "body").grid(row=0, column=0, padx=(0, 8))
        self.pid_entry = self._entry(pid_container, "ej. 4812", 140)
        self.pid_entry.grid(row=0, column=1, padx=4)
        self._make_output("Memoria", tab)

    def _build_tab_disco(self) -> None:
        """Construye las herramientas de análisis de disco."""
        tab = self.tabs["Disco"]
        row = self._button_row(tab)
        self._action(row, "Espacio por unidad", self.on_drives_report, column=0)
        self._action(row, "Analizar una carpeta", self.on_disk_analysis,
                     secondary=True, column=1)
        self._make_output("Disco", tab)

    def _build_tab_duplicados(self) -> None:
        """Construye la interfaz de gestión de duplicados."""
        tab = self.tabs["Duplicados"]
        row = self._button_row(tab)
        self._action(row, "Buscar duplicados", self.on_find_duplicates, column=0)
        self._action(row, "Aislar copias extra", self.on_quarantine_duplicates,
                     danger=True, column=1)
        self._make_output("Duplicados", tab)

    def _build_tab_navegadores(self) -> None:
        """Construye la interfaz de reporte de cachés."""
        tab = self.tabs["Navegadores"]
        row = self._button_row(tab)
        self._action(row, "Detectar caché", self.on_browser_report, column=0)
        self._make_output("Navegadores", tab)

    def _build_tab_inicio(self) -> None:
        """Construye la interfaz de programas al inicio."""
        tab = self.tabs["Inicio"]
        row = self._button_row(tab)
        self._action(row, "Ver programas de inicio", self.on_startup_report, column=0)
        self._make_output("Inicio", tab)

    def _build_tab_informe(self) -> None:
        """Construye el generador de reportes de sesión."""
        tab = self.tabs["Informe"]
        row = self._button_row(tab)
        self._action(row, "Armar informe", self.on_build_report, column=0)
        self._action(row, "Guardar como .txt", lambda: self.on_save_report(False),
                     secondary=True, column=1)
        self._action(row, "Guardar como .md", lambda: self.on_save_report(True),
                     secondary=True, column=2)
        self._make_output("Informe", tab)

    def _build_tab_asistente(self) -> None:
        """Construye el chat del asistente de IA."""
        tab = self.tabs["Asistente"]
        row = self._button_row(tab)
        self._action(row, "Preguntar", self.on_ask_assistant, column=0)
        self._action(row, "¿Qué arreglo primero?",
                     lambda: self.on_ask_assistant("¿Qué es lo más urgente que debería arreglar?"),
                     secondary=True, column=1)
        self._action(row, "Limpiar charla", lambda: self.clear("Asistente"),
                     secondary=True, column=2)

        pregunta_container = ctk.CTkFrame(tab, fg_color="transparent")
        pregunta_container.pack(fill="x", padx=12, pady=(12, 0))
        pregunta_container.grid_columnconfigure(0, weight=1)
        self.question_entry = self._entry(pregunta_container, "Escribí tu pregunta y apretá Enter", 600)
        self.question_entry.grid(row=0, column=0, sticky="ew")
        self.question_entry.bind("<Return>", lambda _e: self.on_ask_assistant())

        sugeridas_container = ctk.CTkFrame(tab, fg_color="transparent")
        sugeridas_container.pack(fill="x", padx=12, pady=(10, 0))
        for i, texto in enumerate(assistant.SUGGESTED_QUESTIONS):
            ctk.CTkButton(
                sugeridas_container, text=texto, height=28, corner_radius=14,
                fg_color=branding.color("surface_alt"),
                hover_color=branding.color("surface_hover"),
                text_color=branding.color("text_muted"),
                font=ctk.CTkFont(size=branding.font_size("caption")),
                command=lambda t=texto: self.on_ask_assistant(t),
            ).grid(row=i // 3, column=i % 3, padx=4, pady=4, sticky="w")
        self._make_output("Asistente", tab)

    def _add_setting_label(self, parent: ctk.CTkFrame, text: str, row: int, col: int = 0) -> None:
        """Helper para agregar etiquetas en el panel de ajustes."""
        self._create_styled_label(parent, text, "body", anchor="w").grid(
            row=row, column=col, sticky="w", padx=(0, 10), pady=6
        )

    def _add_setting_switch(self, parent: ctk.CTkFrame, clave: str, texto: str, row: int, col: int) -> None:
        """Helper para agregar interruptores en el panel de ajustes."""
        variable = ctk.BooleanVar(value=bool(self.settings.get(clave)))
        self.setting_vars[clave] = variable
        ctk.CTkSwitch(
            parent, text=texto, variable=variable,
            progress_color=branding.color("accent"),
            button_color=branding.color("text"),
            text_color=branding.color("text"),
            font=ctk.CTkFont(size=branding.font_size("body")),
        ).grid(row=row, column=col, sticky="w", padx=(0, 24), pady=6)

    def _build_tab_ajustes(self) -> None:
        """Construye el formulario de configuración global."""
        tab = self.tabs["Ajustes"]
        row = self._button_row(tab)
        self._action(row, "Guardar ajustes", self.on_save_settings, column=0)
        self._action(row, "Ver configuración", self.on_show_settings, secondary=True, column=1)
        self._action(row, "Restaurar de fábrica", self.on_reset_settings, danger=True, column=2)

        grilla = ctk.CTkFrame(tab, fg_color="transparent")
        grilla.pack(fill="x", padx=12, pady=(14, 0))

        self._add_setting_label(grilla, "Tema:", 0)
        self.setting_vars["tema"] = ctk.StringVar(value=self.settings.get("tema", "oscuro"))
        self._menu(grilla, list(settings_mod.VALID_THEMES), self.setting_vars["tema"], width=150).grid(row=0, column=1, sticky="w")

        self._add_setting_label(grilla, "Acento:", 0, 2)
        self.setting_vars["acento"] = ctk.StringVar(value=self.settings.get("acento", "menta"))
        self._menu(grilla, list(settings_mod.VALID_ACCENTS), self.setting_vars["acento"], width=150).grid(row=0, column=3, sticky="w")

        self._add_setting_switch(grilla, "mostrar_barras", "Barras visuales", 1, 0)
        self._add_setting_switch(grilla, "analisis_en_paralelo", "Análisis en paralelo", 1, 1)
        self._add_setting_switch(grilla, "recordar_ultima_carpeta", "Recordar última carpeta", 1, 2)

        self._add_setting_label(grilla, "Duplicados desde (KB):", 2)
        self.min_dup_entry = self._entry(grilla, "64", 100)
        self.min_dup_entry.insert(0, str(self.settings.get("duplicados_tamano_minimo_kb", 64)))
        self.min_dup_entry.grid(row=2, column=1, sticky="w")

        self._add_setting_label(grilla, "Top de archivos:", 2, 2)
        self.top_files_entry = self._entry(grilla, "15", 100)
        self.top_files_entry.insert(0, str(self.settings.get("top_archivos", 15)))
        self.top_files_entry.grid(row=2, column=3, sticky="w")

        self._create_styled_label(
            tab, f"{branding.icon('Asistente')}  Asistente en línea (opcional)", "title",
            anchor="w", text_color=branding.color("accent2")
        ).pack(fill="x", padx=14, pady=(18, 0))

        ia_container = ctk.CTkFrame(tab, fg_color="transparent")
        ia_container.pack(fill="x", padx=12, pady=(6, 0))

        self.setting_vars["asistente_activado"] = ctk.BooleanVar(value=bool(self.settings.get("asistente_activado")))
        ctk.CTkSwitch(
            ia_container, text="Activar asistente en línea",
            variable=self.setting_vars["asistente_activado"],
            progress_color=branding.color("accent2"),
            button_color=branding.color("text"),
            text_color=branding.color("text"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 20), pady=6)

        self._create_styled_label(ia_container, "Clave de API:", "body").grid(row=0, column=1, padx=(0, 8))
        self.api_key_entry = self._entry(ia_container, f"vacío = usar {settings_mod.API_KEY_ENV_VAR}", 260)
        self.api_key_entry.configure(show="*")
        self.api_key_entry.grid(row=0, column=2, sticky="w")
        self._make_output("Ajustes", tab)

    # ------------------------------------------------------------------
    # Utilidades generales y helpers
    # ------------------------------------------------------------------

    def _is_safe_path(self, path: Union[str, Path]) -> bool:
        """Valida que una ruta no esté protegida y no sea un enlace simbólico."""
        try:
            p = Path(path).resolve(strict=True)
            if p.is_symlink():
                return False
            return not safety.is_protected_path(p)
        except (OSError, RuntimeError, PermissionError):
            return False

    def _is_safe_target_dir(self, path: Union[str, Path]) -> bool:
        """Valida que un directorio sea seguro para ser seleccionado como objetivo."""
        try:
            p = Path(path).resolve(strict=True)
            return p.is_dir() and not safety.is_protected_path(p)
        except (OSError, PermissionError):
            return False

    def _is_valid_dir(self, path: Optional[Union[str, Path]]) -> bool:
        """Valida la existencia y accesibilidad de lectura de un directorio."""
        if not path:
            return False
        try:
            p = Path(path).resolve(strict=True)
            return p.is_dir() and os.access(p, os.R_OK)
        except (OSError, PermissionError):
            return False

    def _get_cached_data(self, key: str) -> Any:
        """Helper para recuperar datos del caché centralizado."""
        return self._get_cached(key)

    def _get_cached(self, key: str, provider: Optional[Callable] = None, force: bool = False) -> Any:
        """Retorna datos cacheados si están vigentes; gestiona política LRU."""
        now = time.time()
        if not force and key in self._cache:
            data, timestamp = self._cache[key]
            if now - timestamp < self._cache_ttl:
                self._cache.move_to_end(key)
                return data
            del self._cache[key]
        
        if provider:
            try:
                data = provider()
                if data is not None:
                    if len(self._cache) >= self._cache_max_size:
                        # Política FIFO (popitem(last=False)) para mantener el tamaño bajo control
                        self._cache.popitem(last=False)
                    self._cache[key] = (data, now)
                return data
            except Exception as e:
                logging.error("Error al obtener datos para caché %s: %s", key, e)
        return None

    def _get_cached_or_run(self, key: str, provider: Callable, on_complete: Callable) -> None:
        """Ejecuta una función asíncrona solo si no existe caché válido."""
        cached = self._get_cached(key)
        if cached is not None:
            on_complete(cached)
        else:
            self.run_async(lambda: on_complete(provider()))

    def _invalidate_cache(self, key_prefix: str) -> None:
        """Purga entradas del caché que coincidan con un prefijo."""
        keys_to_del = [k for k in self._cache if k.startswith(key_prefix)]
        for k in keys_to_del:
            del self._cache[k]

    def _box(self, tab: str) -> ctk.CTkTextbox:
        """Helper para recuperar el textbox de log específico de una pestaña."""
        return self.outputs.get(tab) or self.outputs["Limpieza"]

    def log(self, text: str, tab: str = "Limpieza") -> None:
        """Escribe una cadena de texto en el log de la pestaña especificada."""
        box = self._box(tab)

        def append():
            try:
                box.insert("end", f"{text}\n")
                box.see("end")
            except Exception:
                pass

        self.after(0, append)

    def clear(self, tab: str = "Limpieza") -> None:
        """Elimina todo el contenido actual del cuadro de log de una pestaña."""
        box = self._box(tab)
        self.after(0, lambda: box.delete("1.0", "end"))

    def set_status(self, text: str) -> None:
        """Actualiza el mensaje mostrado en la barra de estado inferior."""
        self.after(0, lambda: self.status.configure(text=text))

    def log_lines(self, lines: List[str], tab: str) -> None:
        """Escribe una lista de líneas en el log y almacena los resultados."""
        self.clear(tab)
        box = self._box(tab)
        self.after(0, lambda: (box.insert("1.0", "\n".join(lines)), box.see("1.0")))
        self.report_data[tab.lower()] = list(lines)

    def _set_busy(self, busy: bool) -> None:
        """Controla el estado visual de la barra de progreso global."""
        def actualizar():
            if busy:
                self._tasks_running += 1
            else:
                self._tasks_running = max(0, self._tasks_running - 1)
            
            if self._tasks_running > 0:
                self.activity.pack(side="right")
                self.activity.start()
            else:
                self.activity.stop()
                self.activity.pack_forget()

        self.after(0, actualizar)

    def _validate_and_log_error(self, e: Exception, tab: str) -> None:
        """Manejador centralizado que traduce excepciones de sistema para el usuario."""
        if isinstance(e, safety.UnsafePathError):
            self.log(f"Bloqueado por seguridad: {e}", tab)
        elif isinstance(e, PermissionError):
            self.log("Error: permiso denegado. Ejecutá como administrador.", tab)
        elif isinstance(e, FileNotFoundError):
            self.log(f"Error: ruta no encontrada: {getattr(e, 'filename', 'desconocida')}", tab)
        elif isinstance(e, OSError):
            self.log(f"Error de sistema ({e.errno}): {e.strerror}", tab)
        else:
            logging.exception("Error inesperado en tarea asíncrona: %s", e)
            self.log(f"Error inesperado: {type(e).__name__}", tab)

    def _safe_run(self, fn: Callable, tab: str) -> None:
        """Envuelve la ejecución en un try-except para gestionar errores."""
        try:
            fn()
        except Exception as e:
            self._validate_and_log_error(e, tab)

    def run_async(self, fn: Callable) -> None:
        """Envía una tarea al pool de ejecución asíncrono."""
        self._set_busy(True)
        tab = self._current_tab()
        
        def wrapper():
            try:
                self._safe_run(fn, tab)
            finally:
                self._set_busy(False)
                self.set_status("Listo.")

        self._executor.submit(wrapper)

    def _current_tab(self) -> str:
        """Devuelve el nombre de la pestaña actualmente visible."""
        try:
            etiqueta = self.tabview.get()
            if not isinstance(etiqueta, str): return "Limpieza"
        except Exception:
            return "Limpieza"
        for nombre in TABS:
            if nombre in etiqueta:
                return nombre
        return "Limpieza"

    def _ask_folder(self, title: str) -> Optional[str]:
        """Diálogo de selección de carpetas con validación de seguridad."""
        folder = filedialog.askdirectory(title=title)
        if not folder or not isinstance(folder, str):
            return None
        
        # Pre-validación contra caracteres de control o inyección sutil (RTL)
        if any(ord(c) < 32 for c in folder) or "\u202e" in folder:
            messagebox.showerror("Ruta sospechosa", "La ruta seleccionada contiene caracteres ilegales.")
            return None
        
        # Validación final de seguridad contra el sistema
        if not self._is_safe_target_dir(folder):
            messagebox.showwarning("Ruta no segura", "Esa ruta está protegida o es inválida.")
            return None
            
        return folder

    def _confirm(self, title: str, message: str) -> bool:
        """Diálogo de confirmación."""
        return messagebox.askyesno(title, message, icon="warning")

    # ------------------------------------------------------------------
    # Lógica de Salud
    # ------------------------------------------------------------------

    def _compile_metrics(self) -> Tuple[healthscore.SystemMetrics, memory_mod.Snapshot, diskreport.DriveInfo]:
        """Consolida métricas del sistema priorizando caché existente."""
        hallazgos = self._get_cached_data("suspicions") or []
        arranque = self._get_cached_data("startup") or []
        junk = self._get_cached_data("junk") or []
        dups = self._get_cached_data("dups") or []

        snapshot = memory_mod.read_snapshot()
        home = os.path.expanduser("~")
        # Usamos un caché de sesión para el reporte de disco si la ruta no cambió
        cache_key = f"disk_info_{home}"
        unidad = self._get_cached(cache_key)
        if not unidad and os.path.exists(home):
            unidad = diskreport.drive_usage(home)
            self._cache[cache_key] = (unidad, time.time())
        
        metrics = healthscore.SystemMetrics(
            junk_mb=sum(j.size_bytes for j in junk) / (1024 * 1024),
            suspicious_count=len(hallazgos),
            suspicious_warnings=sum(1 for h in hallazgos if h.severity == "warning"),
            memory_available_percent=snapshot.available_percent if snapshot else 100.0,
            disk_free_percent=(unidad.free / unidad.total * 100) if (unidad and unidad.total > 0) else 100.0,
            duplicate_mb=duplicates_mod.reclaimable_bytes(dups) / (1024 * 1024),
            startup_count=len(arranque),
            quarantined_count=len(quarantine.list_items()),
        )
        return metrics, snapshot or memory_mod.Snapshot(0, 0, 0), unidad or diskreport.DriveInfo(0, 0, 0, "")

    def on_full_analysis(self) -> None:
        """Inicia el proceso de análisis completo del sistema."""
        def task():
            self.set_status("Analizando el sistema...")
            self.clear("Salud")
            self.log("Analizando... esto no modifica nada.", "Salud")

            metrics, snapshot, unidad = self._compile_metrics()
            resultado = healthscore.compute_score(metrics)

            self.assistant_context = assistant.build_context(
                metrics=metrics, health=resultado,
                memory_total_gb=snapshot.total / (1024 ** 3) if (snapshot and snapshot.total) else 0.0,
            )

            self._update_health_visuals(
                resultado, metrics.junk_mb, metrics.suspicious_count,
                metrics.memory_available_percent, metrics.disk_free_percent
            )

            lineas = healthscore.summarize(resultado)
            if not self._get_cached_data("dups"):
                lineas += ["", "Nota: los duplicados no se contaron todavía. "
                               "Corré la pestaña Duplicados para incluirlos."]
            self.log_lines(lineas, "Salud")
            self.set_status(f"Salud: {resultado.score}/100 (nota {resultado.grade})")

        self.run_async(task)

    def _update_health_visuals(self, resultado: healthscore.ScoreResult, junk_mb: float, 
                               sospechosos: int, ram_libre: float, disco_libre: float) -> None:
        """Actualiza la interfaz de salud tras completar el análisis."""
        state_key = (resultado.score, junk_mb, sospechosos, ram_libre, disco_libre)
        if self._last_health_state == state_key:
            return
        self._last_health_state = state_key

        def actualizar():
            self._draw_gauge(resultado.score, resultado.grade)

            valores = {
                "basura": f"{junk_mb:.0f} MB",
                "sospechosos": str(sospechosos),
                "ram": f"{ram_libre:.0f}%",
                "disco": f"{disco_libre:.0f}%",
            }
            colores = {
                "basura": branding.color("accent") if junk_mb < 1000 else branding.color("warning"),
                "sospechosos": branding.color("accent") if sospechosos == 0 else branding.color("warning"),
                "ram": branding.score_color(ram_libre * 3),
                "disco": branding.score_color(disco_libre * 5),
            }
            
            for clave, label in self.cards.items():
                label.configure(text=valores[clave], text_color=colores[clave])

            for clave, (barra, label) in self.area_bars.items():
                puntos = resultado.breakdown.get(clave, 0)
                maximo = healthscore.WEIGHTS.get(clave, 1)
                proporcion = puntos / maximo if maximo else 0
                c = branding.score_color(proporcion * 100)
                barra.configure(progress_color=c)
                barra.set(proporcion)
                label.configure(text=f"{puntos:.0f}/{maximo}", text_color=c)

        self.after(0, actualizar)

    # ------------------------------------------------------------------
    # Lógica de Limpieza
    # ------------------------------------------------------------------

    def on_target_choice_changed(self, choice: str) -> None:
        """Maneja el cambio de ubicación de destino para el escaneo."""
        if choice == "Elegir carpeta...":
            folder = self._ask_folder("Elegí una carpeta para escanear")
            if folder:
                self.scan_target = folder
                self.target_label.configure(text=folder)
            else:
                self.target_choice.set("Por defecto (Temp + Descargas)")
                self.scan_target = None
                self.target_label.configure(text="")
        elif choice == "Por defecto (Temp + Descargas)":
            self.scan_target = None
            self.target_label.configure(text="")
        else:
            if self._is_valid_dir(choice):
                self.scan_target = choice
                self.target_label.configure(text=f"Unidad completa: {choice}")
            else:
                self.log(f"Error: La unidad {choice} ya no es accesible.", "Limpieza")
                self.target_choice.set("Por defecto (Temp + Descargas)")
                self.scan_target = None
                self.target_label.configure(text="")

    def on_scan_junk(self) -> None:
        """Ejecuta la lógica de escaneo de basura."""
        def task():
            destino = self.scan_target or "carpetas por defecto (Temp/Descargas)"
            self.set_status(f"Buscando basura en {destino}...")
            self.clear("Limpieza")
            self.log(f"Buscando archivos basura en: {destino}...", "Limpieza")
            directories = [self.scan_target] if self.scan_target else None
            junk = scan_for_junk(directories)
            
            # Gestión de caché: invalidar caché previo de basura antes de insertar el nuevo
            self._invalidate_cache("junk")
            self._cache["junk"] = (junk, time.time())
            
            total_mb = round(sum(j.size_bytes for j in junk) / (1024 * 1024), 2)
            self.log(f"Encontrados {len(junk)} candidatos ({total_mb} MB).", "Limpieza")
            self.refresh_list()

        self.run_async(task)

    def refresh_list(self) -> None:
        """Refresca la vista de la lista de basura según el criterio seleccionado."""
        junk = self._get_cached_data("junk") or []
        ordered = sort_junk(junk, by=self.sort_by.get())
        lines = [f"{jf.size_mb:>8} MB  |  {jf.modified:%Y-%m-%d}  |  {jf.path}" for jf in ordered]
        self.report_data["limpieza"] = lines
        box = self._box("Limpieza")
        self.after(0, lambda: (box.delete("1.0", "end"), box.insert("1.0", "\n".join(lines))))

    def on_stage(self) -> None:
        """Mueve candidatos de limpieza a la zona de revisión."""
        junk = self._get_cached_data("junk") or []
        if not junk:
            messagebox.showinfo("Sin candidatos", "Primero usá 'Buscar basura'.")
            return
        
        aptos = [jf for jf in junk if self._is_safe_path(jf.path)]
        
        if not aptos:
            messagebox.showwarning("Sin candidatos seguros", "Todos los archivos encontrados están en rutas protegidas.")
            return

        if not self._confirm(
            "Mover a revisión",
            f"Se van a MOVER {len(aptos)} archivos seguros a la carpeta de revisión.\n\n"
            "No se borra nada: podés verlos y decidir después. ¿Seguimos?",
        ):
            return

        def task():
            self.set_status("Moviendo a revisión...")
            dest = stage_for_review(aptos)
            self.log(f"Movidos {len(aptos)} archivos a: {dest}", "Limpieza")
            self._invalidate_cache("junk")

        self.run_async(task)

    def on_delete_reviewed(self) -> None:
        """Ejecuta el borrado final de los archivos en revisión."""
        if not self._confirm(
            "Vaciar carpeta de revisión",
            "Esto BORRA de forma permanente los archivos que están en la carpeta "
            "de revisión.\n\nNo se puede deshacer. ¿Confirmás?",
        ):
            return

        def task():
            self.set_status("Vaciando la carpeta de revisión...")
            n = delete_reviewed()
            self.log(f"Borrados {n} archivos de la carpeta de revisión.", "Limpieza")

        self.run_async(task)

    # ------------------------------------------------------------------
    # Lógica de Seguridad
    # ------------------------------------------------------------------

    def _run_heuristic_scan(self, folder: str) -> None:
        """Ejecuta el escaneo heurístico de seguridad en la carpeta dada."""
        def task():
            if not self._is_valid_dir(folder):
                self.log(f"Error: La carpeta {folder} no es accesible.", "Seguridad")
                return
            
            self.set_status(f"Escaneando {folder}...")
            self.clear("Seguridad")
            self.log(f"Escaneo heurístico en: {folder}", "Seguridad")
            
            results = scan_directory(folder)
            
            self._invalidate_cache("suspicions")
            self._cache["suspicions"] = (results, time.time())

            if not results:
                self.log("Sin hallazgos sospechosos.", "Seguridad")
                self.report_data["seguridad"] = ["Sin hallazgos sospechosos."]
                return

            lineas = []
            for r in results:
                marca = branding.severity_icon(r.severity)
                etiqueta = branding.severity_label(r.severity)
                lineas.append(f"{marca} [{etiqueta}] {r.path} — {r.reason}")
            self.log_lines([f"{len(results)} hallazgo(s):", ""] + lineas, "Seguridad")
            self.log("", "Seguridad")
            self.log("Recordá: son señales, no una condena. Usá 'Aislar hallazgos' "
                     "para moverlos a cuarentena sin borrarlos.", "Seguridad")

        self.run_async(task)

    def on_heuristic_scan(self) -> None:
        """Inicia escaneo heurístico de Descargas."""
        downloads_path = os.path.expanduser("~/Downloads")
        if not os.path.isdir(downloads_path):
            self.log("No se encontró la carpeta de Descargas.", "Seguridad")
            return
        self._run_heuristic_scan(downloads_path)

    def on_heuristic_scan_folder(self) -> None:
        """Inicia escaneo heurístico en carpeta manual."""
        folder = self._ask_folder("Elegí una carpeta para escanear")
        if folder:
            self._run_heuristic_scan(folder)

    def on_quarantine_findings(self) -> None:
        """Mueve hallazgos de seguridad a cuarentena."""
        suspicions = self._get_cached_data("suspicions") or []
        if not suspicions:
            messagebox.showinfo("Sin hallazgos", "Primero corré un escaneo heurístico.")
            return
        
        aptos = [s for s in suspicions if self._is_safe_path(s.path)]
        
        if not aptos:
            messagebox.showwarning("Nada que aislar", "Los archivos sospechosos se encuentran en rutas protegidas.")
            return

        if not self._confirm(
            "Aislar en cuarentena",
            f"Se van a MOVER {len(aptos)} archivo(s) seguro(s) a la cuarentena.\n\n"
            "No se borran: quedan guardados con su ruta original y se pueden "
            "restaurar cuando quieras. ¿Seguimos?",
        ):
            return

        def task():
            self.set_status("Aislando archivos...")
            aislados = 0
            for item_s in aptos:
                item = quarantine.quarantine_file(item_s.path, reason="Marcado por escaneo heurístico")
                self.log(f"Aislado [{item.item_id}] {item_s.path}", "Seguridad")
                aislados += 1
            self.log(f"Listo: {aislados} aislado(s).", "Seguridad")
            self._invalidate_cache("suspicions")

        self.run_async(task)

    def on_defender_scan(self) -> None:
        """Invoca el escaneo rápido de Windows Defender."""
        def task():
            self.set_status("Windows Defender en curso...")
            self.log("Iniciando escaneo rápido de Windows Defender (puede tardar)...", "Seguridad")
            output = run_windows_defender_quick_scan()
            self.log(output, "Seguridad")

        self.run_async(task)

    # ------------------------------------------------------------------
    # Lógica de Cuarentena
    # ------------------------------------------------------------------

    def on_list_quarantine(self) -> None:
        """Lista el contenido actual de la cuarentena."""
        def task():
            self.log_lines(quarantine.summarize(), "Cuarentena")

        self.run_async(task)

    def on_restore_quarantine(self) -> None:
        """Restaura un archivo aislado desde su ID."""
        raw_id = self.quarantine_id.get().strip()
        if not raw_id:
            messagebox.showinfo("Falta el ID", "Pegá el ID del archivo que querés restaurar.")
            return

        def task():
            if not quarantine.item_exists(raw_id):
                self.log(f"Error: El ID '{raw_id}' no existe en la cuarentena.", "Cuarentena")
                return
            
            try:
                item = quarantine.get_item(raw_id)
            except Exception:
                self.log("Error: No se pudo acceder al manifiesto de este ID.", "Cuarentena")
                return

            if not item or not hasattr(item, 'original_path'):
                self.log("Error: El manifiesto de este ID está corrupto.", "Cuarentena")
                return

            if not self._is_safe_path(item.original_path):
                self.log(f"Error: La ruta original {item.original_path} está protegida o es inválida.", "Cuarentena")
                return
            
            destino = quarantine.restore_item(raw_id)
            self.log(f"Restaurado en: {destino}", "Cuarentena")

        self.run_async(task)

    def on_purge_quarantine(self) -> None:
        """Elimina todos los archivos en cuarentena."""
        items = quarantine.list_items()
        if not items:
            messagebox.showinfo("Cuarentena vacía", "No hay nada para borrar.")
            return
        if not self._confirm(
            "Vaciar cuarentena",
            f"Esto BORRA de forma permanente {len(items)} archivo(s) aislado(s).\n\n"
            "Después no se van a poder restaurar. ¿Confirmás?",
        ):
            return

        def task():
            borrados = quarantine.purge_all()
            self.log(f"Borrados {borrados} archivo(s) de la cuarentena.", "Cuarentena")

        self.run_async(task)

    # ------------------------------------------------------------------
    # Lógica de Memoria
    # ------------------------------------------------------------------

    def on_memory_report(self) -> None:
        """Genera un reporte del uso actual de RAM."""
        def task():
            snapshot = memory_mod.read_snapshot()
            procesos = memory_mod.top_memory_processes(limit=5)
            lineas = memory_mod.diagnose(snapshot, procesos)
            if snapshot and snapshot.total:
                lineas = [
                    f"Uso de memoria  {branding.bar(snapshot.used_percent, 30)}  "
                    f"{snapshot.used_percent:.0f}%",
                    "",
                ] + lineas
            self.log_lines(lineas, "Memoria")

        self.run_async(task)

    def on_memory_processes(self) -> None:
        """Lista los procesos con mayor consumo de memoria."""
        def task():
            procesos = memory_mod.top_memory_processes(limit=15)
            if not procesos:
                self.log_lines(["No se pudo obtener la lista de procesos en este sistema."],
                               "Memoria")
                return
            tope = max(p.working_set_mb for p in procesos) or 1
            lineas = ["Procesos por consumo de memoria:", ""]
            for p in procesos:
                relativo = p.working_set_mb / tope * 100
                lineas.append(
                    f"  {branding.bar(relativo, 18)}  {p.working_set_mb:>9} MB  "
                    f"PID {p.pid:<7} {p.name}"
                )
            lineas += ["", "Cerrar el que no uses libera memoria de verdad. "
                           "Copiá el PID si querés probar el trim manual."]
            self.log_lines(lineas, "Memoria")

        self.run_async(task)

    def on_trim_process(self) -> None:
        """Intenta liberar el Working Set de un proceso dado."""
        raw = self.pid_entry.get().strip()
        if not raw:
            messagebox.showwarning("Entrada vacía", "Ingresá un PID.")
            return
        
        try:
            pid = int(raw)
        except ValueError:
            messagebox.showwarning("Entrada inválida", "El PID debe ser un número entero.")
            return
            
        if pid < 0:
            messagebox.showwarning("PID inválido", "El PID debe ser positivo.")
            return
        
        # Validación: procesos del sistema (PID < 100)
        if pid < 100:
            messagebox.showerror("Bloqueado", "Este PID corresponde a un proceso del sistema esencial y no puede ser modificado.")
            return

        # Pre-chequeo de existencia del proceso antes de intentar trim
        try:
            if not memory_mod.process_exists(pid):
                messagebox.showerror("No encontrado", f"No se encontró un proceso activo con PID {pid}.")
                return
        except Exception as e:
            self.log(f"Error al verificar proceso {pid}: {e}", "Memoria")
            return

        if not self._confirm("Liberar working set", memory_mod.TRIM_WARNING + "\n\n¿Seguimos?"):
            return

        def task():
            try:
                ok, mensaje = memory_mod.trim_working_set(pid)
                self.log(("OK: " if ok else "Sin efecto: ") + mensaje, "Memoria")
            except Exception as e:
                self.log(f"Error crítico al intentar trim en PID {pid}: {e}", "Memoria")

        self.run_async(task)

    # ------------------------------------------------------------------
    # Lógica de Disco
    # ------------------------------------------------------------------

    def on_drives_report(self) -> None:
        """Genera el reporte de uso de todas las unidades conectadas."""
        def task():
            unidades = diskreport.all_drives_usage()
            if not unidades:
                self.log_lines(["No se detectaron unidades."], "Disco")
                return
            lineas = ["Espacio por unidad:", ""]
            for u in unidades:
                alerta = "  <-- casi llena" if u.is_almost_full else ""
                lineas.append(
                    f"  {u.mount:<6} {branding.bar(u.used_percent, 22)} {u.used_percent:>5.1f}%"
                )
                lineas.append(
                    f"         {diskreport.format_size(u.used)} usados de "
                    f"{diskreport.format_size(u.total)} — libre: "
                    f"{diskreport.format_size(u.free)}{alerta}"
                )
                lineas.append("")
            self.log_lines(lineas, "Disco")

        self.run_async(task)

    def on_disk_analysis(self) -> None:
        """Inicia el análisis de espacio de una carpeta específica."""
        folder = self._ask_folder("Elegí una carpeta para analizar")
        if not folder:
            return
        self.analysis_folder = folder

        def task():
            if not self._is_valid_dir(folder):
                self.log("Error: La carpeta seleccionada ya no existe.", "Disco")
                return
            self.set_status(f"Analizando {folder}...")
            self.clear("Disco")
            self.log(f"Analizando {folder} (solo lectura, puede tardar)...", "Disco")
            self.log_lines(diskreport.summarize(folder), "Disco")

        self.run_async(task)

    # ------------------------------------------------------------------
    # Lógica de Duplicados
    # ------------------------------------------------------------------

    def on_find_duplicates(self) -> None:
        """Ejecuta la búsqueda de archivos duplicados."""
        folder = self._ask_folder("Elegí una carpeta donde buscar duplicados")
        if not folder:
            return

        def task():
            if not self._is_valid_dir(folder):
                self.log("Error: La carpeta seleccionada ya no existe.", "Duplicados")
                return
            self.set_status(f"Buscando duplicados en {folder}...")
            self.clear("Duplicados")
            self.log(f"Buscando duplicados en {folder} (solo lectura, puede tardar)...",
                     "Duplicados")
            dups = duplicates_mod.find_duplicates([folder])
            
            self._invalidate_cache("dups")
            self._cache["dups"] = (dups, time.time())
            
            if not dups:
                self.log_lines(["No se encontraron duplicados."], "Duplicados")
                return
            recuperable = duplicates_mod.reclaimable_bytes(dups)
            lineas = [
                f"{len(dups)} grupo(s) de duplicados",
                f"Espacio recuperable: {diskreport.format_size(recuperable)}",
                "",
            ]
            for grupo in dups[:40]:
                lineas.extend(duplicates_mod.format_group(grupo))
                lineas.append("")
            self.log_lines(lineas, "Duplicados")

        self.run_async(task)

    def on_quarantine_duplicates(self) -> None:
        """Aísla archivos duplicados redundantes."""
        dups = self._get_cached_data("dups") or []
        if not dups:
            messagebox.showinfo("Sin duplicados", "Primero usá 'Buscar duplicados'.")
            return

        a_mover = []
        for grupo in dups:
            conservar = duplicates_mod.suggest_keeper(grupo)
            a_mover.extend([p for p in grupo.paths if p != conservar])

        aptos = [r for r in a_mover if self._is_safe_path(r)]
        
        if not aptos:
            messagebox.showwarning("Nada que aislar", "Las copias extra están en rutas protegidas.")
            return
        
        if not self._confirm(
            "Aislar copias duplicadas",
            f"Se van a MOVER {len(aptos)} copia(s) segura(s) a la cuarentena.\n\n"
            "No se borran: se pueden restaurar. ¿Seguimos?",
        ):
            return

        def task():
            self.set_status("Aislando copias duplicadas...")
            movidos = 0
            for ruta in aptos:
                quarantine.quarantine_file(ruta, reason="Copia duplicada")
                movidos += 1
            self.log(f"Aisladas {movidos} copia(s). Revisá la pestaña Cuarentena.", "Duplicados")
            self._invalidate_cache("dups")

        self.run_async(task)

    # ------------------------------------------------------------------
    # Lógica de Navegadores y arranque
    # ------------------------------------------------------------------

    def on_browser_report(self) -> None:
        """Genera el reporte de caché de navegadores."""
        def task():
            self.set_status("Midiendo caché de navegadores...")
            self.log_lines(browser.summarize(), "Navegadores")

        self.run_async(task)

    def on_startup_report(self) -> None:
        """Genera el reporte de elementos de inicio."""
        def task():
            self.set_status("Leyendo programas de inicio...")
            # Limpiar caché viejo de inicio si existiera
            self._invalidate_cache("startup")
            self._get_cached("startup", startup_mod.list_startup_entries)
            self.log_lines(startup_mod.summarize(), "Inicio")

        self.run_async(task)

    # ------------------------------------------------------------------
    # Lógica de Informe
    # ------------------------------------------------------------------

    def on_build_report(self) -> None:
        """Compila el informe final con los datos recopilados."""
        def task():
            if not self.report_data:
                self.log_lines(["Todavía no corriste ningún análisis. "
                                "Empezá por la pestaña Salud."], "Informe")
                return
            texto = reporting.build_report(self.report_data)
            self.clear("Informe")
            for linea in texto.splitlines():
                self.log(linea, "Informe")

        self.run_async(task)

    def on_save_report(self, as_markdown: bool) -> None:
        """Guarda el informe en disco."""
        if not self.report_data:
            messagebox.showinfo("Sin datos", "Primero corré algún análisis.")
            return
        extension = ".md" if as_markdown else ".txt"
        destino = filedialog.asksaveasfilename(
            title="Guardar informe",
            defaultextension=extension,
            initialfile=f"informe-omega{extension}",
            filetypes=[("Markdown", "*.md")] if as_markdown else [("Texto", "*.txt")],
        )
        if not destino:
            return

        def task():
            ruta = reporting.save_report(self.report_data, destino, as_markdown=as_markdown)
            self.log(f"Informe guardado en: {ruta}", "Informe")

        self.run_async(task)

    # ------------------------------------------------------------------
    # Lógica de Asistente
    # ------------------------------------------------------------------

    def on_ask_assistant(self, question: Optional[str] = None) -> None:
        """Gestiona la comunicación con el motor de IA."""
        texto = (question or self.question_entry.get()).strip()
        if not texto:
            self.log("Escribí una pregunta o elegí una sugerida.", "Asistente")
            return
        
        if question is None:
            self.question_entry.delete(0, "end")

        def task():
            self.set_status("Consultando al asistente...")
            self.log(f"\n> {texto}", "Asistente")
            respuesta = assistant.ask(texto, self.assistant_context)
            origen = "en línea" if respuesta.is_online else "local"
            self.log(f"[{origen}] {respuesta.text}", "Asistente")
            if respuesta.notice:
                self.log(f"    ({respuesta.notice})", "Asistente")

        self.run_async(task)

    # ------------------------------------------------------------------
    # Lógica de Ajustes
    # ------------------------------------------------------------------

    def _validate_numeric_setting(self, value: Optional[str], default: int) -> int:
        """Valida que una entrada de configuración sea un número entero positivo."""
        if not value or not isinstance(value, str):
            return default
        try:
            val = int(value.strip())
            return val if val > 0 else default
        except (ValueError, TypeError):
            return default

    def _collect_settings(self) -> Dict[str, Any]:
        """Sincroniza el estado visual de los ajustes con el sistema."""
        valores = dict(self.settings)
        for clave, variable in self.setting_vars.items():
            try:
                valores[clave] = variable.get()
            except Exception:
                continue
        
        # Aplicar validación de entradas numéricas antes de guardar
        valores["duplicados_tamano_minimo_kb"] = self._validate_numeric_setting(
            self.min_dup_entry.get() if hasattr(self, 'min_dup_entry') else None, 64
        )
        valores["top_archivos"] = self._validate_numeric_setting(
            self.top_files_entry.get() if hasattr(self, 'top_files_entry') else None, 15
        )
            
        clave_api = self.api_key_entry.get().strip() if hasattr(self, 'api_key_entry') else ""
        if clave_api:
            valores["asistente_clave_api"] = clave_api
        return valores

    def on_save_settings(self) -> None:
        """Persiste los ajustes del usuario."""
        propuestos = self._collect_settings()
        if propuestos.get("asistente_activado") and not self.settings.get("asistente_activado"):
            if not self._confirm(
                "Activar asistente en línea",
                assistant.PRIVACY_NOTICE + "\n\n¿Lo activamos?",
            ):
                self.setting_vars["asistente_activado"].set(False)
                return

        def task():
            self.settings = settings_mod.update(propuestos)
            ruta = settings_mod.settings_path()
            self.log_lines(
                [f"Ajustes guardados en: {ruta}", ""] + settings_mod.describe(),
                "Ajustes",
            )
            self.set_status("Ajustes guardados.")

        self.run_async(task)

    def on_show_settings(self) -> None:
        """Muestra los ajustes actuales en pantalla."""
        def task():
            self.log_lines(settings_mod.describe(), "Ajustes")

        self.run_async(task)

    def on_reset_settings(self) -> None:
        """Restablece los ajustes a los valores de fábrica."""
        if not self._confirm(
            "Restaurar de fábrica",
            "Se van a descartar todos tus ajustes, incluida la clave del "
            "asistente si la guardaste acá.\n\n¿Confirmás?",
        ):
            return

        def task():
            self.settings = settings_mod.reset()
            for clave, variable in self.setting_vars.items():
                try:
                    if clave in settings_mod.DEFAULTS:
                        variable.set(settings_mod.DEFAULTS[clave])
                except Exception:
                    continue
            self.log_lines(["Ajustes restaurados a los valores de fábrica.", ""]
                           + settings_mod.describe(), "Ajustes")

        self.run_async(task)


if __name__ == "__main__":
    app = LimpiezaTotalOmegaApp()
    app.mainloop()
