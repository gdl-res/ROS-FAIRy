"""Harvest connected sensors and hardware devices.

Read-only and non-invasive. Every external command has a timeout. Permission
errors and missing binaries are recorded as partial results, never fatal.

Spec: specs/data_model.md — HardwareDevice
"""

import logging
import re
import shutil
import subprocess
from glob import glob
from pathlib import Path

log = logging.getLogger("fair_ros.harvest.hardware_devices")

HARDWARE_CMD_TIMEOUT_S = 10
_LSUSB_VERBOSE_TIMEOUT_S = 20
HARDWARE_TOTAL_TIMEOUT_S = 60

_UDEV_WHITELIST = frozenset({
    "DEVNAME", "DEVTYPE", "SUBSYSTEM", "ID_BUS",
    "ID_VENDOR", "ID_VENDOR_ID", "ID_MODEL", "ID_MODEL_ID",
    "ID_SERIAL_SHORT", "ID_USB_CLASS", "ID_USB_SUBCLASS",
    "ID_DRIVER", "ID_PATH", "MAJOR", "MINOR",
})

_DMESG_PATTERN = re.compile(
    r"usb|video|tty|camera|serial|sensor", re.IGNORECASE)


def _run(cmd: list[str], timeout: float = HARDWARE_CMD_TIMEOUT_S
         ) -> "subprocess.CompletedProcess | None":
    """Run a command; return None if binary missing, timed out, or OS error."""
    if not shutil.which(cmd[0]):
        return None
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.debug("timed out: %s", " ".join(cmd))
        return None
    except (OSError, FileNotFoundError) as exc:
        log.debug("command error: %s: %s", " ".join(cmd), exc)
        return None


def _parse_lsusb(stdout: str) -> list[dict]:
    """Parse `lsusb` one-line-per-device output."""
    devices = []
    for line in stdout.splitlines():
        m = re.match(
            r"Bus (\d+) Device (\d+): ID ([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)",
            line.strip())
        if not m:
            continue
        bus, dev, vid, pid, rest = m.groups()
        devices.append({
            "device_class": "usb",
            "vendor_id": vid.lower(),
            "product_id": pid.lower(),
            "vendor_name": None,
            "product_name": rest.strip() or None,
            "serial_number": None,
            "device_path": None,
            "bus_path": f"Bus {bus} Device {dev}",
            "driver": None,
            "source_command": "lsusb",
            "udev_properties": None,
        })
    return devices


def _serials_from_verbose(verbose: str) -> dict[str, str]:
    """Map 'Bus NNN Device NNN' → iSerial string from `lsusb -v` output."""
    serials: dict[str, str] = {}
    current: str | None = None
    for line in verbose.splitlines():
        m = re.match(r"Bus (\d+) Device (\d+):", line)
        if m:
            current = f"Bus {m.group(1)} Device {m.group(2)}"
        m2 = re.match(r"\s+iSerial\s+\d+\s+(\S+)", line)
        if m2 and current:
            sn = m2.group(1)
            if sn not in ("0", ""):
                serials[current] = sn
    return serials


def _parse_lspci(stdout: str) -> list[dict]:
    """Parse `lspci -mm` machine-readable output (one device per line)."""
    devices = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Fields may be quoted strings or bare tokens.
        parts = re.findall(r'"[^"]*"|[^\s"]+', line)
        def unq(s: str) -> str | None:
            s = s.strip('"').strip()
            return s or None
        try:
            slot = unq(parts[0])
            vendor = unq(parts[2]) if len(parts) > 2 else None
            device = unq(parts[3]) if len(parts) > 3 else None
        except IndexError:
            continue
        devices.append({
            "device_class": "pci",
            "vendor_id": None,
            "product_id": None,
            "vendor_name": vendor,
            "product_name": device,
            "serial_number": None,
            "device_path": slot,
            "bus_path": slot,
            "driver": None,
            "source_command": "lspci",
            "udev_properties": None,
        })
    return devices


def _glob_devices(pattern: str, device_class: str) -> list[dict]:
    return [
        {
            "device_class": device_class,
            "vendor_id": None, "product_id": None,
            "vendor_name": None, "product_name": None,
            "serial_number": None,
            "device_path": p,
            "bus_path": None, "driver": None,
            "source_command": f"glob:{pattern}",
            "udev_properties": None,
        }
        for p in sorted(glob(pattern))
    ]


def _serial_by_id() -> list[dict]:
    """Resolve /dev/serial/by-id/* symlinks; the link name carries model info."""
    by_id = Path("/dev/serial/by-id")
    if not by_id.is_dir():
        return []
    devices = []
    for link in sorted(by_id.iterdir()):
        try:
            target = str(link.resolve())
        except OSError:
            target = None
        devices.append({
            "device_class": "serial",
            "vendor_id": None, "product_id": None,
            "vendor_name": None,
            "product_name": link.name,
            "serial_number": None,
            "device_path": target,
            "bus_path": None, "driver": None,
            "source_command": "glob:/dev/serial/by-id/*",
            "udev_properties": None,
        })
    return devices


def _parse_v4l2_paths(stdout: str) -> list[str]:
    return [line.strip() for line in stdout.splitlines()
            if line.strip().startswith("/dev/video")]


def _parse_udev_props(stdout: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in stdout.splitlines():
        key, _, val = line.partition("=")
        key = key.strip()
        if key in _UDEV_WHITELIST:
            props[key] = val.strip()
    return props


def _enrich_udev(devices: list[dict], max_devices: int = 10) -> None:
    """Add udev properties to the first `max_devices` entries with a path."""
    if not shutil.which("udevadm"):
        return
    enriched = 0
    for dev in devices:
        if enriched >= max_devices:
            break
        path = dev.get("device_path")
        if not path:
            continue
        r = _run(["udevadm", "info", "--query=property", f"--name={path}"],
                 timeout=5)
        enriched += 1
        if r is None or r.returncode != 0:
            continue
        props = _parse_udev_props(r.stdout)
        if not props:
            continue
        dev["udev_properties"] = props
        dev["driver"] = dev["driver"] or props.get("ID_DRIVER")
        dev["serial_number"] = dev["serial_number"] or props.get("ID_SERIAL_SHORT")
        dev["vendor_name"] = dev["vendor_name"] or props.get("ID_VENDOR")
        dev["vendor_id"] = dev["vendor_id"] or props.get("ID_VENDOR_ID")
        dev["product_name"] = dev["product_name"] or props.get("ID_MODEL")
        dev["product_id"] = dev["product_id"] or props.get("ID_MODEL_ID")


def harvest() -> dict:
    """Collect hardware device metadata. Never raises.

    Returns a dict with keys:
      "devices"       — list of HardwareDevice-shaped dicts
      "lsusb_verbose" — raw lsusb -v text, or None
      "dmesg_usb"     — filtered dmesg lines, or None
      "status"        — "ok", "partial", or "skipped"
    """
    try:
        return _harvest()
    except Exception as exc:
        log.warning("hardware_devices harvest failed entirely: %s", exc)
        return {"devices": [], "lsusb_verbose": None, "dmesg_usb": None,
                "status": "partial"}


def _harvest() -> dict:
    devices: list[dict] = []
    partial = False

    # 1. USB — basic list
    r = _run(["lsusb"])
    if r is not None:
        if r.returncode == 0:
            devices.extend(_parse_lsusb(r.stdout))
        else:
            partial = True

    # 2. USB — verbose for serial numbers
    lsusb_verbose: str | None = None
    r_v = _run(["lsusb", "-v"], timeout=_LSUSB_VERBOSE_TIMEOUT_S)
    if r_v is not None:
        if r_v.returncode == 0:
            lsusb_verbose = r_v.stdout
            serials = _serials_from_verbose(r_v.stdout)
            for dev in devices:
                if dev["device_class"] == "usb" and dev["bus_path"]:
                    sn = serials.get(dev["bus_path"])
                    if sn:
                        dev["serial_number"] = sn
        else:
            # Permission denied is the common case — not a hard failure
            partial = True
            log.debug("lsusb -v returned %d: %s", r_v.returncode,
                      r_v.stderr[:120] if r_v.stderr else "")

    # 3. PCI
    r = _run(["lspci", "-mm"])
    if r is not None:
        if r.returncode == 0:
            devices.extend(_parse_lspci(r.stdout))
        else:
            partial = True

    # 4. Video devices — glob + v4l2-ctl
    video_devs = _glob_devices("/dev/video*", "video")
    existing_video_paths = {d["device_path"] for d in video_devs}
    r = _run(["v4l2-ctl", "--list-devices"])
    if r is not None and r.returncode == 0:
        for path in _parse_v4l2_paths(r.stdout):
            if path not in existing_video_paths:
                video_devs.append({
                    "device_class": "video",
                    "vendor_id": None, "product_id": None,
                    "vendor_name": None, "product_name": None,
                    "serial_number": None, "device_path": path,
                    "bus_path": None, "driver": None,
                    "source_command": "v4l2-ctl",
                    "udev_properties": None,
                })
                existing_video_paths.add(path)
    devices.extend(video_devs)

    # 5. Serial devices — ttyUSB*, ttyACM*, by-id links
    seen: set[str] = set()
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*"):
        for dev in _glob_devices(pattern, "serial"):
            p = dev["device_path"]
            if p and p not in seen:
                seen.add(p)
                devices.append(dev)
    for dev in _serial_by_id():
        target = dev["device_path"]
        if target and target in seen:
            # Enrich the existing ttyUSB entry with the human-readable name
            for existing in devices:
                if existing["device_path"] == target and \
                        existing["product_name"] is None:
                    existing["product_name"] = dev["product_name"]
        else:
            if target:
                seen.add(target)
            devices.append(dev)

    # 6. udevadm enrichment (caps at 10 devices to avoid O(n) slowdown)
    _enrich_udev(devices)

    # 7. dmesg — USB/video/serial kernel messages only
    dmesg_usb: str | None = None
    r = _run(["dmesg", "--level=warn,err,info", "-T"])
    if r is None:
        # Try without flags (older kernels don't support --level)
        r = _run(["dmesg"])
    if r is not None and r.returncode == 0:
        filtered = "\n".join(
            line for line in r.stdout.splitlines()
            if _DMESG_PATTERN.search(line))
        dmesg_usb = filtered or None
    elif r is not None:
        partial = True

    # Determine status
    commands_checked = any(shutil.which(c) for c in
                           ("lsusb", "lspci", "v4l2-ctl"))
    has_glob_results = bool(
        glob("/dev/video*") or glob("/dev/ttyUSB*") or glob("/dev/ttyACM*"))
    if not devices and not commands_checked and not has_glob_results:
        status = "skipped"
    elif partial:
        status = "partial"
    else:
        status = "ok"

    return {
        "devices": devices,
        "lsusb_verbose": lsusb_verbose,
        "dmesg_usb": dmesg_usb,
        "status": status,
    }
