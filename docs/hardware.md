# Hardware

The reference build separates the display from the controller. A Raspberry Pi
Zero 2 W lives behind the frame and shows approved plates. An existing Mac,
Linux computer, Raspberry Pi 4 or 5, or Docker host can run discovery, Codex
generation and review, catalog serving, and notifications.

The two roles may run on one capable Raspberry Pi, but the split build keeps the
computer behind the artwork small, quiet, and easy to replace.

## Choose a display

| Display | Active area | Board size | Canvas | Best fit |
| --- | --- | --- | --- | --- |
| PIM774, 13.3 inch | Approximately 7.98 x 10.65 inches | Verify the current board against the frame | `1600x1200` | Reference portrait frame |
| PIM773, 7.3 inch | Approximately 6.30 x 3.78 inches | Approximately 6.86 x 4.85 inches | `800x480` | Smaller build |

PIM774 consumes the canonical `1600x1200` display asset unchanged. The display
node fits the complete asset onto PIM773 without cropping or stretching and
leaves narrow paper-colored margins. The application reads panel geometry from
Pimoroni’s EEPROM; no display model or resolution setting belongs in
`config.toml`.

Pimoroni lists both panels as compatible with every 40-pin Raspberry Pi,
including Zero variants. A Pi without a header requires soldering. The
recommended Pi Zero 2 W option below has a pre-soldered header.

## Recommended framed display

This bill of materials covers the part that hangs on the wall.

| Part | Qty | Unit price | Extended | Purpose |
| --- | ---: | ---: | ---: | --- |
| [Pimoroni Inky Impression 13.3 inch (PIM774)](https://www.adafruit.com/product/6472) | 1 | $275.00 | $275.00 | Six-color, 1600x1200 e-paper display; mounting hardware and GPIO extension header are included |
| [Raspberry Pi Zero 2 W with pre-soldered header](https://www.pishop.us/product/raspberry-pi-zero-2w-with-headers/) | 1 | $20.75 | $20.75 | Compact Wi-Fi display node; no soldering required |
| [5V 2.5A Micro-USB power supply](https://www.adafruit.com/product/1995) | 1 | $8.25 | $8.25 | Powers the display node with a standard straight cable |
| [Official Raspberry Pi 64GB A2 microSD card](https://www.pishop.us/product/raspberry-pi-sd-card-64gb/) | 1 | $29.95 | $29.95 | Operating system and local image cache |
| [Golden State Art 12 x 16 inch bronze frame](https://www.amazon.com/gp/aw/d/B0C1Q5MYG9) | 1 | $24.99 | $24.99 | Portrait frame; the included 8 x 10.5 inch mat must be enlarged or replaced |
| **Framed display subtotal** |  |  | **$358.94** | Before tax and shipping |

Reference prices were checked on July 9, 2026. Retail prices and availability
change. Totals exclude tax and shipping. A computer with a microSD reader is
needed to flash the display card.

## Fit the mat and backing

The included 8 x 10.5 inch mat masks part of the PIM774 active area and must not
be used unchanged. Enlarge it or order a custom mat with an opening of at least
8.1 x 10.75 inches. Verify the opening against the physical panel before
cutting; published dimensions are not a substitute for a test fit.

Test-fit the display and Pi, trace their position on the supplied rear backing
board, and cut an opening that leaves the Pi, microSD card, and power connector
accessible. The Pi connects directly to the display and does not need a
separate case. A right-angle power cable is not required.

Avoid pressure on the e-paper panel. Keep the display cable relaxed, leave
connectors and ventilation unobstructed, and test-fit every layer before cutting
the backing. Follow Pimoroni’s handling and mounting guidance for the panel.

## Reuse hardware you already own

The reference installation below used an Inky Impression display, a Compute
Module 4, and a Waveshare carrier board that were already on hand. The CM4 is
larger and more powerful than the display role requires, but reusing it made the
build practical without buying another computer. This is one working layout,
not required hardware.

<table>
<tr>
<td width="50%" align="center">
<img src="images/reference-build-open-back.jpg" alt="Open back of the framed display during assembly, with the panel and CM4 carrier visible" width="100%">
<br><strong>Before the backing board.</strong> Heavy-duty duct tape holds the panel securely while the display node remains accessible.
</td>
<td width="50%" align="center">
<img src="images/reference-build-backing-cutout.jpg" alt="Rear backing board cut around the CM4 carrier in the assembled frame" width="100%">
<br><strong>With the backing fitted.</strong> The supplied board was cut around the carrier so power, storage, and service access remain available.
</td>
</tr>
</table>

Any supported 40-pin Raspberry Pi can perform the display role. For a new
build, the smaller Pi Zero 2 W remains the recommended choice.

## Optional dedicated controller

An existing 64-bit macOS or Linux computer can run the controller at no added
hardware cost. For a self-contained installation, the reference controller is a
Raspberry Pi 4 running 64-bit Ubuntu Server.

| Part | Qty | Unit price | Extended | Purpose |
| --- | ---: | ---: | ---: | --- |
| [Raspberry Pi 4 Model B, 4GB](https://www.adafruit.com/product/4296) | 1 | $120.00 | $120.00 | Runs discovery, Codex, review, catalog, and HTTP services |
| [Official Raspberry Pi 5.1V 3A USB-C power supply](https://www.adafruit.com/product/4298) | 1 | $8.74 | $8.74 | Controller power |
| [Flirc passive aluminum Raspberry Pi 4 case](https://www.adafruit.com/product/4553) | 1 | $14.95 | $14.95 | Silent enclosure and passive cooling |
| [Official Raspberry Pi 64GB A2 microSD card](https://www.pishop.us/product/raspberry-pi-sd-card-64gb/) | 1 | $29.95 | $29.95 | 64-bit OS, application, references, and generated assets |
| **Dedicated controller subtotal** |  |  | **$173.64** | Before tax and shipping |
| **Complete dedicated build** |  |  | **$532.58** | Framed display plus dedicated controller |

No HDMI cable, keyboard, mouse, right-angle cable, or display-node enclosure is
required for normal operation.

## Requirements beyond the hardware

The controller requires:

- Python 3.11 or newer for a native installation, or a supported AMD64 or ARM64
  Docker host;
- a ChatGPT plan that includes Codex, or separately billed OpenAI API access;
- network access to configured observation, geocoder, reference, research, and
  Codex services; and
- durable storage for private state, generated work, and approved plates.

The display node requires:

- Raspberry Pi OS Bookworm or later on a 40-pin Raspberry Pi;
- Pimoroni’s Inky package;
- private-network access to the controller; and
- SSH access from the setup computer for installation and troubleshooting.

The supported topology is one active display node per controller. Multiple
panels can read the same catalog, but health state describes the display role as
a whole rather than each panel independently.

## Continue to installation

Use the [native installation guide](installation.md) for the display and for a
native controller. If the controller will run on Docker or a NAS, start with the
[Docker controller guide](docker.md); it returns to the same display procedure
after the controller is healthy.
