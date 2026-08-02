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
Se implementa TTL (Time-To-Live) para asegurar que los datos no queden obsoletos.

Instalar dependencias:
    pip install customtkinter

Ejecutar:
    python main.py
"""

import concurrent.futures
import logging
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any, Callable

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
    """Ventana principal: orquesta la arquitectura de la GUI, el estado 
    compartido y la delegación de tareas pesadas a hilos secundarios.
    """

    def __init__(self):
        super().__init__()
        self._init_window_properties()
        self._init_state()
        self._build_layout()

    def _init_window_properties(self) -> None:
        """Configura los parámetros visuales básicos de la ventana principal."""
        self.title(branding.app_title())
        self.geometry("1120x780")
        self.minsize(980, 680)
        self.configure(fg_color=branding.color("background"))

    def _init_state(self) -> None:
        """Inicializa estructuras de datos volátiles y el pool de hilos para procesamiento."""
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._cache_ttl = 300  # 5 minutos de validez para datos de disco
        self._last_health_state: Optional[Tuple] = None
        self.scan_target: Optional[str] = None
        self.analysis_folder: Optional[str] = None
        self.report_data: Dict[str, List[str]] = {}
        self.assistant_context = assistant.SystemContext()
        
        try:
            self.settings = settings_mod.load()
            if not isinstance(self.settings, dict): 
                raise ValueError("Configuración no es un diccionario")
        except Exception as e:
            logging.error("Fallo al cargar ajustes, reseteando: %s", e)
            self.settings = settings_mod.reset()
            
        self.setting_vars: Dict[str, Any] = {}
        self.outputs: Dict[str, ctk.CTkTextbox] = {}
        self.tabs: Dict[str, ctk.CTkFrame] = {}
        self.cards: Dict[str, ctk.CTkLabel] = {}
        self.area_bars: Dict[str, Tuple[ctk.CTkProgressBar, ctk.CTkLabel]] = {}
        self._tasks_running = 0
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

    def _is_valid_dir(self, path: str) -> bool:
        """Verifica si una ruta es un directorio existente y accesible."""
        try:
            p = Path(path)
            return p.exists() and p.is_dir()
        except Exception:
            return False

    def _get_cached(self, key: str, provider: Optional[Callable] = None, force: bool = False) -> Any:
        """
        Recupera datos del caché si existen y no expiraron; si se provee un proveedor, se invoca.
        """
        now = time.time()
        if not force and key in self._cache:
            data, timestamp = self._cache[key]
            if now - timestamp < self._cache_ttl:
                return data
        
        if provider:
            try:
                data = provider()
                self._cache[key] = (data, now)
                return data
            except Exception as e:
                logging.error("Error al obtener datos para caché %s: %s", key, e)
        return None

    def _invalidate_cache(self, key_prefix: str) -> None:
        """Elimina entradas del caché que comiencen con el prefijo indicado para forzar refresco."""
        keys_to_del = [k for k in self._cache if k.startswith(key_prefix)]
        for k in keys_to_del:
            del self._cache[k]

    # ------------------------------------------------------------------
    # Construcción de la interfaz (GUI Factory Methods)
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        """Ensambla la jerarquía de componentes: encabezado, pestañas y pie de página."""
        self._build_header()
        self._build_tabs_container()
        self._build_footer()

    def _build_tabs_container(self) -> None:
        """
        Inicializa el contenedor de pestañas (Tabview) y delega la 
        construcción de cada pestaña individual a sus métodos correspondientes.
        """
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

        # Mapa de constructores: vincula el nombre de la pestaña con su lógica de construcción
        tab_constructors: Dict[str, Callable] = {
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

        for name in TABS:
            try:
                self.tabs[name] = self.tabview.add(branding.tab_label(name))
                if name in tab_constructors:
                    tab_constructors[name]()
            except Exception as e:
                logging.error("No se pudo construir la pestaña %s: %s", name, e)

        self.output = self.outputs.get("Limpieza")

    def _build_header(self) -> None:
        """Genera el encabezado con el logo, branding y la barra decorativa."""
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(16, 0))

        canvas = tk.Canvas(
            header, width=72, height=72,
            bg=branding.color("background"),
            highlightthickness=0, bd=0,
        )
        canvas.grid(row=0, column=0, rowspan=2, padx=(0, 16))
        branding.draw_logo(canvas, size=72)

        ctk.CTkLabel(
            header, text=branding.APP_NAME,
            font=ctk.CTkFont(size=branding.font_size("title"), weight="bold"),
            text_color=branding.color("text"),
        ).grid(row=0, column=1, sticky="sw")

        ctk.CTkLabel(
            header, text=branding.APP_TAGLINE,
            font=ctk.CTkFont(size=branding.font_size("subtitle")),
            text_color=branding.color("text_muted"),
        ).grid(row=1, column=1, sticky="nw")

        ctk.CTkLabel(
            header, text=f"  v{branding.APP_VERSION}  ",
            font=ctk.CTkFont(size=branding.font_size("caption"), weight="bold"),
            text_color=branding.color("background"),
            fg_color=branding.color("accent"), corner_radius=9,
        ).grid(row=0, column=2, sticky="e", padx=(16, 0))
        header.grid_columnconfigure(2, weight=1)

        franja = tk.Canvas(self, height=3, bg=branding.color("background"),
                           highlightthickness=0, bd=0)
        franja.pack(fill="x", padx=18, pady=(12, 6))
        franja.bind(
            "<Configure>",
            lambda e, c=franja: (c.delete("all"), branding.draw_gradient_bar(c, e.width, 3)),
        )

    def _build_footer(self) -> None:
        """Crea la barra de estado inferior que indica actividad asíncrona mediante un indicador visual."""
        pie = ctk.CTkFrame(self, fg_color="transparent")
        pie.pack(fill="x", padx=18, pady=(0, 12))

        self.status = ctk.CTkLabel(
            pie, text="Listo. Nada se borra sin tu confirmación.",
            text_color=branding.color("text_muted"),
            font=ctk.CTkFont(size=branding.font_size("caption")),
            anchor="w",
        )
        self.status.pack(side="left")

        self.activity = ctk.CTkProgressBar(
            pie, width=170, height=6, mode="indeterminate",
            fg_color=branding.color("surface_alt"),
            progress_color=branding.color("accent"),
        )
        self.activity.pack(side="right")
        self.activity.pack_forget()

    def _make_output(self, tab_name: str, parent: ctk.CTk) -> ctk.CTkTextbox:
        """Crea el cuadro de texto para logs en una pestaña específica."""
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
        """Genera un contenedor de fila para agrupar botones de acción de forma uniforme."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(12, 0))
        return row

    def _action(self, parent: ctk.CTk, text: str, command: Callable, 
                danger: bool = False, column: int = 0, secondary: bool = False) -> ctk.CTkButton:
        """Factory de botones con colores semánticos según su impacto (peligro/acción)."""
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
        """Etiqueta informativa sutil para guiar al usuario en el uso de la pestaña."""
        ctk.CTkLabel(
            parent, text=text, text_color=branding.color("text_muted"),
            font=ctk.CTkFont(size=branding.font_size("caption")),
            wraplength=1010, justify="left",
        ).pack(fill="x", padx=14, pady=(10, 0))

    def _menu(self, parent: ctk.CTk, values: List[str], variable: tk.StringVar, 
              command: Optional[Callable] = None, width: int = 190) -> ctk.CTkOptionMenu:
        """Crea un selector desplegable con el estilo corporativo aplicado."""
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
        """Input field para datos de usuario con validación de estilo."""
        return ctk.CTkEntry(
            parent, width=width, placeholder_text=placeholder,
            fg_color=branding.color("card"),
            border_color=branding.color("border"),
            text_color=branding.color("text"),
            corner_radius=9,
        )

    # -- Métodos de Construcción de Pestañas Específicas ------------------

    def _build_tab_salud(self) -> None:
        """Construye la interfaz de la pestaña Salud."""
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
        """Crea tarjetas de resumen (métricas numéricas rápidas)."""
        metrics = (("basura", "Basura"), ("sospechosos", "Sospechosos"),
                   ("ram", "RAM libre"), ("disco", "Disco libre"))
        for i, (clave, titulo) in enumerate(metrics):
            container.grid_columnconfigure(i, weight=1)
            self.cards[clave] = self._metric_card(container, titulo, i)

    def _build_health_area_bars(self, parent: ctk.CTk) -> None:
        """Crea las barras de progreso proporcionales para el desglosado de salud."""
        area_container = ctk.CTkFrame(parent, fg_color="transparent")
        area_container.grid(row=0, column=1, sticky="ew")
        area_container.grid_columnconfigure(1, weight=1)
        for fila, (clave, etiqueta) in enumerate(HEALTH_AREAS):
            self._build_single_health_bar(area_container, clave, etiqueta, fila)

    def _build_single_health_bar(self, container: ctk.CTkFrame, clave: str, etiqueta: str, fila: int) -> None:
        """Renderiza una única barra de progreso de salud."""
        ctk.CTkLabel(
            container, text=etiqueta, anchor="w", width=150,
            text_color=branding.color("text_muted"),
            font=ctk.CTkFont(size=branding.font_size("body")),
        ).grid(row=fila, column=0, sticky="w", pady=4)
        
        barra = ctk.CTkProgressBar(
            container, height=9, corner_radius=5,
            fg_color=branding.color("surface_alt"),
            progress_color=branding.color("accent"),
        )
        barra.grid(row=fila, column=1, sticky="ew", padx=10, pady=4)
        barra.set(0)
        
        valor_label = ctk.CTkLabel(
            container, text="-", width=64, anchor="e",
            text_color=branding.color("text"),
            font=ctk.CTkFont(size=branding.font_size("caption"), weight="bold"),
        )
        valor_label.grid(row=fila, column=2, sticky="e", pady=4)
        self.area_bars[clave] = (barra, valor_label)

    def _metric_card(self, parent: ctk.CTk, title: str, column: int) -> ctk.CTkLabel:
        """Crea una tarjeta UI con valor numérico y etiqueta inferior."""
        tarjeta = ctk.CTkFrame(
            parent, fg_color=branding.color("card"), corner_radius=12,
            border_width=1, border_color=branding.color("border"),
        )
        tarjeta.grid(row=0, column=column, padx=6, sticky="ew")

        valor_label = ctk.CTkLabel(
            tarjeta, text="-",
            font=ctk.CTkFont(size=branding.font_size("title"), weight="bold"),
            text_color=branding.color("accent"),
        )
        valor_label.pack(pady=(14, 0))
        ctk.CTkLabel(
            tarjeta, text=title.upper(),
            font=ctk.CTkFont(size=branding.font_size("caption")),
            text_color=branding.color("text_dim"),
        ).pack(pady=(0, 14))
        return valor_label

    def _draw_gauge(self, score: int, grade: str) -> None:
        """Solicita el redibujado de la interfaz circular de salud en el hilo principal."""
        def update_canvas():
            if not self.gauge.winfo_exists(): return
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
        """Construye la interfaz de la pestaña Limpieza."""
        tab = self.tabs["Limpieza"]
        row = self._button_row(tab)
        self._action(row, "Buscar basura", self.on_scan_junk, column=0)
        self._action(row, "Mover a revisión", self.on_stage, secondary=True, column=1)
        self._action(row, "Vaciar revisados", self.on_delete_reviewed, danger=True, column=2)

        options_container = ctk.CTkFrame(tab, fg_color="transparent")
        options_container.pack(fill="x", padx=12, pady=(12, 0))

        ctk.CTkLabel(options_container, text="Buscar en:", text_color=branding.color("text_muted")).grid(
            row=0, column=0, padx=(0, 8))
        drive_options = ["Por defecto (Temp + Descargas)"] + list_available_drives() + ["Elegir carpeta..."]
        self.target_choice = ctk.StringVar(value=drive_options[0])
        self._menu(options_container, drive_options, self.target_choice,
                   self.on_target_choice_changed, width=240).grid(row=0, column=1, padx=4)

        self.target_label = ctk.CTkLabel(options_container, text="",
                                         text_color=branding.color("accent"))
        self.target_label.grid(row=0, column=2, padx=10)

        ctk.CTkLabel(options_container, text="Ordenar por:",
                     text_color=branding.color("text_muted")).grid(row=0, column=3, padx=(20, 8))
        self.sort_by = ctk.StringVar(value="size")
        self._menu(options_container, ["size", "date"], self.sort_by,
                   lambda _: self.refresh_list(), width=110).grid(row=0, column=4, padx=4)

        self._make_output("Limpieza", tab)

    def _build_tab_seguridad(self) -> None:
        """Construye la interfaz de la pestaña Seguridad."""
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
        """Construye la interfaz de la pestaña Cuarentena."""
        tab = self.tabs["Cuarentena"]
        row = self._button_row(tab)
        self._action(row, "Ver cuarentena", self.on_list_quarantine, column=0)
        self._action(row, "Restaurar por ID", self.on_restore_quarantine,
                     secondary=True, column=1)
        self._action(row, "Vaciar cuarentena", self.on_purge_quarantine,
                     danger=True, column=2)

        id_container = ctk.CTkFrame(tab, fg_color="transparent")
        id_container.pack(fill="x", padx=12, pady=(12, 0))
        ctk.CTkLabel(id_container, text="ID a restaurar:",
                     text_color=branding.color("text_muted")).grid(row=0, column=0, padx=(0, 8))
        self.quarantine_id = self._entry(id_container, "pegá el ID que ves en la lista", 240)
        self.quarantine_id.grid(row=0, column=1, padx=4)
        self._make_output("Cuarentena", tab)

    def _build_tab_memoria(self) -> None:
        """Construye la interfaz de la pestaña Memoria."""
        tab = self.tabs["Memoria"]
        row = self._button_row(tab)
        self._action(row, "Diagnóstico de RAM", self.on_memory_report, column=0)
        self._action(row, "Procesos que más consumen", self.on_memory_processes,
                     secondary=True, column=1)
        self._action(row, "Liberar working set (PID)", self.on_trim_process,
                     danger=True, column=2)

        pid_container = ctk.CTkFrame(tab, fg_color="transparent")
        pid_container.pack(fill="x", padx=12, pady=(12, 0))
        ctk.CTkLabel(pid_container, text="PID:",
                     text_color=branding.color("text_muted")).grid(row=0, column=0, padx=(0, 8))
        self.pid_entry = self._entry(pid_container, "ej. 4812", 140)
        self.pid_entry.grid(row=0, column=1, padx=4)
        self._make_output("Memoria", tab)

    def _build_tab_disco(self) -> None:
        """Construye la interfaz de la pestaña Disco."""
        tab = self.tabs["Disco"]
        row = self._button_row(tab)
        self._action(row, "Espacio por unidad", self.on_drives_report, column=0)
        self._action(row, "Analizar una carpeta", self.on_disk_analysis,
                     secondary=True, column=1)
        self._make_output("Disco", tab)

    def _build_tab_duplicados(self) -> None:
        """Construye la interfaz de la pestaña Duplicados."""
        tab = self.tabs["Duplicados"]
        row = self._button_row(tab)
        self._action(row, "Buscar duplicados", self.on_find_duplicates, column=0)
        self._action(row, "Aislar copias extra", self.on_quarantine_duplicates,
                     danger=True, column=1)
        self._make_output("Duplicados", tab)

    def _build_tab_navegadores(self) -> None:
        """Construye la interfaz de la pestaña Navegadores."""
        tab = self.tabs["Navegadores"]
        row = self._button_row(tab)
        self._action(row, "Detectar caché", self.on_browser_report, column=0)
        self._make_output("Navegadores", tab)

    def _build_tab_inicio(self) -> None:
        """Construye la interfaz de la pestaña Inicio."""
        tab = self.tabs["Inicio"]
        row = self._button_row(tab)
        self._action(row, "Ver programas de inicio", self.on_startup_report, column=0)
        self._make_output("Inicio", tab)

    def _build_tab_informe(self) -> None:
        """Construye la interfaz de la pestaña Informe."""
        tab = self.tabs["Informe"]
        row = self._button_row(tab)
        self._action(row, "Armar informe", self.on_build_report, column=0)
        self._action(row, "Guardar como .txt", lambda: self.on_save_report(False),
                     secondary=True, column=1)
        self._action(row, "Guardar como .md", lambda: self.on_save_report(True),
                     secondary=True, column=2)
        self._make_output("Informe", tab)

    def _build_tab_asistente(self) -> None:
        """Construye la interfaz de la pestaña Asistente."""
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

    def _build_tab_ajustes(self) -> None:
        """Construye la interfaz de la pestaña Ajustes."""
        tab = self.tabs["Ajustes"]
        row = self._button_row(tab)
        self._action(row, "Guardar ajustes", self.on_save_settings, column=0)
        self._action(row, "Ver configuración", self.on_show_settings,
                     secondary=True, column=1)
        self._action(row, "Restaurar de fábrica", self.on_reset_settings,
                     danger=True, column=2)

        grilla = ctk.CTkFrame(tab, fg_color="transparent")
        grilla.pack(fill="x", padx=12, pady=(14, 0))

        def etiqueta(texto: str, fila: int, columna: int = 0):
            ctk.CTkLabel(grilla, text=texto, anchor="w",
                         text_color=branding.color("text_muted"),
                         font=ctk.CTkFont(size=branding.font_size("body"))
                         ).grid(row=fila, column=columna, sticky="w", padx=(0, 10), pady=6)

        def interruptor(clave: str, texto: str, fila: int, columna: int):
            variable = ctk.BooleanVar(value=bool(self.settings.get(clave)))
            self.setting_vars[clave] = variable
            ctk.CTkSwitch(
                grilla, text=texto, variable=variable,
                progress_color=branding.color("accent"),
                button_color=branding.color("text"),
                text_color=branding.color("text"),
                font=ctk.CTkFont(size=branding.font_size("body")),
            ).grid(row=fila, column=columna, sticky="w", padx=(0, 24), pady=6)

        etiqueta("Tema:", 0)
        self.setting_vars["tema"] = ctk.StringVar(value=self.settings.get("tema", "oscuro"))
        self._menu(grilla, list(settings_mod.VALID_THEMES),
                   self.setting_vars["tema"], width=150).grid(row=0, column=1, sticky="w")

        etiqueta("Acento:", 0, 2)
        self.setting_vars["acento"] = ctk.StringVar(value=self.settings.get("acento", "menta"))
        self._menu(grilla, list(settings_mod.VALID_ACCENTS),
                   self.setting_vars["acento"], width=150).grid(row=0, column=3, sticky="w")

        interruptor("mostrar_barras", "Barras visuales", 1, 0)
        interruptor("analisis_en_paralelo", "Análisis en paralelo", 1, 1)
        interruptor("recordar_ultima_carpeta", "Recordar última carpeta", 1, 2)

        etiqueta("Duplicados desde (KB):", 2)
        self.min_dup_entry = self._entry(grilla, "64", 100)
        self.min_dup_entry.insert(0, str(self.settings.get("duplicados_tamano_minimo_kb", 64)))
        self.min_dup_entry.grid(row=2, column=1, sticky="w")

        etiqueta("Top de archivos:", 2, 2)
        self.top_files_entry = self._entry(grilla, "15", 100)
        self.top_files_entry.insert(0, str(self.settings.get("top_archivos", 15)))
        self.top_files_entry.grid(row=2, column=3, sticky="w")

        ctk.CTkLabel(
            tab, text=f"{branding.icon('Asistente')}  Asistente en línea (opcional)",
            anchor="w", text_color=branding.color("accent2"),
            font=ctk.CTkFont(size=branding.font_size("heading"), weight="bold"),
        ).pack(fill="x", padx=14, pady=(18, 0))

        ia_container = ctk.CTkFrame(tab, fg_color="transparent")
        ia_container.pack(fill="x", padx=12, pady=(6, 0))

        self.setting_vars["asistente_activado"] = ctk.BooleanVar(
            value=bool(self.settings.get("asistente_activado")))
        ctk.CTkSwitch(
            ia_container, text="Activar asistente en línea",
            variable=self.setting_vars["asistente_activado"],
            progress_color=branding.color("accent2"),
            button_color=branding.color("text"),
            text_color=branding.color("text"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 20), pady=6)

        ctk.CTkLabel(ia_container, text="Clave de API:",
                     text_color=branding.color("text_muted")).grid(row=0, column=1, padx=(0, 8))
        self.api_key_entry = self._entry(ia_container, f"vacío = usar {settings_mod.API_KEY_ENV_VAR}", 260)
        self.api_key_entry.configure(show="*")
        self.api_key_entry.grid(row=0, column=2, sticky="w")
        self._make_output("Ajustes", tab)

    # ------------------------------------------------------------------
    # Utilidades generales
    # ------------------------------------------------------------------

    def _box(self, tab: str) -> ctk.CTkTextbox:
        """Helper para recuperar el textbox de log específico de una pestaña."""
        return self.outputs.get(tab) or self.outputs["Limpieza"]

    def log(self, text: str, tab: str = "Limpieza") -> None:
        """Escribe en el log de una pestaña (thread-safe mediante `after`)."""
        box = self._box(tab)

        def append():
            try:
                box.insert("end", f"{text}\n")
                box.see("end")
            except Exception:
                pass

        self.after(0, append)

    def clear(self, tab: str = "Limpieza") -> None:
        """Vacía el contenido de texto del log de una pestaña."""
        box = self._box(tab)
        self.after(0, lambda: box.delete("1.0", "end"))

    def set_status(self, text: str) -> None:
        """Actualiza el mensaje de estado en la barra inferior."""
        self.after(0, lambda: self.status.configure(text=text))

    def log_lines(self, lines: List[str], tab: str) -> None:
        """Escribe múltiples líneas en el log y actualiza el reporte de sesión."""
        self.clear(tab)
        box = self._box(tab)
        self.after(0, lambda: (box.insert("1.0", "\n".join(lines)), box.see("1.0")))
        self.report_data[tab.lower()] = list(lines)

    def _set_busy(self, busy: bool) -> None:
        """Actualiza la barra de progreso global; gestiona el contador de tareas concurrentes."""
        def actualizar():
            self._tasks_running = max(0, self._tasks_running + (1 if busy else -1))
            if self._tasks_running > 0:
                self.activity.pack(side="right")
                self.activity.start()
            else:
                self.activity.stop()
                self.activity.pack_forget()

        self.after(0, actualizar)

    def _validate_and_log_error(self, e: Exception, tab: str) -> None:
        """Handler centralizado de excepciones (logs internos y reporte en GUI)."""
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
        """Ejecuta una tarea de forma segura capturando cualquier excepción."""
        try:
            fn()
        except Exception as e:
            self._validate_and_log_error(e, tab)

    def run_async(self, fn: Callable) -> None:
        """Ejecuta un proceso en un hilo, delegando errores al handler central."""
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
        """Determina la pestaña activa para dirigir los logs de error."""
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
        """Diálogo de selección de carpeta con filtrado de seguridad estricto."""
        folder = filedialog.askdirectory(title=title)
        if not folder:
            return None
        
        path_obj = Path(folder)
        if not path_obj.exists():
            return None
        
        # Validar que no sea ruta de sistema antes de retornar
        try:
            safety.ensure_safe_to_modify(path_obj)
        except (safety.UnsafePathError, PermissionError):
            messagebox.showwarning(
                "Carpeta protegida o inaccesible",
                "Esa carpeta es vital para el sistema o requiere permisos elevados.\n\n"
                "Elegí una carpeta de usuario (Descargas, Documentos, etc.).",
            )
            return None
            
        return folder

    def _confirm(self, title: str, message: str) -> bool:
        """Diálogo de confirmación modal para acciones destructivas."""
        return messagebox.askyesno(title, message, icon="warning")

    # ------------------------------------------------------------------
    # Lógica de Salud
    # ------------------------------------------------------------------

    def _compile_metrics(self) -> Tuple[healthscore.SystemMetrics, memory_mod.Snapshot, diskreport.DriveInfo]:
        """Agrega los datos de todos los módulos para el análisis consolidado de salud."""
        hallazgos = self._get_cached("suspicions") or []
        snapshot = memory_mod.read_snapshot()
        home = os.path.expanduser("~")
        unidad = diskreport.drive_usage(home) if os.path.exists(home) else None
        
        arranque = self._get_cached("startup") or []
        junk = self._get_cached("junk") or []
        dups = self._get_cached("dups") or []

        junk_mb = sum(j.size_bytes for j in junk) / (1024 * 1024)
        advertencias = sum(1 for h in hallazgos if h.severity == "warning")
        libre_pct = (unidad.free / unidad.total * 100) if unidad and unidad.total else 100.0
        en_cuarentena = quarantine.list_items()
        duplicado_mb = duplicates_mod.reclaimable_bytes(dups) / (1024 * 1024)

        metrics = healthscore.SystemMetrics(
            junk_mb=junk_mb,
            suspicious_count=len(hallazgos),
            suspicious_warnings=advertencias,
            memory_available_percent=snapshot.available_percent,
            disk_free_percent=libre_pct,
            duplicate_mb=duplicado_mb,
            startup_count=len(arranque),
            quarantined_count=len(en_cuarentena),
        )
        return metrics, snapshot, unidad or diskreport.DriveInfo(0, 0, 0, "")

    def on_full_analysis(self) -> None:
        """Ejecuta el análisis de salud completo en segundo plano."""
        def task():
            self.set_status("Analizando el sistema...")
            self.clear("Salud")
            self.log("Analizando... esto no modifica nada.", "Salud")

            metrics, snapshot, unidad = self._compile_metrics()
            resultado = healthscore.compute_score(metrics)

            self.assistant_context = assistant.build_context(
                metrics=metrics, health=resultado,
                memory_total_gb=snapshot.total / (1024 ** 3) if snapshot.total else 0.0,
            )

            self._update_health_visuals(
                resultado, metrics.junk_mb, metrics.suspicious_count,
                snapshot.available_percent, metrics.disk_free_percent
            )

            lineas = healthscore.summarize(resultado)
            if not self._cache.get("dups"):
                lineas += ["", "Nota: los duplicados no se contaron todavía. "
                               "Corré la pestaña Duplicados para incluirlos."]
            self.log_lines(lineas, "Salud")
            self.set_status(f"Salud: {resultado.score}/100 (nota {resultado.grade})")

        self.run_async(task)

    def _update_health_visuals(self, resultado: healthscore.ScoreResult, junk_mb: float, 
                               sospechosos: int, ram_libre: float, disco_libre: float) -> None:
        """Sincroniza los valores calculados con la representación visual (indicadores)."""
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
                label.configure(text=valores.get(clave, "-"),
                                text_color=colores.get(clave, branding.color("accent")))

            for clave, (barra, label) in self.area_bars.items():
                puntos = resultado.breakdown.get(clave, 0)
                maximo = healthscore.WEIGHTS.get(clave, 1)
                proporcion = puntos / maximo if maximo else 0
                barra.set(proporcion)
                barra.configure(progress_color=branding.score_color(proporcion * 100))
                label.configure(text=f"{puntos:.0f}/{maximo}",
                                text_color=branding.score_color(proporcion * 100))

        self.after(0, actualizar)

    # ------------------------------------------------------------------
    # Lógica de Limpieza
    # ------------------------------------------------------------------

    def on_target_choice_changed(self, choice: str) -> None:
        """Maneja cambios de contexto de escaneo en la pestaña de Limpieza."""
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
        """Inicia el proceso de recolección de archivos basura."""
        def task():
            destino = self.scan_target or "carpetas por defecto (Temp/Descargas)"
            self.set_status(f"Buscando basura en {destino}...")
            self.clear("Limpieza")
            self.log(f"Buscando archivos basura en: {destino}...", "Limpieza")
            directories = [self.scan_target] if self.scan_target else None
            junk = scan_for_junk(directories)
            self._cache["junk"] = (junk, time.time())
            total_mb = round(sum(j.size_bytes for j in junk) / (1024 * 1024), 2)
            self.log(f"Encontrados {len(junk)} candidatos ({total_mb} MB).", "Limpieza")
            self.refresh_list()

        self.run_async(task)

    def refresh_list(self) -> None:
        """Ordena y renderiza la lista de candidatos según selección del usuario."""
        junk = self._get_cached("junk") or []
        ordered = sort_junk(junk, by=self.sort_by.get())
        lines = [f"{jf.size_mb:>8} MB  |  {jf.modified:%Y-%m-%d}  |  {jf.path}" for jf in ordered]
        self.report_data["limpieza"] = lines
        box = self._box("Limpieza")
        self.after(0, lambda: (box.delete("1.0", "end"), box.insert("1.0", "\n".join(lines))))

    def on_stage(self) -> None:
        """Prepara archivos seguros para revisión eliminando basura en rutas bloqueadas."""
        junk = self._get_cached("junk") or []
        if not junk:
            messagebox.showinfo("Sin candidatos", "Primero usá 'Buscar basura'.")
            return
        
        aptos = [jf for jf in junk if safety.is_safe_to_modify(Path(jf.path))]
        
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
            self._cache["junk"] = ([j for j in junk if j not in aptos], time.time())
            self._invalidate_cache("junk")

        self.run_async(task)

    def on_delete_reviewed(self) -> None:
        """Ejecuta la eliminación permanente de archivos confirmados."""
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
        """Wrapper para ejecutar escaneos heurísticos con validaciones de existencia."""
        def task():
            if not self._is_valid_dir(folder):
                self.log(f"Error: La carpeta {folder} no es accesible.", "Seguridad")
                return
            
            self.set_status(f"Escaneando {folder}...")
            self.clear("Seguridad")
            self.log(f"Escaneo heurístico en: {folder}", "Seguridad")
            
            results = scan_directory(folder)
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
        """Escaneo heurístico por defecto (Descargas)."""
        downloads_path = os.path.expanduser("~/Downloads")
        if not os.path.isdir(downloads_path):
            self.log("No se encontró la carpeta de Descargas.", "Seguridad")
            return
        self._run_heuristic_scan(downloads_path)

    def on_heuristic_scan_folder(self) -> None:
        """Escaneo heurístico en carpeta seleccionada."""
        folder = self._ask_folder("Elegí una carpeta para escanear")
        if folder:
            self._run_heuristic_scan(folder)

    def on_quarantine_findings(self) -> None:
        """Aísla archivos sospechosos en el módulo de cuarentena."""
        suspicions = self._get_cached("suspicions") or []
        if not suspicions:
            messagebox.showinfo("Sin hallazgos", "Primero corré un escaneo heurístico.")
            return
        
        aptos = [s for s in suspicions if safety.is_safe_to_modify(Path(s.path))]
        
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
            self._cache["suspicions"] = ([s for s in suspicions if s not in aptos], time.time())
            self._invalidate_cache("suspicions")

        self.run_async(task)

    def on_defender_scan(self) -> None:
        """Dispara un escaneo rápido nativo con Windows Defender."""
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
        """Lista los archivos aislados."""
        def task():
            self.log_lines(quarantine.summarize(), "Cuarentena")

        self.run_async(task)

    def on_restore_quarantine(self) -> None:
        """Restaura un archivo aislado a su ruta original tras validaciones de seguridad."""
        raw_id = self.quarantine_id.get().strip()
        if not raw_id:
            messagebox.showinfo("Falta el ID", "Pegá el ID del archivo que querés restaurar.")
            return

        # Validación estricta del ID
        if not raw_id.isalnum():
             messagebox.showerror("Error", "El ID debe ser alfanumérico.")
             return

        def task():
            if not quarantine.item_exists(raw_id):
                self.log(f"Error: El ID '{raw_id}' no existe en la cuarentena.", "Cuarentena")
                return
            
            item = quarantine.get_item(raw_id)
            ruta_orig = Path(item.original_path)
            
            # Validación de seguridad centralizada
            safety.ensure_safe_to_modify(ruta_orig)
            
            destino = quarantine.restore_item(raw_id)
            self.log(f"Restaurado en: {destino}", "Cuarentena")

        self.run_async(task)

    def on_purge_quarantine(self) -> None:
        """Elimina permanentemente el contenido de la cuarentena."""
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
        """Ejecuta un diagnóstico rápido del uso de RAM."""
        def task():
            snapshot = memory_mod.read_snapshot()
            procesos = memory_mod.top_memory_processes(limit=5)
            lineas = memory_mod.diagnose(snapshot, procesos)
            if snapshot.total:
                lineas = [
                    f"Uso de memoria  {branding.bar(snapshot.used_percent, 30)}  "
                    f"{snapshot.used_percent:.0f}%",
                    "",
                ] + lineas
            self.log_lines(lineas, "Memoria")

        self.run_async(task)

    def on_memory_processes(self) -> None:
        """Lista los procesos que más RAM consumen."""
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
        """Solicita la liberación de memoria de un proceso por PID."""
        raw = self.pid_entry.get().strip()
        if not raw:
            messagebox.showwarning("PID vacío", "Ingresá un PID.")
            return
        if not raw.isdigit():
            messagebox.showwarning("PID inválido", "El PID debe ser un número entero.")
            return
        
        pid = int(raw)
        if pid <= 0:
            messagebox.showwarning("PID inválido", "El PID debe ser mayor a 0.")
            return
        
        # Seguridad: Validación centralizada vía path ficticio para evitar procesos protegidos
        try:
            safety.ensure_safe_to_modify(Path(f"PROCESS_PID_{pid}"))
        except safety.UnsafePathError:
            messagebox.showwarning("Acción denegada", "Ese proceso es crítico para el sistema.")
            return

        if not self._confirm("Liberar working set", memory_mod.TRIM_WARNING + "\n\n¿Seguimos?"):
            return

        def task():
            ok, mensaje = memory_mod.trim_working_set(pid)
            self.log(("OK: " if ok else "Sin efecto: ") + mensaje, "Memoria")

        self.run_async(task)

    # ------------------------------------------------------------------
    # Lógica de Disco
    # ------------------------------------------------------------------

    def on_drives_report(self) -> None:
        """Genera un resumen de espacio en todas las unidades."""
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
        """Realiza un escaneo profundo de una carpeta específica."""
        folder = self._ask_folder("Elegí una carpeta para analizar")
        if not folder:
            return
        self.analysis_folder = folder

        def task():
            # Volver a verificar existencia antes de procesar
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
        """Busca archivos duplicados en la ubicación especificada."""
        folder = self._ask_folder("Elegí una carpeta donde buscar duplicados")
        if not folder:
            return

        def task():
            # Verificación redundante de seguridad/existencia
            if not self._is_valid_dir(folder):
                self.log("Error: La carpeta seleccionada ya no existe.", "Duplicados")
                return
            self.set_status(f"Buscando duplicados en {folder}...")
            self.clear("Duplicados")
            self.log(f"Buscando duplicados en {folder} (solo lectura, puede tardar)...",
                     "Duplicados")
            dups = duplicates_mod.find_duplicates([folder])
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
        """Mueve archivos duplicados excedentes a la cuarentena."""
        dups = self._get_cached("dups") or []
        if not dups:
            messagebox.showinfo("Sin duplicados", "Primero usá 'Buscar duplicados'.")
            return

        a_mover = []
        for grupo in dups:
            conservar = duplicates_mod.suggest_keeper(grupo)
            a_mover.extend([p for p in grupo.paths if p != conservar])

        aptos = [r for r in a_mover if safety.is_safe_to_modify(Path(r))]
        
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
            self._cache["dups"] = ([], time.time())
            self._invalidate_cache("dups")

        self.run_async(task)

    # ------------------------------------------------------------------
    # Lógica de Navegadores y arranque
    # ------------------------------------------------------------------

    def on_browser_report(self) -> None:
        """Genera reporte de caché de los navegadores detectados."""
        def task():
            self.set_status("Midiendo caché de navegadores...")
            self.log_lines(browser.summarize(), "Navegadores")

        self.run_async(task)

    def on_startup_report(self) -> None:
        """Lista el inventario de programas en inicio."""
        def task():
            self.set_status("Leyendo programas de inicio...")
            self._get_cached("startup", startup_mod.list_startup_entries)
            self.log_lines(startup_mod.summarize(), "Inicio")

        self.run_async(task)

    # ------------------------------------------------------------------
    # Lógica de Informe
    # ------------------------------------------------------------------

    def on_build_report(self) -> None:
        """Genera un resumen unificado de todas las acciones ejecutadas."""
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
        """Exporta el informe a un archivo local (TXT o MD)."""
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
        """Envía pregunta al asistente (local o en línea)."""
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

    def _collect_settings(self) -> Dict[str, Any]:
        """Sincroniza el estado visual de los widgets con los valores de configuración."""
        valores = dict(self.settings)
        for clave, variable in self.setting_vars.items():
            try:
                valores[clave] = variable.get()
            except Exception:
                continue
        
        try:
            val_dup = self.min_dup_entry.get().strip()
            if val_dup.isdigit() and int(val_dup) > 0:
                valores["duplicados_tamano_minimo_kb"] = int(val_dup)
            
            val_top = self.top_files_entry.get().strip()
            if val_top.isdigit() and int(val_top) > 0:
                valores["top_archivos"] = int(val_top)
        except (ValueError, AttributeError):
            logging.error("Error al validar campos de entrada en Ajustes")
            
        clave_api = self.api_key_entry.get().strip()
        if clave_api:
            valores["asistente_clave_api"] = clave_api
        return valores

    def on_save_settings(self) -> None:
        """Persiste los cambios de configuración en el archivo local."""
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
        """Muestra la configuración cargada actualmente."""
        def task():
            self.log_lines(settings_mod.describe(), "Ajustes")

        self.run_async(task)

    def on_reset_settings(self) -> None:
        """Restaura los valores por defecto del sistema."""
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
