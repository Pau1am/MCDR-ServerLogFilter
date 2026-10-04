# Language files

One JSON file per language: a flat object mapping a **message key** to a template.

```
server_log_filter/lang/
├── en_us.json     <- English (the fallback: keep this one complete)
├── zh_cn.json     <- Simplified Chinese
└── <your_code>.json
```

## Adding a language

1. Copy `en_us.json` to `<code>.json`, using MCDR's language codes: `<language>_<REGION>`
   in lower case — `de_de`, `fr_fr`, `ja_jp`, `zh_tw`, `pt_br`, ...
2. Translate the **values only**. Never rename, add or remove a key.
3. Run the test suite:

   ```bash
   PYTHONPATH=.testlibs python -m pytest tests -k language -q
   ```

   Two checks matter here: every shipped file must have exactly the same key set as
   `en_us.json`, and every `{}` placeholder must survive translation (`{count}`, `{pattern!r}` …).
4. Open a pull request. That is all — no Python changes are needed anywhere.

Two files are worth translating if you can: `README.md` (Simplified Chinese) has a
counterpart `README_en.md`, and the plugin description in `mcdreforged.plugin.json` is a
`{"en_us": ..., "zh_cn": ...}` object. Both are optional; adding them to your PR is welcome.

## How the language is chosen

The plugin's own option decides, and it defaults to `auto`:

| `language` in `config/server_log_filter/config.json` | What the plugin speaks |
|---|---|
| `auto` *(default)* | Whatever MCDR itself is set to (`language` in MCDR's `config.yml`) |
| `zh_cn` / `en_us` / … | That language, regardless of MCDR's setting |
| a language no file provides | The fallback below, plus a one-line warning in the log |

Anything MCDR is set to that we do not ship a file for falls back like this: the exact
region first, then the same language in another region (`zh_tw` → `zh_cn`, mirroring
MCDR's own fallback), then `en_us`. `-` and `_` are interchangeable and case does not
matter, so `zh-CN`, `ZH_cn` and `zh_cn` are the same request.

## Rules for a good translation

- **Placeholders are part of the contract.** `{count}`, `{name}`, `{reason}`, `{pattern!r}`
  (that `!r` means "Python repr") and format specs such as `{cost:.0f}` must appear
  exactly as they do in `en_us.json`. You may move them around within the sentence.
- **One-line templates are used verbatim.** Several values are sent to `logger.info` as a
  single line, so keep them on one line. The multi-line blocks are built from several
  consecutive keys; their leading spaces are the indentation of the printed block, so
  keep the same alignment.
- **A literal brace is not supported.** Avoid `{` and `}` in prose; if a template cannot be
  formatted, the plugin prints it raw rather than crashing, but it will look wrong.
- **Keep rule names and command names in English**: `patterns`, `stale_rule_threshold`,
  `!!logfilter test`, `state.json`, `config.json.old`. They are what the user has to type.
- **No trailing whitespace, and keep the file UTF-8 with a trailing newline.**

The plugin never fails because of a language file: an unreadable or malformed file is
reported once in the log, and the affected messages fall back to English, then to the raw
key. The key names are descriptive on purpose, so even that last resort is readable.
