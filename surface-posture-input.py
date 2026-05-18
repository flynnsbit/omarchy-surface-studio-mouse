#!/usr/bin/env python3
"""
surface-posture-input  (system service, runs as root)
=====================================================

Posture-aware input enabling/disabling for the Microsoft Surface
Laptop Studio on the linux-surface kernel.

Mechanism: writes 0/1 to /sys/class/input/<input>/inhibited for the
specific Surface input devices. This is a kernel-level toggle that
hides the device from ALL userspace consumers (Hyprland, libinput,
TTY, X11) and back. It is bulletproof: if this daemon crashes or
exits, systemd ExecStopPost resets every device to inhibited=0, so
the worst case is a brief input outage, never a lost touchpad.

Posture is read from:
    /sys/devices/platform/MSHW0123:00/01:26:01:00:01/state
Trigger:
    /dev/input/event12 (POS Tablet Mode Switch) for instant binary
    transitions, plus a 500 ms safety re-read for stage<->tablet
    transitions that the input device may not emit.

Policy (per user spec):
    laptop  -> keyboard ON,  touchpad ON,  pointstick ON
    slate   -> keyboard OFF, touchpad ON,  pointstick ON
    tablet  -> keyboard OFF, touchpad OFF, pointstick OFF
    any other / unreadable -> everything ON (fail-safe), logged
"""

from __future__ import annotations

import os
import re
import select
import signal
import sys
import time
from pathlib import Path

STATE_PATH = Path(
    "/sys/devices/platform/MSHW0123:00/01:26:01:00:01/state"
)
SWITCH_EVENT_DEVICE = Path("/dev/input/event12")
POLL_TIMEOUT_S = 0.5

# Match by the canonical kernel device name (input.name attribute).
# Each Surface "hardware" exposes multiple input subdevices (main
# keys + function/consumer keys for the keyboard; touchpad + mouse +
# stylus-like UNKNOWN subdevices for the pointing assembly). We match
# ALL of them by USB product prefix so a single posture transition
# fully suppresses every event stream that hardware produces.
KEYBOARD_RE   = re.compile(r"^Microsoft Surface 045E:09B0(?:$| )")
POINTING_RE   = re.compile(r"^Microsoft Surface 045E:09AF(?:$| )")
# The tablet-mode switch input device. libinput auto-suspends the
# touchpad/keyboard whenever this fires SW_TABLET_MODE=1, which
# defeats our per-device policy. We inhibit this device unconditionally
# while the service is running so libinput never sees the switch,
# leaving our explicit inhibit writes as the only authority. The
# kernel still updates STATE_PATH regardless of input inhibit.
SWITCH_RE     = re.compile(r"^Microsoft Surface POS Tablet Mode Switch$")

LAPTOP = "laptop"
TABLET_STRINGS = {"tablet", "book"}


def log(msg: str) -> None:
    print(f"surface-posture-input: {msg}", file=sys.stderr, flush=True)


def read_state() -> str:
    try:
        return STATE_PATH.read_text().strip()
    except FileNotFoundError:
        return "missing"
    except OSError as e:
        log(f"sysfs state read failed: {e}")
        return "missing"


def find_inhibit_paths() -> dict[str, list[Path]]:
    """Map role -> list of /sys/class/input/inputN/inhibited paths."""
    roles: dict[str, list[Path]] = {
        "keyboard": [],
        "pointing": [],
        "switch": [],
    }
    for entry in Path("/sys/class/input").iterdir():
        if not entry.name.startswith("input"):
            continue
        name_file = entry / "name"
        inhibit_file = entry / "inhibited"
        if not name_file.exists() or not inhibit_file.exists():
            continue
        try:
            name = name_file.read_text().strip()
        except OSError:
            continue
        if KEYBOARD_RE.match(name):
            roles["keyboard"].append(inhibit_file)
        elif POINTING_RE.match(name):
            roles["pointing"].append(inhibit_file)
        elif SWITCH_RE.match(name):
            roles["switch"].append(inhibit_file)
    return roles


def write_inhibit(path: Path, inhibited: bool) -> None:
    value = "1" if inhibited else "0"
    try:
        path.write_text(value)
    except OSError as e:
        log(f"write {value} -> {path} failed: {e}")


def policy_for(state: str) -> tuple[bool, bool, str]:
    """Returns (kb_inhibit, pointing_inhibit, label).

    True means INHIBITED (disabled). Touchpad and pointing-stick are
    grouped under "pointing" because the user's spec treats them
    identically: both ON in laptop+slate, both OFF in tablet.
    """
    if state == LAPTOP:
        return (False, False, "laptop")
    if state in TABLET_STRINGS:
        return (True, True, "tablet")
    if state == "missing":
        return (False, False, "missing")
    # Any other (slate / unknown stage variant): keyboard off, pointing on.
    return (True, False, f"stage({state})")


def apply(state: str, last_applied: str | None) -> str | None:
    if state == last_applied:
        return last_applied
    kb_inh, pt_inh, label = policy_for(state)
    roles = find_inhibit_paths()
    if not (roles["keyboard"] or roles["pointing"]):
        log(f"no matching Surface input devices found in /sys/class/input "
            f"(state={state!r}); will retry on next event")
        return last_applied
    # Always keep the tablet-mode switch device inhibited so libinput
    # cannot auto-suspend the touchpad/keyboard out from under us.
    for p in roles["switch"]:
        write_inhibit(p, True)
    for p in roles["keyboard"]:
        write_inhibit(p, kb_inh)
    for p in roles["pointing"]:
        write_inhibit(p, pt_inh)
    log(
        f"posture={state!r} ({label}) -> "
        f"keyboard_inhibit={kb_inh} pointing_inhibit={pt_inh} "
        f"switch_inhibit=True "
        f"(devices: kb={[str(p) for p in roles['keyboard']]}, "
        f"pt={[str(p) for p in roles['pointing']]}, "
        f"sw={[str(p) for p in roles['switch']]})"
    )
    return state


def restore_all_enabled() -> None:
    """Fail-safe used on SIGTERM/SIGINT and in ExecStopPost."""
    roles = find_inhibit_paths()
    for paths in roles.values():
        for p in paths:
            write_inhibit(p, False)
    log("restored all Surface devices to inhibited=0 (enabled)")


def main() -> int:
    if os.geteuid() != 0:
        log("must run as root (writes to /sys/class/input/.../inhibited)")
        return 1

    def _bye(signum, _frame):
        log(f"caught signal {signum}; restoring devices and exiting")
        restore_all_enabled()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    sw_fd: int | None = None
    try:
        sw_fd = os.open(str(SWITCH_EVENT_DEVICE), os.O_RDONLY | os.O_NONBLOCK)
        log(f"watching {SWITCH_EVENT_DEVICE} + polling {STATE_PATH} "
            f"every {POLL_TIMEOUT_S}s")
    except OSError as e:
        log(f"could not open {SWITCH_EVENT_DEVICE}: {e}; polling only")

    poller = select.poll()
    if sw_fd is not None:
        poller.register(sw_fd, select.POLLIN)

    last_applied: str | None = None
    last_applied = apply(read_state(), last_applied)

    while True:
        try:
            events = poller.poll(int(POLL_TIMEOUT_S * 1000))
        except InterruptedError:
            continue
        if events and sw_fd is not None:
            try:
                while True:
                    chunk = os.read(sw_fd, 4096)
                    if not chunk:
                        break
            except BlockingIOError:
                pass
            except OSError as e:
                log(f"read({SWITCH_EVENT_DEVICE}) failed: {e}; reopening")
                try:
                    poller.unregister(sw_fd)
                except (KeyError, ValueError):
                    pass
                try:
                    os.close(sw_fd)
                except OSError:
                    pass
                sw_fd = None
                time.sleep(1.0)
                try:
                    sw_fd = os.open(
                        str(SWITCH_EVENT_DEVICE),
                        os.O_RDONLY | os.O_NONBLOCK,
                    )
                    poller.register(sw_fd, select.POLLIN)
                except OSError as e2:
                    log(f"reopen failed: {e2}; continuing poll-only")
        last_applied = apply(read_state(), last_applied)


if __name__ == "__main__":
    sys.exit(main())
