"""Systemd deployment consistency tests (Issue #103, #149).

The repo templates (``systemd/orbi@.service`` and
``orbi@.timer``) are the single source of truth. The install
command is idempotent and must never start/stop/restart the service:
a currently running Runner keeps running, and the new config takes
effect at the next service start. The install enables the two timer
instances (``orbi@1.timer``, ``orbi@2.timer``) and
migrates the pre-#149 non-templated units away once. The pre-start
check compares the installed units against the templates and fails
fast with a structured ``unit_drift`` line when they drift.
"""
import hashlib
import subprocess
from pathlib import Path

import pytest

from orbi import systemd_deploy


def make_repo(tmp_path: Path) -> Path:
    """A deployment checkout carrying the two unit templates."""
    repo = tmp_path / "repo"
    systemd = repo / "systemd"
    systemd.mkdir(parents=True)
    (systemd / "orbi@.service").write_text(
        "[Service]\nExecStart=/usr/bin/python3 bootstrap_runner.py\n",
        encoding="utf-8",
    )
    (systemd / "orbi@.timer").write_text(
        "[Timer]\nOnCalendar=*-*-* *:00/5\n", encoding="utf-8",
    )
    return repo


def make_installed(
    tmp_path: Path, repo: Path, mutate: str | None = None,
) -> Path:
    """An installed unit dir holding copies of the repo templates."""
    installed = tmp_path / "home" / ".config" / "systemd" / "user"
    installed.mkdir(parents=True)
    for name in systemd_deploy.UNIT_NAMES:
        (installed / name).write_bytes(
            (repo / "systemd" / name).read_bytes(),
        )
    if mutate is not None:
        (installed / mutate).write_text(
            (installed / mutate).read_text(encoding="utf-8") + "# drift\n",
            encoding="utf-8",
        )
    return installed


def test_unit_names_cover_service_and_timer():
    # The check must cover BOTH template units (Issue #103
    # requirement, the instantiated names since Issue #149).
    assert systemd_deploy.UNIT_NAMES == (
        "orbi@.service", "orbi@.timer",
    )


def test_instance_names_cover_two_timers_and_two_services():
    # Issue #149: exactly two timer instances, each triggering its own
    # service instance (the capacity is the flock slots, not the
    # instance count).
    assert systemd_deploy.TIMER_INSTANCES == (
        "orbi@1.timer", "orbi@2.timer",
    )
    assert systemd_deploy.SERVICE_INSTANCES == (
        "orbi@1.service", "orbi@2.service",
    )


def test_repo_unit_dir_points_at_the_systemd_directory(tmp_path):
    repo = tmp_path / "repo"
    assert systemd_deploy.repo_unit_dir(repo) == repo / "systemd"


def test_installed_unit_dir_defaults_to_the_user_config_dir(
    monkeypatch, tmp_path,
):
    monkeypatch.delenv("ORBI_UNIT_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert systemd_deploy.installed_unit_dir() == (
        tmp_path / ".config" / "systemd" / "user"
    )


def test_installed_unit_dir_respects_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.delenv("ORBI_UNIT_DIR", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert systemd_deploy.installed_unit_dir() == (
        xdg / "systemd" / "user"
    )


def test_installed_unit_dir_explicit_argument_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("ORBI_UNIT_DIR", raising=False)
    target = tmp_path / "elsewhere"
    assert systemd_deploy.installed_unit_dir(str(target)) == target


def test_installed_unit_dir_env_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "override"
    monkeypatch.setenv("ORBI_UNIT_DIR", str(target))
    assert systemd_deploy.installed_unit_dir() == target


def test_sha256_hex_matches_the_file_content(tmp_path):
    path = tmp_path / "unit.service"
    path.write_bytes(b"[Service]\n")
    assert systemd_deploy.sha256_hex(path) == hashlib.sha256(
        b"[Service]\n",
    ).hexdigest()


def test_unit_status_reports_clean_when_templates_match(tmp_path):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo)
    status = systemd_deploy.unit_status(repo, installed)
    assert [entry["unit"] for entry in status] == list(
        systemd_deploy.UNIT_NAMES,
    )
    for entry in status:
        assert entry["drifted"] is False
        assert entry["missing"] is False
        assert entry["repo_sha256"] == entry["installed_sha256"]
        assert entry["repo_path"] == repo / "systemd" / entry["unit"]
        assert entry["installed_path"] == installed / entry["unit"]


def test_unit_status_reports_drift_for_a_changed_unit(tmp_path):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo, mutate="orbi@.timer")
    status = systemd_deploy.unit_status(repo, installed)
    by_unit = {entry["unit"]: entry for entry in status}
    assert by_unit["orbi@.service"]["drifted"] is False
    assert by_unit["orbi@.timer"]["drifted"] is True
    assert by_unit["orbi@.timer"]["missing"] is False
    assert (
        by_unit["orbi@.timer"]["repo_sha256"]
        != by_unit["orbi@.timer"]["installed_sha256"]
    )


def test_unit_status_reports_missing_installed_unit_as_drift(tmp_path):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo)
    (installed / "orbi@.service").unlink()
    status = systemd_deploy.unit_status(repo, installed)
    by_unit = {entry["unit"]: entry for entry in status}
    assert by_unit["orbi@.service"]["drifted"] is True
    assert by_unit["orbi@.service"]["missing"] is True
    assert by_unit["orbi@.service"]["installed_sha256"] is None
    assert by_unit["orbi@.timer"]["drifted"] is False


def test_unit_status_reports_missing_template_as_drift(tmp_path):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo)
    (repo / "systemd" / "orbi@.timer").unlink()
    status = systemd_deploy.unit_status(repo, installed)
    by_unit = {entry["unit"]: entry for entry in status}
    assert by_unit["orbi@.timer"]["drifted"] is True
    assert by_unit["orbi@.timer"]["repo_sha256"] is None
    assert by_unit["orbi@.service"]["drifted"] is False


def test_drift_lines_carry_paths_hashes_and_fix_command(tmp_path):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo, mutate="orbi@.service")
    status = systemd_deploy.unit_status(repo, installed)
    lines = systemd_deploy.drift_lines(status)
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("unit_drift unit=orbi@.service ")
    assert f"repo={repo / 'systemd' / 'orbi@.service'}" in line
    assert f"installed={installed / 'orbi@.service'}" in line
    drifted = [e for e in status if e["drifted"]][0]
    assert f"repo_sha256={drifted['repo_sha256']}" in line
    assert f"installed_sha256={drifted['installed_sha256']}" in line
    assert "fix=" in line
    assert "install-units" in line


def test_drift_lines_quote_values_with_spaces(tmp_path):
    # A repo path that really carries a space: the field must be
    # quoted (the pi_activity.quote_value convention) so the line
    # stays parseable.
    spaced = tmp_path / "my repo"
    repo = make_repo(spaced)
    installed = make_installed(spaced, repo, mutate="orbi@.timer")
    status = systemd_deploy.unit_status(repo, installed)
    lines = systemd_deploy.drift_lines(status)
    assert f'repo="{repo / "systemd" / "orbi@.timer"}"' in lines[0]
    assert f'installed="{installed / "orbi@.timer"}"' in lines[0]


def test_drift_lines_is_empty_when_clean(tmp_path):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo)
    status = systemd_deploy.unit_status(repo, installed)
    assert systemd_deploy.drift_lines(status) == []


def test_check_unit_drift_raises_with_all_drifted_units(tmp_path, caplog):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo, mutate="orbi@.service")
    (installed / "orbi@.timer").unlink()
    with caplog.at_level("ERROR"):
        with pytest.raises(
            systemd_deploy.UnitDriftError, match="unit_drift",
        ) as excinfo:
            systemd_deploy.check_unit_drift(repo, installed)
    message = str(excinfo.value)
    assert "orbi@.service" in message
    assert "orbi@.timer" in message
    assert "install-units" in message
    # Every drifted unit is logged as a structured line.
    assert "unit_drift unit=orbi@.service" in caplog.text
    assert "unit_drift unit=orbi@.timer" in caplog.text


def test_check_unit_drift_logs_clean_and_returns(tmp_path, caplog):
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo)
    with caplog.at_level("INFO"):
        systemd_deploy.check_unit_drift(repo, installed)
    assert "unit_drift clean" in caplog.text


def test_check_unit_drift_uses_the_default_installed_dir(monkeypatch,
                                                        tmp_path):
    """Without an explicit dir the check reads the standard user dir
    (the ORBI_UNIT_DIR override is honored)."""
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo)
    monkeypatch.setenv("ORBI_UNIT_DIR", str(installed))
    systemd_deploy.check_unit_drift(repo)  # clean: must not raise


def test_install_units_rejects_an_existing_deployment_for_another_config(
    tmp_path, caplog,
):
    """A second checkout must not silently take ownership of the units."""
    first_repo = make_repo(tmp_path / "first")
    second_repo = make_repo(tmp_path / "second")
    for repo in (first_repo, second_repo):
        (repo / "systemd" / "orbi@.service").write_text(
            '[Service]\nEnvironment="ORBI_CONFIG='
            '{{ORBI_REPO_DIR}}/orbi.toml"\n',
            encoding="utf-8",
        )
    installed = tmp_path / "install"
    systemd_deploy.install_units(
        first_repo, installed, run_command=lambda command, **kwargs: "",
    )
    before = {
        name: (installed / name).read_bytes()
        for name in systemd_deploy.UNIT_NAMES
    }

    with caplog.at_level("ERROR"):
        with pytest.raises(
            systemd_deploy.UnitConflictError,
            match="different ORBI_CONFIG.*uninstall",
        ):
            systemd_deploy.install_units(
                second_repo, installed,
                run_command=lambda command, **kwargs: pytest.fail(
                    "conflicting install must stop before systemctl"
                ),
            )

    assert {
        name: (installed / name).read_bytes()
        for name in systemd_deploy.UNIT_NAMES
    } == before
    assert "unit_conflict" in caplog.text


def test_install_units_allows_reinstall_for_the_same_config(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "systemd" / "orbi@.service").write_text(
        '[Service]\nEnvironment="ORBI_CONFIG='
        '{{ORBI_REPO_DIR}}/orbi.toml"\n',
        encoding="utf-8",
    )
    installed = tmp_path / "install"
    systemd_deploy.install_units(
        repo, installed, run_command=lambda command, **kwargs: "",
    )
    systemd_deploy.install_units(
        repo, installed, run_command=lambda command, **kwargs: "",
    )
    assert systemd_deploy.installed_config(
        installed / "orbi@.service",
    ) == repo.resolve() / "orbi.toml"


def test_install_units_copies_templates_and_reloads(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    installed = tmp_path / "install"
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "0123456789abcdef0123456789abcdef01234567"
        return ""

    result = systemd_deploy.install_units(
        repo, installed, run_command=fake_run,
    )
    # Both templates are installed (overwritten) at the target dir.
    for name in systemd_deploy.UNIT_NAMES:
        assert (installed / name).read_bytes() == (
            repo / "systemd" / name
        ).read_bytes()
    # daemon-reload runs, BOTH timer instances are enabled (idempotent,
    # activates only the timers), and the service is NEVER
    # started/stopped/restarted by the install.
    assert ["systemctl", "--user", "daemon-reload"] in calls
    for instance in systemd_deploy.TIMER_INSTANCES:
        assert [
            "systemctl", "--user", "enable", "--now", instance,
        ] in calls
    for command in calls:
        if command[:2] == ["systemctl", "--user"]:
            assert command[2] in ("daemon-reload", "enable")
    # The deployed commit is the deployment checkout's HEAD.
    assert result["commit"] == "0123456789abcdef0123456789abcdef01234567"
    assert result["installed_dir"] == installed
    assert sorted(result["units"]) == sorted(systemd_deploy.UNIT_NAMES)
    for name in systemd_deploy.UNIT_NAMES:
        entry = result["units"][name]
        assert entry["installed_path"] == installed / name
        assert entry["sha256"] == systemd_deploy.sha256_hex(
            repo / "systemd" / name,
        )


def test_install_units_enables_only_configured_timer_instances(tmp_path):
    """Issue #189: capacity one enables only @1 and stops surplus
    timers without issuing any command for a service instance."""
    repo = make_repo(tmp_path)
    calls: list[list[str]] = []
    systemd_deploy.install_units(
        repo, tmp_path / "install", max_concurrency=1,
        run_command=lambda command, **kwargs: calls.append(command) or "",
    )
    assert [
        "systemctl", "--user", "enable", "--now", "orbi@1.timer",
    ] in calls
    assert [
        "systemctl", "--user", "disable", "--now", "orbi@2.timer",
    ] in calls
    assert not any(".service" in command[-1] for command in calls)


def test_install_units_rejects_capacity_without_a_timer_instance(tmp_path):
    """Issue #189: configured capacity cannot silently exceed the
    fixed template instance set."""
    repo = make_repo(tmp_path)
    calls: list[list[str]] = []
    with pytest.raises(ValueError, match="max_concurrency"):
        systemd_deploy.install_units(
            repo, tmp_path / "install", max_concurrency=3,
            run_command=lambda command, **kwargs: calls.append(command) or "",
        )
    assert calls == []


def test_install_units_migrates_the_legacy_units_once(
    monkeypatch, tmp_path, caplog,
):
    """Issue #149: the pre-#149 deployment (the non-templated units)
    is migrated away by the SAME idempotent install: the legacy timer
    is stopped (a TIMER stop — the service is never started/stopped/
    restarted, a running Runner keeps running), the legacy files are
    removed, and the new templates + both instances are installed."""
    repo = make_repo(tmp_path)
    installed = tmp_path / "install"
    installed.mkdir()
    # The pre-#149 installed units (the non-templated names).
    (installed / "orbi.service").write_text(
        "[Service]\nExecStart=old\n", encoding="utf-8",
    )
    (installed / "orbi.timer").write_text(
        "[Timer]\nOnCalendar=old\n", encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "0123456789abcdef0123456789abcdef01234567"
        return ""

    with caplog.at_level("INFO"):
        systemd_deploy.install_units(
            repo, installed, run_command=fake_run,
        )
    # The legacy timer is stopped (disable --now: a timer stop, never
    # a service stop) and the legacy files are removed.
    assert [
        "systemctl", "--user", "disable", "--now", "orbi.timer",
    ] in calls
    assert not (installed / "orbi.service").exists()
    assert not (installed / "orbi.timer").exists()
    # The new templates are installed and both instances enabled.
    for name in systemd_deploy.UNIT_NAMES:
        assert (installed / name).is_file()
    for instance in systemd_deploy.TIMER_INSTANCES:
        assert [
            "systemctl", "--user", "enable", "--now", instance,
        ] in calls
    # The service is NEVER started/stopped/restarted (only the legacy
    # TIMER is disabled).
    for command in calls:
        if command[:2] == ["systemctl", "--user"]:
            assert command[2] in ("daemon-reload", "enable", "disable")
            if command[2] == "disable":
                assert command[4] == "orbi.timer"
    assert "legacy_units_migrated" in caplog.text


def test_install_units_legacy_migration_is_idempotent(
    monkeypatch, tmp_path,
):
    """Issue #149: the migration runs exactly once — the first install
    on a pre-#149 world disables the legacy timer, and every later
    install (no legacy files left) is a no-op (no disable call, no
    file removal)."""
    repo = make_repo(tmp_path)
    installed = tmp_path / "install"
    installed.mkdir()
    (installed / "orbi.service").write_text(
        "[Service]\nExecStart=old\n", encoding="utf-8",
    )
    (installed / "orbi.timer").write_text(
        "[Timer]\nOnCalendar=old\n", encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "0123456789abcdef0123456789abcdef01234567"
        return ""

    systemd_deploy.install_units(repo, installed, run_command=fake_run)
    first_calls = list(calls)
    calls.clear()
    systemd_deploy.install_units(repo, installed, run_command=fake_run)
    assert ["systemctl", "--user", "disable", "--now",
            "orbi.timer"] in first_calls
    assert ["systemctl", "--user", "disable", "--now",
            "orbi.timer"] not in calls


def test_migrate_legacy_units_removes_only_the_files_that_exist(
    tmp_path,
):
    """Issue #149: a partial legacy deployment (the legacy timer exists
    but the legacy service file does not) is still migrated: the timer
    is stopped and only the EXISTING legacy file is removed (no missing
    file is fabricated or raised)."""
    installed = tmp_path / "install"
    installed.mkdir()
    (installed / "orbi.timer").write_text(
        "[Timer]\nOnCalendar=old\n", encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return ""

    migrated = systemd_deploy.migrate_legacy_units(
        installed, run_command=fake_run,
    )
    assert migrated is True
    assert ["systemctl", "--user", "disable", "--now",
            "orbi.timer"] in calls
    assert not (installed / "orbi.timer").exists()
    assert not (installed / "orbi.service").exists()


def test_migrate_legacy_units_returns_false_without_legacy_files(
    tmp_path,
):
    installed = tmp_path / "install"
    installed.mkdir()
    calls: list[list[str]] = []
    assert systemd_deploy.migrate_legacy_units(
        installed, run_command=lambda command, **kwargs:
        calls.append(command) or "",
    ) is False
    assert calls == []


def test_install_units_is_idempotent_and_overwrites_drift(
    monkeypatch, tmp_path,
):
    repo = make_repo(tmp_path)
    installed = tmp_path / "install"
    first = systemd_deploy.install_units(
        repo, installed, run_command=lambda command, **kwargs: "",
    )
    # Simulate drift after the first install.
    (installed / "orbi@.service").write_text(
        "tampered\n", encoding="utf-8",
    )
    second = systemd_deploy.install_units(
        repo, installed, run_command=lambda command, **kwargs: "",
    )
    # The repo template wins again: the hashes are identical across
    # installs (idempotent) and the drift is gone.
    assert first["units"] == second["units"]
    status = systemd_deploy.unit_status(repo, installed)
    assert all(entry["drifted"] is False for entry in status)


def test_install_units_defaults_to_the_standard_installed_dir(
    monkeypatch, tmp_path,
):
    """Without an explicit dir the install targets the standard user
    dir (here pointed at the test world via ORBI_UNIT_DIR)."""
    repo = make_repo(tmp_path)
    target = tmp_path / "std"
    monkeypatch.setenv("ORBI_UNIT_DIR", str(target))
    result = systemd_deploy.install_units(
        repo, run_command=lambda command, **kwargs: "",
    )
    assert result["installed_dir"] == target
    for name in systemd_deploy.UNIT_NAMES:
        assert (target / name).is_file()


def test_install_units_fails_fast_when_a_systemctl_step_fails(
    monkeypatch, tmp_path,
):
    repo = make_repo(tmp_path)
    installed = tmp_path / "install"

    def fake_run(command, **kwargs):
        if command[:3] == ["systemctl", "--user", "enable"]:
            raise subprocess.CalledProcessError(1, command, stderr="nope")
        return ""

    with pytest.raises(subprocess.CalledProcessError):
        systemd_deploy.install_units(
            repo, installed, run_command=fake_run,
        )


def test_install_units_fails_fast_on_missing_template(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "systemd" / "orbi@.timer").unlink()
    with pytest.raises(FileNotFoundError, match="orbi@.timer"):
        systemd_deploy.install_units(
            repo, tmp_path / "install",
            run_command=lambda command, **kwargs: "",
        )


def test_unit_drift_error_is_a_runtime_error():
    assert issubclass(systemd_deploy.UnitDriftError, RuntimeError)


# --- pre-start self-heal (Issue #142) ---------------------------------------


def test_sync_drifted_units_installs_and_reverifies_clean(
    monkeypatch, tmp_path, caplog,
):
    """Issue #142: a drifted unit (the normal scene after a template
    change merges to main) is synced with the SAME idempotent install
    (copy, daemon-reload, enable the timer — never start/stop/restart
    the service) and re-verified: the tick can continue."""
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo, mutate="orbi@.timer")
    before_sha = systemd_deploy.sha256_hex(installed / "orbi@.timer")
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "0123456789abcdef0123456789abcdef01234567"
        return ""

    with caplog.at_level("INFO"):
        report = systemd_deploy.sync_drifted_units(
            repo, installed, run_command=fake_run,
        )
    # The install ran: daemon-reload + enable BOTH timer instances, and
    # the service is NEVER started/stopped/restarted by the sync.
    assert ["systemctl", "--user", "daemon-reload"] in calls
    for instance in systemd_deploy.TIMER_INSTANCES:
        assert [
            "systemctl", "--user", "enable", "--now", instance,
        ] in calls
    for command in calls:
        if command[:2] == ["systemctl", "--user"]:
            assert command[2] in ("daemon-reload", "enable")
    # The repo template won: the installed unit matches it again.
    status = systemd_deploy.unit_status(repo, installed)
    assert all(entry["drifted"] is False for entry in status)
    # The report carries one entry per unit with the before/after hashes
    # and the deployed commit.
    assert [entry["unit"] for entry in report] == list(
        systemd_deploy.UNIT_NAMES,
    )
    timer = report[1]
    assert timer["before_sha256"] == before_sha
    assert timer["after_sha256"] == systemd_deploy.sha256_hex(
        repo / "systemd" / "orbi@.timer",
    )
    assert timer["commit"] == "0123456789abcdef0123456789abcdef01234567"
    # The structured auto_synced line is logged for the synced unit.
    assert "unit_drift auto_synced unit=orbi@.timer" in caplog.text
    assert f"before_sha256={before_sha}" in caplog.text
    assert "after_sha256=" in caplog.text
    assert "commit=0123456789abcdef0123456789abcdef01234567" in caplog.text


def test_sync_drifted_units_is_a_no_op_when_clean(monkeypatch, tmp_path):
    """Issue #142: with no drift nothing is installed (no systemctl
    calls, no copy) — the preflight only heals a real drift."""
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo)
    calls: list[list[str]] = []
    report = systemd_deploy.sync_drifted_units(
        repo, installed, run_command=lambda command, **kwargs: calls.append(command) or "",
    )
    assert report == []
    assert calls == []


def test_sync_drifted_units_install_failure_propagates(
    monkeypatch, tmp_path,
):
    """Issue #142: a failing install step (here: enabling the timer)
    fails fast — the error propagates, no auto_synced claim is made."""
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo, mutate="orbi@.service")

    def fake_run(command, **kwargs):
        if command[:3] == ["systemctl", "--user", "enable"]:
            raise subprocess.CalledProcessError(1, command, stderr="nope")
        return ""

    with pytest.raises(subprocess.CalledProcessError):
        systemd_deploy.sync_drifted_units(
            repo, installed, run_command=fake_run,
        )


def test_sync_drifted_units_still_drifted_after_sync_fails_fast(
    monkeypatch, tmp_path, caplog,
):
    """Issue #142: the re-verify is the same hash check — if the units
    still drift after the sync (the scene is not recoverable by the
    idempotent install), the preflight fails fast with the structured
    `unit_drift` lines and `UnitDriftError` (no slot, no claim)."""
    repo = make_repo(tmp_path)
    installed = make_installed(tmp_path, repo, mutate="orbi@.timer")
    # A second process overwrites the installed unit right after the
    # copy: the re-verify sees the drift again.
    real_write_bytes = Path.write_bytes

    def overwrite_after_copy(self, data):
        real_write_bytes(self, data)
        if self.name == "orbi@.timer" and str(self).startswith(
            str(installed),
        ):
            real_write_bytes(self, data + b"# drift\n")

    monkeypatch.setattr(Path, "write_bytes", overwrite_after_copy)
    with caplog.at_level("ERROR"):
        with pytest.raises(
            systemd_deploy.UnitDriftError, match="unit_drift",
        ):
            systemd_deploy.sync_drifted_units(
                repo, installed, run_command=lambda command, **kwargs: "",
            )
    assert "unit_drift unit=orbi@.timer" in caplog.text
    assert "fix=orbi install-units" in caplog.text
    assert "unit_drift auto_synced" not in caplog.text


# --- Issue #262: template path placeholder + renamed-unit migration ----------


def test_render_unit_template_substitutes_the_repo_dir(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    template = (
        "[Service]\n"
        "WorkingDirectory={{ORBI_REPO_DIR}}\n"
        'ExecStartPre=flock {{ORBI_REPO_DIR}}/.orbi/base-sync.lock -c "x"\n'
    )
    rendered = systemd_deploy.render_unit_template(template, repo)
    assert "{{ORBI_REPO_DIR}}" not in rendered
    assert f"WorkingDirectory={repo}" in rendered
    assert f"flock {repo}/.orbi/base-sync.lock" in rendered


def test_render_unit_template_no_placeholder_is_unchanged(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    template = "WorkingDirectory=/fixed/path\n"
    assert systemd_deploy.render_unit_template(template, repo) == template


def test_install_units_writes_the_rendered_unit_with_the_real_path(tmp_path):
    repo = tmp_path / "checkout"
    (repo / "systemd").mkdir(parents=True)
    (repo / "systemd" / "orbi@.service").write_text(
        "[Service]\n"
        "WorkingDirectory={{ORBI_REPO_DIR}}\n"
        'Environment="ORBI_CONFIG={{ORBI_REPO_DIR}}/orbi.toml"\n'
        "EnvironmentFile=-{{ORBI_REPO_DIR}}/.orbi/env\n"
        'ExecStartPre=/usr/bin/flock {{ORBI_REPO_DIR}}/.orbi/base-sync.lock '
        '-c \'git fetch\'\n'
        'ExecStartPre=/usr/bin/flock {{ORBI_REPO_DIR}}/.orbi/base-sync.lock '
        "-c '%h/.local/bin/orbi --version || uv tool install --force "
        "--reinstall --editable --python /usr/bin/python3 "
        "{{ORBI_REPO_DIR}}'\n"
        "ExecStart=%h/.local/bin/orbi\n",
        encoding="utf-8",
    )
    (repo / "systemd" / "orbi@.timer").write_text(
        "[Timer]\nOnCalendar=*-*-* *:00/5\n", encoding="utf-8",
    )
    installed = tmp_path / "install"
    systemd_deploy.install_units(
        repo, installed, run_command=lambda command, **kwargs: "",
    )
    service = (installed / "orbi@.service").read_text(encoding="utf-8")
    # No placeholder or hardcoded path survives; the real checkout path is
    # substituted everywhere, while the home-relative bits stay %h.
    assert "{{ORBI_REPO_DIR}}" not in service
    assert "%h/Documents/orbi/orbi" not in service
    assert f"WorkingDirectory={repo}" in service
    assert f'ORBI_CONFIG={repo}/orbi.toml' in service
    assert f"EnvironmentFile=-{repo}/.orbi/env" in service
    assert f"flock {repo}/.orbi/base-sync.lock" in service
    assert f"python3 {repo}'" in service
    assert "ExecStart=%h/.local/bin/orbi" in service


def test_unit_status_is_clean_when_installed_matches_the_rendered_template(
    tmp_path,
):
    repo = tmp_path / "checkout"
    (repo / "systemd").mkdir(parents=True)
    (repo / "systemd" / "orbi@.service").write_text(
        "WorkingDirectory={{ORBI_REPO_DIR}}\n", encoding="utf-8",
    )
    (repo / "systemd" / "orbi@.timer").write_text(
        "[Timer]\nOnCalendar=*-*-* *:00/5\n", encoding="utf-8",
    )
    installed = tmp_path / "install"
    systemd_deploy.install_units(
        repo, installed, run_command=lambda command, **kwargs: "",
    )
    status = systemd_deploy.unit_status(repo, installed)
    for entry in status:
        assert entry["drifted"] is False
        assert entry["missing"] is False
        assert entry["repo_sha256"] == entry["installed_sha256"]


def test_unit_status_drifts_when_installed_differs_from_the_rendered_template(
    tmp_path,
):
    repo = tmp_path / "checkout"
    (repo / "systemd").mkdir(parents=True)
    (repo / "systemd" / "orbi@.service").write_text(
        "WorkingDirectory={{ORBI_REPO_DIR}}\n", encoding="utf-8",
    )
    (repo / "systemd" / "orbi@.timer").write_text(
        "[Timer]\nOnCalendar=*-*-* *:00/5\n", encoding="utf-8",
    )
    installed = tmp_path / "install"
    systemd_deploy.install_units(
        repo, installed, run_command=lambda command, **kwargs: "",
    )
    # Tamper the installed unit so it no longer matches the rendered
    # template (e.g. a hand edit to a different path).
    (installed / "orbi@.service").write_text(
        "WorkingDirectory=/somewhere/else\n", encoding="utf-8",
    )
    status = systemd_deploy.unit_status(repo, installed)
    service = [e for e in status if e["unit"] == "orbi@.service"][0]
    assert service["drifted"] is True
    assert service["repo_sha256"] != service["installed_sha256"]


def test_migrate_renamed_units_disables_instances_and_removes_files(
    tmp_path, caplog,
):
    installed = tmp_path / "install"
    installed.mkdir()
    (installed / "muyan-pilot@.service").write_text(
        "[Service]\nExecStart=old\n", encoding="utf-8",
    )
    (installed / "muyan-pilot@.timer").write_text(
        "[Timer]\nOnCalendar=old\n", encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return ""

    with caplog.at_level("INFO"):
        migrated = systemd_deploy.migrate_renamed_units(
            installed, run_command=fake_run,
        )
    assert migrated is True
    # Both old timer instances are stopped (TIMER stops — the service is
    # never started/stopped/restarted).
    for instance in ("muyan-pilot@1.timer", "muyan-pilot@2.timer"):
        assert [
            "systemctl", "--user", "disable", "--now", instance,
        ] in calls
    # The old unit files are removed.
    assert not (installed / "muyan-pilot@.service").exists()
    assert not (installed / "muyan-pilot@.timer").exists()
    assert "renamed_units_migrated" in caplog.text


def test_migrate_renamed_units_is_a_noop_without_the_old_files(tmp_path):
    installed = tmp_path / "install"
    installed.mkdir()
    calls: list[list[str]] = []
    assert systemd_deploy.migrate_renamed_units(
        installed, run_command=lambda command, **kwargs:
        calls.append(command) or "",
    ) is False
    assert calls == []


def test_migrate_renamed_units_removes_only_the_files_that_exist(tmp_path):
    installed = tmp_path / "install"
    installed.mkdir()
    (installed / "muyan-pilot@.timer").write_text(
        "[Timer]\nOnCalendar=old\n", encoding="utf-8",
    )
    calls: list[list[str]] = []
    migrated = systemd_deploy.migrate_renamed_units(
        installed, run_command=lambda command, **kwargs:
        calls.append(command) or "",
    )
    assert migrated is True
    assert not (installed / "muyan-pilot@.timer").exists()
    assert not (installed / "muyan-pilot@.service").exists()


def test_install_units_migrates_the_renamed_units_when_present(tmp_path):
    repo = make_repo(tmp_path)
    installed = tmp_path / "install"
    installed.mkdir()
    (installed / "muyan-pilot@.service").write_text(
        "[Service]\nExecStart=old\n", encoding="utf-8",
    )
    (installed / "muyan-pilot@.timer").write_text(
        "[Timer]\nOnCalendar=old\n", encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "0123456789abcdef0123456789abcdef01234567"
        return ""

    systemd_deploy.install_units(repo, installed, run_command=fake_run)
    for instance in ("muyan-pilot@1.timer", "muyan-pilot@2.timer"):
        assert [
            "systemctl", "--user", "disable", "--now", instance,
        ] in calls
    assert not (installed / "muyan-pilot@.service").exists()
    assert not (installed / "muyan-pilot@.timer").exists()
    # The new templates are installed and both new instances enabled.
    for name in systemd_deploy.UNIT_NAMES:
        assert (installed / name).is_file()
    for instance in systemd_deploy.TIMER_INSTANCES:
        assert [
            "systemctl", "--user", "enable", "--now", instance,
        ] in calls
