"""Bilibili 歌单自动播放器 —— 图形界面入口。

用法：python main.py
"""

from __future__ import annotations

import json
import os
import queue
import random
import re
import sys
import datetime  # noqa: I001
import time
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import List, Optional

import ui_theme
import video_overlay
from bilibili import BiliSearcher, Video, fmt_duration, pick_best
from player import BiliPlayer, find_browser
from playlist import Song, load_playlist, parse_line, save_playlist, dedupe_songs
from settings_dialog import SettingsDialog
from floating_window import FloatingWindow

def _app_dir() -> str:
    """配置/歌单存放目录：打包后固定为 exe 所在目录，保证设置能持久化。"""
    if getattr(sys, "frozen", False):          # PyInstaller 打包后
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _bundle_dir() -> str:
    """随程序打包进来的资源目录（video.mp4 等）。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()
BUNDLE_DIR = _bundle_dir()
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

MATCH_FIRST = "搜索结果第一首"
MATCH_BEST = "智能最佳匹配"

SHUFFLE_COUNT = 2         # 抽选模式默认抽取的歌曲数（可在设置里改）
SHUFFLE_STEPS = 26        # 抽取动画的帧数（越大转得越久）
SHUFFLE_VIDEO_MIN_MS = 1200   # 动画太短时至少循环播放这么久
SHUFFLE_VIDEO_MAX_MS = 8000   # 兜底：视频过长时最多放这么久（正常是播完即结束）
PITY_CAP_HOURS = 72           # 后台保底：一首歌「多久没被抽到」的加成封顶（小时）

FILE_TYPES = [
    ("歌单文件", "*.txt *.m3u *.m3u8 *.csv *.json *.lst"),
    ("文本文件", "*.txt"),
    ("播放列表", "*.m3u *.m3u8"),
    ("CSV/JSON", "*.csv *.json"),
    ("所有文件", "*.*"),
]


class App(tk.Tk):
    MATCH_FIRST = MATCH_FIRST
    MATCH_BEST = MATCH_BEST

    GEOM_RE = re.compile(r"^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$")
    PANE_MIN_SEARCH = 140     # 搜索结果区最小高度（像素）
    PANE_MIN_PICK = 110       # 抽选结果区最小高度

    def __init__(self):
        super().__init__()
        self.title("Bilibili 歌单自动播放器")
        # 按屏幕 DPI 缩放，高分屏下字体与控件才不会过小/发虚
        self._scale = ui_theme.apply_tk_scaling(self)

        self.songs: List[Song] = []
        self.results: List[Video] = []
        self.current_index = -1
        self.ui_queue: queue.Queue = queue.Queue()
        self.searcher = BiliSearcher()
        self.player = BiliPlayer(on_state=self._on_state,
                                 on_ended=self._on_ended,
                                 on_error=self._on_error,
                                 on_closed=self._on_closed)
        self._search_seq = 0
        self._paused = False      # 播放器回传的状态，用于显示
        self._ui_paused = False   # 按钮的即时状态，避免和每秒回传的状态打架
        self._recover_attempts = 0
        self._floating = None      # 桌面悬浮窗实例（单例，_toggle_floating 管理）

        # 抽选结果 + 播放队列（抽选/队列播放共用一套连播机制）
        # 队列里存的是「歌曲键」（"歌名 - 歌手"），不是下标 —— 这样从歌单里删歌也不会错位
        self._shuffle_picks: List[str] = []   # 本次抽中的歌曲键（"uid|歌名 - 歌手"）
        self._shuffle_running = False
        self._drawn: dict = {}                # 已抽过的歌曲：uid -> 歌曲键（会持久化）
        self._draw_stats: dict = {}           # 抽选历史：uid -> {"count": int, "last": float}
        self._uid_seq = 0                     # 歌曲唯一编号发放器
        self.queue: List[str] = []            # 当前连播队列（歌曲键）
        self.queue_pos: Optional[int] = None
        self.queue_label = ""                 # "抽选" / "队列"
        self._pick_videos: dict = {}          # 预搜索缓存：歌曲键 -> 视频列表
        self.results_owner: Optional[str] = None  # 当前「搜索结果」属于哪首歌
        self._overlay: Optional[object] = None  # 抽选时的全屏视频浮层

        # 定时关闭
        self._timer_left = 0          # 剩余秒数
        self._timer_job = None
        self._timer_action = "stop"   # 到点动作：stop / quit
        self._timer_wait_current = False  # 是否等当前这首播完再停
        self._timer_pending = False   # 时间已到，正在等这首播完
        self._timer_target_label = ""  # 定时到具体时刻时的目标标签（如 23:30）

        # 设置项（不常用的都收进「设置」对话框）
        self.browser_var = tk.StringVar()
        self.cookie_var = tk.StringVar()
        self.min_var = tk.StringVar(value="60")
        self.max_var = tk.StringVar(value="900")
        self.auto_var = tk.BooleanVar(value=True)
        self.match_var = tk.StringVar(value=MATCH_FIRST)
        self.pick_count_var = tk.StringVar(value=str(SHUFFLE_COUNT))  # 抽选个数
        self.no_repeat_var = tk.BooleanVar(value=True)   # 抽过的歌不再参与抽选
        self.remember_var = tk.BooleanVar(value=True)    # 记忆未抽歌单：启动时只载入没抽过的歌
        self.auto_remove_var = tk.BooleanVar(value=False)  # 播完自动从歌单移除（移除前自动备份）
        self.timer_min_var = tk.StringVar(value="60")      # 定时关闭默认分钟数
        self.timer_action_var = tk.StringVar(value="停止播放")
        self.timer_wait_var = tk.BooleanVar(value=True)    # 到点后播完当前这首再停
        # 抽选爆率（权重 + 保底）
        self.weight_mode_var = tk.BooleanVar(value=False)  # 按歌单标注的权重抽选
        self.pity_var = tk.BooleanVar(value=False)         # 后台保底：久未抽中的歌爆率递增
        self.pity_scale_var = tk.StringVar(value="1.0")   # 保底强度系数
        self.dedupe_var = tk.BooleanVar(value=False)  # 歌单重复歌曲：合并权重并去重
        self.video_var = tk.BooleanVar(value=True)    # 抽选时是否全屏播放视频动画
        self.video_path_var = tk.StringVar(value="")  # 自定义动画文件（留空则用程序目录的 video.mp4）
        self.video_path: Optional[str] = None         # 当前实际生效的动画文件
        self.port_var = tk.StringVar(value="9222")
        self.glass_var = tk.BooleanVar(value=True)
        self.alpha_var = tk.StringVar(value="0.96")
        self.tint_var = tk.StringVar(value="235")   # 毛玻璃底色调浓度（越大越白）
        self.solid_var = tk.BooleanVar(value=True)   # 默认浅色实底，背景必定是白的

        self._settings_win: Optional[SettingsDialog] = None
        self._glass_effect = ""
        self._last_playlist = ""
        self._saved_geom = ""
        self._saved_sash = 0      # 右侧上下两块的分割位置

        self.configure(bg=ui_theme.GLASS_KEY)
        ui_theme.setup_style(self, glass=True)
        self._build_ui()
        self._load_settings()
        self._place_window()
        self.after(200, self._restore_sash)   # 等布局稳定后再还原分割位置
        self.after(80, self._drain)
        self.after(120, self._init_glass)   # 等窗口真正创建出来再上毛玻璃
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 窗口尺寸 / 分辨率自适应 ----------------
    def _place_window(self) -> None:
        """按当前屏幕工作区决定窗口大小：能用上次的尺寸就用，否则自适应并居中。"""
        wa_x, wa_y, wa_w, wa_h = ui_theme.work_area()
        width = max(880, min(1180, wa_w - 80))
        height = max(560, min(760, wa_h - 80))
        self.minsize(min(880, max(640, wa_w - 40)),
                     min(560, max(420, wa_h - 60)))
        if self._apply_saved_geometry(self._saved_geom):
            return
        self.geometry(f"{width}x{height}+{wa_x + (wa_w - width) // 2}"
                      f"+{wa_y + (wa_h - height) // 2}")

    def _restore_sash(self) -> None:
        """恢复上次拖动出来的分区比例；没有记录时给一个默认比例（抽选区约 170px）。"""
        try:
            total = self.right_pane.winfo_height()
            if total < 200:
                return
            default = max(self.PANE_MIN_SEARCH, total - 170)
            target = self._saved_sash or default
            pos = max(self.PANE_MIN_SEARCH,
                      min(target, total - self.PANE_MIN_PICK))
            self.right_pane.sash_place(0, 0, pos)
        except Exception:
            pass

    def _save_sash(self) -> None:
        try:
            pos = self.right_pane.sash_coord(0)[1]   # (x, y)，垂直分割看 y
            if pos and pos > 0:
                self._saved_sash = int(pos)
        except Exception:
            pass

    def _apply_saved_geometry(self, geom: str) -> bool:
        """恢复上次窗口位置；分辨率/显示器变了导致跑到屏幕外时返回 False。"""
        match = self.GEOM_RE.match((geom or "").strip())
        if not match:
            return False
        width, height = int(match.group(1)), int(match.group(2))
        pos_x, pos_y = int(match.group(3)), int(match.group(4))
        if width < 400 or height < 300:
            return False
        vx, vy, vw, vh = ui_theme.virtual_screen()
        if (pos_x + width < vx + 40 or pos_x > vx + vw - 40 or
                pos_y + height < vy + 40 or pos_y > vy + vh - 40):
            return False
        _, _, wa_w, wa_h = ui_theme.work_area()
        width = min(width, max(640, wa_w - 40))
        height = min(height, max(420, wa_h - 40))
        self.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
        return True

    # ---------------- 外观 ----------------
    def _init_glass(self) -> None:
        """首次套用毛玻璃；不支持时自动退回实心界面。"""
        self.apply_settings()
        if self._glass_effect:
            self.set_status(f"{self.status_var.get()}   ·  窗口材质：{self._glass_effect}")

    def apply_settings(self) -> str:
        """把设置项落到界面/播放器，返回生效的窗口材质名（可能为空）。"""
        glass = bool(self.glass_var.get())
        try:
            alpha = float(self.alpha_var.get() or 0.96)
        except ValueError:
            alpha = 0.96

        self.configure(bg=ui_theme.GLASS_KEY if glass else ui_theme.BG_SOLID)
        ui_theme.setup_style(self, glass=glass)
        self._style_widgets()

        if glass and bool(self.solid_var.get()):
            # 浅色实底 + 整体半透明：不依赖系统材质，背景必定是浅色
            self.configure(bg=ui_theme.BG_SOLID)
            ui_theme.setup_style(self, glass=False)
            self._style_widgets()
            self._glass_effect = ui_theme.enable_translucent(self, alpha=alpha)
        elif glass:
            tint = self._int(self.tint_var.get(), 235)
            self._glass_effect = ui_theme.enable_glass(self, alpha=alpha,
                                                       tint_alpha=tint)
            if not self._glass_effect:      # 系统不支持，退回浅色实底
                self.configure(bg=ui_theme.BG_SOLID)
                ui_theme.setup_style(self, glass=False)
                self._style_widgets()
                self._glass_effect = ui_theme.enable_translucent(self, alpha=alpha)
        else:
            ui_theme.disable_glass(self)
            self._glass_effect = ""

        try:
            self.player.set_port(int(self.port_var.get() or 9222))
        except ValueError:
            pass
        self._sync_shuffle_button()
        # 「自动移除」与「记忆未抽歌单」语义重叠，二选一（记忆优先，避免边删边记）
        if self.remember_var.get() and self.auto_remove_var.get():
            self.auto_remove_var.set(False)
        self._timer_action = "quit" if "退出" in self.timer_action_var.get() else "stop"
        self._timer_wait_current = bool(self.timer_wait_var.get())
        self.video_path = self._current_video_path()
        self.searcher.set_cookie(self.cookie_var.get().strip())
        self._save_settings()
        return self._glass_effect

    def _style_widgets(self) -> None:
        """tk 原生控件（Listbox）不跟随 ttk 样式，单独刷一遍颜色。"""
        kwargs = ui_theme.listbox_kwargs()
        for box in (self.song_list, self.result_list, self.shuffle_list):
            box.configure(**kwargs)
            for i in range(box.size()):
                box.itemconfig(i, fg=ui_theme.FG)
        self._highlight_current()

    def _open_settings(self) -> None:
        if self._settings_win is not None and self._settings_win.winfo_exists():
            self._settings_win.lift()
            return
        win = SettingsDialog(self)
        self._settings_win = win
        win.protocol("WM_DELETE_WINDOW", lambda: (setattr(self, "_settings_win", None),
                                                  win.destroy()))

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # 顶栏：标题 + 歌单信息 + 设置（兼作可拖拽的标题栏）
        head = ttk.Frame(self, padding=(12, 10), style="Head.TFrame")
        head.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        head.columnconfigure(1, weight=1)

        ttk.Label(head, text="Bilibili 歌单自动播放器", style="Head.TLabel").grid(
            row=0, column=0, sticky="w")
        self.count_var = tk.StringVar(value="歌单：0 首")
        ttk.Label(head, textvariable=self.count_var, style="Head.Sub.TLabel").grid(
            row=0, column=1, sticky="w", padx=(14, 0))
        ttk.Button(head, text="⚙ 设置", command=self._open_settings).grid(
            row=0, column=2, sticky="e")
        ttk.Button(head, text="🪟 浮窗", command=self._toggle_floating).grid(
            row=0, column=3, sticky="e", padx=(6, 0))
        self._make_draggable(head)
        self.bind("<Unmap>", self._on_unmap)   # 最小化时自动召出悬浮窗

        # 主体
        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)
        body.columnconfigure(0, weight=2)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(body, text=" 歌单 ", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)
        self.search_var = tk.StringVar()
        self._view_indices = []   # 列表可见项对应的原始 songs 下标（搜索过滤时错位）

        bar = ttk.Frame(left)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for text, cmd in (("导入歌单", self._import_playlist),
                          ("添加", self._add_song),
                          ("删除", self._remove_song),
                          ("清空", self._clear_playlist),
                          ("保存", self._save_playlist)):
            ttk.Button(bar, text=text, command=cmd, style="Tool.TButton").pack(
                side="left", padx=(0, 6))
        self.btn_shuffle = ttk.Button(bar, text=f"🎲 抽选 {SHUFFLE_COUNT} 首",
                                      command=self._start_shuffle,
                                      style="Tool.TButton")
        self.btn_shuffle.pack(side="left")

        # 歌单内本地搜索：输入即按歌名/歌手过滤（不联网）
        sf = ttk.Frame(left)
        sf.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        sf.columnconfigure(0, weight=1)
        self.search_ent = ttk.Entry(sf, textvariable=self.search_var)
        self.search_ent.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.search_var.trace_add("write", lambda *a: self._filter_songs())
        ttk.Button(sf, text="✕", width=3, style="Tool.TButton",
                   command=lambda: (self.search_var.set(""),
                                    self.search_ent.focus_set())
                   ).pack(side="left")

        self.song_list = tk.Listbox(left, **ui_theme.listbox_kwargs())
        self.song_list.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, command=self.song_list.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.song_list.configure(yscrollcommand=scroll.set)
        self.song_list.bind("<Double-Button-1>", lambda e: self._play_selected_song())

        # 右侧：搜索结果 与 抽选结果 分成两块，中间可拖动分隔条调整各自高度
        # 用原生 PanedWindow：它支持 minsize 与把手，ttk 版本不支持
        right = tk.PanedWindow(body, orient="vertical", sashwidth=8,
                               sashrelief="flat", showhandle=True,
                               bg=ui_theme.PANEL_HI)
        right.grid(row=0, column=1, sticky="nsew")
        self.right_pane = right

        search = ttk.LabelFrame(right, text=" 搜索结果（双击换视频） ", padding=6)
        search.rowconfigure(0, weight=1)
        search.columnconfigure(0, weight=1)
        right.add(search, minsize=self.PANE_MIN_SEARCH, stretch="always")

        self.result_list = tk.Listbox(search, **ui_theme.listbox_kwargs())
        self.result_list.grid(row=0, column=0, sticky="nsew")
        rscroll = ttk.Scrollbar(search, command=self.result_list.yview)
        rscroll.grid(row=0, column=1, sticky="ns")
        self.result_list.configure(yscrollcommand=rscroll.set)
        self.result_list.bind("<Double-Button-1>", self._play_selected_result)

        rbar = ttk.Frame(search)
        rbar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(rbar, text="重新搜索", style="Tool.TButton",
                   command=lambda: self._search_current(True)).pack(side="left")
        ttk.Button(rbar, text="在浏览器打开", style="Tool.TButton",
                   command=self._open_in_web).pack(side="left", padx=6)

        pick = ttk.LabelFrame(right, text=" 抽选结果（双击播放） ", padding=6)
        pick.rowconfigure(0, weight=1)
        pick.columnconfigure(0, weight=1)
        right.add(pick, minsize=self.PANE_MIN_PICK, stretch="never")

        self.shuffle_list = tk.Listbox(pick, **ui_theme.listbox_kwargs())
        self.shuffle_list.grid(row=0, column=0, sticky="nsew")
        pscroll = ttk.Scrollbar(pick, command=self.shuffle_list.yview)
        pscroll.grid(row=0, column=1, sticky="ns")
        self.shuffle_list.configure(yscrollcommand=pscroll.set)
        self.shuffle_list.bind("<Double-Button-1>", self._play_shuffle_result)
        self.shuffle_list.bind("<Return>", self._play_shuffle_result)

        pbar = ttk.Frame(pick)
        pbar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(pbar, text="▶ 播放抽选结果", style="Tool.TButton",
                   command=self._play_shuffle_queue).pack(side="left")
        ttk.Button(pbar, text="▶ 队列播放", style="Tool.TButton",
                   command=self._start_queue_play).pack(side="left", padx=6)
        ttk.Button(pbar, text="清空结果", style="Tool.TButton",
                   command=self._clear_shuffle_result).pack(side="left")
        ttk.Button(pbar, text="重置抽选记录", style="Tool.TButton",
                   command=self._reset_drawn).pack(side="left", padx=6)
        self.queue_var = tk.StringVar(value="队列：未开始")
        ttk.Label(pbar, textvariable=self.queue_var, style="Sub.TLabel").pack(
            side="left", padx=(12, 0))

        # 控制条
        ctrl = ttk.LabelFrame(self, text=" 播放控制 ", padding=8)
        ctrl.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 10))
        ctrl.columnconfigure(6, weight=1)

        self.btn_prev = ttk.Button(ctrl, text="⏮ 上一首", command=self._prev)
        self.btn_play = ttk.Button(ctrl, text="▶ 播放", style="Accent.TButton",
                                   command=self._play_selected_song)
        self.btn_queue = ttk.Button(ctrl, text="▶ 队列播放",
                                    command=self._start_queue_play)
        self.btn_pause = ttk.Button(ctrl, text="⏸ 暂停", command=self._toggle_pause)
        self.btn_next = ttk.Button(ctrl, text="⏭ 下一首", command=self._next)
        self.btn_stop = ttk.Button(ctrl, text="■ 停止", command=self._stop)
        self.btn_front = ttk.Button(ctrl, text="⤢ 拉回前台", command=self._bring_front)
        self.btn_timer = ttk.Button(ctrl, text="⏱ 定时", command=self._open_timer_dialog)
        for i, btn in enumerate((self.btn_prev, self.btn_play, self.btn_queue,
                                 self.btn_pause, self.btn_next, self.btn_stop,
                                 self.btn_front, self.btn_timer)):
            btn.grid(row=0, column=i, padx=(0, 6))

        self.timer_var = tk.StringVar(value="")
        ttk.Label(ctrl, textvariable=self.timer_var, style="Sub.TLabel").grid(
            row=0, column=9, sticky="w", padx=(6, 0))

        self.now_var = tk.StringVar(value="未在播放")
        ttk.Label(ctrl, textvariable=self.now_var,
                  font=(ui_theme.FONT, 11, "bold")).grid(
            row=0, column=8, sticky="w", padx=(12, 0))

        self.time_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(ctrl, textvariable=self.time_var).grid(row=0, column=8,
                                                         padx=(0, 10))

        self.progress = ttk.Progressbar(ctrl, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, columnspan=8, sticky="ew", pady=(10, 0))

        self.status_var = tk.StringVar(value="就绪：先导入或添加歌曲，然后点「播放」")
        ttk.Label(ctrl, textvariable=self.status_var, style="Sub.TLabel").grid(
            row=1, column=8, sticky="e", pady=(10, 0))

    # ---------------- 定时关闭 ----------------
    def _timer_status_text(self) -> str:
        """当前定时状态的可读文本（对话框与主界面共用）。"""
        if self._timer_pending:
            return "已设置：播完当前这首后停止"
        if self._timer_left > 0:
            h, rem = divmod(self._timer_left, 3600)
            m, s = divmod(rem, 60)
            left = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
            if self._timer_target_label:
                return f"已设置：{self._timer_target_label}（剩 {left}）"
            return f"已设置：剩 {left}"
        return "未设置"

    def _open_timer_dialog(self) -> None:
        """弹出定时关闭设置对话框（图形界面，代替旧的纯菜单入口）。"""
        dlg = tk.Toplevel(self)
        dlg.title("定时关闭")
        dlg.configure(bg=ui_theme.PANEL)
        dlg.resizable(False, False)
        dlg.transient(self)
        try:
            dlg.grab_set()
        except Exception:
            pass
        P, FG, SUB, AC = (ui_theme.PANEL, ui_theme.FG, ui_theme.SUBFG, ui_theme.ACCENT)
        font = (ui_theme.FONT, 10)

        status_var = tk.StringVar(value=self._timer_status_text())
        def refresh_status() -> None:
            status_var.set(self._timer_status_text())

        # 当前状态
        top = tk.Frame(dlg, bg=P)
        top.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(top, text="当前：", bg=P, fg=FG, font=font).pack(side="left")
        tk.Label(top, textvariable=status_var, bg=P, fg=AC,
                 font=(ui_theme.FONT, 10, "bold")).pack(side="left")

        # 模式切换
        mode_var = tk.StringVar(value="countdown")
        mode_f = tk.Frame(dlg, bg=P)
        mode_f.pack(fill="x", padx=12, pady=4)
        tk.Radiobutton(mode_f, text="倒计时（分钟）", variable=mode_var, value="countdown",
                       bg=P, fg=FG, selectcolor=P, activebackground=P, font=font,
                       command=lambda: show_mode()).pack(side="left", padx=(0, 14))
        tk.Radiobutton(mode_f, text="指定时间（HH:MM）", variable=mode_var, value="at",
                       bg=P, fg=FG, selectcolor=P, activebackground=P, font=font,
                       command=lambda: show_mode()).pack(side="left")

        # 倒计时输入
        f_count = tk.Frame(dlg, bg=P)
        tk.Label(f_count, text="分钟：", bg=P, fg=FG, font=font).pack(side="left")
        min_var = tk.StringVar(value=self.timer_min_var.get())
        tk.Spinbox(f_count, from_=1, to=600, textvariable=min_var, width=6,
                   bg=ui_theme.PANEL_SOFT, fg=FG, buttonbackground=ui_theme.PANEL_SOFT,
                   relief="flat", font=font).pack(side="left", padx=(0, 8))
        for m in (15, 30, 60, 90, 120):
            ttk.Button(f_count, text=f"{m}", width=4, style="Tool.TButton",
                       command=lambda v=m: min_var.set(str(v))).pack(side="left", padx=2)

        # 指定时间输入
        f_at = tk.Frame(dlg, bg=P)
        tk.Label(f_at, text="时间：", bg=P, fg=FG, font=font).pack(side="left")
        hh_var = tk.StringVar(value="23")
        mm_var = tk.StringVar(value="30")
        tk.Spinbox(f_at, from_=0, to=23, textvariable=hh_var, width=4,
                   bg=ui_theme.PANEL_SOFT, fg=FG, buttonbackground=ui_theme.PANEL_SOFT,
                   relief="flat", font=font).pack(side="left")
        tk.Label(f_at, text=":", bg=P, fg=FG, font=(ui_theme.FONT, 11)).pack(side="left")
        tk.Spinbox(f_at, from_=0, to=59, textvariable=mm_var, width=4,
                   bg=ui_theme.PANEL_SOFT, fg=FG, buttonbackground=ui_theme.PANEL_SOFT,
                   relief="flat", font=font).pack(side="left", padx=(0, 6))
        tk.Label(f_at, text="24 小时制，已过则顺延到明天", bg=P, fg=SUB,
                 font=(ui_theme.FONT, 9)).pack(side="left")

        def show_mode() -> None:
            if mode_var.get() == "countdown":
                f_count.pack(fill="x", padx=12, pady=4)
                f_at.pack_forget()
            else:
                f_at.pack(fill="x", padx=12, pady=4)
                f_count.pack_forget()
        show_mode()

        # 选项区
        opt = tk.LabelFrame(dlg, text="选项", bg=P, fg=AC, font=(ui_theme.FONT, 9, "bold"))
        opt.pack(fill="x", padx=12, pady=(8, 4))
        wait_var = tk.BooleanVar(value=self._timer_wait_current)
        tk.Checkbutton(opt, text="到点后播完当前这首再停止", variable=wait_var,
                       bg=P, fg=FG, selectcolor=P, activebackground=P,
                       font=font).pack(anchor="w", padx=8, pady=4)
        action_var = tk.StringVar(value=self._timer_action)
        ar = tk.Frame(opt, bg=P)
        ar.pack(anchor="w", padx=8, pady=(0, 6))
        tk.Radiobutton(ar, text="停止播放", variable=action_var, value="stop",
                       bg=P, fg=FG, selectcolor=P, activebackground=P, font=font
                       ).pack(side="left", padx=(0, 16))
        tk.Radiobutton(ar, text="退出程序", variable=action_var, value="quit",
                       bg=P, fg=FG, selectcolor=P, activebackground=P, font=font
                       ).pack(side="left")

        # 底部按钮
        btns = tk.Frame(dlg, bg=P)
        btns.pack(fill="x", padx=12, pady=(4, 12))
        ttk.Button(btns, text="开始定时", style="Accent.TButton",
                   command=lambda: do_start()).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="仅播完当前首即停", style="Tool.TButton",
                   command=lambda: do_stop_current()).pack(side="left", padx=(0, 8))
        if self._timer_left > 0 or self._timer_pending:
            ttk.Button(btns, text="取消定时", style="Tool.TButton",
                       command=lambda: do_cancel()).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="关闭", style="Tool.TButton",
                   command=dlg.destroy).pack(side="right")
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        def do_start() -> None:
            self._timer_wait_current = bool(wait_var.get())
            self._timer_action = action_var.get()
            if mode_var.get() == "countdown":
                try:
                    minutes = max(1, int(float(min_var.get())))
                except (ValueError, TypeError):
                    minutes = 60
                self.timer_min_var.set(str(minutes))
                self._start_timer(minutes)
            else:
                try:
                    hh = int(hh_var.get())
                    mm = int(mm_var.get())
                    if not (0 <= hh < 24 and 0 <= mm < 60):
                        raise ValueError
                except (ValueError, TypeError):
                    messagebox.showinfo("定时关闭",
                                        "时间应为 0-23 小时、0-59 分钟",
                                        parent=dlg)
                    return
                now = datetime.datetime.now()
                target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if target <= now:                       # 已过则顺延到明天
                    target += datetime.timedelta(days=1)
                seconds = (target - now).total_seconds()
                label = target.strftime("%H:%M")
                if target.date() > now.date():
                    label += "（明天）"
                self._start_timer_at(seconds, label)
            dlg.destroy()

        def do_stop_current() -> None:
            self._stop_after_current()
            dlg.destroy()

        def do_cancel() -> None:
            self._cancel_timer()
            refresh_status()
            dlg.destroy()

    def _start_timer_at(self, seconds: float, label: str) -> None:
        """按「到某时刻的秒数」开始倒计时。"""
        self._cancel_timer()
        self._timer_left = max(1, int(seconds))
        self._timer_target_label = label
        self._timer_pending = False
        self._timer_job = self.after(1000, self._timer_tick)
        self._render_timer()
        action = "停止播放" if self._timer_action == "stop" else "退出程序"
        self.set_status(f"定时关闭：{label} 到点{action}")

    def _start_timer(self, minutes: int) -> None:
        self._cancel_timer()
        self._timer_left = max(1, int(minutes * 60))
        self._timer_target_label = ""
        self._timer_pending = False
        self._timer_job = self.after(1000, self._timer_tick)
        self._render_timer()
        self.set_status(f"定时关闭：{minutes} 分钟后"
                        + ("停止播放" if self._timer_action == "stop" else "退出程序"))

    def _stop_after_current(self) -> None:
        """不等倒计时，播完当前这首就停。"""
        self._cancel_timer()
        self._timer_pending = True
        self._render_timer()
        self.set_status("定时关闭：播完当前这首后停止"
                        if self.player.is_active else "定时关闭：当前没有在播放")

    def _cancel_timer(self) -> None:
        if self._timer_job:
            try:
                self.after_cancel(self._timer_job)
            except Exception:
                pass
        self._timer_job = None
        self._timer_left = 0
        self._timer_pending = False
        self._render_timer()

    def _timer_tick(self) -> None:
        if self._timer_left <= 0:
            self._timer_job = None
            self._fire_timer()
            return
        self._timer_left -= 1
        self._render_timer()
        self._timer_job = self.after(1000, self._timer_tick)

    def _fire_timer(self) -> None:
        if self._timer_wait_current and self.player.is_active:
            self._timer_pending = True      # 等 _handle_ended 里收尾
            self._render_timer()
            self.set_status("定时到点：播完当前这首后停止")
            return
        self._do_timer_action()

    def _do_timer_action(self) -> None:
        self.player.stop()
        self._clear_queue()
        self._refresh_shuffle_list()
        self.now_var.set("定时关闭：已停止")
        self.set_status("定时关闭：已停止播放")
        was_quit = self._timer_action == "quit"
        self._cancel_timer()
        if was_quit:
            self.after(400, self._on_close)

    def _render_timer(self) -> None:
        if self._timer_pending:
            self.timer_var.set("⏳ 播完即停")
            self.btn_timer.configure(text="⏱ 播完即停")
            return
        if self._timer_left <= 0:
            self.timer_var.set("")
            self.btn_timer.configure(text="⏱ 定时")
            return
        m, s = divmod(self._timer_left, 60)
        h, m = divmod(m, 60)
        txt = f"⏳ {h}:{m:02d}:{s:02d}" if h else f"⏳ {m:02d}:{s:02d}"
        self.timer_var.set(txt)
        self.btn_timer.configure(
            text=f"⏱ {self._timer_target_label}" if self._timer_target_label
            else f"⏱ {txt[2:]}")

    # ---------------- 窗口拖动 ----------------
    def _make_draggable(self, widget) -> None:
        """让顶栏（含标题文字）可以拖动窗口。

        开了 -transparentcolor 之后，透明区域不接收鼠标事件，
        所以顶栏必须是实底的，并显式绑定拖动。
        """
        if isinstance(widget, ttk.Button):     # 按钮保留自己的点击行为
            return
        widget.bind("<ButtonPress-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)
        for child in widget.winfo_children():
            self._make_draggable(child)

    def _drag_start(self, event) -> None:
        self._drag_origin = (event.x_root, event.y_root)

    def _drag_move(self, event) -> None:
        if not getattr(self, "_drag_origin", None):
            return
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        if dx == 0 and dy == 0:
            return
        self._drag_origin = (event.x_root, event.y_root)
        self.geometry(f"+{self.winfo_x() + dx}+{self.winfo_y() + dy}")

    # ---------------- 设置存取 ----------------
    def _load_settings(self) -> None:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        self.browser_var.set(data.get("browser") or find_browser() or "")
        self.cookie_var.set(data.get("cookie", ""))
        self.min_var.set(str(data.get("min_dur", 60)))
        self.max_var.set(str(data.get("max_dur", 900)))
        self.auto_var.set(bool(data.get("auto_next", True)))
        saved_match = data.get("match_mode", MATCH_FIRST)
        self.match_var.set(saved_match if saved_match in (MATCH_FIRST, MATCH_BEST)
                           else MATCH_FIRST)
        self.pick_count_var.set(str(data.get("shuffle_count", SHUFFLE_COUNT)))
        self.video_var.set(bool(data.get("shuffle_video", True)))
        self.no_repeat_var.set(bool(data.get("no_repeat", True)))
        self.remember_var.set(bool(data.get("remember_undrawn", True)))
        self.timer_min_var.set(str(data.get("timer_minutes", 60)))
        self.timer_action_var.set(data.get("timer_action", "停止播放"))
        self.timer_wait_var.set(bool(data.get("timer_wait_current", True)))
        self.auto_remove_var.set(bool(data.get("auto_remove", False)))
        if self.remember_var.get() and self.auto_remove_var.get():
            self.auto_remove_var.set(False)   # 两者互斥，记忆优先
        self.weight_mode_var.set(bool(data.get("weight_mode", False)))
        self.pity_var.set(bool(data.get("pity", False)))
        self.pity_scale_var.set(str(data.get("pity_scale", "1.0")))
        self.dedupe_var.set(bool(data.get("dedupe", False)))
        self.video_path_var.set(data.get("video_path", ""))
        self.port_var.set(str(data.get("port", 9222)))
        self.glass_var.set(bool(data.get("glass", True)))
        self.alpha_var.set(str(data.get("alpha", 0.96)))
        self.tint_var.set(str(data.get("tint_alpha", 235)))
        self.solid_var.set(bool(data.get("solid_bg", True)))
        self._saved_geom = str(data.get("win_geom", ""))
        self._saved_sash = self._int(data.get("sash_pos", 0), 0)
        if self.cookie_var.get():
            self.searcher.set_cookie(self.cookie_var.get())
        # 恢复上次记录的「已抽过」：uid 与歌名都对得上才算，歌单改过就自动失效
        for key in data.get("drawn", []) or []:
            uid, name = self._key_uid(key), self._key_name(key)
            if uid > 0 and (uid, name) not in self._drawn.items():
                self._drawn[uid] = key
        # 还原抽选历史（保底用）：uid 漂移不影响，仅作权重微调
        for k, st in (data.get("draw_stats") or {}).items():
            try:
                self._draw_stats[int(k)] = st
            except (ValueError, TypeError):
                pass

        last = data.get("last_playlist")
        if last and os.path.exists(last):
            try:
                songs = load_playlist(last)
                if self.dedupe_var.get():
                    songs = dedupe_songs(songs)
                for i, song in enumerate(songs, 1):   # 与 _assign_uids 一致：1..N
                    song.uid = i
                if self.remember_var.get() and self._drawn:
                    kept = [s for s in songs if s.uid not in self._drawn]
                    removed = len(songs) - len(kept)
                    songs = kept
                else:
                    removed = 0
                self._set_songs(songs, keep_drawn=True)
                self.status_var.set(
                    f"已载入上次歌单：{os.path.basename(last)}"
                    + (f"（跳过已抽过的 {removed} 首）" if removed else ""))
                if not songs and removed:
                    self.status_var.set("上次歌单里的歌都抽过了 —— 点「重置抽选记录」重新开始")
            except Exception:
                pass

    def _save_settings(self) -> None:
        data = {
            "browser": self.browser_var.get().strip(),
            "cookie": self.cookie_var.get().strip(),
            "min_dur": self._int(self.min_var.get(), 60),
            "max_dur": self._int(self.max_var.get(), 900),
            "auto_next": self.auto_var.get(),
            "match_mode": self.match_var.get(),
            "shuffle_count": self._int(self.pick_count_var.get(), SHUFFLE_COUNT),
            "shuffle_video": self.video_var.get(),
            "no_repeat": self.no_repeat_var.get(),
            "remember_undrawn": self.remember_var.get(),
            "timer_minutes": self._int(self.timer_min_var.get(), 60),
            "timer_action": self.timer_action_var.get(),
            "timer_wait_current": self.timer_wait_var.get(),
            "drawn": list(self._drawn.values()),
            "weight_mode": self.weight_mode_var.get(),
            "pity": self.pity_var.get(),
            "pity_scale": self._pity_scale(),
            "dedupe": self.dedupe_var.get(),
            "draw_stats": {str(uid): st for uid, st in self._draw_stats.items()},
            "auto_remove": self.auto_remove_var.get(),
            "video_path": self.video_path_var.get().strip(),
            "port": self._int(self.port_var.get(), 9222),
            "glass": self.glass_var.get(),
            "alpha": float(self.alpha_var.get() or 0.96),
            "tint_alpha": self._int(self.tint_var.get(), 235),
            "solid_bg": self.solid_var.get(),
            "win_geom": self._saved_geom,
            "sash_pos": self._saved_sash,
            "last_playlist": self._last_playlist,
        }
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def _int(value: str, default: int) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    # ---------------- 线程 → UI ----------------
    def _post(self, func) -> None:
        self.ui_queue.put(func)

    def _drain(self) -> None:
        try:
            for _ in range(50):
                func = self.ui_queue.get_nowait()
                try:
                    func()
                except Exception as exc:
                    self.status_var.set(f"界面更新出错：{exc}")
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    # ---------------- 歌单操作 ----------------
    def _assign_uids(self) -> None:
        """换歌单时按列表顺序统一编号 1..N。

        用固定序号而不是自增计数，这样「已抽记录」跨次启动还能对得上；
        同名歌曲也各有一个号，不会互相连坐。
        """
        self._uid_seq = 0
        for song in self.songs:
            self._uid_seq += 1
            song.uid = self._uid_seq

    def _append_song(self, song: Song) -> None:
        """追加一首歌：只给新歌发号，已有歌曲的编号保持不变。"""
        self._uid_seq += 1
        song.uid = self._uid_seq
        self.songs.append(song)

    def _set_songs(self, songs: List[Song], keep_drawn: bool = False) -> None:
        """换整个歌单：旧的抽选结果作废。

        keep_drawn=True 用于启动时恢复「记忆歌单」，此时已抽记录要保留。
        """
        self.songs = songs
        self._assign_uids()
        self.song_list.selection_clear(0, "end")
        if not keep_drawn:
            self._drawn.clear()      # 新歌单重新开始
            self._draw_stats.clear()
            self._save_settings()
        self._clear_shuffle_result()
        self._clear_queue()
        self._render_song_list()

    def _render_song_list(self) -> None:
        """重建歌单列表（支持本地搜索过滤，保留抽选结果与队列）。"""
        self.song_list.delete(0, "end")
        self._view_indices = []
        kw = (self.search_var.get().strip().lower()
              if getattr(self, "search_var", None) else "")
        shown = 0
        for i, s in enumerate(self.songs):
            if kw and kw not in f"{s.name} {s.artist}".strip().lower():
                continue
            self._view_indices.append(i)
            shown += 1
            self.song_list.insert("end", f"{shown:>3}. {s.display}")
        left = len([s for s in self.songs if s.uid not in self._drawn])
        self.count_var.set(f"歌单：{len(self.songs)} 首"
                           + (f"（未抽过 {left} 首）" if self._drawn else "")
                           + (f"　匹配 {shown} 首" if kw else ""))
        self._paint_picks()
        self._highlight_current()

    def _filter_songs(self) -> None:
        """搜索框内容变化时重新渲染歌单列表（按需过滤）。"""
        self._render_song_list()

    def _refresh_song_row(self, index: int) -> None:
        if not (0 <= index < len(self.songs)):
            return
        try:
            pos = self._view_indices.index(index)
        except ValueError:
            return   # 该行被搜索过滤掉了，无需刷新
        s = self.songs[index]
        self.song_list.delete(pos)
        self.song_list.insert(pos, f"{pos + 1:>3}. {s.display}")

    def _import_playlist(self) -> None:
        path = filedialog.askopenfilename(title="选择歌单文件", filetypes=FILE_TYPES)
        if not path:
            return
        try:
            songs = load_playlist(path)
            if self.dedupe_var.get():
                songs = dedupe_songs(songs)
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        if not songs:
            messagebox.showwarning("空歌单", "没有从该文件里解析到歌曲")
            return
        self.search_var.set("")      # 导入后显示完整歌单
        self._set_songs(songs)
        self._last_playlist = path
        self.status_var.set(f"已导入 {len(songs)} 首：{os.path.basename(path)}")
        self._save_settings()

    def _add_song(self) -> None:
        text = simpledialog.askstring("添加歌曲", "格式：歌名 - 歌手（歌手可省略）")
        if not text:
            return
        for line in text.splitlines():
            song = parse_line(line)
            if song and song.name:
                self._append_song(song)
        self.search_var.set("")      # 添加后显示完整歌单，便于看到刚加的歌
        self._render_song_list()

    def _remove_song(self) -> None:
        sel = self.song_list.curselection()
        if not sel:
            return
        index = self._view_indices[sel[0]]
        del self.songs[index]
        if self.current_index > index:
            self.current_index -= 1
        elif self.current_index == index:
            self.current_index = -1
        self._set_songs(self.songs)
        self._highlight_current()

    def _clear_playlist(self) -> None:
        if self.songs and not messagebox.askyesno("确认", "清空整个歌单？"):
            return
        self._set_songs([])
        self.current_index = -1
        self._clear_shuffle_result()
        self._clear_queue()

    # ---------------- 抽选模式 ----------------
    @staticmethod
    def _key_of(song: Song) -> str:
        """歌曲键 = 唯一编号 + 显示名，同名歌曲不会互相影响。"""
        return f"{song.uid}|{song.display}"

    @staticmethod
    def _key_uid(key: Optional[str]) -> int:
        try:
            return int(str(key).split("|", 1)[0])
        except (TypeError, ValueError):
            return -1

    @staticmethod
    def _key_name(key: Optional[str]) -> str:
        text = str(key or "")
        return text.split("|", 1)[1] if "|" in text else text

    def _index_of(self, key: Optional[str]) -> int:
        """按歌曲键找它在歌单里的下标，找不到返回 -1。"""
        uid = self._key_uid(key)
        if uid < 0:
            return -1
        for i, s in enumerate(self.songs):
            if s.uid == uid:
                return i
        return -1

    def _current_video_path(self) -> Optional[str]:
        """当前生效的抽选动画文件：自定义路径优先，否则用程序目录里的 video.mp4。"""
        custom = self.video_path_var.get().strip()
        if custom and os.path.exists(custom):
            return custom
        # 先看随程序打包的（exe 内部），再看 exe 旁边用户自己放的
        return video_overlay.find_video(BUNDLE_DIR, APP_DIR)

    def _shuffle_count(self) -> int:
        """设置里的抽选个数（至少 1，最多不超过歌单长度）。"""
        want = max(1, self._int(self.pick_count_var.get(), SHUFFLE_COUNT))
        return min(want, len(self.songs)) if self.songs else want

    def _close_overlay(self) -> None:
        if getattr(self, "_overlay", None) is not None:
            try:
                self._overlay.close()
            except Exception:
                pass
            self._overlay = None

    def _sync_shuffle_button(self) -> None:
        """按钮文字跟随抽选个数；动画进行中不要覆盖「抽选中…」。"""
        if self._shuffle_running:
            return
        if getattr(self, "btn_shuffle", None) and self.btn_shuffle.winfo_exists():
            want = max(1, self._int(self.pick_count_var.get(), SHUFFLE_COUNT))
            self.btn_shuffle.configure(text=f"🎲 抽选 {want} 首")

    def _start_shuffle(self) -> None:
        """从歌单随机抽 N 首（已抽过的不再参与），先跑抽取动画。"""
        if self._shuffle_running:
            return
        want = max(1, self._int(self.pick_count_var.get(), SHUFFLE_COUNT))

        # 抽选池：开了「不重复」就只从没抽过的歌里抽
        if self.no_repeat_var.get():
            pool = [i for i, s in enumerate(self.songs) if s.uid not in self._drawn]
        else:
            pool = list(range(len(self.songs)))
        if len(pool) < want:
            hint = ("歌单里没抽过的歌只剩 "
                    f"{len(pool)} 首（共 {len(self.songs)} 首）\n"
                    "点「重置抽选记录」可重新开始") if self._drawn else \
                   f"歌单至少需要 {want} 首歌才能抽选（当前 {len(self.songs)} 首）"
            messagebox.showinfo("抽选模式", hint, parent=self)
            return

        self._shuffle_running = True
        self.btn_shuffle.configure(text="🎲 抽选中…", state="disabled")
        self.set_status(f"正在抽选 {want} 首…")
        picks = self._pick_indices(pool, want)            # 下标，动画要用（支持爆率权重/保底）
        keys = [self._key_of(self.songs[i]) for i in picks]  # 歌曲键，后续都用它
        self._shuffle_picks = keys
        now = time.time()
        for i in picks:                                    # 记下来，下次启动不再抽它们
            uid = self.songs[i].uid
            self._drawn[uid] = self._key_of(self.songs[i])
            st = self._draw_stats.setdefault(uid, {"count": 0, "last": 0.0})
            st["count"] += 1
            st["last"] = now
        self._save_settings()
        self._render_song_list()
        self._refresh_shuffle_list()

        # 开了开关且找得到动画文件，就全屏独占播放它；否则用列表闪动
        path = self._current_video_path()
        if path and video_overlay.available() and self.video_var.get():
            try:
                # 播完一遍才收尾；过长的视频由 MAX 兜底，点击/Esc 可随时跳过
                self._overlay = video_overlay.VideoOverlay(
                    self, path,
                    on_close=self._skip_shuffle_video,
                    on_finish=lambda: self._shuffle_finish_if_running(list(keys)),
                    loop=False, min_ms=SHUFFLE_VIDEO_MIN_MS)
            except Exception:
                self._overlay = None
        if self._overlay is not None:
            self.after(SHUFFLE_VIDEO_MAX_MS,
                       lambda: self._shuffle_finish_if_running(list(keys)))
        else:
            self._shuffle_anim(0, picks, list(keys))

    def _pity_scale(self) -> float:
        """后台保底强度系数（解析设置项，非数字或负数按 1.0 处理）。"""
        try:
            v = float(self.pity_scale_var.get())
        except (ValueError, TypeError):
            v = 1.0
        return max(0.0, v)

    def _pick_indices(self, pool: List[int], want: int) -> List[int]:
        """从候选下标里抽 want 个：默认均匀；开启权重/保底则用加权无放回抽样。"""
        if not (self.weight_mode_var.get() or self.pity_var.get()):
            return random.sample(pool, want)
        now = time.time()
        weights = []
        for i in pool:
            s = self.songs[i]
            base = s.weight if (s.weight and s.weight > 0) else 1.0
            if self.pity_var.get():
                st = self._draw_stats.get(s.uid)
                last = st["last"] if st else 0.0
                elapsed = (now - last) / 3600.0
                base *= 1.0 + self._pity_scale() * min(elapsed, PITY_CAP_HOURS)
            weights.append(base)
        # 顺序加权无放回：每次按权重选一个并从池里移除，重复 want 次
        idxs = list(range(len(pool)))
        wts = list(weights)
        picks = []
        for _ in range(want):
            total = sum(wts)
            if total <= 0:
                c = random.randrange(len(idxs))
            else:
                r = random.random() * total
                acc = 0.0
                c = 0
                for k, w in enumerate(wts):
                    acc += w
                    if acc >= r:
                        c = k
                        break
            real = idxs.pop(c)
            wts.pop(c)
            picks.append(real)
        return picks

    def _shuffle_finish_if_running(self, keys: List[str]) -> None:
        """全屏动画到点结束；用户提前关闭时不再重复收尾。"""
        if self._shuffle_running:
            self._shuffle_finish(keys)

    def _skip_shuffle_video(self) -> None:
        """用户点了全屏动画（或按 Esc）→ 立即出结果。"""
        self._overlay = None
        if self._shuffle_running and self._shuffle_picks:
            self._shuffle_finish(list(self._shuffle_picks))

    def _shuffle_anim(self, step: int, picks: List[int], keys: List[str]) -> None:
        """抽取动画：列表快速随机闪动并逐渐减速，最后定格在抽中的几首。"""
        if not self._shuffle_running:
            return
        tail = SHUFFLE_STEPS - 4
        if step >= tail:                     # 最后几帧在结果间来回，做定格过渡
            idx = picks[step % len(picks)]
        else:
            idx = random.randrange(len(self.songs))

        self._paint_picks()
        try:
            pos = self._view_indices.index(idx)
        except ValueError:
            pos = None
        if pos is not None:
            self.song_list.itemconfig(pos, bg=ui_theme.ACCENT, fg="white")
            self.song_list.see(pos)
        self.now_var.set(f"🎲 {self.songs[idx].display}")

        step += 1
        if step < SHUFFLE_STEPS:
            delay = 40 + int(230 * (step / SHUFFLE_STEPS) ** 2)   # 越来越慢
            self.after(delay, lambda: self._shuffle_anim(step, picks, keys))
        else:
            self._shuffle_finish(keys)

    def _shuffle_finish(self, keys: List[str]) -> None:
        self._shuffle_running = False
        self._close_overlay()
        self.btn_shuffle.configure(state="normal")
        self._sync_shuffle_button()
        self._shuffle_picks = keys
        self._paint_picks()
        self._refresh_shuffle_list()
        if keys:                       # 预选第一项，方便直接回车播放
            self.shuffle_list.selection_set(0)
        # 只出结果，不自动播放：等用户在「抽选结果」里选一首
        names = "  →  ".join(self._key_name(k) for k in keys)
        self.set_status(f"抽中：{names} —— 双击右侧「抽选结果」里的"
                        f"某一首开始播放，或点「▶ 播放抽选结果」按顺序连播")
        self.now_var.set(f"🎲 已抽中 {len(keys)} 首，请选择要播放的歌")
        self._prefetch_shuffle(list(keys))   # 后台先搜好，选了就能立刻播

    def _prefetch_shuffle(self, keys: List[str]) -> None:
        """抽选后先后台搜索这几首（只展示结果，不播放），选中的时候就不用再等。"""
        self._pick_videos = {}

        def work():
            for pos, key in enumerate(keys):
                index = self._index_of(key)
                if index < 0:
                    continue
                try:
                    videos = self.searcher.search(self.songs[index].keyword, page=1)
                except Exception:
                    continue
                self._pick_videos[key] = videos
                if pos == 0:      # 第一首的结果先展示出来
                    mode = self.match_var.get()
                    # 默认参数绑定当前循环值，避免闭包延迟绑定串到别的歌
                    self._post(lambda k=key, vids=videos:
                               self._fill_results(k, vids, None, mode))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 抽选结果列表 ----------------
    def _refresh_shuffle_list(self) -> None:
        """刷新「抽选结果」列表：▶ 播放中 / ✓ 已播 / · 待播 / ✗ 已从歌单移除。"""
        if self.queue:
            self.queue_var.set(f"{self.queue_label}："
                               f"{(self.queue_pos or 0) + 1}/{len(self.queue)}")
        else:
            self.queue_var.set("队列：未开始")
        box = self.shuffle_list
        box.delete(0, "end")
        for pos, key in enumerate(self._shuffle_picks):
            name = self._key_name(key)
            index = self._index_of(key)
            if index < 0:
                box.insert("end", f"✗ {pos + 1}. {name}（已从歌单移除）")
                continue
            if index == self.current_index:
                mark = "▶"
            elif self.queue_label == "抽选" and pos < (self.queue_pos or 0):
                mark = "✓"
            else:
                mark = "·"
            box.insert("end", f"{mark} {pos + 1}. {name}")

    def _play_shuffle_result(self, event=None) -> None:
        """双击抽选结果里的某一首，直接从这首开始连播抽选结果。"""
        sel = self.shuffle_list.curselection()
        if not sel or not (0 <= sel[0] < len(self._shuffle_picks)):
            return
        pos = sel[0]
        key = self._shuffle_picks[pos]
        index = self._index_of(key)
        if index < 0:
            messagebox.showinfo("抽选结果",
                                f"「{self._key_name(key)}」已从歌单移除，无法播放",
                                parent=self)
            return
        self._set_queue(list(self._shuffle_picks), "抽选")
        self.queue_pos = pos
        self.set_status(f"播放抽选结果第 {pos + 1} 首")
        self.play_index(index)

    def _play_shuffle_queue(self) -> None:
        """「▶ 播放抽选结果」按钮：从头连播抽中的几首。"""
        if not self._shuffle_picks:
            messagebox.showinfo("抽选结果", "还没有抽选结果，先点「🎲 抽选」",
                                parent=self)
            return
        self._set_queue(list(self._shuffle_picks), "抽选")
        self.set_status("开始连播抽选结果")
        self._play_queue_key(self._shuffle_picks[0])

    def _play_queue_key(self, key: str) -> None:
        """按歌曲键开始播放（找不到就提示）。"""
        index = self._index_of(key)
        if index < 0:
            messagebox.showinfo("播放", f"「{self._key_name(key)}」已从歌单移除，"
                                        "无法播放", parent=self)
            return
        self.play_index(index)

    # ---------------- 播放队列（抽选 / 歌单队列共用） ----------------
    def _set_queue(self, keys: List[str], label: str) -> None:
        self.queue = list(keys)
        self.queue_label = label
        self.queue_pos = 0
        self._refresh_shuffle_list()

    def _clear_queue(self) -> None:
        self.queue = []
        self.queue_pos = None
        self.queue_label = ""

    def _start_queue_play(self) -> None:
        """「▶ 队列播放」按钮：从当前选中（或第 1 首）开始按顺序连播整个歌单。"""
        if not self.songs:
            messagebox.showinfo("队列播放", "歌单是空的，先导入或添加歌曲", parent=self)
            return
        start = self._selected_index()
        keys = [self._key_of(s) for s in self.songs[start:]]
        self._set_queue(keys, "队列")
        self.set_status(f"队列播放：从第 {start + 1} 首开始，共 {len(self.queue)} 首")
        self._play_queue_key(keys[0])

    def _next_queue_key(self) -> Optional[str]:
        """取队列里的下一首（已被移除的自动跳过）。"""
        if not self.queue or self.queue_pos is None:
            return None
        pos = self.queue_pos + 1
        while pos < len(self.queue):
            if self._index_of(self.queue[pos]) >= 0:
                return self.queue[pos]
            pos += 1
        return None

    # ---------------- 从歌单移除 / 备份 ----------------
    def _backup_playlist(self) -> Optional[str]:
        """把当前歌单另存一份，避免删掉后找不回来。"""
        if not self.songs:
            return None
        path = os.path.join(APP_DIR, "歌单备份.txt")
        try:
            save_playlist(path, self.songs)
            return path
        except Exception:
            return None

    def _remove_key(self, key: str) -> bool:
        """从歌单里删掉一首（按歌曲键定位，不怕下标错位）。"""
        index = self._index_of(key)
        if index < 0:
            return False
        del self.songs[index]
        if self.current_index > index:
            self.current_index -= 1
        elif self.current_index == index:
            self.current_index = -1
        self._render_song_list()
        self._refresh_shuffle_list()
        return True

    def _remove_finished_if_enabled(self) -> None:
        """播完一首后，按设置把它从歌单移除（移除前先备份）。"""
        if not self.auto_remove_var.get() or not self.queue:
            return
        if self.queue_pos is None or not (0 <= self.queue_pos < len(self.queue)):
            return
        key = self.queue[self.queue_pos]
        name = self._key_name(key)
        path = self._backup_playlist()
        if self._remove_key(key):
            self.set_status(f"已从歌单移除：{name}"
                            + (f"（歌单已备份到 {os.path.basename(path)}）" if path else ""))

    def _reset_drawn(self) -> None:
        """重置抽选记录，让所有歌重新参与抽选（含持久化记录）。"""
        had = len(self._drawn)
        self._drawn.clear()
        self._draw_stats.clear()
        self._render_song_list()
        self._save_settings()
        self.set_status("已重置抽选记录，所有歌重新参与抽选"
                        + (f"（清掉 {had} 条）" if had else ""))

    def _paint_picks(self) -> None:
        """已抽过 / 本次抽中的曲目标成浅粉底，其余恢复默认。"""
        for pos, orig in enumerate(self._view_indices):
            song = self.songs[orig]
            key = self._key_of(song)
            if key in self._shuffle_picks or song.uid in self._drawn:
                self.song_list.itemconfig(pos, bg=ui_theme.PICK_BG, fg=ui_theme.PICK_FG)
            else:
                self.song_list.itemconfig(pos, bg=ui_theme.PANEL_SOFT, fg=ui_theme.FG)

    def _clear_shuffle_result(self) -> None:
        """只清空抽选结果（展示与标记），不影响播放队列。"""
        self._shuffle_picks = []
        self._pick_videos = {}
        self._shuffle_running = False
        if getattr(self, "btn_shuffle", None) and self.btn_shuffle.winfo_exists():
            self.btn_shuffle.configure(state="normal")
        self._sync_shuffle_button()
        self._paint_picks()
        self._refresh_shuffle_list()

    def _finish_queue(self, message: str) -> None:
        self._clear_queue()
        self._refresh_shuffle_list()
        self.now_var.set("播放结束")
        self.set_status(message)

    def _save_playlist(self) -> None:
        if not self.songs:
            return
        path = filedialog.asksaveasfilename(
            title="保存歌单", defaultextension=".txt",
            filetypes=[("文本歌单", "*.txt"), ("CSV", "*.csv"), ("JSON", "*.json")])
        if not path:
            return
        save_playlist(path, self.songs)
        self.status_var.set(f"歌单已保存：{path}")

    # ---------------- 浏览器 / Cookie ----------------
    def _open_login(self) -> None:
        def go():
            if not self.player.browser:
                return
            try:
                self.player.suspend()  # 登录页没有视频，先停掉播放监控
                self.player.browser.navigate("https://passport.bilibili.com/login")
            except Exception as exc:
                self._post(lambda: self.set_status(f"打开登录页失败：{exc}"))
                return
            self._post(lambda: self.set_status(
                "请在浏览器窗口里登录 B 站，登录完成后直接播放即可（登录状态会保存在独立目录中）"))

        self._ensure_browser(go)

    def _ensure_browser(self, then) -> None:
        if self.player.is_running:
            then()
            return
        self.set_status("正在启动浏览器…")

        def work():
            try:
                self.player.start(exe=self.browser_var.get().strip() or None)
                self._post(lambda: self.set_status("浏览器已就绪"))
                then()
            except Exception as exc:
                self._post(lambda: self.set_status(f"启动浏览器失败：{exc}"))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 播放流程 ----------------
    def _selected_index(self) -> int:
        sel = self.song_list.curselection()
        if sel:
            return self._view_indices[sel[0]]
        return self.current_index if self.current_index >= 0 else 0

    def _play_selected_song(self) -> None:
        if not self.songs:
            messagebox.showinfo("提示", "请先导入或添加歌曲")
            return
        self.play_index(self._selected_index())

    def play_index(self, index: int, auto: bool = False) -> None:
        if not (0 <= index < len(self.songs)):
            if auto:
                self.set_status("歌单已播放完毕")
                self.now_var.set("播放结束")
            return
        self.current_index = index
        self._ui_paused = False       # 切歌即恢复播放状态
        self._sync_pause_button()
        self._highlight_current()
        self._refresh_shuffle_list()
        self.result_list.delete(0, "end")
        self.results = []
        song = self.songs[index]
        self.now_var.set(song.display)
        self.set_status(f"搜索中：{song.keyword}")
        self._ensure_browser(lambda: self._run_search(index, song.keyword))

    def _search_current(self, research: bool = False) -> None:
        index = self.current_index if self.current_index >= 0 else self._selected_index()
        if not (0 <= index < len(self.songs)):
            return
        self.current_index = index
        self._highlight_current()
        song = self.songs[index]
        keyword = song.keyword
        if research:  # 允许临时修改关键词
            value = simpledialog.askstring("重新搜索", "搜索关键词：",
                                           initialvalue=keyword, parent=self)
            if value is None:
                return
            keyword = value.strip() or keyword
        self.set_status(f"搜索中：{keyword}")
        self._ensure_browser(lambda: self._run_search(index, keyword))

    def _run_search(self, index: int, keyword: str) -> None:
        if not (0 <= index < len(self.songs)):
            return
        song = self.songs[index]
        self._search_seq += 1
        seq = self._search_seq
        min_dur = self._int(self.min_var.get(), 60)
        max_dur = self._int(self.max_var.get(), 900)
        mode = self.match_var.get()
        cached = self._pick_videos.get(song.display)

        def work():
            videos = cached
            if videos is None:      # 抽选时已预搜索过就不必再搜
                try:
                    videos = self.searcher.search(keyword, page=1)
                except Exception as exc:
                    self._post(lambda: self._on_search_error(index, str(exc)))
                    return
            if seq != self._search_seq or index != self.current_index:
                return
            # 默认播放搜索列表第一首；切到「智能最佳匹配」时才按打分挑
            chosen = videos[0] if (mode != MATCH_BEST and videos) else \
                pick_best(videos, song.name, song.artist, min_dur, max_dur)
            self._post(lambda: self._fill_results(index, videos, chosen, mode))
            if chosen:
                self._start_video(index, chosen)

        threading.Thread(target=work, daemon=True).start()

    def _fill_results(self, owner_key: str, videos: List[Video],
                      chosen: Optional[Video], mode: str) -> None:
        self.results = videos
        self.results_owner = owner_key
        self.result_list.delete(0, "end")
        for i, v in enumerate(videos, 1):
            mark = "★ " if chosen is not None and v.bvid == chosen.bvid else "  "
            self.result_list.insert("end", f"{mark}{i:>2}. {v.label()}")
            if mark.startswith("★"):
                self.result_list.selection_set(i - 1)
                self.result_list.see(i - 1)
        if chosen:
            what = "第一条结果" if mode != MATCH_BEST else "最佳匹配"
            self.set_status(f"找到 {len(videos)} 个结果，正在播放{what}")
        elif videos:
            self.set_status(f"找到 {len(videos)} 个结果 —— 双击抽选结果里的某一首开始播放")
        else:
            self.set_status("没有找到结果")

    def _on_search_error(self, index: int, message: str) -> None:
        self.set_status(message)
        if self.auto_var.get() and index == self.current_index:
            self.after(2000, lambda: self.play_index(index + 1, auto=True))

    def _start_video(self, index: int, video: Video) -> None:
        if index != self.current_index:
            return
        song = self.songs[index]
        song.bvid = video.bvid
        song.picked = video.title
        self._recover_attempts = 0
        self._post(lambda: (
            self.now_var.set(f"{song.display}  ←  {video.title}"),
            self._refresh_song_row(index),
            self.set_status(f"播放中：{video.title}"),
        ))
        try:
            self.player.play(video.bvid)
        except Exception as exc:
            self._post(lambda: self.set_status(f"播放失败：{exc}"))

    def _play_selected_result(self, event=None) -> None:
        """双击搜索结果：直接播放它（可以是抽选后预搜索出来的结果）。"""
        sel = self.result_list.curselection()
        if not sel or not (0 <= sel[0] < len(self.results)):
            return
        owner = self.results_owner
        if owner is None and 0 <= self.current_index < len(self.songs):
            owner = self._key_of(self.songs[self.current_index])
        index = self._index_of(owner) if owner else -1
        if index < 0:
            return
        if self.current_index >= 0 and index != self.current_index:
            return                       # 正在播别的歌，忽略
        if self.current_index < 0:       # 还没开始播：切到该曲，抽选的话顺便排进队列
            self.current_index = index
            self._highlight_current()
            if owner in self._shuffle_picks:
                self._set_queue(list(self._shuffle_picks), "抽选")
                self.queue_pos = self._shuffle_picks.index(owner)
                self._refresh_shuffle_list()
        self._start_video(index, self.results[sel[0]])

    def _open_in_web(self) -> None:
        sel = self.result_list.curselection()
        if sel and 0 <= sel[0] < len(self.results):
            webbrowser.open(self.results[sel[0]].page_url)
        elif 0 <= self.current_index < len(self.songs) and self.songs[self.current_index].bvid:
            webbrowser.open(f"https://www.bilibili.com/video/"
                            f"{self.songs[self.current_index].bvid}")

    def _highlight_current(self) -> None:
        self.song_list.selection_clear(0, "end")
        self._paint_picks()          # 抽中标记要保留
        if 0 <= self.current_index < len(self.songs):
            try:
                pos = self._view_indices.index(self.current_index)
            except ValueError:
                return   # 当前歌被搜索过滤掉了，不在可见列表里
            self.song_list.selection_set(pos)
            self.song_list.see(pos)
            self.song_list.itemconfig(pos, fg=ui_theme.ACCENT)

    # ---------------- 播放器回调 ----------------
    def _on_state(self, state: dict) -> None:
        self._post(lambda: self._render_state(state))

    def _render_state(self, state: dict) -> None:
        duration = state.get("d") or 0
        current = state.get("t") or 0
        self._paused = bool(state.get("paused"))
        self.time_var.set(f"{fmt_duration(current)} / {fmt_duration(duration)}")
        if duration:
            self.progress["value"] = min(100.0, current / duration * 100)
        self._render_now_label()

    def _render_now_label(self) -> None:
        if not (0 <= self.current_index < len(self.songs)):
            return
        label = "暂停" if (self._ui_paused or self._paused) else "播放中"
        self.now_var.set(f"{self.songs[self.current_index].display}   [{label}]")

    def _on_ended(self) -> None:
        self._post(self._handle_ended)

    def _handle_ended(self) -> None:
        if self._timer_pending:          # 定时到点：播完这首就收尾，不再连播
            self._timer_pending = False
            self._do_timer_action()
            return
        self._advance(delay=2000, reason="本首播放完毕")

    def _advance(self, delay: int, reason: str) -> bool:
        """安排下一首：有播放队列就走队列，否则按自动连播走歌单顺序。"""
        if self.queue:
            key = self._next_queue_key()
            if key is not None:
                self._remove_finished_if_enabled()   # 播完的这首按设置决定是否移出歌单
                self.queue_pos = self.queue.index(key)
                total = len(self.queue)
                self.set_status(f"{reason}，即将播放{self.queue_label}下一首"
                                f"（{self.queue_pos + 1}/{total}）…")
                self.after(delay, lambda: self._play_queue_key(key))
                self._refresh_shuffle_list()
                return True
            if self.auto_remove_var.get():
                self._remove_finished_if_enabled()   # 最后一首播完也移除
            total = len(self.queue)
            self._finish_queue(f"{self.queue_label}播放完毕（共 {total} 首）")
            return False
        if self.auto_var.get() and self.current_index + 1 < len(self.songs):
            self.set_status(f"{reason}，即将播放下一首…")
            self.after(delay, lambda: self.play_index(self.current_index + 1, auto=True))
            return True
        self.now_var.set("播放结束")
        self.set_status("播放结束")
        return False

    def _on_error(self, message: str) -> None:
        self._post(lambda: self._handle_error(message))

    def _handle_error(self, message: str) -> None:
        # 浏览器已经不在了：交给关闭恢复逻辑处理，不要当成播放失败跳下一首
        if not self.player.is_running:
            return
        self.set_status(message)
        if self.queue or (self.auto_var.get() and self.current_index >= 0):
            self._advance(delay=3000, reason="播放异常，跳过")

    # ---------------- 窗口被关闭 → 自动拉回 ----------------
    def _on_closed(self) -> None:
        self._post(self._handle_closed)

    def _handle_closed(self) -> None:
        if not self.player.is_active:
            return
        self._recover_attempts += 1
        if self._recover_attempts > 5:
            self.set_status("浏览器反复被关闭，已停止自动重新打开（点「播放」可重新开始）")
            return
        self.set_status(f"检测到浏览器窗口被关闭，正在自动重新打开…"
                        f"（第 {self._recover_attempts} 次）")
        self.now_var.set(f"{self.songs[self.current_index].display}   [等待恢复]"
                         if 0 <= self.current_index < len(self.songs) else "等待恢复")
        threading.Thread(target=self._recover_worker, daemon=True).start()

    def _recover_worker(self) -> None:
        try:
            self.player.recover()
        except Exception as exc:
            self._post(lambda: self.set_status(f"自动重新打开失败：{exc}"))
        else:
            self._post(self._on_recovered)

    def _on_recovered(self) -> None:
        self._recover_attempts = 0
        self.set_status("已重新打开浏览器，继续播放")

    # ---------------- 控制按钮 ----------------
    def _prev(self) -> None:
        base = self.current_index if self.current_index > 0 else 1
        self.play_index(base - 1)

    def _next(self) -> None:
        if self.queue and self.queue_pos is not None:
            key = self._next_queue_key()
            if key is not None:
                self.queue_pos = self.queue.index(key)
                self._play_queue_key(key)
                self._refresh_shuffle_list()
            else:
                self._finish_queue(f"{self.queue_label}播放完毕（共 {len(self.queue)} 首）")
            return
        self.play_index((self.current_index if self.current_index >= 0 else -1) + 1)

    def _toggle_pause(self) -> None:
        if not self.player.is_running:
            self._play_selected_song()
            return
        # 用本地状态决定下一步：播放器状态每秒才回传一次，跟着它走会点两次才生效
        if self._ui_paused:
            self.player.resume()
            self._ui_paused = False
            self.set_status("继续播放")
        else:
            self.player.pause()
            self._ui_paused = True
            self.set_status("已暂停")
        self._sync_pause_button()
        self._render_now_label()

    def _sync_pause_button(self) -> None:
        self.btn_pause.configure(text="▶ 继续" if self._ui_paused else "⏸ 暂停")

    def _stop(self) -> None:
        self.player.stop()
        self._cancel_timer()             # 手动停止就撤掉定时
        self._clear_queue()
        self._refresh_shuffle_list()
        self.now_var.set("已停止")
        self.set_status("已停止播放（抽选结果保留）")

    def _toggle_floating(self) -> None:
        """打开/关闭桌面悬浮窗（单例）。"""
        if self._floating is not None:
            self._floating.destroy()
            self._floating = None
            return
        self._floating = FloatingWindow(self)

    def _summon_floating(self) -> None:
        """若悬浮窗未打开则打开它（供最小化自动召出使用）。"""
        if self._floating is None:
            self._floating = FloatingWindow(self)

    def _on_unmap(self, event=None) -> None:
        """主窗口最小化（iconic）时自动召出悬浮窗。"""
        try:
            if self.state() == "iconic":
                self._summon_floating()
        except Exception:
            pass

    def _bring_front(self) -> None:
        """把播放页面拉回前台；页面被逛走时顺带强制导航回当前视频。"""
        if not self.player.is_running:
            self.set_status("浏览器还没启动")
            return
        self.player.restore_page()
        self.player.bring_front()
        self.set_status("已把播放页面拉回前台")

    # ---------------- 退出 ----------------
    def _on_close(self) -> None:
        self._cancel_timer()
        try:                                  # 记住窗口大小与位置，下次按同样分辨率还原
            geom = self.geometry()
            if self.GEOM_RE.match(geom.strip()):
                self._saved_geom = geom
        except Exception:
            pass
        self._save_sash()
        self._save_settings()
        self._close_overlay()
        try:
            self.player.quit()
        except Exception:
            pass
        self.destroy()


def main() -> None:
    ui_theme.enable_dpi_awareness()   # 必须在创建窗口之前，否则高分屏会发虚
    App().mainloop()


if __name__ == "__main__":
    main()
