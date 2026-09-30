"""桌面悬浮窗：抽选 / 切歌 / 暂停 / 播放列表（置顶、可拖拽、列表可折叠）。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import ui_theme


class FloatingWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("浮窗")
        self.overrideredirect(True)            # 无边框，更像悬浮控件
        self.attributes("-topmost", True)      # 始终置顶
        # 沿用主界面亚克力（玻璃）效果：跟随设置里的 玻璃/纯色/透明度/色调
        glass = bool(self.app.glass_var.get())
        solid = bool(self.app.solid_var.get())
        see_through = glass and not solid
        self.configure(bg=ui_theme.GLASS_KEY if see_through else ui_theme.BG_SOLID)
        ui_theme.setup_style(self, glass=see_through)
        if glass:
            self.after(60, self._apply_glass_effect)

        self._drag = {"x": 0, "y": 0}
        self._list_open = False
        self._last_count = -1

        # 标题条（可拖拽移动）
        bar = ttk.Frame(self, style="Head.TFrame")
        bar.pack(fill="x")
        ttk.Label(bar, text="🎵 浮窗", style="Head.TLabel").pack(
            side="left", padx=8, pady=4)
        ttk.Button(bar, text="📋", width=3, style="Tool.TButton",
                   command=self._toggle_list).pack(side="right", padx=(2, 2))
        ttk.Button(bar, text="✕", width=3, style="Tool.TButton",
                   command=self.destroy).pack(side="right", padx=(2, 6))
        bar.bind("<ButtonPress-1>", self._start_drag)
        bar.bind("<B1-Motion>", self._on_drag)

        # 当前歌曲
        self.now_lbl = ttk.Label(self, text="未在播放", style="Sub.TLabel", anchor="w")
        self.now_lbl.pack(fill="x", padx=10, pady=(6, 2))

        # 控制按钮：上一首 / 暂停-继续 / 下一首 / 抽选
        ctl = ttk.Frame(self)
        ctl.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Button(ctl, text="⏮", width=3, style="Tool.TButton",
                   command=self._prev).pack(side="left", expand=True, padx=2)
        self.btn_pause = ttk.Button(ctl, text="⏸", width=3, style="Tool.TButton",
                                    command=self._toggle_pause)
        self.btn_pause.pack(side="left", expand=True, padx=2)
        ttk.Button(ctl, text="⏭", width=3, style="Tool.TButton",
                   command=self._next).pack(side="left", expand=True, padx=2)
        ttk.Button(ctl, text="🎲", width=3, style="Tool.TButton",
                   command=self._shuffle).pack(side="left", expand=True, padx=2)

        # 播放列表（默认收起，点 📋 展开）
        self.list_frame = ttk.Frame(self)
        self.song_box = tk.Listbox(self.list_frame, **ui_theme.listbox_kwargs(), height=10)
        self.song_box.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        sbar = ttk.Scrollbar(self.list_frame, command=self.song_box.yview)
        sbar.pack(side="right", fill="y", pady=(0, 8))
        self.song_box.configure(yscrollcommand=sbar.set)
        self.song_box.bind("<Double-Button-1>", self._on_pick)

        # 初始位置：屏幕右下角
        self.update_idletasks()
        w, h = self.winfo_width() or 240, self.winfo_height() or 120
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{sw - w - 24}+{sh - h - 24}")

        self._poll()

    # ---------------- 亚克力效果 ----------------
    def _apply_glass_effect(self) -> None:
        """沿用主界面亚克力：跟随 玻璃/纯色/透明度/色调 设置。"""
        app = self.app
        if not bool(app.glass_var.get()):
            return
        alpha = float(app.alpha_var.get())
        if bool(app.solid_var.get()):
            ui_theme.enable_translucent(self, alpha=alpha)
        else:
            tint = int(float(app.tint_var.get() or 235))
            ui_theme.enable_glass(self, alpha=alpha, tint_alpha=tint)

    # ---------------- 拖拽 ----------------
    def _start_drag(self, e):
        self._drag["x"] = e.x
        self._drag["y"] = e.y

    def _on_drag(self, e):
        x = self.winfo_x() + e.x - self._drag["x"]
        y = self.winfo_y() + e.y - self._drag["y"]
        self.geometry(f"+{x}+{y}")

    # ---------------- 列表 ----------------
    def _toggle_list(self):
        self._list_open = not self._list_open
        if self._list_open:
            self.list_frame.pack(fill="both", expand=True)
            self._refresh_list()
        else:
            self.list_frame.pack_forget()

    def _refresh_list(self):
        box = self.song_box
        box.delete(0, "end")
        for i, s in enumerate(self.app.songs):
            box.insert("end", f"{i + 1:>3}. {s.display}")
        self._last_count = len(self.app.songs)

    def _on_pick(self, e):
        sel = self.song_box.curselection()
        if sel:
            self.app.play_index(sel[0])

    # ---------------- 控制 ----------------
    def _prev(self):
        self.app._prev()

    def _next(self):
        self.app._next()

    def _toggle_pause(self):
        self.app._toggle_pause()

    def _shuffle(self):
        self.app._start_shuffle()

    # ---------------- 与主窗口同步 ----------------
    def _poll(self):
        if not self.winfo_exists():
            return
        app = self.app
        idx = app.current_index
        if 0 <= idx < len(app.songs):
            label = "暂停" if (app._ui_paused or app._paused) else "播放中"
            self.now_lbl.configure(text=f"{app.songs[idx].display}  [{label}]")
            if self._list_open and 0 <= idx < self.song_box.size():
                self.song_box.selection_clear(0, "end")
                self.song_box.selection_set(idx)
                self.song_box.see(idx)
        else:
            self.now_lbl.configure(text="未在播放")
        self.btn_pause.configure(text="▶" if app._ui_paused else "⏸")
        if self._list_open and self._last_count != len(app.songs):
            self._refresh_list()
        self.after(400, self._poll)
