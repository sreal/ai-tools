# skillctl

A small CLI for discovering, installing, and removing Claude/Codex "skill"
directories across multiple homes (e.g. WSL + Windows host) and projects, with
a hand-managed vault as the source of truth.

This is a personal POC. The whole tool is one file (`skillctl.py`), runnable
directly via [`uv`](https://docs.astral.sh/uv/) with PEP 723 inline metadata.

---

## Install / run

The script declares its dependencies inline (PEP 723), so you only need `uv`.

### Run directly

On Linux/macOS the shebang lets you invoke the file:

```
chmod +x skillctl.py
./skillctl.py --help
```

On Windows (or anywhere) you can always invoke it through `uv`:

```
uv run skillctl.py --help
```

### Install as a tool

```
uv tool install ./skillctl.py
skillctl --help
```

---

## Concepts

- **Vault** — a directory you maintain by hand. Contains every skill version
  you care about, plus a `default` text file per skill naming the version to
  install when no `@version` is given.
- **Homes** — user-scope install roots (e.g. `/home/you`,
  `/mnt/c/Users/you`). Configured manually in the config file.
- **Projects** — project-scope install roots discovered by `scan`.
- **Layouts** — built-in mappings from a home/project to a skills root:
  - `claude`: `{home}/.claude/skills`, `{project}/.claude/skills`
  - `codex`: `{home}/.codex/skills`, `{project}/.agents/skills`

Installs fan out across `homes × install_layouts` (or, with `--project`,
across that one project × `install_layouts`).

---

## Vault layout

You populate the vault by hand. There is no `vault add` command.

```
<vault>/
  <skill-name>/
    default                 # text file; contents = version string, e.g. "1.0.0"
    <version>/
      SKILL.md              # required; YAML frontmatter may include `version: ...`
      ...other files...
    <other-version>/
      SKILL.md
      ...
```

- `default` is a plain text file. **No symlinks anywhere.**
- The skill name comes from the directory name (the `version` frontmatter
  field is read for display; the `name` field is ignored).
- Missing or unparseable `version` frontmatter is treated as `0.0.0`.

---

## Config

Config path resolution: `--config <path>` → `$SKILLCTL_CONFIG` →
`$XDG_CONFIG_HOME/skillctl/config.json` (default
`~/.config/skillctl/config.json`).

Shape:

```json
{
  "vault": "/home/you/.skillctl/vault",
  "homes": ["/home/you", "/mnt/c/Users/you"],
  "install_layouts": ["claude", "codex"],
  "projects": ["/home/you/code/app"],
  "scan_roots": ["/home/you/code"]
}
```

`scan` rewrites `projects`. Other fields are edited by hand (or by
`vault set`). Round-trip preserves unknown keys.

---

## Commands

```
skillctl scan [--root PATH]...
skillctl list [--project PATH] [--scope user|project] [--layout claude|codex]
skillctl install <skill>[@<version>] [--project PATH] [--force]
skillctl remove  <skill>             [--project PATH]
skillctl vault set <path>
skillctl vault list
skillctl config show
```

### Exit codes

- `0` — success
- `1` — generic error
- `2` — `install` aborted because at least one destination already exists
  (use `--force` to overwrite)

### `scan`

Walks each scan root, honoring `.gitignore` at the root and always skipping
`.git`, `.claude`, and `.agents` (the latter two are project markers we
detect by direct probe, so there's nothing useful below them). A directory
is recorded as a project when it directly contains `.claude/skills/` or
`.agents/skills/`. Results overwrite `projects` in the config.

```
skillctl scan [--root PATH]... [--max-depth N] [--append] [--verbose]
```

`--root` (`-r`) is a one-off override for that invocation only — it does
**not** modify `scan_roots` in the config. To persist roots, edit
`scan_roots` in the config file by hand.

By default, scan **replaces** `projects` in the config with what it just
found. To scan multiple roots across separate invocations and accumulate
the results, pass `--append` (`-a`) to union the new findings into the
existing list. As a safety check, when scan would replace existing
projects with a *smaller* set, it prints a warning to stderr suggesting
`--append`.

Use `--max-depth N` (or `-d N`) to cap descent. Root is depth 0; immediate
children are 1; etc. Recommended values:

- `--max-depth 0` — only check the root directory itself.
- `--max-depth 4-6` — typical layouts (`~/code/group/proj/sub`).
- *(omitted)* — unlimited.

This is essential on slow filesystems (Windows mounts under WSL, network
drives, deeply-nested monorepos with `node_modules` etc.) where unlimited
scans take forever. With `--verbose`, the per-root summary reports how
many directories had their descent truncated.

Pass the global `--verbose` (or `-v`) flag to see progress on stderr —
each root entered, each project found, a milestone every 2000 directories
visited, and a per-root summary. Verbose output goes to stderr only, so it
does not interfere with `--json` output on stdout.

### `install`

Resolves `<vault>/<skill>/<version>/`, defaulting `<version>` to the contents
of the `default` file if `@version` is omitted. Computes destinations via
`homes × install_layouts` (or `[project] × install_layouts` with `--project`).

- Pre-flight: aborts with exit code `2` if any destination already exists,
  unless `--force`.
- Fan-out copy is **best-effort**: if a destination fails mid-fan-out, the
  remaining destinations are still attempted. A per-destination
  `ok`/`FAIL` table is printed, and the process exits non-zero on any
  failure. Partial state is left as-is.

### `remove`

Same destination set as install. Missing destinations emit a warning to
stderr but do not fail.

### `list`

Walks the configured destinations and reads each skill's `version` from
SKILL.md frontmatter. `source=managed` if the (name, version) pair exists in
the vault, otherwise `source=unmanaged`. Hashes are not compared.

---

## Output format

All listings use a fixed-column, space-padded plain-text table — same format
for humans and machines, no color, no borders. Errors and warnings go to
stderr.

Pass `--json` (a global flag, e.g. `skillctl --json list`) to emit
machine-readable JSON instead. Each subcommand has a documented shape:

- `list --json` — array of skill records.
- `vault list --json` — array of `{skill, version, default, path}`.
- `scan --json` — `{"projects": [...]}`.
- `config show --json` — `{config_path, config, targets: [...]}`.
- `install --json` / `remove --json` — `{"results":[{status, path, error?}]}`,
  or, on install pre-flight abort, `{"already_installed":[...]}` with exit 2.
- Errors that bail before work emit `{"error":"..."}` to stdout (and the
  same message to stderr) and exit 1.

---

## Emacs

A small Emacs front-end ships under `emacs/skillctl.el`. It uses
`tabulated-list-mode` for the list and vault views and shells out to the
CLI with `--json` for everything else.

### Setup

```elisp
(load-file "/path/to/skillctl/emacs/skillctl.el")

;; If `skillctl' isn't on PATH, point at the script directly:
(setq skillctl-program '("uv" "run" "/path/to/skillctl/skillctl.py"))

;; Optional: pin the config file (otherwise skillctl resolves it itself):
(setq skillctl-config-file "~/.config/skillctl/config.json")

;; Optional: cap scan depth (root=0); useful on /mnt/c. With C-u, the
;; `skillctl-scan' command prompts for an override.
(setq skillctl-scan-max-depth 5)
```

### Commands

- `M-x skillctl-list` — tabulated buffer of installed skills.
- `M-x skillctl-vault-list` — tabulated buffer of vault contents.
- `M-x skillctl-install` — prompts for skill (vault completion). With
  `C-u`, also prompts for a version; with `C-u C-u`, also for a project.
  Asks whether to pass `--force`. If install exits 2 (already installed),
  offers to retry with `--force`.
- `M-x skillctl-remove` — prompts for skill (installed-list completion).
  With `C-u`, also prompts for a project.
- `M-x skillctl-scan` — runs `skillctl scan` and refreshes any open
  list buffers.
- `M-x skillctl-vault-set` — set the vault directory.
- `M-x skillctl-config-show` — pretty-printed config + resolved targets.

### Keys in `skillctl-list-mode` / `skillctl-vault-list-mode`

| Key   | Action |
|-------|--------|
| `g`   | refresh |
| `i`   | install (skill at point in vault buffer; prompted in list buffer) |
| `d`   | remove the skill at point (list buffer only) |
| `RET` | open the row's `SKILL.md` in another window |
| `s`   | trigger a scan |
| `v`   | switch to the vault buffer |
| `c`   | open the config buffer |
| `q`   | quit window |

Install and remove run asynchronously into a `*skillctl-process*` buffer;
open list buffers auto-refresh when the process exits.
