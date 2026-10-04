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

## Is it safe?

**It will not break your server** — that is the property this plugin cares about most.

- **Lines are hidden from the console, not thrown away.**
  Filtered lines are still delivered to MCDR and to your other plugins, so **even an overly broad
  rule cannot affect MCDR's own "server startup / stop / player join-leave" detection** — the worst
  case is simply that you no longer see the line.
- **The server's own log file is untouched.**
  `server/logs/latest.log` is written by the server process itself, and **keeps every record**.

So: need the full raw log → read `server/logs/latest.log`; want a clean console / web panel →
use this plugin.

> Technically, the plugin uses `InfoActionFlag.hidden()` rather than `discarded()`: the former drops
> only the console echo and keeps event dispatch, while the latter throws the line away. That is
> where the "other plugins are unaffected" point above comes from.

## Installation

1. Download `ServerLogFilter-vX.Y.Z.mcdr` from [Releases](../../releases), or build it yourself (see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md))
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
    "language": "auto",
    "patterns": [
        "standing on air - force-sending blocks below"
    ],
    "log_matched_lines": false,
    "report_on_server_stop": true,
    "warn_about_stale_rules": true,
    "stale_rule_threshold": 3,
    "validate_patterns": true,
    "pattern_probe_timeout_ms": 25,
    "announce_config_upgrade": true,
    "announce_broken_config": true
}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `language` | `string` | `"auto"` | Message language. `auto` follows MCDR's own `language` setting; `zh_cn` / `en_us` pin it — see [Language](#language) |
| `patterns` | `string[]` | see above | Regex rules. Matched against the **body** of each line (MCDR has already stripped the `[time] [thread/level]:` prefix). A match hides the line |
| `log_matched_lines` | `bool` | `false` | Debug aid. When `true`, every hidden line is written to the MCDR log at INFO level |
| `report_on_server_stop` | `bool` | `true` | On server stop, log a summary of how many lines were hidden this run |
| `warn_about_stale_rules` | `bool` | `true` | Warn when a rule has matched nothing for several sessions — see below |
| `stale_rule_threshold` | `int` | `3` | How many consecutive idle sessions before warning. `0` disables the warning |
| `validate_patterns` | `bool` | `true` | Check rules for catastrophic backtracking at load time — see below |
| `pattern_probe_timeout_ms` | `int` | `25` | Time budget for that check, in milliseconds. Rarely needs changing |
| `announce_config_upgrade` | `bool` | `true` | Say which options were filled in after an upgrade — see below |
| `announce_broken_config` | `bool` | `true` | Say why a broken config was reset, and where the backup went — see below |

> **Matching uses `re.search` (substring match), not full match.**
> Writing `standing on air` matches the whole line — no `.*` needed.
> The flip side: a `.` in your pattern matches any character, so use `\.` for a literal dot.

> The config file is JSON, which **has no comment syntax** — each key is explained in the table above.

### Language

Every message a human reads — console notices, log lines, the replies to `!!logfilter` — lives
in a language file, and `language` picks which one:

| Value | Effect |
|---|---|
| `auto` *(default)* | **Follow MCDR's current language** (the `language` key in MCDR's `config.yml`) — set MCDR to Chinese and the plugin speaks Chinese, with nothing to configure |
| `zh_cn` | Simplified Chinese, whatever MCDR is set to |
| `en_us` | English, whatever MCDR is set to |

- Case and `-` / `_` are free: `zh-CN`, `EN_us` and `en` are all understood.
- A language with no file falls back to `en_us` and says so once in the log — silently
  switching language is harder to debug than a warning.
- MCDR set to `zh_tw` with no Traditional Chinese file yet resolves to `zh_cn`, matching
  MCDR's own fallback order.
- When the config file is broken, this value can only be read from **your own file text**
  (the config has already failed to parse), so it works even if it sits after the syntax
  error — the "your config was reset" notice is written in the language it names.
- Save the file and run `!!logfilter reload`; no restart needed.

> **`auto` reads MCDR's *current* `language` setting from `config.yml` — it is not a
> built-in default.** Set MCDR to `zh_cn` and the plugin speaks Chinese with no
> configuration at all. The flip side: only if you have **never changed** MCDR's language
> (MCDR ships with `en_us`) will the notices turn English after upgrading to 1.2.2 — set
> `language` to `zh_cn` if you would rather keep the Chinese ones.

**Want to add a language?** Copy `en_us.json`, rename it to `<code>.json`
(`ja_jp.json`, `zh_tw.json`, …), translate the *values*, run `pytest tests -k language`,
and open a pull request — **no code changes needed**. The files live in
`server_log_filter/lang/`, and the `README.md` next to them is written for translators.
`en_us` is the fallback catalogue, so please keep it complete.

### Upgrades fill in new options, and say so

After a plugin update, options missing from your config are **filled in from their defaults**
(MCDR already did this — your existing values are never overwritten). What is new is that the
plugin now reports it in the log:

```
==================================================================
[ServerLogFilter] Config updated: 4 new option(s) were added and written with their default values
  - warn_about_stale_rules  (added in v1.1.0)
      ...
  Config file: config/server_log_filter/config.json
  See the "Configuration" section of the README for what each one does.
==================================================================
```

Prefer quiet? Set `announce_config_upgrade` to `false` — see "Turning the notices off" below.

One side note: a **mistyped** option name is also treated as redundant and removed. Your config
therefore never accumulates dead entries — but a typo fails silently rather than erroring, so run
`!!logfilter` once after editing to confirm the change took effect.

### What happens if you break the config file

JSON is strict, and hand-adding a rule to `patterns` while **dropping a comma** breaks parsing.
The plugin checks the file itself before handing it to MCDR. When something is wrong it:

1. renames the offending file to `config.json.old` (nothing is lost),
2. regenerates a default `config.json`, and the plugin keeps working,
3. tells you plainly what happened:

```
==================================================================
[ServerLogFilter] The config file cannot be parsed; it has been reset to the default config
  - The file at fault: config/server_log_filter/config.json
  - Your original file was saved as: config/server_log_filter/config.json.old
  - What went wrong: JSON syntax error: Expecting ',' delimiter: line 4 column 9 (char 83)
  - A new config.json was generated with the default values. Fix the content against your backup and put it back,
    or run !!logfilter reload once you have fixed it.
  Usual cause: every element of the patterns array needs a comma "," between them,
              and there must be no trailing comma after the last one.
==================================================================
```

**The line and column point straight at the problem.** Fix it, then run `!!logfilter reload` —
no restart needed.

> Why the plugin does this itself: on a parse failure MCDR **overwrites the file with defaults**.
> Without moving the original aside first, your rules would simply be gone.
> `config.json.old` is a single backup slot — a later failure replaces it.

Also treated as broken: an empty file, or a file whose top level is not a JSON object
(say, the whole file is an array).

To skip that report, set `announce_broken_config` to `false`. **The backup still happens** —
only the message is silenced. The one exception is the "even the backup failed" error, which
announces imminent data loss and always gets through.

### Idle-rule reminder

If a rule matches nothing for several consecutive sessions, it is most likely dead weight:
either the regex is wrong (typo, letter case, escaping), or the log line it targets is gone
for good (for instance because the upstream bug got fixed). The plugin says so once, right
after the server finishes starting (i.e. after the `Done` line):

```
==================================================================
[ServerLogFilter] Heads up: 2 filter rule(s) have matched nothing for 3 or more consecutive sessions
  · standing on air - force-sending blocks below
  · test12345   (idle for 9 sessions)
  There are usually only two explanations:
    1. The regex is wrong (spelling / case / escaping, or it does not match this server version)
    2. The log line it targets is no longer produced (e.g. the upstream bug was fixed)
  Advice: use !!logfilter test <a log line> to check whether the rule still matches;
          if it is useless, delete that entry from patterns,
          if it is simply rare, raise stale_rule_threshold,
          or use !!logfilter reset to clear the counters and start observing again.
  (The reminder itself costs no performance -- it is only there to keep the config clean.)
==================================================================
```

The count lives in the header only; each rule is listed once. A rule idle **longer than the
threshold** gets a short `(idle for 9 sessions)` note — that part is not in the header.

Worth knowing:

- **This is not a performance feature.** All bookkeeping happens at server start/stop; the
  per-line filtering path gains no extra work. Its purpose is keeping the config honest.
- The history lives in `config/server_log_filter/state.json`, separate from the `config.json`
  you edit. The plugin never touches your config.
- **Delete a rule and its history goes with it.** After editing `patterns`, run
  `!!logfilter reload` (or `!!MCDR reload plugin server_log_filter`) and the statistics for
  the rules you removed are cleared straight away — no need to wait for the next
  start/stop cycle. The plugin logs exactly which rules it forgot. Rules that are still in
  `patterns` but simply have not matched yet are untouched: only rules **actually removed
  from the config** are forgotten.
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
[ServerLogFilter] Filter rule risks catastrophic backtracking, skipped: '(a+)+$' -- took 90 ms on a 22-character probe string (budget 25 ms). This rule would stall MCDR's main thread on nearly every line; rewrite it as a plain substring or drop the nested quantifier and try again.
```

The check runs once at load; realistic patterns cost microseconds, and a refused rule never
affects the others. Leave `validate_patterns` on unless you know exactly what you are doing.

### Turning the notices off

Each of the three notices above has its own switch, all on by default:

| Notice | Switch | What turning it off does |
|---|---|---|
| Options filled in after an upgrade | `announce_config_upgrade` | The config is **still** completed and written back — it just stops saying so |
| Broken config reset | `announce_broken_config` | The broken file is **still** preserved as `config.json.old` and rebuilt — it just stops saying so |
| Rule idle for several sessions | `warn_about_stale_rules` | The statistics are **still** recorded in `state.json` — it just stops reminding you |

**A switch governs the message, never the work.** All three safety behaviours (completing the
config, backing up a broken file, recording statistics) keep running with every notice off.

Two details worth knowing:

- When the config fails to parse, the value of `announce_broken_config` can only be read **from
  your own file** — there is no parsed config left to ask. It therefore works even when written
  after the syntax error, and `false` really does silence the report.
- A reset config is replaced by defaults, so the switch **returns to `true`** at that point.
  Fix the file, and if you want quiet you will need to set it to `false` again.

## Commands

| Command | Permission | Description |
|---|---|---|
| `!!logfilter` | user | Show status: rule count, per-rule hits and idle streaks |
| `!!logfilter list` | user | Same as above |
| `!!logfilter test <text>` | user | Check whether a line would be hidden, and which rule matches |
| `!!logfilter reload` | admin | Re-read the config file and apply it immediately; also forgets rules removed from it |
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

## What it looks like once installed

The console stops flooding, and the server's own log keeps every line:

```
[ServerLogFilter] Enabled 1 log filter rule; matches are hidden from the MCDR console only, the server log is unaffected
[ServerLogFilter] Hidden 3 server log lines from the MCDR console this run (server log file unaffected)
```

Those same lines are **still complete** in `server/logs/latest.log`.

## Troubleshooting

| Symptom | What to do |
|---|---|
| A rule does not seem to work | `!!logfilter test <a log line>` to see whether it would be hidden; then `!!logfilter reload` |
| A rule is reported as idle | Its regex is probably wrong, or that log line no longer occurs — follow the advice in the reminder |
| The config file was reset | Look for `config.json.old`: that is your original file. The message names the cause and the line number |
| Temporarily disable filtering | Empty `patterns`, then `!!logfilter reload` |

## Requirements & compatibility

| Dimension | Support |
|---|---|
| MCDReforged | **>= 2.15.0** (2.15.0 / 2.15.7 / 2.16.0 tested) |
| Python | >= 3.9 (bundled with MCDR; usually nothing to do) |
| Minecraft | **Independent of the MC version.** 1.16.5 / 1.19.4 / 1.20.1 / 1.20.6 / 1.21.8 / 1.21.11 / 26.1 / 26.2 / 26.3 all tested against real servers |

Installing on an older MCDR fails loudly rather than silently — MCDR reports the unmet version
requirement and refuses to load the plugin.

Filtering happens on the MCDR side (matching each line the server prints), so it does not change
with the MC version. The only version-dependent part is **what the default rule targets**:
`standing on air - force-sending blocks below` is produced only by **MC 26.3**. On earlier
versions the plugin still works — the default rule simply never matches, and you can put whatever
noise your version emits into `patterns`.

## Developers

Building, testing and the version compatibility matrix live in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md); the test suite is documented in
[tests/README.md](tests/README.md).

## License

[MIT](LICENSE)
