"""
ui.py — Stage 2: Keyboard-driven photo culling UI.

Walks through similarity clusters found by ``analyzer.analyze_directory``
and lets the user keep the best photo per cluster via the keyboard:

  1 / 2 / 3   Keep that image; move the others to _Discarded/.
  Space        Accept the auto-recommended best pick.
  S / →        Skip cluster (no files moved).
  U            Undo the last move (persisted across restarts).
  Q / Esc      Quit (progress is saved; safe to relaunch).
"""

from __future__ import annotations

import logging
import queue as _queue
import threading
from pathlib import Path
from typing import List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from analyzer import ImageMetrics, SimilarityCluster, analyze_directory
import config
import journal
import logging_setup
from widgets import (
    make_thumbnail,
    THUMB_WIDTH, BG, FG, ACCENT, BEST_BG, BEST_FG, PANEL_BG, DIM,
    DISCARDED_DIRNAME,
)

logger = logging.getLogger("snapsort.ui")


class PhotoCullerUI(tk.Tk):
    """Keyboard-driven side-by-side similarity review window."""

    def __init__(self, organized_folder: str) -> None:
        super().__init__()
        self.title("SnapSort — Photo Culler")
        self.configure(bg=BG)
        self.geometry("1320x860")
        self.minsize(1100, 700)

        self.organized_folder = str(Path(organized_folder).expanduser().absolute())
        self.queue: List[SimilarityCluster] = []
        self.queue_index = 0
        self._analysis_done = False
        self._msg_q: "_queue.Queue" = _queue.Queue()
        self.undo_stack = journal.load_undo_stack(self.organized_folder)
        self._photo_refs: List[object] = []
        self._thumb_frames: List[tk.Frame] = []

        self._build_widgets()
        self._bind_hotkeys()
        self.protocol("WM_DELETE_WINDOW", self._on_quit)
        self.status_var.set("Analyzing folder…")
        self._start_analysis()
        self.after(100, self._poll_analysis_queue)

    # ------------------------------------------------------------------
    # Analysis thread
    # ------------------------------------------------------------------

    def _start_analysis(self) -> None:
        logger.info("Analyzing: %s", self.organized_folder)

        def _worker() -> None:
            try:
                per_folder = analyze_directory(
                    self.organized_folder,
                    progress_cb=lambda done, total: self._msg_q.put(("progress", done, total)),
                    config=config.load_config(self.organized_folder),
                )
            except Exception as exc:
                logger.exception("Analysis failed: %s", self.organized_folder)
                self._msg_q.put(("error", str(exc), 0))
                return
            clusters: List[SimilarityCluster] = []
            for folder_clusters in per_folder.values():
                clusters.extend(folder_clusters)
            clusters.sort(key=lambda c: (c.folder, -len(c.members)))
            self._msg_q.put(("done", clusters, 0))

        threading.Thread(target=_worker, daemon=True, name="snapsort-analysis").start()

    def _poll_analysis_queue(self) -> None:
        try:
            while True:
                self._handle_analysis_msg(self._msg_q.get_nowait())
        except _queue.Empty:
            pass
        if not self._analysis_done:
            self.after(100, self._poll_analysis_queue)

    def _handle_analysis_msg(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "progress":
            _, done, total = msg
            if total:
                self.progress_bar.configure(maximum=total, value=done)
                self.status_var.set(f"Analyzing… folder {done}/{total}")
        elif kind == "error":
            self._analysis_done = True
            messagebox.showerror("SnapSort", f"Analysis failed:\n{self.organized_folder}", parent=self)
            self._show_empty_state()
        else:
            self.queue = list(msg[1])
            self._analysis_done = True
            self.progress_bar["value"] = self.progress_bar["maximum"]
            self._load_current() if self.queue else self._show_empty_state()

    # ------------------------------------------------------------------
    # Widget construction
    # ------------------------------------------------------------------

    def _build_widgets(self) -> None:
        top = tk.Frame(self, bg=BG)
        top.pack(side=tk.TOP, fill=tk.X, padx=16, pady=(12, 4))
        self.folder_label = tk.Label(top, text="", bg=BG, fg=DIM,
                                     font=("Helvetica", 11, "bold"), anchor="w")
        self.folder_label.pack(side=tk.LEFT)
        self.progress_label = tk.Label(top, text="", bg=BG, fg=ACCENT,
                                       font=("Helvetica", 11), anchor="e")
        self.progress_label.pack(side=tk.RIGHT)

        self.progress_bar = ttk.Progressbar(self, length=400, mode="determinate")
        self.progress_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 4))

        self.stage = tk.Frame(self, bg=BG)
        self.stage.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)

        legend = tk.Frame(self, bg=PANEL_BG)
        legend.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 12))
        tk.Label(
            legend,
            text="[1/2/3] Keep photo      [Space] Best pick      [S/→] Skip      [U] Undo      [Q] Quit",
            bg=PANEL_BG, fg=DIM, font=("Helvetica", 10), anchor="w",
        ).pack(fill=tk.X, padx=8, pady=6)

        log_file = logging_setup.get_log_file()
        self.status_var = tk.StringVar(value=f"Ready.   |   Log: {log_file}")
        tk.Label(self, textvariable=self.status_var, bg=BG, fg=FG,
                 font=("Helvetica", 10), anchor="w").pack(
            side=tk.BOTTOM, fill=tk.X, padx=20)

    def _bind_hotkeys(self) -> None:
        for key, idx in [("<KeyPress-1>", 0), ("<KeyPress-2>", 1), ("<KeyPress-3>", 2)]:
            self.bind(key, lambda e, i=idx: self._on_keep(i))
        self.bind("<space>", lambda e: self._on_auto())
        self.bind("<KeyPress-s>", lambda e: self._on_skip())
        self.bind("<KeyPress-S>", lambda e: self._on_skip())
        self.bind("<Right>", lambda e: self._on_skip())
        self.bind("<KeyPress-u>", lambda e: self._on_undo())
        self.bind("<KeyPress-U>", lambda e: self._on_undo())
        self.bind("<KeyPress-q>", lambda e: self._on_quit())
        self.bind("<Escape>", lambda e: self._on_quit())
        self.bind("<Button-1>", self._on_panel_click)

    # ------------------------------------------------------------------
    # Cluster rendering
    # ------------------------------------------------------------------

    def _clear_stage(self) -> None:
        for w in self.stage.winfo_children():
            w.destroy()
        self._photo_refs.clear()
        self._thumb_frames.clear()
        self._current_members: List[ImageMetrics] = []
        self._current_best: Optional[ImageMetrics] = None

    def _load_current(self) -> None:
        self._clear_stage()
        cluster = self.queue[self.queue_index]
        self._current_cluster = cluster
        self.folder_label.config(text=Path(cluster.folder).name or cluster.folder)
        total = len(self.queue)
        self.progress_label.config(text=f"Cluster {self.queue_index + 1} / {total}")
        self.progress_bar.configure(maximum=max(1, total), value=self.queue_index + 1)

        members = cluster.sorted_members()
        best = cluster.best_pick()
        self._current_members = members
        self._current_best = best
        n = len(members)
        self.stage.grid_columnconfigure(list(range(n)), weight=1, uniform="pane")
        self.stage.grid_rowconfigure(0, weight=1)

        for col, metric in enumerate(members):
            is_best = metric is best
            pane_bg = BEST_BG if is_best else PANEL_BG
            pane = tk.Frame(self.stage, bg=pane_bg, padx=6, pady=6)
            pane.grid(row=0, column=col, sticky="nsew", padx=6)
            self._thumb_frames.append(pane)

            badge = f"[{col + 1}]" + ("   ★ RECOMMENDED BEST PICK" if is_best else "")
            tk.Label(pane, text=badge, bg=pane_bg,
                     fg=BEST_FG if is_best else ACCENT,
                     font=("Helvetica", 11, "bold")).pack(anchor="w", padx=4)

            thumb = make_thumbnail(metric.path)
            thumb_frame = tk.Frame(pane, bg="#111111")
            thumb_frame.pack(padx=4, pady=(2, 4))
            if thumb is not None:
                self._photo_refs.append(thumb)
                tk.Label(thumb_frame, image=thumb, bg="#111111").pack()
            else:
                tk.Label(thumb_frame, text="[unavailable]",
                         bg="#111111", fg=DIM, font=("Helvetica", 11)).pack(padx=40, pady=40)

            sharp = f"{metric.sharpness:,.0f}" if metric.sharpness is not None else "n/a"
            tilt = f"{metric.tilt_score:.1f}°" if metric.tilt_score is not None else "n/a"
            tk.Label(pane, text=f"Sharpness {sharp}   Tilt {tilt} ({metric.dominant_axis or '—'})",
                     bg=pane_bg, fg=FG, font=("Helvetica", 10)).pack(anchor="w", padx=4)
            tk.Label(pane, text=Path(metric.path).name, bg=pane_bg, fg=DIM,
                     font=("Helvetica", 9), anchor="w",
                     wraplength=THUMB_WIDTH).pack(anchor="w", padx=4, pady=(0, 2))

            for w in (pane, thumb_frame):
                w.bind("<Button-1>", lambda e, i=col: self._on_keep(i))

        self.status_var.set(
            f"{len(cluster.members)} similar photos — "
            f"press 1-{min(n, 3)} to keep, Space for best pick, S to skip."
        )

    def _show_empty_state(self) -> None:
        self._clear_stage()
        self.folder_label.config(text=self.organized_folder)
        self.progress_label.config(text="Done")
        tk.Label(self.stage,
                 text="No similar-photo clusters found.\nYour library is already clean.",
                 bg=BG, fg=BEST_FG, font=("Helvetica", 16, "bold")).place(
            relx=0.5, rely=0.5, anchor="center")
        self.status_var.set("All clusters processed.")

    # ------------------------------------------------------------------
    # Hotkey handlers
    # ------------------------------------------------------------------

    def _on_keep(self, index: int) -> None:
        members = getattr(self, "_current_members", [])
        if 0 <= index < len(members):
            self._select(members[index], auto=False)

    def _on_auto(self) -> None:
        best = getattr(self, "_current_best", None)
        if best is not None:
            self._select(best, auto=True)

    def _on_panel_click(self, event) -> None:
        widget = event.widget
        for i, pane in enumerate(self._thumb_frames):
            w = widget
            while w is not None:
                if w is pane:
                    self._on_keep(i)
                    return
                w = w.master

    def _on_skip(self) -> None:
        self.status_var.set("Cluster skipped.")
        self._advance()

    def _on_undo(self) -> None:
        entry = journal.pop_undo(self.organized_folder)
        if entry is None:
            self.status_var.set("Nothing to undo.")
            return
        try:
            Path(entry["original"]).parent.mkdir(parents=True, exist_ok=True)
            journal.safe_move(
                self.organized_folder, entry["source"], entry["original"],
                op="undo", restore=entry["source"],
            )
            self.status_var.set(f"Undid: {Path(entry['original']).name}")
        except (OSError, ValueError) as exc:
            logger.error("Undo failed: %s", exc)
            self.status_var.set(f"Undo failed: {exc}")

    def _on_quit(self) -> None:
        self.destroy()

    # ------------------------------------------------------------------
    # Selection / file operations
    # ------------------------------------------------------------------

    def _discarded_dir(self, folder: str) -> str:
        event = Path(folder).name or folder
        dest = Path(self.organized_folder) / DISCARDED_DIRNAME / event
        dest.mkdir(parents=True, exist_ok=True)
        return str(dest)

    def _unique_dest(self, dest_dir: str, filename: str) -> str:
        p = Path(filename)
        base, ext = p.stem, p.suffix
        candidate = Path(dest_dir) / filename
        counter = 1
        while candidate.exists():
            candidate = Path(dest_dir) / f"{base}_{counter}{ext}"
            counter += 1
        return str(candidate)

    def _select(self, keep: ImageMetrics, auto: bool) -> None:
        cluster: SimilarityCluster = self._current_cluster
        discarded_dir = self._discarded_dir(cluster.folder)
        event = Path(cluster.folder).name or cluster.folder
        moved = 0
        try:
            for metric in cluster.members:
                if metric is keep or not Path(metric.path).exists():
                    if metric is not keep:
                        logger.warning("Missing (already culled?): %s", metric.path)
                    continue
                dest = self._unique_dest(discarded_dir, Path(metric.path).name)
                if Path(metric.path).resolve() == Path(dest).resolve():
                    continue
                journal.safe_move(
                    self.organized_folder, metric.path, dest,
                    op="reject", restore=metric.path, event=event,
                )
                journal.push_undo(
                    self.organized_folder,
                    source=dest, original=metric.path, kind="reject",
                )
                moved += 1
        except (OSError, ValueError) as exc:
            logger.error("Move failed: %s", exc)
            self.status_var.set(f"Move failed: {exc}")
            return

        verb = "Best pick accepted" if auto else "Kept"
        self.status_var.set(
            f"{verb} {Path(keep.path).name}; "
            f"{moved} photo(s) moved to {DISCARDED_DIRNAME}/{event}/."
        )
        self._advance()

    def _advance(self) -> None:
        self.queue_index += 1
        if self.queue_index >= len(self.queue):
            self._show_empty_state()
        else:
            self._load_current()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_culler(organized_folder: str) -> None:
    """Launch the culling UI on an organized folder."""
    PhotoCullerUI(organized_folder).mainloop()


def pick_folder_and_run(initial: Optional[str] = None) -> None:
    """Prompt the user for a folder, then launch the culling UI."""
    root = tk.Tk()
    root.withdraw()
    root.update()
    chosen = filedialog.askdirectory(
        title="Select the organized photos folder",
        initialdir=initial or str(Path.home()),
        parent=root,
    )
    root.destroy()
    if chosen:
        run_culler(chosen)
    else:
        logger.info("User cancelled folder selection.")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_culler(sys.argv[1]) if len(sys.argv) >= 2 else pick_folder_and_run()
