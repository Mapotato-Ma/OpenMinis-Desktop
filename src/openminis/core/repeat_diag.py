"""Repetition diagnostics for the streaming/agent path.

# PORT-FIX(desktop diagnostics): NOT from upstream. Added to chase a field
report — "大模型输出会一直重复一大段内容" (the model keeps repeating a large
block of text). The repetition can originate in three different places and
they need different fixes, so before guessing we log *where* it happens:

* inside a single stream (the model degenerates into a loop mid-response, or a
  compatible relay double-sends deltas) → :class:`StreamRepeatWatch`;
* across agent turns (the tool loop re-runs and the model re-emits the same
  answer every round) → :func:`turn_repeat_ratio`.

Everything here is pure + cheap (bounded-length string ops), logs under the
``openminis`` tree so it lands in both ``minis.log`` and the windowed build's
``desktop.log``, and never raises into the hot path.
"""

from __future__ import annotations

from .logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "StreamRepeatWatch",
    "turn_repeat_ratio",
    "longest_repeated_tail",
]


def longest_repeated_tail(text: str, *, min_len: int = 40) -> int:
    """Length of the longest block that appears at least twice, tail-anchored.

    Cheap heuristic tuned for "the same paragraph keeps coming back": we only
    check whether the **most recent** ``min_len``-plus block already occurred
    earlier in ``text``. Returns the repeated length (0 = no repeat found).

    Not a full longest-repeated-substring — that would be O(n²). This answers
    the one question we care about: is the stream re-emitting what it already
    said?
    """
    n = len(text)
    if n < min_len * 2:
        return 0
    # Grow a window at the tail and see if it exists earlier in the string.
    best = 0
    # cap the probe so a megabyte stream can't turn this quadratic
    max_probe = min(n // 2, 4000)
    lo, hi = min_len, max_probe
    while lo <= hi:
        mid = (lo + hi) // 2
        tail = text[n - mid:]
        # search only the region that could hold an earlier copy
        if text.find(tail, 0, n - mid) != -1:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


class StreamRepeatWatch:
    """Accumulate visible stream text and warn once a big repeat appears.

    Fed the visible-text deltas of a single response. The first time the
    accumulated text contains a repeated tail block at/over ``threshold``
    characters it logs ONE warning (with previews), so a user's log pinpoints
    the exact response that ran away without spamming a line per delta.
    """

    def __init__(self, *, label: str, threshold: int = 240) -> None:
        self._label = label
        self._threshold = threshold
        self._buf: list[str] = []
        self._len = 0
        self._fired = False

    def feed(self, text: str) -> None:
        if not text or self._fired:
            return
        self._buf.append(text)
        self._len += len(text)
        # Only probe once the buffer is large enough to *hold* a repeat, and
        # rate-limit the (bounded but non-trivial) scan to whole-KB growth.
        if self._len < self._threshold * 2:
            return
        joined = "".join(self._buf)
        rep = longest_repeated_tail(joined, min_len=self._threshold)
        if rep >= self._threshold:
            self._fired = True
            sample = joined[-rep:]
            logger.warning(
                "[repeat-diag] %s: stream is repeating a %d-char block "
                "(total=%d). This is the '一直重复一大段' symptom. "
                "Repeated head: %r",
                self._label, rep, self._len, sample[:120],
            )

    def total_chars(self) -> int:
        return self._len

    def fired(self) -> bool:
        return self._fired


def turn_repeat_ratio(current: str, previous: list[str]) -> float:
    """How much this turn's text duplicates any earlier turn (0..1).

    Returns the best overlap ratio against any previous turn — 1.0 means an
    exact repeat. Used to log a warning when the agent loop keeps re-emitting
    the same answer round after round (a different bug from an intra-stream
    loop, and the reason we log both).
    """
    cur = current.strip()
    if not cur:
        return 0.0
    best = 0.0
    for prev in previous:
        p = prev.strip()
        if not p:
            continue
        if cur == p:
            return 1.0
        # containment either way, normalised by the longer string
        shorter, longer = (cur, p) if len(cur) <= len(p) else (p, cur)
        if len(shorter) >= 40 and shorter in longer:
            best = max(best, len(shorter) / len(longer))
    return best
