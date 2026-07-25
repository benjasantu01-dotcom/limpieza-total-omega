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
import customtkinter as ctk
from organizer import scan_for_junk, sort_junk, stage_for_review, delete_reviewed
from scanner import scan_directory, run_windows_defender_quick_scan

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class LimpiezaTotalOmegaApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Limpieza Total Omega")
        self.geometry("820x560")

        self.junk_files = []

        self._build_layout()

    def _build_layout(self):
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
        self.output.insert("end", text + "\n")
        self.output.see("end")

    def run_async(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def on_scan_junk(self):
        def task():
            self.log("Buscando archivos basura...")
            self.junk_files = scan_for_junk()
            self.log(f"Encontrados {len(self.junk_files)} candidatos.")
            self.refresh_list()
        self.run_async(task)

    def refresh_list(self):
        self.output.delete("1.0", "end")
        ordered = sort_junk(self.junk_files, by=self.sort_by.get())
        for jf in ordered:
            self.log(f"{jf.size_mb:>8} MB  |  {jf.modified:%Y-%m-%d}  |  {jf.path}")

    def on_stage(self):
        def task():
            if not self.junk_files:
                self.log("Primero hacé 'Buscar basura'.")
                return
            dest = stage_for_review(self.junk_files)
            self.log(f"Movidos {len(self.junk_files)} archivos a: {dest}")
            self.junk_files = []
        self.run_async(task)

    def on_delete_reviewed(self):
        def task():
            n = delete_reviewed()
            self.log(f"Borrados {n} archivos de la carpeta de revisión.")
        self.run_async(task)

    def on_heuristic_scan(self):
        def task():
            import os
            self.log("Escaneo heurístico en Descargas...")
            results = scan_directory(os.path.expanduser("~/Downloads"))
            if not results:
                self.log("Sin hallazgos sospechosos.")
            for r in results:
                self.log(f"[{r.severity.upper()}] {r.path} — {r.reason}")
        self.run_async(task)

    def on_defender_scan(self):
        def task():
            self.log("Iniciando escaneo rápido de Windows Defender (puede tardar)...")
            output = run_windows_defender_quick_scan()
            self.log(output)
        self.run_async(task)


if __name__ == "__main__":
    app = LimpiezaTotalOmegaApp()
    app.mainloop()
