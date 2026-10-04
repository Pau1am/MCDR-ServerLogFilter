#!/usr/bin/env python3
"""Measure what Server Log Filter actually costs, and why the regex guard exists.

The zero-idle-rule reminder is a *config hygiene* feature, not a speed-up: this
script is here so that claim can be checked rather than believed. It measures

1. the cost of filtering a line, and how it compares with the work MCDR already
   does on that same line, and
2. the cost of a catastrophic-backtracking pattern, which is the one thing that
   can genuinely stall the server.

Run from the repository root::

    python benchmarks/bench_filter.py

Needs ``mcdreforged`` importable (see tests/README.md for the ``.testlibs``
setup). Without it, section 2 is skipped.
"""

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server_log_filter import DEFAULT_PATTERN, Rule, ServerLogFilter  # noqa: E402


class _SilentLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


LINES = [
    "Player Pau1am standing on air - force-sending blocks below",
    "[Server thread/INFO]: Pau1am joined the game",
    "[Server thread/INFO]: <Pau1am> hello everyone",
    "[Server thread/INFO]: Pau1am moved too quickly! 1.5,2.5,3.5",
    "[Server thread/INFO]: Saving and pausing game...",
    "[Server thread/INFO]: Preparing spawn area: 42%",
    "[Server thread/INFO]: Pau1am lost connection: Disconnected",
    "[Server thread/INFO]: Time elapsed: 1234 ms",
    "System chat: /advancement (grant|revoke) <targets> <advancement>",
    '[Server thread/INFO]: Done (0.648s)! For help, type "help"',
]


class _Info:
    __slots__ = ("content", "raw_content", "action_flag")

    def __init__(self, content):
        self.content = content
        self.raw_content = content
        self.action_flag = None


def make_rules(n):
    patterns = [DEFAULT_PATTERN] + ["noise-marker-{:03d}-\\w+".format(i) for i in range(1, n)]
    return [Rule(p) for p in patterns]


def time_filter(rule_count, rounds=3000):
    """Microseconds per line, end to end through the real filter."""
    f = ServerLogFilter(make_rules(rule_count), _SilentLogger(), False)
    infos = [_Info(line) for line in LINES]
    for _ in range(200):  # warm up
        for info in infos:
            f.filter_server_info(info)

    start = time.perf_counter()
    for _ in range(rounds):
        for info in infos:
            f.filter_server_info(info)
    return (time.perf_counter() - start) / (rounds * len(LINES)) * 1e6


def section1():
    print("=" * 68)
    print("1. Cost of filtering a line")
    print("=" * 68)
    for n in (1, 5, 10, 25):
        us = time_filter(n)
        # at 1000 lines/s, `us` microseconds per line => us milliseconds per second
        cpu = us * 1000 / 1e6 * 100
        print("  {:>2} rule(s): {:>7.3f} µs/line   ({:.4f}% of one core at 1000 lines/s)".format(
            n, us, cpu))

    us = time_filter(1)
    print()
    for rate, label in ((1, "idle server (1 line/s)"), (10, "small server (10 lines/s)"),
                        (100, "busy server (100 lines/s)"), (1000, "extreme burst (1000 lines/s)")):
        per_hour = us * rate * 3600 / 1e6
        print("  {:26} {:>8.4f} s/hour of filtering".format(label, per_hour))


def section2():
    print()
    print("=" * 68)
    print("2. How that compares with MCDR's own per-line work")
    print("=" * 68)
    try:
        from mcdreforged.handler.impl.vanilla_handler import VanillaHandler
    except ImportError:
        print("  (mcdreforged not importable — skipping)")
        return

    handler = VanillaHandler()
    line = '[12:00:00] [Server thread/INFO]: Player Pau1am standing on air - force-sending blocks below'
    n = 20000
    start = time.perf_counter()
    for _ in range(n):
        handler.parse_server_stdout(line)
    mcdr_us = (time.perf_counter() - start) / n * 1e6

    us = time_filter(1)
    print("  MCDR parsing one line : {:>7.3f} µs".format(mcdr_us))
    print("  this plugin filtering : {:>7.3f} µs  ({:.1f}% of MCDR's own cost)".format(
        us, us / mcdr_us * 100))


def section3():
    print()
    print("=" * 68)
    print("3. Catastrophic backtracking — the one real risk")
    print("=" * 68)
    cases = [
        (r"(a+)+$", "a" * 22 + "!", "nested quantifier"),
        (r"(a+)+$", "a" * 26 + "!", "nested quantifier (longer)"),
        (r"^(a|a)*$", "a" * 24 + "!", "alternation"),
        (r"(\w+\s?)*$", "a " * 16 + "!", r"(\w+\s?)*"),
    ]
    for pattern, subject, desc in cases:
        rx = re.compile(pattern)
        start = time.perf_counter()
        rx.search(subject)
        us = (time.perf_counter() - start) * 1e6
        print("  {:<28} {:<6} input  {:>14,.0f} µs/line".format(desc, pattern, us))
    print()
    normal = time_filter(1)
    print("  Normal filtering is {:.1f} µs/line — a bad rule can be millions of times worse,".format(normal))
    print("  which is why validate_patterns refuses them at load time.")


def section4():
    print()
    print("=" * 68)
    print("4. Probe calibration (what validate_patterns checks against)")
    print("=" * 68)
    subjects = ["a" * 22, "a" * 22 + "!"]
    dangerous = [r"(a+)+$", r"^(a|a)*$", r"(\w+\s?)*$", r"(a*)*b"]
    realistic = [
        DEFAULT_PATTERN,
        r"Player \w+ .*",
        r"joined the game|left the game",
        r"moved too quickly! [\d\.,]+",
        r"^\S+ has too many items",
        r"(?:Rejecting|Ignoring) \w+",
        r"Ignoring chat session from \S+ due to missing Services public key",
        r"Preparing spawn area: \d+%",
    ]

    def worst(pattern):
        """Slowest single-subject time for this pattern, in microseconds."""
        rx = re.compile(pattern)
        slowest = 0.0
        for subject in subjects:
            start = time.perf_counter()
            rx.search(subject)
            slowest = max(slowest, (time.perf_counter() - start) * 1e6)
        return slowest

    print("  dangerous patterns (must exceed the 25 ms budget):")
    for p in dangerous:
        us = worst(p)
        print("    {:<16} {:>12,.0f} µs   {}".format(p, us, "refused" if us > 25000 else "!! MISSED"))

    print("  realistic patterns (must stay far below it):")
    worst_good = max(worst(p) for p in realistic)
    print("    worst of {} patterns: {:.1f} µs  -> headroom {:.0f}x".format(
        len(realistic), worst_good, 25000 / worst_good))


if __name__ == "__main__":
    section1()
    section2()
    section3()
    section4()
    print()
    print("See tests/README.md for how these numbers are used.")
