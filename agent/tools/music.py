"""网易云点歌：play_music。

──────────────────────────────────────────────────────────────
一句"播放陈奕迅的十年"是怎么放出来的
──────────────────────────────────────────────────────────────
1. 搜：用网易云网页版的搜索接口（不登录）把"歌名 + 歌手"换成歌曲 ID
2. 发：os.startfile("orpheus://" + base64('{"type":"song","id":"…","cmd":"play"}'))
   这是网页上"在客户端中打开"用的链接，网易云安装时注册了 orpheus:// 协议。
   客户端只认四种：单曲 song、专辑 album、歌单 playlist、电台节目 program——没有"歌手"
3. 验：网易云主窗口的标题就是"歌名 - 歌手"，标题变过去了才算真的在放

──────────────────────────────────────────────────────────────
实测踩过的坑（网易云 3.1.31）
──────────────────────────────────────────────────────────────
- 网易云没开时，指令会被它丢掉：要先启动，等主窗口出来、再等几秒，才发
- 有些歌网易云上只有翻唱（比如周杰伦的大部分歌）：指定了歌手就只认这个歌手唱的，
  没有就如实说，不拿翻唱冒充
- "某歌手的所有歌"客户端不支持，只能找网友整理的"全部歌曲"歌单：排除钢琴/伴奏/翻唱类，
  再抽查前 10 首大部分是不是本人唱的
- 不登录查不到你的账号能不能播（版权、会员），只能发出去之后看标题有没有变

──────────────────────────────────────────────────────────────
风险
──────────────────────────────────────────────────────────────
搜索接口和 orpheus:// 指令都不是官方公开的，网易云更新后可能失效，失效时工具会如实报错。
发给网易云的只有歌名和歌手名；不读你的账号、Cookie、歌单和听歌记录。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import subprocess
import time
import urllib.parse
import urllib.request
import winreg
from dataclasses import dataclass

from agent.tools import _win32
from agent.tools.apps import normalize
from agent.tools.base import Tool, ToolResult
from rapidfuzz import fuzz

API = "https://music.163.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://music.163.com/"}
PROCESS = "cloudmusic.exe"
NOT_SONG_WINDOWS = {"桌面歌词", "迷你播放器", "网易云音乐"}
# 歌单名里带这些词的多半不是原唱合集
NOT_ORIGINAL = re.compile(r"钢琴|伴奏|翻唱|cover|纯音乐|dj|铃声|八音盒|吉他|二胡|古筝|remix|女声版|男声版|抖音", re.I)
COLLECTION = re.compile(r"全部|所有|合集|全集|热门|精选|歌曲|大全|经典")

START_TIMEOUT = 25        # 网易云冷启动，等主窗口出来最多几秒
SETTLE_AFTER_START = 4    # 主窗口出来后还要等它内部准备好，不然指令会被丢掉
VERIFY_TIMEOUT = 8        # 发完指令等标题变过去最多几秒


# ── 搜索（网易云网页接口）──────────────────────────────────

def _api(path: str, params: dict | None = None) -> dict:
    body = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(f"{API}/{path}", data=body, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 200:
        raise RuntimeError(f"网易云接口返回 {data.get('code')}")
    return data


def search(query: str, kind: str, limit: int = 20) -> list[dict]:
    """kind：song / artist / playlist。"""
    type_id, key = {"song": (1, "songs"), "artist": (100, "artists"), "playlist": (1000, "playlists")}[kind]
    result = _api("cloudsearch/pc", {"s": query, "type": type_id, "limit": limit}).get("result") or {}
    return result.get(key) or []


def artist_hot_songs(artist_id: int) -> list[dict]:
    return _api(f"artist/top/song?id={artist_id}").get("songs") or []


def playlist_sample(playlist_id: int) -> list[dict]:
    """不登录只能拿到歌单前 10 首，够抽查了。"""
    return (_api(f"v6/playlist/detail?id={playlist_id}").get("playlist") or {}).get("tracks") or []


# ── 挑选（纯函数，测试里直接喂假数据）──────────────────────

def clean_title(name: str) -> str:
    """去掉括号里的版本说明再比较："十年 (Live)" → "十年"。

    >>> clean_title("晴天 (钢琴版) [原唱: 周杰伦]")
    '晴天'
    """
    return re.sub(r"\s*[（(\[【].*?[)）\]】]", "", name).strip()


def artist_names(song: dict) -> list[str]:
    return [a.get("name") or "" for a in (song.get("ar") or song.get("artists") or [])]


def name_key(name: str) -> str:
    """比较歌手名用：只忽略大小写和空格。不像 normalize 那样去标点——
    网易云上有"陈奕迅."这种名字，去掉点就和本人一样了。

    >>> name_key("Eason Chan") == name_key("eason chan"), name_key("陈奕迅.") == name_key("陈奕迅")
    (True, False)
    """
    return re.sub(r"\s+", "", name.lower())


def _names_of(artist: dict) -> set[str]:
    """一个歌手的所有叫法（名字、别名、译名），用 name_key 规整过。"""
    names = [artist.get("name") or "", *(artist.get("alias") or []), *(artist.get("tns") or [])]
    if artist.get("trans"):
        names.append(artist["trans"])
    return {name_key(n) for n in names if n}


def sings(song: dict, names: set[str], artist_id: int | None = None) -> bool:
    """这首歌是不是这个歌手唱的。

    知道歌手编号就只认编号：周杰伦的别名里有"Jay"，按名字比，
    网易云上一个也叫"Jay"的账号上传的翻唱就会混进来。
    """
    artists = song.get("ar") or song.get("artists") or []
    if artist_id is not None:
        return any(a.get("id") == artist_id for a in artists)
    return any(_names_of(a) & names for a in artists)


def leads(song: dict, artist_id: int) -> bool:
    """这个歌手是第一演唱者，而且不是一大群人的合唱（排除 60 人合唱的《手牵手》这种）。"""
    artists = song.get("ar") or []
    return bool(artists) and artists[0].get("id") == artist_id and len(artists) <= 3


def title_score(found: str, wanted: str) -> float:
    a, b = normalize(clean_title(found)), normalize(clean_title(wanted))
    if not a or not b:
        return 0
    if a == b:
        return 100
    if b in a or a in b:
        return 85
    return fuzz.ratio(a, b)


def pick_artist(results: list[dict], wanted: str) -> dict | None:
    """先找名字（或别名、译名）完全一样的；没有再找包含的（"Eason"→别名"Eason Chan"）。
    搜索结果本身按相关度排好了，同样对得上取靠前的。都对不上就不猜。

    >>> eason = {"id": 2116, "name": "陈奕迅", "alias": ["Eason Chan"]}
    >>> pick_artist([{"id": 1, "name": "陈奕迅.", "alias": []}, eason], "陈奕迅")["id"]
    2116
    >>> pick_artist([eason, {"id": 3, "name": "Eason亦儿", "alias": []}], "Eason")["id"]
    2116
    >>> pick_artist([{"id": 4, "name": "寻找陈奕迅", "alias": []}], "王菲") is None
    True
    """
    target = name_key(wanted)
    for a in results:
        if target in _names_of(a):
            return a
    if len(target) >= 3:
        for a in results:
            if any(target in n for n in _names_of(a)):
                return a
    return None


def artist_aliases(artist: dict | None, wanted: str) -> set[str]:
    return {name_key(wanted)} | (_names_of(artist) if artist else set())


def pick_song(results: list[dict], title: str, singers: set[str] | None,
              artist_id: int | None = None) -> dict | None:
    """歌名最像的；指定了歌手就只要这个歌手唱的。
    同样像时：歌名一字不差的 > 不是钢琴伴奏翻唱版的 > 更热门的。"""
    best, best_key = None, None
    for s in results:
        name = s.get("name") or ""
        score = title_score(name, title)
        if score < 80 or (singers and not sings(s, singers, artist_id)):
            continue
        exact = normalize(name) == normalize(title)
        is_cover = bool(NOT_ORIGINAL.search(name))
        key = (score, exact, not is_cover, s.get("pop") or 0)
        if best_key is None or key > best_key:
            best, best_key = s, key
    return best


def pick_playlist(playlists: list[dict], singers: set[str], sample,
                  artist_id: int | None = None) -> tuple[dict, list[dict]] | None:
    """找"某歌手全部歌曲"这类歌单：名字里有歌手、像合集、不是钢琴伴奏翻唱，
    再按播放量从高到低抽查前 10 首，大部分（≥60%）是本人唱的才要。sample 是取前 10 首的函数。"""
    candidates = [
        p for p in playlists
        if any(n in name_key(p.get("name") or "") for n in singers)
        and COLLECTION.search(p.get("name") or "")
        and not NOT_ORIGINAL.search(p.get("name") or "")
        and 15 <= (p.get("trackCount") or 0) <= 2000
    ]
    candidates.sort(key=lambda p: p.get("playCount") or 0, reverse=True)
    for p in candidates[:4]:
        tracks = sample(p["id"])
        if tracks and sum(sings(t, singers, artist_id) for t in tracks) / len(tracks) >= 0.6:
            return p, tracks
    return None


def describe(song: dict) -> str:
    return f"《{song.get('name')}》- {'/'.join(artist_names(song))}"


# ── 网易云客户端 ────────────────────────────────────────────

def orpheus_url(kind: str, item_id: int) -> str:
    """给网易云客户端的播放链接：orpheus:// 加 base64 编码的 JSON。

    >>> orpheus_url("song", 66842)
    'orpheus://eyJ0eXBlIjoic29uZyIsImlkIjoiNjY4NDIiLCJjbWQiOiJwbGF5In0='
    """
    payload = json.dumps({"type": kind, "id": str(item_id), "cmd": "play"}, separators=(",", ":"))
    return "orpheus://" + base64.b64encode(payload.encode()).decode()


def now_playing() -> str:
    """网易云主窗口标题（"歌名 - 歌手"）。网易云没开返回空串。"""
    for w in _win32.list_windows():
        if w.process.lower() == PROCESS and w.title and w.title not in NOT_SONG_WINDOWS \
                and not w.title.startswith("GDI+") and " - " in w.title:
            return w.title
    return ""


def client_exe() -> str:
    """从 orpheus:// 协议的注册信息里找网易云装在哪。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"orpheus\shell\open\command") as key:
            command = winreg.QueryValue(key, None)
    except OSError:
        return ""
    m = re.match(r'\s*"([^"]+)"', command) or re.match(r"\s*(\S+)", command)
    return m.group(1) if m else ""


def running() -> bool:
    return any(w.process.lower() == PROCESS for w in _win32.list_windows())


def ensure_running() -> str | None:
    """网易云没开就先打开，等它准备好。返回 None 表示好了，否则是出错原因。"""
    if running() and now_playing():
        return None
    exe = client_exe()
    if not exe or not os.path.exists(exe):
        return "没找到网易云音乐客户端（orpheus:// 协议没注册）"
    subprocess.Popen([exe], creationflags=subprocess.DETACHED_PROCESS)
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if now_playing() or any(w.process.lower() == PROCESS and w.shown for w in _win32.list_windows()):
            time.sleep(SETTLE_AFTER_START)
            return None
        time.sleep(0.5)
    return "网易云启动太慢，没等到它的窗口"


def play(kind: str, item_id: int, expect: str | None) -> bool:
    """发播放指令，等标题变成 expect（歌名）。expect 为 None 时只要标题变了就算。没变就再发一次。"""
    before = now_playing()
    for _attempt in range(2):
        os.startfile(orpheus_url(kind, item_id))
        deadline = time.monotonic() + VERIFY_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(0.4)
            title = now_playing()
            song_part = title.rsplit(" - ", 1)[0]
            if expect is not None and title_score(song_part, expect) >= 85:
                return True
            if expect is None and title and title != before:
                return True
    return expect is not None and title_score(before.rsplit(" - ", 1)[0], expect) >= 85


def level(seconds: float = 1.2) -> float:
    """网易云这一会儿的最大输出电平（0 = 没出声）。比看"音频会话是否活跃"准：暂停时会话也还活跃。"""
    import comtypes
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    peak = 0.0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for session in AudioUtilities.GetAllSessions():
            if session.Process and session.Process.name().lower() == PROCESS:
                peak = max(peak, session._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue())
        time.sleep(0.1)
    return peak


def resume() -> str:
    """没说放什么：打开网易云，接着放上次的歌。已经在响就什么都不做（再按一下反而会暂停）。"""
    problem = ensure_running()
    if problem:
        return ToolResult.error(problem)
    title = now_playing()
    if level() > 0.001:
        return f"网易云已经在放了：{title}"
    _win32.press_key(0xB3)                  # 播放/暂停键
    if level(2.0) > 0.001:
        return f"继续播放：{now_playing() or title}"
    return ToolResult.error("按了播放，网易云没有出声（可能播放列表是空的），问用户想听什么")


# ── 工具 ──────────────────────────────────────────────────

@dataclass
class Plan:
    kind: str          # song / playlist
    item_id: int
    expect: str | None
    spoken: str        # 成功后告诉模型的话


class PlayMusicTool(Tool):
    name = "play_music"
    description = (
        "用网易云音乐放歌。mode 怎么选：\n"
        "song：放指定的歌，如“播放晴天”（只填 song）、“播放陈奕迅的十年”（song 和 artist 都填）；\n"
        "artist_one：随便放一首某歌手的歌，如“放一首陈奕迅的歌”；\n"
        "artist_all：放某歌手的很多歌，如“从头播放陈奕迅的所有歌”（找“全部歌曲”歌单来放）；\n"
        "resume：没说放什么，如“放首歌”“来点音乐”“继续放歌”——打开网易云接着放上次的歌。\n"
        "暂停、下一首、上一首用 media，不用这个。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["song", "artist_one", "artist_all", "resume"]},
            "song": {"type": "string", "description": "歌名，按用户说的填，如 十年"},
            "artist": {"type": "string", "description": "歌手名，如 陈奕迅；用户没说就不填"},
        },
        "required": ["mode"],
    }
    needs_confirm = False

    async def execute(self, mode: str, song: str = "", artist: str = "") -> str:
        return await asyncio.to_thread(self._run, mode, song.strip(), artist.strip())

    def _run(self, mode: str, song: str, artist: str) -> str:
        if mode == "resume":
            return resume()
        try:
            plan = self._plan(mode, song, artist)
        except (OSError, RuntimeError, ValueError) as exc:
            return ToolResult.error(f"连不上网易云的搜索：{exc}")
        if isinstance(plan, str):
            return ToolResult.error(plan)
        problem = ensure_running()
        if problem:
            return ToolResult.error(problem)
        if play(plan.kind, plan.item_id, plan.expect):
            return plan.spoken
        return ToolResult.error(
            f"已经让网易云放{plan.spoken.removeprefix('正在播放')}，但它没开始放"
            "（可能这首没有版权、需要会员，或者网易云更新后指令变了）"
        )

    def _plan(self, mode: str, song: str, artist: str) -> Plan | str:
        singer = None
        if artist:
            singer = pick_artist(search(artist, "artist", 5), artist)
            if singer is None and mode != "song":
                return f"网易云上没找到歌手「{artist}」"
        singers = artist_aliases(singer, artist) if artist else None
        artist_id = singer["id"] if singer else None

        if mode == "song":
            if not song:
                return "要放哪首歌？song 不能为空"
            # 用网易云上的正式名字去搜（说"Eason"时搜"陈奕迅"），找不到歌手就用原话
            who = singer["name"] if singer else artist
            results = search(f"{song} {who}".strip(), "song")
            found = pick_song(results, song, singers, artist_id)
            if found is None:
                others = "、".join(describe(s) for s in results[:3])
                who = f"{artist}唱的" if artist else ""
                return f"网易云上没找到{who}《{song}》。" + (f"搜到的有：{others}" if others else "")
            return Plan("song", found["id"], found["name"], f"正在播放{describe(found)}")

        hot = artist_hot_songs(singer["id"])
        if mode == "artist_one":
            own = [s for s in hot if leads(s, artist_id)] or [s for s in hot if sings(s, singers, artist_id)] or hot
            if not own:
                return f"网易云上没有{singer['name']}的歌"
            found = random.choice(own[:20])
            return Plan("song", found["id"], found["name"], f"正在播放{describe(found)}")

        if mode == "artist_all":
            playlists = search(f"{singer['name']} 全部歌曲", "playlist") + search(f"{singer['name']} 热门歌曲", "playlist")
            picked = pick_playlist(playlists, singers, playlist_sample, artist_id)
            if picked is None:
                if not hot:
                    return f"网易云上没有{singer['name']}的歌"
                found = hot[0]
                return Plan("song", found["id"], found["name"],
                            f"没找到合适的{singer['name']}全部歌曲歌单，先放一首最热门的：{describe(found)}")
            playlist, tracks = picked
            return Plan("playlist", playlist["id"], None,
                        f"正在播放歌单《{playlist['name']}》（{playlist.get('trackCount')} 首，网友整理的）。"
                        "网易云不能直接按歌手放全部，用的是歌单；网易云要是开着随机播放，就不是从第一首开始")
        return f"不认识的 mode：{mode}"
