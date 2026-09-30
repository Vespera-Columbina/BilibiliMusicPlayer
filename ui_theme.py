"""窗口玻璃效果（Win11 Mica / Win10 亚克力）与 ttk 主题。

仅在 Windows 上生效；其他平台或系统不支持时自动降级为普通深色界面，
不会影响功能。
"""

from __future__ import annotations

import ctypes
import platform
from tkinter import ttk

IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    try:
        import winreg
    except Exception:
        winreg = None
else:
    winreg = None

# 关键色：窗口上「这个颜色」的像素会被系统抠掉，从而露出背后的毛玻璃
GLASS_KEY = "#010101"

# 浅色系
BG_SOLID = "#f2f3f7"     # 关闭玻璃效果时的实心底色
PANEL = "#fbfbfe"        # 面板（半透明观感）
PANEL_SOFT = "#eceef5"   # 输入/列表底
PANEL_HI = "#d3d8e6"     # 描边、分隔线
FG = "#1c1f28"
SUBFG = "#61687c"
ACCENT = "#fb7299"       # B 站粉
ACCENT_BLUE = "#00aeec"  # B 站蓝
PICK_BG = "#ffe1ea"      # 抽中歌曲底色（浅粉）
PICK_FG = "#b3245b"      # 抽中歌曲文字
GLASS_TINT = "#f7f8fc"   # 毛玻璃底色调（浅）

FONT = "Microsoft YaHei UI"

# DWM / 用户32 常量
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38      # Win11 22H2+
DWMSBT_MAINWINDOW = 2               # Mica
DWMSBT_TRANSIENTWINDOW = 3          # Acrylic（薄）
DWMSBT_TABBEDWINDOW = 4
WCA_ACCENT_POLICY = 19


SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SPI_GETWORKAREA = 0x0030


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def enable_dpi_awareness() -> None:
    """让窗口在高分屏上按真实 DPI 渲染（必须在创建任何 Tk 窗口之前调用）。

    不做这一步，Windows 会把整个窗口位图拉伸，界面发虚、尺寸也不对。
    """
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def work_area() -> tuple:
    """主屏工作区（排除任务栏）：(x, y, width, height)。"""
    if IS_WINDOWS:
        try:
            rect = _Rect()
            if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0,
                                                          ctypes.byref(rect), 0):
                return (rect.left, rect.top,
                        max(320, rect.right - rect.left),
                        max(240, rect.bottom - rect.top))
        except Exception:
            pass
    try:
        user32 = ctypes.windll.user32
        return (0, 0, max(320, user32.GetSystemMetrics(0)),
                max(240, user32.GetSystemMetrics(1)))
    except Exception:
        return (0, 0, 1280, 720)


def virtual_screen() -> tuple:
    """整个虚拟屏幕（多显示器合并）：(x, y, width, height)。"""
    if IS_WINDOWS:
        try:
            user32 = ctypes.windll.user32
            x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
            h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
            if w > 0 and h > 0:
                return (x, y, w, h)
        except Exception:
            pass
    return work_area()


def apply_tk_scaling(root) -> float:
    """按屏幕 DPI 设置 Tk 的 pt→px 缩放，返回实际 DPI 缩放比。"""
    try:
        dpi = root.winfo_fpixels("1i")
        if dpi and dpi > 0:
            root.tk.call("tk", "scaling", dpi / 72.0)   # scaling = 每 point 的像素数
            return max(1.0, dpi / 96.0)
    except Exception:
        pass
    return 1.0


class _AccentPolicy(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int)]


class _WindowCompositionAttribData(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int),
                ("Data", ctypes.POINTER(_AccentPolicy)),
                ("SizeOfData", ctypes.c_size_t)]


def _hwnd(win) -> int:
    """取 Tk 窗口的顶层 HWND（必须是外框，否则毛玻璃不生效）。"""
    if not IS_WINDOWS:
        return 0
    try:
        return int(win.frame(), 16)      # wm frame：外框句柄
    except Exception:
        pass
    try:
        return int(win.winfo_id())
    except Exception:
        return 0


def _tint_to_abgr(color: str, alpha: int) -> int:
    """DWM 的 GradientColor 是 0xAABBGGRR。"""
    color = (color or "#000000").lstrip("#")
    r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    return ((alpha & 0xFF) << 24) | (b << 16) | (g << 8) | r


def is_light_theme() -> bool:
    """系统应用主题是否为浅色（决定能否用跟随系统主题的材质）。"""
    if not (IS_WINDOWS and winreg):
        return True
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return bool(value)
    except Exception:
        return True


def _set_int_attr(dwm, hwnd: int, attr: int, value: int) -> bool:
    try:
        val = ctypes.c_int(value)
        return dwm.DwmSetWindowAttribute(hwnd, ctypes.c_int(attr),
                                         ctypes.byref(val),
                                         ctypes.sizeof(val)) == 0
    except Exception:
        return False


def apply_backdrop(win, tint: str = GLASS_TINT, tint_alpha: int = 235,
                   dark_titlebar: bool = False) -> str:
    """给窗口套上系统背景材质，返回生效的效果名：mica / acrylic / 空字符串。"""
    if not IS_WINDOWS:
        return ""
    try:
        dwm = ctypes.windll.dwmapi
        user = ctypes.windll.user32
    except Exception:
        return ""

    hwnd = _hwnd(win)
    if not hwnd:
        return ""

    # 标题栏明暗跟随界面 + 圆角（失败也不影响后续）
    dark = 1 if dark_titlebar else 0
    if not _set_int_attr(dwm, hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, dark):
        _set_int_attr(dwm, hwnd, 19, dark)
    _set_int_attr(dwm, hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, 2)

    effect = ""
    # 首选带 tint 的亚克力：颜色由我们自己给，浅色界面不会被系统深色主题带跑
    gradient = _tint_to_abgr(tint, tint_alpha)
    for state in (3, 4):                # 3=可控 tint 的亚克力，4=Win11 主机背景
        try:
            accent = _AccentPolicy()
            accent.AccentState = state
            accent.AccentFlags = 2 if state == 3 else 0
            accent.GradientColor = gradient
            data = _WindowCompositionAttribData()
            data.Attribute = WCA_ACCENT_POLICY
            data.Data = ctypes.pointer(accent)
            data.SizeOfData = ctypes.sizeof(accent)
            if user.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)):
                effect = "acrylic"
                break
        except Exception:
            continue

    if not effect:
        # 再退到 Win11 22H2+ 的系统背景材质；但它跟随系统主题，
        # 系统是深色时会把浅色界面衬暗，那种情况下宁可不用
        if is_light_theme():
            for value, name in ((DWMSBT_TRANSIENTWINDOW, "acrylic"),
                                (DWMSBT_MAINWINDOW, "mica"),
                                (DWMSBT_TABBEDWINDOW, "mica")):
                if _set_int_attr(dwm, hwnd, DWMWA_SYSTEMBACKDROP_TYPE, value):
                    effect = name
                    break
    return effect


def clear_backdrop(win) -> None:
    """关闭玻璃效果：恢复系统默认背景。"""
    if not IS_WINDOWS:
        return
    try:
        dwm = ctypes.windll.dwmapi
    except Exception:
        return
    hwnd = _hwnd(win)
    if not hwnd:
        return
    _set_int_attr(dwm, hwnd, DWMWA_SYSTEMBACKDROP_TYPE, 1)   # 1 = 自动/无
    try:
        accent = _AccentPolicy()
        accent.AccentState = 0
        data = _WindowCompositionAttribData()
        data.Attribute = WCA_ACCENT_POLICY
        data.Data = ctypes.pointer(accent)
        data.SizeOfData = ctypes.sizeof(accent)
        ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
    except Exception:
        pass


def set_alpha(win, alpha: float) -> None:
    try:
        win.attributes("-alpha", max(0.5, min(1.0, alpha)))
    except Exception:
        pass


def enable_glass(win, alpha: float = 0.96, tint: str = GLASS_TINT,
                 tint_alpha: int = 235) -> str:
    """开启半透明 + 毛玻璃。返回效果名（空表示系统不支持，已自动降级）。"""
    effect = apply_backdrop(win, tint, tint_alpha=tint_alpha)
    set_alpha(win, alpha)
    if effect:
        try:
            win.attributes("-transparentcolor", GLASS_KEY)
        except Exception:
            pass
    return effect


def enable_translucent(win, alpha: float = 0.96) -> str:
    """不依赖系统材质：浅色实底 + 整体半透明。

    亚克力在某些机器上表现不稳定（背景发暗/发灰）时用这个，一定是浅色。
    """
    disable_glass(win)
    set_alpha(win, alpha)
    return "半透明浅底"


def disable_glass(win) -> None:
    try:
        win.attributes("-transparentcolor", "")
    except Exception:
        pass
    try:
        win.attributes("-alpha", 1.0)
    except Exception:
        pass
    clear_backdrop(win)


def setup_style(root, glass: bool = True) -> None:
    """配置 ttk 主题。glass=True 时窗口底色用关键色（透明，露出毛玻璃）。"""
    bg_key = GLASS_KEY if glass else BG_SOLID
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    style.configure(".", background=bg_key, foreground=FG,
                    fieldbackground=PANEL_SOFT, bordercolor=PANEL_HI,
                    focuscolor=ACCENT, lightcolor=PANEL, darkcolor=PANEL_HI)
    style.configure("TFrame", background=bg_key)
    style.configure("TLabel", background=bg_key, foreground=FG)
    style.configure("Sub.TLabel", background=bg_key, foreground=SUBFG)
    style.configure("Title.TLabel", background=bg_key, foreground=FG,
                    font=(FONT, 13, "bold"))
    # 顶栏：窗口开了透明色后，透明区域的鼠标会穿透、拖不动窗口，
    # 所以顶栏用不透明底色充当「可拖拽的假标题栏」
    style.configure("Head.TFrame", background=PANEL)
    style.configure("Head.TLabel", background=PANEL, foreground=FG,
                    font=(FONT, 13, "bold"))
    style.configure("Head.Sub.TLabel", background=PANEL, foreground=SUBFG)
    style.configure("TLabelframe", background=bg_key, bordercolor=PANEL_HI,
                    relief="flat", borderwidth=1)
    style.configure("TLabelframe.Label", background=bg_key, foreground=ACCENT,
                    font=(FONT, 9, "bold"))

    style.configure("TButton", background=PANEL_SOFT, foreground=FG,
                    borderwidth=0, relief="flat", padding=(12, 6),
                    focuscolor=PANEL_SOFT)
    style.map("TButton",
              background=[("active", PANEL_HI), ("pressed", ACCENT)],
              foreground=[("pressed", "white")])
    style.configure("Accent.TButton", background=ACCENT, foreground="white",
                    borderwidth=0, relief="flat", padding=(12, 6))
    style.map("Accent.TButton", background=[("active", "#ff8fae")])
    style.configure("Tool.TButton", padding=(8, 4))

    style.configure("TCheckbutton", background=bg_key, foreground=FG,
                    indicatorbackground=PANEL_SOFT, borderwidth=0)
    style.map("TCheckbutton", background=[("active", bg_key)],
              indicatorbackground=[("selected", ACCENT)])
    style.configure("TEntry", fieldbackground=PANEL_SOFT, foreground=FG,
                    insertcolor=FG, borderwidth=0, relief="flat", padding=5)
    style.configure("Horizontal.TProgressbar", background=ACCENT,
                    troughcolor=PANEL_SOFT, borderwidth=0, thickness=6)
    style.configure("Vertical.TScrollbar", background=PANEL_SOFT,
                    troughcolor=bg_key, borderwidth=0, arrowsize=12)
    style.map("Vertical.TScrollbar", background=[("active", PANEL_HI)])
    style.configure("Horizontal.TScale", background=bg_key, troughcolor=PANEL_SOFT)

    # 下拉框：黑字浅底，保证可读（含展开后的选项列表）
    style.configure("TCombobox", foreground="black", fieldbackground="#ffffff",
                    background="#ffffff", arrowcolor="black", insertcolor="black",
                    borderwidth=0, padding=4)
    style.map("TCombobox",
              foreground=[("readonly", "black"), ("disabled", "#666666")],
              fieldbackground=[("readonly", "#ffffff")])
    root.option_add("*TCombobox*Listbox.foreground", "black")
    root.option_add("*TCombobox*Listbox.background", "#ffffff")
    root.option_add("*TCombobox*Listbox.selectForeground", "black")
    root.option_add("*TCombobox*Listbox.selectBackground", "#ccd2e0")
    root.option_add("*TCombobox*Listbox.font", (FONT, 9))

    # 可拖动分隔条：加粗一点，方便抓住调整上下两块高度
    style.configure("TPanedwindow", background=bg_key)
    try:
        style.configure("Sash", sashthickness=8, gripcount=8, gripmargin=2,
                        background=PANEL_HI, borderwidth=0)
    except Exception:
        pass

    # 设置窗口用的分页
    style.configure("TNotebook", background=bg_key, borderwidth=0, tabmargins=(2, 4, 2, 0))
    style.configure("TNotebook.Tab", background=PANEL_SOFT, foreground=SUBFG,
                    borderwidth=0, padding=(14, 6))
    style.map("TNotebook.Tab",
              background=[("selected", PANEL_HI)],
              foreground=[("selected", FG)])


def listbox_kwargs() -> dict:
    """列表控件统一外观（tk.Listbox 不走 ttk 样式）。"""
    return dict(bg=PANEL_SOFT, fg=FG, bd=0, highlightthickness=0,
                selectbackground=ACCENT, selectforeground="white",
                activestyle="none", relief="flat",
                font=(FONT, 10))
