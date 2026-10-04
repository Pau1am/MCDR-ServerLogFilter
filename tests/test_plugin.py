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

import ast
import importlib
import json
import os
import pathlib
import re
import shutil
import string
import subprocess
import sys
import tempfile
import time
import zipfile

import pytest

import server_log_filter as slf
from server_log_filter import i18n


# ---------------------------------------------------------------------------
#  helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def pinned_language(monkeypatch):
    """Pin the module-level language so message assertions cannot leak across tests.

    The plugin resolves its language once per load and keeps it in a module global, so a
    test that loads an ``en_us`` config would otherwise change what the *next* test sees —
    an order-dependent suite. Pinning it to ``zh_cn`` (the language these assertions are
    written in, and what the plugin printed before it had any language support at all)
    makes every test independent. The language tests below set it explicitly, or go
    through ``on_load``, which sets it from the config.
    """
    monkeypatch.setattr(slf, "_language", "zh_cn")


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
    def __init__(
        self,
        logger=None,
        config_data=None,
        state_data=None,
        data_folder=None,
        mcdr_language="zh_cn",
    ):
        self.logger = logger or RecordingLogger()
        self.info_filters = []
        self.help_messages = []
        self.commands = []
        self.config = None
        self.config_data = config_data
        self.state_data = state_data
        self.saved = {}
        self.load_kwargs = {}
        self.data_folder = data_folder
        # Mirrors ``ServerInterface.get_mcdr_language()``. The default is zh_cn because
        # that is the language this suite's message assertions are written in; the
        # language tests override it to prove ``auto`` really follows MCDR.
        self.mcdr_language = mcdr_language

    def get_mcdr_language(self):
        return self.mcdr_language

    def get_data_folder(self):
        """A throwaway folder by default, so no real config file is ever touched."""
        if self.data_folder is None:
            self.data_folder = tempfile.mkdtemp(prefix="slf_cfg_")
        return self.data_folder

    def register_info_filter(self, f):
        self.info_filters.append(f)

    def register_help_message(self, prefix, msg):
        self.help_messages.append((prefix, msg))

    def register_command(self, cmd):
        self.commands.append(cmd)

    def load_config_simple(self, file_name=None, target_class=None, **kwargs):
        if file_name == slf.STATE_FILE_NAME:
            return target_class.deserialize(self.state_data or {})

        self.load_kwargs = kwargs
        # Mirror MCDR: the data_processor runs against the raw dict read from the
        # file, its return value decides whether the file gets written back, and
        # the (possibly mutated) dict is what gets deserialized.
        data = dict(self.config_data) if self.config_data is not None else None
        processor = kwargs.get("data_processor")
        needs_save = False
        if processor is not None and data is not None:
            needs_save = bool(processor(data))

        self.config = target_class.deserialize(data if data is not None else {})
        if needs_save:
            self.saved["config.json"] = self.config.serialize()
        return self.config

    def save_config_simple(self, config, file_name=None, **kwargs):
        data = config.serialize()
        self.saved[file_name] = data
        if file_name == slf.STATE_FILE_NAME:
            # Mirror MCDR writing the file to disk: a later on_load in the same
            # test reads it back, exactly like a plugin reload would.
            self.state_data = data


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
    assert data["warn_about_stale_rules"] is True
    assert data["stale_rule_threshold"] == 3
    assert data["validate_patterns"] is True
    assert data["pattern_probe_timeout_ms"] == 25
    assert data["announce_config_upgrade"] is True
    assert data["announce_broken_config"] is True


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


def test_packager_ships_exactly_the_allowlist(tmp_path):
    """Pin the shipped file set so the allowlist cannot drift unnoticed.

    A packaging change that nobody announces shows up to users as "the plugin got
    much bigger for no reason". Listing the exact contents here makes any change
    to that list a deliberate, visible edit.

    The READMEs are deliberately absent: MCDR never reads them, and together they
    accounted for over half the artifact (28 KB -> 14 KB without them).
    """
    _, names = _build_package(tmp_path)

    assert sorted(names) == [
        "CHANGELOG.md",
        "LICENSE",
        "mcdreforged.plugin.json",
        "server_log_filter/__init__.py",
        "server_log_filter/i18n.py",
        "server_log_filter/lang/en_us.json",
        "server_log_filter/lang/zh_cn.json",
    ], names

    for excluded in (
        "README.md",
        "README_en.md",
        # the translator guide: a README by another name, so it stays out too
        "server_log_filter/lang/README.md",
    ):
        assert excluded not in names, "{} must not be shipped".format(excluded)


def test_changelog_keeps_only_the_latest_release():
    """The shipped changelog must not accumulate one section per release.

    CHANGELOG.md travels inside the artifact, so every past entry keeps costing
    users bytes forever -- 1.2.0 -> 1.2.1 was +10.3%, all of it the changelog
    growing. Only the newest entry belongs there; the full history stays on the
    Releases page, where it has already been published.

    Asserting the single heading *equals the current version* is what makes this
    a real constraint: "at most one heading" is satisfied by an empty file too.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    meta = json.loads((root / "mcdreforged.plugin.json").read_text(encoding="utf-8"))

    headings = re.findall(r"^## \[([^\]]+)\]", changelog, re.M)
    assert headings == [meta["version"]], (
        "CHANGELOG.md should document exactly the shipped version ({}), found {}".format(
            meta["version"], headings
        )
    )

    # no leftover link-reference definitions for the removed versions either
    leftovers = re.findall(r"^\[\d+\.\d+\.\d+\]:", changelog, re.M)
    assert leftovers == [], "stale version links left behind: {}".format(leftovers)


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


# ---------------------------------------------------------------------------
#  8. stale-rule detection
# ---------------------------------------------------------------------------

IDLE = "a-rule-that-never-matches"
LIVE = slf.DEFAULT_PATTERN


def start_plugin(config=None, state=None, mcdr_language="zh_cn"):
    """Boot the plugin against a fake server and return (server, state_module)."""
    server = FakePluginServer(
        config_data=config, state_data=state, mcdr_language=mcdr_language
    )
    slf.on_load(server, None)
    return server


def run_session(server, hits=None, reach_startup=True):
    """Simulate one full server session: start -> startup -> stop.

    ``hits`` maps pattern -> how many lines that rule matched during the session.
    """
    slf.on_server_start(server)
    if reach_startup:
        slf.on_server_startup(server)
    hits = hits or {}
    for rule in slf._log_filter.rules:
        rule.count = hits.get(rule.pattern, 0)
    slf.on_server_stop(server, 0)


def warnings_of(server):
    return "\n".join(server.logger.warnings)


def test_idle_rule_is_reported_once_the_streak_reaches_the_threshold():
    server = start_plugin(
        config={"patterns": [LIVE, IDLE], "stale_rule_threshold": 2}
    )
    # session 1 and 2: the idle rule never matches -> streak grows to 2
    run_session(server, hits={LIVE: 5})
    assert IDLE not in warnings_of(server), "must not warn before the threshold is reached"
    run_session(server, hits={LIVE: 5})
    assert IDLE not in warnings_of(server), "the warning belongs to the NEXT startup"

    # session 3: the startup now sees streak == 2 and speaks up
    slf.on_server_start(server)
    slf.on_server_startup(server)
    text = warnings_of(server)
    assert IDLE in text
    assert "有 1 条过滤规则连续 2 次及以上开服都没有命中" in text
    # the rule that is working is not mentioned
    assert LIVE not in text


def test_stale_warning_does_not_repeat_the_streak_for_every_rule():
    """Two idle rules must not produce two identical "已连续 N 次…" lines.

    The count is already stated in the header; repeating it per rule pushes the
    useful part (which rules, and what to do) off the screen.
    """
    server = start_plugin(
        config={"patterns": [IDLE, "another-idle-rule", LIVE], "stale_rule_threshold": 1}
    )
    run_session(server, hits={LIVE: 3})
    slf.on_server_start(server)
    slf.on_server_startup(server)

    text = warnings_of(server)
    assert "有 2 条过滤规则连续 1 次及以上开服都没有命中" in text
    # both patterns are listed
    assert "· " + IDLE in text
    assert "· another-idle-rule" in text
    # ...but the per-rule streak sentence appears exactly zero times
    assert "从未命中过" not in text
    assert "开服零命中；" not in text
    assert text.count("都没有命中") == 1, "the header must be stated once"


def test_a_rule_idle_longer_than_the_threshold_still_says_so():
    """Information the header cannot carry is still worth showing.

    The header only promises "threshold or more"; a rule idle far longer than that
    is a stronger signal (probably obsolete rather than mistyped).
    """
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 1})
    for _ in range(4):
        run_session(server)                      # never matches -> streak climbs
    slf.on_server_start(server)
    slf.on_server_startup(server)

    text = warnings_of(server)
    assert "连续 1 次及以上" in text
    assert "（已连续 4 次零命中）" in text, "a longer streak must not be hidden"


def test_a_rule_at_exactly_the_threshold_gets_no_annotation():
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 3})
    for _ in range(3):
        run_session(server)
    slf.on_server_start(server)
    slf.on_server_startup(server)

    text = warnings_of(server)
    assert "（已连续" not in text, "nothing to add beyond the header, so add nothing"


def test_a_matching_rule_never_becomes_stale():
    server = start_plugin(config={"patterns": [LIVE], "stale_rule_threshold": 1})
    for _ in range(5):
        run_session(server, hits={LIVE: 3})
        slf.on_server_start(server)
        slf.on_server_startup(server)
        assert "都没有命中" not in warnings_of(server)


def test_a_single_hit_resets_the_streak():
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 2})

    def streak():
        return slf._state.rules[IDLE].zero_streak

    run_session(server)
    run_session(server)
    assert streak() == 2

    run_session(server, hits={IDLE: 1})  # rule fires once
    assert streak() == 0

    run_session(server)
    assert streak() == 1


def test_a_session_that_never_reached_startup_is_not_counted():
    """A server that fails to boot must not be able to fake an 'idle rule'.

    Otherwise three consecutive crashes would make every rule look unused and
    produce a bogus warning.
    """
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 2})
    for _ in range(5):
        run_session(server, reach_startup=False)
    assert slf._state.session_index == 0, "failed startups must not advance the history"
    assert IDLE not in slf._state.rules

    slf.on_server_start(server)
    slf.on_server_startup(server)
    assert "都没有命中" not in warnings_of(server)


def test_warning_can_be_switched_off():
    server = start_plugin(
        config={"patterns": [IDLE], "warn_about_stale_rules": False, "stale_rule_threshold": 1}
    )
    run_session(server)
    slf.on_server_start(server)
    slf.on_server_startup(server)
    assert IDLE not in warnings_of(server)
    # ...but the statistics are still collected, just not announced
    assert slf._state.rules[IDLE].zero_streak == 1


def test_threshold_zero_disables_the_warning():
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 0})
    for _ in range(4):
        run_session(server)
        slf.on_server_start(server)
        slf.on_server_startup(server)
    assert "都没有命中" not in warnings_of(server)


def test_state_forgets_patterns_that_left_the_config():
    server = start_plugin(config={"patterns": [LIVE, IDLE], "stale_rule_threshold": 1})
    run_session(server)
    assert set(slf._state.rules) == {LIVE, IDLE}

    # the admin removes the idle rule
    server.config_data = {"patterns": [LIVE], "stale_rule_threshold": 1}
    slf._apply_config(server)
    run_session(server)
    assert set(slf._state.rules) == {LIVE}, "removed patterns must be pruned from the state file"


def state_rules_of(server):
    return set(server.saved[slf.STATE_FILE_NAME]["rules"])


def test_deleting_a_rule_prunes_its_state_immediately_on_plugin_reload():
    """The admin should not have to wait for a whole server session.

    Pruning used to happen only in on_server_stop, so a rule deleted and the
    plugin reloaded stayed in state.json until the next successful shutdown.
    """
    server = start_plugin(config={"patterns": [LIVE, IDLE], "stale_rule_threshold": 1})
    run_session(server)
    assert state_rules_of(server) == {LIVE, IDLE}

    server.config_data = {"patterns": [LIVE], "stale_rule_threshold": 1}
    slf.on_load(server, None)          # !!MCDR reload plugin

    assert state_rules_of(server) == {LIVE}, "the deleted rule must be gone right away"
    assert IDLE not in slf._state.rules


def test_deleting_a_rule_prunes_its_state_on_the_reload_command():
    server = start_plugin(config={"patterns": [LIVE, IDLE], "stale_rule_threshold": 1})
    run_session(server)

    server.config_data = {"patterns": [LIVE], "stale_rule_threshold": 1}
    source = FakeSource()
    slf._reload(source)

    assert state_rules_of(server) == {LIVE}
    assert "已清除 1 条已删除规则的统计" in "".join(str(x) for x in source.replies)


def test_pruning_does_not_touch_the_file_when_nothing_was_deleted():
    """No change, no write: state.json must not be rewritten on every reload."""
    server = start_plugin(config={"patterns": [LIVE, IDLE], "stale_rule_threshold": 1})
    run_session(server)

    server.saved.clear()
    slf.on_load(server, None)

    assert server.saved == {}, "an unchanged state must not be written back"


def test_a_rule_rejected_by_the_safety_probe_keeps_its_history():
    """Only rules whose text has actually left ``patterns`` may be forgotten.

    Pruning must compare against the *configured* patterns, not the compiled
    rules: a rule that is still written down but gets skipped at load time
    (here, rejected by the catastrophic-backtracking guard after an upgrade)
    is not a deletion, and starting its statistics over would be a regression.
    """
    danger = r"(a+)+$"
    server = start_plugin(
        config={
            "patterns": [LIVE, danger],
            "stale_rule_threshold": 1,
            "validate_patterns": False,
        }
    )
    run_session(server)
    assert danger in slf._state.rules

    # the same two patterns, but the probe is on now, so the dangerous one is skipped
    server.config_data = {
        "patterns": [LIVE, danger],
        "stale_rule_threshold": 1,
        "validate_patterns": True,
    }
    slf._apply_config(server)

    assert all(rule.pattern != danger for rule in slf._log_filter.rules), "it is skipped"
    assert danger in slf._state.rules, "still configured, so still remembered"


def test_a_reset_config_does_not_wipe_the_rule_history(tmp_path):
    """A syntax error must not cost the admin every rule's history.

    When config.json is broken it is quarantined and regenerated with defaults, so
    the patterns seen at that moment are *not* what the user configured. Pruning
    against them would throw away statistics that are recoverable once the file is
    restored by hand.
    """
    server, folder = server_with_config_file(tmp_path, BROKEN_JSON)
    server.state_data = {
        "session_index": 4,
        "rules": {
            LIVE: {"hits_last_session": 2, "zero_streak": 0, "total_hits": 9, "last_hit_session": 4},
            IDLE: {"hits_last_session": 0, "zero_streak": 3, "total_hits": 0, "last_hit_session": 0},
        },
    }

    slf.on_load(server, None)

    assert set(slf._state.rules) == {LIVE, IDLE}, "the history must survive the reset"
    assert slf.STATE_FILE_NAME not in server.saved, "nothing may be written back either"

    # ...and the session-end cleanup must not quietly finish the job either
    run_session(server, hits={LIVE: 1})
    assert set(slf._state.rules) == {LIVE, IDLE}, "the history must survive the session too"


def test_pruning_announces_what_it_removed():
    server = start_plugin(config={"patterns": [LIVE, IDLE], "stale_rule_threshold": 1})
    run_session(server)
    server.logger.infos.clear()

    server.config_data = {"patterns": [LIVE], "stale_rule_threshold": 1}
    slf.on_load(server, None)

    text = "\n".join(server.logger.infos)
    assert "已从 state.json 清除 1 条规则统计" in text
    assert IDLE in text, "the admin needs to know which rule was forgotten"


def test_state_file_round_trips_through_json():
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 1})
    run_session(server)
    saved = server.saved[slf.STATE_FILE_NAME]

    import json as _json

    again = slf.State.deserialize(_json.loads(_json.dumps(saved)))
    assert again.serialize() == saved


def test_history_is_kept_between_runs():
    first = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 2})
    run_session(first)
    carried = first.saved[slf.STATE_FILE_NAME]

    # a fresh process picks up the file the previous one wrote
    second = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 2}, state=carried)
    assert slf._state.session_index == 1
    run_session(second)
    assert second.saved[slf.STATE_FILE_NAME]["rules"][IDLE]["zero_streak"] == 2


def test_reload_does_not_fake_an_idle_session():
    """A plugin reload mid-session must not restart the counters from zero.

    If counts were dropped, a rule that had actually matched would look idle for
    that session and could push an innocent rule over the threshold.
    """
    server = start_plugin(config={"patterns": [LIVE], "stale_rule_threshold": 1})
    slf.on_server_start(server)
    slf.on_server_startup(server)
    for _ in range(4):
        slf._log_filter.filter_server_info(
            FakeInfo("Player Steve standing on air - force-sending blocks below")
        )
    assert slf._log_filter.total == 4

    # !!MCDR reload plugin -> on_load runs again with the PREVIOUS module.
    # MCDR hands the old module object in; simulate that faithfully, because the
    # new module overwrites the globals before carry_over_from() is called.
    old_filter = slf._log_filter

    class PreviousModule:
        _log_filter = old_filter

    slf.on_load(server, PreviousModule)
    assert slf._log_filter is not old_filter, "reload must build a fresh filter"
    assert slf._log_filter.total == 4, "counters must survive a plugin reload"

    slf.on_server_stop(server, 0)
    assert slf._state.rules[LIVE].zero_streak == 0
    assert slf._state.rules[LIVE].hits_last_session == 4


def test_reload_carries_the_running_session_over_to_the_new_module():
    """A real reload builds a *fresh* module, so the 'startup happened' flag resets.

    If the flag were dropped, the reloaded plugin would skip recording the session
    entirely and the server's uptime would silently disappear from the statistics.
    """
    server = start_plugin(config={"patterns": [LIVE], "stale_rule_threshold": 1})
    slf.on_server_start(server)
    slf.on_server_startup(server)
    old_filter = slf._log_filter

    class PreviousModule:
        _log_filter = old_filter
        _session_reached_startup = True

    # simulate the brand-new module MCDR hands to on_load
    slf._session_reached_startup = False
    slf.on_load(server, PreviousModule)
    assert slf._session_reached_startup is True, "the running session must survive a reload"

    slf.on_server_stop(server, 0)
    assert slf._state.session_index == 1, "the session must still be recorded"


def test_status_command_surfaces_the_streak():
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 2})
    run_session(server)
    run_session(server)

    source = FakeSource()
    slf._show_status(source)
    text = "".join(str(x) for x in source.replies)
    assert "连续 2 次开服零命中" in text
    assert "已统计 2 个开服周期" in text


def test_reset_command_clears_the_streak():
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 1})
    run_session(server)
    assert slf._state.rules[IDLE].zero_streak == 1

    source = FakeSource()
    slf._reset_streaks(source)
    assert slf._state.rules[IDLE].zero_streak == 0
    assert "已重置" in "".join(str(x) for x in source.replies)
    assert server.saved[slf.STATE_FILE_NAME]["rules"][IDLE]["zero_streak"] == 0


# ---------------------------------------------------------------------------
#  9. catastrophic-backtracking guard
# ---------------------------------------------------------------------------

# Patterns a user could plausibly write, all of which must survive the probe.
REALISTIC_PATTERNS = [
    r"standing on air - force-sending blocks below",
    r"Player \w+ .*",
    r"joined the game|left the game",
    r"moved too quickly! [\d\.,]+",
    r"^\S+ has too many items",
    r"(?:Rejecting|Ignoring) \w+",
    r"Ignoring chat session from \S+ due to missing Services public key",
    r"Preparing spawn area: \d+%",
]


@pytest.mark.parametrize("pattern", REALISTIC_PATTERNS)
def test_realistic_patterns_pass_the_probe(pattern):
    server = FakePluginServer()
    rules = slf._build_rules(server, [pattern])
    assert [r.pattern for r in rules] == [pattern]
    assert server.logger.errors == []


@pytest.mark.parametrize(
    "pattern",
    [r"(a+)+$", r"^(a|a)*$", r"(\w+\s?)*$", r"(a*)*b"],
)
def test_catastrophic_patterns_are_refused(pattern):
    """These freeze MCDR's main thread for hundreds of ms per line, so they must go."""
    server = FakePluginServer()
    rules = slf._build_rules(server, [pattern])
    assert rules == []
    assert any("灾难性回溯" in msg for msg in server.logger.errors)


def test_one_bad_pattern_does_not_take_down_the_others():
    server = FakePluginServer()
    rules = slf._build_rules(server, [LIVE, r"(a+)+$", "joined the game"])
    assert [r.pattern for r in rules] == [LIVE, "joined the game"]


def test_probe_can_be_turned_off():
    server = FakePluginServer()
    rules = slf._build_rules(server, [r"(a+)+$"], validate=False)
    assert [r.pattern for r in rules] == [r"(a+)+$"]


def test_on_load_actually_applies_the_probe():
    """The guard must be wired into the real load path, not merely callable.

    Without this, disabling `validate_patterns` inside ``on_load`` would go
    unnoticed while every direct ``_build_rules`` test kept passing.
    """
    server = start_plugin(config={"patterns": [LIVE, r"(a+)+$"]})
    assert [r.pattern for r in slf._log_filter.rules] == [LIVE]
    assert any("灾难性回溯" in msg for msg in server.logger.errors)


def test_on_load_respects_validate_patterns_false():
    server = start_plugin(config={"patterns": [r"(a+)+$"], "validate_patterns": False})
    assert [r.pattern for r in slf._log_filter.rules] == [r"(a+)+$"]
    assert server.logger.errors == []


def test_on_load_honours_the_timeout_setting():
    """Wiring guard: the configured budget is what the probe actually receives."""
    seen = []
    original = slf._probe_pattern

    def spy(regex, budget_ms):
        seen.append(budget_ms)
        return original(regex, budget_ms)

    slf._probe_pattern = spy
    try:
        start_plugin(config={"patterns": [LIVE], "pattern_probe_timeout_ms": 7})
    finally:
        slf._probe_pattern = original

    assert seen == [7]


def test_a_nonsense_probe_budget_does_not_reject_every_rule():
    """A budget of 0 ms means "every cost is too much", so it used to refuse *all* rules.

    Worse, each refusal blamed catastrophic backtracking — for a pattern that is a plain
    literal. The filter then silently hid nothing while the log pointed at the wrong
    cause. The budget is floored at 1 ms, which still separates microseconds (real rules)
    from hundreds of milliseconds (dangerous ones).
    """
    for budget in (0, -5):
        server = start_plugin(
            config={"patterns": [LIVE], "pattern_probe_timeout_ms": budget}
        )
        assert len(slf._log_filter.rules) == 1, "rule lost at budget={}".format(budget)
        assert server.logger.errors == [], server.logger.errors


def test_probing_realistic_patterns_stays_cheap():
    """Load-time validation must not turn into a startup delay.

    8 realistic patterns cost microseconds in practice; the bound is deliberately
    loose so this stays a regression guard rather than a flaky benchmark.
    """
    server = FakePluginServer()
    start = time.perf_counter()
    rules = slf._build_rules(server, REALISTIC_PATTERNS)
    elapsed = time.perf_counter() - start

    assert len(rules) == len(REALISTIC_PATTERNS)
    assert server.logger.errors == []
    assert elapsed < 0.1, "pattern validation became expensive: {:.3f}s".format(elapsed)


def test_per_line_filtering_stays_in_the_microsecond_range():
    """A blunt but real guard: filtering must not cost milliseconds per line.

    1000 lines with 5 rules should be well under 20 ms (measured: ~1 ms).
    """
    patterns = [LIVE] + ["noise-marker-{}".format(i) for i in range(4)]
    f = slf.ServerLogFilter([slf.Rule(p) for p in patterns], RecordingLogger(), False)
    lines = ["Player Steve standing on air - force-sending blocks below",
             "Steve joined the game"] * 500
    infos = [FakeInfo(line) for line in lines]

    start = time.perf_counter()
    for info in infos:
        f.filter_server_info(info)
    elapsed = time.perf_counter() - start

    per_line_us = elapsed / len(infos) * 1e6
    assert elapsed < 0.02, "filtering {:.1f} µs/line is too slow".format(per_line_us)


def test_probe_accepts_a_compiled_regex_without_recompiling():
    rx = re.compile(LIVE)
    rule = slf.Rule(LIVE, rx)
    assert rule.regex is rx


def test_rule_pattern_is_stripped_and_blank_ones_skipped():
    server = FakePluginServer()
    rules = slf._build_rules(server, ["  " + LIVE + "  ", "", None, "   "])
    assert [r.pattern for r in rules] == [LIVE]


# ---------------------------------------------------------------------------
#  10. lightweight invariants (the hot path must stay cheap)
# ---------------------------------------------------------------------------


def test_hidden_flag_is_built_once_and_reused():
    """InfoActionFlag.hidden() allocates a new flag; the hot path must not call it."""
    f = make_filter()
    assert f._hidden_flag is not None
    assert f._hidden_flag is f._hidden_flag

    info = FakeInfo("Player Steve standing on air - force-sending blocks below")
    f.filter_server_info(info)
    assert info.action_flag is f._hidden_flag, "each hit must reuse the cached flag"


def test_rules_are_stored_immutably():
    f = make_filter([LIVE, "joined the game"])
    assert isinstance(f.rules, tuple)


def test_no_rules_short_circuits_before_reading_the_line():
    """With nothing to filter the plugin must not even touch the info object."""

    class ProbeInfo:
        def __init__(self):
            self.action_flag = "UNTOUCHED"
            self.content_reads = 0

        @property
        def content(self):
            self.content_reads += 1
            return "Player Steve standing on air - force-sending blocks below"

        @property
        def raw_content(self):
            self.content_reads += 1
            return "Player Steve standing on air - force-sending blocks below"

    info = ProbeInfo()
    f = slf.ServerLogFilter([], RecordingLogger(), False)
    f.filter_server_info(info)
    assert info.content_reads == 0
    assert info.action_flag == "UNTOUCHED"


def test_non_matching_line_leaves_the_action_flag_alone():
    f = make_filter()
    info = FakeInfo("Steve joined the game")
    f.filter_server_info(info)
    assert info.action_flag == "UNTOUCHED"


def test_matching_stops_at_the_first_rule():
    """Rule order decides, and later rules are not even evaluated."""
    expensive = slf.Rule(r"never-matches-this-\d")
    f = slf.ServerLogFilter([slf.Rule(LIVE), expensive], RecordingLogger(), False)
    info = FakeInfo("Player Steve standing on air - force-sending blocks below")
    f.filter_server_info(info)
    assert f.rules[0].count == 1
    assert expensive.count == 0


def test_state_tracking_adds_no_work_to_the_per_line_path():
    """The new feature must be free per line: all bookkeeping happens at session edges.

    Proven structurally — filter_server_info never touches the State object.
    """
    server = start_plugin(config={"patterns": [LIVE], "stale_rule_threshold": 1})
    slf.on_server_start(server)
    slf.on_server_startup(server)

    touched = []
    original_save = server.save_config_simple

    def spy(config, file_name=None, **kwargs):
        touched.append(file_name)
        return original_save(config, file_name=file_name, **kwargs)

    server.save_config_simple = spy
    for _ in range(50):
        slf._log_filter.filter_server_info(
            FakeInfo("Player Steve standing on air - force-sending blocks below")
        )
    assert touched == [], "per-line filtering must not write the state file"
    assert slf._log_filter.total == 50


# ---------------------------------------------------------------------------
#  11. upgrade notification
# ---------------------------------------------------------------------------

def config_options():
    """The real, user-facing configuration options."""
    return set(slf.Config.get_field_annotations())


def test_every_config_option_has_a_description():
    """Coverage invariant for the notes shown when a config gains new options.

    Adding an option without recording its version would make the upgrade notice
    announce a blank entry, so this fails until the option is described — in *every*
    shipped language, since the sentence itself now comes from the catalogues.
    """
    documented = set(slf.CONFIG_DOC)
    assert config_options() == documented, (
        "undocumented options: {}; stale entries: {}".format(
            config_options() - documented, documented - config_options()
        )
    )
    for name, (since, doc_key) in slf.CONFIG_DOC.items():
        assert re.match(r"^\d+\.\d+\.\d+$", since), "{}: bad version {!r}".format(name, since)
        for language in i18n.available_languages():
            text = i18n.translate(doc_key, language)
            assert text != doc_key, "{}: no {} description for {}".format(
                name, language, doc_key
            )
            assert text.strip(), "{}: empty description in {}".format(name, language)


def test_the_generated_config_has_no_comment_fields():
    """The config must stay plain — no bookkeeping keys mixed in with user settings.

    An earlier revision injected ``#option`` fields to imitate comments; it made the
    file look complicated, so it was removed. Nothing but real options may ship.
    """
    data = slf.Config().serialize()
    assert set(data) == config_options()
    assert not [k for k in data if k.startswith("#")]


def test_migrator_reports_the_options_that_were_missing():
    slf._newly_added_options = []
    raw = {"patterns": ["x"]}                      # a 1.0.x era config
    slf._config_migrator(raw)
    assert slf._newly_added_options == [
        "language", "log_matched_lines", "report_on_server_stop",
        "warn_about_stale_rules", "stale_rule_threshold", "validate_patterns",
        "pattern_probe_timeout_ms", "announce_config_upgrade", "announce_broken_config",
    ]


def test_migrator_says_nothing_when_the_config_is_complete():
    slf._newly_added_options = ["stale"]
    assert slf._config_migrator(slf.Config().serialize()) is False
    assert slf._newly_added_options == []


def test_migrator_never_asks_for_a_write():
    """Filling in the missing options is MCDR's job; the hook only observes."""
    slf._newly_added_options = []
    assert slf._config_migrator({"patterns": ["x"]}) is False


def test_migrator_tolerates_a_non_dict_config():
    """A malformed file must not crash the loader before MCDR can regenerate it."""
    assert slf._config_migrator(["not", "a", "dict"]) is False


def test_on_load_wires_the_migrator_into_config_loading():
    """Guard the wiring, not just the function — a correct function that is not
    connected does nothing."""
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)
    assert server.load_kwargs.get("data_processor") is slf._config_migrator


def test_reload_command_also_migrates():
    server = start_plugin(config={"patterns": [LIVE]})
    server.load_kwargs = {}
    server.config_data = {"patterns": [LIVE]}       # a stale, partial config appears
    slf._apply_config(server)
    assert server.load_kwargs.get("data_processor") is slf._config_migrator


def announce_output(server):
    return "\n".join(server.logger.infos)


def test_upgrade_announcement_lists_options_with_versions():
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)
    text = announce_output(server)
    assert "配置已更新" in text
    for name in ("warn_about_stale_rules", "stale_rule_threshold",
                 "validate_patterns", "pattern_probe_timeout_ms"):
        assert name in text
    assert "v1.1.0 加入" in text


def test_upgrade_announcement_explains_each_new_option():
    """Naming the option is not enough — say what it does."""
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)
    text = announce_output(server)
    for name, (_, doc_key) in slf.CONFIG_DOC.items():
        if name in ("patterns", "log_matched_lines", "report_on_server_stop"):
            continue
        # the sentence shown must be the catalogue's, in the language in force
        expected = i18n.translate(doc_key, "zh_cn").split("。")[0]
        assert expected in text, "{}: description missing".format(name)


def test_no_announcement_when_the_config_is_already_current():
    server = start_plugin(config=slf.Config().serialize())
    server.logger.infos.clear()
    slf.on_load(server, None)
    assert "配置已更新" not in announce_output(server)


def test_announcement_is_logged_exactly_once():
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)
    assert announce_output(server).count("配置已更新") == 1


def test_upgrade_announcement_can_be_switched_off():
    """``announce_config_upgrade = false`` silences the notice — and nothing else.

    The switch governs the *speaking*, not the filling-in: MCDR still writes the
    missing options into the file (that part is not ours to gate), the admin just
    stops being told about it at every load.
    """
    server = FakePluginServer(
        config_data={"patterns": [LIVE], "announce_config_upgrade": False}
    )
    slf.on_load(server, None)

    assert "配置已更新" not in announce_output(server)
    # the config still got completed — only the announcement is gone
    assert set(slf._config.serialize()) == config_options()
    assert slf._newly_added_options, "the missing options were still detected"


def test_the_upgrade_switch_defaults_to_on():
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)
    assert slf._config.announce_config_upgrade is True


def test_the_upgrade_switch_survives_a_reload():
    """Reloading with the switch off must stay quiet the second time too."""
    config = {"patterns": [LIVE], "announce_config_upgrade": False}
    server = start_plugin(config=config)
    server.logger.infos.clear()

    server.config_data = dict(config, validate_patterns=False)
    slf._apply_config(server)

    assert "配置已更新" not in announce_output(server)
    assert slf._newly_added_options, "the reload should still have found new options"


# ---------------------------------------------------------------------------
#  12. a broken config file is preserved, not silently overwritten
# ---------------------------------------------------------------------------

# What a user typically produces by hand-adding a rule and forgetting the comma.
BROKEN_JSON = '''{
    "patterns": [
        "standing on air - force-sending blocks below"
        "my-own-rule"
    ],
    "report_on_server_stop": true
}
'''


def server_with_config_file(tmp_path, text):
    """A fake server whose data folder holds ``text`` as config.json."""
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / "config.json").write_text(text, encoding="utf-8")
    return FakePluginServer(data_folder=str(folder)), folder


def test_parse_error_truncated():
    """"line"/"column" must survive into the reported reason: that is the part
    that tells a user where to look."""
    import json

    try:
        json.loads(BROKEN_JSON)
    except ValueError as error:
        reason = "JSON 语法错误：{}".format(error)
    else:
        raise AssertionError("the fixture is supposed to be invalid JSON")
    assert "line" in reason and "column" in reason, reason


def test_broken_config_is_backed_up_before_being_regenerated(tmp_path):
    server, folder = server_with_config_file(tmp_path, BROKEN_JSON)

    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    backup = folder / "config.json.old"
    assert backup.is_file(), "the user's file must be preserved"
    assert backup.read_text(encoding="utf-8") == BROKEN_JSON, "byte for byte"
    assert not (folder / "config.json").exists(), "the original is moved aside"


def test_backup_announcement_says_what_where_and_why(tmp_path):
    server, folder = server_with_config_file(tmp_path, BROKEN_JSON)

    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert len(server.logger.errors) == 1
    text = server.logger.errors[0]
    assert "配置文件无法解析" in text
    assert "已重置为默认配置" in text
    assert "config.json.old" in text, "the user must be told where the backup is"
    assert "JSON 语法错误" in text, "and why it failed"
    assert "line" in text and "column" in text, "and where in the file"
    assert "逗号" in text, "plus the likely cause for this very common mistake"


def test_a_valid_config_is_left_alone(tmp_path):
    server, folder = server_with_config_file(
        tmp_path, json.dumps({"patterns": ["x"]}, indent=4)
    )

    assert slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME) is None
    assert not (folder / "config.json.old").exists()
    assert server.logger.errors == []


def test_a_missing_config_is_not_an_error(tmp_path):
    """First run: there is nothing to back up and nothing to complain about."""
    folder = tmp_path / "empty"
    folder.mkdir()
    server = FakePluginServer(data_folder=str(folder))

    assert slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME) is None
    assert server.logger.errors == []
    assert not (folder / "config.json.old").exists()


def test_an_empty_config_file_is_treated_as_broken(tmp_path):
    """A truncated file is a real way to lose a config."""
    server, folder = server_with_config_file(tmp_path, "")

    assert slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME) is not None
    assert (folder / "config.json.old").is_file()


@pytest.mark.parametrize("text", ['["a", "b"]', '"just a string"', "42"])
def test_json_that_is_not_an_object_is_also_quarantined(tmp_path, text):
    """MCDR would reject these too, so they must not be silently replaced."""
    server, folder = server_with_config_file(tmp_path, text)

    reason = slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert reason is not None and "顶层应为 JSON 对象" in reason
    assert (folder / "config.json.old").read_text(encoding="utf-8") == text


def test_a_second_failure_overwrites_the_previous_backup(tmp_path):
    """One ``.old`` slot: the most recent broken file is the one worth reading."""
    server, folder = server_with_config_file(tmp_path, BROKEN_JSON)
    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    (folder / "config.json").write_text("{ even more broken", encoding="utf-8")
    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert (folder / "config.json.old").read_text(encoding="utf-8") == "{ even more broken"


def test_on_load_checks_the_config_file():
    """Wiring guard: the check has to run on the real load path."""
    checked = []
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    original = slf._quarantine_broken_config

    def spy(srv, name, data=None):
        checked.append(name)
        return original(srv, name, data)

    slf._quarantine_broken_config = spy
    try:
        slf.on_load(server, None)
    finally:
        slf._quarantine_broken_config = original
    assert checked == [slf.CONFIG_FILE_NAME]


def test_reload_command_checks_the_config_file_too():
    server = start_plugin(config={"patterns": [LIVE]})
    checked = []
    original = slf._quarantine_broken_config

    def spy(srv, name, data=None):
        checked.append(name)
        return original(srv, name, data)

    slf._quarantine_broken_config = spy
    try:
        slf._apply_config(server)
    finally:
        slf._quarantine_broken_config = original
    assert checked == [slf.CONFIG_FILE_NAME]


def test_quarantine_survives_an_unwritable_location(tmp_path, monkeypatch):
    """If even the backup fails, the plugin must still load rather than crash."""
    server, folder = server_with_config_file(tmp_path, BROKEN_JSON)

    def boom(src, dst):
        raise OSError("simulated")

    monkeypatch.setattr(slf.os, "replace", boom)
    reason = slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert reason is not None, "the problem is still reported"
    assert any("备份" in m for m in server.logger.errors)


# ---------------------------------------------------------------------------
#  12b. the notice switches for the broken-config path
# ---------------------------------------------------------------------------

def _broken_config_mentioning(line):
    """``BROKEN_JSON`` with an extra line at the top — still unparseable.

    The switch has to end up in the *raw text*: when the file cannot be parsed,
    that text is the only place the admin's choice can be read from.
    """
    return BROKEN_JSON.replace("{\n", "{\n    " + line + "\n", 1)


def test_the_broken_config_switch_is_a_real_option():
    """Drift guard: the name the quarantine peeks for must exist on Config.

    Renaming the field without renaming the constant would leave the switch
    silently dead — it would be written to the file and never looked at.
    """
    assert slf.BROKEN_CONFIG_NOTICE_OPTION in slf.Config.get_field_annotations()


def test_broken_config_notice_can_be_switched_off(tmp_path):
    text = _broken_config_mentioning('"announce_broken_config": false,')
    server, folder = server_with_config_file(tmp_path, text)

    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert server.logger.errors == [], "the notice was supposed to be silenced"
    assert (folder / "config.json.old").is_file(), "the backup happens regardless"
    assert not (folder / "config.json").exists()


@pytest.mark.parametrize(
    "line, announced",
    [
        ('"announce_broken_config": false,', False),
        ('"announce_broken_config" :  false ,', False),   # layout is free-form
        ('"announce_broken_config": FALSE,', False),      # a hand-written capital
        ('"announce_broken_config": true,', True),
        ('"announce_broken_config": "false",', True),     # a string is not a boolean
        ('"announce_broken_configx": false,', True),      # a longer name is not a match
        (None, True),                                     # absent -> default (on)
    ],
)
def test_the_switch_is_read_leniently_from_the_raw_file(tmp_path, line, announced):
    text = BROKEN_JSON if line is None else _broken_config_mentioning(line)
    server, folder = server_with_config_file(tmp_path, text)

    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert bool(server.logger.errors) is announced, server.logger.errors
    assert (folder / "config.json.old").is_file(), "the backup always happens"


def test_raw_bool_option_finds_nothing_when_absent():
    assert slf._raw_bool_option('{"a": true}', "b") is None


def test_a_failed_backup_is_reported_even_with_the_notice_off(tmp_path, monkeypatch):
    """Silencing a notice must not silence a *data-loss* error.

    When the file cannot even be moved aside, MCDR is about to overwrite it in
    place. That is not routine chatter, so it gets through the switch on purpose.
    """
    text = _broken_config_mentioning('"announce_broken_config": false,')
    server, _ = server_with_config_file(tmp_path, text)

    def boom(src, dst):
        raise OSError("simulated")

    monkeypatch.setattr(slf.os, "replace", boom)
    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert any("备份" in m for m in server.logger.errors), server.logger.errors


# ---------------------------------------------------------------------------
#  12. language — the plugin speaks the admin's language
#
#  Every message lives in server_log_filter/lang/<code>.json; the ``language``
#  option picks one, defaulting to ``auto`` (follow MCDR). These tests cover the
#  resolution rules, the wiring into every surface that speaks, and the structural
#  invariants that keep the catalogues honest.
# ---------------------------------------------------------------------------

REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE_FILES = ["server_log_filter/__init__.py", "server_log_filter/i18n.py"]


def catalogue(language):
    return i18n.get_catalog(language).messages


def placeholders(template):
    """The ``{}`` field names a template uses, e.g. ``{"count"}``."""
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def keys_referenced_in_the_code():
    """Every catalogue key the plugin's own source asks for.

    Parsed rather than grepped: a dotted literal such as ``"state.json"`` inside a
    docstring is not a message key, and only real call sites should count.
    """
    keys = set()
    for relative in SOURCE_FILES:
        tree = ast.parse((REPO / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            target = node.func
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name not in ("_t", "say", "translate"):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.add(first.value)
    keys |= {doc_key for _, doc_key in slf.CONFIG_DOC.values()}
    # Never a literal at a call site: i18n.resolve() hands it back as a note key.
    keys.add(i18n.UNSUPPORTED_KEY)
    return keys


def test_the_language_option_is_a_real_option_defaulting_to_auto():
    assert slf.Config().language == "auto"
    assert slf.LANGUAGE_OPTION in config_options()


def test_auto_follows_mcdr():
    """The documented default: MCDR says en_us, so the plugin does too."""
    server = start_plugin(config={"patterns": [LIVE]}, mcdr_language="en_us")
    assert slf._language == "en_us"
    assert "Enabled 1 log filter rule" in announce_output(server)
    assert server.help_messages == [
        ("!!logfilter", i18n.translate("help.logfilter", "en_us"))
    ]


def test_auto_follows_mcdr_in_the_other_direction():
    """Stated separately on purpose: a fallback that happens to be en_us would
    otherwise make ``auto`` look like it works while ignoring MCDR entirely."""
    server = start_plugin(config={"patterns": [LIVE]}, mcdr_language="zh_cn")
    assert slf._language == "zh_cn"
    assert "已启用 1 条日志过滤规则" in announce_output(server)


def test_auto_is_silent_about_an_mcdr_language_we_do_not_ship():
    """``auto`` is a deliberate deferral, so an unsupported MCDR language is not an error.

    The admin never asked for a specific language; nagging them about the one MCDR
    happens to be set to would be noise on every single load.
    """
    server = start_plugin(config={"patterns": [LIVE]}, mcdr_language="ja_jp")
    assert slf._language == i18n.FALLBACK_LANGUAGE
    assert server.logger.warnings == [], server.logger.warnings


def test_an_explicit_language_overrides_mcdr():
    server = start_plugin(
        config={"patterns": [LIVE], "language": "zh_cn"}, mcdr_language="en_us"
    )
    assert slf._language == "zh_cn"
    assert "已启用 1 条日志过滤规则" in announce_output(server)


@pytest.mark.parametrize(
    "written, expected",
    [
        ("EN_us", "en_us"),           # case is free
        ("zh-CN", "zh_cn"),           # a hyphen is as good as an underscore
        ("  en  ", "en_us"),          # surrounding space, and a bare language code
        ("zh", "zh_cn"),
        ("zh_TW", "zh_cn"),           # no zh_tw file: the sibling script is closer
    ],
)
def test_language_codes_are_matched_leniently(written, expected):
    server = start_plugin(config={"patterns": [LIVE], "language": written})
    assert slf._language == expected, written
    assert server.logger.warnings == [], "a value we understood must not be complained about"


def test_an_unknown_language_falls_back_and_says_so():
    server = start_plugin(config={"patterns": [LIVE], "language": "klingon"})
    assert slf._language == i18n.FALLBACK_LANGUAGE
    warnings = "\n".join(server.logger.warnings)
    assert "klingon" in warnings, "the admin must be told their value was ignored"
    assert "en_us" in warnings and "zh_cn" in warnings, "and what the valid values are"


def only_these_catalogues(monkeypatch, files):
    """Point the catalogue loader at an in-memory set of language files.

    ``files`` maps a language code to the *text* of its catalogue. This substitutes the
    package-resource loader rather than a directory on disk, which is the code path a
    packed ``.mcdr`` uses — MCDR imports the plugin straight out of the zip, so the
    catalogues are read through the loader there, never through ``open()``.
    """

    def reader(relative):
        language = relative.rsplit("/", 1)[-1][: -len(".json")]
        text = files.get(language)
        return None if text is None else text.encode("utf-8")

    monkeypatch.setattr(i18n, "_resource_bytes", reader)
    monkeypatch.setattr(
        i18n, "_list_lang_files", lambda: sorted("{}.json".format(k) for k in files)
    )
    i18n.clear_cache()


def test_a_broken_catalogue_falls_back_and_is_reported(monkeypatch):
    """A contributor's broken translation must not break the server.

    ``zh_cn`` is the one that fails, and ``zh_cn`` is what is selected, so every
    message falls back to the readable ``en_us`` catalogue — and the admin is told
    which file to fix.
    """
    only_these_catalogues(
        monkeypatch, {"en_us": json.dumps(catalogue("en_us")), "zh_cn": "{ not json"}
    )
    try:
        server = start_plugin(config={"patterns": [LIVE], "language": "zh_cn"})
        assert slf._language == "zh_cn"
        warnings = "\n".join(server.logger.warnings)
        assert "zh_cn.json" in warnings, warnings
        # messages fall back to the catalogue that does load, rather than showing keys
        assert "Enabled 1 log filter rule" in announce_output(server)
    finally:
        i18n.clear_cache()


def test_two_broken_catalogues_still_do_not_break_the_plugin(monkeypatch):
    """With nothing readable left, the message degrades to its key name.

    That is the deliberate last resort: the key is descriptive on purpose, and it
    beats both an exception and a blank line.
    """
    only_these_catalogues(monkeypatch, {"en_us": "{ not json", "zh_cn": "{ not json"})
    try:
        server = start_plugin(config={"patterns": [LIVE], "language": "zh_cn"})
        assert slf._log_filter is not None, "the plugin still loaded"
        assert "summary.rules_enabled" in announce_output(server)
    finally:
        i18n.clear_cache()


def test_the_packed_plugin_can_still_read_its_catalogues(tmp_path):
    """The failure mode that makes all of this worth pinning.

    MCDR imports a packed plugin **out of the zip**, so ``__file__`` points inside the
    ``.mcdr`` and anything that opens a file next to it fails. When that happens the
    plugin still starts and still filters — it just quietly shows raw keys to every
    user, which no ordinary test would notice. So: build the real artifact, import it
    through the real loader in a subprocess, and require a real sentence to come out.
    """
    pack = importlib.import_module("pack")
    artifact = tmp_path / "ServerLogFilter.mcdr"
    pack.build(artifact)

    script = (
        "import sys\n"
        "sys.path.insert(0, {path!r})\n"
        "import server_log_filter as slf\n"
        "from server_log_filter import i18n\n"
        "print('loaded from', slf.__file__)\n"
        "print('languages', i18n.available_languages())\n"
        "print(i18n.translate('summary.rules_enabled', 'zh_cn', count=2))\n"
        "print(i18n.translate('summary.rules_enabled', 'en_us', count=2))\n"
    ).format(path=str(artifact))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(REPO / ".testlibs")},
    )

    assert proc.returncode == 0, proc.stderr
    assert ".mcdr" in proc.stdout, "the artifact itself must have been imported"
    assert "['en_us', 'zh_cn']" in proc.stdout, proc.stdout
    assert "已启用 2 条日志过滤规则" in proc.stdout, proc.stdout
    assert "Enabled 2 log filter rule(s)" in proc.stdout, proc.stdout


def test_the_language_switches_on_reload_without_a_restart():
    server = start_plugin(config={"patterns": [LIVE], "language": "zh_cn"})
    assert slf._language == "zh_cn"

    server.config_data = {"patterns": [LIVE], "language": "en_us"}
    source = FakeSource()
    slf._reload(source)
    assert slf._language == "en_us"
    assert "Filter rules reloaded" in "".join(str(x) for x in source.replies)


def test_mcdr_language_is_read_from_the_config_dict_when_the_method_is_absent():
    """Belt and braces for an MCDR old enough to lack ``get_mcdr_language()``."""
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    server.get_mcdr_language = None
    server.get_mcdr_config = lambda: {"language": "en_us"}
    slf.on_load(server, None)
    assert slf._language == "en_us"


def test_a_failing_mcdr_language_lookup_does_not_take_the_plugin_down():
    server = FakePluginServer(config_data={"patterns": [LIVE]})

    def boom():
        raise RuntimeError("no language for you")

    server.get_mcdr_language = boom
    server.get_mcdr_config = boom
    slf.on_load(server, None)

    assert slf._language == i18n.FALLBACK_LANGUAGE
    assert slf._log_filter is not None, "the plugin still loaded"


def test_the_broken_config_notice_uses_the_language_from_the_raw_file(tmp_path):
    """The file is unparseable, so its own text is the only source for the language.

    MCDR here is zh_cn; the admin's broken file says en_us. Reporting the reset in
    Chinese would be exactly backwards — that message is the one that has to be read.
    """
    text = _broken_config_mentioning('"language": "en_us",')
    server, _ = server_with_config_file(tmp_path, text)     # MCDR language: zh_cn
    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert any("cannot be parsed" in m for m in server.logger.errors), server.logger.errors
    assert not any("无法解析" in m for m in server.logger.errors)


def test_english_reaches_the_stale_rule_warning():
    server = start_plugin(
        config={"patterns": [IDLE], "stale_rule_threshold": 1, "language": "en_us"}
    )
    run_session(server)
    slf.on_server_start(server)
    slf.on_server_startup(server)

    text = warnings_of(server)
    assert "have matched nothing for 1 or more consecutive sessions" in text
    assert "!!logfilter test" in text, "the advice has to survive translation"


def test_english_reaches_the_commands_and_the_run_summary():
    server = start_plugin(config={"patterns": [LIVE], "language": "en_us"}, state=None)

    status = FakeSource()
    slf._show_status(status)
    assert "Rules:" in "".join(str(x) for x in status.replies)

    hit = FakeSource()
    slf._test_line(hit, {"text": "Player Steve " + LIVE})
    assert "would be hidden" in "".join(str(x) for x in hit.replies)

    miss = FakeSource()
    slf._test_line(miss, {"text": 'Done (0.648s)! For help, type "help"'})
    assert "would not be hidden" in "".join(str(x) for x in miss.replies)

    reset = FakeSource()
    slf._reset_streaks(reset)
    assert "Idle-session counters reset" in "".join(str(x) for x in reset.replies)

    # the end-of-run summary counts what the filter itself saw, so drive real
    # matches through it rather than poking the counters
    server.logger.infos.clear()
    slf.on_server_start(server)
    for _ in range(7):
        slf._log_filter.filter_server_info(FakeInfo("Player Steve " + LIVE))
    slf.on_server_stop(server, 0)
    assert any(
        "Hidden 7 server log line(s)" in m for m in server.logger.infos
    ), server.logger.infos


def test_english_reaches_the_prune_announcement_and_the_rule_errors():
    server = FakePluginServer(
        config_data={"patterns": [LIVE], "language": "en_us"},
        state_data={"session_index": 1, "rules": {"a-gone-rule": {}}},
        mcdr_language="en_us",
    )
    slf.on_load(server, None)

    assert any(
        "Cleared 1 rule statistic(s)" in m for m in server.logger.infos
    ), server.logger.infos

    bad = FakePluginServer(logger=RecordingLogger(), mcdr_language="en_us")
    slf._build_rules(bad, ["(unclosed"])
    assert any("failed to compile" in m for m in bad.logger.warnings), bad.logger.warnings

    slf._language = "en_us"
    danger = FakePluginServer(logger=RecordingLogger())
    slf._build_rules(danger, [r"(a+)+$"])
    assert any(
        "catastrophic backtracking" in m for m in danger.logger.errors
    ), danger.logger.errors


def test_english_reaches_the_upgrade_notice():
    server = FakePluginServer(
        config_data={"patterns": [LIVE], "language": "en_us"}, mcdr_language="en_us"
    )
    slf.on_load(server, None)

    text = announce_output(server)
    assert "Config updated" in text
    assert "added in v1.1.0" in text
    assert 'See the "Configuration" section' in text


# --- catalogue invariants ---------------------------------------------------

def test_every_shipped_language_file_loads():
    languages = i18n.available_languages()
    assert languages, "no language files were found"
    assert i18n.FALLBACK_LANGUAGE in languages, "the fallback must ship"
    for language in languages:
        assert i18n.get_catalog(language).error is None, language


def test_every_language_file_has_exactly_the_same_keys():
    """A language missing a key shows the key name to the user instead of a sentence."""
    languages = i18n.available_languages()
    reference = set(catalogue(i18n.FALLBACK_LANGUAGE))
    assert reference
    for language in languages:
        keys = set(catalogue(language))
        assert keys == reference, "{}: missing {}; unexpected {}".format(
            language, sorted(reference - keys), sorted(keys - reference)
        )


def test_every_translation_keeps_the_placeholders():
    """Dropping ``{count}`` turns a sentence into a lie, not just a typo."""
    reference = catalogue(i18n.FALLBACK_LANGUAGE)
    for language in i18n.available_languages():
        translated = catalogue(language)
        for key, template in reference.items():
            assert placeholders(translated[key]) == placeholders(template), (
                "{}: {} lost or gained a placeholder".format(language, key)
            )


def test_every_key_the_code_asks_for_exists_in_every_catalogue():
    keys = keys_referenced_in_the_code()
    assert keys, "the extractor found no keys at all -- it is broken, not the code"
    for language in i18n.available_languages():
        missing = sorted(k for k in keys if k not in catalogue(language))
        assert not missing, "{}: {}".format(language, missing)


def test_no_catalogue_key_is_left_unused():
    """Dead keys ship to every user and quietly stop being maintained."""
    unused = sorted(set(catalogue("en_us")) - keys_referenced_in_the_code())
    assert not unused, "unused catalogue keys: {}".format(unused)


def test_the_config_descriptions_are_exactly_the_options():
    described = {k for k in catalogue("en_us") if k.startswith("config_doc.")}
    expected = {"config_doc." + name for name in config_options()}
    assert described == expected, sorted(described ^ expected)


def test_the_english_catalogue_is_actually_english():
    """Catches a forgotten translation pasted straight back in."""
    for key, template in catalogue("en_us").items():
        cjk = [c for c in template if "\u4e00" <= c <= "\u9fff"]
        assert not cjk, "{} is not translated: {!r}".format(key, template)


# ---------------------------------------------------------------------------
#  14. 1.2.3: an undecodable config, the test-prefix trap, and !!logfilter
# ---------------------------------------------------------------------------


def server_with_raw_config(tmp_path, data):
    """A fake server whose data folder holds exactly ``data`` as config.json."""
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / "config.json").write_bytes(data)
    return FakePluginServer(data_folder=str(folder)), folder


def test_a_config_that_is_not_utf8_is_quarantined(tmp_path):
    """ANSI/GBK is what an editor on Chinese Windows writes by default.

    MCDR cannot parse such a file either, so it belongs on the same backup-and-reset path
    as a syntax error. Before the fix it raised UnicodeDecodeError — a ValueError, so the
    ``except OSError`` there did not catch it — and the plugin failed to load at all.
    """
    payload = '{"patterns": ["中文规则"]}'.encode("gbk")
    server, folder = server_with_raw_config(tmp_path, payload)
    slf._language = "zh_cn"

    reason = slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert reason is not None, "an undecodable file is a broken config"
    backup = folder / (slf.CONFIG_FILE_NAME + slf.CONFIG_BACKUP_SUFFIX)
    assert backup.read_bytes() == payload, "the original bytes are kept verbatim"
    assert any("UTF-8" in m for m in server.logger.errors), server.logger.errors
    assert not (folder / slf.CONFIG_FILE_NAME).exists(), "the file was moved aside"


def test_on_load_survives_a_config_that_is_not_utf8(tmp_path):
    """The decode error must not reach on_load: that is what killed the plugin."""
    payload = '{"patterns": ["中文规则"]}'.encode("gbk")
    server, folder = server_with_raw_config(tmp_path, payload)

    slf.on_load(server, None)  # must not raise

    assert slf._log_filter is not None, "the filter is still installed"
    assert (folder / (slf.CONFIG_FILE_NAME + slf.CONFIG_BACKUP_SUFFIX)).exists()
    assert len(slf._log_filter.rules) == 1, "the regenerated default config is in force"


def test_the_test_command_strips_the_console_prefix():
    """``^``-anchored rules are the trap here.

    The filter matches the body MCDR has already stripped, while admins paste the whole
    console line. Without stripping, ``!!logfilter test`` answers "no match" for a rule
    that works in practice — and the user goes off to break a rule that was correct.
    """
    start_plugin(config={"patterns": [r"^Player \w+ joined"]})
    hit = FakeSource()
    slf._test_line(
        hit, {"text": '[12:00:00] [Server thread/INFO]: Player Steve joined the game'}
    )

    text = "".join(str(x) for x in hit.replies)
    assert "命中规则" in text, text
    assert "不匹配" not in text, text
    assert "前缀" in text, "the reply must say the prefix was stripped: {}".format(text)


def test_the_test_command_stays_quiet_without_a_prefix():
    start_plugin(config={"patterns": [LIVE]})
    hit = FakeSource()
    slf._test_line(hit, {"text": "Player Steve " + LIVE})
    assert "前缀" not in "".join(str(x) for x in hit.replies)


def test_status_shows_the_language_and_where_it_came_from():
    """The 1.2.2 headline feature must be checkable without reading the config file."""
    start_plugin(config={"patterns": [LIVE], "language": "en_us"}, state=None)
    fixed = FakeSource()
    slf._show_status(fixed)
    assert "Language: en_us (set in the config)" in "".join(
        str(x) for x in fixed.replies
    )

    start_plugin(config={"patterns": [LIVE]}, state=None, mcdr_language="en_us")
    following = FakeSource()
    slf._show_status(following)
    assert "Language: en_us (following MCDR)" in "".join(
        str(x) for x in following.replies
    )


def test_status_reports_skipped_rules_and_the_last_hit():
    """Both used to be invisible: a rejected rule, and when a rule last fired."""
    start_plugin(
        config={"patterns": [LIVE, r"(a+)+$"]},
        state={"session_index": 3, "rules": {LIVE: {"last_hit_session": 2}}},
    )
    status = FakeSource()
    slf._show_status(status)

    text = "".join(str(x) for x in status.replies)
    assert "条规则已被跳过" in text, text
    assert "上次命中：第 2 个开服周期" in text, text


def test_a_pattern_that_overflows_is_skipped_not_fatal():
    """``a{999999999999}`` is a fat-fingered quantifier, not a re.error.

    ``re.compile`` raises OverflowError for it, which the old ``except re.error`` missed —
    so one typo took down ``on_load`` instead of just that one rule.
    """
    server = FakePluginServer()
    rules = slf._build_rules(server, [LIVE, "a{999999999999}", "joined the game"])

    assert [r.pattern for r in rules] == [LIVE, "joined the game"], "good rules survived"
    assert len(server.logger.warnings) == 1, server.logger.warnings
    assert "999999999999" in server.logger.warnings[0]


def test_a_deeply_nested_pattern_is_skipped_not_fatal():
    """Thousands of nested groups blow the C stack while compiling, not re.error."""
    pattern = "(" * 600 + "a" + ")" * 600
    server = FakePluginServer()

    rules = slf._build_rules(server, [LIVE, pattern])

    assert [r.pattern for r in rules] == [LIVE]
    assert len(server.logger.warnings) == 1


def test_a_template_with_a_bad_attribute_is_returned_raw():
    """The "a bad translation can never raise" promise has to include ``{a.b}``."""
    import server_log_filter.i18n as i18n

    i18n._CATALOGS["xx_broken"] = i18n.Catalog({"t": "{a.b}"}, None)
    try:
        assert i18n.translate("t", "xx_broken", a=1) == "{a.b}"
    finally:
        i18n._CATALOGS.pop("xx_broken", None)
