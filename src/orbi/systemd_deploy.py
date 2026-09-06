"""Systemd deployment consistency for Orbi (Issue #103, #149).

The repo templates ``systemd/orbi@.service`` and
``systemd/orbi@.timer`` are the single source of truth for the
user-level units. This module provides:

- an idempotent install (copy the templates into the user unit
  directory after confirming an existing deployment uses the same
  config, ``systemctl --user daemon-reload``, enable timer
  instances through the configured ``max_concurrency`` and disable
  surplus timers) that NEVER starts/stops/restarts the service: a currently running
  Runner keeps running, and the new config takes effect at the next
  service start. The install also migrates the pre-#149
  non-templated units away once (stop the legacy timer — a timer stop
  never touches the service — and remove the legacy files), so the
  old single-instance schedule cannot keep firing the old service;
- a pre-start consistency check that compares BOTH installed template
  units against the templates and fails fast with a structured
  ``unit_drift`` line (repo path, installed path, hashes, fix
  command) when they drift.

No database, queue, daemon or second state store: the installed files
and systemd itself are the only state.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

from orbi.pi_activity import quote_value

LOGGER = logging.getLogger("orbi.systemd_deploy")

SERVICE_UNIT = "orbi@.service"
TIMER_UNIT = "orbi@.timer"
UNIT_NAMES = (SERVICE_UNIT, TIMER_UNIT)
# Issue #149: the two enabled timer instances. Each instance triggers
# its own service instance (orbi@1.timer ->
# orbi@1.service, ...@2 -> ...@2), so two independent Runner
# instances can run concurrently; the capacity is still the flock
# slots in the Runner (max_concurrency), never the instance count.
TIMER_INSTANCES = ("orbi@1.timer", "orbi@2.timer")
SERVICE_INSTANCES = (
    "orbi@1.service", "orbi@2.service",
)
# The pre-#149 non-templated units: install_units migrates them away
# once (a template change is a deployment change, no human step).
LEGACY_TIMER_UNIT = "orbi.timer"
LEGACY_UNIT_NAMES = ("orbi.service", "orbi.timer")

# Issue #262: the unit templates are machine-independent. The single
# machine-specific value (the deployment checkout path) is carried as
# this placeholder and substituted at install time with the checkout's
# resolved absolute path — the templates no longer hardcode
# ``%h/Documents/orbi/orbi``, so a checkout at ANY path deploys cleanly.
REPO_DIR_PLACEHOLDER = "{{ORBI_REPO_DIR}}"

# Issue #262: the pre-#246 renamed units. The brand rename (#246/#261)
# changed the unit names from ``muyan-pilot@*`` to ``orbi@*``; a machine
# deployed before the rename still carries the OLD installed units, whose
# ExecStartPre self-heal probes ``muyan-pilot --version`` and reinstalls
# from the (now ``orbi``) checkout — a guaranteed probe → reinstall →
# probe dead loop. install_units migrates them away once.
RENAMED_TEMPLATE_UNITS = (
    "muyan-pilot@.service", "muyan-pilot@.timer",
)
RENAMED_TIMER_INSTANCES = (
    "muyan-pilot@1.timer", "muyan-pilot@2.timer",
)

# The idempotent install command that repairs any drift (carried on
# every unit_drift line as the fix command). Issue #140: the official
# entry is the installed `orbi` CLI (the uv-tool console
# script), not a hand-written Python file entry.
FIX_COMMAND = "orbi install-units"


class UnitDriftError(RuntimeError):
    """The installed units have drifted from the repo templates."""


class UnitConflictError(RuntimeError):
    """An existing unit belongs to a different deployment checkout."""


def installed_config(unit_path: Path) -> Path | None:
    """Return the ORBI_CONFIG value from an installed service unit."""
    if not unit_path.is_file():
        return None
    text = unit_path.read_text(encoding="utf-8")
    match = re.search(
        r"\bORBI_CONFIG=(?:\"([^\"]+)\"|([^\s\"]+))", text,
    )
    if match is None:
        return None
    return Path(match.group(1) or match.group(2)).expanduser().resolve()


def reject_different_deployment(repo_dir: Path, installed_dir: Path) -> None:
    """Refuse to overwrite units owned by another checkout."""
    existing = installed_config(installed_dir / SERVICE_UNIT)
    if existing is None:
        return
    expected = (Path(repo_dir).resolve() / "orbi.toml").resolve()
    if existing == expected:
        return
    message = (
        "existing systemd deployment points at a different ORBI_CONFIG: "
        f"{existing} (this checkout uses {expected}); uninstall the existing "
        "deployment before installing this checkout"
    )
    LOGGER.error(
        "unit_conflict unit=%s installed_config=%s expected_config=%s "
        "action=uninstall_existing_deployment",
        SERVICE_UNIT, existing, expected,
    )
    raise UnitConflictError(message)


def repo_unit_dir(repo_dir: Path) -> Path:
    """The deployment checkout's unit template directory."""
    return Path(repo_dir) / "systemd"


def installed_unit_dir(unit_dir: str | None = None) -> Path:
    """The user unit directory of this machine.

    An explicit ``unit_dir`` (or ``$ORBI_UNIT_DIR``, the
    test/e2e seam) wins; then ``$XDG_CONFIG_HOME/systemd/user``; then
    ``~/.config/systemd/user`` (the standard systemd user unit
    location).
    """
    override = unit_dir or os.environ.get("ORBI_UNIT_DIR")
    if override:
        return Path(override).expanduser()
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home).expanduser() / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def sha256_hex(path: Path) -> str:
    """The sha256 of one file's content (the unit's identity)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def render_unit_template(template_text: str, repo_dir: Path) -> str:
    """Substitute the deployment checkout path into a unit template.

    The repo templates are machine-independent: the single
    machine-specific value (the deployment checkout path) is carried as
    the ``{{ORBI_REPO_DIR}}`` placeholder and replaced here with the
    checkout's resolved absolute path. A template without the placeholder
    is returned unchanged.
    """
    return template_text.replace(
        REPO_DIR_PLACEHOLDER, str(Path(repo_dir).resolve()),
    )


def unit_status(repo_dir: Path, installed_dir: Path) -> list[dict]:
    """Compare the installed units against the repo templates.

    One entry per unit (service and timer, in order): the repo and
    installed paths, both sha256s (None when the file is missing) and
    whether the unit drifted. A missing template or a missing
    installed unit is drift: the deployment is not verifiable.
    """
    repo_dir = Path(repo_dir)
    installed_dir = Path(installed_dir)
    entries: list[dict] = []
    for name in UNIT_NAMES:
        repo_path = repo_unit_dir(repo_dir) / name
        installed_path = installed_dir / name
        if repo_path.is_file():
            # Issue #262: the installed unit is the RENDERED template
            # (the checkout path substituted), so the drift check must
            # compare against the rendered form — otherwise a clean
            # install would always look drifted.
            rendered = render_unit_template(
                repo_path.read_text(encoding="utf-8"), repo_dir,
            )
            repo_sha = hashlib.sha256(
                rendered.encode("utf-8"),
            ).hexdigest()
        else:
            repo_sha = None
        installed_sha = (
            sha256_hex(installed_path) if installed_path.is_file() else None
        )
        entries.append({
            "unit": name,
            "repo_path": repo_path,
            "installed_path": installed_path,
            "repo_sha256": repo_sha,
            "installed_sha256": installed_sha,
            "missing": installed_sha is None,
            "drifted": (
                repo_sha is None
                or installed_sha is None
                or repo_sha != installed_sha
            ),
        })
    return entries


def drift_lines(status: list[dict]) -> list[str]:
    """One structured ``unit_drift`` line per drifted unit.

    Every line carries the repo path, the installed path, both hashes
    and the idempotent fix command (Issue #103). Values containing
    spaces are quoted (the pi_activity.quote_value convention) so the
    line stays parseable.
    """
    lines: list[str] = []
    for entry in status:
        if not entry["drifted"]:
            continue
        lines.append(
            "unit_drift "
            f"unit={entry['unit']} "
            f"repo={quote_value(str(entry['repo_path']))} "
            f"installed={quote_value(str(entry['installed_path']))} "
            f"repo_sha256={entry['repo_sha256'] or '-'} "
            f"installed_sha256={entry['installed_sha256'] or '-'} "
            f"fix={FIX_COMMAND}"
        )
    return lines


def check_unit_drift(repo_dir: Path,
                     installed_dir: Path | None = None) -> None:
    """Pre-start deployment check (Issue #103).

    Compares BOTH installed units against the repo templates. Clean:
    logs ``unit_drift clean`` and returns. Drift: logs one structured
    ``unit_drift`` line per drifted unit and raises
    ``UnitDriftError`` — the caller fails fast and claims no Issue
    until the units are synced with the idempotent install command.
    """
    if installed_dir is None:
        installed_dir = installed_unit_dir()
    status = unit_status(repo_dir, installed_dir)
    lines = drift_lines(status)
    if not lines:
        LOGGER.info("unit_drift clean installed_dir=%s", installed_dir)
        return
    for line in lines:
        LOGGER.error(line)
    raise UnitDriftError(
        "installed systemd units have drifted from the repo templates; "
        f"sync with: {FIX_COMMAND}\n" + "\n".join(lines)
    )


def sync_drifted_units(repo_dir: Path,
                       installed_dir: Path | None = None,
                       *, max_concurrency: int = len(TIMER_INSTANCES),
                       run_command) -> list[dict]:
    """Pre-start self-heal for drifted units (Issue #142).

    The normal scene: a template change merged to main, the
    ExecStartPre-synced checkout carries the new templates, and the
    installed units are still the old ones. Runs the SAME idempotent
    install (``install_units``: copy the templates, daemon-reload,
    sync the configured timer instances — never start/stop/restart the service, so a
    currently running Runner is untouched) and re-verifies with the
    SAME hash check (``unit_status``). Clean after the sync: logs one
    structured ``unit_drift auto_synced`` line per unit (unit,
    before/after sha256, deployed commit) and returns the per-unit
    report. Still drifted after the sync: logs the structured
    ``unit_drift`` lines and raises ``UnitDriftError`` (fail fast —
    the caller claims no Issue). A failing install step propagates
    unchanged. No drift: returns ``[]`` without touching anything.
    """
    repo_dir = Path(repo_dir)
    if installed_dir is None:
        installed_dir = installed_unit_dir()
    installed_dir = Path(installed_dir)
    before = unit_status(repo_dir, installed_dir)
    if not any(entry["drifted"] for entry in before):
        return []
    result = install_units(
        repo_dir, installed_dir, max_concurrency=max_concurrency,
        run_command=run_command,
    )
    after = unit_status(repo_dir, installed_dir)
    lines = drift_lines(after)
    if lines:
        for line in lines:
            LOGGER.error(line)
        raise UnitDriftError(
            "installed systemd units still drift after the pre-start "
            f"sync; sync with: {FIX_COMMAND}\n" + "\n".join(lines)
        )
    report: list[dict] = []
    for entry_before, entry_after in zip(before, after):
        LOGGER.info(
            "unit_drift auto_synced unit=%s "
            "before_sha256=%s after_sha256=%s commit=%s",
            entry_after["unit"],
            entry_before["installed_sha256"] or "-",
            entry_after["installed_sha256"],
            result["commit"],
        )
        report.append({
            "unit": entry_after["unit"],
            "before_sha256": entry_before["installed_sha256"],
            "after_sha256": entry_after["installed_sha256"],
            "commit": result["commit"],
        })
    return report


def migrate_legacy_units(installed_dir: Path, *, run_command) -> bool:
    """One-time migration away from the pre-#149 non-templated units.

    Returns True when a legacy timer unit file was present (and
    migrated), False when there was nothing to migrate (a fresh
    install or an already-migrated machine — idempotent).

    The legacy ``orbi.timer`` is stopped with ``disable --now``
    (a TIMER stop: it never starts, stops or restarts the SERVICE — a
    currently running Runner keeps running) and the legacy
    ``orbi.service``/``orbi.timer`` files are removed,
    so the old single-instance schedule cannot keep firing the old
    service (a third Runner without the ExecStartPre flock, Issue
    #149). A failing step propagates unchanged (fail fast).
    """
    if not (installed_dir / LEGACY_TIMER_UNIT).is_file():
        return False
    run_command([
        "systemctl", "--user", "disable", "--now", LEGACY_TIMER_UNIT,
    ])
    for name in LEGACY_UNIT_NAMES:
        legacy = installed_dir / name
        if legacy.is_file():
            legacy.unlink()
    LOGGER.info(
        "legacy_units_migrated installed_dir=%s removed=%s",
        installed_dir, ",".join(LEGACY_UNIT_NAMES),
    )
    return True


def migrate_renamed_units(installed_dir: Path, *, run_command) -> bool:
    """One-time migration away from the pre-#246 renamed units (#262).

    The brand rename (#246/#261) changed the unit names from
    ``muyan-pilot@*`` to ``orbi@*``, but a machine deployed before the
    rename still carries the OLD installed units. Their ``ExecStartPre``
    self-heal probes ``muyan-pilot --version`` and, on failure, reinstalls
    from the checkout — but the checkout's package is now ``orbi``, so the
    reinstall produces ``orbi``, never ``muyan-pilot``: a guaranteed probe
    → reinstall → probe dead loop, one crash per timer tick. This
    migration breaks the loop: it disables the old timer instances (TIMER
    stops — the service is never started/stopped/restarted, so a running
    Runner keeps running) and removes the old unit files, so the old
    schedule cannot keep firing. One-time and idempotent: no old timer
    file → no-op (a fresh or already-migrated machine). A failing step
    propagates unchanged (fail fast).
    """
    if not (installed_dir / "muyan-pilot@.timer").is_file():
        return False
    for instance in RENAMED_TIMER_INSTANCES:
        run_command(["systemctl", "--user", "disable", "--now", instance])
    for name in RENAMED_TEMPLATE_UNITS:
        legacy = installed_dir / name
        if legacy.is_file():
            legacy.unlink()
    LOGGER.info(
        "renamed_units_migrated installed_dir=%s removed=%s",
        installed_dir, ",".join(RENAMED_TEMPLATE_UNITS),
    )
    return True


def install_units(repo_dir: Path, installed_dir: Path | None = None,
                  *, max_concurrency: int = len(TIMER_INSTANCES),
                  run_command) -> dict:
    """Idempotently install the repo templates as the user units.

    Overwrites BOTH installed template units with the repo templates
    (the repo is the single source of truth), unless the installed
    service belongs to a different config, which fails before any
    migration or write. Migrates the pre-#149
    non-templated units away once (see ``migrate_legacy_units``), runs
    ``systemctl --user daemon-reload``, enables instances through
    ``max_concurrency`` and disables surplus timers. These operations
    activate or stop only timers, never services. The services are NEVER started,
    stopped or restarted: a currently running Runner keeps running,
    and the new config takes effect at the next service start.
    Returns the deployed commit (the deployment checkout's HEAD) and
    the installed units' hashes.
    """
    if not 1 <= max_concurrency <= len(TIMER_INSTANCES):
        raise ValueError(
            "max_concurrency must have a matching Runner timer instance "
            f"(1..{len(TIMER_INSTANCES)})"
        )
    repo_dir = Path(repo_dir)
    if installed_dir is None:
        installed_dir = installed_unit_dir()
    installed_dir = Path(installed_dir)
    reject_different_deployment(repo_dir, installed_dir)
    for name in UNIT_NAMES:
        template = repo_unit_dir(repo_dir) / name
        if not template.is_file():
            raise FileNotFoundError(
                f"unit template missing: {template} (the repo "
                "templates are the single source of truth)"
            )
    installed_dir.mkdir(parents=True, exist_ok=True)
    migrate_legacy_units(installed_dir, run_command=run_command)
    migrate_renamed_units(installed_dir, run_command=run_command)
    for name in UNIT_NAMES:
        # Issue #262: render the template (substitute the deployment
        # checkout path for the {{ORBI_REPO_DIR}} placeholder) so the
        # installed unit points at THIS checkout regardless of where it
        # lives. A template without the placeholder is written unchanged.
        template_text = (
            repo_unit_dir(repo_dir) / name
        ).read_text(encoding="utf-8")
        rendered = render_unit_template(template_text, repo_dir)
        (installed_dir / name).write_bytes(rendered.encode("utf-8"))
    run_command(["systemctl", "--user", "daemon-reload"])
    for instance in TIMER_INSTANCES[:max_concurrency]:
        run_command(["systemctl", "--user", "enable", "--now", instance])
    for instance in TIMER_INSTANCES[max_concurrency:]:
        run_command(["systemctl", "--user", "disable", "--now", instance])
    commit = run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    units = {
        name: {
            "installed_path": installed_dir / name,
            "sha256": sha256_hex(installed_dir / name),
        }
        for name in UNIT_NAMES
    }
    LOGGER.info(
        "units_installed commit=%s installed_dir=%s units=%s "
        "instances=%s",
        commit, installed_dir, ",".join(UNIT_NAMES),
        ",".join(TIMER_INSTANCES[:max_concurrency]),
    )
    return {
        "commit": commit,
        "installed_dir": installed_dir,
        "units": units,
    }
