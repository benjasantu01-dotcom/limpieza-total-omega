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

Instalar dependencias:
    pip install customtkinter

Ejecutar:
    python main.py
"""

import threading
import logging
import os
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import List, Callable, Optional, Dict, TypedDict

import customtkinter as ctk

import branding
import browser
import diskreport
import duplicates as duplicates_mod
import healthscore
import memory as memory_mod
import quarantine
import reporting
import safety
import startup as startup_mod
from organizer import (
    scan_for_junk,
    sort_junk,
    stage_for_review,
    delete_reviewed,
    list_available_drives,
)
from scanner import scan_directory, run_windows_defender_quick_scan

# Define la estructura de datos recolectada por cada pestaña para el reporte final
class ReportData(TypedDict, total=False):
    salud: List[str]
    limpieza: List[str]
    seguridad: List[str]
    cuarentena: List[str]
    memoria: List[str]
    disco: List[str]
    duplicados: List[str]
    navegadores: List[str]
    inicio: List[str]

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

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
)


class LimpiezaTotalOmegaApp(ctk.CTk):
    """Ventana principal: arma la interfaz y coordina las tareas en hilos."""

    def __init__(self):
        super().__init__()
        self.title(branding.app_title())
        self.geometry("1020x720")
        self.minsize(900, 620)
        self.configure(fg_color=branding.color("background"))

        self.junk_files = []
        self.suspicions = []
        self.duplicate_groups = []
        self.scan_target = None 
        self.analysis_folder = None
        self.report_data: ReportData = {}
        self.outputs: Dict[str, ctk.CTkTextbox] = {}
        self.is_running = False
        self._last_health_score = None 
        self._lock = threading.Lock()

        self._build_layout()

    # ------------------------------------------------------------------
    # Construcción de la interfaz
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        """Inicializa la jerarquía de widgets y registra las pestañas."""
        self._build_header()

        self.tabview = ctk.CTkTabview(
            self,
            fg_color=branding.color("surface"),
            segmented_button_fg_color=branding.color("surface_alt"),
            segmented_button_selected_color=branding.color("accent"),
            segmented_button_selected_hover_color=branding.color("accent_hover"),
            text_color=branding.color("text"),
        )
        self.tabview.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        for name in TABS:
            self.tabview.add(name)

        self._build_tab_salud()
        self._build_tab_limpieza()
        self._build_tab_seguridad()
        self._build_tab_cuarentena()
        self._build_tab_memoria()
        self._build_tab_disco()
        self._build_tab_duplicados()
        self._build_tab_navegadores()
        self._build_tab_inicio()
        self._build_tab_informe()

        self.output = self.outputs["Limpieza"]

        self.status = ctk.CTkLabel(
            self,
            text="Listo. Nada se borra sin tu confirmación.",
            text_color=branding.color("text_muted"),
            font=ctk.CTkFont(size=branding.font_size("caption")),
        )
        self.status.pack(pady=(0, 10))

    def _build_header(self) -> None:
        """Renderiza el logotipo y la información de versión en la parte superior."""
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(16, 10))

        canvas = tk.Canvas(
            header, width=64, height=64,
            bg=branding.color("background"),
            highlightthickness=0, bd=0,
        )
        canvas.grid(row=0, column=0, rowspan=2, padx=(0, 14))
        branding.draw_logo(canvas, size=64)

        ctk.CTkLabel(
            header, text=branding.APP_NAME,
            font=ctk.CTkFont(size=branding.font_size("title"), weight="bold"),
            text_color=branding.color("text"),
        ).grid(row=0, column=1, sticky="w")

        ctk.CTkLabel(
            header, text=f"{branding.APP_TAGLINE}  ·  v{branding.APP_VERSION}",
            font=ctk.CTkFont(size=branding.font_size("subtitle")),
            text_color=branding.color("text_muted"),
        ).grid(row=1, column=1, sticky="w")

    def _make_output(self, tab_name: str, parent: ctk.CTkFrame) -> ctk.CTkTextbox:
        """Crea y registra un área de texto estilizada para una pestaña específica."""
        box = ctk.CTkTextbox(
            parent,
            fg_color=branding.color("background"),
            text_color=branding.color("text"),
            border_color=branding.color("border"),
            border_width=1,
            font=ctk.CTkFont(family="Consolas", size=branding.font_size("mono")),
        )
        box.pack(fill="both", expand=True, padx=10, pady=10)
        self.outputs[tab_name] = box
        return box

    def _button_row(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        """Crea un contenedor horizontal para alinear los botones de acción."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(10, 0))
        return row

    def _action(self, parent: ctk.CTkFrame, text: str, command: Callable, danger: bool = False, column: int = 0) -> ctk.CTkButton:
        """Genera un botón con color dinámico basado en si la acción es potencialmente destructiva."""
        button = ctk.CTkButton(
            parent, text=text, command=command,
            fg_color=branding.color("danger") if danger else branding.color("accent"),
            hover_color=branding.color("danger_hover") if danger else branding.color("accent_hover"),
            text_color="#0b0f14" if not danger else branding.color("text"),
            font=ctk.CTkFont(size=branding.font_size("body"), weight="bold"),
            width=190, height=34,
        )
        button.grid(row=0, column=column, padx=6, pady=4, sticky="w")
        return button

    def _hint(self, parent: ctk.CTkFrame, text: str) -> None:
        """Añade texto explicativo de ayuda para el usuario en cada pestaña."""
        ctk.CTkLabel(
            parent, text=text, text_color=branding.color("text_muted"),
            font=ctk.CTkFont(size=branding.font_size("caption")),
            wraplength=940, justify="left",
        ).pack(fill="x", padx=12, pady=(8, 0))

    # -- Pestañas (Construcción detallada de vistas) --------------------

    def _build_tab_salud(self) -> None:
        """Vista de resumen general del estado del sistema."""
        tab = self.tabview.tab("Salud")
        row = self._button_row(tab)
        self._action(row, "Analizar el sistema", self.on_full_analysis, column=0)
        self._action(row, "Limpiar panel", lambda: self.clear("Salud"), column=1)

        self.score_label = ctk.CTkLabel(
            tab, text="—/100",
            font=ctk.CTkFont(size=44, weight="bold"),
            text_color=branding.color("text_muted"),
        )
        self.score_label.pack(pady=(12, 0))

        self._hint(tab, "Combina limpieza, seguridad, memoria, disco y arranque en un solo "
                        "puntaje. Es un análisis de solo lectura: no modifica nada.")
        self._make_output("Salud", tab)

    def _build_tab_limpieza(self) -> None:
        """Vista para búsqueda y gestión de archivos temporales/basura."""
        tab = self.tabview.tab("Limpieza")
        row = self._button_row(tab)
        self._action(row, "Buscar basura", self.on_scan_junk, column=0)
        self._action(row, "Mover a revisión", self.on_stage, column=1)
        self._action(row, "Vaciar revisados", self.on_delete_reviewed, danger=True, column=2)

        options = ctk.CTkFrame(tab, fg_color="transparent")
        options.pack(fill="x", padx=10, pady=(10, 0))

        ctk.CTkLabel(options, text="Buscar en:", text_color=branding.color("text")).grid(
            row=0, column=0, padx=(0, 6))
        drive_options = ["Por defecto (Temp + Descargas)"] + list_available_drives() + ["Elegir carpeta..."]
        self.target_choice = ctk.StringVar(value=drive_options[0])
        ctk.CTkOptionMenu(
            options, values=drive_options, variable=self.target_choice,
            command=self.on_target_choice_changed,
            fg_color=branding.color("surface_alt"),
            button_color=branding.color("accent"),
            button_hover_color=branding.color("accent_hover"),
        ).grid(row=0, column=1, padx=6)

        self.target_label = ctk.CTkLabel(options, text="", text_color=branding.color("text_muted"))
        self.target_label.grid(row=0, column=2, padx=6)

        ctk.CTkLabel(options, text="Ordenar por:", text_color=branding.color("text")).grid(
            row=0, column=3, padx=(18, 6))
        self.sort_by = ctk.StringVar(value="size")
        ctk.CTkOptionMenu(
            options, values=["size", "date"], variable=self.sort_by,
            command=lambda _: self.refresh_list(),
            fg_color=branding.color("surface_alt"),
            button_color=branding.color("accent"),
            button_hover_color=branding.color("accent_hover"),
        ).grid(row=0, column=4, padx=6)

        self._hint(tab, "Los candidatos se mueven a una carpeta de revisión, no se borran. "
                        "'Vaciar revisados' es el único paso que elimina, y pide confirmación.")
        self._make_output("Limpieza", tab)

    def _build_tab_seguridad(self) -> None:
        """Vista para escaneos de integridad y hallazgos sospechosos."""
        tab = self.tabview.tab("Seguridad")
        row = self._button_row(tab)
        self._action(row, "Escaneo heurístico", self.on_heuristic_scan, column=0)
        self._action(row, "Elegir carpeta y escanear", self.on_heuristic_scan_folder, column=1)
        self._action(row, "Aislar hallazgos", self.on_quarantine_findings, column=2)
        self._action(row, "Windows Defender", self.on_defender_scan, column=3)

        self._hint(tab, "El escaneo heurístico marca señales sospechosas (doble extensión, "
                        "ejecutables recién bajados, nombres que imitan procesos del sistema). "
                        "No es un antivirus: para eso está Windows Defender. 'Aislar hallazgos' "
                        "mueve lo marcado a cuarentena, de donde se puede restaurar.")
        self._make_output("Seguridad", tab)

    def _build_tab_cuarentena(self) -> None:
        """Vista para gestionar archivos aislados y restauraciones."""
        tab = self.tabview.tab("Cuarentena")
        row = self._button_row(tab)
        self._action(row, "Ver cuarentena", self.on_list_quarantine, column=0)
        self._action(row, "Restaurar por ID", self.on_restore_quarantine, column=1)
        self._action(row, "Vaciar cuarentena", self.on_purge_quarantine, danger=True, column=2)

        id_row = ctk.CTkFrame(tab, fg_color="transparent")
        id_row.pack(fill="x", padx=10, pady=(10, 0))
        ctk.CTkLabel(id_row, text="ID a restaurar:", text_color=branding.color("text")).grid(
            row=0, column=0, padx=(0, 6))
        self.quarantine_id = ctk.CTkEntry(
            id_row, width=220, placeholder_text="pegá el ID que ves en la lista",
            fg_color=branding.color("background"), border_color=branding.color("border"),
        )
        self.quarantine_id.grid(row=0, column=1, padx=6)

        self._hint(tab, "La cuarentena guarda la ruta original de cada archivo, así se puede "
                        "devolver exactamente a su lugar. Restaurar hacia una carpeta de "
                        "sistema está bloqueado.")
        self._make_output("Cuarentena", tab)

    def _build_tab_memoria(self) -> None:
        """Vista de diagnóstico de uso de memoria RAM."""
        tab = self.tabview.tab("Memoria")
        row = self._button_row(tab)
        self._action(row, "Diagnóstico de RAM", self.on_memory_report, column=0)
        self._action(row, "Procesos que más consumen", self.on_memory_processes, column=1)
        self._action(row, "Liberar working set (PID)", self.on_trim_process, danger=True, column=2)

        pid_row = ctk.CTkFrame(tab, fg_color="transparent")
        pid_row.pack(fill="x", padx=10, pady=(10, 0))
        ctk.CTkLabel(pid_row, text="PID:", text_color=branding.color("text")).grid(
            row=0, column=0, padx=(0, 6))
        self.pid_entry = ctk.CTkEntry(
            pid_row, width=140, placeholder_text="ej. 4812",
            fg_color=branding.color("background"), border_color=branding.color("border"),
        )
        self.pid_entry.grid(row=0, column=1, padx=6)

        self._hint(tab, "Acá no hay 'limpiador de RAM' y es a propósito: forzar la liberación "
                        "de memoria hace subir el número de RAM libre pero empeora el "
                        "rendimiento, porque Windows tiene que releer del disco lo que acaba "
                        "de descartar. Lo que sí sirve es ver qué consume y cerrar eso.")
        self._make_output("Memoria", tab)

    def _build_tab_disco(self) -> None:
        """Vista de reporte de uso de disco por unidad y carpeta."""
        tab = self.tabview.tab("Disco")
        row = self._button_row(tab)
        self._action(row, "Espacio por unidad", self.on_drives_report, column=0)
        self._action(row, "Analizar una carpeta", self.on_disk_analysis, column=1)

        self._hint(tab, "Solo lectura: mide y ordena para que puedas ver en qué se fue el "
                        "espacio. Las carpetas de sistema se saltean siempre.")
        self._make_output("Disco", tab)

    def _build_tab_duplicados(self) -> None:
        """Vista de búsqueda de archivos duplicados por hash."""
        tab = self.tabview.tab("Duplicados")
        row = self._button_row(tab)
        self._action(row, "Buscar duplicados", self.on_find_duplicates, column=0)
        self._action(row, "Aislar copias extra", self.on_quarantine_duplicates, danger=True, column=1)

        self._hint(tab, "Compara por tamaño, después por hash parcial y por último por hash "
                        "completo, así no lee de más. 'Aislar copias extra' conserva una copia "
                        "de cada grupo y manda el resto a cuarentena, no las borras.")
        self._make_output("Duplicados", tab)

    def _build_tab_navegadores(self) -> None:
        """Vista de detección de caché de navegadores soportados."""
        tab = self.tabview.tab("Navegadores")
        row = self._button_row(tab)
        self._action(row, "Detectar caché", self.on_browser_report, column=0)

        self._hint(tab, "Se listan solo carpetas de caché, que el navegador regenera solo. "
                        "Nunca se tocan contraseñas, cookies, marcadores ni historial.")
        self._make_output("Navegadores", tab)

    def _build_tab_inicio(self) -> None:
        """Vista de listado de aplicaciones en el inicio del sistema."""
        tab = self.tabview.tab("Inicio")
        row = self._button_row(tab)
        self._action(row, "Ver programas de inicio", self.on_startup_report, column=0)

        self._hint(tab, "Solo lectura. Deshabilitar programas se hace desde el Administrador "
                        "de tareas de Windows, que guarda respaldo del cambio; esta app no "
                        "modifica el registro de arranque a propósito.")
        self._make_output("Inicio", tab)

    def _build_tab_informe(self) -> None:
        """Vista de generación y exportación de reportes unificados."""
        tab = self.tabview.tab("Informe")
        row = self._button_row(tab)
        self._action(row, "Armar informe", self.on_build_report, column=0)
        self._action(row, "Guardar como .txt", lambda: self.on_save_report(False), column=1)
        self._action(row, "Guardar como .md", lambda: self.on_save_report(True), column=2)

        self._hint(tab, "Junta todo lo que analizaste en esta sesión en un solo documento.")
        self._make_output("Informe", tab)

    # ------------------------------------------------------------------
    # Utilidades (Gestión de UI y asincronismo)
    # ------------------------------------------------------------------

    def _box(self, tab: str) -> ctk.CTkTextbox:
        """Devuelve el widget de texto de la pestaña especificada, o el de Limpieza."""
        return self.outputs.get(tab) or self.outputs["Limpieza"]

    def _is_path_safe(self, path: str) -> bool:
        """
        Valida que la ruta proporcionada sea segura mediante `safety.py`.
        Si la ruta es crítica o de sistema, alerta al usuario y bloquea la operación.
        """
        if safety.is_protected_path(path):
            messagebox.showwarning(
                "Acceso restringido",
                f"La ruta '{path}' está protegida o es de sistema. Operación abortada."
            )
            return False
        return True

    def log(self, text: str, tab: str = "Limpieza") -> None:
        """Escribe una línea en la pestaña indicada; operación segura para hilos."""
        box = self._box(tab)

        def append():
            box.insert("end", f"{text}\n")
            box.see("end")

        self.after(0, append)

    def clear(self, tab: str = "Limpieza") -> None:
        """Elimina todo el contenido del cuadro de texto de una pestaña."""
        box = self._box(tab)
        self.after(0, lambda: box.delete("1.0", "end"))

    def set_status(self, text: str) -> None:
        """Actualiza el label del pie de ventana."""
        self.after(0, lambda: self.status.configure(text=text))

    def log_lines(self, lines: List[str], tab: str) -> None:
        """Vacía una pestaña y escribe una lista de líneas; actualiza cache del reporte."""
        text_block = "\n".join(lines) + "\n"
        self.report_data[tab.lower()] = list(lines)
        
        def update_ui():
            box = self._box(tab)
            box.delete("1.0", "end")
            box.insert("1.0", text_block)
            box.see("end")
            
        self.after(0, update_ui)

    def run_async(self, fn: Callable) -> None:
        """
        Encapsula la ejecución de una tarea costosa en un hilo separado.
        Gestiona el estado de bloqueo de la app y errores de sistema.
        """
        with self._lock:
            if self.is_running:
                return
            self.is_running = True

        def wrapper():
            try:
                fn()
            except safety.UnsafePathError as e:
                self.log(f"Bloqueado por seguridad: {e}", self._current_tab())
            except PermissionError:
                self.log("Error: permiso denegado. Ejecutá la app como administrador.", self._current_tab())
            except FileNotFoundError as e:
                self.log(f"Error: ruta no encontrada: {getattr(e, 'filename', e)}", self._current_tab())
            except OSError as e:
                logging.error("Error de sistema: %s", e)
                self.log(f"Error de sistema ({e.errno}): {e.strerror}", self._current_tab())
            except Exception as e:
                logging.exception("Error inesperado en tarea asíncrona: %s", e)
                self.log(f"Error inesperado: {type(e).__name__}", self._current_tab())
            finally:
                with self._lock:
                    self.is_running = False
                self.set_status("Listo.")

        threading.Thread(target=wrapper, daemon=True).start()

    def _current_tab(self) -> str:
        """Obtiene la pestaña activa actual."""
        try:
            return self.tabview.get()
        except Exception:
            return "Limpieza"

    def _ask_folder(self, title: str) -> Optional[str]:
        """
        Abre un diálogo nativo de selección de carpetas.
        Valida que la carpeta seleccionada no infrinja las reglas de seguridad.
        """
        folder = filedialog.askdirectory(title=title)
        if not folder or not os.path.isdir(folder):
            return None
        if not self._is_path_safe(folder):
            return None
        return folder

    def _confirm(self, title: str, message: str) -> bool:
        """
        Muestra un diálogo de confirmación modal para prevenir acciones destructivas.
        Devuelve True solo si el usuario confirma explícitamente.
        """
        return messagebox.askyesno(title, message, icon="warning")

    # ------------------------------------------------------------------
    # Lógica de negocio por pestaña
    # ------------------------------------------------------------------

    def on_full_analysis(self) -> None:
        """Calcula el score de salud mediante el agregado de todas las métricas."""
        def task():
            self.set_status("Analizando el sistema (solo lectura)...")
            self.clear("Salud")
            self.log("Analizando... esto no modifica nada.", "Salud")

            junk = scan_for_junk()
            junk_mb = sum(j.size_bytes for j in junk) / (1024 * 1024)

            descargas = os.path.expanduser("~/Downloads")
            hallazgos = scan_directory(descargas) if os.path.isdir(descargas) else []
            advertencias = sum(1 for h in hallazgos if h.severity == "warning")

            snapshot = memory_mod.read_snapshot()
            unidad = diskreport.drive_usage(os.path.expanduser("~"))
            libre_pct = (unidad.free / unidad.total * 100) if unidad and unidad.total else 100.0
            arranque = startup_mod.list_startup_entries()
            en_cuarentena = quarantine.list_items()

            duplicado_mb = duplicates_mod.reclaimable_bytes(self.duplicate_groups) / (1024 * 1024)

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
            resultado = healthscore.compute_score(metrics)
            self._last_health_score = resultado

            self.after(0, lambda: self.score_label.configure(
                text=f"{resultado.score}/100  ·  {resultado.grade}",
                text_color=branding.grade_color(resultado.grade),
            ))

            lineas = healthscore.summarize(resultado)
            if not self.duplicate_groups:
                lineas += ["", "Nota: los duplicados no se contaron todavía. "
                               "Corré la pestaña Duplicados para incluirlos."]
            self.log_lines(lineas, "Salud")

        self.run_async(task)

    def on_target_choice_changed(self, choice: str) -> None:
        """Actualiza el objetivo del escaneo según la selección del usuario."""
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
            self.scan_target = choice
            self.target_label.configure(text=f"Unidad completa: {choice}")

    def on_scan_junk(self) -> None:
        """Ejecuta el escaneo de archivos basura."""
        def task():
            destino = self.scan_target or "carpetas por defecto (Temp/Descargas)"
            self.set_status(f"Buscando basura en {destino}...")
            self.clear("Limpieza")
            self.log(f"Buscando archivos basura en: {destino}...", "Limpieza")
            directories = [self.scan_target] if self.scan_target else None
            self.junk_files = scan_for_junk(directories)
            total_mb = round(sum(j.size_bytes for j in self.junk_files) / (1024 * 1024), 2)
            self.log(f"Encontrados {len(self.junk_files)} candidatos ({total_mb} MB).", "Limpieza")
            self.refresh_list()

        self.run_async(task)

    def refresh_list(self) -> None:
        """Actualiza la visualización de la lista de archivos encontrados."""
        ordered = sort_junk(self.junk_files, by=self.sort_by.get())
        lines = [f"{jf.size_mb:>8} MB  |  {jf.modified:%Y-%m-%d}  |  {jf.path}" for jf in ordered]
        self.report_data["limpieza"] = lines
        text_block = "\n".join(lines)

        def update_ui():
            box = self._box("Limpieza")
            box.delete("1.0", "end")
            box.insert("1.0", text_block)

        self.after(0, update_ui)

    def on_stage(self) -> None:
        """Mueve archivos seleccionados a la zona de revisión."""
        if not self.junk_files:
            messagebox.showinfo("Sin candidatos", "Primero usá 'Buscar basura'.")
            return
        if not self._confirm(
            "Mover a revisión",
            f"Se van a MOVER {len(self.junk_files)} archivos a la carpeta de revisión.\n\n"
            "No se borra nada: podés verlos y decidir después. ¿Seguimos?",
        ):
            return

        def task():
            self.set_status("Moviendo a revisión...")
            valid_files = []
            for jf in self.junk_files:
                path_str = str(jf.path.resolve())
                if self._is_path_safe(path_str):
                    valid_files.append(jf)
                else:
                    self.log(f"Omitido por protección: {path_str}", "Limpieza")
            
            if not valid_files:
                self.log("Ningún archivo es apto para moverse.", "Limpieza")
                return

            dest = stage_for_review(valid_files)
            self.log(f"Movidos {len(valid_files)} archivos a: {dest}", "Limpieza")
            self.junk_files = []

        self.run_async(task)

    def on_delete_reviewed(self) -> None:
        """Borra permanentemente los archivos en revisión."""
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

    def _run_heuristic_scan(self, folder: str) -> None:
        """Lógica central para escaneo heurístico."""
        def task():
            self.set_status(f"Escaneando {folder}...")
            self.clear("Seguridad")
            self.log(f"Escaneo heurístico en: {folder}", "Seguridad")
            self.suspicions = scan_directory(folder)
            if not self.suspicions:
                self.log("Sin hallazgos sospechosos.", "Seguridad")
                self.report_data["seguridad"] = ["Sin hallazgos sospechosos."]
                return
            lineas = [f"{len(self.suspicions)} hallazgo(s):", ""]
            for r in self.suspicions:
                etiqueta = branding.severity_label(r.severity)
                lineas.append(f"[{etiqueta}] {r.path} — {r.reason}")
            lineas.extend(["", "Recordá: son señales, no una condena. Usá 'Aislar hallazgos' "
                             "para moverlos a cuarentena sin borrarlos."])
            self.log_lines(lineas, "Seguridad")

        self.run_async(task)

    def on_heuristic_scan(self) -> None:
        downloads_path = os.path.expanduser("~/Downloads")
        if not os.path.isdir(downloads_path):
            self.log("No se encontró la carpeta de Descargas.", "Seguridad")
            return
        self._run_heuristic_scan(downloads_path)

    def on_heuristic_scan_folder(self) -> None:
        folder = self._ask_folder("Elegí una carpeta para escanear")
        if folder:
            self._run_heuristic_scan(folder)

    def on_quarantine_findings(self) -> None:
        if not self.suspicions:
            messagebox.showinfo("Sin hallazgos", "Primero corré un escaneo heurístico.")
            return
        rutas = sorted({str(s.path) for s in self.suspicions})
        if not self._confirm(
            "Aislar en cuarentena",
            f"Se van a MOVER {len(rutas)} archivo(s) a la cuarentena.\n\n"
            "No se borran: quedan guardados con su ruta original y se pueden "
            "restaurar cuando quieras. ¿Seguimos?",
        ):
            return

        def task():
            self.set_status("Aislando archivos...")
            aislados, bloqueados = 0, 0
            for ruta in rutas:
                try:
                    if self._is_path_safe(ruta):
                        item = quarantine.quarantine_file(ruta, reason="Marcado por escaneo heurístico")
                        self.log(f"Aislado [{item.item_id}] {ruta}", "Seguridad")
                        aislados += 1
                    else:
                        bloqueados += 1
                except (safety.UnsafePathError, FileNotFoundError, OSError) as e:
                    self.log(f"No se aisló {ruta}: {e}", "Seguridad")
                    bloqueados += 1
            self.log(f"Listo: {aislados} aislado(s), {bloqueados} omitido(s).", "Seguridad")
            self.suspicions = []

        self.run_async(task)

    def on_defender_scan(self) -> None:
        def task():
            self.set_status("Windows Defender en curso...")
            self.log("Iniciando escaneo rápido de Windows Defender (puede tardar)...", "Seguridad")
            output = run_windows_defender_quick_scan()
            self.log(output, "Seguridad")

        self.run_async(task)

    def on_list_quarantine(self) -> None:
        def task():
            self.log_lines(quarantine.summarize(), "Cuarentena")

        self.run_async(task)

    def on_restore_quarantine(self) -> None:
        item_id = self.quarantine_id.get().strip()
        if not item_id:
            messagebox.showinfo("Falta el ID", "Pegá el ID del archivo que querés restaurar.")
            return

        def task():
            try:
                item = quarantine.get_manifest_item(item_id)
                if not item:
                    self.log(f"ID no encontrado: {item_id}", "Cuarentena")
                    return
                
                if not self._is_path_safe(item.original_path):
                    return

                parent_dir = os.path.dirname(item.original_path)
                if not os.path.exists(parent_dir):
                    self.log(f"Error: La carpeta original '{parent_dir}' ya no existe.", "Cuarentena")
                    return

                destino = quarantine.restore_item(item_id)
                self.log(f"Restaurado en: {destino}", "Cuarentena")
            except (KeyError, FileNotFoundError, safety.UnsafePathError) as e:
                self.log(f"No se pudo restaurar: {e}", "Cuarentena")
            except Exception as e:
                self.log(f"Error inesperado al restaurar: {e}", "Cuarentena")

        self.run_async(task)

    def on_purge_quarantine(self) -> None:
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

    def on_memory_report(self) -> None:
        def task():
            snapshot = memory_mod.read_snapshot()
            procesos = memory_mod.top_memory_processes(limit=5)
            self.log_lines(memory_mod.diagnose(snapshot, procesos), "Memoria")

        self.run_async(task)

    def on_memory_processes(self) -> None:
        def task():
            procesos = memory_mod.top_memory_processes(limit=15)
            if not procesos:
                self.log_lines(["No se pudo obtener la lista de procesos en este sistema."],
                               "Memoria")
                return
            lineas = ["Procesos por consumo de memoria:", ""]
            for p in procesos:
                lineas.append(f"  {p.working_set_mb:>9} MB  PID {p.pid:<7} {p.name}")
            lineas += ["", "Cerrar el que no uses libera memoria de verdad. "
                           "Copiá el PID si querés probar el trim manual."]
            self.log_lines(lineas, "Memoria")

        self.run_async(task)

    def on_trim_process(self) -> None:
        raw = self.pid_entry.get().strip()
        if not raw:
            messagebox.showinfo("PID vacío", "Escribí el número de PID.")
            return
        if not raw.isdigit():
            messagebox.showinfo("PID inválido", "El PID debe ser un número entero.")
            return
        
        pid = int(raw)
        if pid < 100:
            messagebox.showwarning("Bloqueo de seguridad", "No se permite manipular procesos del sistema.")
            return

        if not self._confirm("Liberar working set", memory_mod.TRIM_WARNING + "\n\n¿Seguimos?"):
            return

        def task():
            ok, mensaje = memory_mod.trim_working_set(pid)
            self.log(("OK: " if ok else "Sin efecto: ") + mensaje, "Memoria")

        self.run_async(task)

    def on_drives_report(self) -> None:
        def task():
            unidades = diskreport.all_drives_usage()
            if not unidades:
                self.log_lines(["No se detectaron unidades."], "Disco")
                return
            lineas = ["Espacio por unidad:", ""]
            for u in unidades:
                alerta = "  <-- casi llena" if u.is_almost_full else ""
                lineas.append(
                    f"  {u.mount:<6} {diskreport.format_size(u.used):>10} usados de "
                    f"{diskreport.format_size(u.total):<10} "
                    f"({u.used_percent}%) — libre: {diskreport.format_size(u.free)}{alerta}"
                )
            self.log_lines(lineas, "Disco")

        self.run_async(task)

    def on_disk_analysis(self) -> None:
        folder = self._ask_folder("Elegí una carpeta para analizar")
        if not folder:
            return
        self.analysis_folder = folder

        def task():
            self.set_status(f"Analizando {folder}...")
            self.clear("Disco")
            self.log(f"Analizando {folder} (solo lectura, puede tardar)...", "Disco")
            self.log_lines(diskreport.summarize(folder), "Disco")

        self.run_async(task)

    def on_find_duplicates(self) -> None:
        folder = self._ask_folder("Elegí una carpeta donde buscar duplicados")
        if not folder:
            return

        def task():
            self.set_status(f"Buscando duplicados en {folder}...")
            self.clear("Duplicados")
            self.log(f"Buscando duplicados en {folder} (solo lectura, puede tardar)...",
                     "Duplicados")
            self.duplicate_groups = duplicates_mod.find_duplicates([folder])
            if not self.duplicate_groups:
                self.log_lines(["No se encontraron duplicados."], "Duplicados")
                return
            recuperable = duplicates_mod.reclaimable_bytes(self.duplicate_groups)
            lineas = [
                f"{len(self.duplicate_groups)} grupo(s) de duplicados",
                f"Espacio recuperable: {diskreport.format_size(recuperable)}",
                "",
            ]
            for grupo in self.duplicate_groups[:40]:
                lineas.extend(duplicates_mod.format_group(grupo))
                lineas.append("")
            self.log_lines(lineas, "Duplicados")

        self.run_async(task)

    def on_quarantine_duplicates(self) -> None:
        if not self.duplicate_groups:
            messagebox.showinfo("Sin duplicados", "Primero usá 'Buscar duplicados'.")
            return

        a_mover = []
        for grupo in self.duplicate_groups:
            conservar = duplicates_mod.suggest_keeper(grupo)
            a_mover.extend([p for p in grupo.paths if p != conservar])

        if not a_mover:
            messagebox.showinfo("Nada para mover", "No hay copias extra que aislar.")
            return
        if not self._confirm(
            "Aislar copias duplicadas",
            f"Se van a MOVER {len(a_mover)} copia(s) a la cuarentena, conservando "
            f"una de cada grupo.\n\nNo se borran: se pueden restaurar. ¿Seguimos?",
        ):
            return

        def task():
            self.set_status("Aislando copias duplicadas...")
            movidos = 0
            for ruta in a_mover:
                try:
                    if self._is_path_safe(ruta):
                        quarantine.quarantine_file(ruta, reason="Copia duplicada")
                        movidos += 1
                except (safety.UnsafePathError, FileNotFoundError, OSError) as e:
                    self.log(f"No se aisló {ruta}: {e}", "Duplicados")
            self.log(f"Aisladas {movidos} copia(s). Revisá la pestaña Cuarentena.", "Duplicados")
            self.duplicate_groups = []

        self.run_async(task)

    def on_browser_report(self) -> None:
        def task():
            self.set_status("Midiendo caché de navegadores...")
            self.log_lines(browser.summarize(), "Navegadores")

        self.run_async(task)

    def on_startup_report(self) -> None:
        def task():
            self.set_status("Leyendo programas de inicio...")
            self.log_lines(startup_mod.summarize(), "Inicio")

        self.run_async(task)

    def on_build_report(self) -> None:
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


if __name__ == "__main__":
    app = LimpiezaTotalOmegaApp()
    app.mainloop()
