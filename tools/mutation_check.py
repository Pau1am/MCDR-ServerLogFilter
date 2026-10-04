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

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = "server_log_filter/__init__.py"
I18N = "server_log_filter/i18n.py"
LANG_ZH = "server_log_filter/lang/zh_cn.json"
LANG_EN = "server_log_filter/lang/en_us.json"

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

def inject_a_key_into_the_config(src):
    """往配置里塞一个非选项键（模拟「自己加注释」的诱惑）。

    配置里除了真正的选项不该有任何键，否则文件会变复杂——这正是注释字段被移除的原因。
    注入后它会被当成一个真字段，于是生成的 config.json 里会多出一项。
    """
    return src.replace(
        "def _config_option_names() -> List[str]:",
        'Config.__annotations__["#手写的注释"] = str\n'
        'setattr(Config, "#手写的注释", "x")\n'
        "\n"
        "\n"
        "def _config_option_names() -> List[str]:",
        1,
    )


def drop_config_descriptions(src):
    """让某个配置项不再有说明（升级提示里会变成「未记录说明」）。"""
    return src.replace(
        '    "patterns": (\n        "1.0.0",',
        '    "patterns_UNUSED": (\n        "1.0.0",',
        1,
    )


QUARANTINE_CALL = (
    "    _config_was_reset = _quarantine_broken_config(server, CONFIG_FILE_NAME, raw) is not None\n"
)

DOC_SUFFIX_LINE = '            suffix = "   （已连续 {} 次零命中）".format(entry.zero_streak)\n'
DOC_SUFFIX_GUARD = "        if entry.zero_streak > threshold:\n"

def drop_quarantine_call(src):
    """不再检查配置文件 —— 坏文件会被 MCDR 直接覆盖，用户的规则就此消失。"""
    return src.replace(
        QUARANTINE_CALL, "    _config_was_reset = False  # mutation: no quarantine\n"
    )

def drop_backup_move(src):
    """检测到了问题，但不真的备份。"""
    return src.replace(
        "        os.replace(path, backup)\n",
        "        pass  # mutation: no backup\n",
    )

def repeat_streak_for_every_rule(src):
    """恢复成「每条规则都重复一遍零命中次数」的旧格式。"""
    return src.replace(
        DOC_SUFFIX_GUARD,
        "        if True:  # mutation: always annotate\n",
    )

def silence_upgrade_announcement(src):
    """不再报告配置里新增了哪些选项（升级悄悄发生）。"""
    return src.replace(
        "    if config.announce_config_upgrade:\n        _announce_new_options(server)\n",
        "    pass  # mutation: upgrade not announced\n",
    )

def ignore_the_upgrade_switch(src):
    """无视 announce_config_upgrade —— 关了也照样播报。"""
    return src.replace(
        "    if config.announce_config_upgrade:\n",
        "    if True:  # mutation: upgrade switch ignored\n",
        1,
    )

def ignore_the_broken_config_switch(src):
    """无视 announce_broken_config —— 关了也照样播报。"""
    return src.replace(
        "    if _raw_bool_option(raw, BROKEN_CONFIG_NOTICE_OPTION) is False:\n",
        "    if False:  # mutation: broken-config switch ignored\n",
        1,
    )

def silence_reset_announcement(src):
    """不再提示配置已被重置（坏文件被悄悄换掉了）。"""
    anchor = '    server.logger.error(\n        "\\n".join('
    if anchor not in src:
        return src
    return src.replace(anchor, "    return reason  # mutation: reset not announced\n" + anchor, 1)


PRUNE_ON_LOAD = "    _announce_pruned(server, _prune_state(server, _state, _config.patterns))\n"
PRUNE_ON_RELOAD = "    pruned = [] if _state is None else _prune_state(server, _state, _config.patterns)\n"
PRUNE_CALL = "    removed = _forget_orphans(state, patterns)\n"
RESET_GUARD = "    if _config_was_reset:\n"

def drop_prune_on_load(src):
    """重载插件时不再清理已删除规则的统计（回到「要等一次完整开服周期」）。"""
    return src.replace(PRUNE_ON_LOAD, "    pass  # mutation: no prune on load\n")

def drop_prune_on_reload_command(src):
    """!!logfilter reload 不再清理已删除规则的统计。"""
    return src.replace(PRUNE_ON_RELOAD, "    pruned = []  # mutation: no prune on reload\n")

def prune_against_compiled_rules(src):
    """按「编译成功的规则」而不是「配置原文」判断孤立项 —— 会误删被安全探测拦下的规则。"""
    return src.replace(
        PRUNE_CALL,
        "    removed = _forget_orphans(state, [r.pattern for r in _log_filter.rules])  # mutation\n",
    )

def ignore_the_reset_guard(src):
    """配置刚被自动重置（写坏）时也照常清理 —— 会顺手抹掉用户还在的历史。"""
    return src.replace(RESET_GUARD, "    if False:  # mutation: reset guard ignored\n")


# --- language ---------------------------------------------------------------
#
# These exist because this feature has a nasty failure mode: getting it wrong does not
# crash anything, it just quietly stops talking to the user. Two of them mutate a
# language file rather than Python, which is why every entry below names its target.

LANGUAGE_ASSIGN = "    _language = _resolve_language(server, setting)\n"
MCDR_LOOKUP = "    choice = i18n.resolve(setting, _mcdr_language(server))\n"
NOTE_GUARD = "    if choice.note_key is not None and warn:\n"
CATALOG_GUARD = "    if problem is not None and warn:\n"
QUARANTINE_LANGUAGE = (
    "        server, _raw_str_option(raw, LANGUAGE_OPTION) or i18n.AUTO, warn=False\n"
)
LOADER_READ = (
    "    try:\n"
    "        data = pkgutil.get_data(__package__, relative)\n"
    "    except (OSError, ImportError, ValueError):\n"
    "        data = None\n"
)


def ignore_the_language_option(src):
    """无视配置里的 language —— 一律用回落语言说话。"""
    return src.replace(
        LANGUAGE_ASSIGN, "    _language = i18n.FALLBACK_LANGUAGE  # mutation\n"
    )


def ignore_the_mcdr_language(src):
    """auto 不再跟随 MCDR，直接落回默认语言。"""
    return src.replace(MCDR_LOOKUP, "    choice = i18n.resolve(setting, None)  # mutation\n")


def skip_the_catalogues(src):
    """不再经过加载器读语言文件 —— 打包成 .mcdr 后整包就只剩裸键名。"""
    return src.replace(LOADER_READ, "    data = None  # mutation: bypass the loader\n")


def silence_the_unknown_language_warning(src):
    """认不出的 language 取值被静默吞掉（用户会以为设置生效了）。"""
    return src.replace(NOTE_GUARD, "    if False:  # mutation\n", 1)


def silence_the_unreadable_catalogue_warning(src):
    """语言文件坏掉也不吭声。"""
    return src.replace(CATALOG_GUARD, "    if False:  # mutation\n", 1)


def ignore_the_language_in_a_broken_config(src):
    """写坏的配置里写的语言不再被读到 —— 播报会莫名其妙换一种语言。"""
    return src.replace(
        QUARANTINE_LANGUAGE, "        server, i18n.AUTO, warn=False,  # mutation\n"
    )


def drop_a_translation(src):
    """某个语言少了一条消息（用户会看到裸键名）。适用于语言文件而非 Python。"""
    data = json.loads(src)
    data.pop("status.tip", None)
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def drop_a_placeholder(src):
    """译文里漏掉 {count} —— 句子不再只是难看，而是说谎。"""
    data = json.loads(src)
    data["summary.rules_enabled"] = data["summary.rules_enabled"].replace("{count}", "")
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# (名称, 被改的文件, 改法, pytest 选择器)
MUTATIONS = [
    ("idle-rule warning disabled", SRC, disable_warning,
     ["tests/test_plugin.py", "-k", "idle or stale or streak"]),
    ("threshold changed from >= to >", SRC, threshold_off_by_one,
     ["tests/test_plugin.py", "-k", "threshold or idle_rule"]),
    ("'session finished starting' guard removed", SRC, drop_startup_guard,
     ["tests/test_plugin.py", "-k", "never_reached_startup"]),
    ("reload counter carry-over broken", SRC, break_carry_over,
     ["tests/test_plugin.py", "-k", "reload_does_not_fake"]),
    ("running-session flag lost on reload", SRC, drop_session_flag_carry_over,
     ["tests/test_plugin.py", "-k", "reload_carries_the_running_session"]),
    ("catastrophic-backtracking probe disabled", SRC, disable_probe,
     ["tests/test_plugin.py", "-k", "catastrophic or applies_the_probe or validate_patterns"]),
    ("warning moved before the Done line", SRC, warn_before_startup,
     ["tests/test_e2e.py", "-k", "arrives_after"]),
    ("a non-option key injected into the config", SRC, inject_a_key_into_the_config,
     ["tests/test_plugin.py", "-k", "no_comment_fields or generated_config_contains_only"]),
    ("an option lost its description", SRC, drop_config_descriptions,
     ["tests/test_plugin.py", "-k", "has_a_description"]),
    ("upgrade announcement silenced", SRC, silence_upgrade_announcement,
     ["tests/test_plugin.py", "-k", "announcement or announcement_lists"]),
    ("the upgrade switch ignored", SRC, ignore_the_upgrade_switch,
     ["tests/test_plugin.py", "-k", "switch"]),
    ("the broken-config switch ignored", SRC, ignore_the_broken_config_switch,
     ["tests/test_plugin.py", "-k", "switch or leniently"]),
    ("broken config no longer quarantined", SRC, drop_quarantine_call,
     ["tests/test_plugin.py", "-k", "broken_config or quarantine or on_load_checks"]),
    ("problem detected but file not backed up", SRC, drop_backup_move,
     ["tests/test_plugin.py", "-k", "backed_up_before"]),
    ("streak repeated for every rule again", SRC, repeat_streak_for_every_rule,
     ["tests/test_plugin.py", "-k", "no_annotation or does_not_repeat"]),
    ("config reset not announced", SRC, silence_reset_announcement,
     ["tests/test_plugin.py", "-k", "announcement_says_what_where_and_why"]),
    ("no pruning of deleted rules on plugin load", SRC, drop_prune_on_load,
     ["tests/test_plugin.py", "-k", "prunes_its_state_immediately or announces_what_it_removed"]),
    ("no pruning of deleted rules on !!logfilter reload", SRC, drop_prune_on_reload_command,
     ["tests/test_plugin.py", "-k", "prunes_its_state_on_the_reload_command"]),
    ("orphans judged against the compiled rules", SRC, prune_against_compiled_rules,
     ["tests/test_plugin.py", "-k", "rejected_by_the_safety_probe"]),
    ("history wiped when the config was auto-reset", SRC, ignore_the_reset_guard,
     ["tests/test_plugin.py", "-k", "reset_config_does_not_wipe"]),
    ("the language option ignored", SRC, ignore_the_language_option,
     ["tests/test_plugin.py", "-k",
      "explicit_language_overrides or leniently or switches_on_reload"]),
    ("auto no longer follows MCDR", SRC, ignore_the_mcdr_language,
     ["tests/test_plugin.py", "-k", "auto_follows_mcdr or idle or threshold"]),
    ("catalogues read from the filesystem, not the loader", I18N, skip_the_catalogues,
     ["tests/test_plugin.py", "-k", "packed_plugin_can_still_read"]),
    ("an unrecognised language value not reported", SRC, silence_the_unknown_language_warning,
     ["tests/test_plugin.py", "-k", "unknown_language"]),
    ("an unreadable catalogue not reported", SRC, silence_the_unreadable_catalogue_warning,
     ["tests/test_plugin.py", "-k", "broken_catalogue_falls_back"]),
    ("a broken config's language not read from its own text", SRC,
     ignore_the_language_in_a_broken_config,
     ["tests/test_plugin.py", "-k", "broken_config_notice_uses_the_language"]),
    ("a message missing from one translation", LANG_ZH, drop_a_translation,
     ["tests/test_plugin.py", "-k", "same_keys"]),
    ("a placeholder dropped from a translation", LANG_ZH, drop_a_placeholder,
     ["tests/test_plugin.py", "-k", "keeps_the_placeholders"]),
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
    # One pristine copy per file a mutation touches, so a mutation can be undone
    # without rebuilding the tree.
    baselines = {
        relative: (repo / relative).read_text(encoding="utf-8")
        for _, relative, _, _ in MUTATIONS
    }

    code, summary = pytest_ok(repo, ["tests/test_plugin.py", "-k",
                                     "stale or idle or threshold or reload_does_not_fake or "
                                     "never_reached_startup or catastrophic or migrator or "
                                     "no_comment_fields or has_a_description or "
                                     "generated_config_contains_only or prun or "
                                     "rejected_by_the_safety_probe or switch or "
                                     "leniently or raw_bool or language or english or "
                                     "auto or catalogue or translation or packed_plugin"])
    print("baseline (unmutated): exit={} {}".format(code, summary))
    if code != 0:
        print("baseline is not green — fix the tests first")
        return 1
    print()

    killed = 0
    for name, relative, mutate, selector in MUTATIONS:
        baseline = baselines[relative]
        mutated = mutate(baseline)
        if mutated == baseline:
            print("  !! {}: mutation did not apply (anchor moved?)".format(name))
            continue
        (repo / relative).write_text(mutated, encoding="utf-8")
        code, summary = pytest_ok(repo, selector)
        (repo / relative).write_text(baseline, encoding="utf-8")

        if code == 1:
            print("  caught   {:<52} {}".format(name, summary))
            killed += 1
        elif code in (4, 5):
            print("  HARNESS  {:<52} exit={} — selector matched nothing".format(name, code))
        else:
            print("  SURVIVED {:<52} exit={} {}".format(name, code, summary))

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
