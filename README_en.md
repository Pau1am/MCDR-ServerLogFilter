# MCDR-ServerLogFilter

**Language / 语言:** **English** | [简体中文](README.md)

An MCDReforged plugin that **hides noisy server console lines from the MCDR console while leaving the server's own log file completely untouched.**

[![MCDR](https://img.shields.io/badge/MCDReforged-%3E%3D2.15-blue)](https://mcdreforged.com/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.9-blue)](https://www.python.org/)

---

## Why

MCDR echoes every line the server prints to its console. That is usually exactly what you want — but some server versions repeatedly print lines that carry no useful information at all, for example Minecraft 26.3:

```
[Server thread/INFO]: Player Steve standing on air - force-sending blocks below
```

This is an INFO line from the new "ghost block" auto-repair mechanism added in 26.3. It is a known **false positive** (Mojira [MC-311474](https://bugs.mojang.com/browse/MC-311474) / [MC-311727](https://bugs.mojang.com/browse/MC-311727)): it fires during ordinary jumping and movement, at most once per player every 10 seconds, and keeps spamming the log.

Editing the server's `log4j2.xml` also works, but that is a **global** change: get it wrong and the server may lose important log lines, and it only affects the server side. This plugin takes a different route — **it only touches MCDR**.

## Two key design decisions

### 1. Hide the echo, never discard the info

The plugin uses MCDR's `InfoActionFlag.hidden()`, **not** `discarded()`:

| | Console echo | Dispatched to plugin events | MCDR state detection |
|---|---|---|---|
| `hidden()` (this plugin) | ❌ hidden | ✅ intact | ✅ works |
| `discarded()` | ❌ hidden | ❌ lost | ⚠️ may break |

Keeping `process` means the line is **still dispatched to MCDR's info reactors and plugin events**. As a result, **even an overly broad rule cannot break MCDR's own "server startup / stop / player join-leave" detection** — the worst case is simply that you don't see the line on the console. This is the plugin's most important safety property.

### 2. The server's own log file is untouched

`server/logs/latest.log` is written by the server process itself through log4j, entirely independent of this plugin. **Not a single original record is lost.** So:

- Need the full raw log → read `server/logs/latest.log`
- Want a clean MCDR console / web panel → use this plugin

## Installation

1. Download `ServerLogFilter-vX.Y.Z.mcdr` from [Releases](../../releases), or build it yourself (see below)
2. Drop it into the `plugins/` folder of the MCDR instance:

```
<MCDR dir>/plugins/ServerLogFilter-vX.Y.Z.mcdr
```

Running several backends? Put one copy in each — configurations are independent.

3. Activate:

- MCDR already running: `!!MCDR reload plugin server_log_filter`
- Otherwise: just start the server

## Configuration

`config/server_log_filter/config.json` is generated on first load:

```json
{
    "patterns": [
        "standing on air - force-sending blocks below"
    ],
    "log_matched_lines": false,
    "report_on_server_stop": true,
    "warn_about_stale_rules": true,
    "stale_rule_threshold": 3,
    "validate_patterns": true,
    "pattern_probe_timeout_ms": 25
}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `patterns` | `string[]` | see above | Regex rules. Matched against the **body** of each line (MCDR has already stripped the `[time] [thread/level]:` prefix). A match hides the line |
| `log_matched_lines` | `bool` | `false` | Debug aid. When `true`, every hidden line is written to the MCDR log at INFO level |
| `report_on_server_stop` | `bool` | `true` | On server stop, log a summary of how many lines were hidden this run |
| `warn_about_stale_rules` | `bool` | `true` | Warn when a rule has matched nothing for several sessions — see below |
| `stale_rule_threshold` | `int` | `3` | How many consecutive idle sessions before warning. `0` disables the warning |
| `validate_patterns` | `bool` | `true` | Check rules for catastrophic backtracking at load time — see below |
| `pattern_probe_timeout_ms` | `int` | `25` | Time budget for that check, in milliseconds. Rarely needs changing |

> **Matching uses `re.search` (substring match), not full match.**
> Writing `standing on air` matches the whole line — no `.*` needed.
> The flip side: a `.` in your pattern matches any character, so use `\.` for a literal dot.

The config file is JSON and **does not support comments**.

### Idle-rule reminder

If a rule matches nothing for several consecutive sessions, it is most likely dead weight:
either the regex is wrong (typo, letter case, escaping), or the log line it targets is gone
for good (for instance because the upstream bug got fixed). The plugin says so once, right
after the server finishes starting (i.e. after the `Done` line):

```
==================================================================
[ServerLogFilter] 注意：有 1 条过滤规则连续 3 次及以上开服都没有命中
  · standing on air - force-sending blocks below
      已连续 3 次开服零命中；自启用以来从未命中过
  ...
==================================================================
```

Worth knowing:

- **This is not a performance feature.** All bookkeeping happens at server start/stop; the
  per-line filtering path gains no extra work. Its purpose is keeping the config honest.
- The history lives in `config/server_log_filter/state.json`, separate from the `config.json`
  you edit. The plugin never touches your config.
- If a rule is legitimately rare (fires once a month, say), raise `stale_rule_threshold`, or
  use `!!logfilter reset` to clear the counters and start observing again.
- Only sessions that actually **finished starting** are counted, so a server that fails to boot
  cannot make every rule look idle.

### Regex safety check

A regex with nested quantifiers (e.g. `(a+)+$`) backtracks exponentially when it fails to match,
which can freeze MCDR for hundreds of milliseconds — or seconds — **per line**. On load, the
plugin tries every rule against a few very short probe strings and refuses the dangerous ones
with a clear error:

```
[ServerLogFilter] 过滤规则存在灾难性回溯风险，已跳过: '(a+)+$' —— 在 22 字符的探测串上已耗时 90 ms（上限 25 ms）。
```

The check runs once at load; realistic patterns cost microseconds, and a refused rule never
affects the others. Leave `validate_patterns` on unless you know exactly what you are doing.

## Commands

| Command | Permission | Description |
|---|---|---|
| `!!logfilter` | user | Show status: rule count, per-rule hits and idle streaks |
| `!!logfilter list` | user | Same as above |
| `!!logfilter test <text>` | user | Check whether a line would be hidden, and which rule matches |
| `!!logfilter reload` | admin | Re-read the config file and apply it immediately |
| `!!logfilter reset` | admin | Clear the idle-session counters and start observing again |

`!!logfilter test` is especially handy: paste a line from your log to verify the rule without waiting for it to actually fire.

## Adding more rules

Just append the log body you want to filter to the `patterns` array:

```json
{
    "patterns": [
        "standing on air - force-sending blocks below",
        "Ignoring chat session from .* due to missing Services public key",
        "^\\[Async Chat Thread"
    ],
    "log_matched_lines": false,
    "report_on_server_stop": true
}
```

Verify with `!!logfilter test <text>` first, then `!!logfilter reload`.

A malformed regex will not crash the plugin — the rule is skipped with a warning in the MCDR log and the remaining rules keep working.

## Verification

### Unit tests (filtering logic, edge cases, safety properties)

- The target spam line is hidden while `process` is preserved (the key safety property)
- **15 classes of must-not-touch lines verified individually**: `Done (...)!`, player join/leave,
  `Stopping the server` / `Stopping server`, `moved too quickly`, `moved wrongly`,
  `Rejecting UseItemOnPacket`, `dropping items too fast`, chat signature warnings,
  `lost connection`, the version banner, `Preparing level`, death messages,
  `Saving and pausing game...`
- No crash on `""` / `None` / whitespace-only `content`
- Matches regardless of player name
- Multiple rules keep independent counts; `reload` resets counters
- An invalid regex is skipped with a warning and does not affect other rules
- Nearly-identical-but-different lines are **not** matched (proving it is not a blanket filter)
- **Idle-rule reminder**: fires only at the threshold, a single hit resets the streak, sessions that
  never finished starting are ignored, a plugin reload cannot fake an idle rule, it can be switched
  off, and `reset` clears the counters
- **Regex safety check**: four classes of catastrophic-backtracking pattern are refused, while eight
  realistic rule shapes all pass

### End-to-end (real MCDR + a fake server, full lifecycle)

Observed console echo (default rule):

| Log line | Result |
|---|---|
| `Player <any name> standing on air - force-sending blocks below` | ✅ hidden (0 occurrences) |
| `Steve moved too quickly!` / `moved wrongly!` | ✅ kept |
| `Steve joined the game` | ✅ kept |
| Any ordinary log line | ✅ kept |
| `Stopping server` | ✅ kept |

Plugin log:

```
Plugin server_log_filter@1.1.0 loaded
Enabled 1 log filter rule; matches are hidden from the MCDR console only, the server log is unaffected
Hidden 3 server log lines from the MCDR console this run (server log file unaffected)
```

**No errors at all.**

There is also a **canary-backed end-to-end case** guarding the "hidden but still dispatched" safety
property: it makes the filter *also* match the server-startup line, then asserts both that the line
left the console **and** that MCDR's `SERVER_STARTUP` event was still dispatched. That is what makes
`hidden()` and `discarded()` observably different — with the default rule alone, both look identical,
because noise lines never take part in lifecycle detection. See [tests/README.md](tests/README.md).

### Running the tests

The behaviour above is covered by an automated suite (`tests/`, 113 cases) that runs against a real
MCDR — **including the end-to-end group below**, which boots an actual MCDR instance, loads the
`.mcdr` produced by `pack.py`, and drives a fake server through a full lifecycle:

```bash
python -m pip install --target .testlibs -r tests/requirements-test.txt
PYTHONPATH=.testlibs python -m pytest tests -v      # Windows: $env:PYTHONPATH=".testlibs"
```

The suite asserts the plugin's key safety property: a hidden line **keeps `process` and only
loses `echo_to_console`** — that it **really is absent from the console**, and that **events are
still dispatched**. If a future MCDR release changes the semantics of `hidden()`, the tests fail
loudly instead of letting the plugin misbehave silently on your server.
The end-to-end group takes ~5–6 s (one MCDR boot shared by all cases); skip it with
`MCDR_SKIP_E2E=1`.
See [tests/README.md](tests/README.md) for details.

## Building from source

The repository follows MCDR's standard layout — root metadata plus a same-named code package:

```
MCDR-ServerLogFilter/
├── mcdreforged.plugin.json
├── LICENSE
├── README.md            ← not shipped
├── README_en.md         ← not shipped
├── CHANGELOG.md
├── pack.py
└── server_log_filter/
    └── __init__.py
```

Build with the included, allowlist-based packer:

```bash
python pack.py            # -> ServerLogFilter-v<version>.mcdr
```

The release artifact contains exactly **4 files** (about 14 KiB):

| File | Purpose |
|---|---|
| `mcdreforged.plugin.json` | plugin metadata (required) |
| `server_log_filter/__init__.py` | the plugin code |
| `CHANGELOG.md` | shipped changelog |
| `LICENSE` | MIT licence |

Neither README is packaged: MCDR never reads them, they duplicate the release page, and
dropping them halves the artifact (28 KB -> 14 KB).

> **Why an allowlist and not a skip list?** An earlier version of this section used
> `rglob("*")` with a short `skip` set, which is a *denylist*: any new file in the repo
> silently ends up in the release artifact. Two concrete consequences:
>
> 1. **The artifact would fail to load at all.** MCDR validates the root entries of a
>    `.mcdr` (`PackedPlugin._check_dir_legality`) and raises
>    `IllegalPluginStructure: Packed plugin cannot contain other module` for a root-level
>    `conftest.py` or `setup.py`. The test suite's `conftest.py` sits exactly there, so the
>    denylist approach ships a plugin that cannot be loaded.
> 2. **Runaway size.** After installing `.testlibs/` per `tests/README.md`, the denylist
>    bundled all of MCDR and its dependencies: measured at **1362 files / 7.11 MB**.
>
> `test_packaged_artifact_is_loadable` in `tests/test_plugin.py` runs `pack.py` and checks
> the result with MCDR's own validation, and `test_packager_ships_exactly_the_allowlist`
> pins the artifact's contents so the list cannot drift again.

## Requirements

- MCDReforged **>= 2.15.0**

  > Why not 2.13? The plugin's core design relies on `InfoActionFlag.hidden()`
  > (strip the console echo only, keep event dispatch), and that class was introduced in
  > **MCDR 2.15.0**. In 2.14.x and earlier, `InfoFilter` only supports
  > "return `False` to discard the whole line", which cannot reproduce this plugin's
  > behaviour, so those versions are not supported.
  > Installing on an older MCDR never fails silently — MCDR reports
  > `dependency mcdreforged@x.y.z does not satisfy version requirement >=2.15.0`.

- Python >= 3.9 (bundled with MCDR)

## Compatibility

| Dimension | Support |
|---|---|
| MCDR | **>= 2.15.0** |
| Minecraft | **Independent of the MC version.** 1.16.5 / 1.19.4 / 1.20.1 / 1.20.6 / 1.21.8 / 1.21.11 / 26.1 / 26.2 / 26.3 all tested against real servers |

Filtering happens on the MCDR side (matching each line of the server's stdout), so it does
not change with the MC version. The only version-dependent part is **what the default rule
targets**: `standing on air - force-sending blocks below` is only produced by **MC 26.3**.
On earlier versions the plugin still works — the default rule simply never matches anything,
and you can configure `patterns` to filter whatever noise your version emits.

### MCDR versions, measured

| MCDR | Result |
|---|---|
| 2.13.0 / 2.14.1 | Blocked by MCDR's own dependency check: `dependency mcdreforged@x.y.z does not satisfy version requirement >=2.15.0` |
| **2.15.0** (minimum) | Loading, filtering, idle-rule reminder, state file, and commands (including `!!logfilter reset`) all verified |
| 2.15.7 | Same — all verified |
| 2.16.0 | Same — all verified |

The reason the floor is 2.15.0 is explained under [Requirements](#requirements).
Every feature — including the idle-rule reminder and the regex safety check added in
v1.1.0 — behaves **identically** on these three versions (same warning timing, same
idle-streak bookkeeping, same state file, same command tree, no exceptions).

> The APIs the v1.1.0 features rely on (`save_config_simple`, `load_config_simple` with
> `file_name`, nested `Serializable`, `on_server_startup`) have all existed since
> **MCDR 2.13.0**, so **the minimum version requirement is unchanged at 2.15.0**.

## License

[MIT](LICENSE)
