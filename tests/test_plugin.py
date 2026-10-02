"""Unit tests for Server Log Filter.

The suite has two layers, and both run against a **real** MCDR:

1. Behaviour tests exercising the filter directly: matching, counter and reload
   semantics, config defaults, and the error-tolerance guarantees documented in
   the README (a broken regex must not take down the working rules).
2. Contract tests asserting the safety property the whole plugin design rests
   on: a hidden line keeps ``InfoActionFlag.process`` (events keep flowing)
   while losing ``InfoActionFlag.echo_to_console`` (console stays clean), and
   critical lifecycle lines are never matched at all.

MCDR is a hard requirement, not an optional extra: the plugin subclasses
``mcdreforged.api.utils.Serializable`` for its config, so importing
``server_log_filter`` at all needs MCDR present. See tests/README.md for setup.

Run:  python -m pytest tests -v
"""

import importlib
import json
import pathlib
import shutil
import tempfile
import zipfile

import pytest

import server_log_filter as slf


# ---------------------------------------------------------------------------
#  helpers
# ---------------------------------------------------------------------------

class RecordingLogger:
    """Captures logger calls so tests can assert on warnings / info messages."""

    def __init__(self):
        self.infos = []
        self.warnings = []
        self.errors = []

    def info(self, msg, *a, **k):
        self.infos.append(str(msg))

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))

    def error(self, msg, *a, **k):
        self.errors.append(str(msg))


def make_rules(patterns):
    """Compile patterns the same way on_load does, via the real helper."""
    server = FakePluginServer(logger=RecordingLogger())
    return slf._build_rules(server, patterns), server.logger


def make_filter(patterns=None, log_matched=False):
    rules = [slf.Rule(p) for p in (patterns or [slf.DEFAULT_PATTERN])]
    return slf.ServerLogFilter(rules, RecordingLogger(), log_matched)


class FakeInfo:
    """Minimal stand-in for mcdreforged Info (pure-logic layer)."""

    def __init__(self, content, raw_content=None):
        self.content = content
        self.raw_content = raw_content if raw_content is not None else content
        self.action_flag = "UNTOUCHED"


class FakeSource:
    def __init__(self):
        self.replies = []

    def reply(self, msg):
        self.replies.append(msg)

    def has_permission(self, level):
        return True


class FakePluginServer:
    def __init__(self, logger=None):
        self.logger = logger or RecordingLogger()
        self.info_filters = []
        self.help_messages = []
        self.commands = []
        self.config = None

    def register_info_filter(self, f):
        self.info_filters.append(f)

    def register_help_message(self, prefix, msg):
        self.help_messages.append((prefix, msg))

    def register_command(self, cmd):
        self.commands.append(cmd)

    def load_config_simple(self, target_class=None, **kwargs):
        self.config = target_class()
        return self.config


# The set of noisy lines this plugin exists to suppress (server side, MC 26.3).
NOISY_LINES = [
    "Player Steve standing on air - force-sending blocks below",
    "Player Notch standing on air - force-sending blocks below",
    "Player a_very_long_name_123 standing on air - force-sending blocks below",
]

# Lines that must NEVER be hidden: MCDR relies on them for lifecycle detection
# and general observability.
CRITICAL_LINES = [
    'Done (0.648s)! For help, type "help"',
    "Steve joined the game",
    "Steve left the game",
    "Stopping the server",
    "Stopping server",
    "Steve moved too quickly! 1.2,0.0,3.4",
    "Steve moved wrongly! 0.5",
    "Rejecting UseItemOnPacket from Steve",
    "Steve has too many items, dropping items too fast",
    "Ignoring chat session from Steve due to missing Services public key",
    "Steve lost connection: Disconnected",
    "Starting minecraft server version 1.21.8",
    "[Server thread/INFO]: Preparing level \"world\"",
    "Steve was slain by Zombie",
    "Saving and pausing game...",
]


# ---------------------------------------------------------------------------
#  1. filtering behaviour and the core safety property
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", NOISY_LINES)
def test_noisy_lines_are_matched(line):
    assert make_filter().match(line) is not None


@pytest.mark.parametrize("line", CRITICAL_LINES)
def test_critical_lines_are_never_matched(line):
    """The regression guard: 15 realistic lines must not be touched."""
    assert make_filter().match(line) is None


def test_matching_is_substring_not_anchored():
    f = make_filter(["standing on air"])
    assert f.match("prefix Player Steve standing on air - force-sending blocks below suffix")


def test_dot_is_a_regex_wildcard_as_documented():
    """README warns that '.' is a wildcard; verify the documented sharp edge."""
    f = make_filter(["a.c"])
    assert f.match("abc") is not None      # '.' matched 'b'
    f2 = make_filter([r"a\.c"])
    assert f2.match("abc") is None
    assert f2.match("a.c") is not None


def test_near_miss_lines_do_not_match():
    """Proof the plugin is not a blunt 'filter everything'."""
    f = make_filter()
    for line in [
        "Player Steve standing on ground - force-sending blocks below",
        "Player Steve standing on air - sending blocks below",
        "standing on air",
        "",
    ]:
        assert f.match(line) is None, line


def test_hidden_line_keeps_process_flag():
    """THE safety property: hidden, not discarded."""
    try:
        from mcdreforged.api.types import InfoActionFlag
    except ImportError:
        pytest.skip("mcdreforged not installed")
    f = make_filter()
    info = FakeInfo("Player Steve standing on air - force-sending blocks below")
    f.filter_server_info(info)
    assert info.action_flag is InfoActionFlag.hidden()
    assert InfoActionFlag.process in info.action_flag, "events must keep flowing"
    assert InfoActionFlag.echo_to_console not in info.action_flag, "console must be clean"
    assert info.action_flag != InfoActionFlag.discarded(), "must never discard the info"


def test_unmatched_line_is_left_untouched():
    f = make_filter()
    info = FakeInfo('Done (0.648s)! For help, type "help"')
    f.filter_server_info(info)
    assert info.action_flag == "UNTOUCHED", "filter must not mutate unrelated info"


@pytest.mark.parametrize("content", ["", None, "   ", "\t", "\n"])
def test_degenerate_content_does_not_crash(content):
    info = FakeInfo(content)
    make_filter().filter_server_info(info)  # must not raise


def test_falls_back_to_raw_content_when_content_is_none():
    """MCDR may hand over info whose parsed content is empty."""
    f = make_filter()
    info = FakeInfo(None, raw_content="Player Steve standing on air - force-sending blocks below")
    f.filter_server_info(info)
    assert info.action_flag != "UNTOUCHED"


def test_player_name_variation_all_match():
    f = make_filter()
    for name in ["Steve", "玩家A", "x" * 16, "Name With Spaces"]:
        assert f.match("Player {} standing on air - force-sending blocks below".format(name))


# ---------------------------------------------------------------------------
#  2. rule compilation and error tolerance
# ---------------------------------------------------------------------------

def test_invalid_regex_is_skipped_with_warning_and_others_survive():
    rules, logger = make_rules(["[unclosed", slf.DEFAULT_PATTERN])
    assert len(rules) == 1
    assert logger.warnings, "a skipped rule must warn"
    assert rules[0].pattern == slf.DEFAULT_PATTERN


def test_blank_and_whitespace_patterns_are_dropped_silently():
    rules, logger = make_rules(["", "   ", "\t", slf.DEFAULT_PATTERN])
    assert len(rules) == 1
    assert logger.warnings == []


def test_patterns_are_stripped():
    rules, _ = make_rules(["  " + slf.DEFAULT_PATTERN + "  "])
    assert rules[0].pattern == slf.DEFAULT_PATTERN


def test_empty_pattern_list_is_tolerated():
    f = make_filter([])
    assert f.match("anything") is None
    f.filter_server_info(FakeInfo("anything"))  # no crash, nothing hidden


# ---------------------------------------------------------------------------
#  3. counters, reload and reset semantics
# ---------------------------------------------------------------------------

def test_counters_increment_per_rule_independently():
    f = make_filter(["alpha", "beta"])
    for line in ["alpha 1", "beta 1", "alpha 2", "beta 2", "beta 3"]:
        f.filter_server_info(FakeInfo(line))
    assert f.rules[0].count == 2
    assert f.rules[1].count == 3
    assert f.total == 5


def test_unmatched_lines_do_not_increment_counters():
    f = make_filter()
    f.filter_server_info(FakeInfo("nothing to see here"))
    assert f.total == 0


def test_reload_replaces_rules_and_zeroes_counters():
    f = make_filter(["alpha"])
    f.filter_server_info(FakeInfo("alpha"))
    assert f.total == 1
    new_rules = [slf.Rule("gamma")]
    f.reload_rules(new_rules, False)
    assert f.rules[0].pattern == "gamma"
    assert f.total == 0
    assert f.match("alpha") is None
    assert f.match("gamma") is not None


def test_reset_counters_zeroes_total_and_each_rule():
    f = make_filter(["alpha", "beta"])
    f.filter_server_info(FakeInfo("alpha"))
    f.filter_server_info(FakeInfo("beta"))
    f.reset_counters()
    assert f.total == 0
    assert all(r.count == 0 for r in f.rules)


def test_first_matching_rule_wins_and_is_counted_once():
    f = make_filter(["alpha", "alpha beta"])
    f.filter_server_info(FakeInfo("alpha beta"))
    assert f.rules[0].count == 1
    assert f.rules[1].count == 0


def test_log_matched_lines_flag_writes_to_logger():
    logger = RecordingLogger()
    f = slf.ServerLogFilter([slf.Rule(slf.DEFAULT_PATTERN)], logger, True)
    f.filter_server_info(FakeInfo("Player Steve standing on air - force-sending blocks below"))
    assert any("已隐去" in m for m in logger.infos)


def test_log_matched_lines_disabled_stays_quiet():
    logger = RecordingLogger()
    f = slf.ServerLogFilter([slf.Rule(slf.DEFAULT_PATTERN)], logger, False)
    f.filter_server_info(FakeInfo("Player Steve standing on air - force-sending blocks below"))
    assert logger.infos == []


# ---------------------------------------------------------------------------
#  4. configuration object
# ---------------------------------------------------------------------------

def test_config_defaults_match_documented_json():
    cfg = slf.Config()
    data = cfg.serialize()
    assert data["patterns"] == [slf.DEFAULT_PATTERN]
    assert data["log_matched_lines"] is False
    assert data["report_on_server_stop"] is True


def test_config_deserializes_from_json_like_dict():
    cfg = slf.Config.deserialize(
        {
            "patterns": ["standing on air", r"Ignoring chat session from .*"],
            "log_matched_lines": True,
            "report_on_server_stop": False,
        }
    )
    assert len(cfg.patterns) == 2
    assert cfg.log_matched_lines is True
    assert cfg.report_on_server_stop is False


def test_config_round_trip_is_stable():
    cfg = slf.Config.deserialize({"patterns": ["a", "b"], "log_matched_lines": True})
    again = slf.Config.deserialize(cfg.serialize())
    assert again.serialize() == cfg.serialize()


# ---------------------------------------------------------------------------
#  5. the MCDR contract (skipped when MCDR is unavailable)
# ---------------------------------------------------------------------------

def test_info_action_flag_contract_documented_in_readme():
    """README claims hidden() keeps distribution and drops console echo."""
    flag = pytest.importorskip("mcdreforged.api.types").InfoActionFlag
    hidden = flag.hidden()
    assert flag.process in hidden
    assert flag.echo_to_console not in hidden
    assert flag.send_to_server in hidden
    assert hidden != flag.discarded()
    assert flag.discarded() == flag(0)


def test_info_filter_accepts_action_flag_edit():
    """The plugin relies on InfoFilter letting it mutate info.action_flag."""
    info_mod = pytest.importorskip("mcdreforged.info_reactor.info")
    types_mod = pytest.importorskip("mcdreforged.api.types")
    info = info_mod.Info(info_mod.InfoSource.SERVER, "Player Steve standing on air - force-sending blocks below")
    info.content = "Player Steve standing on air - force-sending blocks below"
    f = make_filter()
    f.filter_server_info(info)
    assert types_mod.InfoActionFlag.process in info.action_flag
    assert types_mod.InfoActionFlag.echo_to_console not in info.action_flag


# ---------------------------------------------------------------------------
#  6. command surface
# ---------------------------------------------------------------------------

def test_on_load_registers_filter_help_and_command():
    slf.on_load(FakePluginServer(), None)
    # registration happened on the fake server we just built
    # (rebuild so we can assert on the same instance)
    fake = FakePluginServer()
    slf.on_load(fake, None)
    assert len(fake.info_filters) == 1
    assert fake.help_messages and fake.help_messages[0][0] == "!!logfilter"
    assert len(fake.commands) == 1


def test_show_status_reports_rules_and_counts():
    fake = FakePluginServer()
    slf.on_load(fake, None)
    slf._log_filter.filter_server_info(FakeInfo("Player Steve standing on air - force-sending blocks below"))
    source = FakeSource()
    slf._show_status(source)
    text = "".join(str(x) for x in source.replies)
    assert "1" in text  # one rule
    assert "已隐去" in text


def test_show_status_without_rules_warns_user():
    fake = FakePluginServer()
    fake.load_config_simple = lambda target_class=None, **kw: _empty_config(target_class)
    slf.on_load(fake, None)
    source = FakeSource()
    slf._show_status(source)
    text = "".join(str(x) for x in source.replies)
    assert "无规则" in text or "patterns" in text


def _empty_config(target_class):
    cfg = target_class()
    cfg.patterns = []
    return cfg


def test_test_command_reports_hit_and_miss():
    fake = FakePluginServer()
    slf.on_load(fake, None)

    hit = FakeSource()
    slf._test_line(hit, {"text": "Player Steve standing on air - force-sending blocks below"})
    assert "会被隐去" in "".join(str(x) for x in hit.replies)

    miss = FakeSource()
    slf._test_line(miss, {"text": 'Done (0.648s)! For help, type "help"'})
    assert "不会被隐去" in "".join(str(x) for x in miss.replies)


def test_reload_rereads_config_and_reports_count():
    fake = FakePluginServer()
    slf.on_load(fake, None)
    source = FakeSource()
    slf._reload(source)
    text = "".join(str(x) for x in source.replies)
    assert "已重载" in text


def test_reload_requires_admin_permission():
    """!!logfilter reload must be gated at ADMIN level."""
    fake = FakePluginServer()
    slf.on_load(fake, None)
    from mcdreforged.api.types import PermissionLevel

    cmd = fake.commands[0]
    # walk the command tree looking for the 'reload' literal
    reload_node = _find_literal(cmd, "reload")
    assert reload_node is not None, "reload subcommand must exist"
    requirements = [getattr(r, "requirement", r) for r in getattr(reload_node, "_requirements", [])]
    assert requirements, "reload must carry a permission requirement"

    class Src:
        def __init__(self, level):
            self.level = level

        def has_permission(self, required):
            return self.level >= required

    # every requirement must pass for ADMIN, and USER must be rejected
    assert all(req(Src(PermissionLevel.ADMIN)) for req in requirements)
    assert not all(req(Src(PermissionLevel.USER)) for req in requirements)


def _find_literal(node, name):
    """Depth-first search of an MCDR command tree for a Literal named `name`.

    MCDR stores sub-literals in ``_children_literal``, a defaultdict mapping the
    literal name to a *list* of candidate nodes.
    """
    children = getattr(node, "_children_literal", None)
    if not isinstance(children, dict):
        return None
    for child in children.get(name, []):
        return child
    for nodes in children.values():
        for child in nodes:
            found = _find_literal(child, name)
            if found is not None:
                return found
    return None


def test_min_mcdr_version_matches_plugin_metadata():
    meta = json.loads((pathlib.Path(__file__).resolve().parent.parent / "mcdreforged.plugin.json").read_text("utf-8"))
    assert meta["dependencies"]["mcdreforged"] == ">=" + slf.MIN_MCDR_VERSION


def test_plugin_metadata_matches_repo_conventions():
    meta = json.loads((pathlib.Path(__file__).resolve().parent.parent / "mcdreforged.plugin.json").read_text("utf-8"))
    assert meta["id"] == "server_log_filter"
    assert meta["license"] == "MIT"
    assert set(meta["description"]) == {"en_us", "zh_cn"}
    assert meta["authors"][0]["name"] == "Pau1am"


# ---------------------------------------------------------------------------
#  7. release packaging
# ---------------------------------------------------------------------------

class _LegalityZip:
    """Stands in for PackedPlugin: feeds MCDR's own legality check a real archive.

    ``_check_dir_legality`` only reads the zip and calls ``get_id``, so this is
    enough to run MCDR's genuine validation without booting an MCDR instance.
    ``_ILLEGAL_ROOT_PY_FILE_STEM`` is copied onto the class by the tests below.
    """

    def __init__(self, path, plugin_id):
        self._zip = zipfile.ZipFile(path)
        self._plugin_id = plugin_id

    def get_id(self):
        return self._plugin_id

    @property
    def _PackedPlugin__zip_file(self):
        return self._zip


def _build_package(tmp_path):
    """Run the shipped packer and return (archive path, member names)."""
    pack = importlib.import_module("pack")
    out = tmp_path / "ServerLogFilter-test.mcdr"
    pack.build(out)
    names = zipfile.ZipFile(out).namelist()
    return out, names


def test_packaged_artifact_is_loadable(tmp_path):
    """The built .mcdr must pass MCDR's own root-entry validation.

    Regression guard: a root-level module (the test suite's own conftest.py, a
    setup.py, ...) makes MCDR refuse the package with IllegalPluginStructure.
    """
    packed = pytest.importorskip("mcdreforged.plugin.type.packed_plugin")
    meta = json.loads(
        (pathlib.Path(__file__).resolve().parent.parent / "mcdreforged.plugin.json").read_text("utf-8")
    )
    out, _ = _build_package(tmp_path)

    checker = _LegalityZip(out, meta["id"])
    type(checker)._ILLEGAL_ROOT_PY_FILE_STEM = packed.PackedPlugin._ILLEGAL_ROOT_PY_FILE_STEM
    # must not raise
    packed.PackedPlugin._check_dir_legality(checker)


def test_packager_excludes_repo_infrastructure(tmp_path):
    """Sanity check on the shipped artifact's contents."""
    _, names = _build_package(tmp_path)

    assert "mcdreforged.plugin.json" in names
    assert "server_log_filter/__init__.py" in names

    for unwanted in ("conftest.py", "pack.py", "tests/test_plugin.py"):
        assert unwanted not in names, unwanted
    assert not [n for n in names if n.startswith(".testlibs/") or n.startswith("tests/")]
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]


def test_packager_keeps_artifact_small(tmp_path):
    """A stray .testlibs/ once blew this up to 1362 files / 7.11 MB."""
    out, names = _build_package(tmp_path)
    assert len(names) < 20, f"unexpectedly many files packed: {names}"
    assert out.stat().st_size < 200 * 1024


def test_packager_ships_submodules_recursively(tmp_path, monkeypatch):
    """Submodules of the plugin package must not be silently dropped.

    Regression guard: an earlier allowlist used ``rel.parent == Path(PACKAGE_NAME)``,
    which only matches *direct* children. Any ``server_log_filter/sub/helper.py``
    would have been omitted from the release without any error — a silently
    broken artifact. The package is only ``__init__.py`` today, so this test
    plants a submodule in a temporary copy to prove the packer keeps up.
    """
    pack = importlib.import_module("pack")
    src = pathlib.Path(__file__).resolve().parent.parent

    staged = tmp_path / "staged"
    shutil.copytree(
        src, staged, ignore=shutil.ignore_patterns(".git", ".testlibs", "__pycache__", "*.pyc")
    )
    sub = staged / "server_log_filter" / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")

    monkeypatch.setattr(pack, "SRC", staged)
    out = tmp_path / "nested.mcdr"
    pack.build(out)
    names = zipfile.ZipFile(out).namelist()

    assert "server_log_filter/sub/__init__.py" in names, names
    assert "server_log_filter/sub/helper.py" in names, names


def test_packager_root_entries_would_be_illegal_if_denylisted():
    """Documents *why* the allowlist exists, using a real denylist build.

    The old README recipe (rglob + tiny skip set) produces an archive that MCDR
    rejects. This test asserts the failure mode is real, so nobody 'simplifies'
    pack.py back into a denylist.
    """
    packed = pytest.importorskip("mcdreforged.plugin.type.packed_plugin")
    meta = json.loads(
        (pathlib.Path(__file__).resolve().parent.parent / "mcdreforged.plugin.json").read_text("utf-8")
    )
    src = pathlib.Path(__file__).resolve().parent.parent

    denylist_skip = {".git", "__pycache__"}
    files = [
        p
        for p in src.rglob("*")
        if p.is_file() and not (denylist_skip & set(p.parts)) and p.suffix != ".pyc"
    ]
    assert any(p.name == "conftest.py" for p in files), "conftest.py must exist for this guard to matter"

    tmp = tempfile.mkdtemp()
    archive = pathlib.Path(tmp) / "denylist.mcdr"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, p.relative_to(src).as_posix())

    checker = _LegalityZip(archive, meta["id"])
    type(checker)._ILLEGAL_ROOT_PY_FILE_STEM = packed.PackedPlugin._ILLEGAL_ROOT_PY_FILE_STEM
    with pytest.raises(packed.IllegalPluginStructure):
        packed.PackedPlugin._check_dir_legality(checker)
