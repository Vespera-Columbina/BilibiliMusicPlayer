"""设置窗口：把不常用的配置项收进分页对话框。"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import filedialog, ttk

import ui_theme
import video_overlay

# 自己算，避免和 main 形成循环导入；打包后指向 exe 所在目录
APP_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))


class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("设置")
        self.geometry("580x560")
        self.minsize(540, 520)
        self.transient(app)
        self.resizable(False, False)

        glass = bool(app.glass_var.get())
        solid = bool(app.solid_var.get())
        see_through = glass and not solid
        self.configure(bg=ui_theme.GLASS_KEY if see_through else ui_theme.BG_SOLID)
        ui_theme.setup_style(self, glass=see_through)
        if glass:
            self.after(60, lambda: (
                ui_theme.enable_translucent(self, alpha=float(app.alpha_var.get()))
                if solid else
                ui_theme.enable_glass(self, alpha=float(app.alpha_var.get()),
                                      tint_alpha=int(float(app.tint_var.get() or 235)))))

        # 用副本编辑，点「应用/确定」才写回
        self.auto_next = tk.BooleanVar(value=app.auto_var.get())
        self.match_mode = tk.StringVar(value=app.match_var.get())
        self.pick_count = tk.StringVar(value=app.pick_count_var.get())
        self.video = tk.BooleanVar(value=app.video_var.get())
        self.video_path = tk.StringVar(value=app.video_path_var.get())
        self.min_dur = tk.StringVar(value=app.min_var.get())
        self.max_dur = tk.StringVar(value=app.max_var.get())
        self.browser = tk.StringVar(value=app.browser_var.get())
        self.port = tk.StringVar(value=app.port_var.get())
        self.cookie = tk.StringVar(value=app.cookie_var.get())
        self.glass = tk.BooleanVar(value=glass)
        self.alpha = tk.DoubleVar(value=float(app.alpha_var.get()))
        self.tint = tk.IntVar(value=int(float(app.tint_var.get() or 235)))
        self.solid = tk.BooleanVar(value=bool(app.solid_var.get()))

        self._build()

    # ---------------- 界面 ----------------
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 4))
        nb.add(self._tab_play(nb), text="播放")
        nb.add(self._tab_browser(nb), text="浏览器")
        nb.add(self._tab_appearance(nb), text="外观")

        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 10))
        bar.columnconfigure(0, weight=1)
        ttk.Button(bar, text="应用", style="Accent.TButton",
                   command=self._apply).pack(side="right")
        ttk.Button(bar, text="确定",
                   command=self._ok).pack(side="right", padx=(0, 8))
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right",
                                                                padx=(0, 8))
        self.hint = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.hint, style="Sub.TLabel").pack(side="left")

    def _tab_play(self, parent) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=14)
        tab.columnconfigure(1, weight=1)

        ttk.Checkbutton(tab, text="播完自动播放下一首（自动连播）",
                        variable=self.auto_next).grid(row=0, column=0,
                                                      columnspan=3, sticky="w")

        ttk.Label(tab, text="匹配方式：").grid(row=1, column=0, sticky="w", pady=(14, 0))
        ttk.Combobox(tab, textvariable=self.match_mode, width=18, state="readonly",
                     values=(self.app.MATCH_FIRST, self.app.MATCH_BEST)
                     ).grid(row=1, column=1, sticky="w", pady=(14, 0))

        ttk.Label(tab, text="抽选个数：").grid(row=2, column=0, sticky="w", pady=(14, 0))
        ttk.Spinbox(tab, from_=1, to=99, width=6, textvariable=self.pick_count,
                    wrap=False).grid(row=2, column=1, sticky="w", pady=(14, 0))
        ttk.Label(tab, text="点「🎲 抽选」时抽几首（歌单不够会提示）",
                  style="Sub.TLabel").grid(row=2, column=2, sticky="w",
                                           padx=(12, 0), pady=(14, 0))

        ttk.Label(tab, text="时长范围（秒）：").grid(row=3, column=0, sticky="w", pady=(14, 0))
        dur = ttk.Frame(tab)
        dur.grid(row=3, column=1, sticky="w", pady=(14, 0))
        ttk.Entry(dur, textvariable=self.min_dur, width=7).pack(side="left")
        ttk.Label(dur, text=" ~ ").pack(side="left")
        ttk.Entry(dur, textvariable=self.max_dur, width=7).pack(side="left")

        ttk.Checkbutton(tab, text="抽选时全屏播放视频动画（独占屏幕，点一下可跳过）",
                        variable=self.video).grid(row=4, column=0, columnspan=3,
                                                  sticky="w", pady=(14, 0))

        ttk.Label(tab, text="动画文件：").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(tab, textvariable=self.video_path).grid(row=5, column=1,
                                                          sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(tab, text="选择…", command=self._pick_video).grid(row=5, column=2,
                                                                     pady=(8, 0))
        self.video_state = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.video_state, style="Sub.TLabel",
                  wraplength=480, justify="left").grid(row=6, column=0, columnspan=3,
                                                       sticky="w", pady=(6, 0))

        ttk.Label(tab, text="默认播放搜索结果的第一条；改成「智能最佳匹配」后，"
                            "会按标题/歌手命中、时长、播放量打分挑选（时长范围仅在此时生效）。",
                  style="Sub.TLabel", wraplength=480, justify="left"
                  ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(14, 0))
        self._sync_video_state()
        return tab

    def _pick_video(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择抽选动画文件",
            filetypes=[("视频", "*.mp4 *.webm *.mov *.avi *.mkv"), ("所有文件", "*.*")])
        if path:
            self.video_path.set(path)
            self._sync_video_state()

    def _sync_video_state(self) -> None:
        custom = self.video_path.get().strip()
        if custom and os.path.exists(custom):
            self.video_state.set(f"已指定：{custom}")
            return
        if custom:
            self.video_state.set(f"指定的文件不存在：{custom}（将回退到程序目录）")
            return
        found = video_overlay.find_video(APP_DIR, getattr(sys, "_MEIPASS", APP_DIR))
        if found:
            self.video_state.set(f"使用程序目录里的动画：{found}")
        else:
            self.video_state.set("程序目录里没有 video.mp4，也没有指定文件，"
                                 "抽选时将使用列表闪动动画")

    def _tab_browser(self, parent) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=14)
        tab.columnconfigure(1, weight=1)

        ttk.Label(tab, text="浏览器：").grid(row=0, column=0, sticky="w")
        ttk.Entry(tab, textvariable=self.browser).grid(row=0, column=1,
                                                       sticky="ew", padx=6)
        ttk.Button(tab, text="选择…", command=self._pick_browser).grid(row=0, column=2)

        ttk.Label(tab, text="调试端口：").grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(tab, textvariable=self.port, width=10).grid(row=1, column=1,
                                                              sticky="w", padx=6,
                                                              pady=(12, 0))
        ttk.Label(tab, text="换端口可避免与其它调试中的浏览器冲突（下次启动浏览器生效）",
                  style="Sub.TLabel").grid(row=1, column=1, sticky="e", pady=(12, 0))

        ttk.Label(tab, text="Cookie（可选）：").grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(tab, textvariable=self.cookie).grid(row=2, column=1,
                                                      sticky="ew", padx=6, pady=(12, 0))
        ttk.Button(tab, text="登录", command=self._login).grid(row=2, column=2,
                                                               pady=(12, 0))

        ttk.Label(tab, text="填 SESSDATA=xxx; bili_jct=xxx 可降低搜索被风控的概率。"
                            "获取方式：浏览器登录 B 站 → F12 → 应用/存储 → Cookie。"
                            "点「登录」可在程序专用窗口里直接登录。",
                  style="Sub.TLabel", wraplength=480, justify="left"
                  ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(12, 0))
        return tab

    def _tab_appearance(self, parent) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=14)
        tab.columnconfigure(1, weight=1)

        ttk.Checkbutton(tab, text="亚克力 / 毛玻璃窗口（半透明）",
                        variable=self.glass).grid(row=0, column=0, columnspan=3,
                                                  sticky="w")
        ttk.Checkbutton(tab, text="背景用浅色实底（默认开启，背景必定是白的）",
                        variable=self.solid).grid(row=1, column=0, columnspan=3,
                                                  sticky="w", pady=(10, 0))

        ttk.Label(tab, text="不透明度：").grid(row=2, column=0, sticky="w", pady=(14, 0))
        scale = ttk.Scale(tab, from_=0.70, to=1.0, orient="horizontal",
                          variable=self.alpha)
        scale.grid(row=2, column=1, sticky="ew", pady=(14, 0))
        self.alpha_label = ttk.Label(tab, text="", style="Sub.TLabel", width=6)
        self.alpha_label.grid(row=2, column=2, pady=(14, 0))
        scale.configure(command=lambda v: self._sync_alpha_label())
        self._sync_alpha_label()

        ttk.Label(tab, text="背景白度：").grid(row=3, column=0, sticky="w", pady=(14, 0))
        tint_scale = ttk.Scale(tab, from_=120, to=255, orient="horizontal",
                               variable=self.tint)
        tint_scale.grid(row=3, column=1, sticky="ew", pady=(14, 0))
        self.tint_label = ttk.Label(tab, text="", style="Sub.TLabel", width=6)
        self.tint_label.grid(row=3, column=2, pady=(14, 0))
        tint_scale.configure(command=lambda v: self._sync_tint_label())
        self._sync_tint_label()

        ttk.Label(tab, text="「背景白度」控制毛玻璃底色的白度，越接近 255 越白（仅亚克力模式生效）。"
                            "若背景在你的机器上仍然发暗/发灰，勾选「浅色实底」即可；"
                            "系统不支持时也会自动退回浅色实底。",
                  style="Sub.TLabel", wraplength=480, justify="left"
                  ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(14, 0))
        return tab

    def _sync_alpha_label(self) -> None:
        self.alpha_label.configure(text=f"{self.alpha.get():.2f}")

    def _sync_tint_label(self) -> None:
        self.tint_label.configure(text=str(int(self.tint.get())))

    # ---------------- 操作 ----------------
    def _pick_browser(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择 Edge/Chrome 可执行文件",
            filetypes=[("浏览器", "*.exe"), ("所有文件", "*.*")])
        if path:
            self.browser.set(path)

    def _login(self) -> None:
        self._apply()
        self.app._open_login()
        self.hint.set("已打开登录窗口")

    def _apply(self) -> None:
        app = self.app
        app.auto_var.set(self.auto_next.get())
        app.match_var.set(self.match_mode.get())
        app.pick_count_var.set(self.pick_count.get().strip() or "2")
        app.video_var.set(self.video.get())
        app.video_path_var.set(self.video_path.get().strip())
        app.min_var.set(self.min_dur.get().strip() or "60")
        app.max_var.set(self.max_dur.get().strip() or "900")
        app.browser_var.set(self.browser.get().strip())
        app.port_var.set(self.port.get().strip() or "9222")
        app.cookie_var.set(self.cookie.get().strip())
        app.glass_var.set(self.glass.get())
        app.alpha_var.set(f"{self.alpha.get():.2f}")
        app.tint_var.set(str(int(self.tint.get())))
        app.solid_var.set(self.solid.get())

        effect = app.apply_settings()
        if self.glass.get():
            self.hint.set("已应用" + (f"（{effect}）" if effect else "（系统不支持，已用普通界面）"))
        else:
            self.hint.set("已应用")

    def _ok(self) -> None:
        self._apply()
        self.destroy()
