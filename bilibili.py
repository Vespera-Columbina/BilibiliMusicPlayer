"""Bilibili 搜索接口封装（含 WBI 签名）与「最佳匹配」挑选。"""

from __future__ import annotations

import hashlib
import html
import math
import random
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)

# WBI mixin key 置换表（B站公开算法）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

TAG_RE = re.compile(r"<[^>]+>")
NOISE_WORDS = ("翻唱", "cover", "reaction", "react", "混剪", "剪辑", "卡点", "舞蹈",
               "舞蹈教学", "教程", "直播", "录屏", "合集", "伴奏", "纯伴奏", "remix",
               "reaction", "速弹", "教学")


def strip_tags(text: str) -> str:
    return html.unescape(TAG_RE.sub("", text or "")).strip()


def parse_duration(text) -> int:
    if isinstance(text, (int, float)):
        return int(text)
    parts = str(text).strip().split(":")
    try:
        nums = [int(float(p)) for p in parts]
    except ValueError:
        return 0
    total = 0
    for n in nums:
        total = total * 60 + n
    return total


def fmt_duration(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_count(n: int) -> str:
    n = int(n or 0)
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def normalize(text: str) -> str:
    """归一化：去掉空格/标点并转小写，用于标题匹配。"""
    return re.sub(r"[\s\W_]+", "", (text or "")).lower()


@dataclass
class Video:
    bvid: str
    title: str
    author: str
    duration: int
    play: int = 0
    danmaku: int = 0
    pubdate: int = 0
    description: str = ""

    @property
    def page_url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}"

    def label(self) -> str:
        return (f"[{fmt_duration(self.duration)}] {self.title}   "
                f"UP:{self.author}   ▶{fmt_count(self.play)}")


class BiliSearcher:
    """B站视频搜索。自动完成风控 Cookie 引导与 WBI 签名。"""

    def __init__(self, timeout: int = 12, cookie: str = ""):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self._mk = ""
        self._ready = False
        if cookie:
            self.set_cookie(cookie)

    def set_cookie(self, cookie: str) -> None:
        """cookie 形如 'SESSDATA=xxx; bili_jct=xxx'，可提升搜索成功率与清晰度。"""
        for part in cookie.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                self.session.cookies.set(k, v, domain=".bilibili.com")
        self._ready = False

    # ---------- 初始化：Cookie + WBI 密钥 ----------
    def _bootstrap(self) -> None:
        if self._ready:
            return
        try:
            self.session.get("https://www.bilibili.com/", timeout=self.timeout)
        except requests.RequestException:
            pass
        try:
            r = self.session.get("https://api.bilibili.com/x/frontend/finger/spi",
                                 timeout=self.timeout).json()
            data = r.get("data") or {}
            if data.get("b_3"):
                self.session.cookies.set("buvid3", data["b_3"], domain=".bilibili.com")
            if data.get("b_4"):
                self.session.cookies.set("buvid4", data["b_4"], domain=".bilibili.com")
        except Exception:
            pass
        # b_lsid 由页面 JS 生成，WBI 搜索接口缺了它会返回空结果，这里自己造一个
        if not self.session.cookies.get("b_lsid"):
            self.session.cookies.set("b_lsid", self._random_lsid(), domain=".bilibili.com")
        try:
            r = self.session.get("https://api.bilibili.com/x/web-interface/nav",
                                 timeout=self.timeout).json()
            img = ((r.get("data") or {}).get("wbi_img") or {})
            img_key = self._key_from_url(img.get("img_url", ""))
            sub_key = self._key_from_url(img.get("sub_url", ""))
            if img_key and sub_key:
                self._mk = self._mixin_key(img_key, sub_key)
        except Exception:
            pass
        self._ready = True

    @staticmethod
    def _random_lsid() -> str:
        return f"{random.getrandbits(32):08X}_{random.getrandbits(40):010X}"

    @staticmethod
    def _key_from_url(url: str) -> str:
        if not url:
            return ""
        name = url.rsplit("/", 1)[-1]
        return name.split(".")[0]

    @staticmethod
    def _mixin_key(img_key: str, sub_key: str) -> str:
        raw = img_key + sub_key
        return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB if i < len(raw))[:32]

    def _wbi_sign(self, params: dict) -> dict:
        params = dict(params)
        params["wts"] = int(time.time())
        filtered = {
            k: "".join(ch for ch in str(params[k]) if ch not in "!'()*")
            for k in sorted(params)
        }
        query = urllib.parse.urlencode(filtered)
        params["w_rid"] = hashlib.md5((query + self._mk).encode("utf-8")).hexdigest()
        return params

    # ---------- 搜索 ----------
    def search(self, keyword: str, page: int = 1, order: str = "totalrank") -> List[Video]:
        self._bootstrap()
        base = {"search_type": "video", "keyword": keyword, "page": page, "order": order}
        url = "https://api.bilibili.com/x/web-interface/search/type"
        params = base
        if self._mk:
            url = "https://api.bilibili.com/x/web-interface/wbi/search/type"
            params = self._wbi_sign(base)

        headers = {"Referer": "https://search.bilibili.com/"}
        resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        data = resp.json()

        if data.get("code") != 0 and self._mk:  # 签名失败时退回旧接口
            resp = self.session.get(
                "https://api.bilibili.com/x/web-interface/search/type",
                params=base, headers=headers, timeout=self.timeout)
            data = resp.json()

        items = ((data.get("data") or {}).get("result") or [])
        if not items:  # WBI 接口偶发返回空结果，回退旧接口
            resp = self.session.get(
                "https://api.bilibili.com/x/web-interface/search/type",
                params=base, headers=headers, timeout=self.timeout)
            data = resp.json()

        code = data.get("code")
        if code != 0:
            msg = data.get("message") or "未知错误"
            if code in (-412, -799, 412):
                msg = f"{msg}（触发风控，建议在设置里填入 B 站 Cookie 后重试）"
            raise RuntimeError(f"搜索失败(code={code}): {msg}")

        items = ((data.get("data") or {}).get("result") or [])
        videos: List[Video] = []
        for it in items:
            bvid = it.get("bvid") or ""
            if not bvid:
                continue
            videos.append(Video(
                bvid=bvid,
                title=strip_tags(it.get("title", "")),
                author=strip_tags(it.get("author", "")),
                duration=parse_duration(it.get("duration", 0)),
                play=int(it.get("play") or 0),
                danmaku=int(it.get("video_review") or it.get("danmaku") or 0),
                pubdate=int(it.get("pubdate") or 0),
                description=strip_tags(it.get("description", ""))[:120],
            ))
        return videos


def score_video(v: Video, name: str, artist: str, min_dur: int = 60, max_dur: int = 900) -> float:
    """给候选视频打分，越高越可能是「这首歌」。"""
    title_n = normalize(v.title)
    name_n = normalize(name)
    artist_n = normalize(artist)
    score = 0.0

    if name_n:
        if name_n in title_n:
            score += 60
        elif len(name_n) >= 4 and name_n[:4] in title_n:
            score += 20
        elif len(name_n) >= 2 and name_n[:2] in title_n:
            score += 5
        else:
            score -= 30
    if artist_n:
        if artist_n in title_n:
            score += 25
        elif artist_n in normalize(v.author):
            score += 12

    dur = v.duration
    if min_dur <= dur <= max_dur:
        score += 20
    elif dur and dur < min_dur:
        score -= (min_dur - dur) / 5.0
    elif dur and dur > max_dur:
        score -= (dur - max_dur) / 60.0

    low = normalize(v.title)
    for word in NOISE_WORDS:
        if word in low:
            score -= 15
            break

    if v.play > 0:
        score += min(20.0, math.log10(v.play + 1) * 4)
    score -= min(15.0, len(v.title) * 0.15)
    return score


def pick_best(videos: List[Video], name: str, artist: str,
              min_dur: int = 60, max_dur: int = 900) -> Optional[Video]:
    if not videos:
        return None
    return max(videos, key=lambda v: score_video(v, name, artist, min_dur, max_dur))
