import os
from pathlib import Path
import shlex
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_staging_wrapper_changes_to_hermes_home_before_gateway_start() -> None:
    wrapper = (ROOT / "docker" / "railway-staging-wrapper.sh").read_text()

    home = 'home="${HERMES_HOME:-/opt/data}"'
    export_home = 'export HERMES_HOME="$home"'
    first_config = "hermes config set model.default"
    change_directory = 'cd "$home"'
    gateway_start = "exec /opt/hermes/docker/main-wrapper.sh gateway run"

    assert home in wrapper
    assert wrapper.startswith("#!/command/with-contenv sh\n")
    assert wrapper.index(export_home) < wrapper.index(first_config)
    assert wrapper.index(change_directory) < wrapper.index(gateway_start)


def test_railway_overlay_is_baked_and_uses_staging_entrypoint() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()

    sync_lines = [line for line in dockerfile.splitlines() if line.startswith("RUN uv sync ")]
    assert len(sync_lines) == 1
    sync_args = shlex.split(sync_lines[0].removeprefix("RUN "))
    assert sum(
        sync_args[index : index + 2] == ["--extra", "otlp"]
        for index in range(len(sync_args) - 1)
    ) == 1
    assert "/opt/hermes/docker/railway-staging-wrapper.sh" in dockerfile
    assert "/opt/hermes/docker/railway-direct-wrapper.sh" in dockerfile
    assert (
        'ENTRYPOINT [ "/init", "/opt/hermes/docker/railway-staging-wrapper.sh" ]'
        in dockerfile
    )


def test_railway_wrappers_cannot_reset_the_persistent_home() -> None:
    for name in ("railway-staging-wrapper.sh", "railway-direct-wrapper.sh"):
        wrapper = (ROOT / "docker" / name).read_text()

        assert "HERMES_RESET_GENERATION" not in wrapper
        assert "rm -rf" not in wrapper
        assert "chown -R" not in wrapper


def test_railway_wrappers_enable_private_staging_health_export() -> None:
    required = (
        "monitoring.gateway_health_export.enabled true",
        "monitoring.gateway_health_export.metrics_enabled true",
        "monitoring.gateway_health_export.diagnostic_events_enabled true",
        "monitoring.export.otlp.enabled true",
        "http://otel-collector.railway.internal:4318/v1/traces",
    )
    for name in ("railway-staging-wrapper.sh", "railway-direct-wrapper.sh"):
        wrapper = (ROOT / "docker" / name).read_text()

        for setting in required:
            assert setting in wrapper


def test_monitoring_settings_land_in_the_exported_hermes_home(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path)
    settings = (
        ("monitoring.gateway_health_export.enabled", "true"),
        ("monitoring.gateway_health_export.metrics_enabled", "true"),
        ("monitoring.gateway_health_export.diagnostic_events_enabled", "true"),
        ("monitoring.gateway_health_export.warning_error_events_enabled", "true"),
        ("monitoring.gateway_health_export.export_interval_seconds", "15"),
        ("monitoring.export.otlp.enabled", "true"),
        (
            "monitoring.export.otlp.endpoint",
            "http://otel-collector.railway.internal:4318/v1/traces",
        ),
    )
    for key, value in settings:
        subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "config", "set", key, value],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    gateway = config["monitoring"]["gateway_health_export"]
    otlp = config["monitoring"]["export"]["otlp"]
    assert gateway == {
        "enabled": True,
        "metrics_enabled": True,
        "diagnostic_events_enabled": True,
        "warning_error_events_enabled": True,
        "export_interval_seconds": 15,
    }
    assert otlp == {
        "enabled": True,
        "endpoint": "http://otel-collector.railway.internal:4318/v1/traces",
    }