# Development history & design context

This file captures the iteration that produced the current implementation,
including the dead-ends, so future-you (or another contributor) understands
*why* the code looks the way it does instead of trying obvious "simpler"
approaches that don't work.

## The hardware story

The Surface Laptop Studio has a hinge that supports three discrete
postures the user cares about:

1. **Laptop** — screen upright behind the keyboard, like a regular laptop.
2. **Slate / Stage** — screen pulled forward and resting in front of the
   keyboard, with the touchpad still exposed below.
3. **Tablet** — screen folded all the way down, flat against the keyboard
   deck, covering both keyboard and touchpad.

The `linux-surface` kernel exposes this via a Surface Aggregator Module
(SSAM) endpoint that ships with the kernel module
`surface_aggregator_tabletsw`. The driver creates:

- A read-only sysfs file
  `/sys/devices/platform/MSHW0123:00/01:26:01:00:01/state`
  whose contents are one of the literal strings `laptop`, `slate`, or
  `tablet`. **This is a 3-state attribute, not a binary one.**
- An input device named *Microsoft Surface POS Tablet Mode Switch* that
  fires the standard `EV_SW`/`SW_TABLET_MODE` event on binary transitions
  between laptop and not-laptop. **This is a 2-state event source.**

The 2-state-ness of the input event matters: any solution that relies on
`SW_TABLET_MODE` alone (which is what every off-the-shelf compositor /
libinput-based tool does) cannot distinguish slate from tablet. We need the
3-state sysfs attribute as the source of truth.

The IIO inclinometer (`/sys/bus/iio/devices/iio:device0/`) is also
available as a fallback signal but is not currently used — the SSAM
posture attribute is more reliable and doesn't require any angle-threshold
heuristics.

## The four Surface input device groups

`/proc/bus/input/devices` shows that each of the user-relevant Surface
peripherals exposes **multiple input subdevices**:

| Group           | USB ID       | Subdevices visible to userspace                    |
|-----------------|--------------|-----------------------------------------------------|
| Keyboard        | `045E:09B0`  | `…09B0 Keyboard`, `…09B0`, `…09B0` (3 total: keys + 2 function/consumer) |
| Touchpad+stick  | `045E:09AF`  | `…09AF Touchpad`, `…09AF Mouse`, `…09AF UNKNOWN` ×2 (4 total) |
| Tablet switch   | (SSAM)       | `Microsoft Surface POS Tablet Mode Switch` (1)     |
| Touchscreen+pen | `045E:0C1B`  | Several (untouched by this service)                 |

The original v1/v2 implementations matched only the `Keyboard`, `Touchpad`,
and `Mouse` strings, which is why the keyboard kept working in slate
posture even when the daemon said it was inhibited — the function-key
subdevices weren't being inhibited. The current version matches by USB
product prefix and inhibits **every** subdevice that belongs to a target
group.

## The libinput auto-suspend trap

libinput watches the kernel tablet-mode switch and **automatically
suspends the touchpad and internal keyboard** whenever it sees
`SW_TABLET_MODE=1`. This is the right default behaviour for normal
2-in-1 convertibles (laptop ↔ tablet) but it directly conflicts with
the 3-state slate-keeps-the-touchpad policy we want.

Symptom: even with `inhibited=0` on every touchpad subdevice in slate
posture, the touchpad stayed dead — because libinput had already
suspended it at a higher layer. The kernel inhibit flag controls
whether *events flow*; libinput's suspend controls whether the
already-flowing events are *consumed*. They're independent gates.

Fix: keep the *tablet-mode switch input device itself* (`input18`,
"Microsoft Surface POS Tablet Mode Switch") permanently `inhibited=1`
while our service runs. The kernel still updates the `state` sysfs
attribute on hinge transitions (we re-read it every 500 ms), so we
don't lose posture detection, but libinput stops seeing the switch
device entirely and gives up on its auto-suspend logic.

## What did *not* work (and why)

### v1: hyprctl-based device toggle
- Mechanism: `hyprctl keyword device[<name>]:enabled true|false`
- Failure mode: disable worked, but the re-enable path failed silently
  in this Hyprland build — `hyprctl` returned `ok` but the device stayed
  detached internally. The touchpad ended up dead even in laptop posture
  after the first slate→laptop transition. Recovery: `hyprctl reload`.
- Verdict: rejected; abandoned in favor of kernel-level inhibit.

### v2: kernel inhibit, narrow regex
- Mechanism: write 0/1 to `/sys/class/input/inputN/inhibited` for the
  three specific device names.
- Failure mode 1: keyboard still responded in slate because the
  function-key subdevices (`045E:09B0` without the " Keyboard" suffix)
  were unmatched and kept firing key events.
- Failure mode 2: touchpad died in slate anyway because libinput
  auto-suspended it when it saw `SW_TABLET_MODE=1` on the switch device.
- Verdict: rejected; replaced with v3.

### v3 (current): kernel inhibit, broad regex, switch device inhibited
- Match every Surface keyboard/pointing subdevice by USB-product prefix.
- Always inhibit the tablet-mode switch device while the service runs
  to block libinput's auto-suspend.
- Verified working end-to-end through `laptop → slate → tablet → slate
  → laptop` sequences with no stuck states.

## Why a system service (not a user service)?

Writing to `/sys/class/input/<input>/inhibited` requires root. The v1 user
service didn't have that problem because hyprctl is a user-space IPC, but
that mechanism turned out to be unreliable. With the v3 kernel-level
approach we accept the tradeoff that the daemon has to run as root.

Hardening in the unit:

- `NoNewPrivileges=yes`, `PrivateTmp=yes`, `ProtectHome=yes`,
  `LockPersonality=yes`, `RestrictRealtime=yes`,
  `MemoryDenyWriteExecute=yes`, `RestrictSUIDSGID=yes`, etc.
- `ProtectSystem=full` (not `strict`, because we need
  `/sys/class/input/*/inhibited` writable).
- `ProtectKernelModules=yes` — we don't need to load modules.
- `ProtectKernelTunables=no` — we need to write sysfs, which the kernel
  tunables protection blocks.

## Fail-safe semantics

- **Daemon SIGTERM/SIGINT handler** restores every matched device to
  `inhibited=0` before exiting.
- **Unit `ExecStopPost`** runs the same cleanup *regardless* of how the
  daemon exited (including segfaults, OOM kill, manual `systemctl stop`,
  power loss while the unit is stopping, etc.). It's a plain `sh` loop
  over `/sys/class/input/input*/name` that doesn't depend on the daemon
  binary even being present.
- Net effect: stopping the service is always safe — every input device
  returns to its kernel default of `inhibited=0`, and Hyprland's normal
  device handling takes over from there.

## Why the daemon polls every 500 ms in addition to watching the input device

The input event for the switch is binary (laptop ↔ not-laptop). When you
fold from slate to tablet (or back) the kernel *may* not emit any input
event because `SW_TABLET_MODE` doesn't change — both postures are
"non-laptop". The 500 ms poll guarantees we catch slate↔tablet
transitions within at most half a second. The input-event handler is
still primary for laptop↔anything transitions and gives sub-millisecond
reaction; the poll is just a backstop.

(Once the daemon starts up it can't open `/dev/input/event12` if it's
running as a regular user — it's `root:input 0660`. The system service
runs as root so this isn't an issue. The daemon does still gracefully
fall back to poll-only if the event device can't be opened, which is
useful for testing in a debugger.)

## Possible future work

- **Display rotation.** The IIO accelerometer/gravity sensors are
  already exposed and could drive `hyprctl keyword monitor` rotation
  through `iio-sensor-proxy`. Out of scope for this service.
- **Auto-launch on-screen keyboard in tablet posture.** Easy add-on
  (`exec hyprctl dispatch exec wvkbd-mobintl` etc.); left out so this
  service has exactly one responsibility.
- **Per-user policy file.** If anyone wants different per-posture
  behaviour, a small TOML in `/etc/surface-posture-input.conf` could
  replace the hardcoded `policy_for()` function. Currently YAGNI.

## Provenance

This was iterated on a real machine over a single session in May 2026
between Shawn (flynnsbit) and GitHub Copilot CLI. The full session
transcript including every failed approach and the diagnostic commands
that revealed each root cause is preserved in the omarchy session store
under repo identifier `omarchy-surface-studio-mouse`. The high-level
plan that drove the implementation is at
`~/.copilot/session-state/<session>/plan.md`.
