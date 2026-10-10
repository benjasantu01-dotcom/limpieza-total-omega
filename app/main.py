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

CRITERIO DE DESEÑO
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
Carga perezosa de pestañas implementada para alertar el inicio de la app.
Se optimiza la recolección de basura mediante procesamiento por generadores.
Se optimiza el volcado de reportes mediante inserción de bloques de texto únicos.
Se implementa memoización de contexto para evitar re-cálculos en el asistente.
Optimización de caché mediante marcas de tiempo para reducir re-cálculos de UI.
Invalidación inteligente mediante digests de estado para evitar re-compilaciones.
Uso de conjuntos (sets) para gestión de estado de UI y debounces.

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
from functools import lru_cache, wraps
from collections import OrderedDict, defaultdict
from tkinter import filedialog, messagebox
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any, Callable, Union, TypedDict, TypeAlias, Set

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

# Type Aliases para mejorar la claridad de las firmas y estructuras
AsyncCallback: TypeAlias = Callable[[], Any]
LogEntry: TypeAlias = Tuple[str, str]
HealthMetricConfig: TypeAlias = Tuple[str, str]

# Definición centralizada de áreas para el dashboard de salud
HEALTH_METRICS_CONFIG: List[HealthMetricConfig] = [
    ("basura", "Basura"),
    ("sospechosos", "Sospechosos"),
    ("ram", "RAM libre"),
    ("disco", "Disco libre"),
]

@lru_cache(maxsize=1)
def get_cached_settings() -> Dict[str, Any]:
    """Carga inicial de configuración desde el archivo persistente."""
    try:
        raw = settings_mod.load()
        if isinstance(raw, dict):
            # Validar que carpetas guardadas en ajustes sigan siendo seguras
            for key in ["carpeta_excluida"]:
                if key in raw and raw[key] and not safety.is_safe_to_modify(Path(raw[key]).resolve()):
                    raw[key] = ""
            return raw
    except (IOError, ValueError) as e:
        logging.error("Error al cargar settings: %s", e)
    return settings_mod.reset()

class AppSettings(TypedDict, total=False):
    """Esquema de configuración de la aplicación para mayor legibilidad y tipado."""
    tema: str
    acento: str
    mostrar_barras: bool
    analisis_en_paralelo: bool
    recordar_ultima_carpeta: bool
    duplicados_tamano_minimo_kb: int
    top_archivos: int
    asistente_activado: bool
    asistente_clave_api: str

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)

def ensure_safety(func: Callable) -> Callable:
    """
    Decorador: invoca `safety.ensure_safe_to_modify` antes de delegar
    ejecución a cualquier método que realice escrituras o modificaciones.
    """
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        home_path = Path.home().resolve()
        safety.ensure_safe_to_modify(home_path)
        return func(*args, **kwargs)
    return wrapper

def safe_ui_operation(func: Callable) -> Callable:
    """
    Decorador: intercepta excepciones de ciclo de vida de widgets para evitar
    cierres inesperados al interactuar con elementos destruidos.
    """
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Optional[Any]:
        try:
            if args and hasattr(args[0], 'winfo_exists') and not args[0].winfo_exists():
                return None
            return func(*args, **kwargs)
        except (tk.TclError, RuntimeError, AttributeError) as e:
            logging.debug("Ignorando error de UI en %s: %s", func.__name__, e)
            return None
    return wrapper

def validated_ui_operation(func: Callable) -> Callable:
    """
    Decorador: asegura que el componente esté activo antes de proceder con
    la lógica del callback, protegiendo la integridad de la cola de eventos.
    """
    @wraps(func)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Optional[Any]:
        if getattr(self, '_closing', False) or (hasattr(self, 'winfo_exists') and not self.winfo_exists()):
            return None
        try:
            return func(self, *args, **kwargs)
        except (tk.TclError, RuntimeError) as e:
            logging.warning("Error de UI recuperable en %s: %s", func.__name__, e)
            return None
    return wrapper

def _check_environment_integrity() -> None:
    """Valida requisitos de seguridad críticos al arrancar el proceso."""
    cwd = Path.cwd().resolve()
    # Prevenir ejecución desde rutas de red o sistema volátiles
    if str(cwd).startswith(r"\\"):
        raise RuntimeError("Ejecución en ruta UNC prohibida.")
    if "system32" in str(cwd).lower() or "windows" in str(cwd).lower():
        raise RuntimeError("Ejecución en entorno protegido.")
        
    safety.ensure_safe_to_modify(Path.home().resolve())
    safety.ensure_safe_to_modify(cwd)

try:
    _check_environment_integrity()
except safety.UnsafePathError as e:
    logging.critical("Iniciando desde ruta insegura: %s", e)
    raise
except Exception as e:
    logging.critical("Error crítico de sistema durante validación: %s", e)
    raise

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

TABS: Tuple[str, ...] = (
    "Salud", "Limpieza", "Seguridad", "Cuarentena", "Memoria", "Disco",
    "Duplicados", "Navegadores", "Inicio", "Informe", "Asistente", "Ajustes",
)

HEALTH_AREAS: Tuple[HealthMetricConfig, ...] = (
    ("basura", "Archivos basura"),
    ("seguridad", "Seguridad"),
    ("memoria", "Memoria RAM"),
    ("disco", "Espacio en disco"),
    ("duplicados", "Duplicados"),
    ("inicio", "Programas de inicio"),
)


class LimpiezaTotalOmegaApp(ctk.CTk):
    """
    Orquestador principal: gestiona la interfaz de usuario, el ciclo de vida
    de la aplicación y coordina las tareas asíncronas entre los módulos.
    """

    def __init__(self) -> None:
        """Constructor: inicializa el registro de componentes y despliega la UI."""
        super().__init__()
        self._init_component_registry()
        self._setup_application()

    def _init_component_registry(self) -> None:
        """Inicializa estructuras de datos, caché, colas y estado inicial."""
        self._initialized_tabs: Dict[str, bool] = {name: False for name in TABS}
        self._health_components_lazy_loaded = False
        
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._task_lock = threading.Lock()
        self._executor_lock = threading.Lock()
        self._closing = False
        self._tasks_running = 0
        
        self._log_queue: List[LogEntry] = []
        self._log_lock = threading.Lock()
        self._log_scheduled = False
        self._active_buttons: Set[ctk.CTkButton] = set()
        
        self._last_card_values: Dict[str, str] = {}
        self._last_gauge_state: Tuple[int, str] = (-1, "")
        self._last_health_state: Optional[Tuple] = None
        self._last_compilation_digest: Any = None
        self.settings: AppSettings = {}
        self.setting_vars: Dict[str, Any] = {}
        
        self.outputs: Dict[str, ctk.CTkTextbox] = {}
        self.cards: Dict[str, ctk.CTkLabel] = {}
        self.area_bars: Dict[str, Tuple[ctk.CTkProgressBar, ctk.CTkLabel]] = {}
        self._debounces: Dict[str, str] = {}

    def _setup_application(self) -> None:
        """Configura el entorno de ejecución, la ventana principal y los protocolos."""
        try:
            self._validate_environment()
            self._init_window_properties()
            self._init_state()
            self._build_layout()
            self.protocol("WM_DELETE_WINDOW", self._on_closing)
        except Exception as e:
            logging.critical("Error fatal al inicializar la aplicación: %s", e)
            if hasattr(self, 'winfo_exists') and self.winfo_exists():
                messagebox.showerror("Error de inicio", "La aplicación no pudo inicializarse.")
                self.destroy()
            else:
                raise

    def _on_closing(self) -> None:
        """Gestiona el cierre de hilos y la limpieza de recursos al salir."""
        with self._executor_lock:
            self._closing = True
            executor = self._executor
            self._executor = None
        
        if executor:
            executor.shutdown(wait=False)
        
        self.quit()
        self.destroy()

    @safe_ui_operation
    def _safe_run_ui_callback(self, callback: AsyncCallback) -> None:
        """Ejecuta una función en el hilo principal de forma segura."""
        self.after_idle(callback)

    def _validate_environment(self) -> None:
        """Valida que el sistema cumple las restricciones de seguridad."""
        try:
            app_root = Path(__file__).resolve().parent
            cwd = Path.cwd().resolve()
            home = Path.home()
            
            # Verificación de permisos y accesibilidad
            if not os.access(app_root, os.W_OK) and not os.access(home, os.W_OK):
                raise RuntimeError("Permisos insuficientes en rutas críticas.")
                
            safety.ensure_safe_to_modify(app_root)
            safety.ensure_safe_to_modify(home)
            safety.ensure_safe_to_modify(cwd)
            
            validations = [
                (app_root.exists(), "Directorio de aplicación inexistente."),
                (not app_root.is_symlink(), "App ubicada en enlace simbólico."),
                (home.exists(), "Directorio home del usuario inaccesible."),
                (home.resolve().is_absolute(), "La ruta home no es absoluta."),
                (safety.is_safe_to_modify(cwd), "Directorio de trabajo inseguro."),
                (not safety.is_protected_path(app_root), "Directorio de aplicación protegido."),
                (not cwd.is_symlink(), "Directorio de trabajo es un enlace simbólico o junction.")
            ]
            
            for condition, error_msg in validations:
                if not condition:
                    raise RuntimeError(f"Entorno inválido: {error_msg}")
        except (safety.UnsafePathError, PermissionError) as e:
            raise RuntimeError(f"Violación de seguridad de entorno: {e}")

    def _validate_disk_access(self, path: Union[str, Path]) -> Path:
        """Valida la integridad de la ruta y permisos de seguridad."""
        p = Path(path).resolve()
        
        # Prevenir Path Traversal y caracteres no deseados
        app_dir = Path(__file__).resolve().parent
        if str(p).startswith(str(app_dir)) and p != app_dir:
             raise safety.UnsafePathError("Operación denegada: Acceso a estructura interna de la App.")

        # Validación estricta de formato: evita caracteres prohibidos en nombres de archivos Windows
        if any(c in '<>|?*' for c in str(p.name)) or len(str(p)) < 3:
            raise safety.UnsafePathError("Ruta contiene caracteres inválidos para el sistema de archivos")
        
        if not p.exists():
            raise FileNotFoundError(f"Ruta inexistente: {p}")
            
        if p.is_symlink():
            raise safety.UnsafePathError("Ruta inválida o enlace prohibido")
            
        safety.ensure_safe_to_modify(p)
        return p

    def _ensure_path_writable_and_clean(self, path: Union[str, Path]) -> None:
        """Verifica la seguridad y limpieza de la ruta antes de escribir."""
        self._validate_disk_access(path)

    def _init_window_properties(self) -> None:
        """Configura el branding, dimensiones y geometría de la ventana."""
        self.title(branding.app_title())
        self.geometry("1120x780")
        self.minsize(980, 680)
        bg_color = branding.color("background")
        if bg_color:
            self.configure(fg_color=bg_color)

    def _init_state(self) -> None:
        """Inicializa la caché, variables de estado y el pool de hilos."""
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._cache_ttl = 300
        self._cache_max_size = 20
        self._cache_access_times: Dict[str, float] = {}
        
        self.scan_target: Optional[str] = None
        self.analysis_folder: Optional[str] = None
        self.report_data: Dict[str, List[str]] = {}
        self.assistant_context = assistant.SystemContext()
        
        self.settings = get_cached_settings()
            
        with self._executor_lock:
            self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
            
    @safe_ui_operation
    def _toggle_ui_availability(self, active: bool) -> None:
        """Habilita o deshabilita botones durante tareas de larga duración."""
        for btn in self._active_buttons:
            if btn.winfo_exists():
                btn.configure(state="normal" if active else "disabled")

    @safe_ui_operation
    def _debounce_action(self, key: str, delay: int, callback: AsyncCallback) -> None:
        """Implementa debounce para minimizar llamadas frecuentes en la UI."""
        if key in self._debounces:
            self.after_cancel(self._debounces[key])
        self._debounces[key] = self.after(delay, callback)

    def _create_styled_label(self, parent: ctk.CTk, text: str, style: str, **kwargs: Any) -> ctk.CTkLabel:
        """Crea una etiqueta aplicando estilos centralizados por branding."""
        font_config = {"size": branding.font_size(style)}
        if style in ("title", "caption"): font_config["weight"] = "bold"
        
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
        """Genera un componente de terminal (Textbox) para logs."""
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
        """Crea un contenedor para organizar botones en línea."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(12, 0))
        return row

    def _action(self, parent: ctk.CTk, text: str, command: AsyncCallback, 
                danger: bool = False, column: int = 0, secondary: bool = False) -> ctk.CTkButton:
        """Crea botones de acción estilizados con semántica visual."""
        if danger:
            fondo, hover, texto = ("danger", "danger_hover", "text")
        elif secondary:
            fondo, hover, texto = ("accent2", "accent2_hover", "text")
        else:
            fondo, hover, texto = ("accent", "accent_hover", "background")

        @safe_ui_operation
        def safe_cmd():
            if self._closing: return
            command()

        button = ctk.CTkButton(
            parent, text=text, command=safe_cmd,
            fg_color=branding.color(fondo),
            hover_color=branding.color(hover),
            text_color=branding.color(texto),
            font=ctk.CTkFont(size=branding.font_size("body"), weight="bold"),
            width=190, height=36, corner_radius=9,
        )
        button.grid(row=0, column=column, padx=6, pady=4, sticky="w")
        self._active_buttons.add(button)
        return button

    def _hint(self, parent: ctk.CTk, text: str) -> None:
        """Crea un texto informativo con formato de nota al pie."""
        self._create_styled_label(
            parent, text, "caption",
            wraplength=1010, justify="left"
        ).pack(fill="x", padx=14, pady=(10, 0))

    def _menu(self, parent: ctk.CTk, values: List[str], variable: tk.StringVar, 
              command: Optional[Callable[[str], Any]] = None, width: int = 190) -> ctk.CTkOptionMenu:
        """Crea menús desplegables integrados con la paleta de branding."""
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
        """Crea campos de entrada con estilo consistente."""
        return ctk.CTkEntry(
            parent, width=width, placeholder_text=placeholder,
            fg_color=branding.color("card"),
            border_color=branding.color("border"),
            text_color=branding.color("text"),
            corner_radius=9,
        )

    def _build_layout(self) -> None:
        """Ensambla los bloques principales de la interfaz."""
        self._build_header()
        self._build_tabs_container()
        self._build_footer()

    def _tab_factory(self, name: str) -> None:
        """
        Carga perezosa (Lazy Loading): construye la pestaña solo cuando se solicita.
        """
        if self._initialized_tabs.get(name):
            return
            
        constructors = {
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
        if constructor and self.winfo_exists():
            try:
                constructor()
                self._initialized_tabs[name] = True
            except (tk.TclError, AttributeError) as e:
                logging.error("Fallo crítico en el constructor de la pestaña %s: %s", name, e)

    def _build_tabs_container(self) -> None:
        """Configura el componente contenedor de pestañas."""
        try:
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
                command=self._on_tab_change
            )
            self.tabview.pack(fill="both", expand=True, padx=18, pady=(4, 8))

            for name in TABS:
                self.tabview.add(branding.tab_label(name))
                
            self._tab_factory(TABS[0])
        except Exception as e:
            logging.error("Error al construir tabview: %s", e)

    @validated_ui_operation
    def _on_tab_change(self, tab_label: str) -> None:
        """Gestor de eventos para el cambio de pestaña actual."""
        for name in TABS:
            if branding.tab_label(name) == tab_label:
                self._tab_factory(name)
                break

    def _build_header(self) -> None:
        """Renderiza la sección superior con identidad de marca."""
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(16, 0))

        canvas = tk.Canvas(
            header, width=72, height=72,
            bg=branding.color("background"),
            highlightthickness=0, bd=0,
        )
        canvas.grid(row=0, column=0, rowspan=2, padx=(0, 16))
        branding.draw_logo(canvas, size=72)

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
            if self.winfo_exists():
                self._debounce_action("resize", 100, lambda: (franja.delete("all"), branding.draw_gradient_bar(franja, event.width, 3)))

        franja.bind("<Configure>", on_resize)

    def _build_footer(self) -> None:
        """Renderiza la barra de estado inferior para feedback de usuario."""
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
        """Constructor de la pestaña Salud."""
        tab = self.tabview.tab(branding.tab_label("Salud"))

        row = self._button_row(tab)
        self._action(row, "Analizar el sistema", self.on_full_analysis, column=0)
        self._action(row, "Limpiar panel", lambda: self.clear("Salud"),
                     secondary=True, column=1)

        self.health_container = ctk.CTkFrame(tab, fg_color="transparent")
        self.health_container.pack(fill="x", padx=12, pady=(14, 0))
        
        self.center_container = ctk.CTkFrame(tab, fg_color="transparent")
        self.center_container.pack(fill="x", padx=12, pady=(16, 0))
        self.center_container.grid_columnconfigure(1, weight=1)

        self.gauge = tk.Canvas(
            self.center_container, width=176, height=176,
            bg=branding.color("surface"), highlightthickness=0, bd=0,
        )
        self.gauge.grid(row=0, column=0, padx=(4, 22))
        
        self._hint(tab, "Combina limpieza, seguridad, memoria, disco y arranque en un solo "
                        "puntaje. Es un análisis de solo lectura: no modifica nada.")
        self._make_output("Salud", tab)
        
        self._lazy_init_health_ui()
        self._draw_gauge(0, "-")

    def _lazy_init_health_ui(self) -> None:
        """Inicializa los componentes visuales de salud bajo demanda."""
        if self._health_components_lazy_loaded: return
        self._build_health_metrics_row(self.health_container)
        self._build_health_area_bars(self.center_container)
        self._health_components_lazy_loaded = True

    def _build_health_metrics_row(self, container: ctk.CTkFrame) -> None:
        """Construye la fila de tarjetas resumen (basura, RAM, etc)."""
        for i, (clave, titulo) in enumerate(HEALTH_METRICS_CONFIG):
            container.grid_columnconfigure(i, weight=1)
            self.cards[clave] = self._metric_card(container, titulo, i)

    def _metric_card(self, parent: ctk.CTk, title: str, column_idx: int) -> ctk.CTkLabel:
        """Factory de tarjeta métrica individual."""
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
        """Construye las barras de progreso individuales por área."""
        area_container = ctk.CTkFrame(parent, fg_color="transparent")
        area_container.grid(row=0, column=1, sticky="ew")
        area_container.grid_columnconfigure(1, weight=1)
        
        for row_idx, (clave, etiqueta) in enumerate(HEALTH_AREAS):
            self._build_single_health_bar(area_container, clave, etiqueta, row_idx)

    def _build_single_health_bar(self, container: ctk.CTkFrame, clave: str, etiqueta: str, row_idx: int) -> None:
        """Helper para renderizar una barra y su etiqueta asociada."""
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

    def _update_health_bar_ui(self, barra: ctk.CTkProgressBar, label: ctk.CTkLabel, puntos: float, maximo: int) -> None:
        """Actualiza el estado visual de una barra de salud."""
        proporcion = puntos / maximo if maximo else 0
        c = branding.score_color(proporcion * 100)
        barra.configure(progress_color=c)
        barra.set(proporcion)
        label.configure(text=f"{puntos:.0f}/{maximo}", text_color=c)

    def _draw_gauge(self, score: int, grade: str) -> None:
        """Actualiza el indicador circular de salud mediante debounce."""
        if (score, grade) == self._last_gauge_state:
            return
        self._last_gauge_state = (score, grade)
        self._debounce_action("gauge", 50, lambda: self._safe_run_ui_callback(lambda: self._render_gauge(score, grade)))

    @safe_ui_operation
    def _render_gauge(self, score: int, grade: str) -> None:
        """Dibuja el gráfico circular del score en el canvas."""
        if not hasattr(self, 'gauge') or not self.gauge.winfo_exists():
            return
        
        self.gauge.delete("all")
        branding.draw_ring(self.gauge, score, size=176, thickness=15)
        
        color_nota = branding.grade_color(grade) if grade != "-" else branding.color("text_dim")
        self.gauge.create_text(88, 78, text=str(score), fill=branding.score_color(score),
                                font=("Segoe UI", branding.font_size("display"), "bold"))
        self.gauge.create_text(88, 116, text=f"nota {grade}", fill=color_nota,
                                font=("Segoe UI", branding.font_size("body"), "bold"))

    def _build_tab_limpieza(self) -> None:
        """Constructor de la pestaña Limpieza."""
        tab = self.tabview.tab(branding.tab_label("Limpieza"))
        row = self._button_row(tab)
        self._action(row, "Buscar basura", self.on_scan_junk, column=0)
        self._action(row, "Mover a revisión", self.on_stage, secondary=True, column=1)
        self._action(row, "Vaciar revisados", self.on_delete_reviewed, danger=True, column=2)

        self._build_limpieza_controls(tab)
        self._make_output("Limpieza", tab)

    def _build_limpieza_controls(self, tab: ctk.CTk) -> None:
        """Configura los controles de selección de carpeta para limpieza."""
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

    def _build_tab_seguridad(self) -> None:
        """Constructor de la pestaña Seguridad."""
        tab = self.tabview.tab(branding.tab_label("Seguridad"))
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
        """Constructor de la pestaña Cuarentena."""
        tab = self.tabview.tab(branding.tab_label("Cuarentena"))
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
        """Constructor de la pestaña Memoria."""
        tab = self.tabview.tab(branding.tab_label("Memoria"))
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
        """Constructor de la pestaña Disco."""
        tab = self.tabview.tab(branding.tab_label("Disco"))
        row = self._button_row(tab)
        self._action(row, "Espacio por unidad", self.on_drives_report, column=0)
        self._action(row, "Analizar una carpeta", self.on_disk_analysis,
                     secondary=True, column=1)
        self._make_output("Disco", tab)

    def _build_tab_duplicados(self) -> None:
        """Constructor de la pestaña Duplicados."""
        tab = self.tabview.tab(branding.tab_label("Duplicados"))
        row = self._button_row(tab)
        self._action(row, "Buscar duplicados", self.on_find_duplicates, column=0)
        self._action(row, "Aislar copias extra", self.on_quarantine_duplicates,
                     danger=True, column=1)
        self._make_output("Duplicados", tab)

    def _build_tab_navegadores(self) -> None:
        """Constructor de la pestaña Navegadores."""
        tab = self.tabview.tab(branding.tab_label("Navegadores"))
        row = self._button_row(tab)
        self._action(row, "Detectar caché", self.on_browser_report, column=0)
        self._make_output("Navegadores", tab)

    def _build_tab_inicio(self) -> None:
        """Constructor de la pestaña Inicio."""
        tab = self.tabview.tab(branding.tab_label("Inicio"))
        row = self._button_row(tab)
        self._action(row, "Ver programas de inicio", self.on_startup_report, column=0)
        self._make_output("Inicio", tab)

    def _build_tab_informe(self) -> None:
        """Constructor de la pestaña Informe."""
        tab = self.tabview.tab(branding.tab_label("Informe"))
        row = self._button_row(tab)
        self._action(row, "Armar informe", self.on_build_report, column=0)
        self._action(row, "Guardar como .txt", lambda: self.on_save_report(False),
                     secondary=True, column=1)
        self._action(row, "Guardar como .md", lambda: self.on_save_report(True),
                     secondary=True, column=2)
        self._make_output("Informe", tab)

    def _build_tab_asistente(self) -> None:
        """Constructor de la pestaña Asistente."""
        tab = self.tabview.tab(branding.tab_label("Asistente"))
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
        """Constructor de la pestaña Ajustes."""
        tab = self.tabview.tab(branding.tab_label("Ajustes"))
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
        
        self._add_setting_label(grilla, "Top de archivos:", 2, 2)
        self.top_files_entry = self._entry(grilla, "15", 100)
        
        self._update_entry_fields(self.min_dup_entry, "duplicados_tamano_minimo_kb", 64)
        self._update_entry_fields(self.top_files_entry, "top_archivos", 15)

        self._build_ia_settings(tab)
        self._make_output("Ajustes", tab)

    def _update_entry_fields(self, widget: ctk.CTkEntry, key: str, default: int) -> None:
        """Inicializa los campos de entrada de ajustes con valores guardados."""
        try:
            widget.insert(0, str(self.settings.get(key, default)))
            widget.grid(row=2, column=3 if "top" in key else 1, sticky="w")
        except (tk.TclError, AttributeError):
            pass

    def _build_ia_settings(self, tab: ctk.CTk) -> None:
        """Construye los controles de configuración específicos de la IA."""
        label = self._create_styled_label(
            tab, f"{branding.icon('Asistente')}  Asistente en línea (opcional)", "title",
            anchor="w", text_color=branding.color("accent2")
        )
        label.pack(fill="x", padx=14, pady=(18, 0))

        ia_container = ctk.CTkFrame(tab, fg_color="transparent")
        ia_container.pack(fill="x", padx=12, pady=(6, 0))

        self.setting_vars["asistente_activado"] = ctk.BooleanVar(value=bool(self.settings.get("asistente_activado")))
        self._add_ia_switch(ia_container, "Activar asistente en línea", "asistente_activado")

        self._create_styled_label(ia_container, "Clave de API:", "body").grid(row=0, column=1, padx=(0, 8))
        self.api_key_entry = self._entry(ia_container, f"vacío = usar {settings_mod.API_KEY_ENV_VAR}", 260)
        self.api_key_entry.configure(show="*")
        self.api_key_entry.grid(row=0, column=2, sticky="w")

    def _add_ia_switch(self, parent: ctk.CTk, texto: str, var_key: str) -> None:
        """Factory de switch para activar/desactivar asistente."""
        ctk.CTkSwitch(
            parent, text=texto,
            variable=self.setting_vars[var_key],
            progress_color=branding.color("accent2"),
            button_color=branding.color("text"),
            text_color=branding.color("text"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 20), pady=6)

    def _add_setting_label(self, parent: ctk.CTkFrame, text: str, row: int, column: int = 0) -> None:
        """Crea una etiqueta descriptiva para la sección de ajustes."""
        self._create_styled_label(parent, text, "body", anchor="w").grid(
            row=row, column=column, sticky="w", padx=(0, 10), pady=6
        )

    def _add_setting_switch(self, parent: ctk.CTkFrame, clave: str, texto: str, row: int, column: int) -> None:
        """Factory de switch para configuraciones booleanas."""
        variable = ctk.BooleanVar(value=bool(self.settings.get(clave)))
        self.setting_vars[clave] = variable
        ctk.CTkSwitch(
            parent, text=texto, variable=variable,
            progress_color=branding.color("accent"),
            button_color=branding.color("text"),
            text_color=branding.color("text"),
            font=ctk.CTkFont(size=branding.font_size("body")),
        ).grid(row=row, column=column, sticky="w", padx=(0, 24), pady=6)

    def _safe_get_entry_value(self, entry_widget: ctk.CTkEntry, default: Any, numeric: bool = False) -> Any:
        """Sanitiza y extrae el valor contenido en un widget de entrada."""
        try:
            if entry_widget is None or not entry_widget.winfo_exists():
                return default
            raw = entry_widget.get().strip()
            if not raw:
                return default
            clean_raw = "".join(c for c in raw if c.isprintable())
            if numeric:
                return self._validate_numeric_setting(clean_raw, default)
            return clean_raw
        except (ValueError, TypeError, tk.TclError, AttributeError):
            return default

    def _is_safe_disk_operation(self, path: Union[str, Path]) -> bool:
        """Valida si la ruta es segura para realizar operaciones de disco."""
        try:
            self._validate_disk_access(path)
            return True
        except (safety.UnsafePathError, PermissionError, FileNotFoundError, OSError):
            return False

    def _is_safe_file_access(self, path: Union[str, Path]) -> bool:
        """Verifica permisos de acceso seguro a un archivo."""
        return self._is_safe_disk_operation(path)

    def _is_safe_path(self, path: Union[str, Path]) -> bool:
        """Valida si la ruta es apta para procesamiento por la lógica de app."""
        return self._is_safe_disk_operation(path)

    def _verify_disk_path(self, path: str) -> bool:
        """Verifica si la ruta puede ser recorrida recursivamente."""
        try:
            self._validate_disk_access(path)
            return True
        except (safety.UnsafePathError, PermissionError, FileNotFoundError, OSError):
            return False

    def _is_safe_target_dir(self, path: Union[str, Path]) -> bool:
        """Valida la seguridad del directorio destino de una operación."""
        return self._is_safe_disk_operation(path)

    def _is_valid_dir(self, path: Optional[Union[str, Path]]) -> bool:
        """Verifica que la ruta sea un directorio existente y accesible."""
        if not path:
            return False
        try:
            p = Path(path).resolve(strict=True)
            return p.is_dir()
        except (OSError, RuntimeError):
            return False

    def _get_cached_data(self, key: str) -> Any:
        """Recupera datos del caché interno."""
        return self._get_cached(key)

    def _get_cached(self, key: str, provider: Optional[Callable[[], Any]] = None, force: bool = False) -> Any:
        """
        Gestiona el caché expirada mediante TTL e invalida registros obsoletos
        implementando un mecanismo LRU limitado.
        """
        now = time.time()
        if not force and key in self._cache:
            if now - self._cache_access_times.get(key, 0) < self._cache_ttl:
                self._cache.move_to_end(key)
                return self._cache[key]
        
        if provider:
            try:
                data = provider()
                if data is not None:
                    if len(self._cache) >= self._cache_max_size:
                        oldest_key, _ = self._cache.popitem(last=False)
                        self._cache_access_times.pop(oldest_key, None)
                    self._cache[key] = data
                    self._cache_access_times[key] = now
                return data
            except Exception:
                return None
        return None

    def _get_cached_or_run(self, key: str, provider: Callable[[], Any], on_complete: Callable[[Any], None]) -> None:
        """Obtiene datos de caché o ejecuta una tarea asíncrona si no están disponibles."""
        cached = self._get_cached(key)
        if cached is not None:
            on_complete(cached)
        else:
            self.run_async(lambda: on_complete(provider()))

    def _invalidate_cache(self, key_prefix: str) -> None:
        """Invalida entradas del caché basándose en un prefijo compartido."""
        keys_to_del = [k for k in self._cache.keys() if k.startswith(key_prefix)]
        for k in keys_to_del:
            del self._cache[k]
            self._cache_access_times.pop(k, None)

    def _box(self, tab: str) -> Optional[ctk.CTkTextbox]:
        """Obtiene el widget de log asociado a la pestaña."""
        return self.outputs.get(tab)

    @validated_ui_operation
    def log(self, text: str, tab: str = "Limpieza") -> None:
        """Encola un mensaje para ser renderizado en el widget de log de una pestaña."""
        with self._log_lock:
            self._log_queue.append((tab, text))
            if not self._log_scheduled:
                self._log_scheduled = True
                self.after_idle(self._flush_logs)

    @safe_ui_operation
    def _flush_logs(self) -> None:
        """Vuelca la cola de logs acumulados a la interfaz gráfica."""
        self._log_scheduled = False
        
        with self._log_lock:
            if not self._log_queue: return
            pendientes = self._log_queue
            self._log_queue = []
        
        grouped = defaultdict(list)
        for tab, msg in pendientes:
            grouped[tab].append(msg)
            
        for tab, msgs in grouped.items():
            box = self._box(tab)
            if box and box.winfo_exists():
                box.insert("end", "\n".join(msgs) + "\n")
                box.see("end")

    @safe_ui_operation
    def clear(self, tab: str = "Limpieza") -> None:
        """Limpia el contenido del log en la pestaña especificada."""
        box = self._box(tab)
        if box and box.winfo_exists():
            box.delete("1.0", "end")

    @safe_ui_operation
    def set_status(self, text: str) -> None:
        """Actualiza el texto informativo en la barra inferior."""
        if hasattr(self, 'status') and self.status.winfo_exists():
            self.status.configure(text=text)

    @safe_ui_operation
    def log_lines(self, lines: List[str], tab: str) -> None:
        """Reemplaza el log de una pestaña con una lista nueva de líneas."""
        self.clear(tab)
        box = self._box(tab)
        if box and box.winfo_exists():
            box.insert("1.0", "\n".join(lines))
            box.see("1.0")
        self.report_data[tab.lower()] = list(lines)

    def _set_ui_busy_state(self, busy: bool) -> None:
        """Gestiona la visibilidad y estado del indicador de actividad."""
        is_mapped = hasattr(self, 'activity') and self.activity.winfo_exists() and self.activity.winfo_ismapped()
        
        if busy:
            self._toggle_ui_availability(False)
            if is_mapped:
                self.activity.start()
        else:
            self._toggle_ui_availability(True)
            if is_mapped:
                self.activity.stop()
                self.activity.pack_forget()

    def _set_busy(self, busy: bool) -> None:
        """Controla el estado global de ocupación de la aplicación."""
        with self._task_lock:
            if busy:
                self._tasks_running += 1
                if self._tasks_running == 1 and not self._closing:
                    self._safe_run_ui_callback(lambda: (
                        self.activity.pack(side="right") if hasattr(self, 'activity') else None,
                        self._set_ui_busy_state(True)
                    ))
            else:
                self._tasks_running = max(0, self._tasks_running - 1)
                if self._tasks_running == 0 and not self._closing:
                    self._safe_run_ui_callback(lambda: (
                        self._set_ui_busy_state(False),
                        self.set_status("Listo.")
                    ))

    def _validate_and_log_error(self, e: Exception, tab: str) -> None:
        """Traduce excepciones de bajo nivel en mensajes amigables para el usuario."""
        if isinstance(e, safety.UnsafePathError):
            self.log(f"Bloqueado por seguridad: {e}", tab)
        elif isinstance(e, PermissionError):
            self.log("Error: permiso denegado. Ejecutá como administrador.", tab)
        elif isinstance(e, FileNotFoundError):
            self.log(f"Error: ruta no encontrada: {getattr(e, 'filename', 'desconocida')}", tab)
        elif isinstance(e, OSError):
            self.log(f"Error de sistema ({e.errno}): {e.strerror}", tab)
        else:
            logging.error("Error inesperado en tarea asíncrona: %s", type(e).__name__)
            self.log(f"Error inesperado: {type(e).__name__}", tab)

    def _safe_run(self, fn: AsyncCallback, tab: str) -> None:
        """Wrapper seguro para la ejecución de tareas con manejo de errores."""
        if self._closing: return
        try:
            if Path.cwd().exists():
                fn()
            else:
                raise RuntimeError("Directorio de trabajo actual eliminado o inaccesible.")
        except Exception as e:
            if not self._closing:
                self._validate_and_log_error(e, tab)

    def _worker_thread_logic(self, fn: AsyncCallback, tab: str) -> None:
        """Wrapper que gestiona el estado ocupado y el manejo de excepciones en hilos."""
        try:
            if not self._closing:
                self._safe_run(fn, tab)
        finally:
            if not self._closing:
                self._set_busy(False)

    def run_async(self, fn: AsyncCallback, target: Optional[str] = None) -> None:
        """Envía una tarea al pool de hilos tras validar la seguridad del target."""
        with self._executor_lock:
            if self._closing or self._executor is None or not self.winfo_exists(): return
            
            if target and not self._is_safe_disk_operation(target):
                self.log("Acción denegada: la ruta destino no es segura.", self._current_tab())
                return
            
            self._set_busy(True)
            tab = self._current_tab()
            self._executor.submit(self._worker_thread_logic, fn, tab)

    def _current_tab(self) -> str:
        """Determina el nombre de la pestaña actualmente visualizada."""
        try:
            if not hasattr(self, 'tabview') or not self.tabview.winfo_exists():
                return "Limpieza"
            etiqueta = self.tabview.get()
            if not isinstance(etiqueta, str): return "Limpieza"
        except (tk.TclError, RuntimeError):
            return "Limpieza"
        for nombre in TABS:
            if branding.tab_label(nombre) in etiqueta:
                return nombre
        return "Limpieza"

    def _ask_folder(self) -> Optional[str]:
        """Abre un diálogo de selección de carpetas con validación de seguridad."""
        try:
            folder = filedialog.askdirectory(title="Seleccionar carpeta")
            if not folder:
                return None
            
            self._validate_disk_access(folder)
            return str(Path(folder).resolve())
        except Exception:
            messagebox.showwarning("Ruta no segura", "Operación no permitida en esta ruta.")
            return None

    def _confirm(self, title: str, message: str) -> bool:
        """Muestra un diálogo de confirmación (Si/No) al usuario."""
        return messagebox.askyesno(title, message, icon="warning")

    @lru_cache(maxsize=1)
    def _get_home_disk_info(self) -> Optional[diskreport.DriveInfo]:
        """Obtiene información sobre el uso del disco en el directorio home."""
        try:
            return diskreport.drive_usage(Path.home())
        except Exception:
            return None

    def _compile_metrics(self) -> Tuple[healthscore.SystemMetrics, memory_mod.Snapshot, diskreport.DriveInfo]:
        """Consolida las métricas de todos los módulos para el dashboard de Salud."""
        junk_items = self._get_cached("junk") or []
        suspicious_items = self._get_cached("suspicions") or []
        duplicate_items = self._get_cached("dups") or []
        startup_items = self._get_cached("startup") or []
        quarantine_items = quarantine.list_items()
        
        ram_snapshot = self._get_cached("ram_snapshot")
        if not ram_snapshot:
            ram_snapshot = memory_mod.read_snapshot()
            self._cache["ram_snapshot"] = ram_snapshot
            self._cache_access_times["ram_snapshot"] = time.time()
            
        disk_info = self._get_home_disk_info()
            
        metrics = healthscore.SystemMetrics(
            junk_mb=sum(item.size_bytes for item in junk_items) / 1048576,
            suspicious_count=len(suspicious_items),
            suspicious_warnings=sum(1 for item in suspicious_items if item.severity == "warning"),
            memory_available_percent=ram_snapshot.available_percent if ram_snapshot else 100.0,
            disk_free_percent=(disk_info.free / disk_info.total * 100) if (disk_info and disk_info.total > 0) else 100.0,
            duplicate_mb=(duplicates_mod.reclaimable_bytes(duplicate_items) / 1048576) if duplicate_items else 0.0,
            startup_count=len(startup_items),
            quarantined_count=len(quarantine_items),
        )
        return metrics, ram_snapshot or memory_mod.Snapshot(0, 0, 0), disk_info or diskreport.DriveInfo(0, 0, 0, "")

    @validated_ui_operation
    @ensure_safety
    def on_full_analysis(self) -> None:
        """Callback: realiza un análisis de salud global del sistema."""
        self._lazy_init_health_ui()

        state_digest = (
            len(self._get_cached("suspicions") or []),
            len(self._get_cached("startup") or []),
            len(quarantine.list_items()),
            len(self._get_cached("dups") or [])
        )

        def task() -> None:
            self.set_status("Analizando el sistema...")
            self.clear("Salud")
            self.log("Analizando... esto no modifica nada.", "Salud")

            self._invalidate_cache("ram_snapshot")
            
            metrics, snapshot, _ = self._compile_metrics()
            score_result = healthscore.compute_score(metrics)

            if self._last_compilation_digest != state_digest:
                self.assistant_context = assistant.build_context(
                    metrics=metrics, 
                    health=score_result,
                    memory_total_gb=snapshot.total / (1024 ** 3) if (snapshot and snapshot.total) else 0.0,
                )
                self._last_compilation_digest = state_digest
            
            self._update_health_visuals(
                score_result, metrics.junk_mb, metrics.suspicious_count,
                metrics.memory_available_percent, metrics.disk_free_percent
            )

            summary_lines = healthscore.summarize(score_result)
            if not self._get_cached("dups"):
                summary_lines += ["", "Nota: los duplicados no se contaron todavía. "
                               "Corré la pestaña Duplicados para incluirlos."]
            self.log_lines(summary_lines, "Salud")
            self.set_status(f"Salud: {score_result.score}/100 (nota {score_result.grade})")

        self.run_async(task, target=str(Path.home()))

    def _update_health_visuals(self, resultado: healthscore.ScoreResult, junk_mb: float, 
                               sospechosos: int, ram_libre: float, disco_libre: float) -> None:
        """Actualiza el dashboard de salud central."""
        state_key = (resultado.score, round(junk_mb, 1), sospechosos, round(ram_libre, 1), round(disco_libre, 1))
        if self._last_health_state == state_key:
            return
        self._last_health_state = state_key

        self._safe_run_ui_callback(lambda: self._apply_ui_update_safely(resultado, junk_mb, sospechosos, ram_libre, disco_libre))

    def _apply_ui_update_safely(self, resultado: healthscore.ScoreResult, junk_mb: float, sospechosos: int, ram_libre: float, disco_libre: float) -> None:
        try:
            self._draw_gauge(resultado.score, resultado.grade)
            self._apply_card_updates(junk_mb, sospechosos, ram_libre, disco_libre)
            self._update_health_bars(resultado)
        except (tk.TclError, RuntimeError, AttributeError):
            pass

    def _update_cards(self, junk_mb: float, sospechosos: int, ram_libre: float, disco_libre: float) -> None:
        """Helper para actualizar las tarjetas métricas con debounce."""
        self._debounce_action("update_cards", 100, lambda: self._safe_run_ui_callback(lambda: self._apply_card_updates(junk_mb, sospechosos, ram_libre, disco_libre)))

    @safe_ui_operation
    def _apply_card_updates(self, junk_mb: float, sospechosos: int, ram_libre: float, disco_libre: float) -> None:
        """Aplica cambios de texto y color a las tarjetas de resumen."""
        valores = {
            "basura": f"{junk_mb:.0f} MB",
            "sospechosos": str(sospechosos),
            "ram": f"{ram_libre:.0f}%",
            "disco": f"{disco_libre:.0f}%",
        }
        
        changed = False
        for clave, valor in valores.items():
            if self._last_card_values.get(clave) != valor:
                changed = True
                break
        
        if not changed:
            return
        self._last_card_values = valores

        colores = {
            "basura": branding.color("accent") if junk_mb < 1000 else branding.color("warning"),
            "sospechosos": branding.color("accent") if sospechosos == 0 else branding.color("warning"),
            "ram": branding.score_color(ram_libre * 3),
            "disco": branding.score_color(disco_libre * 5),
        }
        try:
            for clave, label in self.cards.items():
                if label is not None and label.winfo_exists():
                    label.configure(text=valores.get(clave, "-"), text_color=colores.get(clave, branding.color("text")))
        except Exception:
            pass

    @safe_ui_operation
    def _update_health_bars(self, resultado: healthscore.ScoreResult) -> None:
        """Actualiza las barras de progreso individuales según los resultados."""
        for clave, (barra, label) in self.area_bars.items():
            if barra.winfo_exists():
                puntos = resultado.breakdown.get(clave, 0)
                maximo = healthscore.WEIGHTS.get(clave, 1)
                self._update_health_bar_ui(barra, label, puntos, maximo)

    @validated_ui_operation
    def on_target_choice_changed(self, choice: str) -> None:
        """Callback: Maneja el cambio de directorio destino en Limpieza."""
        def update_label(txt: str) -> None:
            if hasattr(self, 'target_label') and self.target_label.winfo_exists():
                self.target_label.configure(text=txt)

        if choice == "Elegir carpeta...":
            folder = self._ask_folder()
            if folder:
                self.scan_target = folder
                update_label(folder)
            else:
                self.target_choice.set("Por defecto (Temp + Descargas)")
                self.scan_target = None
                update_label("")
        elif choice == "Por defecto (Temp + Descargas)":
            self.scan_target = None
            update_label("")
        else:
            if self._is_safe_disk_operation(choice):
                self.scan_target = choice
                update_label(f"Unidad: {choice}")
            else:
                self.log(f"Error: La ruta seleccionada ({choice}) no es segura.", "Limpieza")
                self.target_choice.set("Por defecto (Temp + Descargas)")
                self.scan_target = None
                update_label("")

    @validated_ui_operation
    @ensure_safety
    def on_scan_junk(self) -> None:
        """Callback: Inicia el escaneo de archivos basura."""
        def task() -> None:
            target = self.scan_target
            destino = target or "carpetas por defecto"
            self.set_status(f"Buscando basura en {destino}...")
            self.clear("Limpieza")
            self.log(f"Buscando basura en: {destino}...", "Limpieza")
            
            raw_scan = scan_for_junk([target] if target else None)
            junk = [item for item in raw_scan if item.size_bytes > 0]
            
            self._invalidate_cache("junk")
            self._cache["junk"] = junk
            self._cache_access_times["junk"] = time.time()
            
            total_mb = round(sum(j.size_bytes for j in junk) / 1048576, 2)
            self.log(f"Encontrados {len(junk)} candidatos ({total_mb} MB).", "Limpieza")
            self._safe_run_ui_callback(self.refresh_list)

        self.run_async(task, target=self.scan_target or str(Path.home()))

    @safe_ui_operation
    def refresh_list(self) -> None:
        """Refresca la visualización de la lista de archivos encontrados."""
        junk = self._get_cached("junk") or []
        ordered = sort_junk(junk, by=self.sort_by.get())
        lines = [f"{jf.size_mb:>8} MB  |  {jf.modified:%Y-%m-%d}  |  {jf.path}" for jf in ordered]
        self.report_data["limpieza"] = lines
        self.log_lines(lines, "Limpieza")

    @validated_ui_operation
    @ensure_safety
    def on_stage(self) -> None:
        """Callback: Mueve archivos seleccionados a la zona de revisión."""
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
            try:
                confirmados = [jf for jf in aptos if self._is_safe_path(jf.path)]
                dest = stage_for_review(confirmados)
                self.log(f"Movidos {len(confirmados)} archivos a: {dest}", "Limpieza")
                self._invalidate_cache("junk")
                self._safe_run_ui_callback(self.refresh_list)
            except Exception as e:
                self.log(f"Error al mover archivos: {e}", "Limpieza")

        self.run_async(task, target=str(Path.home()))

    @validated_ui_operation
    @ensure_safety
    def on_delete_reviewed(self) -> None:
        """Callback: Elimina permanentemente los archivos revisados."""
        if not self._confirm(
            "Vaciar carpeta de revisión",
            "Esto BORRA de forma permanente los archivos que están en la carpeta "
            "de revisión.\n\nNo se puede deshacer. ¿Confirmás?",
        ):
            return

        def task() -> None:
            try:
                self.set_status("Vaciando la carpeta de revisión...")
                n = delete_reviewed()
                self._safe_run_ui_callback(lambda: self.log(f"Borrados {n} archivos de la carpeta de revisión.", "Limpieza"))
            except Exception as e:
                self._safe_run_ui_callback(lambda: self.log(f"Error en borrado: {e}", "Limpieza"))

        self.run_async(task, target=str(Path.home()))

    def _run_heuristic_scan(self, folder: str) -> None:
        """Ejecuta un escaneo heurístico en una ruta específica."""
        try:
            p = self._validate_disk_access(folder)
        except Exception:
            self.log(f"Error: La ruta {folder} no es una carpeta válida.", "Seguridad")
            return

        def task() -> None:
            self.set_status(f"Escaneando {folder}...")
            self.clear("Seguridad")
            self.log(f"Escaneo heurístico en: {folder}", "Seguridad")
            results = scan_directory(str(p))
            self._invalidate_cache("suspicions")
            self._cache["suspicions"] = results
            self._cache_access_times["suspicions"] = time.time()

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

        self.run_async(task, target=str(p))

    @validated_ui_operation
    @ensure_safety
    def on_heuristic_scan(self) -> None:
        """Callback: Realiza un escaneo heurístico en la carpeta de descargas."""
        downloads_path = Path.home() / "Downloads"
        if not downloads_path.is_dir():
            self.log("No se encontró la carpeta de Descargas.", "Seguridad")
            return
        self._run_heuristic_scan(str(downloads_path))

    @validated_ui_operation
    @ensure_safety
    def on_heuristic_scan_folder(self) -> None:
        """Callback: Realiza un escaneo heurístico en una carpeta seleccionada."""
        folder = self._ask_folder()
        if folder:
            self._run_heuristic_scan(folder)

    @validated_ui_operation
    @ensure_safety
    def on_quarantine_findings(self) -> None:
        """Callback: Mueve archivos sospechosos a cuarentena."""
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
            for item_s in suspicions:
                if self._is_safe_path(item_s.path):
                    try:
                        p = self._validate_disk_access(item_s.path)
                        item = quarantine.quarantine_file(str(p), reason="Marcado por escaneo heurístico")
                        self.log(f"Aislado [{item.item_id}] {item_s.path}", "Seguridad")
                        aislados += 1
                    except Exception as e:
                        self.log(f"Error al aislar {item_s.path}: {e}", "Seguridad")
            self.log(f"Listo: {aislados} aislado(s).", "Seguridad")
            self._invalidate_cache("suspicions")

        self.run_async(task, target=str(Path.home()))

    @validated_ui_operation
    def on_defender_scan(self) -> None:
        """Callback: Ejecuta el escaneo rápido de Windows Defender."""
        def task() -> None:
            self.set_status("Windows Defender en curso...")
            self.log("Iniciando escaneo rápido de Windows Defender (puede tardar)...", "Seguridad")
            output = run_windows_defender_quick_scan()
            self.log(output, "Seguridad")

        self.run_async(task)

    @validated_ui_operation
    def on_list_quarantine(self) -> None:
        """Callback: Muestra la lista de archivos en cuarentena."""
        def task() -> None:
            self.log_lines(quarantine.summarize(), "Cuarentena")

        self.run_async(task)

    @validated_ui_operation
    @ensure_safety
    def on_restore_quarantine(self) -> None:
        """Callback: Restaura un archivo desde la cuarentena mediante su ID."""
        raw_id = self._safe_get_entry_value(getattr(self, 'quarantine_id', None), "")
        if not raw_id:
            messagebox.showinfo("Falta el ID", "Pegá el ID del archivo que querés restaurar.")
            return
        
        # Validar el formato del ID (alfanumérico y guiones) para prevenir inyecciones
        clean_id = "".join(c for c in raw_id if c.isalnum() or c == "-")
        if not clean_id or not quarantine.item_exists(clean_id):
            self.log(f"Error: El ID '{clean_id}' no existe o es inválido.", "Cuarentena")
            return

        def task() -> None:
            try:
                item = quarantine.get_item(clean_id)
                if not item or not hasattr(item, 'original_path'):
                    self._safe_run_ui_callback(lambda: self.log("Error: Manifiesto de cuarentena corrupto.", "Cuarentena"))
                    return
                
                if not self._is_safe_path(item.original_path):
                    self._safe_run_ui_callback(lambda: self.log("Error: La ruta destino no es segura.", "Cuarentena"))
                    return
                
                destino = quarantine.restore_item(clean_id)
                self._safe_run_ui_callback(lambda: self.log(f"Restaurado en: {destino}", "Cuarentena"))
            except Exception as e:
                self._safe_run_ui_callback(lambda: self.log(f"Error inesperado al restaurar: {e}", "Cuarentena"))

        self.run_async(task, target=str(Path.home()))

    @validated_ui_operation
    @ensure_safety
    def on_purge_quarantine(self) -> None:
        """Callback: Borra permanentemente todos los archivos en cuarentena."""
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
            try:
                borrados = quarantine.purge_all()
                self._safe_run_ui_callback(lambda: self.log(f"Borrados {borrados} archivo(s) de la cuarentena.", "Cuarentena"))
            except Exception as e:
                self._safe_run_ui_callback(lambda: self.log(f"Error en borrado: {e}", "Cuarentena"))

        self.run_async(task, target=str(Path.home()))

    @validated_ui_operation
    def on_memory_report(self) -> None:
        """Callback: Genera reporte de diagnóstico de memoria RAM."""
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

    @validated_ui_operation
    def on_memory_processes(self) -> None:
        """Callback: Lista procesos con mayor consumo de memoria."""
        def task() -> None:
            try:
                procesos = memory_mod.top_memory_processes(limit=15)
                procesos_validos = [p for p in procesos if memory_mod.process_exists(p.pid)]
                
                if not procesos_validos:
                    self.log_lines(["No se pudo obtener la lista de procesos activos."], "Memoria")
                    return

                tope = max([p.working_set_mb for p in procesos_validos], default=1) or 1
                lineas = ["Procesos por consumo de memoria:", ""]
                for p in procesos_validos:
                    relativo = p.working_set_mb / tope * 100
                    lineas.append(
                        f"  {branding.bar(relativo, 18)}  {p.working_set_mb:>9} MB  "
                        f"PID {p.pid:<7} {p.name}"
                    )
                lineas += ["", "Cerrar el que no uses libera memoria de verdad. "
                               "Copiá el PID si querés probar el trim manual."]
                self.log_lines(lineas, "Memoria")
            except Exception:
                self.log("Error procesando lista de procesos.", "Memoria")

        self.run_async(task)

    @validated_ui_operation
    @ensure_safety
    def on_trim_process(self) -> None:
        """Callback: Intenta liberar memoria working set de un proceso específico."""
        raw_pid = self._safe_get_entry_value(getattr(self, 'pid_entry', None), None)
        
        # Validación de PID: solo numérico y mayor que 100 para evitar procesos críticos del sistema (PID < 100)
        try:
            pid = int(raw_pid)
            if pid < 100:
                raise ValueError
        except (TypeError, ValueError):
            self.log("Error: PID inválido o crítico del sistema (debe ser numérico y >= 100).", "Memoria")
            return
        
        if not memory_mod.process_exists(pid):
            self.log(f"Error: El proceso {pid} no está activo.", "Memoria")
            return

        if not self._confirm("Liberar working set", memory_mod.TRIM_WARNING + "\n\n¿Seguimos?"):
            return

        def task() -> None:
            try:
                safety.ensure_safe_to_modify(Path.home())
                ok, mensaje = memory_mod.trim_working_set(pid)
                self._safe_run_ui_callback(lambda: self.log(("OK: " if ok else "Sin efecto: ") + mensaje, "Memoria"))
            except Exception as e:
                self._safe_run_ui_callback(lambda: self.log(f"Error al intentar liberar proceso: {e}", "Memoria"))

        self.run_async(task, target=str(Path.home()))

    @validated_ui_operation
    def on_drives_report(self) -> None:
        """Callback: Reporta el estado de espacio en disco por unidad."""
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

    @validated_ui_operation
    @ensure_safety
    def on_disk_analysis(self) -> None:
        """Callback: Analiza la estructura de uso de una carpeta."""
        folder = self._ask_folder()
        if not folder:
            return
        
        self.analysis_folder = folder

        def task() -> None:
            self.set_status(f"Analizando {folder}...")
            self.clear("Disco")
            self.log(f"Analizando {folder} (solo lectura, puede tardar)...", "Disco")
            self.log_lines(diskreport.summarize(folder), "Disco")

        self.run_async(task, target=folder)

    @validated_ui_operation
    @ensure_safety
    def on_find_duplicates(self) -> None:
        """Callback: Busca archivos duplicados en la carpeta seleccionada."""
        folder = self._ask_folder()
        if not folder:
            return

        def task() -> None:
            self.set_status(f"Buscando duplicados en {folder}...")
            self.clear("Duplicados")
            self.log(f"Buscando duplicados en {folder} (solo lectura, puede tardar)...",
                     "Duplicados")
            dups = duplicates_mod.find_duplicates([folder])
            
            self._invalidate_cache("dups")
            self._cache["dups"] = dups
            self._cache_access_times["dups"] = time.time()
            
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

        self.run_async(task, target=folder)

    @validated_ui_operation
    @ensure_safety
    def on_quarantine_duplicates(self) -> None:
        """Callback: Mueve las copias redundantes de duplicados a cuarentena."""
        dups = self._get_cached("dups") or []
        if not dups:
            messagebox.showinfo("Sin duplicados", "Primero usá 'Buscar duplicados'.")
            return

        a_mover = []
        for grupo in dups:
            conservar = duplicates_mod.suggest_keeper(grupo)
            if conservar:
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
                if self._is_safe_path(ruta):
                    try:
                        p = self._validate_disk_access(ruta)
                        quarantine.quarantine_file(str(p), reason="Copia duplicada")
                        movidos += 1
                    except Exception as e:
                        self.log(f"Error al aislar {ruta}: {e}", "Duplicados")
            self.log(f"Aisladas {movidos} copia(s). Revisá la pestaña Cuarentena.", "Duplicados")
            self._invalidate_cache("dups")

        self.run_async(task, target=str(Path.home()))

    @validated_ui_operation
    def on_browser_report(self) -> None:
        """Callback: Mide el caché de los navegadores detectados."""
        def task() -> None:
            self.set_status("Midiendo caché de navegadores...")
            self.log_lines(browser.summarize(), "Navegadores")

        self.run_async(task)

    @validated_ui_operation
    def on_startup_report(self) -> None:
        """Callback: Genera reporte de programas que inician con Windows."""
        def task() -> None:
            self.set_status("Leyendo programas de inicio...")
            self._invalidate_cache("startup")
            self._get_cached("startup", provider=startup_mod.list_startup_entries)
            self.log_lines(startup_mod.summarize(), "Inicio")

        self.run_async(task)

    @validated_ui_operation
    def on_build_report(self) -> None:
        """Callback: Compila el informe unificado de la sesión actual."""
        def task() -> None:
            if not self.report_data:
                self.log_lines(["Todavía no corriste ningún análisis. "
                                "Empezá por la pestaña Salud."], "Informe")
                return
            
            sanitized_data = {k: [str(line).replace('\x00', '') for line in v] for k, v in self.report_data.items()}
            texto = reporting.build_report(sanitized_data)
            
            self.clear("Informe")
            for linea in texto.splitlines():
                self.log(linea, "Informe")

        self.run_async(task)

    @validated_ui_operation
    @ensure_safety
    def on_save_report(self, as_markdown: bool) -> None:
        """Callback: Guarda el informe generado en el disco."""
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
            try:
                ruta_destino = Path(destino).resolve()
                self._ensure_path_writable_and_clean(ruta_destino.parent)
                
                ruta = reporting.save_report(self.report_data, str(ruta_destino), as_markdown=as_markdown)
                self.log(f"Informe guardado en: {ruta}", "Informe")
            except Exception as e:
                self.log(f"Error al guardar reporte: {e}", "Informe")

        self.run_async(task, target=str(Path(destino).parent))

    @validated_ui_operation
    def on_ask_assistant(self, question: Optional[str] = None) -> None:
        """Callback: Envía una consulta al motor del asistente local."""
        texto = self._safe_get_entry_value(getattr(self, 'question_entry', None), (question or "").strip())
        
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

    def _get_numeric_setting_from_widget(self, widget: ctk.CTkEntry, key: str, default: int) -> int:
        """Extrae un valor numérico de un widget de entrada validado."""
        return self._safe_get_entry_value(widget, default, numeric=True)

    def _validate_numeric_setting(self, value: Any, default: int) -> int:
        """Valida que un ajuste numérico sea un entero no negativo."""
        try:
            if value is None: return default
            val = int(value)
            return val if val >= 0 else default
        except (ValueError, TypeError):
            return default

    def _collect_settings(self) -> AppSettings:
        """Recopila y sanitiza las preferencias desde los widgets de la interfaz."""
        valores: AppSettings = dict(self.settings)  # type: ignore
        for clave, variable in self.setting_vars.items():
            try:
                if hasattr(variable, 'get'):
                    val = variable.get()
                    if isinstance(val, str):
                        val = "".join(c for c in val if c.isprintable())
                    valores[clave] = val  # type: ignore
            except (tk.TclError, Exception):
                continue
        
        try:
            if hasattr(self, 'min_dup_entry') and self.min_dup_entry.winfo_exists():
                valores["duplicados_tamano_minimo_kb"] = self._get_numeric_setting_from_widget(self.min_dup_entry, "duplicados_tamano_minimo_kb", 64)
                
            if hasattr(self, 'top_files_entry') and self.top_files_entry.winfo_exists():
                valores["top_archivos"] = self._get_numeric_setting_from_widget(self.top_files_entry, "top_archivos", 15)
                
            if hasattr(self, 'api_key_entry') and self.api_key_entry.winfo_exists():
                clave_raw = self._safe_get_entry_value(self.api_key_entry, "")
                if clave_raw:
                    valores["asistente_clave_api"] = "".join(c for c in clave_raw if c.isprintable() and len(c) < 128)
        except Exception as e:
            logging.error("Fallo durante recolección segura de parámetros UI: %s", e)
            
        return valores

    @validated_ui_operation
    @ensure_safety
    def on_save_settings(self) -> None:
        """Callback: Guarda la configuración en el archivo persistente."""
        try:
            propuestos = self._collect_settings()
            
            if "carpeta_excluida" in propuestos and propuestos["carpeta_excluida"]:
                if not self._is_safe_disk_operation(propuestos["carpeta_excluida"]):
                    self.log(f"Error: Ruta de configuración restringida: {propuestos['carpeta_excluida']}", "Ajustes")
                    return

            if propuestos.get("asistente_activado") and not self.settings.get("asistente_activado"):
                if not self._confirm(
                    "Activar asistente en línea",
                    assistant.PRIVACY_NOTICE + "\n\n¿Lo activamos?",
                ):
                    if hasattr(self, 'setting_vars') and "asistente_activado" in self.setting_vars:
                        self.setting_vars["asistente_activado"].set(False)
                    return

            def task() -> None:
                try:
                    self.settings = settings_mod.update(propuestos)
                    ruta = settings_mod.settings_path()
                    if ruta:
                        self._safe_run_ui_callback(lambda: self.log_lines(
                            [f"Ajustes guardados en: {ruta}", ""] + settings_mod.describe(),
                            "Ajustes",
                        ))
                    self.set_status("Ajustes guardados.")
                except Exception as e:
                    self.log(f"Error al escribir ajustes: {e}", "Ajustes")
                    logging.error("Fallo persistencia ajustes: %s", e)

            self.run_async(task)
        except Exception as e:
            logging.error("Error al recopilar ajustes: %s", e)

    @validated_ui_operation
    def on_show_settings(self) -> None:
        """Callback: Muestra la configuración actual."""
        def task() -> None:
            self._safe_run_ui_callback(lambda: self.log_lines(settings_mod.describe(), "Ajustes"))

        self.run_async(task)

    @validated_ui_operation
    def on_reset_settings(self) -> None:
        """Callback: Restaura la configuración a valores de fábrica."""
        if not self._confirm(
            "Restaurar de fábrica",
            "Se van a descartar todos tus ajustes, incluida la clave del "
            "asistente si la guardaste acá.\n\n¿Confirmás?",
        ):
            return

        def task() -> None:
            self.settings = settings_mod.reset()
            for clave, variable in self.setting_vars.items():
                if clave in settings_mod.DEFAULTS:
                    try:
                        variable.set(settings_mod.DEFAULTS[clave])
                    except (tk.TclError, RuntimeError, Exception):
                        continue
            
            self._safe_run_ui_callback(lambda: (
                self.min_dup_entry.delete(0, "end") if hasattr(self, 'min_dup_entry') and self.min_dup_entry.winfo_exists() else None,
                self.min_dup_entry.insert(0, str(settings_mod.DEFAULTS.get("duplicados_tamano_minimo_kb", 64))) if hasattr(self, 'min_dup_entry') and self.min_dup_entry.winfo_exists() else None,
                self.top_files_entry.delete(0, "end") if hasattr(self, 'top_files_entry') and self.top_files_entry.winfo_exists() else None,
                self.top_files_entry.insert(0, str(settings_mod.DEFAULTS.get("top_archivos", 15))) if hasattr(self, 'top_files_entry') and self.top_files_entry.winfo_exists() else None,
                self.log_lines(["Ajustes restaurados a los valores de fábrica.", ""] + settings_mod.describe(), "Ajustes")
            ))

        self.run_async(task)


if __name__ == "__main__":
    app = LimpiezaTotalOmegaApp()
    app.mainloop()
