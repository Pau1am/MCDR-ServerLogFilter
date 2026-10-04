#!/usr/bin/env python3
"""Mutation check: deliberately break the plugin and confirm the tests notice.

A passing suite proves nothing on its own. This script introduces a handful of
targeted defects, one at a time, and requires the relevant tests to fail. If any
mutation survives, the test that is supposed to guard that behaviour is
decorative and needs strengthening.

Run from the repository root::

    python tools/mutation_check.py

Needs the ``.testlibs`` setup (see tests/README.md).
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = "server_log_filter/__init__.py"

WARN_BLOCK = """    if (
        not _config.warn_about_stale_rules
        or _state is None
        or _log_filter is None
        or _config.stale_rule_threshold <= 0
    ):
        return

    stale = _collect_stale(_state, list(_log_filter.rules), _config.stale_rule_threshold)
    if stale:
        _emit_stale_warning(server, stale, _config.stale_rule_threshold)
"""

ON_START_TAIL = """    if _log_filter is not None:
        _log_filter.reset_counters()
"""


def disable_warning(src):
    return src.replace(WARN_BLOCK, "    return  # mutation\n")


def threshold_off_by_one(src):
    return src.replace(
        "if entry is not None and entry.zero_streak >= threshold:",
        "if entry is not None and entry.zero_streak > threshold:  # mutation",
    )


def drop_startup_guard(src):
    return src.replace(
        "if _session_reached_startup and _state is not None:",
        "if _state is not None:  # mutation",
    )


def break_carry_over(src):
    return src.replace(
        "        if previous is None:\n            return 0\n",
        "        if previous is None:\n            return 0\n        return 0  # mutation\n",
    )


def drop_session_flag_carry_over(src):
    return src.replace(
        '    if getattr(prev_module, "_session_reached_startup", False):\n'
        "        _session_reached_startup = True\n",
        "    # mutation: running-session flag not carried over\n",
    )


def disable_probe(src):
    return src.replace(
        "        validate=_config.validate_patterns,",
        "        validate=False,  # mutation",
    )


def warn_before_startup(src):
    if WARN_BLOCK not in src or ON_START_TAIL not in src:
        return src
    src = src.replace(WARN_BLOCK, "    pass  # mutation\n")
    early = ON_START_TAIL + """    # mutation: warn before the server finished starting
    if (
        _config is not None
        and _state is not None
        and _log_filter is not None
        and _config.warn_about_stale_rules
        and _config.stale_rule_threshold > 0
    ):
        _m = _collect_stale(_state, list(_log_filter.rules), _config.stale_rule_threshold)
        if _m:
            _emit_stale_warning(server, _m, _config.stale_rule_threshold)
"""
    return src.replace(ON_START_TAIL, early, 1)


MUTATIONS = [
    ("idle-rule warning disabled", disable_warning,
     ["tests/test_plugin.py", "-k", "idle or stale or streak"]),
    ("threshold changed from >= to >", threshold_off_by_one,
     ["tests/test_plugin.py", "-k", "threshold or idle_rule"]),
    ("'session finished starting' guard removed", drop_startup_guard,
     ["tests/test_plugin.py", "-k", "never_reached_startup"]),
    ("reload counter carry-over broken", break_carry_over,
     ["tests/test_plugin.py", "-k", "reload_does_not_fake"]),
    ("running-session flag lost on reload", drop_session_flag_carry_over,
     ["tests/test_plugin.py", "-k", "reload_carries_the_running_session"]),
    ("catastrophic-backtracking probe disabled", disable_probe,
     ["tests/test_plugin.py", "-k", "catastrophic or applies_the_probe or validate_patterns"]),
    ("warning moved before the Done line", warn_before_startup,
     ["tests/test_e2e.py", "-k", "arrives_after"]),
]


def pytest_ok(workdir, selector):
    env = dict(os.environ)
    env["PYTHONPATH"] = ".testlibs"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest"] + selector + ["-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(workdir), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    lines = [l.strip() for l in (proc.stdout + proc.stderr).splitlines() if l.strip()]
    summary = next((l for l in reversed(lines)
                    if "passed" in l or "failed" in l or "error" in l), "(no summary)")
    return proc.returncode, summary


def main():
    if not (REPO / ".testlibs").is_dir():
        print("run the tests/README.md setup first (.testlibs is missing)")
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="mutation_check_"))
    shutil.copytree(REPO, workdir / "repo",
                    ignore=shutil.ignore_patterns(".git", ".pytest_cache"))
    repo = workdir / "repo"
    baseline = (repo / SRC).read_text(encoding="utf-8")

    code, summary = pytest_ok(repo, ["tests/test_plugin.py", "-k",
                                     "stale or idle or threshold or reload_does_not_fake or "
                                     "never_reached_startup or catastrophic"])
    print("baseline (unmutated): exit={} {}".format(code, summary))
    if code != 0:
        print("baseline is not green — fix the tests first")
        return 1
    print()

    killed = 0
    for name, mutate, selector in MUTATIONS:
        mutated = mutate(baseline)
        if mutated == baseline:
            print("  !! {}: mutation did not apply (anchor moved?)".format(name))
            continue
        (repo / SRC).write_text(mutated, encoding="utf-8")
        code, summary = pytest_ok(repo, selector)
        (repo / SRC).write_text(baseline, encoding="utf-8")

        if code == 1:
            print("  caught   {:<42} {}".format(name, summary))
            killed += 1
        elif code in (4, 5):
            print("  HARNESS  {:<42} exit={} — selector matched nothing".format(name, code))
        else:
            print("  SURVIVED {:<42} exit={} {}".format(name, code, summary))

    print()
    print("caught {}/{}".format(killed, len(MUTATIONS)))
    shutil.rmtree(workdir, ignore_errors=True)
    if killed != len(MUTATIONS):
        print("some mutations survived — the corresponding tests are decorative")
        return 1
    print("every mutation was caught — the tests have teeth")
    return 0


if __name__ == "__main__":
    sys.exit(main())
