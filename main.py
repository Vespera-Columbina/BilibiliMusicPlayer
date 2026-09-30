"""Bilibili 歌单自动播放器 —— 图形界面入口。

用法：python main.py
"""

from __future__ import annotations

import json
import os
import queue
import random
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import List, Optional

import ui_theme
import video_overlay
from bilibili import BiliSearcher, Video, fmt_duration, pick_best
from player import BiliPlayer, find_browser
from playlist import Song, load_playlist, parse_line, save_playlist
from settings_dialog import SettingsDialog

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

    def __init__(self):
        super().__init__()
        self.title("Bilibili 歌单自动播放器")
        self.geometry("1180x720")
        self.minsize(960, 620)

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

        # 抽选结果 + 播放队列（抽选/队列播放共用一套连播机制）
        self._shuffle_picks: List[int] = []   # 抽中的歌曲索引，用于展示与标记
        self._shuffle_running = False
        self.queue: List[int] = []            # 当前连播队列（歌曲索引）
        self.queue_pos: Optional[int] = None
        self.queue_label = ""                 # "抽选" / "队列"
        self._overlay: Optional[object] = None  # 抽选时的全屏视频浮层

        # 设置项（不常用的都收进「设置」对话框）
        self.browser_var = tk.StringVar()
        self.cookie_var = tk.StringVar()
        self.min_var = tk.StringVar(value="60")
        self.max_var = tk.StringVar(value="900")
        self.auto_var = tk.BooleanVar(value=True)
        self.match_var = tk.StringVar(value=MATCH_FIRST)
        self.pick_count_var = tk.StringVar(value=str(SHUFFLE_COUNT))  # 抽选个数
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

        self.configure(bg=ui_theme.GLASS_KEY)
        ui_theme.setup_style(self, glass=True)
        self._build_ui()
        self._load_settings()
        self.after(80, self._drain)
        self.after(120, self._init_glass)   # 等窗口真正创建出来再上毛玻璃
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
        self.video_path = self._current_video_path()
        self.searcher.set_cookie(self.cookie_var.get().strip())
        self._save_settings()
        return self._glass_effect

    def _style_widgets(self) -> None:
        """tk 原生控件（Listbox）不跟随 ttk 样式，单独刷一遍颜色。"""
        kwargs = ui_theme.listbox_kwargs()
        for box in (self.song_list, self.result_list):
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
        self._make_draggable(head)

        # 主体
        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)
        body.columnconfigure(0, weight=2)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(body, text=" 歌单 ", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

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

        self.song_list = tk.Listbox(left, **ui_theme.listbox_kwargs())
        self.song_list.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, command=self.song_list.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.song_list.configure(yscrollcommand=scroll.set)
        self.song_list.bind("<Double-Button-1>", lambda e: self._play_selected_song())

        # 右侧：搜索结果 / 抽选结果 分页
        right_nb = ttk.Notebook(body)
        right_nb.grid(row=0, column=1, sticky="nsew")

        right = ttk.Frame(right_nb, padding=6)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        right_nb.add(right, text="搜索结果（双击换视频）")

        self.result_list = tk.Listbox(right, **ui_theme.listbox_kwargs())
        self.result_list.grid(row=0, column=0, sticky="nsew")
        rscroll = ttk.Scrollbar(right, command=self.result_list.yview)
        rscroll.grid(row=0, column=1, sticky="ns")
        self.result_list.configure(yscrollcommand=rscroll.set)
        self.result_list.bind("<Double-Button-1>", self._play_selected_result)

        rbar = ttk.Frame(right)
        rbar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(rbar, text="重新搜索", style="Tool.TButton",
                   command=lambda: self._search_current(True)).pack(side="left")
        ttk.Button(rbar, text="在浏览器打开", style="Tool.TButton",
                   command=self._open_in_web).pack(side="left", padx=6)

        pick = ttk.Frame(right_nb, padding=6)
        pick.rowconfigure(0, weight=1)
        pick.columnconfigure(0, weight=1)
        right_nb.add(pick, text="抽选结果（双击播放）")

        self.shuffle_list = tk.Listbox(pick, **ui_theme.listbox_kwargs())
        self.shuffle_list.grid(row=0, column=0, sticky="nsew")
        pscroll = ttk.Scrollbar(pick, command=self.shuffle_list.yview)
        pscroll.grid(row=0, column=1, sticky="ns")
        self.shuffle_list.configure(yscrollcommand=pscroll.set)
        self.shuffle_list.bind("<Double-Button-1>", self._play_shuffle_result)

        pbar = ttk.Frame(pick)
        pbar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(pbar, text="▶ 播放抽选结果", style="Tool.TButton",
                   command=self._play_shuffle_queue).pack(side="left")
        ttk.Button(pbar, text="▶ 队列播放", style="Tool.TButton",
                   command=self._start_queue_play).pack(side="left", padx=6)
        ttk.Button(pbar, text="清空结果", style="Tool.TButton",
                   command=self._clear_shuffle_result).pack(side="left")
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
        for i, btn in enumerate((self.btn_prev, self.btn_play, self.btn_queue,
                                 self.btn_pause, self.btn_next, self.btn_stop,
                                 self.btn_front)):
            btn.grid(row=0, column=i, padx=(0, 6))

        self.now_var = tk.StringVar(value="未在播放")
        ttk.Label(ctrl, textvariable=self.now_var,
                  font=(ui_theme.FONT, 11, "bold")).grid(
            row=0, column=7, sticky="w", padx=(12, 0))

        self.time_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(ctrl, textvariable=self.time_var).grid(row=0, column=8,
                                                         padx=(0, 10))

        self.progress = ttk.Progressbar(ctrl, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, columnspan=8, sticky="ew", pady=(10, 0))

        self.status_var = tk.StringVar(value="就绪：先导入或添加歌曲，然后点「播放」")
        ttk.Label(ctrl, textvariable=self.status_var, style="Sub.TLabel").grid(
            row=1, column=8, sticky="e", pady=(10, 0))

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
        self.video_path_var.set(data.get("video_path", ""))
        self.port_var.set(str(data.get("port", 9222)))
        self.glass_var.set(bool(data.get("glass", True)))
        self.alpha_var.set(str(data.get("alpha", 0.96)))
        self.tint_var.set(str(data.get("tint_alpha", 235)))
        self.solid_var.set(bool(data.get("solid_bg", True)))
        if self.cookie_var.get():
            self.searcher.set_cookie(self.cookie_var.get())
        last = data.get("last_playlist")
        if last and os.path.exists(last):
            try:
                self._set_songs(load_playlist(last))
                self.status_var.set(f"已载入上次歌单：{last}")
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
            "video_path": self.video_path_var.get().strip(),
            "port": self._int(self.port_var.get(), 9222),
            "glass": self.glass_var.get(),
            "alpha": float(self.alpha_var.get() or 0.96),
            "tint_alpha": self._int(self.tint_var.get(), 235),
            "solid_bg": self.solid_var.get(),
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
    def _set_songs(self, songs: List[Song]) -> None:
        self.songs = songs
        self.song_list.delete(0, "end")
        for i, s in enumerate(songs, 1):
            self.song_list.insert("end", f"{i:>3}. {s.display}")
        self.song_list.selection_clear(0, "end")
        self.count_var.set(f"歌单：{len(songs)} 首")
        self._clear_shuffle_result()  # 列表变了，旧的抽选结果作废
        self._clear_queue()

    def _refresh_song_row(self, index: int) -> None:
        if 0 <= index < len(self.songs):
            s = self.songs[index]
            self.song_list.delete(index)
            self.song_list.insert(index, f"{index + 1:>3}. {s.display}")

    def _import_playlist(self) -> None:
        path = filedialog.askopenfilename(title="选择歌单文件", filetypes=FILE_TYPES)
        if not path:
            return
        try:
            songs = load_playlist(path)
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        if not songs:
            messagebox.showwarning("空歌单", "没有从该文件里解析到歌曲")
            return
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
                self.songs.append(song)
        self._set_songs(self.songs)

    def _remove_song(self) -> None:
        sel = self.song_list.curselection()
        if not sel:
            return
        index = sel[0]
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
        """从歌单随机抽 N 首：先跑抽取动画，定格后自动连播这几首。"""
        if self._shuffle_running:
            return
        want = max(1, self._int(self.pick_count_var.get(), SHUFFLE_COUNT))
        if len(self.songs) < want:
            messagebox.showinfo("抽选模式",
                                f"歌单至少需要 {want} 首歌才能抽选"
                                f"（当前 {len(self.songs)} 首）", parent=self)
            return

        self._shuffle_running = True
        self.btn_shuffle.configure(text="🎲 抽选中…", state="disabled")
        self.set_status(f"正在抽选 {want} 首…")
        picks = random.sample(range(len(self.songs)), want)
        self._shuffle_picks = picks
        self._refresh_shuffle_list()

        # 开了开关且找得到动画文件，就全屏独占播放它；否则用列表闪动
        path = self._current_video_path()
        if path and video_overlay.available() and self.video_var.get():
            try:
                # 播完一遍才收尾；过长的视频由 MAX 兜底，点击/Esc 可随时跳过
                self._overlay = video_overlay.VideoOverlay(
                    self, path,
                    on_close=self._skip_shuffle_video,
                    on_finish=lambda: self._shuffle_finish_if_running(picks),
                    loop=False, min_ms=SHUFFLE_VIDEO_MIN_MS)
            except Exception:
                self._overlay = None
        if self._overlay is not None:
            self.after(SHUFFLE_VIDEO_MAX_MS,
                       lambda: self._shuffle_finish_if_running(picks))
        else:
            self._shuffle_anim(0, picks)

    def _shuffle_finish_if_running(self, picks: List[int]) -> None:
        """全屏动画到点结束；用户提前关闭时不再重复收尾。"""
        if self._shuffle_running:
            self._shuffle_finish(picks)

    def _skip_shuffle_video(self) -> None:
        """用户点了全屏动画（或按 Esc）→ 立即出结果。"""
        self._overlay = None
        if self._shuffle_running and self._shuffle_picks:
            self._shuffle_finish(list(self._shuffle_picks))

    def _shuffle_anim(self, step: int, picks: List[int]) -> None:
        """抽取动画：列表快速随机闪动并逐渐减速，最后定格在抽中的两首。"""
        if not self._shuffle_running:
            return
        tail = SHUFFLE_STEPS - 4
        if step >= tail:                     # 最后几帧在两个结果间来回，做定格过渡
            idx = picks[step % len(picks)]
        else:
            idx = random.randrange(len(self.songs))

        self._paint_picks()
        self.song_list.itemconfig(idx, bg=ui_theme.ACCENT, fg="white")
        self.song_list.see(idx)
        self.now_var.set(f"🎲 {self.songs[idx].display}")

        step += 1
        if step < SHUFFLE_STEPS:
            delay = 40 + int(230 * (step / SHUFFLE_STEPS) ** 2)   # 越来越慢
            self.after(delay, lambda: self._shuffle_anim(step, picks))
        else:
            self._shuffle_finish(picks)

    def _shuffle_finish(self, picks: List[int]) -> None:
        self._shuffle_running = False
        self._close_overlay()
        self.btn_shuffle.configure(state="normal")
        self._sync_shuffle_button()
        self._shuffle_picks = picks
        self._paint_picks()
        self._refresh_shuffle_list()
        names = "  →  ".join(self.songs[i].display for i in picks)
        self.set_status(f"抽中：{names}，开始连播")
        self._set_queue(picks, "抽选")
        self.play_index(picks[0])

    # ---------------- 抽选结果列表 ----------------
    def _refresh_shuffle_list(self) -> None:
        """刷新「抽选结果」列表：▶ 播放中 / ✓ 已播 / · 待播。"""
        if self.queue:
            self.queue_var.set(f"{self.queue_label}："
                               f"{(self.queue_pos or 0) + 1}/{len(self.queue)}")
        else:
            self.queue_var.set("队列：未开始")
        box = self.shuffle_list
        box.delete(0, "end")
        for pos, song_index in enumerate(self._shuffle_picks):
            if not (0 <= song_index < len(self.songs)):
                continue
            if song_index == self.current_index:
                mark = "▶"
            elif self.queue_label == "抽选" and pos < (self.queue_pos or 0):
                mark = "✓"
            else:
                mark = "·"
            box.insert("end", f"{mark} {pos + 1}. {self.songs[song_index].display}")

    def _play_shuffle_result(self, event=None) -> None:
        """双击抽选结果里的某一首，直接从这首开始连播抽选结果。"""
        sel = self.shuffle_list.curselection()
        if not sel or not (0 <= sel[0] < len(self._shuffle_picks)):
            return
        pos = sel[0]
        song_index = self._shuffle_picks[pos]
        if not (0 <= song_index < len(self.songs)):
            return
        self._set_queue(list(self._shuffle_picks), "抽选")
        self.queue_pos = pos
        self.set_status(f"播放抽选结果第 {pos + 1} 首")
        self.play_index(song_index)

    def _play_shuffle_queue(self) -> None:
        """「▶ 播放抽选结果」按钮：从头连播抽中的两首。"""
        if not self._shuffle_picks:
            messagebox.showinfo("抽选结果", "还没有抽选结果，先点「🎲 抽选 2 首」",
                                parent=self)
            return
        self._set_queue(list(self._shuffle_picks), "抽选")
        self.set_status("开始连播抽选结果")
        self.play_index(self._shuffle_picks[0])

    # ---------------- 播放队列（抽选 / 歌单队列共用） ----------------
    def _set_queue(self, indexes: List[int], label: str) -> None:
        self.queue = list(indexes)
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
        self._set_queue(list(range(start, len(self.songs))), "队列")
        self.set_status(f"队列播放：从第 {start + 1} 首开始，共 {len(self.queue)} 首")
        self.play_index(self.queue[0])

    def _paint_picks(self) -> None:
        """把抽中的曲目标成浅粉底，其余恢复默认。"""
        for i in range(self.song_list.size()):
            if i in self._shuffle_picks:
                self.song_list.itemconfig(i, bg=ui_theme.PICK_BG, fg=ui_theme.PICK_FG)
            else:
                self.song_list.itemconfig(i, bg=ui_theme.PANEL_SOFT, fg=ui_theme.FG)

    def _clear_shuffle_result(self) -> None:
        """只清空抽选结果（展示与标记），不影响播放队列。"""
        self._shuffle_picks = []
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
            return sel[0]
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

        def work():
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

    def _fill_results(self, index: int, videos: List[Video],
                      chosen: Optional[Video], mode: str) -> None:
        self.results = videos
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
        else:
            self.set_status(f"找到 {len(videos)} 个结果，但没有合适匹配")

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
        sel = self.result_list.curselection()
        if not sel or not (0 <= sel[0] < len(self.results)):
            return
        if self.current_index < 0:
            return
        self._start_video(self.current_index, self.results[sel[0]])

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
            self.song_list.selection_set(self.current_index)
            self.song_list.see(self.current_index)
            self.song_list.itemconfig(self.current_index, fg=ui_theme.ACCENT)

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
        self._advance(delay=2000, reason="本首播放完毕")

    def _advance(self, delay: int, reason: str) -> bool:
        """安排下一首：有播放队列就走队列，否则按自动连播走歌单顺序。"""
        if self.queue:
            nxt = (self.queue_pos or 0) + 1
            if nxt < len(self.queue):
                self.queue_pos = nxt
                self.set_status(f"{reason}，即将播放{self.queue_label}下一首"
                                f"（{nxt + 1}/{len(self.queue)}）…")
                self.after(delay, lambda: self.play_index(self.queue[nxt]))
                self._refresh_shuffle_list()
                return True
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
            nxt = self.queue_pos + 1
            if nxt < len(self.queue):
                self.queue_pos = nxt
                self.play_index(self.queue[nxt])
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
        self._clear_queue()
        self._refresh_shuffle_list()
        self.now_var.set("已停止")
        self.set_status("已停止播放（抽选结果保留）")

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
        self._save_settings()
        self._close_overlay()
        try:
            self.player.quit()
        except Exception:
            pass
        self.destroy()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
