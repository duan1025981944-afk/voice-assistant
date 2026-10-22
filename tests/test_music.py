"""网易云点歌：挑歌、挑歌手、挑歌单，以及四种说法各走哪条路。全部用假数据，不联网、不出声。"""
import asyncio

import pytest

from agent.tools import music

EASON = {"id": 2116, "name": "陈奕迅", "alias": ["Eason Chan"]}
JAY = {"id": 6452, "name": "周杰伦", "alias": ["Jay Chou", "Jay"]}


ARTIST_IDS = {"陈奕迅": 2116, "周杰伦": 6452, "Jay": 122200643}


def song(id_, name, *singers, pop=50):
    return {"id": id_, "name": name, "pop": pop,
            "ar": [{"name": s, "alias": [], "id": ARTIST_IDS.get(s, 0)} for s in singers]}


SONGS = {
    "十年 陈奕迅": [song(1, "十年 (Live)", "陈奕迅", pop=60), song(2, "十年", "陈奕迅", pop=100), song(3, "十年", "杨亮", pop=90)],
    "十年": [song(3, "十年", "杨亮", pop=90), song(2, "十年", "陈奕迅", pop=100)],
    # 网易云上没有周杰伦唱的晴天，只有翻唱
    "晴天 周杰伦": [song(10, "晴天(深情版)", "Lucky小爱"), song(11, "晴天 (原唱 周杰伦)", "RyaVocal"),
                   song(12, "晴天", "Jay")],     # 一个也叫 Jay 的账号，不是周杰伦本人
}
ARTISTS = {"陈奕迅": [EASON], "Eason": [EASON], "周杰伦": [JAY], "不存在的歌手": []}
HOT = {
    2116: [song(20, "浮夸", "陈奕迅"), song(21, "好久不见", "陈奕迅")],
    6452: [song(22, "手牵手", "王力宏", "陶喆", "周杰伦", "蔡依林"), song(23, "屋顶", "周杰伦", "温岚")],
}
PLAYLISTS = [
    {"id": 100, "name": "陈奕迅歌曲钢琴曲", "trackCount": 30, "playCount": 99999},       # 钢琴：排除
    {"id": 101, "name": "陈奕迅全部歌曲", "trackCount": 300, "playCount": 500},
    {"id": 102, "name": "陈奕迅全部歌曲（翻唱合集）", "trackCount": 80, "playCount": 9000},  # 翻唱：排除
]
SAMPLES = {101: [song(30 + i, f"歌{i}", "陈奕迅") for i in range(9)] + [song(40, "别人的歌", "王菲")]}


@pytest.fixture
def fake_netease(monkeypatch):
    def fake_search(query, kind, limit=20):
        if kind == "song":
            return SONGS.get(query, [])
        if kind == "artist":
            return ARTISTS.get(query, [])
        return PLAYLISTS
    monkeypatch.setattr(music, "search", fake_search)
    monkeypatch.setattr(music, "artist_hot_songs", lambda artist_id: HOT.get(artist_id, []))
    monkeypatch.setattr(music, "playlist_sample", lambda pid: SAMPLES.get(pid, []))


def plan(mode, song="", artist=""):
    return music.PlayMusicTool()._plan(mode, song, artist)


def test_song_with_artist_prefers_exact_title(fake_netease):
    p = plan("song", "十年", "陈奕迅")
    assert (p.kind, p.item_id) == ("song", 2)          # 不要 Live 版，也不要杨亮唱的


def test_artist_by_english_alias(fake_netease):
    assert plan("song", "十年", "Eason").item_id == 2


def test_song_only_picks_most_popular(fake_netease):
    assert plan("song", "十年").item_id == 2


def test_only_covers_is_reported_not_played(fake_netease):
    out = plan("song", "晴天", "周杰伦")
    assert isinstance(out, str) and "没找到周杰伦唱的《晴天》" in out and "RyaVocal" in out


def test_artist_one(fake_netease):
    p = plan("artist_one", artist="陈奕迅")
    assert p.kind == "song" and p.item_id in (20, 21)


def test_artist_all_uses_a_real_collection(fake_netease):
    p = plan("artist_all", artist="陈奕迅")
    assert (p.kind, p.item_id) == ("playlist", 101)
    assert "网友整理" in p.spoken


def test_unknown_artist(fake_netease):
    assert plan("artist_one", artist="不存在的歌手") == "网易云上没找到歌手「不存在的歌手」"


def test_execute_reports_when_netease_did_not_start(fake_netease, monkeypatch):
    monkeypatch.setattr(music, "ensure_running", lambda: None)
    monkeypatch.setattr(music, "play", lambda kind, item_id, expect: False)
    out = asyncio.run(music.PlayMusicTool().execute("song", "十年", "陈奕迅"))
    assert out.is_error and "没开始放" in out


def test_execute_success(fake_netease, monkeypatch):
    sent = []
    monkeypatch.setattr(music, "ensure_running", lambda: None)
    monkeypatch.setattr(music, "play", lambda kind, item_id, expect: sent.append((kind, item_id, expect)) or True)
    out = asyncio.run(music.PlayMusicTool().execute("song", "十年", "陈奕迅"))
    assert out == "正在播放《十年》- 陈奕迅" and sent == [("song", 2, "十年")]


def test_artist_one_skips_big_choruses(fake_netease):
    assert plan("artist_one", artist="周杰伦").item_id == 23     # 不挑 60 人合唱，挑他是第一演唱者的


def test_resume_does_not_pause_what_is_playing(monkeypatch):
    pressed = []
    monkeypatch.setattr(music, "ensure_running", lambda: None)
    monkeypatch.setattr(music, "now_playing", lambda: "演员 - 薛之谦")
    monkeypatch.setattr(music, "level", lambda seconds=1.2: 0.3)        # 已经在响
    monkeypatch.setattr(music._win32, "press_key", lambda vk: pressed.append(vk))
    assert music.resume() == "网易云已经在放了：演员 - 薛之谦" and not pressed


def test_resume_presses_play_when_silent(monkeypatch):
    levels = iter([0.0, 0.25])                                          # 按之前没声音，按之后有
    monkeypatch.setattr(music, "ensure_running", lambda: None)
    monkeypatch.setattr(music, "now_playing", lambda: "演员 - 薛之谦")
    monkeypatch.setattr(music, "level", lambda seconds=1.2: next(levels))
    monkeypatch.setattr(music._win32, "press_key", lambda vk: None)
    assert music.resume() == "继续播放：演员 - 薛之谦"
