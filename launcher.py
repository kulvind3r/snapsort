"""
launcher.py — SnapSort main window.

The entry point for the GUI binary. Shows a source folder picker, a
Step 1 card (organise) with live progress, and a Step 2 card (cull
duplicates) that opens the PhotoCullerUI.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, ttk

import config as snapsort_config
import logging_setup
from organizer import organize_directory, summarize_clusters
from widgets import BG, FG, ACCENT, PANEL_BG, DIM, BEST_FG

logger = logging.getLogger("snapsort.launcher")

# Launcher-specific colour tokens
_HEADER_BG  = "#161616"
_CARD_BG    = "#2d2d2d"
_BTN_OFF_BG = "#383838"
_BTN_OFF_FG = "#666666"
_ERROR_FG   = "#ef5350"


class LauncherUI(tk.Tk):
    """Main launcher window: folder picker + organise + cull."""

    def __init__(self) -> None:
        super().__init__()
        self.title("SnapSort")
        self.configure(bg=BG)
        self.geometry("760x530")
        self.minsize(640, 460)

        self._folder    = tk.StringVar()
        self._status    = tk.StringVar()
        self._org_label = tk.StringVar(value="")
        self._msg_q: queue.Queue = queue.Queue()
        self._running   = False

        self._build()
        self._folder.trace_add("write", lambda *_: self._refresh_buttons())
        self._status.set(f"Ready.   |   Log: {logging_setup.get_log_file()}")
        self.protocol("WM_DELETE_WINDOW", self._on_quit)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self._build_header()
        self._build_folder_row()
        self._build_cards()
        self._build_statusbar()

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=_HEADER_BG)
        hdr.pack(fill=tk.X)
        row = tk.Frame(hdr, bg=_HEADER_BG)
        row.pack(anchor="w", padx=24, pady=16)
        tk.Label(row, text="📷  SnapSort", bg=_HEADER_BG, fg=FG,
                 font=("Helvetica", 20, "bold")).pack(side=tk.LEFT)
        tk.Label(row, text="  —  Organise your photo library",
                 bg=_HEADER_BG, fg=DIM, font=("Helvetica", 11)).pack(side=tk.LEFT)

    def _build_folder_row(self) -> None:
        sec = tk.Frame(self, bg=BG)
        sec.pack(fill=tk.X, padx=24, pady=(18, 6))
        tk.Label(sec, text="Source Folder", bg=BG, fg=ACCENT,
                 font=("Helvetica", 11, "bold")).pack(anchor="w")
        row = tk.Frame(sec, bg=BG)
        row.pack(fill=tk.X, pady=(6, 0))
        tk.Entry(
            row, textvariable=self._folder, bg="#2a2a2a", fg=FG,
            insertbackground=FG, font=("Helvetica", 10), relief=tk.FLAT,
            highlightthickness=1, highlightbackground="#444",
            highlightcolor=ACCENT,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=7, ipadx=6)
        tk.Button(
            row, text="  Browse…  ", command=self._browse,
            bg=PANEL_BG, fg=FG, relief=tk.FLAT,
            activebackground=ACCENT, activeforeground="#000",
            font=("Helvetica", 10), cursor="hand2",
        ).pack(side=tk.LEFT, padx=(8, 0), ipady=7)
        tk.Label(
            sec,
            text="ℹ  Photos are never deleted — "
                 "rejected duplicates are moved to _Discarded/",
            bg=BG, fg=DIM, font=("Helvetica", 9),
        ).pack(anchor="w", pady=(7, 0))
        tk.Label(
            sec,
            text="ℹ  Tip: Cull before Organising — similar photos are detected across"
                 " the whole folder and splitting into subfolders can hide duplicates.",
            bg=BG, fg=DIM, font=("Helvetica", 9),
        ).pack(anchor="w", pady=(4, 0))

    def _build_cards(self) -> None:
        frame = tk.Frame(self, bg=BG)
        frame.pack(fill=tk.BOTH, expand=True, padx=24, pady=(8, 14))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)

        # ── Cull card (left — works best on the original flat folder) ──
        c_cull = tk.Frame(frame, bg=_CARD_BG, padx=18, pady=16)
        c_cull.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        tk.Label(c_cull, text="Cull", bg=_CARD_BG, fg=FG,
                 font=("Helvetica", 15, "bold")).pack(anchor="w")
        self._card_desc(
            c_cull,
            "Remove blurry, shaky, and duplicate photos. "
            "Works best on the original unsorted folder — "
            "finds more similar photos before they are split.",
        )

        self._cull_btn = self._make_btn(c_cull, "Start Culling", self._start_cull)
        self._cull_btn.pack(fill=tk.X, ipady=8)

        # ── Organise card (right) ──────────────────────────────────────
        c_org = tk.Frame(frame, bg=_CARD_BG, padx=18, pady=16)
        c_org.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        tk.Label(c_org, text="Organise", bg=_CARD_BG, fg=FG,
                 font=("Helvetica", 15, "bold")).pack(anchor="w")
        self._card_desc(
            c_org,
            "Sort photos into dated event and trip folders "
            "using EXIF date and GPS coordinates. "
            "Can be run on any folder, before or after culling.",
        )

        self._org_btn = self._make_btn(c_org, "Start Organising", self._start_organise)
        self._org_btn.pack(fill=tk.X, ipady=8)

        self._org_bar = ttk.Progressbar(c_org, mode="indeterminate")
        self._org_bar.pack(fill=tk.X, pady=(10, 4))

        self._org_lbl = tk.Label(c_org, textvariable=self._org_label,
                                 bg=_CARD_BG, fg=DIM,
                                 font=("Helvetica", 9), justify=tk.LEFT,
                                 wraplength=260)
        self._org_lbl.pack(anchor="w")

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, bg=PANEL_BG)
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Label(bar, textvariable=self._status, bg=PANEL_BG, fg=DIM,
                 font=("Helvetica", 9), anchor="w").pack(
            fill=tk.X, padx=16, pady=5)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _card_desc(self, parent: tk.Widget, text: str) -> tk.Label:
        """Description label that auto-wraps to the card's actual rendered width."""
        lbl = tk.Label(
            parent, text=text, bg=_CARD_BG, fg=DIM,
            font=("Helvetica", 9), justify=tk.LEFT, wraplength=220,
            anchor="w",
        )
        lbl.pack(fill=tk.X, pady=(8, 14))
        lbl.bind(
            "<Configure>",
            lambda e, l=lbl: l.configure(wraplength=max(80, e.width - 4)),
        )
        return lbl

    def _make_btn(self, parent: tk.Widget, text: str, cmd) -> tk.Button:
        return tk.Button(
            parent, text=text, command=cmd,
            bg=_BTN_OFF_BG, fg=_BTN_OFF_FG,
            relief=tk.FLAT, font=("Helvetica", 10, "bold"),
            cursor="hand2", state=tk.DISABLED,
            activebackground="#0288d1", activeforeground="#fff",
        )

    def _refresh_buttons(self) -> None:
        has_folder = bool(self._folder.get().strip())
        enabled = has_folder and not self._running
        for btn in (self._org_btn, self._cull_btn):
            btn.config(
                state=tk.NORMAL if enabled else tk.DISABLED,
                bg=ACCENT if enabled else _BTN_OFF_BG,
                fg="#000000" if enabled else _BTN_OFF_FG,
            )

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(
            title="Select source photos folder",
            initialdir=self._folder.get() or str(Path.home()),
            parent=self,
        )
        if chosen:
            self._folder.set(chosen)

    # ------------------------------------------------------------------
    # Stage 1 — Organise
    # ------------------------------------------------------------------

    def _start_organise(self) -> None:
        folder = self._folder.get().strip()
        if not folder or self._running:
            return
        self._running = True
        self._refresh_buttons()
        self._org_label.set("Scanning folder…")
        self._org_lbl.config(fg=DIM)
        self._org_bar.start(10)
        self._status.set("Organising photos — please wait…")

        def _worker() -> None:
            try:
                cfg = snapsort_config.load_config(folder)
                records = organize_directory(folder, config_obj=cfg)
                n = len(records)
                dates = len({r.date for r in records if r.date is not None})
                summary = summarize_clusters(records, cfg)
                self._msg_q.put(("ok", n, dates, summary))
            except Exception as exc:
                logger.exception("Stage 1 failed")
                self._msg_q.put(("err", str(exc), 0, ""))

        threading.Thread(target=_worker, daemon=True,
                         name="snapsort-organise").start()
        self.after(120, self._poll_organise)

    def _poll_organise(self) -> None:
        try:
            msg = self._msg_q.get_nowait()
        except queue.Empty:
            self.after(120, self._poll_organise)
            return

        self._org_bar.stop()
        self._running = False
        if msg[0] == "ok":
            _, n, dates, summary = msg
            lines = [f"✓  {n} photos → {dates} date group(s)."]
            lines += [f"   {ln}" for ln in summary.splitlines()[:4]]
            self._org_label.set("\n".join(lines))
            self._org_lbl.config(fg=BEST_FG)
            self._status.set(f"Step 1 complete — {n} photos organised.")
        else:
            self._org_label.set(f"⚠  {msg[1]}")
            self._org_lbl.config(fg=_ERROR_FG)
            self._status.set("Step 1 failed — check the log for details.")
        self._refresh_buttons()

    # ------------------------------------------------------------------
    # Stage 2 — Cull
    # ------------------------------------------------------------------

    def _start_cull(self) -> None:
        folder = self._folder.get().strip()
        if not folder:
            return
        from ui import PhotoCullerUI
        self.withdraw()
        try:
            culler = PhotoCullerUI(self, folder)
            self.wait_window(culler)      # blocks until culler window closes
        finally:
            self.deiconify()
            self.lift()
            self._status.set(
                f"Culling session ended.   |   Log: {logging_setup.get_log_file()}"
            )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _on_quit(self) -> None:
        """Clean shutdown: destroy the window then force-exit the process."""
        self.destroy()
        sys.exit(0)


def run_launcher() -> None:
    """Open the LauncherUI and block until closed."""
    LauncherUI().mainloop()
