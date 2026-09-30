"""歌单数据模型与导入/导出。

支持的歌单格式：
  * .txt / .lst   每行一首，形如「歌名 - 歌手」（也允许只写歌名）
  * .m3u / .m3u8  标准播放列表（会读取 #EXTINF 里的信息）
  * .csv          第一列歌名，第二列歌手（可选）
  * .json         ["歌名", ...] 或 [{"name": ..., "artist": ...}, ...]
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, asdict
from typing import List, Optional

SEPARATORS = (" - ", " – ", " — ", " -- ", "-")
ORDER_RE = re.compile(r"^\d+\s*[.、)）]\s*")


@dataclass
class Song:
    name: str = ""
    artist: str = ""
    bvid: str = ""          # 已选定的 BV 号（空表示交给自动匹配）
    picked: str = ""        # 已选定的视频标题

    @property
    def display(self) -> str:
        return f"{self.name} - {self.artist}" if self.artist else self.name

    @property
    def keyword(self) -> str:
        return f"{self.name} {self.artist}".strip()

    def to_line(self) -> str:
        return f"{self.name} - {self.artist}" if self.artist else self.name


def parse_line(line: str) -> Optional[Song]:
    """把一行文本解析成 Song，默认格式为「歌名 - 歌手」。"""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    text = ORDER_RE.sub("", text).strip()
    for sep in SEPARATORS:
        if sep in text:
            left, right = text.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return Song(name=left, artist=right)
    return Song(name=text)


def _read_text(path: str) -> List[str]:
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read().splitlines()
        except UnicodeDecodeError:
            continue
    return []


def _load_m3u(lines: List[str]) -> List[Song]:
    songs: List[Song] = []
    pending_meta = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            pending_meta = line.split(",", 1)[1].strip() if "," in line else ""
            continue
        if line.startswith("#"):
            continue
        song = parse_line(pending_meta) if pending_meta else None
        if song is None:
            base = os.path.splitext(os.path.basename(line))[0]
            song = parse_line(base)
        if song and song.name:
            songs.append(song)
        pending_meta = ""
    return songs


def _load_csv(path: str) -> List[Song]:
    songs: List[Song] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    for row in rows:
        if not row or not any(c.strip() for c in row):
            continue
        cells = [c.strip() for c in row]
        if cells[0].lower() in ("name", "title", "歌曲", "歌曲名", "歌名", "标题"):
            continue
        artist = cells[1] if len(cells) > 1 else ""
        if len(cells) > 2 and not artist:
            artist = cells[2]
        songs.append(Song(name=cells[0], artist=artist))
    return songs


def _load_json(path: str) -> List[Song]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("songs") or data.get("playlist") or data.get("list") or []
    songs: List[Song] = []
    for item in data:
        if isinstance(item, str):
            song = parse_line(item)
        elif isinstance(item, dict):
            song = Song(
                name=str(item.get("name") or item.get("title") or item.get("song") or "").strip(),
                artist=str(item.get("artist") or item.get("singer") or item.get("author") or "").strip(),
            )
        else:
            continue
        if song.name:
            songs.append(song)
    return songs


def load_playlist(path: str) -> List[Song]:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".m3u", ".m3u8"):
        return _load_m3u(_read_text(path))
    if ext == ".csv":
        return _load_csv(path)
    if ext == ".json":
        return _load_json(path)
    songs: List[Song] = []
    for line in _read_text(path):
        song = parse_line(line)
        if song and song.name:
            songs.append(song)
    return songs


def save_playlist(path: str, songs: List[Song]) -> None:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump([asdict(s) for s in songs], f, ensure_ascii=False, indent=2)
        return
    if ext == ".csv":
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "artist"])
            for s in songs:
                writer.writerow([s.name, s.artist])
        return
    with open(path, "w", encoding="utf-8") as f:
        for s in songs:
            f.write(s.to_line() + "\n")
