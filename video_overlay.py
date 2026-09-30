"""抽选动画：全屏独占播放 video.mp4。

依赖 opencv-python 与 pillow 读取/渲染视频；缺失时自动降级（调用方改用列表闪动动画）。
"""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from typing import Callable, Optional

try:
    import cv2
except Exception:                       # 没有 opencv 就退化，不影响其它功能
    cv2 = None

try:
    from PIL import Image, ImageTk
except Exception:
    Image = None
    ImageTk = None

VIDEO_NAMES = ("video.mp4", "shuffle.mp4", "draw.mp4")


def available() -> bool:
    return cv2 is not None and Image is not None


def find_video(*directories: str) -> Optional[str]:
    """在给定目录里依次找抽选用视频（找不到返回 None）。"""
    for directory in directories:
        if not directory:
            continue
        for name in VIDEO_NAMES:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                return path
    return None


class VideoOverlay:
    """全屏置顶窗口，循环播放一段视频；Esc / 点击可提前退出。"""

    def __init__(self, master, path: str,
                 on_close: Optional[Callable[[], None]] = None,
                 on_finish: Optional[Callable[[], None]] = None,
                 loop: bool = True, min_ms: int = 0):
        """
        loop=False 时：视频播完一遍就触发 on_finish 并关闭（抽选动画用）。
        min_ms：短于这个时长的视频会先循环到该时长再结束，避免一闪而过。
        """
        self.path = path
        self.on_close = on_close
        self.on_finish = on_finish
        self.loop = loop
        self.min_ms = min_ms
        self._running = True
        self._job: Optional[str] = None
        self._start = time.time()
        self.frames: "queue.Queue" = queue.Queue(maxsize=2)

        self.top = tk.Toplevel(master)
        self.top.configure(bg="black")
        self.top.attributes("-topmost", True)
        self.top.attributes("-fullscreen", True)
        self.top.bind("<Escape>", lambda e: self.close())
        self.top.bind("<Button-1>", lambda e: self.close())

        self.label = tk.Label(self.top, bg="black")
        self.label.pack(fill="both", expand=True)

        # 尺寸在主线程取一次并缓存：读帧线程不能再碰 Tk 对象
        self._width = self.top.winfo_screenwidth()
        self._height = self.top.winfo_screenheight()

        self.cap = cv2.VideoCapture(path)
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 0
        if not fps or fps > 120:
            fps = 30.0
        self.delay = max(16, min(100, int(1000 / fps)))

        threading.Thread(target=self._reader, daemon=True).start()
        self._tick()

    # ---------- 内部 ----------
    def _reader(self) -> None:
        """后台读帧；只生成 PIL 图像，Tk 对象一律在主线程创建。"""
        while self._running:
            try:
                if not self.cap.isOpened():
                    break
                ok, frame = self.cap.read()
                if not ok or frame is None:              # 播到结尾
                    elapsed = (time.time() - self._start) * 1000
                    if self.loop or elapsed < self.min_ms:
                        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    try:
                        self.top.after(0, self._finish)   # 回主线程收尾
                    except Exception:
                        self._finish()
                    break
                image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                image = self._fit(image, max(1, self._width), max(1, self._height))
                try:
                    self.frames.put_nowait(image)
                except queue.Full:
                    pass
            except Exception:
                break

    @staticmethod
    def _fit(image, width: int, height: int):
        iw, ih = image.size
        scale = min(width / iw, height / ih)
        if scale <= 0:
            return image
        return image.resize((max(1, int(iw * scale)), max(1, int(ih * scale))))

    def _tick(self) -> None:
        if not self._running:
            return
        try:
            image = self.frames.get_nowait()
        except queue.Empty:
            image = None
        if image is not None:
            photo = ImageTk.PhotoImage(image)
            self.label.configure(image=photo)
            self.label.image = photo          # 防止被 GC
        self._job = self.top.after(self.delay, self._tick)

    # ---------- 外部 ----------
    def _finish(self) -> None:
        """视频自然播完：关闭并通知 on_finish（不触发 on_close）。"""
        if not self._running:
            return
        self._running = False
        self._release()
        if self.on_finish:
            self.on_finish()

    def _release(self) -> None:
        if self._job:
            try:
                self.top.after_cancel(self._job)
            except Exception:
                pass
        try:
            self.cap.release()
        except Exception:
            pass
        try:
            self.top.destroy()
        except Exception:
            pass

    def close(self) -> None:
        was_running = self._running
        self._running = False
        self._release()
        if was_running and self.on_close:
            self.on_close()
