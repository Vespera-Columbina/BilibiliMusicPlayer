"""通过 Chrome DevTools Protocol 控制 Edge/Chrome 播放 B 站视频。

为什么不用内嵌浏览器控件：PyQt/CEF 等构建通常不含 H.264/AAC 专有编解码，
B 站视频会「有画面没声音」或无法播放。直接驱动系统里真实的 Edge/Chrome
最稳，也便于用户在该窗口里登录 B 站账号（解锁高码率）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from typing import Callable, List, Optional

import websocket  # websocket-client

JS_STATE = """
(() => {
  const v = document.querySelector('video');
  if (!v) return null;
  return {
    t: v.currentTime || 0,
    d: (isFinite(v.duration) ? v.duration : 0) || 0,
    paused: v.paused,
    ended: v.ended,
    ready: v.readyState || 0,
    muted: v.muted
  };
})()
"""

JS_PLAY = """
(() => {
  const v = document.querySelector('video');
  if (!v) return false;
  v.muted = false;
  const p = v.play();
  if (p && p.catch) p.catch(() => {});
  if (v.paused) {
    const btn = document.querySelector(
      '.bpx-player-ctrl-play, .bpx-player-ctrl-btn.bpx-player-ctrl-play, ' +
      '.bilibili-player-video-btn-start');
    if (btn) { btn.click(); return true; }
  }
  return !v.paused;
})()
"""

JS_URL = "location.href"

# 注意：JS 里全是花括号，不能用 str.format，用 %s 拼接
JS_SEEK = "(() => { const v = document.querySelector('video'); if (v) { v.currentTime = %s; } return 1; })()"

JS_ENV = "({hidden: document.hidden, href: location.href})"

JS_PAUSE = """
(() => {
  const v = document.querySelector('video');
  if (!v) return false;
  v.pause();
  return true;
})()
"""


def find_browser() -> Optional[str]:
    """在常见位置 / 注册表里寻找 Edge 或 Chrome。"""
    candidates: List[str] = []
    try:
        for reg_key in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"):
            for root in ("HKLM", "HKCU"):
                out = subprocess.run(["reg", "query", f"{root}\\{reg_key}", "/ve"],
                                     capture_output=True, text=True, timeout=5).stdout
                for line in out.splitlines():
                    if "REG_SZ" in line:
                        path = line.split("REG_SZ", 1)[1].strip()
                        if path and os.path.exists(path):
                            candidates.append(path)
    except Exception:
        pass

    candidates += [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    for name in ("msedge.exe", "chrome.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


class CDPBrowser:
    """极简 CDP 客户端：启动浏览器 + 打开/监控一个标签页。"""

    def __init__(self, exe: Optional[str] = None, port: int = 9222,
                 profile_dir: Optional[str] = None, headless: bool = False):
        self.exe = exe or find_browser()
        self.port = port
        self.headless = headless
        self.profile_dir = profile_dir or os.path.join(tempfile.gettempdir(),
                                                       "bili_player_profile")
        self._proc: Optional[subprocess.Popen] = None
        self._ws = None
        self._ever_connected = False   # 用于区分「启动中」与「连接后断开」
        self._lock = threading.RLock()
        self._msg_id = 0
        self._session = ""
        self._target = ""

    # ---------- 生命周期 ----------
    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def ever_connected(self) -> bool:
        return self._ever_connected

    @property
    def running(self) -> bool:
        """WebSocket 仍连着，且（若由本程序启动）进程未退出。"""
        if self._ws is None:
            return False
        return self._proc is None or self._proc.poll() is None

    def is_alive(self) -> bool:
        """调试端口可达且受控标签页仍在——用于判断窗口是否被用户关掉。"""
        if not self._target:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list",
                                        timeout=2) as resp:
                pages = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return False
        return any(p.get("id") == self._target for p in pages)

    def start(self, timeout: int = 40) -> None:
        if self.running:
            return
        if not self.exe or not os.path.exists(self.exe):
            raise RuntimeError("未找到 Edge/Chrome，请在设置里手动指定浏览器路径")

        os.makedirs(self.profile_dir, exist_ok=True)
        args = [
            self.exe,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-features=Translate,OptimizationHints",
            "--remote-allow-origins=*",
            "--window-size=1100,720",
        ]
        if self.headless:
            args.append("--headless=new")
        args += ["--new-window", "about:blank"]
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)

        version = self._wait_endpoint(timeout)
        # 端口上若已有一个用同一 profile 的实例（例如上次残留），新进程会因单实例锁退出，
        # 此时复用那个仍在运行的实例即可
        if self._proc.poll() is not None:
            self._proc = None
        self._ws = websocket.create_connection(version["webSocketDebuggerUrl"],
                                               timeout=8, suppress_origin=True)
        self._ws.settimeout(1)
        self._ever_connected = True

        target_id = self._first_page_target()
        if not target_id:
            created = self.send("Target.createTarget", {"url": "about:blank"})
            target_id = created.get("targetId", "")
        self._target = target_id
        attached = self.send("Target.attachToTarget",
                             {"targetId": target_id, "flatten": True})
        self._session = attached.get("sessionId", "")
        self.send("Page.enable")
        self.send("Runtime.enable")

    def shutdown(self) -> None:
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        self._session = ""
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._proc = None

    # ---------- HTTP 调试端口 ----------
    def _wait_endpoint(self, timeout: int) -> dict:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/version", timeout=2) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                last = exc
                time.sleep(0.4)
        raise RuntimeError(f"无法连接浏览器调试端口({self.port})：{last}")

    def _first_page_target(self) -> str:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list",
                                        timeout=3) as resp:
                pages = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return ""
        for page in pages:
            if page.get("type") == "page":
                return page.get("id", "")
        return ""

    # ---------- CDP 通信 ----------
    def send(self, method: str, params: Optional[dict] = None, timeout: float = 10,
             use_session: bool = True) -> dict:
        with self._lock:
            if not self._ws:
                raise RuntimeError("浏览器未连接")
            self._msg_id += 1
            msg_id = self._msg_id
            payload = {"id": msg_id, "method": method, "params": params or {}}
            if use_session and self._session:
                payload["sessionId"] = self._session
            self._ws.send(json.dumps(payload))

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw = self._ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception as exc:
                    raise RuntimeError(f"与浏览器通信中断：{exc}")
                if not raw:
                    break
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue          # 偶发残帧/粘包，跳过继续等自己的响应
                if data.get("id") == msg_id:
                    if "error" in data:
                        raise RuntimeError(str(data["error"]))
                    return data.get("result", {})
            raise TimeoutError(f"CDP 调用超时：{method}")

    def navigate(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})

    def bring_to_front(self) -> bool:
        """把受控标签页切回前台（用户切走标签页/最小化时用它抢回焦点）。"""
        try:
            self.send("Page.bringToFront")
            return True
        except Exception:
            return False

    def reopen_target(self) -> bool:
        """标签页被叉掉但浏览器还在时，重新开一个标签页并接管（比重启浏览器快）。"""
        try:
            self._session = ""          # 旧 session 随标签页一起失效了
            created = self.send("Target.createTarget", {"url": "about:blank"},
                                use_session=False)
            target_id = created.get("targetId", "")
            if not target_id:
                return False
            attached = self.send("Target.attachToTarget",
                                 {"targetId": target_id, "flatten": True},
                                 use_session=False)
            self._target = target_id
            self._session = attached.get("sessionId", "")
            try:
                self.send("Page.enable")
                self.send("Runtime.enable")
            except Exception:
                pass
            return True
        except Exception:
            return False

    def evaluate(self, expression: str):
        result = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": False,
        })
        if result.get("exceptionDetails"):
            raise RuntimeError(str(result["exceptionDetails"]))
        return (result.get("result") or {}).get("value")


class BiliPlayer:
    """播放 B 站视频，并在播放结束时回调。

    直接打开视频原页面 https://www.bilibili.com/video/{bvid}（不使用内嵌播放器），
    好处是完整保留原页面的清晰度切换、弹幕、评论，且登录态与会员限制正常生效。
    """

    PAGE = "https://www.bilibili.com/video/{bvid}"

    def __init__(self,
                 on_state: Optional[Callable[[dict], None]] = None,
                 on_ended: Optional[Callable[[], None]] = None,
                 on_error: Optional[Callable[[str], None]] = None,
                 on_closed: Optional[Callable[[], None]] = None,
                 port: int = 9222):
        self.on_state = on_state
        self.on_ended = on_ended
        self.on_error = on_error
        self.on_closed = on_closed
        self.browser: Optional[CDPBrowser] = None
        self._port = port
        self._exe: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._bvid = ""
        self._ended_fired = False
        self._loaded = False
        self._missing = 0
        self._kicks = 0
        self._active = False       # 停止后置 False，避免用户关窗后被重新拉起
        self._closed_fired = False
        self._last_time = 0.0      # 关闭前的播放进度，恢复时接着放
        self._seek_to = 0.0
        self._seek_tries = 0
        self._suspended = False   # 打开登录页等场景下临时停止监控
        self._user_paused = False # 用户主动暂停（区别于自动播放被拦截）
        self._fg_miss = 0         # 页面不在当前视频页的连续次数
        self._bring_at = 0.0      # 上次抢焦点的时间（做冷却，避免一直弹窗）
        self._fg_tick = 0

    # ---------- 启动 / 关闭 ----------
    def start(self, exe: Optional[str] = None, headless: bool = False) -> None:
        with self._lock:
            if exe:
                self._exe = exe
            if self.browser and self.browser.running:
                return
            self.browser = CDPBrowser(exe=exe or self._exe, port=self._port,
                                      headless=headless)
            self.browser.start()
            self._stop.clear()
            if not (self._thread and self._thread.is_alive()):
                self._thread = threading.Thread(target=self._watch_loop, daemon=True)
                self._thread.start()

    @property
    def is_running(self) -> bool:
        return bool(self.browser and self.browser.running)

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def current_bvid(self) -> str:
        return self._bvid

    def set_port(self, port: int) -> None:
        """修改调试端口（下次启动浏览器时生效）。"""
        with self._lock:
            self._port = int(port)
            if self.browser:
                self.browser.port = self._port

    def quit(self) -> None:
        self._stop.set()
        if self.browser:
            self.browser.shutdown()
            self.browser = None

    # ---------- 播放控制 ----------
    def play(self, bvid: str, start_at: float = 0.0) -> None:
        if not self.browser or not self.browser.running:
            self.start()
        with self._lock:
            self._bvid = bvid
            self._active = True
            self._ended_fired = False
            self._closed_fired = False
            self._missing = 0
            self._kicks = 0
            self._loaded = False
            self._seek_to = max(0.0, float(start_at or 0))
            self._seek_tries = 0
            self._suspended = False
            self._user_paused = False   # 换歌/恢复播放都从「未暂停」开始
            self._last_time = 0.0       # 旧歌的进度不能带到新歌上
            self._fg_miss = 0
            self._fg_tick = 0
            url = self.PAGE.format(bvid=bvid)
            if self._seek_to > 2:
                url += f"?t={int(self._seek_to)}"
            try:
                self.browser.navigate(url)
            except Exception as exc:
                self._fail(f"打开视频失败：{exc}")

    def suspend(self) -> None:
        """临时停止播放监控（例如打开登录页时），避免被误判为播放异常。"""
        with self._lock:
            self._suspended = True

    def pause(self) -> None:
        """用户主动暂停：监控线程会尊重这个状态，不再把它当成「自动播放被拦截」。"""
        with self._lock:
            self._user_paused = True
        self._js(JS_PAUSE)

    def resume(self) -> None:
        with self._lock:
            self._user_paused = False
        self._js(JS_PLAY)

    @property
    def is_paused(self) -> bool:
        return self._user_paused

    def stop(self) -> None:
        with self._lock:
            self._active = False       # 用户主动停止：此后关窗不再自动拉起
            self._bvid = ""
            self._ended_fired = True
            self._closed_fired = True
            self._seek_to = 0.0
            self._user_paused = False
            if self.browser and self.browser.running:
                try:
                    self.browser.navigate("about:blank")
                except Exception:
                    pass

    def bring_front(self) -> bool:
        """手动把播放页面拉回前台。"""
        if not self.browser or not self.browser.running:
            return False
        return self.browser.bring_to_front()

    def restore_page(self) -> None:
        """手动把页面强制导航回当前视频（用户逛到别的页面时用）。"""
        with self._lock:
            bvid = self._bvid
        if bvid:
            self._restore_page(bvid)

    def recover(self) -> None:
        """窗口被关掉后重新拉起浏览器，并从上次进度继续播放当前视频。"""
        with self._lock:
            bvid = self._bvid
            active = self._active
            resume_at = self._last_time
        if not bvid or not active:
            return
        try:
            # 只是标签页被叉掉、浏览器还活着时，重开一个标签页即可，不必重启浏览器
            if self.browser and self.browser.running and self.browser.reopen_target():
                self.play(bvid, start_at=resume_at)
                return
            if self.browser:
                try:
                    self.browser.shutdown()
                except Exception:
                    pass
            self.browser = None
            self.start()
            self.play(bvid, start_at=resume_at)
        finally:
            # 恢复失败时允许再次尝试（重试次数上限由上层控制）
            with self._lock:
                self._closed_fired = False

    def state(self) -> Optional[dict]:
        with self._lock:
            if not self.browser or not self.browser.running or not self._bvid:
                return None
            try:
                return self.browser.evaluate(JS_STATE)
            except Exception:
                return None

    # ---------- 内部 ----------
    def _js(self, expression: str):
        with self._lock:
            if not self.browser or not self.browser.running:
                return None
            try:
                return self.browser.evaluate(expression)
            except Exception:
                return None

    def _fail(self, message: str) -> None:
        if self.on_error:
            self.on_error(message)

    def _enforce_foreground(self, bvid: str) -> None:
        """强制保持「当前视频页在前台」：

        * 页面被导航到别处（点了推荐视频/逛到首页）→ 强制导航回当前视频，并续播进度
        * 页面不在前台（切走标签页、窗口被最小化）→ 抢回焦点
        """
        with self._lock:
            if self._ended_fired:      # 刚播完、正准备切下一首，别打架
                return
            self._fg_tick += 1
            check = self._fg_tick % 3 == 0   # 每 3 秒检查一次即可
        if not check:
            return

        try:
            env = self.browser.evaluate(JS_ENV)
        except Exception:
            return
        if not env:
            return

        href = env.get("href") or ""
        if href and bvid not in href:
            self._fg_miss += 1
            if self._fg_miss >= 2:     # 连续两次不匹配才处理，避开换歌瞬间的跳转
                self._fg_miss = 0
                self._restore_page(bvid)
                return
        else:
            self._fg_miss = 0

        if env.get("hidden") and time.time() - self._bring_at > 10:
            self._bring_at = time.time()
            self.browser.bring_to_front()

    def _restore_page(self, bvid: str) -> None:
        """导航回当前视频页，并从记录的进度继续。"""
        with self._lock:
            self._seek_to = self._last_time
            self._seek_tries = 0
            self._loaded = False
            self._kicks = 0
            self._missing = 0
            url = self.PAGE.format(bvid=bvid)
            if self._seek_to > 2:
                url += f"?t={int(self._seek_to)}"
        try:
            self.browser.navigate(url)
        except Exception:
            pass

    def _watch_loop(self) -> None:
        missed = 0
        while not self._stop.is_set():
            try:
                if self._check_closed():
                    missed = 0
                    time.sleep(1.0)
                    continue
                self._tick()
            except Exception as exc:  # 保证监控线程不因异常退出
                missed += 1
                if missed == 1:
                    self._fail(f"播放监控异常：{exc}")
            time.sleep(1.0)

    def _check_closed(self) -> bool:
        """用户关掉了窗口/浏览器：通知上层自动恢复（停止状态下不恢复）。"""
        with self._lock:
            browser = self.browser
            active = self._active
            fired = self._closed_fired
        if browser is None or not active or fired:
            return False
        if not browser.connected and not browser.ever_connected:
            return False            # 还没连上过，属于启动中
        if browser.running and browser.is_alive():
            return False
        with self._lock:
            self._closed_fired = True
        if self.on_closed:
            self.on_closed()
        return True

    def _tick(self) -> None:
        if not self.browser or not self.browser.running:
            return
        with self._lock:
            bvid = self._bvid
            suspended = self._suspended
        if not bvid or suspended:
            return

        # 必须放在读取视频状态之前：页面被导航走后就没有 <video> 了
        self._enforce_foreground(bvid)

        try:
            st = self.browser.evaluate(JS_STATE)
        except Exception:
            return

        if not st:
            self._missing += 1
            if self._missing == 30:
                self._fail("页面里没有检测到视频（可能需要登录、地区限制或视频已失效）")
            return
        self._missing = 0

        if not self._loaded:
            self._loaded = True
            self._kicks = 0

        # 用户按了暂停：即使页面被强制拉回/重新加载，也保持暂停，不再自动续播
        with self._lock:
            user_paused = self._user_paused
        if user_paused:
            if not st.get("paused"):
                self._js(JS_PAUSE)
            if self.on_state:
                self.on_state(st)
            return

        # 恢复播放时先跳回关闭前的进度（首次无条件跳，之后偏差超过 1 秒再补跳）
        if self._seek_to > 2:
            if self._seek_tries == 0 or (self._seek_tries < 5 and
                                         abs((st.get("t") or 0) - self._seek_to) > 1):
                self._seek_tries += 1
                self._js(JS_SEEK % self._seek_to)
            else:
                self._seek_to = 0.0
                self._seek_tries = 0

        # 自动播放兜底：B 站原页面初次进入时不会自动播放，这里点一下播放按钮
        if st.get("paused") and not st.get("ended") and self._kicks < 10:
            self._kicks += 1
            self._js(JS_PLAY)
            return

        if self.on_state:
            self.on_state(st)
        self._last_time = st.get("t") or 0.0

        duration = st.get("d") or 0
        current = st.get("t") or 0
        finished = st.get("ended") or (duration > 5 and current >= duration - 0.6)
        if finished and not self._ended_fired:
            self._ended_fired = True
            self._js(JS_PAUSE)  # 阻止页面自动跳到推荐视频继续出声
            if self.on_ended:
                self.on_ended()
            return

        # 播完后页面可能自动跳转到推荐视频，此时静音暂停，避免和下一首串音
        if self._ended_fired:
            try:
                href = self.browser.evaluate(JS_URL) or ""
            except Exception:
                href = ""
            if href and bvid not in href:
                self._js(JS_PAUSE)
