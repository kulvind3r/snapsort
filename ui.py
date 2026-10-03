"""
ui.py — Three-stage keyboard-driven photo culling UI.

Stage 0  Exact-duplicate removal  — fully automated; user sees progress + summary.
Stage 1  Near-duplicate cluster review  — colour-histogram clusters shown as a
         thumbnail grid; user picks the keeper.  Large clusters use a tournament
         bracket (groups of up to 9).
Stage 2  Quality review  — blurry / shaky photos shown beside their sharpest
         alternative; user discards or keeps.

Keyboard shortcuts
------------------
Stage 1  [1-9] select & confirm   [Space] best pick   [S/→] skip   [U] undo   [Q] quit
Stage 2  [D] discard   [Space] keep   [S/→] skip   [U] undo   [Q] quit
"""

from __future__ import annotations

import logging
import math
import queue as _queue
import threading
from pathlib import Path
from typing import List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from analyzer import (
    ImageMetrics,
    SimilarityCluster,
    analyze_directory_full,
    QualityReviewItem,
)
import config
import journal
import logging_setup
from widgets import (
    make_thumbnail,
    BG, FG, ACCENT, BEST_BG, BEST_FG, PANEL_BG, DIM,
    DISCARDED_DIRNAME,
)

logger = logging.getLogger("snapsort.ui")

# Stage identifiers
_STAGE_DEDUP = 0
_STAGE_CLUSTER = 1
_STAGE_QUALITY = 2

# Max photos per tournament page
PAGE_SIZE = 9

# Highlight colour for selected cluster photo
_SEL_BG = "#1a3a5c"
_SEL_FG = "#64b5f6"


class PhotoCullerUI(tk.Toplevel):
    """Three-stage culling window (exact-dedup → cluster review → quality review)."""

    def __init__(self, master: tk.Misc, organized_folder: str) -> None:
        super().__init__(master)
        self.title("SnapSort — Photo Culler")
        self.configure(bg=BG)
        self.geometry("1320x860")
        self.minsize(1100, 700)

        self.organized_folder = str(Path(organized_folder).expanduser().absolute())

        # Stage
        self._stage = _STAGE_DEDUP

        # Dedup stats
        self._dedup_done = False
        self._dedup_groups = 0
        self._dedup_discarded = 0

        # Cluster queue
        self.queue: List[SimilarityCluster] = []
        self.queue_index = 0

        # Tournament state (cluster grid)
        self._tournament_cluster: Optional[SimilarityCluster] = None
        self._tournament_all: List[ImageMetrics] = []
        self._tournament_remaining: List[ImageMetrics] = []
        self._tournament_winner: Optional[ImageMetrics] = None
        self._tournament_round = 1
        self._tournament_page_loaded = False
        self._cluster_page_photos: List[ImageMetrics] = []
        self._cluster_grid_selection: Optional[int] = None
        self._cluster_badge_labels: List[tk.Label] = []

        # Quality queue
        self._quality_queue: List[QualityReviewItem] = []
        self._quality_index = 0

        # Misc
        self._analysis_done = False
        self._msg_q: "_queue.Queue" = _queue.Queue()
        self.undo_stack = journal.load_undo_stack(self.organized_folder)
        self._photo_refs: List[object] = []
        self._thumb_frames: List[tk.Frame] = []
        self._resize_job: Optional[str] = None

        self._build_widgets()
        self._bind_hotkeys()
        self.protocol("WM_DELETE_WINDOW", self._on_quit)
        self.bind("<Configure>", self._on_window_resize)
        self._build_dedup_view()
        self.status_var.set("Step 1/3: Scanning for exact duplicates…")
        self._start_analysis()
        self.after(100, self._poll_analysis_queue)

    # ------------------------------------------------------------------
    # Analysis thread
    # ------------------------------------------------------------------

    def _start_analysis(self) -> None:
        logger.info("Starting culling analysis: %s", self.organized_folder)

        def _worker() -> None:
            try:
                # ── Phase 1: exact duplicate auto-discard ──────────────────
                from deduplicator import auto_discard_exact_duplicates

                def dedup_cb(done: int, total: int, discarded: int) -> None:
                    self._msg_q.put(("dedup_progress", done, total, discarded))

                groups, discarded = auto_discard_exact_duplicates(
                    self.organized_folder, progress_cb=dedup_cb
                )
                self._msg_q.put(("dedup_done", groups, discarded))

                # ── Phase 2: histogram clusters + quality issues ────────────
                cfg = config.load_config(self.organized_folder)

                def analysis_cb(done, total, new_clusters, new_issues):
                    self._msg_q.put(("batch", done, total, new_clusters, new_issues))

                def scan_cb(images_done: int, images_total: int) -> None:
                    self._msg_q.put(("scan_progress", images_done, images_total))

                all_clusters, all_quality = analyze_directory_full(
                    self.organized_folder,
                    progress_cb=analysis_cb,
                    scan_progress_cb=scan_cb,
                    config=cfg,
                )
                self._msg_q.put(("done", all_clusters, all_quality))
            except Exception as exc:
                logger.exception("Analysis failed: %s", self.organized_folder)
                self._msg_q.put(("error", str(exc)))

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

        if kind == "dedup_progress":
            _, done, total, discarded = msg
            if total:
                self.progress_bar.configure(maximum=total, value=done)
            suffix = f"  —  {discarded} file(s) removed" if discarded else ""
            self.status_var.set(
                f"Step 1/3: Scanning… {done}/{total} folders{suffix}"
            )

        elif kind == "dedup_done":
            _, groups, discarded = msg
            self._dedup_done = True
            self._dedup_groups = groups
            self._dedup_discarded = discarded
            self._show_dedup_summary(groups, discarded)
            # Auto-advance to cluster stage after 2 s (or immediately if clusters arrive)
            self.after(2000, self._maybe_enter_cluster_stage)

        elif kind == "scan_progress":
            _, done, total = msg
            if total:
                self.progress_bar.configure(maximum=total, value=done)
            self.status_var.set(
                f"Step 2/3: Computing image histograms… {done}/{total}"
            )

        elif kind == "batch":
            _, done, total, new_clusters, new_issues = msg
            if total:
                self.progress_bar.configure(maximum=total, value=done)
                self.status_var.set(f"Step 2/3: Analysing… folder {done}/{total}")
            had_clusters = bool(self.queue)
            self.queue.extend(new_clusters)
            self._quality_queue.extend(new_issues)
            # Stream: show first cluster as soon as it arrives
            if not had_clusters and self.queue:
                if self._stage == _STAGE_CLUSTER and self.queue_index == 0:
                    self._prepare_and_load_cluster()
                elif self._stage == _STAGE_DEDUP and self._dedup_done:
                    self._enter_cluster_stage()

        elif kind == "done":
            _, all_clusters, all_quality = msg
            existing_cl = {id(c) for c in self.queue}
            existing_q = {qi.bad.path for qi in self._quality_queue}
            for cl in all_clusters:
                if id(cl) not in existing_cl:
                    self.queue.append(cl)
            for qi in all_quality:
                if qi.bad.path not in existing_q:
                    self._quality_queue.append(qi)
            self._analysis_done = True
            self.progress_bar["value"] = self.progress_bar["maximum"]
            if self._stage == _STAGE_DEDUP and self._dedup_done:
                self._enter_cluster_stage()
            elif self._stage == _STAGE_CLUSTER and self.queue_index >= len(self.queue):
                self._enter_quality_stage()
            elif self._stage == _STAGE_QUALITY and self._quality_index >= len(self._quality_queue):
                self._show_empty_state()

        elif kind == "error":
            self._analysis_done = True
            messagebox.showerror(
                "SnapSort — Analysis Failed",
                f"Analysis failed:\n{msg[1]}\n\nFolder: {self.organized_folder}",
                parent=self,
            )
            self._show_empty_state()

    # ------------------------------------------------------------------
    # Widget construction
    # ------------------------------------------------------------------

    def _build_widgets(self) -> None:
        top = tk.Frame(self, bg=BG)
        top.pack(side=tk.TOP, fill=tk.X, padx=16, pady=(12, 4))
        self.folder_label = tk.Label(
            top, text="", bg=BG, fg=DIM, font=("Helvetica", 11, "bold"), anchor="w"
        )
        self.folder_label.pack(side=tk.LEFT)
        self.progress_label = tk.Label(
            top, text="", bg=BG, fg=ACCENT, font=("Helvetica", 11), anchor="e"
        )
        self.progress_label.pack(side=tk.RIGHT)

        self.progress_bar = ttk.Progressbar(self, length=400, mode="determinate")
        self.progress_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 4))

        self.stage = tk.Frame(self, bg=BG)
        self.stage.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)

        legend = tk.Frame(self, bg=PANEL_BG)
        legend.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 8))
        self._legend_var = tk.StringVar(
            value="[1-9] Select & keep   [Space] Best pick   [S/→] Skip"
                  "      ‖      [D] Discard blurry   [Space] Keep"
                  "      ‖      [U] Undo   [Q] Quit"
        )
        tk.Label(
            legend, textvariable=self._legend_var,
            bg=PANEL_BG, fg=DIM, font=("Helvetica", 10), anchor="w",
        ).pack(fill=tk.X, padx=8, pady=6)

        log_file = logging_setup.get_log_file()
        self.status_var = tk.StringVar(value=f"Ready.   |   Log: {log_file}")
        tk.Label(
            self, textvariable=self.status_var, bg=BG, fg=FG,
            font=("Helvetica", 10), anchor="w",
        ).pack(side=tk.BOTTOM, fill=tk.X, padx=20)

    def _bind_hotkeys(self) -> None:
        for key, idx in [
            ("<KeyPress-1>", 0), ("<KeyPress-2>", 1), ("<KeyPress-3>", 2),
            ("<KeyPress-4>", 3), ("<KeyPress-5>", 4), ("<KeyPress-6>", 5),
            ("<KeyPress-7>", 6), ("<KeyPress-8>", 7), ("<KeyPress-9>", 8),
        ]:
            self.bind(key, lambda e, i=idx: self._on_number_key(i))
        self.bind("<space>", lambda e: self._on_auto())
        self.bind("<Return>", lambda e: self._on_auto())
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
    # Stage 0 — Exact-dedup views
    # ------------------------------------------------------------------

    def _build_dedup_view(self) -> None:
        """Show the automated exact-dedup scanning screen."""
        self._clear_stage()
        self.folder_label.config(text="Step 1/3: Removing exact duplicates  (automated)")
        self.progress_label.config(text="")
        tk.Label(
            self.stage, text="🔍  Scanning for byte-identical photos…",
            bg=BG, fg=FG, font=("Helvetica", 14),
        ).place(relx=0.5, rely=0.35, anchor="center")
        tk.Label(
            self.stage,
            text="No input needed — exact copies are auto-discarded to _Discarded/",
            bg=BG, fg=DIM, font=("Helvetica", 10, "italic"),
        ).place(relx=0.5, rely=0.47, anchor="center")

    def _show_dedup_summary(self, groups: int, discarded: int) -> None:
        """Replace the scanning screen with a brief completion summary."""
        self._clear_stage()
        self.folder_label.config(text="Step 1/3: Exact Duplicate Removal  —  Complete")
        self.progress_label.config(text="Done")

        if discarded == 0:
            icon, msg = "✓", "No exact duplicates found"
            sub = "Your library has no byte-identical copies."
        else:
            icon = "🗑"
            msg = (
                f"Auto-discarded {discarded} "
                f"exact duplicate file{'s' if discarded != 1 else ''}"
            )
            sub = (
                f"from {groups} group{'s' if groups != 1 else ''} — "
                f"moved to _Discarded/_ExactDuplicates/  (undoable with [U])"
            )

        tk.Label(self.stage, text=icon, bg=BG, fg=BEST_FG,
                 font=("Helvetica", 36)).place(relx=0.5, rely=0.28, anchor="center")
        tk.Label(self.stage, text=msg, bg=BG, fg=BEST_FG,
                 font=("Helvetica", 14, "bold")).place(relx=0.5, rely=0.43, anchor="center")
        tk.Label(self.stage, text=sub, bg=BG, fg=DIM,
                 font=("Helvetica", 11)).place(relx=0.5, rely=0.53, anchor="center")
        tk.Label(
            self.stage,
            text="Proceeding to cluster review in a moment…  (press [Space] to skip wait)",
            bg=BG, fg=DIM, font=("Helvetica", 9, "italic"),
        ).place(relx=0.5, rely=0.65, anchor="center")

        self.status_var.set(
            f"Dedup done: {discarded} file(s) removed.  Analysing clusters…"
        )

    def _maybe_enter_cluster_stage(self) -> None:
        """Auto-advance after the 2 s dedup summary delay (if still in Stage 0)."""
        if self._stage == _STAGE_DEDUP:
            self._enter_cluster_stage()

    # ------------------------------------------------------------------
    # Stage transitions
    # ------------------------------------------------------------------

    def _enter_cluster_stage(self) -> None:
        self._stage = _STAGE_CLUSTER
        self._update_legend()
        if self.queue:
            self.queue_index = 0
            self._prepare_and_load_cluster()
        elif self._analysis_done:
            self._enter_quality_stage()
        else:
            # Clusters still arriving; show interim message
            self._clear_stage()
            self.folder_label.config(text="Step 2/3: Near-duplicate Cluster Review")
            tk.Label(
                self.stage, text="📊  Building colour clusters…",
                bg=BG, fg=DIM, font=("Helvetica", 13, "italic"),
            ).place(relx=0.5, rely=0.4, anchor="center")

    def _enter_quality_stage(self) -> None:
        self._stage = _STAGE_QUALITY
        self._update_legend()
        if self._quality_queue:
            self._quality_index = 0
            self._load_quality_item()
        elif self._analysis_done:
            self._show_empty_state()

    def _update_legend(self) -> None:
        if self._stage == _STAGE_CLUSTER:
            text = (
                "Cluster Review — [1-9] Select & keep   [Space] Best pick   "
                "[S/→] Skip cluster      ‖      [U] Undo   [Q] Quit"
            )
        elif self._stage == _STAGE_QUALITY:
            text = (
                "Quality Review — [D] Discard blurry   [Space] Keep   "
                "[S/→] Skip      ‖      [U] Undo   [Q] Quit"
            )
        else:
            text = "[U] Undo   [Q] Quit"
        self._legend_var.set(text)

    # ------------------------------------------------------------------
    # Stage 1 — Cluster grid view
    # ------------------------------------------------------------------

    def _prepare_and_load_cluster(self) -> None:
        """Initialise tournament for the current cluster and render the first page."""
        cluster = self.queue[self.queue_index]

        if self._tournament_cluster is not cluster:
            all_members = cluster.sorted_members()   # best quality first, no cap
            self._tournament_cluster = cluster
            self._tournament_all = list(all_members)
            self._tournament_remaining = list(all_members)
            self._tournament_winner = None
            self._tournament_round = 1
            self._tournament_page_loaded = False

        if not self._tournament_page_loaded:
            self._pop_tournament_page()

        self._render_cluster_grid()

    def _pop_tournament_page(self) -> None:
        """Consume the next batch of photos from the tournament queue."""
        winner = self._tournament_winner
        if winner is not None:
            batch = self._tournament_remaining[: PAGE_SIZE - 1]
            self._tournament_remaining = self._tournament_remaining[PAGE_SIZE - 1:]
        else:
            batch = self._tournament_remaining[:PAGE_SIZE]
            self._tournament_remaining = self._tournament_remaining[PAGE_SIZE:]
        self._cluster_page_photos = ([winner] if winner else []) + batch
        self._cluster_grid_selection = None
        self._cluster_badge_labels = []
        self._tournament_page_loaded = True

    def _render_cluster_grid(self) -> None:
        """Render the current tournament page as a thumbnail grid."""
        self._clear_stage()

        cluster = self.queue[self.queue_index]
        page_photos = self._cluster_page_photos
        total_in_cluster = len(self._tournament_all)
        total_clusters = len(self.queue)

        n = len(page_photos)
        n_cols = min(3, n)
        n_rows = math.ceil(n / n_cols) if n_cols else 1

        # Configure grid weights so cells expand to fill space
        for col in range(n_cols):
            self.stage.grid_columnconfigure(col, weight=1, uniform="gc")
        for row in range(n_rows):
            self.stage.grid_rowconfigure(row, weight=1, uniform="gr")

        img_w, img_h = self._thumb_size_grid(n_cols, n_rows)

        for idx, metric in enumerate(page_photos):
            row = idx // n_cols
            col = idx % n_cols
            is_winner = (
                self._tournament_winner is not None
                and metric is self._tournament_winner
            )
            pane_bg = BEST_BG if is_winner else PANEL_BG
            pane = tk.Frame(self.stage, bg=pane_bg, padx=4, pady=4)
            pane.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
            pane.grid_columnconfigure(0, weight=1)
            pane.grid_rowconfigure(1, weight=1)
            self._thumb_frames.append(pane)

            # Badge (row 0)
            if is_winner:
                badge_text, badge_fg = "★  ROUND WINNER", BEST_FG
            else:
                badge_text, badge_fg = f"[{idx + 1}]", ACCENT
            badge_lbl = tk.Label(
                pane, text=badge_text, bg=pane_bg, fg=badge_fg,
                font=("Helvetica", 10, "bold"), anchor="center",
            )
            badge_lbl.grid(row=0, column=0, sticky="ew", padx=2, pady=(4, 2))
            self._cluster_badge_labels.append(badge_lbl)

            # Image (row 1)
            img_frame = tk.Frame(pane, bg="#111111")
            img_frame.grid(row=1, column=0, sticky="nsew", padx=2)
            img_frame.grid_columnconfigure(0, weight=1)
            img_frame.grid_rowconfigure(0, weight=1)
            thumb = make_thumbnail(metric.path, img_w, img_h)
            if thumb is not None:
                self._photo_refs.append(thumb)
                lbl = tk.Label(img_frame, image=thumb, bg="#111111")
                lbl.image = thumb
                lbl.grid(row=0, column=0)
            else:
                tk.Label(
                    img_frame, text="[unavailable]",
                    bg="#111111", fg=DIM, font=("Helvetica", 10),
                ).grid(row=0, column=0, padx=20, pady=20)

            # Filename (row 2)
            tk.Label(
                pane, text=Path(metric.path).name, bg=pane_bg, fg=DIM,
                font=("Helvetica", 8), anchor="center",
                wraplength=max(80, img_w),
            ).grid(row=2, column=0, sticky="ew", padx=2, pady=(2, 4))

            # Click-to-select on pane and image frame
            for widget in (pane, img_frame, badge_lbl):
                widget.bind("<Button-1>", lambda e, i=idx: self._on_grid_click(i))

        # Update header labels
        round_info = (
            f"  (Round {self._tournament_round})"
            if len(self._tournament_all) > PAGE_SIZE else ""
        )
        self.folder_label.config(
            text=f"Step 2/3: Cluster Review  —  {Path(cluster.folder).name or cluster.folder}"
        )
        remaining_after = len(self._tournament_remaining)
        self.progress_label.config(
            text=f"Cluster {self.queue_index + 1}/{total_clusters}{round_info}"
                 f"  •  {total_in_cluster} photos"
        )
        self.progress_bar.configure(
            maximum=max(1, total_clusters), value=self.queue_index
        )

        if remaining_after > 0:
            self.status_var.set(
                f"{total_in_cluster} similar photos — pick 1 to keep  |  "
                f"{remaining_after} more to compare next  •  "
                f"[1-{n}] select  [Space] best pick  [S] skip"
            )
        else:
            self.status_var.set(
                f"{total_in_cluster} similar photos — pick 1 to keep  •  "
                f"[1-{n}] select & confirm  [Space] best pick  [S] skip"
            )

    def _on_grid_click(self, index: int) -> None:
        """Mouse click: select (highlight) without confirming."""
        if self._stage != _STAGE_CLUSTER:
            return
        if 0 <= index < len(self._cluster_page_photos):
            self._cluster_grid_selection = index
            self._update_grid_highlight()

    def _update_grid_highlight(self) -> None:
        """Refresh pane background and badge text to reflect current selection."""
        sel = self._cluster_grid_selection
        for i, pane in enumerate(self._thumb_frames):
            if i >= len(self._cluster_page_photos):
                break
            metric = self._cluster_page_photos[i]
            is_winner = (
                self._tournament_winner is not None
                and metric is self._tournament_winner
            )
            is_selected = (i == sel)

            if is_selected:
                new_bg, badge_text, badge_fg = _SEL_BG, f"✓  [{i + 1}]  SELECTED", _SEL_FG
            elif is_winner:
                new_bg, badge_text, badge_fg = BEST_BG, "★  ROUND WINNER", BEST_FG
            else:
                new_bg, badge_text, badge_fg = PANEL_BG, f"[{i + 1}]", ACCENT

            try:
                pane.configure(bg=new_bg)
            except tk.TclError:
                pass

            if i < len(self._cluster_badge_labels):
                self._cluster_badge_labels[i].configure(
                    text=badge_text, fg=badge_fg, bg=new_bg
                )

            # Update non-image children (filename label etc.)
            for child in pane.winfo_children():
                if child is self._cluster_badge_labels[i] if i < len(self._cluster_badge_labels) else False:
                    continue  # badge already updated
                try:
                    if child.cget("bg") != "#111111":
                        child.configure(bg=new_bg)
                except tk.TclError:
                    pass

    def _confirm_cluster_selection(self) -> None:
        """Confirm current grid selection, advancing tournament or finishing cluster."""
        sel = self._cluster_grid_selection
        if sel is None or sel >= len(self._cluster_page_photos):
            return
        chosen = self._cluster_page_photos[sel]
        self._tournament_winner = chosen
        self._tournament_page_loaded = False  # next call will pop a new page

        if self._tournament_remaining:
            # More photos to compare: next tournament round
            self._tournament_round += 1
            self._pop_tournament_page()
            self._render_cluster_grid()
        else:
            # Tournament complete: discard all except the winner
            self._select_cluster_winner(chosen)

    def _select_cluster_winner(self, winner: ImageMetrics) -> None:
        """Move all non-winner cluster members to _Discarded/ and advance."""
        cluster = self._tournament_cluster or self.queue[self.queue_index]
        discarded_dir = self._discarded_dir(cluster.folder)
        event = Path(cluster.folder).name or cluster.folder
        moved = 0
        try:
            for metric in self._tournament_all:
                if metric is winner or not Path(metric.path).exists():
                    if metric is not winner:
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

        self.status_var.set(
            f"Kept {Path(winner.path).name}; "
            f"{moved} photo(s) moved to {DISCARDED_DIRNAME}/{event}/."
        )
        self._advance()

    # ------------------------------------------------------------------
    # Responsive layout helpers
    # ------------------------------------------------------------------

    def _thumb_size(self, n_cols: int) -> tuple:
        """Thumbnail size for the 2-pane quality view."""
        self.update_idletasks()
        win_w = max(self.winfo_width(), 1100)
        win_h = max(self.winfo_height(), 700)
        h_overhead = 32 + 12 * (n_cols - 1) + 12 * n_cols
        w = max(200, (win_w - h_overhead) // n_cols)
        h = max(150, win_h - 240)
        return w, h

    def _thumb_size_grid(self, n_cols: int, n_rows: int) -> tuple:
        """Thumbnail size for the cluster grid (up to 3×3)."""
        self.update_idletasks()
        win_w = max(self.winfo_width(), 1100)
        win_h = max(self.winfo_height(), 700)
        h_overhead = 48 + 10 * (n_cols - 1) + 8 * n_cols
        w = max(120, (win_w - h_overhead) // n_cols)
        v_overhead = 200 + 58 * n_rows   # header + footer + badge+filename per row
        h = max(80, (win_h - v_overhead) // n_rows)
        return w, h

    def _on_window_resize(self, event) -> None:
        if event.widget is not self:
            return
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(200, self._reload_view)

    def _reload_view(self) -> None:
        self._resize_job = None
        if not self._analysis_done and self._stage == _STAGE_DEDUP:
            return  # still in automated phase, no thumbnails to resize
        if self._stage == _STAGE_CLUSTER:
            if self.queue and self.queue_index < len(self.queue) and self._tournament_page_loaded:
                self._render_cluster_grid()
        elif self._stage == _STAGE_QUALITY:
            if self._quality_queue and self._quality_index < len(self._quality_queue):
                self._load_quality_item()

    # ------------------------------------------------------------------
    # Stage 2 — Quality (blurry/shaky) review
    # ------------------------------------------------------------------

    def _load_quality_item(self) -> None:
        self._clear_stage()
        if self._quality_index >= len(self._quality_queue):
            self._show_empty_state()
            return
        item = self._quality_queue[self._quality_index]
        self._current_quality_item = item
        total_q = len(self._quality_queue)

        self.folder_label.config(
            text=f"Step 3/3: Quality Review  —  {Path(item.folder).name or item.folder}"
        )
        self.progress_label.config(text=f"Issue {self._quality_index + 1} / {total_q}")
        self.progress_bar.configure(maximum=max(1, total_q), value=self._quality_index + 1)

        self.stage.grid_columnconfigure(0, weight=1, uniform="pane")
        self.stage.grid_columnconfigure(1, weight=1, uniform="pane")
        self.stage.grid_rowconfigure(0, weight=1)

        img_w, img_h = self._thumb_size(n_cols=2)

        self._build_quality_pane(col=0, metric=item.bad, is_bad=True,
                                 img_w=img_w, img_h=img_h)

        alt = item.best_alternative()
        if alt:
            self._build_quality_pane(col=1, metric=alt, is_bad=False,
                                     img_w=img_w, img_h=img_h)
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

    def _build_quality_pane(self, col: int, metric: ImageMetrics, is_bad: bool,
                             img_w: int = 400, img_h: int = 400) -> None:
        bg = "#3a1a1a" if is_bad else BEST_BG
        label_fg = "#ef5350" if is_bad else BEST_FG
        pane = tk.Frame(self.stage, bg=bg, padx=6, pady=6)
        pane.grid(row=0, column=col, sticky="nsew", padx=6)
        pane.grid_columnconfigure(0, weight=1)
        pane.grid_rowconfigure(2, weight=1)
        self._thumb_frames.append(pane)

        # Row 0: badge
        badge = "⚠  LOW QUALITY" if is_bad else "✓  BEST AVAILABLE ALTERNATIVE"
        tk.Label(pane, text=badge, bg=bg, fg=label_fg,
                 font=("Helvetica", 11, "bold"), anchor="center").grid(
            row=0, column=0, sticky="ew", padx=4, pady=(8, 2))

        # Row 1: metrics
        sharp = f"{metric.sharpness:,.0f}" if metric.sharpness is not None else "n/a"
        tilt = f"{metric.tilt_score:.1f}°" if metric.tilt_score is not None else "n/a"
        tk.Label(pane, text=f"Sharpness {sharp}   Tilt {tilt}",
                 bg=bg, fg=FG, font=("Helvetica", 10), anchor="center").grid(
            row=1, column=0, sticky="ew", padx=4, pady=(0, 8))

        # Row 2: image
        thumb_frame = tk.Frame(pane, bg="#111111")
        thumb_frame.grid(row=2, column=0, sticky="nsew", padx=4)
        thumb_frame.grid_columnconfigure(0, weight=1)
        thumb_frame.grid_rowconfigure(0, weight=1)
        thumb = make_thumbnail(metric.path, img_w, img_h)
        if thumb:
            self._photo_refs.append(thumb)
            lbl = tk.Label(thumb_frame, image=thumb, bg="#111111")
            lbl.image = thumb
            lbl.grid(row=0, column=0)
        else:
            tk.Label(thumb_frame, text="[unavailable]", bg="#111111", fg=DIM,
                     font=("Helvetica", 11)).grid(row=0, column=0, padx=40, pady=40)

        # Row 3: filename
        tk.Label(pane, text=Path(metric.path).name, bg=bg, fg=DIM,
                 font=("Helvetica", 9), anchor="center",
                 wraplength=max(200, img_w)).grid(
            row=3, column=0, sticky="ew", padx=4, pady=(4, 8))

    def _on_quality_discard(self) -> None:
        if self._stage != _STAGE_QUALITY:
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
    # Empty state
    # ------------------------------------------------------------------

    def _show_empty_state(self) -> None:
        self._clear_stage()
        self.folder_label.config(text=self.organized_folder)
        self.progress_label.config(text="Done")

        dedup_line = (
            f"{self._dedup_discarded} exact duplicate(s) auto-removed"
            if self._dedup_discarded else "No exact duplicates found"
        )
        c_done = len(self.queue)
        q_done = len(self._quality_queue)

        if c_done == 0 and q_done == 0:
            msg = f"Culling complete.\n{dedup_line}\nNo near-duplicate clusters or blurry photos found."
        elif c_done == 0:
            msg = f"Culling complete.\n{dedup_line}\n{q_done} quality issue(s) reviewed."
        elif q_done == 0:
            msg = f"Culling complete.\n{dedup_line}\n{c_done} cluster(s) reviewed."
        else:
            msg = (
                f"Culling complete.\n{dedup_line}\n"
                f"{c_done} cluster(s) and {q_done} quality issue(s) reviewed."
            )
        tk.Label(self.stage, text=msg, bg=BG, fg=BEST_FG,
                 font=("Helvetica", 14, "bold"), justify=tk.CENTER).place(
            relx=0.5, rely=0.5, anchor="center")
        self.status_var.set("Culling complete.")

    # ------------------------------------------------------------------
    # Clear
    # ------------------------------------------------------------------

    def _clear_stage(self) -> None:
        for w in self.stage.winfo_children():
            w.destroy()
        self._photo_refs.clear()
        self._thumb_frames.clear()
        self._cluster_badge_labels.clear()
        self._current_members: List[ImageMetrics] = []
        self._current_best: Optional[ImageMetrics] = None

    # ------------------------------------------------------------------
    # Hotkey handlers
    # ------------------------------------------------------------------

    def _on_number_key(self, index: int) -> None:
        """Number key: select + immediately confirm in cluster stage."""
        if self._stage == _STAGE_CLUSTER:
            if 0 <= index < len(self._cluster_page_photos):
                self._cluster_grid_selection = index
                self._update_grid_highlight()
                self._confirm_cluster_selection()
        elif self._stage == _STAGE_DEDUP and self._dedup_done:
            # Allow Space-equivalent to skip dedup summary
            self._maybe_enter_cluster_stage()

    def _on_auto(self) -> None:
        if self._stage == _STAGE_DEDUP and self._dedup_done:
            self._maybe_enter_cluster_stage()
        elif self._stage == _STAGE_CLUSTER:
            if self._cluster_grid_selection is None:
                # Auto-select: index 0 is the winner/best-quality pick
                if self._cluster_page_photos:
                    self._cluster_grid_selection = 0
                    self._update_grid_highlight()
            self._confirm_cluster_selection()
        elif self._stage == _STAGE_QUALITY:
            item = getattr(self, "_current_quality_item", None)
            if item is not None:
                self.status_var.set(f"Kept: {Path(item.bad.path).name}")
            self._advance()

    def _on_panel_click(self, event) -> None:
        """Route panel clicks to grid selection in stage 1."""
        if self._stage != _STAGE_CLUSTER:
            return
        widget = event.widget
        for i, pane in enumerate(self._thumb_frames):
            w = widget
            while w is not None:
                if w is pane:
                    self._on_grid_click(i)
                    return
                w = getattr(w, "master", None)

    def _on_skip(self) -> None:
        if self._stage == _STAGE_CLUSTER:
            self.status_var.set("Cluster skipped.")
            self._advance()
        elif self._stage == _STAGE_QUALITY:
            item = getattr(self, "_current_quality_item", None)
            if item is not None:
                self.status_var.set(f"Kept: {Path(item.bad.path).name}")
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
    # Advance
    # ------------------------------------------------------------------

    def _advance(self) -> None:
        if self._stage == _STAGE_CLUSTER:
            # Reset tournament state before moving to next cluster
            self._tournament_cluster = None
            self._tournament_page_loaded = False
            self.queue_index += 1
            if self.queue_index < len(self.queue):
                self._prepare_and_load_cluster()
            elif self._analysis_done:
                self._enter_quality_stage()
            # else: more clusters still streaming
        elif self._stage == _STAGE_QUALITY:
            self._quality_index += 1
            if self._quality_index < len(self._quality_queue):
                self._load_quality_item()
            elif self._analysis_done:
                self._show_empty_state()
            # else: more quality items still streaming

    # ------------------------------------------------------------------
    # File-move helpers
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


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_culler(organized_folder: str) -> None:
    """Launch the culling UI on *organized_folder* (standalone entry point)."""
    root = tk.Tk()
    root.withdraw()
    ui = PhotoCullerUI(root, organized_folder)
    root.wait_window(ui)
    root.destroy()


def pick_folder_and_run(initial: Optional[str] = None) -> None:
    """Prompt the user for a folder, then launch the culling UI."""
    root = tk.Tk()
    root.withdraw()
    root.update()
    chosen = filedialog.askdirectory(
        title="Select the photos folder",
        initialdir=initial or str(Path.home()),
        parent=root,
    )
    if chosen:
        ui = PhotoCullerUI(root, chosen)
        root.wait_window(ui)
    else:
        logger.info("User cancelled folder selection.")
    root.destroy()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_culler(sys.argv[1]) if len(sys.argv) >= 2 else pick_folder_and_run()
