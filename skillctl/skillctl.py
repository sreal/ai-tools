#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "typer>=0.12",
#   "pyyaml>=6.0",
#   "pathspec>=0.12",
# ]
# ///
"""skillctl — manage Claude/Codex skill directories across homes and projects."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import pathspec
import typer
import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LAYOUTS: dict[str, dict[str, str]] = {
    "claude": {
        "user": "{home}/.claude/skills",
        "project": "{project}/.claude/skills",
    },
    "codex": {
        "user": "{home}/.codex/skills",
        "project": "{project}/.agents/skills",
    },
}

# Never descend into these.  `.claude` and `.agents` are project markers we
# detect by direct probe — there's nothing useful below them for the scan.
ALWAYS_SKIP_DIRS: frozenset[str] = frozenset({".git", ".claude", ".agents"})

DEFAULT_VERSION = "0.0.0"
DEFAULT_FILE_NAME = "default"
SKILL_FILE_NAME = "SKILL.md"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ALREADY_INSTALLED = 2

ENV_CONFIG_PATH = "SKILLCTL_CONFIG"


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "skillctl" / "config.json"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_KEYS = ("vault", "homes", "install_layouts", "projects", "scan_roots")


def resolve_config_path(flag_value: Optional[Path]) -> Path:
    if flag_value is not None:
        return flag_value
    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        return Path(env)
    return default_config_path()


def load_config(path: Path) -> dict:
    if not path.exists():
        return {
            "vault": "",
            "homes": [],
            "install_layouts": list(LAYOUTS.keys()),
            "projects": [],
            "scan_roots": [],
        }
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise typer.BadParameter(f"Config at {path} is not a JSON object")
    data.setdefault("vault", "")
    data.setdefault("homes", [])
    data.setdefault("install_layouts", list(LAYOUTS.keys()))
    data.setdefault("projects", [])
    data.setdefault("scan_roots", [])
    return data


def save_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=False)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Skill model
# ---------------------------------------------------------------------------


def read_skill_version(skill_dir: Path) -> str:
    skill_file = skill_dir / SKILL_FILE_NAME
    if not skill_file.is_file():
        return DEFAULT_VERSION
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_VERSION
    if not text.startswith("---"):
        return DEFAULT_VERSION
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return DEFAULT_VERSION
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return DEFAULT_VERSION
    block = "\n".join(lines[1:end_idx])
    try:
        meta = yaml.safe_load(block) or {}
    except yaml.YAMLError:
        return DEFAULT_VERSION
    if not isinstance(meta, dict):
        return DEFAULT_VERSION
    version = meta.get("version")
    if version is None:
        return DEFAULT_VERSION
    return str(version)


# ---------------------------------------------------------------------------
# Layout resolution
# ---------------------------------------------------------------------------


def expand_user_paths(homes: Iterable[str], layouts: Iterable[str]) -> list[tuple[str, str, Path]]:
    """Returns (layout, home, skills_root_path)."""
    out: list[tuple[str, str, Path]] = []
    for home in homes:
        for layout in layouts:
            tmpl = LAYOUTS[layout]["user"]
            out.append((layout, home, Path(tmpl.format(home=home))))
    return out


def expand_project_paths(project: str, layouts: Iterable[str]) -> list[tuple[str, str, Path]]:
    """Returns (layout, project, skills_root_path)."""
    out: list[tuple[str, str, Path]] = []
    for layout in layouts:
        tmpl = LAYOUTS[layout]["project"]
        out.append((layout, project, Path(tmpl.format(project=project))))
    return out


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


@dataclass
class SkillRecord:
    scope: str           # "user" or "project"
    layout: str          # "claude" | "codex"
    location: str        # home dir or project dir
    name: str
    version: str
    path: Path
    source: str = "unmanaged"  # "managed" or "unmanaged"


def _load_gitignore(root: Path) -> Optional[pathspec.PathSpec]:
    gi = root / ".gitignore"
    if not gi.is_file():
        return None
    try:
        lines = gi.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def _walk_for_layouts(
    root: Path, max_depth: Optional[int] = None
) -> tuple[list[Path], list[Path]]:
    """Walk a root and return (claude_project_dirs, codex_project_dirs).

    Detection rule (project scope): a directory is a project if it contains
    `.claude/skills/` (claude) or `.agents/skills/` (codex). We return the
    project root, not the skills dir.

    If MAX_DEPTH is set, do not descend below that depth (root itself is
    depth 0).  `--max-depth 0` only checks the root directory.
    """
    spec = _load_gitignore(root)
    claude_projects: list[Path] = []
    codex_projects: list[Path] = []
    root = root.resolve()
    depth_note = f", max-depth={max_depth}" if max_depth is not None else ""
    vlog(
        f"scan: walking {root}{' (with .gitignore)' if spec else ''}"
        f"{depth_note}"
    )

    dir_count = 0
    truncated = 0
    progress_every = 2000
    for current, dirs, _files in os.walk(root, followlinks=False):
        cur_path = Path(current)
        dir_count += 1
        rel = cur_path.relative_to(root)
        depth = 0 if str(rel) == "." else len(rel.parts)
        if dir_count % progress_every == 0:
            vlog(f"scan: {dir_count} dirs visited under {root} (at {cur_path})")
        # Prune
        pruned: list[str] = []
        for d in dirs:
            if d in ALWAYS_SKIP_DIRS:
                continue
            full = cur_path / d
            try:
                child_rel = full.resolve().relative_to(root)
            except ValueError:
                continue
            rel_str = str(child_rel).replace(os.sep, "/") + "/"
            if spec is not None and spec.match_file(rel_str):
                continue
            pruned.append(d)
        dirs[:] = pruned

        # Detect project layouts at this level (probe, not walk).
        if (cur_path / ".claude" / "skills").is_dir():
            claude_projects.append(cur_path)
            vlog(f"scan: found claude project {cur_path}")
        if (cur_path / ".agents" / "skills").is_dir():
            codex_projects.append(cur_path)
            vlog(f"scan: found codex project {cur_path}")

        # Depth limit: stop descending past max_depth.
        if max_depth is not None and depth >= max_depth and dirs:
            truncated += 1
            dirs[:] = []

    extra = f", truncated descent at {truncated} dir(s)" if truncated else ""
    vlog(
        f"scan: done {root} ({dir_count} dirs, "
        f"{len(claude_projects)} claude, {len(codex_projects)} codex{extra})"
    )
    return claude_projects, codex_projects


def list_skills_in(skills_root: Path) -> list[Path]:
    if not skills_root.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(skills_root.iterdir()):
        if entry.is_dir() and (entry / SKILL_FILE_NAME).is_file():
            out.append(entry)
    return out


def vault_index(vault: Path) -> dict[tuple[str, str], Path]:
    """Map (skill_name, version) -> path inside the vault."""
    idx: dict[tuple[str, str], Path] = {}
    if not vault or not vault.is_dir():
        return idx
    for skill_dir in sorted(vault.iterdir()):
        if not skill_dir.is_dir():
            continue
        for ver_dir in sorted(skill_dir.iterdir()):
            if not ver_dir.is_dir():
                continue
            if (ver_dir / SKILL_FILE_NAME).is_file():
                idx[(skill_dir.name, ver_dir.name)] = ver_dir
    return idx


def collect_records(
    homes: list[str],
    projects: list[str],
    layouts: list[str],
    vault: Optional[Path],
) -> list[SkillRecord]:
    records: list[SkillRecord] = []

    vidx = vault_index(vault) if vault else {}

    for layout, home, skills_root in expand_user_paths(homes, layouts):
        for skill_dir in list_skills_in(skills_root):
            name = skill_dir.name
            version = read_skill_version(skill_dir)
            source = "managed" if (name, version) in vidx else "unmanaged"
            records.append(
                SkillRecord("user", layout, home, name, version, skill_dir, source)
            )

    for project in projects:
        for layout, _proj, skills_root in expand_project_paths(project, layouts):
            for skill_dir in list_skills_in(skills_root):
                name = skill_dir.name
                version = read_skill_version(skill_dir)
                source = "managed" if (name, version) in vidx else "unmanaged"
                records.append(
                    SkillRecord("project", layout, project, name, version, skill_dir, source)
                )

    return records


# ---------------------------------------------------------------------------
# Vault helpers
# ---------------------------------------------------------------------------


def vault_default_version(vault: Path, skill: str) -> Optional[str]:
    f = vault / skill / DEFAULT_FILE_NAME
    if not f.is_file():
        return None
    try:
        return f.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def vault_skill_path(vault: Path, skill: str, version: str) -> Path:
    return vault / skill / version


# ---------------------------------------------------------------------------
# Output helper
# ---------------------------------------------------------------------------


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        # Still print headers for predictable output.
        widths = [len(h) for h in headers]
        line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
        print(line)
        return
    cols = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i in range(cols):
            cell = row[i] if i < len(row) else ""
            if len(cell) > widths[i]:
                widths[i] = len(cell)
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    for row in rows:
        cells = [(row[i] if i < len(row) else "").ljust(widths[i]) for i in range(cols)]
        print("  ".join(cells))


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Typer app
# ---------------------------------------------------------------------------

app = typer.Typer(add_completion=False, help="Manage Claude/Codex skills across homes and projects.")
vault_app = typer.Typer(add_completion=False, help="Vault commands.")
config_app = typer.Typer(add_completion=False, help="Config commands.")
app.add_typer(vault_app, name="vault")
app.add_typer(config_app, name="config")


@dataclass
class AppState:
    config_path: Path
    config: dict
    json_output: bool = False
    verbose: bool = False


state: AppState  # populated in callback


@app.callback()
def _root(
    ctx: typer.Context,
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to config.json (default: $SKILLCTL_CONFIG or XDG location)."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Emit progress messages to stderr."
    ),
) -> None:
    global state
    cfg_path = resolve_config_path(config)
    cfg = load_config(cfg_path)
    state = AppState(
        config_path=cfg_path, config=cfg, json_output=json_output, verbose=verbose
    )


def emit_json(payload) -> None:
    print(json.dumps(payload, indent=2))


def vlog(msg: str) -> None:
    """Write a progress line to stderr when --verbose is set."""
    if state.verbose:
        print(f"[skillctl] {msg}", file=sys.stderr, flush=True)


def _verbose_flag(verbose: bool) -> None:
    """Subcommand-local --verbose flips the global setting on (only)."""
    if verbose:
        state.verbose = True


def _json_flag(json_output: bool) -> None:
    """Subcommand-local --json flips the global setting on (only)."""
    if json_output:
        state.json_output = True


def _global_flags(verbose: bool, json_output: bool) -> None:
    _verbose_flag(verbose)
    _json_flag(json_output)


def fail_json(msg: str, exit_code: int = EXIT_ERROR) -> None:
    """For --json mode: write {"error": ...} to stdout, error to stderr, exit."""
    err(msg)
    if state.json_output:
        emit_json({"error": msg})
    raise typer.Exit(exit_code)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


@app.command("scan")
def cmd_scan(
    root: list[Path] = typer.Option(
        None, "--root", "-r",
        help=(
            "One-off scan root override (repeatable). Does NOT modify "
            "`scan_roots` in the config. Defaults to config `scan_roots`."
        ),
    ),
    max_depth: Optional[int] = typer.Option(
        None,
        "--max-depth", "-d",
        help=(
            "Limit walk depth (root is 0). Useful on large/slow trees "
            "(e.g. WSL /mnt/c). Recommended: 4-6 for typical project layouts."
        ),
    ),
    replace: bool = typer.Option(
        False, "--replace",
        help=(
            "Replace `config.projects` with just the projects found by this run. "
            "By default, scan unions newly-found projects into the existing list."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Emit progress to stderr."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Scan roots for projects with `.claude/skills` or `.agents/skills`. Unions into config.projects (use --replace to overwrite)."""
    _global_flags(verbose, json_output)
    if max_depth is not None and max_depth < 0:
        fail_json("--max-depth must be >= 0")
    cfg = state.config
    roots: list[Path]
    if root:
        roots = [Path(p) for p in root]
    else:
        roots = [Path(p) for p in cfg.get("scan_roots", [])]
    if not roots:
        fail_json("no scan roots configured. Use --root or set scan_roots in config.")

    vlog(f"scan: starting; {len(roots)} root(s): {', '.join(str(r) for r in roots)}")
    found: set[str] = set()
    for r in roots:
        if not r.is_dir():
            warn(f"scan root does not exist: {r}")
            continue
        claude_p, codex_p = _walk_for_layouts(r, max_depth=max_depth)
        for p in claude_p + codex_p:
            found.add(str(p))
    vlog(f"scan: complete; {len(found)} project(s) found in this run")

    previous = set(cfg.get("projects", []))
    merged = sorted(found if replace else previous | found)
    added = len(set(merged) - previous)
    removed = len(previous - set(merged))
    vlog(
        f"scan: cfg.projects {len(previous)} -> {len(merged)} "
        f"(+{added}, -{removed}; mode={'replace' if replace else 'union'})"
    )

    cfg["projects"] = merged
    save_config(state.config_path, cfg)

    if state.json_output:
        emit_json({"projects": merged})
        return

    rows = [[p] for p in merged]
    print_table(["project"], rows)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@app.command("list")
def cmd_list(
    project: Optional[Path] = typer.Option(None, "--project", help="Filter to a single project."),
    scope: Optional[str] = typer.Option(None, "--scope", help="Filter by scope: user|project."),
    layout: Optional[str] = typer.Option(None, "--layout", help="Filter by layout: claude|codex."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Emit progress to stderr."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """List installed skills across configured homes and projects."""
    _global_flags(verbose, json_output)
    cfg = state.config
    if scope is not None and scope not in ("user", "project"):
        fail_json("--scope must be 'user' or 'project'")
    if layout is not None and layout not in LAYOUTS:
        fail_json(f"--layout must be one of: {', '.join(LAYOUTS)}")

    homes = list(cfg.get("homes", []))
    projects = list(cfg.get("projects", []))
    if project is not None:
        projects = [str(project)]
        # When a specific project is requested, drop user-scope rows.
        homes = []
    layouts = [layout] if layout else list(cfg.get("install_layouts", LAYOUTS.keys()))
    vault = Path(cfg["vault"]) if cfg.get("vault") else None

    records = collect_records(homes, projects, layouts, vault)

    if scope is not None:
        records = [r for r in records if r.scope == scope]

    if state.json_output:
        emit_json([
            {
                "scope": r.scope,
                "layout": r.layout,
                "location": r.location,
                "name": r.name,
                "version": r.version,
                "source": r.source,
                "path": str(r.path),
            }
            for r in records
        ])
        return

    rows = [
        [r.scope, r.layout, r.location, r.name, r.version, r.source, str(r.path)]
        for r in records
    ]
    print_table(
        ["scope", "layout", "location", "name", "version", "source", "path"], rows
    )


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def _parse_skill_spec(spec: str) -> tuple[str, Optional[str]]:
    if "@" in spec:
        name, _, version = spec.partition("@")
        return name, version or None
    return spec, None


@app.command("install")
def cmd_install(
    skill: str = typer.Argument(..., help="Skill name, optionally `name@version`."),
    project: Optional[Path] = typer.Option(
        None, "--project", help="Install into a single project instead of all configured homes."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing skill directories."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Emit progress to stderr."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Install a skill from the vault to all configured destinations (or a single project)."""
    _global_flags(verbose, json_output)
    cfg = state.config
    vault_str = cfg.get("vault", "")
    if not vault_str:
        fail_json("vault is not configured. Run `skillctl vault set <path>`.")
    vault = Path(vault_str)
    if not vault.is_dir():
        fail_json(f"vault does not exist: {vault}")

    name, version = _parse_skill_spec(skill)
    if version is None:
        version = vault_default_version(vault, name)
        if version is None:
            fail_json(
                f"no version specified and no `default` file at {vault / name / DEFAULT_FILE_NAME}"
            )

    src = vault_skill_path(vault, name, version)
    if not src.is_dir() or not (src / SKILL_FILE_NAME).is_file():
        fail_json(f"vault entry not found or missing SKILL.md: {src}")

    layouts = list(cfg.get("install_layouts", LAYOUTS.keys()))
    targets: list[tuple[str, str, Path]] = []  # (scope, layout, dest_skill_path)
    if project is not None:
        for layout, _proj, skills_root in expand_project_paths(str(project), layouts):
            targets.append(("project", layout, skills_root / name))
    else:
        homes = list(cfg.get("homes", []))
        if not homes:
            fail_json("no homes configured and no --project given. Edit config or pass --project.")
        for layout, _home, skills_root in expand_user_paths(homes, layouts):
            targets.append(("user", layout, skills_root / name))

    # Pre-flight: existence check.
    if not force:
        existing = [t for t in targets if t[2].exists()]
        if existing:
            for _scope, _layout, p in existing:
                err(f"already installed: {p}")
            if state.json_output:
                emit_json({"already_installed": [str(p) for _s, _l, p in existing]})
            raise typer.Exit(EXIT_ALREADY_INSTALLED)

    # Fan-out copy. Best-effort: continue past failures, report at end.
    successes: list[Path] = []
    failures: list[tuple[Path, str]] = []
    for scope, layout, dest in targets:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest, symlinks=False)
            successes.append(dest)
        except Exception as e:
            failures.append((dest, str(e)))

    if state.json_output:
        results = [{"status": "ok", "path": str(p)} for p in successes]
        results += [{"status": "FAIL", "path": str(p), "error": msg} for p, msg in failures]
        emit_json({"results": results})
    else:
        rows = [["ok", str(p)] for p in successes] + [["FAIL", str(p)] for p, _ in failures]
        print_table(["status", "path"], rows)
    for p, msg in failures:
        err(f"{p}: {msg}")

    if failures:
        raise typer.Exit(EXIT_ERROR)


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


@app.command("remove")
def cmd_remove(
    skill: str = typer.Argument(..., help="Skill name."),
    project: Optional[Path] = typer.Option(
        None, "--project", help="Remove from a single project instead of all configured homes."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Emit progress to stderr."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Remove a skill from all configured destinations (or a single project)."""
    _global_flags(verbose, json_output)
    cfg = state.config
    layouts = list(cfg.get("install_layouts", LAYOUTS.keys()))
    targets: list[Path] = []
    if project is not None:
        for _layout, _proj, skills_root in expand_project_paths(str(project), layouts):
            targets.append(skills_root / skill)
    else:
        homes = list(cfg.get("homes", []))
        if not homes:
            fail_json("no homes configured and no --project given.")
        for _layout, _home, skills_root in expand_user_paths(homes, layouts):
            targets.append(skills_root / skill)

    successes: list[Path] = []
    failures: list[tuple[Path, str]] = []
    rows: list[list[str]] = []
    json_results: list[dict] = []
    for dest in targets:
        if not dest.exists():
            warn(f"not installed: {dest}")
            rows.append(["missing", str(dest)])
            json_results.append({"status": "missing", "path": str(dest)})
            continue
        try:
            shutil.rmtree(dest)
            successes.append(dest)
            rows.append(["ok", str(dest)])
            json_results.append({"status": "ok", "path": str(dest)})
        except Exception as e:
            failures.append((dest, str(e)))
            rows.append(["FAIL", str(dest)])
            json_results.append({"status": "FAIL", "path": str(dest), "error": str(e)})

    if state.json_output:
        emit_json({"results": json_results})
    else:
        print_table(["status", "path"], rows)
    for p, msg in failures:
        err(f"{p}: {msg}")
    if failures:
        raise typer.Exit(EXIT_ERROR)


# ---------------------------------------------------------------------------
# vault
# ---------------------------------------------------------------------------


@vault_app.command("set")
def cmd_vault_set(
    path: Path = typer.Argument(..., help="Vault directory path."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Emit progress to stderr."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Set the vault directory in the config."""
    _global_flags(verbose, json_output)
    cfg = state.config
    cfg["vault"] = str(path)
    save_config(state.config_path, cfg)
    if state.json_output:
        emit_json({"vault": str(path)})
    else:
        print(f"vault set: {path}")


@vault_app.command("list")
def cmd_vault_list(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Emit progress to stderr."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """List skills and versions in the vault."""
    _global_flags(verbose, json_output)
    cfg = state.config
    vault_str = cfg.get("vault", "")
    if not vault_str:
        fail_json("vault is not configured. Run `skillctl vault set <path>`.")
    vault = Path(vault_str)
    if not vault.is_dir():
        fail_json(f"vault does not exist: {vault}")

    rows: list[list[str]] = []
    json_entries: list[dict] = []
    for skill_dir in sorted(vault.iterdir()):
        if not skill_dir.is_dir():
            continue
        default_v = vault_default_version(vault, skill_dir.name) or ""
        for ver_dir in sorted(skill_dir.iterdir()):
            if not ver_dir.is_dir():
                continue
            is_default = ver_dir.name == default_v
            marker = "*" if is_default else ""
            rows.append([skill_dir.name, ver_dir.name, marker, str(ver_dir)])
            json_entries.append({
                "skill": skill_dir.name,
                "version": ver_dir.name,
                "default": is_default,
                "path": str(ver_dir),
            })
    if state.json_output:
        emit_json(json_entries)
    else:
        print_table(["skill", "version", "default", "path"], rows)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@config_app.command("show")
def cmd_config_show(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Emit progress to stderr."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Show resolved config and the set of paths every install would touch."""
    _global_flags(verbose, json_output)
    cfg = state.config
    homes = list(cfg.get("homes", []))
    projects = list(cfg.get("projects", []))
    layouts = list(cfg.get("install_layouts", LAYOUTS.keys()))

    targets: list[dict] = []
    for layout, home, p in expand_user_paths(homes, layouts):
        targets.append({"scope": "user", "layout": layout, "location": home, "skills_root": str(p)})
    for project in projects:
        for layout, _proj, p in expand_project_paths(project, layouts):
            targets.append({"scope": "project", "layout": layout, "location": project, "skills_root": str(p)})

    if state.json_output:
        emit_json({
            "config_path": str(state.config_path),
            "config": cfg,
            "targets": targets,
        })
        return

    print(f"config_path: {state.config_path}")
    print(json.dumps(cfg, indent=2))
    print()
    rows = [[t["scope"], t["layout"], t["location"], t["skills_root"]] for t in targets]
    print_table(["scope", "layout", "location", "skills_root"], rows)


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    app()


if __name__ == "__main__":
    main()
