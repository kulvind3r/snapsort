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

from analyzer import (
    ImageMetrics,
    SimilarityCluster,
    analyze_directory,
    analyze_directory_full,
    QualityReviewItem,
)
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
        self._quality_queue: List[QualityReviewItem] = []
        self._quality_index = 0
        self._stage = 1          # 1 = quality review, 2 = cluster review
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
                cfg = config.load_config(self.organized_folder)

                def on_progress(done, total, new_issues, new_clusters):
                    self._msg_q.put(("batch", done, total, new_issues, new_clusters))

                all_quality, all_clusters = analyze_directory_full(
                    self.organized_folder,
                    progress_cb=on_progress,
                    config=cfg,
                )
                self._msg_q.put(("done", all_quality, all_clusters))
            except Exception as exc:
                logger.exception("Analysis failed: %s", self.organized_folder)
                self._msg_q.put(("error", str(exc), 0))

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
        if kind == "batch":
            _, done, total, new_issues, new_clusters = msg
            if total:
                self.progress_bar.configure(maximum=total, value=done)
                self.status_var.set(f"Analyzing… folder {done}/{total}")
            # Stream: show first items as soon as they arrive
            had_quality = bool(self._quality_queue)
            had_clusters = bool(self.queue)
            self._quality_queue.extend(new_issues)
            self.queue.extend(new_clusters)
            if not had_quality and self._quality_queue and self._stage == 1:
                self._load_quality_item()
            elif not had_clusters and self.queue and self._stage == 2:
                self._load_current()
        elif kind == "done":
            _, all_quality, all_clusters = msg
            # Merge any remaining items not yet added via batch (edge case)
            existing_bad = {qi.bad.path for qi in self._quality_queue}
            existing_cl = {id(c) for c in self.queue}
            for qi in all_quality:
                if qi.bad.path not in existing_bad:
                    self._quality_queue.append(qi)
            for cl in all_clusters:
                if id(cl) not in existing_cl:
                    self.queue.append(cl)
            self._analysis_done = True
            self.progress_bar["value"] = self.progress_bar["maximum"]
            # If nothing is showing yet, bootstrap the stages now
            if not self._quality_queue and not self.queue:
                self._show_empty_state()
            elif self._stage == 1 and self._quality_index >= len(self._quality_queue):
                self._enter_stage2()
            elif self._stage == 2 and self.queue_index >= len(self.queue):
                self._show_empty_state()
        elif kind == "error":
            self._analysis_done = True
            messagebox.showerror("SnapSort — Analysis Failed",
                                 f"Analysis failed:\n{msg[1]}\n\nFolder: {self.organized_folder}",
                                 parent=self)
            self._show_empty_state()

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
            text="Stage 1 — [D] Discard blurry   [Space] Keep   [S/→] Skip"
                 "      ‖      "
                 "Stage 2 — [1/2/3] Keep photo   [Space] Best pick   [S/→] Skip"
                 "      ‖      [U] Undo   [Q] Quit",
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
        self.bind("<KeyPress-d>", lambda e: self._on_quality_discard())
        self.bind("<KeyPress-D>", lambda e: self._on_quality_discard())
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
        self.folder_label.config(
            text=f"Stage 2 of 2: Duplicate Review  —  {Path(cluster.folder).name or cluster.folder}"
        )
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
        q_done = len(self._quality_queue)
        c_done = len(self.queue)
        if q_done == 0 and c_done == 0:
            msg = "No quality issues or duplicate clusters found.\nYour library looks great!"
        elif c_done == 0:
            msg = f"All {q_done} quality issue(s) reviewed.\nNo duplicate clusters found."
        elif q_done == 0:
            msg = f"All {c_done} duplicate cluster(s) reviewed."
        else:
            msg = f"All {q_done} quality issue(s) and {c_done} duplicate cluster(s) reviewed."
        tk.Label(self.stage, text=msg, bg=BG, fg=BEST_FG,
                 font=("Helvetica", 15, "bold")).place(relx=0.5, rely=0.5, anchor="center")
        self.status_var.set("Culling complete.")

    # ------------------------------------------------------------------
    # Stage navigation helpers
    # ------------------------------------------------------------------

    def _enter_stage2(self) -> None:
        """Transition from Stage 1 to Stage 2 (similarity cluster review)."""
        self._stage = 2
        if self.queue:
            self.queue_index = 0
            self._load_current()
        elif self._analysis_done:
            self._show_empty_state()
        # else: clusters still arriving via streaming; _handle_analysis_msg will call _load_current

    # ------------------------------------------------------------------
    # Stage 1 — Quality (blurry/shaky) review
    # ------------------------------------------------------------------

    def _load_quality_item(self) -> None:
        """Render the current Stage 1 quality review item."""
        self._clear_stage()
        if self._quality_index >= len(self._quality_queue):
            self._enter_stage2()
            return
        item = self._quality_queue[self._quality_index]
        self._current_quality_item = item
        total_q = len(self._quality_queue)

        self.folder_label.config(
            text=f"Stage 1 of 2: Quality Review  —  {Path(item.folder).name or item.folder}"
        )
        self.progress_label.config(text=f"Issue {self._quality_index + 1} / {total_q}")
        self.progress_bar.configure(maximum=max(1, total_q), value=self._quality_index + 1)

        self.stage.grid_columnconfigure([0, 1], weight=1, uniform="pane")
        self.stage.grid_rowconfigure(0, weight=1)

        # Left: bad photo
        self._build_quality_pane(col=0, metric=item.bad, is_bad=True)

        # Right: best alternative, or placeholder
        alt = item.best_alternative()
        if alt:
            self._build_quality_pane(col=1, metric=alt, is_bad=False)
        else:
            ph = tk.Frame(self.stage, bg=PANEL_BG, padx=6, pady=6)
            ph.grid(row=0, column=1, sticky="nsew", padx=6)
            tk.Label(ph, text="No similar photo\nfound in this folder",
                     bg=PANEL_BG, fg=DIM, font=("Helvetica", 13, "italic")).place(
                relx=0.5, rely=0.4, anchor="center")
            tk.Label(ph, text="Discard only if confident this is not worth keeping.",
                     bg=PANEL_BG, fg=DIM, font=("Helvetica", 9)).place(
                relx=0.5, rely=0.6, anchor="center")

        alt_note = "(sharp alternative shown →)" if alt else "(no similar photo found)"
        self.status_var.set(
            f"Blurry/shaky photo detected  {alt_note}  —  "
            f"[D] Discard   [Space] Keep anyway   [S/→] Skip"
        )

    def _build_quality_pane(self, col: int, metric: ImageMetrics, is_bad: bool) -> None:
        """Build one pane for Stage 1 quality review."""
        bg = "#3a1a1a" if is_bad else BEST_BG
        label_fg = "#ef5350" if is_bad else BEST_FG
        pane = tk.Frame(self.stage, bg=bg, padx=6, pady=6)
        pane.grid(row=0, column=col, sticky="nsew", padx=6)
        self._thumb_frames.append(pane)

        badge = "⚠  LOW QUALITY — [D] DISCARD" if is_bad else "✓  BEST AVAILABLE ALTERNATIVE"
        tk.Label(pane, text=badge, bg=bg, fg=label_fg,
                 font=("Helvetica", 11, "bold")).pack(anchor="w", padx=4)

        thumb = make_thumbnail(metric.path)
        thumb_frame = tk.Frame(pane, bg="#111111")
        thumb_frame.pack(padx=4, pady=(2, 4))
        if thumb:
            self._photo_refs.append(thumb)
            tk.Label(thumb_frame, image=thumb, bg="#111111").pack()
        else:
            tk.Label(thumb_frame, text="[unavailable]", bg="#111111", fg=DIM,
                     font=("Helvetica", 11)).pack(padx=40, pady=40)

        sharp = f"{metric.sharpness:,.0f}" if metric.sharpness is not None else "n/a"
        tilt = f"{metric.tilt_score:.1f}°" if metric.tilt_score is not None else "n/a"
        tk.Label(pane, text=f"Sharpness {sharp}   Tilt {tilt}",
                 bg=bg, fg=FG, font=("Helvetica", 10)).pack(anchor="w", padx=4)
        tk.Label(pane, text=Path(metric.path).name, bg=bg, fg=DIM,
                 font=("Helvetica", 9), anchor="w", wraplength=THUMB_WIDTH).pack(
            anchor="w", padx=4, pady=(0, 2))

    def _on_quality_discard(self) -> None:
        """Stage 1: move the bad photo to _Discarded."""
        if self._stage != 1:
            return
        item = getattr(self, "_current_quality_item", None)
        if item is None or not Path(item.bad.path).exists():
            self._advance()
            return
        event = Path(item.folder).name or item.folder
        discarded_dir = self._discarded_dir(item.folder)
        try:
            dest = self._unique_dest(discarded_dir, Path(item.bad.path).name)
            journal.safe_move(
                self.organized_folder, item.bad.path, dest,
                op="quality-reject", restore=item.bad.path, event=event,
            )
            journal.push_undo(
                self.organized_folder,
                source=dest, original=item.bad.path, kind="quality-reject",
            )
            self.status_var.set(f"Discarded: {Path(item.bad.path).name}")
        except (OSError, ValueError) as exc:
            logger.error("Quality discard failed: %s", exc)
            self.status_var.set(f"Discard failed: {exc}")
            return
        self._advance()

    # ------------------------------------------------------------------
    # Hotkey handlers
    # ------------------------------------------------------------------

    def _on_keep(self, index: int) -> None:
        if self._stage != 2:
            return
        members = getattr(self, "_current_members", [])
        if 0 <= index < len(members):
            self._select(members[index], auto=False)

    def _on_auto(self) -> None:
        if self._stage == 1:
            # Space in Stage 1 = keep the bad photo (skip discard)
            item = getattr(self, "_current_quality_item", None)
            if item is not None:
                self.status_var.set(f"Kept: {Path(item.bad.path).name}")
            self._advance()
            return
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
        if self._stage == 1:
            item = getattr(self, "_current_quality_item", None)
            if item is not None:
                self.status_var.set(f"Kept: {Path(item.bad.path).name}")
            self._advance()
        else:
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
        if self._stage == 1:
            self._quality_index += 1
            if self._quality_index < len(self._quality_queue):
                self._load_quality_item()
            elif self._analysis_done or not self._quality_queue:
                self._enter_stage2()
            # else: more quality items still arriving via streaming; wait
        else:
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
