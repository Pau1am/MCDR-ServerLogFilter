#!/usr/bin/env python3
"""Compare two released versions of this plugin, head to head.

Built for the question "did the last release get slower?", which should be answered with
numbers rather than a feeling. Every measurement runs in its own subprocess with that
version first on ``sys.path``, so the two never share a module object.

::

    # accepts .mcdr artifacts or directories containing server_log_filter/
    python benchmarks/bench_versions.py old.mcdr new.mcdr

Needs ``mcdreforged`` importable for the measurement child (see tests/README.md for the
``.testlibs`` setup); the parent does not import the plugin at all.
"""

import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# The child talks to us over stdout; keep it dependency-free and self-contained.
CHILD = r'''
import json, sys, time

sys.path.insert(0, sys.argv[1])

import server_log_filter as slf

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


class Logger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


class Info:
    __slots__ = ("content", "raw_content", "action_flag")
    def __init__(self, content):
        self.content = content
        self.raw_content = content
        self.action_flag = None


class Srv:
    logger = Logger()


def rules_for(n):
    patterns = [slf.DEFAULT_PATTERN] + ["noise-marker-%03d-\\w+" % i for i in range(1, n)]
    return slf._build_rules(Srv(), patterns)


def per_line(n, batch=400, batches=7, warm=300):
    """Microseconds per line, reported as the **minimum** batch.

    The minimum is the right estimator here: interference (another process, a GC pause,
    a frequency change) can only ever make a batch slower, never faster, so the fastest
    batch is the closest thing to the code's true cost. Averaging instead lets one hiccup
    move the number by tens of percent — with a single rule the work is under a
    microsecond, and the spread between averages was measured at ~20%.
    """
    f = slf.ServerLogFilter(rules_for(n), Logger(), False)
    infos = [Info(line) for line in LINES]
    for _ in range(warm):
        for info in infos:
            f.filter_server_info(info)
    best = None
    for _ in range(batches):
        start = time.perf_counter()
        for _ in range(batch):
            for info in infos:
                f.filter_server_info(info)
        cost = (time.perf_counter() - start) / (batch * len(LINES)) * 1e6
        best = cost if best is None else min(best, cost)
    return best


def load_cost(n, rounds=200, batches=7):
    """Milliseconds to compile + probe every rule, again as the minimum batch."""
    patterns = [slf.DEFAULT_PATTERN] + ["noise-marker-%03d-\\w+" % i for i in range(1, n)]
    srv = Srv()
    for _ in range(20):                       # warm the regex cache
        slf._build_rules(srv, patterns)
    best = None
    for _ in range(batches):
        start = time.perf_counter()
        for _ in range(rounds):
            slf._build_rules(srv, patterns)
        cost = (time.perf_counter() - start) / rounds * 1000      # milliseconds
        best = cost if best is None else min(best, cost)
    return best


out = {
    "per_line": {str(n): per_line(n) for n in (1, 5, 25, 50)},
    "load_ms": {str(n): load_cost(n) for n in (1, 25, 50)},
    "import_ms": None,
}
print(json.dumps(out))
'''

# Import cost has to be measured from a fresh interpreter, before anything else runs.
IMPORT_CHILD = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
start = time.perf_counter()
import server_log_filter   # noqa: F401
print("%.3f" % ((time.perf_counter() - start) * 1000))
'''


def _stage(target: str, into: Path) -> Path:
    """Return a directory that has ``server_log_filter/`` directly inside it."""
    path = Path(target).resolve()
    if path.is_dir():
        if (path / "server_log_filter").is_dir():
            return path
        nested = path / "server_log_filter"
        if nested.is_dir():
            return path
        raise SystemExit("no server_log_filter/ under {}".format(path))
    if path.suffix == ".mcdr":
        with zipfile.ZipFile(path) as zf:
            zf.extractall(into)
        return into
    raise SystemExit("expected a .mcdr file or a directory: {}".format(path))


def measure(root: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    result = json.loads(
        subprocess.run([sys.executable, "-c", CHILD, str(root)], env=env,
                       capture_output=True, text=True, check=True).stdout
    )
    result["import_ms"] = float(
        subprocess.run([sys.executable, "-c", IMPORT_CHILD, str(root)], env=env,
                       capture_output=True, text=True, check=True).stdout
    )
    return result


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        roots = [
            _stage(sys.argv[1], Path(tmp) / "a"),
            _stage(sys.argv[2], Path(tmp) / "b"),
        ]
        labels = [Path(a).name for a in sys.argv[1:]]
        results = [measure(root) for root in roots]

    def sizes(target):
        """(artifact bytes, __init__.py bytes) read from the source argument itself.

        Read from the original file rather than the extracted copy: the temporary
        directory is already gone by now, and the artifact size is the interesting one
        anyway.
        """
        path = Path(target).resolve()
        if path.suffix == ".mcdr":
            with zipfile.ZipFile(path) as zf:
                return path.stat().st_size, zf.getinfo(
                    "server_log_filter/__init__.py").file_size
        return None, (path / "server_log_filter" / "__init__.py").stat().st_size

    print("=" * 72)
    print("filter cost per line (lower is better)")
    print("=" * 72)
    print("  {:<8} {:>14} {:>14} {:>12}".format("rules", labels[0], labels[1], "delta"))
    for n in ("1", "5", "25", "50"):
        a, b = results[0]["per_line"][n], results[1]["per_line"][n]
        print("  {:<8} {:>11.3f} µs {:>11.3f} µs {:>+11.1f}%".format(
            n, a, b, (b - a) / a * 100))

    print()
    print("=" * 72)
    print("load cost — compile + backtracking probe (lower is better)")
    print("=" * 72)
    print("  {:<8} {:>14} {:>14} {:>12}".format("rules", labels[0], labels[1], "delta"))
    for n in ("1", "25", "50"):
        a, b = results[0]["load_ms"][n], results[1]["load_ms"][n]
        print("  {:<8} {:>11.3f} ms {:>11.3f} ms {:>+11.1f}%".format(
            n, a, b, (b - a) / a * 100))

    print()
    print("=" * 72)
    print("cold import of the plugin module (lower is better)")
    print("=" * 72)
    a, b = results[0]["import_ms"], results[1]["import_ms"]
    print("  {}: {:.3f} ms      {}: {:.3f} ms      {:+.1f}%".format(
        labels[0], a, labels[1], b, (b - a) / a * 100))

    print()
    print("=" * 72)
    print("what the two versions cost in bytes")
    print("=" * 72)
    for label, target in zip(labels, sys.argv[1:]):
        artifact, code = sizes(target)
        shown = "{:,} B".format(artifact) if artifact is not None else "n/a"
        print("  {:<28} artifact {:>10}   __init__.py {:>6} B".format(label, shown, code))

    print()
    print("Reference for scale: MCDR itself spends ~2.1 µs parsing one line")
    print("(benchmarks/bench_filter.py measures that on the same machine).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
