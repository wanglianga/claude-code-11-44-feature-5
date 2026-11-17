"""组帧器单元测试：半帧、多帧、坏校验不吞帧、噪声、截断、连接隔离。"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sender"))

from app.framing import Framer  # noqa: E402
from frames import build_frame  # noqa: E402


def make_conn():
    return SimpleNamespace(batch_id=1, id=1, buffer=b"", offset_base=0)


def statuses(frames):
    return [f.status for f in frames]


def test_half_frame_then_complete():
    conn = make_conn()
    f = build_frame(2002, 1, 1, 80)
    framer = Framer(conn)
    assert framer.feed(f[:7]) == []          # 半帧：只缓冲，不出帧
    out = framer.feed(f[7:])                 # 补齐后出完整帧
    assert statuses(out) == ["ok"]
    assert out[0].start_off == 0 and out[0].end_off == len(f)


def test_multi_frames_one_chunk():
    conn = make_conn()
    blob = b"".join(build_frame(2002, 1, s, 80) for s in range(3))
    out = Framer(conn).feed(blob)
    assert statuses(out) == ["ok", "ok", "ok"]
    assert [f.start_off for f in out] == [0, 16, 32]


def test_bad_crc_does_not_swallow_next_frame():
    conn = make_conn()
    bad = build_frame(2002, 1, 1, 80, corrupt_crc=True)
    good = build_frame(2002, 1, 2, 96)
    out = Framer(conn).feed(bad + good)
    st = statuses(out)
    assert "bad_crc" in st and st[-1] == "ok"
    # 坏帧残余字节按噪声记录，但完整帧被完整解析
    good_frame = out[-1]
    assert good_frame.payload == good


def test_garbage_before_magic_recorded():
    conn = make_conn()
    good = build_frame(2002, 1, 1, 80)
    out = Framer(conn).feed(b"\x00\x11\x22" + good)
    assert statuses(out) == ["garbage", "ok"]
    assert out[0].payload == b"\x00\x11\x22"


def test_truncated_on_close():
    conn = make_conn()
    f = build_frame(2002, 1, 1, 80)
    framer = Framer(conn)
    framer.feed(f[:9])
    out = framer.close()
    assert statuses(out) == ["truncated"]
    assert out[0].end_off - out[0].start_off == 9


def test_connections_do_not_share_fragments():
    c1, c2 = make_conn(), make_conn()
    c2.id = 2
    f = build_frame(2002, 1, 1, 80)
    half1, half2 = f[:7], f[7:]
    Framer(c1).feed(half1)          # 连接 1 收到前半
    out2 = Framer(c2).feed(half2)   # 连接 2 收到后半：不得拼出帧
    assert all(fr.status != "ok" for fr in out2)


def test_byte_offsets_absolute_across_chunks():
    conn = make_conn()
    f1, f2 = build_frame(2002, 1, 1, 80), build_frame(2002, 1, 2, 80)
    framer = Framer(conn)
    framer.feed(f1)
    out = framer.feed(f2)
    assert out[0].start_off == 16 and out[0].end_off == 32
