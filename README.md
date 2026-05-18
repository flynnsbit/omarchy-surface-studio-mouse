# omarchy-surface-studio-mouse

Posture-aware input toggling for the **Microsoft Surface Laptop Studio** on
[linux-surface](https://github.com/linux-surface/linux-surface) under
[omarchy](https://omarchy.org/) / Hyprland.

When you fold the display forward over the keyboard (the "stage" / *slate*
posture on the kernel) the keyboard is disabled but the touchpad keeps
working. When you fold the display all the way down flat against the
keyboard deck (the *tablet* posture) the keyboard, touchpad and pointing
stick are all disabled and only the touchscreen and stylus respond. Snap
the display back upright and everything is restored.

```
┌─────────────────────────────┬──────────┬──────────┬─────────────────────┐
│ Posture                     │ Keyboard │ Touchpad │ Touchscreen / Stylus│
├─────────────────────────────┼──────────┼──────────┼─────────────────────┤
│ laptop  (screen upright)    │    ✅    │    ✅    │         ✅          │
│ slate   (screen forward)    │    ❌    │    ✅    │         ✅          │
│ tablet  (screen flat)       │    ❌    │    ❌    │         ✅          │
└─────────────────────────────┴──────────┴──────────┴─────────────────────┘
```

## Why this exists

Out of the box, libinput auto-disables the touchpad and keyboard the moment
it sees `SW_TABLET_MODE=1` from the kernel — which the linux-surface tablet
mode switch fires as soon as you leave the upright posture. That means the
touchpad **stops working in the slate/stage posture** even though it's still
physically exposed in front of the screen, which is the entire point of
that posture.

This service replaces that all-or-nothing behaviour with a 3-state policy
driven directly off the Surface Aggregator Module's posture sysfs file.

## How it works

- **Source of truth (posture):**
  `/sys/devices/platform/MSHW0123:00/01:26:01:00:01/state` — exposed by the
  `surface_aggregator_tablet_mode_switch` driver. Reads one of `laptop`,
  `slate`, or `tablet` (depending on the physical hinge angle).
- **Trigger (event):** `/dev/input/event12` (the *Microsoft Surface POS
  Tablet Mode Switch*) wakes the daemon on every binary transition, plus
  a 500 ms safety re-poll for `slate`↔`tablet` transitions that the input
  device may not emit.
- **Action (toggle):** the daemon writes `0` or `1` to
  `/sys/class/input/<inputN>/inhibited` for the matching Surface devices.
  This is the standard kernel-level input inhibit primitive — it hides the
  device from *every* userspace consumer (Hyprland, libinput, TTY, X11)
  and is reversible at any time.
- **Bypassing libinput's auto-suspend:** the daemon also keeps the tablet
  mode switch device itself permanently inhibited while the service runs.
  This way libinput never sees `SW_TABLET_MODE` events and doesn't try to
  auto-suspend the touchpad behind our back. The driver still updates the
  `state` sysfs file regardless, so posture detection still works.
- **Fail-safe:** the systemd unit has an `ExecStopPost` that resets every
  matching Surface device to `inhibited=0` on service stop / crash. The
  daemon also handles SIGTERM/SIGINT by restoring inputs before exiting.
  The absolute worst case if something goes wrong is therefore a brief
  outage — never a stuck-disabled touchpad. If anything ever feels off,
  `sudo systemctl stop surface-posture-input` brings everything back.

## Installation

Clone the repo and run the installer (one prompt for sudo):

```bash
git clone https://github.com/flynnsbit/omarchy-surface-studio-mouse.git
cd omarchy-surface-studio-mouse
sudo bash install.sh
```

That copies the daemon to `/usr/local/bin/surface-posture-input`, installs
the system unit to `/etc/systemd/system/surface-posture-input.service`, and
enables + starts it. The service auto-starts at every boot.

## Verifying

Watch the log live and fold through the postures:

```bash
journalctl -u surface-posture-input.service -f
```

You should see lines like:

```
posture='laptop' (laptop)        -> keyboard_inhibit=False pointing_inhibit=False switch_inhibit=True
posture='slate'  (stage(slate))  -> keyboard_inhibit=True  pointing_inhibit=False switch_inhibit=True
posture='tablet' (tablet)        -> keyboard_inhibit=True  pointing_inhibit=True  switch_inhibit=True
```

## Tweaking

- **Different stage label** — if your kernel uses a different string for
  the middle posture (anything other than `laptop` or `tablet` is treated
  as stage), it still works thanks to the fail-open `policy_for()` branch.
- **Different fully-flat label** — edit `TABLET_STRINGS` at the top of
  `surface-posture-input.py` to add the string you see in the logs.
- **Different policy** — `policy_for()` is the entire decision function
  and is twelve lines of obvious Python. Pointing-stick and touchpad are
  collapsed into one `pointing` role because the spec treats them
  identically; if you want them split, restore the original `find_inhibit_paths`
  with separate `touchpad`/`pointstick` regexes.

## Uninstall

```bash
sudo systemctl disable --now surface-posture-input.service
sudo rm /usr/local/bin/surface-posture-input /etc/systemd/system/surface-posture-input.service
sudo systemctl daemon-reload
```

The `ExecStopPost` runs on the `disable --now`, so all Surface input
devices are returned to `inhibited=0` automatically.

## Files

| File | Purpose |
|------|---------|
| `surface-posture-input.py`      | the daemon (Python 3, stdlib only) |
| `surface-posture-input.service` | systemd system unit |
| `install.sh`                    | one-shot installer |
| `CONTEXT.md`                    | development history & design notes (read this if you want to understand *why* the code looks the way it does) |

## Tested hardware / software

- Microsoft Surface Laptop Studio
- omarchy + Hyprland (Wayland)
- Arch Linux + linux-surface kernel `6.19.8-arch1-3-surface`
- USB-product IDs: keyboard `045E:09B0`, touchpad/pointing-stick `045E:09AF`,
  tablet-mode switch (SSAM) `01:26:01:00:01`

If you have a different Surface generation, change the regexes at the top
of `surface-posture-input.py` and the path in `STATE_PATH` to match the
device names + SSAM endpoint that show up in `/proc/bus/input/devices` and
`/sys/bus/surface_aggregator/devices/` on your machine.

## License

MIT.
