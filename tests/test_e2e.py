"""End-to-end tests: run a real MCDR against the real packaged plugin.

These tests boot an actual MCDReforged instance in a temporary directory, load
the ``.mcdr`` artifact produced by ``pack.py``, and drive a fake Minecraft server
through a complete lifecycle. They are the only layer that can prove the two
claims unit tests cannot reach:

* lines are really gone from the console, and
* MCDR's own startup / stop detection still works with the filter installed.

They are slower than the unit suite (~25 s each). Skip with::

    MCDR_SKIP_E2E=1 python -m pytest tests
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TESTLIBS = REPO / ".testlibs"

# The noisy line this plugin exists to suppress.
TARGET = "standing on air - force-sending blocks below"

# Everything the fake server prints, in order. This mirrors the README's
# end-to-end table: the target lines must vanish, all the rest must survive.
FAKE_SERVER_LIFECYCLE = [
    "Starting minecraft server version 1.21.8",
    "Preparing level \"world\"",
    'Done (0.648s)! For help, type "help"',
    "Steve joined the game",
    "Player Steve " + TARGET,
    "Player Alex " + TARGET,
    "Alex moved too quickly! 1.2,0.0,3.4",
    "Steve lost connection: Disconnected",
    # Note: MCDR's vanilla handler tests for exactly 'Stopping server'
    # (AbstractMinecraftHandler.test_server_stopping), without a leading "the".
    "Stopping server",
    "Saving players",
]

FAKE_SERVER_SOURCE = '''
import time

LINES = {lines!r}

for index, content in enumerate(LINES):
    print('[12:00:{{:02d}}] [Server thread/INFO]: {{}}'.format(index, content), flush=True)
    time.sleep(0.05)
time.sleep(0.5)
'''

MCDR_CONFIG = """\
handler: vanilla_handler
# Detection samples messages for a full minute (HANDLER_DETECTION_MINIMUM_SAMPLING_TIME),
# which would dominate the test runtime. The handler is pinned explicitly instead.
handler_detection: false
start_command: '"{python}" fake_server.py'
working_directory: server
advanced_console: false
disable_console_thread: true
disable_console_color: true
check_update: false
telemetry: false
"""


def _require_mcdr() -> None:
    if os.environ.get("MCDR_SKIP_E2E"):
        pytest.skip("MCDR_SKIP_E2E is set")
    if not TESTLIBS.is_dir():
        pytest.skip("run tests/README.md setup first (.testlibs is missing)")
    probe = subprocess.run(
        [sys.executable, "-c", "import mcdreforged"],
        env={**os.environ, "PYTHONPATH": str(TESTLIBS)},
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip("mcdreforged is not importable by " + sys.executable)


def _build_instance(root: Path) -> Path:
    """Create an MCDR instance in ``root`` that loads the packaged plugin."""
    (root / "server").mkdir(parents=True)
    (root / "plugins").mkdir()
    (root / "logs").mkdir()

    (root / "server" / "fake_server.py").write_text(
        FAKE_SERVER_SOURCE.format(lines=FAKE_SERVER_LIFECYCLE), encoding="utf-8"
    )
    (root / "config.yml").write_text(
        MCDR_CONFIG.format(python=sys.executable), encoding="utf-8"
    )
    (root / "permission.yml").write_text("{}\n", encoding="utf-8")

    # Load the real distribution artifact, not the source tree.
    sys.path.insert(0, str(REPO))
    import pack

    pack.build(root / "plugins" / "ServerLogFilter.mcdr")
    return root


def run_mcdr(root: Path, timeout: float = 90.0) -> str:
    """Run MCDR to completion in ``root`` and return everything it printed."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(TESTLIBS)
    env["PYTHONIOENCODING"] = "utf-8"
    env["MCDR_DISABLE_TELEMETRY"] = "1"

    stdout_path = root / "stdout.txt"
    with open(stdout_path, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "mcdreforged"],
            cwd=str(root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.3)
        else:
            proc.kill()
            proc.wait()
            pytest.fail(f"MCDR did not exit within {timeout:.0f}s")

    return stdout_path.read_text(encoding="utf-8", errors="replace")


def echoed_server_lines(output: str):
    """The lines MCDR echoed to the console from the server's stdout."""
    return [l for l in output.splitlines() if l.startswith("[Server]")]


@pytest.fixture(scope="module")
def e2e_output(tmp_path_factory):
    """One MCDR run shared by the assertions below (each run costs ~25 s)."""
    _require_mcdr()
    root = tmp_path_factory.mktemp("mcdr_e2e")
    _build_instance(root)
    output = run_mcdr(root)
    return output, root


def test_target_lines_are_hidden_from_the_console(e2e_output):
    output, _ = e2e_output
    echoed = echoed_server_lines(output)

    assert echoed, "MCDR echoed no server output at all; the harness is broken"

    hidden_hits = [l for l in echoed if TARGET in l]
    assert hidden_hits == [], "target lines were echoed to the console: {}".format(hidden_hits)


def test_innocent_lines_still_reach_the_console(e2e_output):
    """The filter must not be a blanket suppression."""
    output, _ = e2e_output
    echoed = "\n".join(echoed_server_lines(output))

    for expected in [
        "Starting minecraft server version",
        'Preparing level "world"',
        "Steve joined the game",
        "Alex moved too quickly!",
        "Steve lost connection",
        "Saving players",
    ]:
        assert expected in echoed, f"expected line missing from console: {expected!r}"


def test_mcdr_startup_and_stop_detection_still_work(e2e_output):
    """The safety property observed end to end, via MCDR's own log messages.

    If the plugin ever regressed to ``discarded()`` or returned False, MCDR would
    stop seeing these and the assertions below would fail.
    """
    output, root = e2e_output

    assert 'Done (0.648s)! For help, type "help"' in output, "startup line was swallowed"
    assert "Stopping server" in output, "stop line was swallowed"
    assert "Server process stopped with code 0" in output
    assert "Server stopped" in output

    mcdr_log = (root / "logs" / "MCDR.log").read_text(encoding="utf-8", errors="replace")
    assert "Server is running at PID" in mcdr_log


def test_plugin_reports_what_it_hid(e2e_output):
    """on_server_stop must summarise the run (report_on_server_stop defaults True)."""
    output, _ = e2e_output
    summaries = [l for l in output.splitlines() if "控制台隐去" in l and "本次运行" in l]
    assert summaries, "plugin did not log an end-of-run summary"
    # exactly the two TARGET lines were hidden
    assert "2 行" in summaries[0], summaries[0]


def test_packaged_plugin_is_what_was_loaded(e2e_output):
    """The run must have loaded the .mcdr artifact, not the source directory."""
    output, root = e2e_output
    assert "PackedPlugin server_log_filter" in output or "server_log_filter@1.0.2 loaded" in output
    assert (root / "plugins" / "ServerLogFilter.mcdr").is_file()
    assert not (root / "plugins" / "server_log_filter").exists()


def test_plugin_generated_its_default_config(e2e_output):
    _, root = e2e_output
    cfg = root / "config" / "server_log_filter" / "config.json"
    assert cfg.is_file(), "plugin did not create its config file"

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert set(data) == {"patterns", "log_matched_lines", "report_on_server_stop"}
    assert TARGET in data["patterns"]
