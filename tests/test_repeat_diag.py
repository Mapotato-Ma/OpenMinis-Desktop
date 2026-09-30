"""[T-repeat-diag] 重复输出诊断器（追现场「大模型输出一直重复一大段」用）。

只是日志探针，不改变行为 —— 这里锁死它的判据，免得以后误改。
"""

from __future__ import annotations

from openminis.core.repeat_diag import (
    StreamRepeatWatch,
    longest_repeated_tail,
    turn_repeat_ratio,
)


def test_longest_repeated_tail_finds_a_repeat():
    block = "这是一段被重复的内容，长度足够越过阈值门槛用于检测。" * 2
    assert longest_repeated_tail(block, min_len=20) >= 20


def test_longest_repeated_tail_ignores_non_repeating_text():
    text = "".join(chr(0x4E00 + i) for i in range(300))  # 300 个不同汉字
    assert longest_repeated_tail(text, min_len=40) == 0


def test_stream_watch_fires_on_runaway_repeat():
    w = StreamRepeatWatch(label="unit", threshold=60)
    chunk = "模型陷入循环反复输出同一段话，这段话不断重复出现在流里。"
    for _ in range(20):
        w.feed(chunk)
    assert w.fired() is True


def test_stream_watch_stays_quiet_on_normal_output():
    w = StreamRepeatWatch(label="unit", threshold=60)
    # 一段较长、内部不自我重复的正文
    for i in range(20):
        w.feed(f"第{i}段落讲了完全不同的事情，包含独特的名词{i}和描述{i*7}。")
    assert w.fired() is False


def test_turn_repeat_ratio_exact_and_none():
    assert turn_repeat_ratio("abcdefghij" * 6, ["abcdefghij" * 6]) == 1.0
    assert turn_repeat_ratio("完全不一样而且够长的一段新内容在这里", ["旧的一段也不算短的话"]) == 0.0


def test_turn_repeat_ratio_containment():
    prev = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # 48 chars
    cur = prev + "些许新增的尾巴"
    r = turn_repeat_ratio(cur, [prev])
    assert 0.5 < r < 1.0
