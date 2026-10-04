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
PACK = "pack.py"

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
    "    _config_was_reset = _quarantine_broken_config(server, CONFIG_FILE_NAME, data) is not None\n"
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
        '    if _raw_bool_option(raw or "", BROKEN_CONFIG_NOTICE_OPTION) is False:\n',
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
    "        setting = _raw_str_option(raw, LANGUAGE_OPTION) or i18n.AUTO\n"
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
        QUARANTINE_LANGUAGE, "        setting = i18n.AUTO  # mutation: ignore the file\n"
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


def raise_on_an_undecodable_config(src):
    """不再把「不是 UTF-8」当成配置写坏 —— 解码错误会一路冒到 on_load，插件加载不了。"""
    return src.replace(
        '    try:\n'
        '        return data.decode("utf-8")\n'
        '    except UnicodeDecodeError:\n'
        '        return None\n',
        '    return data.decode("utf-8")  # mutation: decode errors escape\n',
    )


def drop_the_probe_floor(src):
    """去掉探测预算的下限 —— 预算写成 0 时每一条规则都会被判成「太慢」而拒掉。"""
    return src.replace(
        "        probe_budget_ms = max(1, probe_budget_ms)\n",
        "        pass  # mutation: no floor\n",
    )


def drop_the_test_prefix_strip(src):
    """!!logfilter test 不再剥掉控制台前缀 —— `^` 锚定的规则会被误报成不命中。"""
    return src.replace(
        '    body = _LOG_LINE_PREFIX.sub("", text, count=1) or text\n',
        "    body = text  # mutation: no prefix strip\n",
    )


def narrow_the_compile_catch(src):
    """回到只接 re.error —— 一个打错的量化符就能把整个 on_load 带下去。"""
    return src.replace(
        "        except (re.error, OverflowError, RecursionError) as error:\n",
        "        except re.error as error:  # mutation: too narrow\n",
    )


def narrow_the_format_catch(src):
    """格式化失败不再兜住 AttributeError —— 一个 `{a.b}` 的笔误就能让消息路径抛异常。"""
    return src.replace(
        "    except (KeyError, IndexError, ValueError, AttributeError, TypeError):\n",
        "    except (KeyError, IndexError, ValueError):  # mutation: too narrow\n",
    )


def bare_command_shows_status_again(src):
    """裸命令又回到状态页 —— 「命令打错了」和「我要看状态」再次得到同一个回应。"""
    return src.replace(
        "        .runs(_show_help)\n",
        "        .runs(_show_status)  # mutation\n",
    )


def drop_the_command_alias(src):
    """!!lf 别名被去掉（只剩一种拼法）。"""
    return src.replace(
        "ROOT_LITERALS = (COMMAND, COMMAND_ALIAS)\n",
        "ROOT_LITERALS = (COMMAND,)  # mutation\n",
    )


def open_everything_up(src):
    """权限门形同虚设：任何玩家都能看到规则与日志内容。"""
    return src.replace(
        "    return source.has_permission(PermissionLevel.ADMIN)\n",
        "    return True  # mutation\n",
    )


def drop_the_click_events(src):
    """帮助行不再可点（游戏内点了没反应）。"""
    return src.replace(
        "        RText(command, RColor.aqua).set_click_event(action, click),\n",
        "        RText(command, RColor.aqua),  # mutation\n",
    )


def drop_the_permission_denial_explanation(src):
    """权限被拒时又只剩默认的「权限不足」——玩家会以为「我明明是 OP 啊」。"""
    return src.replace(
        "        .requires(_admin_only, _admin_denied_message)\n",
        "        .requires(_admin_only)  # mutation\n",
    )


def drop_the_version_from_the_title(src):
    """顶栏不再显示版本号。"""
    return src.replace(
        "    if version:\n"
        "        line.append(RText(\" v\" + version, RColor.yellow))\n",
        "    if False:  # mutation\n"
        "        line.append(RText(\" v\" + version, RColor.yellow))\n",
    )


def break_the_help_alignment(src):
    """帮助行不再补空格 —— 说明文字会跟着子命令长短左右乱跳。"""
    return src.replace(
        '    padding = " " * (_HELP_COMMAND_WIDTH - len(command))\n',
        '    padding = ""  # mutation\n',
    )


def bring_back_dark_text(src):
    """重新用上 dark_gray —— 深色终端上几乎看不见。"""
    return src.replace(
        '        RText(description, RColor.white),\n',
        '        RText(description, RColor.dark_gray),  # mutation\n',
    )


def drop_the_status_row_separator(src):
    """状态页的规则行不再有 ``--`` —— 与帮助页的形状就不一致了。"""
    return src.replace(
        '        parts.append(RText("-- ", RColor.gray))\n',
        '        parts.append(RText("", RColor.gray))  # mutation\n',
    )


def status_labels_lose_their_colour(src):
    """状态页的标签退回 gray —— 又和帮助页的 aqua 分成两套。"""
    return src.replace(
        '    return RTextList(RText(label, RColor.aqua), RText(value, value_colour))\n',
        '    return RTextList(RText(label, RColor.gray), RText(value, value_colour))  # mutation\n',
    )


def always_pad_the_rule_index(src):
    """编号永远补到两位 —— 9 条以内会出现 ``[ 1]`` 这种多余空格。"""
    return src.replace(
        "    index_width = len(str(len(rules)))\n",
        "    index_width = 2  # mutation\n",
    )


def ship_the_code_with_its_comments(src):
    """打包时不再剥注释 —— 包又变回「仓库什么样就发什么样」。"""
    return src.replace(
        "                zf.writestr(rel, packaged_source(path))\n",
        "                zf.writestr(rel, path.read_bytes())  # mutation\n",
    )


def strip_without_keeping_line_numbers(src):
    """剥注释时整行删掉而不是留空 —— 包内行号与仓库对不上，堆栈会指错行。"""
    return src.replace(
        "            lines[row - 1] = line[:col].rstrip() + newline\n",
        '            lines[row - 1] = ""  # mutation: shift line numbers\n',
    )


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
    ("an undecodable config treated as fatal", SRC, raise_on_an_undecodable_config,
     ["tests/test_plugin.py", "-k", "not_utf8"]),
    ("the probe budget floor removed", SRC, drop_the_probe_floor,
     ["tests/test_plugin.py", "-k", "nonsense_probe_budget"]),
    ("the console prefix no longer stripped", SRC, drop_the_test_prefix_strip,
     ["tests/test_plugin.py", "-k", "strips_the_console_prefix"]),
    ("the compile catch narrowed back to re.error", SRC, narrow_the_compile_catch,
     ["tests/test_plugin.py", "-k", "overflows_is_skipped or deeply_nested_is_skipped"]),
    ("the format catch narrowed again", I18N, narrow_the_format_catch,
     ["tests/test_plugin.py", "-k", "bad_attribute_is_returned"]),
    ("the bare command wired back to the status screen", SRC, bare_command_shows_status_again,
     ["tests/test_plugin.py", "-k", "bare_command_is_wired_to_the_help_screen"]),
    ("the !!lf alias dropped", SRC, drop_the_command_alias,
     ["tests/test_plugin.py", "-k", "both_spellings"]),
    ("the admin gate opened up", SRC, open_everything_up,
     ["tests/test_plugin.py", "-k", "open_to_players"]),
    ("the help lines made unclickable", SRC, drop_the_click_events,
     ["tests/test_plugin.py", "-k", "clickable"]),
    ("the permission denial explanation dropped", SRC,
     drop_the_permission_denial_explanation,
     ["tests/test_plugin.py", "-k", "not_vanilla_op"]),
    ("the version dropped from the title bar", SRC, drop_the_version_from_the_title,
     ["tests/test_plugin.py", "-k", "names_the_plugin_and_the_version or different_colour"]),
    ("the help rows no longer aligned", SRC, break_the_help_alignment,
     ["tests/test_plugin.py", "-k", "aligned_in_one_column"]),
    ("dark text brought back", SRC, bring_back_dark_text,
     ["tests/test_plugin.py", "-k", "no_dark_text"]),
    ("the status rows lost their separator", SRC, drop_the_status_row_separator,
     ["tests/test_plugin.py", "-k", "status_rule_rows_have_the_same_separator"]),
    ("the status labels lost their colour", SRC, status_labels_lose_their_colour,
     ["tests/test_plugin.py", "-k", "same_palette_as_help"]),
    ("the rule index always padded", SRC, always_pad_the_rule_index,
     ["tests/test_plugin.py", "-k", "single_digit_rule_index"]),
    ("comments shipped in the package again", PACK, ship_the_code_with_its_comments,
     ["tests/test_plugin.py", "-k", "carries_no_comments"]),
    ("stripping that shifts line numbers", PACK, strip_without_keeping_line_numbers,
     ["tests/test_plugin.py", "-k", "keeps_line_numbers"]),
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
