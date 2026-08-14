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
Se optimizan eventos de redibujo UI y se utiliza gestión de colas de eventos 
para evitar saturación del hilo principal durante el logueo masivo.

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
import threading
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

    def __init__(self) -> None:
        super().__init__()
        self.tabs: Dict[str, ctk.CTkFrame] = {}
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._log_queue: List[Tuple[str, str]] = []
        self._task_lock = threading.Lock()
        self._closing = False
        try:
            self._validate_environment()
            self._init_window_properties()
            self._init_state()
            self._build_layout()
            self.protocol("WM_DELETE_WINDOW", self._on_closing)
        except Exception as e:
            logging.critical("Error fatal al inicializar la aplicación: %s", e)
            raise

    def _on_closing(self) -> None:
        """Cierra la aplicación cancelando tareas pendientes y liberando hilos."""
        self._closing = True
        if self._executor:
            self._executor.shutdown(wait=False)
        self.destroy()

    def _validate_environment(self) -> None:
        """Comprueba permisos de lectura en el home del usuario como requisito base."""
        home = os.path.expanduser("~")
        if not os.path.exists(home) or not os.access(home, os.R_OK):
            raise OSError(f"Directorio de usuario inaccesible: {home}")

    def _init_window_properties(self) -> None:
        """Aplica dimensiones, título y temas visuales definidos en branding.py."""
        self.title(branding.app_title())
        try:
            self.geometry("1120x780")
            self.minsize(980, 680)
        except Exception as e:
            logging.warning("Configuración de geometría ignorada: %s", e)
        try:
            bg_color = branding.color("background")
            if bg_color:
                self.configure(fg_color=bg_color)
        except Exception as e:
            logging.error("Fallo al aplicar colores de branding: %s", e)

    def _init_state(self) -> None:
        """Prepara el pool de hilos, el caché LRU y la configuración persistida."""
        self._cache: OrderedDict = OrderedDict()
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
            logging.error("Fallo al cargar ajustes, reseteando: %s", e)
            self.settings = settings_mod.reset()
            
        self.setting_vars: Dict[str, Any] = {}
        self.outputs: Dict[str, ctk.CTkTextbox] = {}
        self.cards: Dict[str, ctk.CTkLabel] = {}
        self.area_bars: Dict[str, Tuple[ctk.CTkProgressBar, ctk.CTkLabel]] = {}
        
        self._tasks_running = 0
        if self._executor: self._executor.shutdown(wait=False)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        self._debounces: Dict[str, str] = {}
        
    def _debounce_action(self, key: str, delay: int, callback: Callable[[], Any]) -> None:
        """Cancela y reprograma una tarea (debounce) para evitar ejecuciones redundantes."""
        if key in self._debounces:
            try:
                self.after_cancel(self._debounces[key])
            except Exception:
                pass
        if not self._closing:
            self._debounces[key] = self.after(delay, callback)

    def _create_styled_label(self, parent: Any, text: str, style: str, **kwargs: Any) -> ctk.CTkLabel:
        """Crea etiquetas con fuentes y colores normalizados según branding.py."""
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
        """Inicializa una caja de texto con scroll para logs de consola interna por pestaña."""
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
        """Crea un contenedor horizontal transparente para agrupar botones de acción."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(12, 0))
        return row

    def _action(self, parent: ctk.CTk, text: str, command: Callable[[], Any], 
                danger: bool = False, column: int = 0, secondary: bool = False) -> ctk.CTkButton:
        """Crea un botón con colores semánticos (peligro/acción/secundario) según el riesgo."""
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
        """Renderiza texto de ayuda sutil bajo los controles principales."""
        self._create_styled_label(
            parent, text, "caption",
            wraplength=1010, justify="left"
        ).pack(fill="x", padx=14, pady=(10, 0))

    def _menu(self, parent: ctk.CTk, values: List[str], variable: tk.StringVar, 
              command: Optional[Callable[[str], Any]] = None, width: int = 190) -> ctk.CTkOptionMenu:
        """Crea menús desplegables integrados con la paleta visual global."""
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
        """Crea un campo de entrada de texto estilizado."""
        return ctk.CTkEntry(
            parent, width=width, placeholder_text=placeholder,
            fg_color=branding.color("card"),
            border_color=branding.color("border"),
            text_color=branding.color("text"),
            corner_radius=9,
        )

    def _build_layout(self) -> None:
        """Estructura principal de la ventana: cabecera, pestañas y pie de página."""
        self._build_header()
        self._build_tabs_container()
        self._build_footer()

    def _tab_factory(self, name: str) -> None:
        """Invoca el constructor específico (p. ej. _build_tab_salud) según el nombre de la pestaña."""
        method_name = f"_build_tab_{name.lower()}"
        constructor = getattr(self, method_name, None)
        if constructor:
            try:
                constructor()
            except Exception as e:
                logging.error("Fallo crítico en el constructor de la pestaña %s: %s", name, e)
                if name in self.tabs:
                    self._create_styled_label(self.tabs[name], f"Error al cargar: {type(e).__name__}", "caption").pack()

    def _build_tabs_container(self) -> None:
        """Configura el widget tabview y delega la creación de contenido interno por cada pestaña."""
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
            except Exception as e:
                logging.error("Error al construir la pestaña %s: %s", name, e)

    def _build_header(self) -> None:
        """Renderiza la identidad visual de la app: logo, nombre, versión y barra decorativa."""
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
            logging.error("Fallo al renderizar logo: %s", e)

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

        def on_resize(event: tk.Event) -> None:
            self._debounce_action("resize", 100, lambda: (franja.delete("all"), branding.draw_gradient_bar(franja, event.width, 3)))

        franja.bind("<Configure>", on_resize)

    def _build_footer(self) -> None:
        """Crea la barra de estado inferior para mostrar feedback asíncrono al usuario."""
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

    def _build_tab_salud(self) -> None:
        """Construye el dashboard central con indicadores de estado, métricas y el medidor global."""
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
        """Genera una fila de tarjetas numéricas para las categorías principales de salud."""
        metrics = (("basura", "Basura"), ("sospechosos", "Sospechosos"),
                   ("ram", "RAM libre"), ("disco", "Disco libre"))
        for i, (clave, titulo) in enumerate(metrics):
            container.grid_columnconfigure(i, weight=1)
            self.cards[clave] = self._metric_card(container, titulo, i)

    def _metric_card(self, parent: ctk.CTk, title: str, column_idx: int) -> ctk.CTkLabel:
        """Genera un componente de tarjeta visual única para métricas destacadas."""
        tarjeta = ctk.CTkFrame(
            parent, fg_color=branding.color("card"), corner_radius=12,
            border_width=1, border_color=branding.color("border"),
        )
        tarjeta.grid(row=0, column=column_idx, padx=6, sticky="ew")

        valor_label = self._create_styled_label(tarjeta, "-", "accent")
        valor_label.pack(pady=(14, 0))
        self._create_styled_label(tarjeta, title.upper(), "caption").pack(pady=(0, 14))
        return valor_label

    def _build_health_area_bars(self, parent: ctk.CTk) -> None:
        """Estructura las barras de progreso que detallan los componentes del puntaje de salud."""
        area_container = ctk.CTkFrame(parent, fg_color="transparent")
        area_container.grid(row=0, column=1, sticky="ew")
        area_container.grid_columnconfigure(1, weight=1)
        for row_idx, (clave, etiqueta) in enumerate(HEALTH_AREAS):
            self._build_single_health_bar(area_container, clave, etiqueta, row_idx)

    def _build_single_health_bar(self, container: ctk.CTkFrame, clave: str, etiqueta: str, row_idx: int) -> None:
        """Renderiza una barra de salud individual vinculada a un área específica del sistema."""
        self._create_styled_label(container, etiqueta, "body", anchor="w", width=150).grid(row=row_idx, column=0, sticky="w", pady=4)
        
        barra = ctk.CTkProgressBar(
            container, height=9, corner_radius=5,
            fg_color=branding.color("surface_alt"),
            progress_color=branding.color("accent"),
        )
        barra.grid(row=row_idx, column=1, sticky="ew", padx=10, pady=4)
        barra.set(0)
        
        valor_label = self._create_styled_label(container, "-", "caption", width=64, anchor="e")
        valor_label.grid(row=row_idx, column=2, sticky="e", pady=4)
        self.area_bars[clave] = (barra, valor_label)

    def _draw_gauge(self, score: int, grade: str) -> None:
        """Solicita el redibujo del medidor central de salud usando un timer debounce."""
        self._debounce_action("gauge", 50, lambda: self._render_gauge(score, grade))

    def _render_gauge(self, score: int, grade: str) -> None:
        """Dibuja la geometría del medidor circular de salud en el lienzo interno."""
        if self._closing or not hasattr(self, 'gauge') or not self.gauge.winfo_exists():
            return
        
        try:
            self.gauge.delete("all")
            branding.draw_ring(self.gauge, score, size=176, thickness=15)
            
            color_nota = branding.grade_color(grade) if grade != "-" else branding.color("text_dim")
            self.gauge.create_text(88, 78, text=str(score), fill=branding.score_color(score),
                                   font=("Segoe UI", branding.font_size("display"), "bold"))
            self.gauge.create_text(88, 116, text=f"nota {grade}", fill=color_nota,
                                   font=("Segoe UI", branding.font_size("body"), "bold"))
        except Exception as e:
            logging.error("Error al renderizar gauge: %s", e)

    def _build_tab_limpieza(self) -> None:
        """Construye controles para escaneo, revisión y eliminación masiva de archivos basura."""
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
        """Construye controles para escaneos heurísticos y supervisión de Windows Defender."""
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
        """Construye la interfaz de gestión para restaurar o purgar archivos aislados."""
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
        """Construye las herramientas de diagnóstico y optimización del uso de RAM."""
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
        """Construye los reportes de uso de espacio y análisis de árbol de directorios."""
        tab = self.tabs["Disco"]
        row = self._button_row(tab)
        self._action(row, "Espacio por unidad", self.on_drives_report, column=0)
        self._action(row, "Analizar una carpeta", self.on_disk_analysis,
                     secondary=True, column=1)
        self._make_output("Disco", tab)

    def _build_tab_duplicados(self) -> None:
        """Construye controles para búsqueda y gestión de archivos duplicados por hash."""
        tab = self.tabs["Duplicados"]
        row = self._button_row(tab)
        self._action(row, "Buscar duplicados", self.on_find_duplicates, column=0)
        self._action(row, "Aislar copias extra", self.on_quarantine_duplicates,
                     danger=True, column=1)
        self._make_output("Duplicados", tab)

    def _build_tab_navegadores(self) -> None:
        """Construye la vista de detección de cachés de navegadores web instalados."""
        tab = self.tabs["Navegadores"]
        row = self._button_row(tab)
        self._action(row, "Detectar caché", self.on_browser_report, column=0)
        self._make_output("Navegadores", tab)

    def _build_tab_inicio(self) -> None:
        """Construye la lista de aplicaciones y tareas programadas en el inicio del sistema."""
        tab = self.tabs["Inicio"]
        row = self._button_row(tab)
        self._action(row, "Ver programas de inicio", self.on_startup_report, column=0)
        self._make_output("Inicio", tab)

    def _build_tab_informe(self) -> None:
        """Construye controles para generar y exportar reportes unificados de sesión."""
        tab = self.tabs["Informe"]
        row = self._button_row(tab)
        self._action(row, "Armar informe", self.on_build_report, column=0)
        self._action(row, "Guardar como .txt", lambda: self.on_save_report(False),
                     secondary=True, column=1)
        self._action(row, "Guardar como .md", lambda: self.on_save_report(True),
                     secondary=True, column=2)
        self._make_output("Informe", tab)

    def _build_tab_asistente(self) -> None:
        """Construye la interfaz de chat interactiva con el asistente IA local."""
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

    def _add_setting_label(self, parent: ctk.CTkFrame, text: str, row: int, column: int = 0) -> None:
        """Agrega etiqueta descriptiva para campos del formulario de ajustes."""
        self._create_styled_label(parent, text, "body", anchor="w").grid(
            row=row, column=column, sticky="w", padx=(0, 10), pady=6
        )

    def _add_setting_switch(self, parent: ctk.CTkFrame, clave: str, texto: str, row: int, column: int) -> None:
        """Agrega un switch lógico para la configuración de usuario."""
        variable = ctk.BooleanVar(value=bool(self.settings.get(clave)))
        self.setting_vars[clave] = variable
        ctk.CTkSwitch(
            parent, text=texto, variable=variable,
            progress_color=branding.color("accent"),
            button_color=branding.color("text"),
            text_color=branding.color("text"),
            font=ctk.CTkFont(size=branding.font_size("body")),
        ).grid(row=row, column=column, sticky="w", padx=(0, 24), pady=6)

    def _build_tab_ajustes(self) -> None:
        """Construye el formulario de configuración global de la aplicación."""
        tab = self.tabs["Ajustes"]
        row = self._button_row(tab)
        self._action(row, "Guardar ajustes", self.on_save_settings, column=0)
        self._action(row, "Ver configuración", self.on_show_settings, secondary=True, column=1)
        self._action(row, "Restaurar de fábrica", self.on_reset_settings, danger=True, column=2)

        grilla = ctk.CTkFrame(tab, fg_color="transparent")
        grilla.pack(fill="x", padx=12, pady=(14, 0))

        self._add_setting_label(grilla, "Tema:", 0, 0)
        self.setting_vars["tema"] = ctk.StringVar(value=self.settings.get("tema", "oscuro"))
        self._menu(grilla, list(settings_mod.VALID_THEMES), self.setting_vars["tema"], width=150).grid(row=0, column=1, sticky="w")

        self._add_setting_label(grilla, "Acento:", 0, 2)
        self.setting_vars["acento"] = ctk.StringVar(value=self.settings.get("acento", "menta"))
        self._menu(grilla, list(settings_mod.VALID_ACCENTS), self.setting_vars["acento"], width=150).grid(row=0, column=3, sticky="w")

        self._add_setting_switch(grilla, "mostrar_barras", "Barras visuales", 1, 0)
        self._add_setting_switch(grilla, "analisis_en_paralelo", "Análisis en paralelo", 1, 1)
        self._add_setting_switch(grilla, "recordar_ultima_carpeta", "Recordar última carpeta", 1, 2)

        self._add_setting_label(grilla, "Duplicados desde (KB):", 2, 0)
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

    def _is_safe_path(self, path: Union[str, Path]) -> bool:
        """Valida que una ruta no sea enlace simbólico ni esté en zona protegida por sistema."""
        try:
            p = Path(path).resolve(strict=True)
            if p.is_symlink():
                return False
            return not safety.is_protected_path(p)
        except (OSError, RuntimeError, PermissionError):
            return False

    def _is_safe_target_dir(self, path: Union[str, Path]) -> bool:
        """Verifica recursivamente la seguridad de una ruta de directorio."""
        try:
            p = Path(path).resolve(strict=True)
            return p.is_dir() and not safety.is_protected_path(p)
        except (OSError, PermissionError):
            return False

    def _is_valid_dir(self, path: Optional[Union[str, Path]]) -> bool:
        """Verifica si un directorio es existente y legible (lectura pura)."""
        if not path:
            return False
        try:
            p = Path(path).resolve(strict=True)
            return p.is_dir() and os.access(p, os.R_OK)
        except (OSError, PermissionError):
            return False

    def _get_cached_data(self, key: str) -> Any:
        """Wrapper de conveniencia para recuperar datos del caché."""
        return self._get_cached(key)

    def _get_cached(self, key: str, provider: Optional[Callable[[], Any]] = None, force: bool = False) -> Any:
        """Gestiona el caché LRU: verifica TTL y delega a un provider si es necesario."""
        now = time.time()
        if not force and key in self._cache:
            data, timestamp = self._cache[key]
            if now - timestamp < self._cache_ttl:
                return data
            del self._cache[key]
        
        if provider:
            try:
                data = provider()
                if data is not None:
                    if len(self._cache) >= self._cache_max_size:
                        self._cache.popitem(last=False)
                    self._cache[key] = (data, now)
                return data
            except Exception as e:
                logging.error("Error al obtener datos para caché %s: %s", key, e)
        return None

    def _get_cached_or_run(self, key: str, provider: Callable[[], Any], on_complete: Callable[[Any], None]) -> None:
        """Lógica de persistencia temporal: recupera caché o dispara tarea async."""
        cached = self._get_cached(key)
        if cached is not None:
            on_complete(cached)
        else:
            self.run_async(lambda: on_complete(provider()))

    def _invalidate_cache(self, key_prefix: str) -> None:
        """Elimina entradas del caché por prefijo tras cambios en disco."""
        for k in list(self._cache.keys()):
            if k.startswith(key_prefix):
                del self._cache[k]

    def _box(self, tab: str) -> Optional[ctk.CTkTextbox]:
        """Devuelve el widget de texto log de una pestaña específica."""
        return self.outputs.get(tab)

    def log(self, text: str, tab: str = "Limpieza") -> None:
        """Encola logs para ser renderizados de forma segura en UI."""
        self._log_queue.append((tab, text))
        self.after_idle(self._flush_logs)

    def _flush_logs(self) -> None:
        """Vuelca la cola acumulada de mensajes al componente visual de texto."""
        if not self._log_queue or self._closing:
            return
        
        pendientes = list(self._log_queue)
        self._log_queue.clear()
        
        tab_messages = {}
        for tab, text in pendientes:
            tab_messages.setdefault(tab, []).append(text)
            
        for tab, msgs in tab_messages.items():
            box = self._box(tab)
            if box and box.winfo_exists():
                box.insert("end", "\n".join(msgs) + "\n")
                box.see("end")

    def clear(self, tab: str = "Limpieza") -> None:
        """Limpia el contenido del log en la pestaña solicitada."""
        box = self._box(tab)
        if box and box.winfo_exists():
            self.after(0, lambda: box.delete("1.0", "end"))

    def set_status(self, text: str) -> None:
        """Actualiza el texto descriptivo del pie de página."""
        if not self._closing and hasattr(self, 'status') and self.status.winfo_exists():
            self.after_idle(lambda: self.status.configure(text=text))

    def log_lines(self, lines: List[str], tab: str) -> None:
        """Renderiza una lista de líneas en el log de la pestaña dada."""
        self.clear(tab)
        box = self._box(tab)
        if box and box.winfo_exists():
            self.after(0, lambda: (box.insert("1.0", "\n".join(lines)), box.see("1.0")))
        self.report_data[tab.lower()] = list(lines)

    def _set_busy(self, busy: bool) -> None:
        """Gestiona la visibilidad y estado de la barra de progreso."""
        def actualizar() -> None:
            if self._closing or not self.activity.winfo_exists(): return
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

        self.after_idle(actualizar)

    def _validate_and_log_error(self, e: Exception, tab: str) -> None:
        """Traducción de excepciones técnicas a mensajes amigables."""
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

    def _safe_run(self, fn: Callable[[], Any], tab: str) -> None:
        """Ejecuta una tarea controlando excepciones para evitar el cierre de la UI."""
        if self._closing: return
        try:
            fn()
        except Exception as e:
            if not self._closing:
                self._validate_and_log_error(e, tab)

    def run_async(self, fn: Callable[[], Any], check_safety: bool = False, target: Optional[str] = None) -> None:
        """Envía tarea al pool de hilos y garantiza el manejo de bloqueos y seguridad."""
        if self._closing: return
        
        target_path = target or self.scan_target
        if check_safety and target_path and not self._is_safe_target_dir(target_path):
            self.log(f"Abortado: La ruta de destino {target_path} no es segura.", self._current_tab())
            return
            
        self._set_busy(True)
        tab = self._current_tab()
        
        def wrapper() -> None:
            with self._task_lock:
                try:
                    self._safe_run(fn, tab)
                finally:
                    if not self._closing:
                        self._set_busy(False)
                        self.set_status("Listo.")

        if not self._closing and self._executor:
            self._executor.submit(wrapper)

    def _current_tab(self) -> str:
        """Determina qué pestaña está activa para el contexto de logs."""
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
        """Solicita selección de carpeta con filtros de seguridad anti-inyección."""
        folder = filedialog.askdirectory(title=title)
        if not folder or not isinstance(folder, str):
            return None
        
        # Filtro de seguridad: normalizar a absoluta y remover caracteres inusuales
        try:
            clean_path = os.path.abspath(folder)
            clean_path = "".join(c for c in clean_path if ord(c) >= 32 and c not in ('\u202e', '\u202d', '\u202c'))
            p = Path(clean_path).resolve(strict=True)
            
            if not p.is_dir() or not os.access(p, os.R_OK):
                messagebox.showwarning("Ruta inaccesible", "La ruta seleccionada no existe o no se puede leer.")
                return None
            
            safety.ensure_safe_to_modify(p)
            return str(p)
        except (safety.UnsafePathError, OSError, PermissionError, FileNotFoundError):
            messagebox.showwarning("Ruta no segura", "Operación no permitida en esta ruta.")
            return None

    def _confirm(self, title: str, message: str) -> bool:
        """Solicita confirmación explícita para acciones destructivas."""
        return messagebox.askyesno(title, message, icon="warning")

    def _compile_metrics(self) -> Tuple[healthscore.SystemMetrics, memory_mod.Snapshot, diskreport.DriveInfo]:
        """Agrupa métricas de todos los módulos para calcular el puntaje global de salud."""
        hallazgos = self._get_cached("suspicions") or []
        arranque = self._get_cached("startup") or []
        junk = self._get_cached("junk") or []
        dups = self._get_cached("dups") or []

        snapshot = memory_mod.read_snapshot()
        home = os.path.expanduser("~")
        
        disk_info = self._get_cached(f"disk_info_{home}", provider=lambda: diskreport.drive_usage(home) if os.path.exists(home) else None)
        
        metrics = healthscore.SystemMetrics(
            junk_mb=sum(j.size_bytes for j in junk) / (1024 * 1024),
            suspicious_count=len(hallazgos),
            suspicious_warnings=sum(1 for h in hallazgos if h.severity == "warning"),
            memory_available_percent=snapshot.available_percent if snapshot else 100.0,
            disk_free_percent=(disk_info.free / disk_info.total * 100) if (disk_info and disk_info.total > 0) else 100.0,
            duplicate_mb=duplicates_mod.reclaimable_bytes(dups) / (1024 * 1024),
            startup_count=len(arranque),
            quarantined_count=len(quarantine.list_items()),
        )
        return metrics, snapshot or memory_mod.Snapshot(0, 0, 0), disk_info or diskreport.DriveInfo(0, 0, 0, "")

    def on_full_analysis(self) -> None:
        """Dispara el cálculo de salud global y notifica al asistente con el nuevo contexto."""
        def task() -> None:
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
            if not self._get_cached("dups"):
                lineas += ["", "Nota: los duplicados no se contaron todavía. "
                               "Corré la pestaña Duplicados para incluirlos."]
            self.log_lines(lineas, "Salud")
            self.set_status(f"Salud: {resultado.score}/100 (nota {resultado.grade})")

        self.run_async(task)

    def _update_health_visuals(self, resultado: healthscore.ScoreResult, junk_mb: float, 
                               sospechosos: int, ram_libre: float, disco_libre: float) -> None:
        """Actualiza la interfaz del dashboard de salud."""
        state_key = (resultado.score, junk_mb, sospechosos, ram_libre, disco_libre)
        if self._last_health_state == state_key:
            return
        self._last_health_state = state_key

        def actualizar() -> None:
            if self._closing: return
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
                if label.winfo_exists():
                    label.configure(text=valores[clave], text_color=colores[clave])

            for clave, (barra, label) in self.area_bars.items():
                if barra.winfo_exists():
                    puntos = resultado.breakdown.get(clave, 0)
                    maximo = healthscore.WEIGHTS.get(clave, 1)
                    proporcion = puntos / maximo if maximo else 0
                    c = branding.score_color(proporcion * 100)
                    barra.configure(progress_color=c)
                    barra.set(proporcion)
                    label.configure(text=f"{puntos:.0f}/{maximo}", text_color=c)

        self.after_idle(actualizar)

    def on_target_choice_changed(self, choice: str) -> None:
        """Maneja el selector de destinos para el escaneo de basura."""
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
            if self._is_safe_target_dir(choice):
                self.scan_target = choice
                self.target_label.configure(text=f"Unidad completa: {choice}")
            else:
                self.log(f"Error: La ruta {choice} no es válida o es insegura.", "Limpieza")
                self.target_choice.set("Por defecto (Temp + Descargas)")
                self.scan_target = None
                self.target_label.configure(text="")

    def on_scan_junk(self) -> None:
        """Dispara el escaneo de basura y almacena los resultados en caché."""
        def task() -> None:
            destino = self.scan_target or "carpetas por defecto (Temp/Descargas)"
            self.set_status(f"Buscando basura en {destino}...")
            self.clear("Limpieza")
            self.log(f"Buscando archivos basura en: {destino}...", "Limpieza")
            directories = [self.scan_target] if self.scan_target else None
            junk = scan_for_junk(directories)
            
            self._invalidate_cache("junk")
            self._cache["junk"] = (junk, time.time())
            
            total_mb = round(sum(j.size_bytes for j in junk) / (1024 * 1024), 2)
            self.log(f"Encontrados {len(junk)} candidatos ({total_mb} MB).", "Limpieza")
            self.refresh_list()

        self.run_async(task, check_safety=True)

    def refresh_list(self) -> None:
        """Actualiza el listado visual de archivos basura según el filtro de orden seleccionado."""
        junk = self._get_cached("junk") or []
        ordered = sort_junk(junk, by=self.sort_by.get())
        lines = [f"{jf.size_mb:>8} MB  |  {jf.modified:%Y-%m-%d}  |  {jf.path}" for jf in ordered]
        self.report_data["limpieza"] = lines
        box = self._box("Limpieza")
        if box and box.winfo_exists():
            self.after_idle(lambda: (box.delete("1.0", "end"), box.insert("1.0", "\n".join(lines))))

    def on_stage(self) -> None:
        """Mueve candidatos de basura a la zona de revisión segura."""
        junk = self._get_cached("junk") or []
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

        def task() -> None:
            self.set_status("Moviendo a revisión...")
            dest = stage_for_review(aptos)
            self.log(f"Movidos {len(aptos)} archivos a: {dest}", "Limpieza")
            self._invalidate_cache("junk")

        self.run_async(task)

    def on_delete_reviewed(self) -> None:
        """Ejecuta el borrado de archivos revisados tras confirmación explícita."""
        if not self._confirm(
            "Vaciar carpeta de revisión",
            "Esto BORRA de forma permanente los archivos que están en la carpeta "
            "de revisión.\n\nNo se puede deshacer. ¿Confirmás?",
        ):
            return

        def task() -> None:
            self.set_status("Vaciando la carpeta de revisión...")
            n = delete_reviewed()
            self.log(f"Borrados {n} archivos de la carpeta de revisión.", "Limpieza")

        self.run_async(task)

    def _run_heuristic_scan(self, folder: str) -> None:
        """Ejecuta el proceso de escaneo heurístico de seguridad."""
        def task() -> None:
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

        self.run_async(task, check_safety=True, target=folder)

    def on_heuristic_scan(self) -> None:
        """Inicia escaneo heurístico en la carpeta de Descargas."""
        downloads_path = os.path.expanduser("~/Downloads")
        if not os.path.isdir(downloads_path):
            self.log("No se encontró la carpeta de Descargas.", "Seguridad")
            return
        self._run_heuristic_scan(downloads_path)

    def on_heuristic_scan_folder(self) -> None:
        """Inicia escaneo heurístico en una carpeta seleccionada por el usuario."""
        folder = self._ask_folder("Elegí una carpeta para escanear")
        if folder:
            self.scan_target = folder
            self._run_heuristic_scan(folder)

    def on_quarantine_findings(self) -> None:
        """Mueve archivos identificados como sospechosos a la cuarentena."""
        suspicions = self._get_cached("suspicions") or []
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

        def task() -> None:
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
        """Dispara escaneo rápido de Windows Defender."""
        def task() -> None:
            self.set_status("Windows Defender en curso...")
            self.log("Iniciando escaneo rápido de Windows Defender (puede tardar)...", "Seguridad")
            output = run_windows_defender_quick_scan()
            self.log(output, "Seguridad")

        self.run_async(task)

    def on_list_quarantine(self) -> None:
        """Lista el contenido actual almacenado en cuarentena."""
        def task() -> None:
            self.log_lines(quarantine.summarize(), "Cuarentena")

        self.run_async(task)

    def on_restore_quarantine(self) -> None:
        """Restaura un archivo aislado mediante su identificador único."""
        if not hasattr(self, 'quarantine_id') or not self.quarantine_id.winfo_exists():
            return
        
        raw_id = self.quarantine_id.get().strip()
        if not raw_id:
            messagebox.showinfo("Falta el ID", "Pegá el ID del archivo que querés restaurar.")
            return

        def task() -> None:
            try:
                if not quarantine.item_exists(raw_id):
                    self.log(f"Error: El ID '{raw_id}' no existe en la cuarentena.", "Cuarentena")
                    return
                
                item = quarantine.get_item(raw_id)
                if not item or not hasattr(item, 'original_path'):
                    self.log("Error: Manifiesto de cuarentena corrupto.", "Cuarentena")
                    return
                
                if not self._is_safe_path(item.original_path):
                    self.log(f"Error: La ruta original {item.original_path} ahora es insegura.", "Cuarentena")
                    return
                
                destino = quarantine.restore_item(raw_id)
                self.log(f"Restaurado en: {destino}", "Cuarentena")
            except Exception as e:
                self.log(f"Error al intentar restaurar: {e}", "Cuarentena")

        self.run_async(task)

    def on_purge_quarantine(self) -> None:
        """Elimina permanentemente todos los archivos almacenados en cuarentena."""
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

        def task() -> None:
            borrados = quarantine.purge_all()
            self.log(f"Borrados {borrados} archivo(s) de la cuarentena.", "Cuarentena")

        self.run_async(task)

    def on_memory_report(self) -> None:
        """Genera el diagnóstico detallado de memoria RAM."""
        def task() -> None:
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
        """Reporta procesos de mayor consumo de RAM."""
        def task() -> None:
            procesos = memory_mod.top_memory_processes(limit=15)
            if not procesos:
                self.log_lines(["No se pudo obtener la lista de procesos en este sistema."],
                               "Memoria")
                return
            tope = max([p.working_set_mb for p in procesos], default=1) or 1
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
        """Intenta liberar el Working Set del proceso seleccionado."""
        if not hasattr(self, 'pid_entry') or not self.pid_entry.winfo_exists():
            return
            
        raw = self.pid_entry.get().strip()
        if not raw.isdigit():
            messagebox.showwarning("Error", "Ingresá un PID numérico válido.")
            return
        pid = int(raw)

        if pid < 100:
            self.log("Error: PID de sistema protegido.", "Memoria")
            return

        if not self._confirm("Liberar working set", memory_mod.TRIM_WARNING + "\n\n¿Seguimos?"):
            return

        def task() -> None:
            try:
                if not memory_mod.process_exists(pid):
                    self.log(f"Error: El proceso {pid} ya no está activo.", "Memoria")
                    return
                ok, mensaje = memory_mod.trim_working_set(pid)
                self.log(("OK: " if ok else "Sin efecto: ") + mensaje, "Memoria")
            except Exception as e:
                self.log(f"Error al intentar trim en PID {pid}: {e}", "Memoria")

        self.run_async(task)

    def on_drives_report(self) -> None:
        """Genera reporte de uso de disco por unidad."""
        def task() -> None:
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
        """Analiza el uso de disco dentro de una carpeta específica."""
        folder = self._ask_folder("Elegí una carpeta para analizar")
        if not folder:
            return
        self.analysis_folder = folder

        def task() -> None:
            if not self._is_valid_dir(folder):
                self.log("Error: La carpeta seleccionada ya no existe.", "Disco")
                return
            self.set_status(f"Analizando {folder}...")
            self.clear("Disco")
            self.log(f"Analizando {folder} (solo lectura, puede tardar)...", "Disco")
            self.log_lines(diskreport.summarize(folder), "Disco")

        self.run_async(task, check_safety=True, target=folder)

    def on_find_duplicates(self) -> None:
        """Busca y reporta duplicados en la ruta elegida."""
        folder = self._ask_folder("Elegí una carpeta donde buscar duplicados")
        if not folder:
            return

        def task() -> None:
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

        self.run_async(task, check_safety=True, target=folder)

    def on_quarantine_duplicates(self) -> None:
        """Aísla archivos duplicados adicionales en cuarentena."""
        dups = self._get_cached("dups") or []
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

        def task() -> None:
            self.set_status("Aislando copias duplicadas...")
            movidos = 0
            for ruta in aptos:
                quarantine.quarantine_file(ruta, reason="Copia duplicada")
                movidos += 1
            self.log(f"Aisladas {movidos} copia(s). Revisá la pestaña Cuarentena.", "Duplicados")
            self._invalidate_cache("dups")

        self.run_async(task)

    def on_browser_report(self) -> None:
        """Reporta las cachés de los navegadores web detectados."""
        def task() -> None:
            self.set_status("Midiendo caché de navegadores...")
            self.log_lines(browser.summarize(), "Navegadores")

        self.run_async(task)

    def on_startup_report(self) -> None:
        """Reporta las entradas de programas y tareas de inicio."""
        def task() -> None:
            self.set_status("Leyendo programas de inicio...")
            self._invalidate_cache("startup")
            self._get_cached("startup", provider=startup_mod.list_startup_entries)
            self.log_lines(startup_mod.summarize(), "Inicio")

        self.run_async(task)

    def on_build_report(self) -> None:
        """Genera un reporte unificado de la sesión actual."""
        def task() -> None:
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
        """Exporta el reporte actual a un archivo."""
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

        def task() -> None:
            ruta = reporting.save_report(self.report_data, destino, as_markdown=as_markdown)
            self.log(f"Informe guardado en: {ruta}", "Informe")

        self.run_async(task)

    def on_ask_assistant(self, question: Optional[str] = None) -> None:
        """Envía consultas al asistente IA local."""
        texto = (question or (self.question_entry.get() if hasattr(self, 'question_entry') else "")).strip()
        texto = "".join(c for c in texto if c.isprintable())[:500]
        
        if not texto:
            self.log("Escribí una pregunta válida.", "Asistente")
            return
        
        if question is None and hasattr(self, 'question_entry'):
            self.question_entry.delete(0, "end")

        def task() -> None:
            self.set_status("Consultando al asistente...")
            self.log(f"\n> {texto}", "Asistente")
            respuesta = assistant.ask(texto, self.assistant_context)
            origen = "en línea" if respuesta.is_online else "local"
            self.log(f"[{origen}] {respuesta.text}", "Asistente")
            if respuesta.notice:
                self.log(f"    ({respuesta.notice})", "Asistente")

        self.run_async(task)

    def _validate_numeric_setting(self, value: Optional[str], default: int) -> int:
        """Valida que un campo de configuración sea numérico y positivo."""
        if not value:
            return default
        try:
            val = int(value.strip())
            return val if val > 0 else default
        except (ValueError, TypeError):
            return default

    def _collect_settings(self) -> Dict[str, Any]:
        """Recopila y valida la configuración desde el panel de Ajustes."""
        valores = dict(self.settings)
        for clave, variable in self.setting_vars.items():
            try:
                valores[clave] = variable.get()
            except Exception:
                continue
        
        try:
            if hasattr(self, 'min_dup_entry') and self.min_dup_entry.winfo_exists():
                valores["duplicados_tamano_minimo_kb"] = self._validate_numeric_setting(
                    self.min_dup_entry.get(), 64
                )
            
            if hasattr(self, 'top_files_entry') and self.top_files_entry.winfo_exists():
                valores["top_archivos"] = self._validate_numeric_setting(
                    self.top_files_entry.get(), 15
                )
                
            if hasattr(self, 'api_key_entry') and self.api_key_entry.winfo_exists():
                clave_api = "".join(c for c in self.api_key_entry.get().strip() if c.isprintable())
                if clave_api:
                    valores["asistente_clave_api"] = clave_api
        except Exception as e:
            logging.error("Error al recopilar ajustes de la UI: %s", e)
        return valores

    def on_save_settings(self) -> None:
        """Guarda ajustes y solicita confirmación de privacidad al activar el asistente en línea."""
        propuestos = self._collect_settings()
        if propuestos.get("asistente_activado") and not self.settings.get("asistente_activado"):
            if not self._confirm(
                "Activar asistente en línea",
                assistant.PRIVACY_NOTICE + "\n\n¿Lo activamos?",
            ):
                self.setting_vars["asistente_activado"].set(False)
                return

        def task() -> None:
            self.settings = settings_mod.update(propuestos)
            ruta = settings_mod.settings_path()
            self.log_lines(
                [f"Ajustes guardados en: {ruta}", ""] + settings_mod.describe(),
                "Ajustes",
            )
            self.set_status("Ajustes guardados.")

        self.run_async(task)

    def on_show_settings(self) -> None:
        """Despliega en pantalla los ajustes actuales de la aplicación."""
        def task() -> None:
            self.log_lines(settings_mod.describe(), "Ajustes")

        self.run_async(task)

    def on_reset_settings(self) -> None:
        """Resetea los ajustes a los valores de fábrica predeterminados."""
        if not self._confirm(
            "Restaurar de fábrica",
            "Se van a descartar todos tus ajustes, incluida la clave del "
            "asistente si la guardaste acá.\n\n¿Confirmás?",
        ):
            return

        def task() -> None:
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
