# foco

Laser pointer and screen spotlight for **Linux on X11**, driven by a
**Logitech Spotlight 2** presenter — including the button's two pressure
levels, which no other Linux tool currently supports.

Hold the presenter button softly and a spotlight follows your pointer. Press
harder and you get a laser dot. Keyboard shortcuts do the same without the
remote.

![status](https://img.shields.io/badge/X11-supported-brightgreen)
![license](https://img.shields.io/badge/license-MIT-blue)

## Why this exists

[Projecteur](https://github.com/gbin/Projecteur) solves this problem well and
you should use it if you can. But its current development line is
**Wayland-only**: it requires KDE Plasma 6.7+, Qt 6.10+ and a Wayland session.
The Qt5/X11 code lives on a separate legacy branch that predates the
Spotlight 2 and does not know its device IDs.

So if you run **XFCE, MATE, Cinnamon or GNOME on X11** and own a Spotlight 2,
there is currently nothing. That is the gap this fills.

## Supported devices

| Device | ID | Tested |
|---|---|---|
| Logitech Spotlight 2 (Bluetooth) | `046d:b506` | **yes** |
| Logitech Spotlight 2 (Logi Bolt receiver) | `046d:c548` | not yet |
| Logitech Spotlight (Bluetooth) | `046d:b503` | not yet |
| Logitech Spotlight (Unifying receiver) | `046d:c53e` | not yet |

Reports from the untested ones are very welcome.

## Install

```bash
git clone https://github.com/fmesasc/foco
cd foco
sudo make install
sudo usermod -aG input $USER     # if you are not already in it
# log out and back in
```

Dependencies (Debian/Ubuntu):

```bash
sudo apt install python3-gi python3-evdev python3-pyudev \
                 gir1.2-gtk-3.0 python3-cairo
```

You also need a **running compositor** — the overlay is a translucent window.
On XFCE: *Settings → Window Manager Tweaks → Compositor*.

## Use

**With the presenter:**

| Action | Effect |
|---|---|
| Press the button **softly** and hold | Spotlight follows the pointer |
| Press **hard** | Laser dot |
| Release | Effect disappears |

Each pressure level always shows the same effect. This is deliberate: an
earlier version cycled through effects on a hard press, and it felt broken
because the current mode was invisible state that changed under your fingers.

**With the keyboard** (bind these to `foco --enviar ...`):

| Command | Effect |
|---|---|
| `foco --enviar alternar` | Toggle on/off (stays until toggled) |
| `foco --enviar siguiente` | Cycle spotlight → laser → both |
| `foco --enviar mas` / `menos` | Grow / shrink the spotlight |
| `foco --enviar apagar` | Turn off |

**Options:**

```
foco --modo-suave spotlight --modo-fuerte laser   # effect per pressure level
foco --radio 180              # spotlight radius (px)
foco --radio-laser 13         # laser dot radius (px)
foco --oscurecer 0.72         # dim opacity, 0..1
foco --ocultar-cursor laser   # laser | siempre | nunca
foco --listar                 # list input devices
foco -v                       # explain what it is doing
```

## What we had to reverse engineer

None of this is documented by Logitech. It was measured against the real
device, and it is the part that may be useful to other projects.

### The buttons are silent until you ask

Logitech's reprogrammable controls report **nothing** over HID++ by default.
You must explicitly ask for them with `0x1B04 setCidReporting`. Logi Options+
does this on Windows at startup; on Linux nobody did, which is exactly why the
button "does nothing".

### The two pressure levels are two different control IDs

| CID | Meaning |
|---|---|
| `0x00D8` | First level — soft press |
| `0x01A8` | Second level — hard press (the one that vibrates on Windows) |

Both are declared **force-sensitive** by the device itself. Releasing from
level 2 passes briefly through level 1, so `0x01A8 → 0x00D8 → none` is a
single release, not two presses.

### Gotchas that cost us hours

- **The Spotlight 2 does not accept short HID++ reports.** Its descriptor
  declares report IDs `0x01, 0x02, 0x03` and `0x11` only. Sending the short
  `0x10` report gives you **silence, not an error**, which is far harder to
  debug.
- **Every field has a "valid" bit.** Sending flags `0x03` sets divert but
  leaves `rawXY` untouched, because you did not validate that field. To turn
  `rawXY` off you must send `0x23`.
- **Do not divert `rawXY`.** If you do, gyro motion stops arriving on the
  mouse channel and the cursor freezes. Let the kernel keep moving the
  pointer; you only need the buttons.
- **The device silently drops commands sent back to back.** Ten
  `setCidReporting` in a row: some are applied, some are not, with no error.
  Space them out.

### Feature map of the Spotlight 2 (HID++ 4.5)

| Feature | Index | Notes |
|---|---|---|
| `0x1B04` ReprogrammableKeysV4 | 11 | The buttons |
| `0x1A01` PresenterHaptic | 17 | Vibration motor |
| `0x1004` UnifiedBattery | 9 | Battery level |
| `0x18A1` LEDControl | 27 | hidden/engineering |
| `0x1814` ChangeHost / `0x1815` HostsInfo | 12 / 13 | Multi-host pairing |

## Design notes

**Power.** Idle CPU is 0.0%: the process blocks in the GLib loop on the device
descriptors, and the kernel wakes it. Reconnection is detected through udev
rather than polling, so when the presenter sleeps the program sleeps too. The
overlay window is hidden, not destroyed, and repaints are coalesced to at most
60 Hz while visible.

**The gyroscope is not decoded.** The kernel already delivers it as ordinary
mouse motion, so the cursor moves by itself and we just draw where it already
is. Much less code and much less to go wrong.

**Cursor hiding uses XFixes through ctypes**, with no extra dependency.
Setting a blank cursor only affects your own windows, and ours is
click-through, so the pointer is never "over" it. The hide is re-asserted a
few times a second because crossing a window that defines its own cursor
brings it back.

## Status and contributing

Working and in daily use, but young. Untested on anything but XFCE/X11 with a
Bluetooth Spotlight 2.

The most useful contributions right now: reports from the other device IDs,
the correct `0x1A01` haptic command (the current one is a guess), and Wayland
support.

## Licence

MIT. See [LICENSE](LICENSE).

Thanks to [Projecteur](https://github.com/gbin/Projecteur) for the device IDs
and for solving this properly on Wayland.
