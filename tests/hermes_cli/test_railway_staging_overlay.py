import os
from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[2]
WRAPPERS = ("railway-staging-wrapper.sh", "railway-direct-wrapper.sh")
SEEDER = ROOT / "docker" / "seed_flex_dotfiles.sh"
RESOURCE_HELPER = ROOT / "docker" / "ensure_monitoring_resource_attributes.py"
MARKER = "HERMES-FLEX-ENV"


def run_seeder(home: Path, *, check: bool = False) -> subprocess.CompletedProcess[str]:
    assert SEEDER.is_file(), "the dedicated unprivileged seeder must exist"
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    return subprocess.run(
        ["sh", str(SEEDER)],
        cwd=ROOT,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def test_flex_dotfile_seeder_rejects_symlinks_without_touching_target(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "target"
    target.write_text("root-owned-content\n")
    (home / ".profile").symlink_to(target)

    result = run_seeder(home)

    assert result.returncode != 0
    assert target.read_text() == "root-owned-content\n"
    assert MARKER not in target.read_text()


def test_flex_dotfile_seeder_rejects_non_regular_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".profile").mkdir()

    result = run_seeder(home)

    assert result.returncode != 0


def test_flex_dotfile_seeder_is_idempotent_for_regular_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    profile = home / ".profile"
    profile.write_text("existing\n")

    for _ in range(2):
        run_seeder(home, check=True)

    assert profile.read_text().startswith("existing\n")
    assert profile.read_text().count(MARKER) == 2
    assert (home / ".bashrc").read_text().count(MARKER) == 2


def test_railway_wrappers_seed_dotfiles_after_privilege_drop() -> None:
    expected = {
        "railway-staging-wrapper.sh": (
            "s6-setuidgid hermes /opt/hermes/docker/seed_flex_dotfiles.sh"
        ),
        "railway-direct-wrapper.sh": (
            "/command/s6-setuidgid hermes "
            "/opt/hermes/docker/seed_flex_dotfiles.sh"
        ),
    }
    for name in WRAPPERS:
        wrapper = (ROOT / "docker" / name).read_text()

        assert expected[name] in wrapper
        assert 'cat >> "$rc"' not in wrapper
        assert 'chown hermes:hermes "$home/.profile"' not in wrapper


def test_monitoring_resource_helper_writes_flat_semconv_key(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "monitoring": {
                    "gateway_health_export": {
                        "resource_attributes": {
                            "deployment.environment.name": "production",
                            "service.name": "hermes-gateway",
                        }
                    }
                }
            },
            sort_keys=False,
        )
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path)

    subprocess.run(
        [sys.executable, str(RESOURCE_HELPER), "staging"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    config = yaml.safe_load(config_path.read_text())
    attrs = config["monitoring"]["gateway_health_export"]["resource_attributes"]
    assert attrs == {
        "deployment.environment.name": "staging",
        "service.name": "hermes-gateway",
    }
    assert "deployment" not in attrs

    from agent.monitoring.gateway_health_export import _runtime_resource_attributes

    exported = _runtime_resource_attributes(config, telemetry_scope="gateway_health")
    assert exported["deployment.environment.name"] == "staging"


def test_railway_wrappers_stamp_staging_with_flat_key_helper_and_bound_status() -> None:
    helper = (
        "/opt/hermes/docker/ensure_monitoring_resource_attributes.py staging"
    )
    bad_nested_set = (
        "config set monitoring.gateway_health_export.resource_attributes."
        "deployment.environment.name staging"
    )
    for name in WRAPPERS:
        wrapper = (ROOT / "docker" / name).read_text()

        assert helper in wrapper
        assert bad_nested_set not in wrapper
        assert "display.platforms.slack.live_status verb" in wrapper
        assert wrapper.index(helper) < wrapper.index(
            "monitoring.gateway_health_export.enabled true"
        )
