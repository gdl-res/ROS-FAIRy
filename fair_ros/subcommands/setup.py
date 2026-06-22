"""ros2 fair setup — one-time per-robot configuration wizard (specs/cli.md).

Engineer-facing: the one place where ROS jargon is acceptable. Idempotent;
re-running shows current values as defaults.
"""

import grp
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from fair_ros.subcommands import VerbExtension, _configure_logging
from fair_ros.utils import paths

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SLUG_RE = re.compile(r"^[a-z0-9_]+$")
SENSOR_TYPES = ["gps", "lidar", "camera", "imu", "sonar", "other"]
SERVICE_NAME = "fair-ros-watchdog.service"
GROUP_NAME = "fair-ros"
MAX_ATTEMPTS = 3


class SetupAborted(Exception):
    pass


def _ask(console: Console, prompt: str, validate=None, default=None,
         reason: str = "that doesn't look right", **kwargs) -> str:
    for _ in range(MAX_ATTEMPTS):
        answer = Prompt.ask(prompt, console=console,
                            **({"default": default} if default else {}),
                            **kwargs).strip()
        if answer and (validate is None or validate(answer)):
            return answer
        console.print(f"[yellow]Sorry, {reason}.[/yellow]")
    raise SetupAborted(f"Gave up on '{prompt}' after {MAX_ATTEMPTS} attempts.")


def _existing_identity() -> dict:
    path = paths.robot_identity_path()
    if not path.is_file():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}


def _live_topics(console: Console) -> list[str]:
    try:
        out = subprocess.run(["ros2", "topic", "list"], capture_output=True,
                             text=True, timeout=10)
        if out.returncode == 0:
            return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return []


def ask_robot(console: Console, current: dict) -> dict:
    robot = current.get("robot") or {}
    owner = current.get("owner") or {}
    answers = {
        "name": _ask(console, "Robot name",
                     validate=lambda s: len(s) <= 40,
                     default=robot.get("name"),
                     reason="40 characters max"),
        "platform": _ask(console, "Platform (make and model)",
                         default=robot.get("platform")),
        "serial_number": _ask(console, "Serial number / asset tag",
                              default=robot.get("serial_number")),
    }
    org = _ask(console, "Owning organization",
               default=owner.get("organization"))
    email = _ask(console, "Contact email",
                 validate=lambda s: bool(EMAIL_RE.match(s)),
                 default=owner.get("contact_email"),
                 reason="that doesn't look like an email address")
    return {"robot": answers,
            "owner": {"organization": org, "contact_email": email,
                      **({"default_license": owner["default_license"]}
                         if owner.get("default_license") else {})}}


def ask_sensors(console: Console, current: dict) -> tuple[list, list]:
    sensors: list[dict] = []
    calibrations: list[dict] = []
    live = _live_topics(console)
    if live:
        console.print(f"[dim]Live topics seen: {', '.join(live[:12])}"
                      f"{' …' if len(live) > 12 else ''}[/dim]")
    while Confirm.ask("Add a sensor?", default=True, console=console):
        seen = {s["sensor_id"] for s in sensors}
        sid = _ask(console, "Sensor id (lowercase slug, e.g. gps0)",
                   validate=lambda s, seen=seen: bool(SLUG_RE.match(s))
                   and s not in seen,
                   reason="lowercase letters/digits/underscore, and unique")
        stype = Prompt.ask("Type", choices=SENSOR_TYPES, console=console)
        make = _ask(console, "Make and model")
        topic = _ask(console, "Topic", validate=lambda s: s.startswith("/"),
                     reason="topics start with /")
        if live and topic not in live:
            if not Confirm.ask(f"{topic} isn't being published right now — "
                               f"use it anyway?", default=True,
                               console=console):
                continue
        frame = Prompt.ask("TF frame id (Enter to skip)", default="",
                           console=console).strip() or None
        sensor = {"sensor_id": sid, "type": stype, "make_model": make,
                  "topic": topic}
        if frame:
            sensor["frame_id"] = frame
        cal_path = Prompt.ask("Calibration file path (Enter to skip)",
                              default="", console=console).strip()
        if cal_path:
            for _ in range(MAX_ATTEMPTS - 1):
                if Path(cal_path).is_file():
                    break
                console.print("[yellow]That file doesn't exist.[/yellow]")
                cal_path = Prompt.ask("Calibration file path (Enter to "
                                      "skip)", default="",
                                      console=console).strip()
                if not cal_path:
                    break
            if cal_path and Path(cal_path).is_file():
                cal_name = f"{sid}_cal"
                calibrations.append({"name": cal_name,
                                     "source_path": cal_path})
                sensor["calibration"] = cal_name
        sensors.append(sensor)
    return sensors, calibrations


def review(console: Console, config: dict) -> bool:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Robot", f"{config['robot']['name']} "
                           f"({config['robot']['platform']})")
    table.add_row("Serial", config["robot"]["serial_number"])
    table.add_row("Owner", f"{config['owner']['organization']} "
                           f"<{config['owner']['contact_email']}>")
    for sensor in config.get("sensors", []):
        cal = f", cal: {sensor['calibration']}" if sensor.get(
            "calibration") else ""
        table.add_row(f"Sensor {sensor['sensor_id']}",
                      f"{sensor['type']} — {sensor['make_model']} on "
                      f"{sensor['topic']}{cal}")
    console.print(Panel(table, title="Configuration review",
                        border_style="cyan"))
    return Confirm.ask("Write this configuration?", default=True,
                       console=console)


def write_identity(config: dict) -> None:
    config_dir = paths.config_dir()
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    path = paths.robot_identity_path()
    path.write_text(yaml.safe_dump(config, sort_keys=False,
                                   allow_unicode=True))
    path.chmod(0o644)


def create_dirs() -> None:
    try:
        gid = grp.getgrnam(GROUP_NAME).gr_gid
    except KeyError:
        subprocess.run(["groupadd", "--system", GROUP_NAME], check=False)
        try:
            gid = grp.getgrnam(GROUP_NAME).gr_gid
        except KeyError:
            gid = -1
    for directory in (paths.var_dir(), paths.spool_dir(), paths.bags_dir(),
                      paths.archive_dir()):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o2775)
        if gid >= 0:
            try:
                os.chown(directory, -1, gid)
            except PermissionError:
                pass


def install_service(console: Console) -> bool:
    unit_src = Path(__file__).resolve().parent.parent / "watchdog" / \
        SERVICE_NAME
    shutil.copy(unit_src, Path("/etc/systemd/system") / SERVICE_NAME)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", SERVICE_NAME],
                   check=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        active = subprocess.run(["systemctl", "is-active", SERVICE_NAME],
                                capture_output=True, text=True)
        if active.stdout.strip() == "active" and \
                paths.watchdog_state_path().is_file():
            return True
        time.sleep(0.5)
    return False


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    if os.geteuid() != 0:
        console.print("[red]Setup needs root (it installs a system service "
                      "and writes /etc). Re-run with sudo.[/red]")
        return 1
    if shutil.which("ros2") is None:
        console.print("[red]ros2 is not on PATH. Source your ROS 2 "
                      "environment first.[/red]")
        return 1
    if shutil.which("docker") is None:
        console.print("[yellow]Docker not found — container snapshots will "
                      "be skipped. That's fine if this robot doesn't use "
                      "containers.[/yellow]")

    current = _existing_identity()
    if current:
        console.print("[dim]Existing configuration found — current values "
                      "are offered as defaults.[/dim]")
    try:
        config = ask_robot(console, current)
        sensors, calibrations = ask_sensors(console, current)
        if sensors:
            config["sensors"] = sensors
        if calibrations:
            config["calibrations"] = calibrations
        if not review(console, config):
            console.print("Nothing was written.")
            return 0
    except SetupAborted as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    write_identity(config)
    create_dirs()
    if install_service(console):
        console.print(Panel("Setup complete. fair-ros is now watching for "
                            "recordings.", border_style="green"))
        return 0
    console.print("[red]The watchdog service didn't come up within 10 "
                  "seconds. Check: journalctl -u fair-ros-watchdog[/red]")
    return 1


class SetupVerb(VerbExtension):
    """One-time robot setup: identity file, directories, watchdog service."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return run(args)
