"""
main.py — Limpieza Total Omega
App de escritorio para Windows 11. Organiza archivos basura y ofrece
un escaneo heurístico de seguridad. Requiere aprobación manual del
usuario para cualquier acción destructiva (nada se borra solo).

Instalar dependencias:
    pip install customtkinter

Ejecutar:
    python main.py
"""

import threading
import logging
import os
import customtkinter as ctk
from organizer import scan_for_junk, sort_junk, stage_for_review, delete_reviewed
from scanner import scan_directory, run_windows_defender_quick_scan

# Configuración de logging para capturar errores de ejecución en archivo y consola
logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class LimpiezaTotalOmegaApp(ctk.CTk):
    """Clase principal de la aplicación que gestiona la interfaz y la ejecución de tareas."""
    
    def __init__(self):
        super().__init__()
        self.title("Limpieza Total Omega")
        self.geometry("820x560")

        self.junk_files = []
        self._build_layout()

    def _build_layout(self):
        """Inicializa los componentes de la interfaz de usuario."""
        header = ctk.CTkLabel(self, text="🧹 Limpieza Total Omega", font=ctk.CTkFont(size=24, weight="bold"))
        header.pack(pady=(20, 10))

        subtitle = ctk.CTkLabel(self, text="Organizá tu PC. Nada se borra sin que vos lo confirmes.", text_color="gray")
        subtitle.pack(pady=(0, 20))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=10)

        ctk.CTkButton(btn_frame, text="🔍 Buscar basura", command=self.on_scan_junk).grid(row=0, column=0, padx=8)
        ctk.CTkButton(btn_frame, text="📂 Mover a revisión", command=self.on_stage).grid(row=0, column=1, padx=8)
        ctk.CTkButton(btn_frame, text="🗑️ Vaciar revisados", fg_color="#8b2020", hover_color="#6b1818",
                      command=self.on_delete_reviewed).grid(row=0, column=2, padx=8)
        ctk.CTkButton(btn_frame, text="🛡️ Escaneo heurístico", command=self.on_heuristic_scan).grid(row=0, column=3, padx=8)
        ctk.CTkButton(btn_frame, text="🛡️ Defender (real)", command=self.on_defender_scan).grid(row=0, column=4, padx=8)

        sort_frame = ctk.CTkFrame(self, fg_color="transparent")
        sort_frame.pack(pady=(10, 0))
        ctk.CTkLabel(sort_frame, text="Ordenar por:").grid(row=0, column=0, padx=6)
        self.sort_by = ctk.StringVar(value="size")
        ctk.CTkOptionMenu(sort_frame, values=["size", "date"], variable=self.sort_by,
                           command=lambda _: self.refresh_list()).grid(row=0, column=1, padx=6)

        self.output = ctk.CTkTextbox(self, width=760, height=340)
        self.output.pack(pady=20)

    def log(self, text: str):
        """Agrega texto al cuadro de salida de la interfaz de forma segura para hilos."""
        self.after(0, lambda: self.output.insert("end", f"{text}\n"))
        self.after(0, lambda: self.output.see("end"))

    def run_async(self, fn):
        """Ejecuta una función en un hilo separado para no bloquear la interfaz de usuario."""
        def wrapper():
            try:
                fn()
            except PermissionError:
                self.log("Error: Permiso denegado. Verifique permisos de administrador.")
            except FileNotFoundError as e:
                self.log(f"Error: No se encontró la ruta: {e.filename}")
            except OSError as e:
                logging.error("Error de sistema: %s", e)
                self.log(f"Error de sistema ({e.errno}): {e.strerror}")
            except Exception as e:
                logging.exception("Error inesperado en tarea asíncrona: %s", e)
                self.log(f"Error inesperado: {type(e).__name__}")
        
        threading.Thread(target=wrapper, daemon=True).start()

    def on_scan_junk(self):
        """Busca archivos basura de forma asíncrona."""
        def task():
            self.log("Buscando archivos basura...")
            self.junk_files = scan_for_junk()
            self.log(f"Encontrados {len(self.junk_files)} candidatos.")
            self.refresh_list()
        self.run_async(task)

    def refresh_list(self):
        """Limpia y repuebla el cuadro de texto con los archivos encontrados."""
        self.output.delete("1.0", "end")
        ordered = sort_junk(self.junk_files, by=self.sort_by.get())
        for jf in ordered:
            self.log(f"{jf.size_mb:>8} MB  |  {jf.modified:%Y-%m-%d}  |  {jf.path}")

    def on_stage(self):
        """Mueve archivos encontrados a la zona de revisión."""
        def task():
            if not self.junk_files:
                self.log("Primero debe realizar 'Buscar basura'.")
                return
            dest = stage_for_review(self.junk_files)
            self.log(f"Movidos {len(self.junk_files)} archivos a: {dest}")
            self.junk_files = []
        self.run_async(task)

    def on_delete_reviewed(self):
        """Elimina permanentemente los archivos en el directorio de revisión."""
        def task():
            n = delete_reviewed()
            self.log(f"Borrados {n} archivos de la carpeta de revisión.")
        self.run_async(task)

    def on_heuristic_scan(self):
        """Ejecuta un escaneo heurístico en la carpeta de descargas del usuario."""
        def task():
            self.log("Escaneo heurístico en Descargas...")
            downloads_path = os.path.expanduser("~/Downloads")
            if not os.path.isdir(downloads_path):
                self.log("Error: Carpeta de descargas no encontrada o no es un directorio.")
                return
            results = scan_directory(downloads_path)
            if not results:
                self.log("Sin hallazgos sospechosos.")
            for r in results:
                self.log(f"[{r.severity.upper()}] {r.path} — {r.reason}")
        self.run_async(task)

    def on_defender_scan(self):
        """Lanza un escaneo rápido utilizando Windows Defender."""
        def task():
            self.log("Iniciando escaneo rápido de Windows Defender (puede tardar)...")
            output = run_windows_defender_quick_scan()
            self.log(output)
        self.run_async(task)


if __name__ == "__main__":
    app = LimpiezaTotalOmegaApp()
    app.mainloop()
