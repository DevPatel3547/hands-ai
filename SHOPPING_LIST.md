# Hands starter kit: shopping list

All prices are **approximate** (USD, typical US online prices in late 2026) and change often.
No specific stores are listed: paste the search term into whatever retailer you use, and check
the listing photos and specs against the column "Make sure".

| # | Part | Search term | Make sure | Qty | Approx. price |
|---|------|-------------|-----------|-----|---------------|
| 1 | Raspberry Pi Pico WH | `Raspberry Pi Pico WH` | The **H** means headers are already soldered on. Pico **W** (wireless) version. | 1 | ~$7 |
| 2 | Micro-USB cable | `micro USB data cable` | Must be a **data** cable, not charge-only. You may already own one. | 1 | ~$4 (or $0) |
| 3 | Breadboard + jumper wires | `830 tie point breadboard jumper wire kit` | Full-size 830-point (63 columns, two rails per side). Male-to-male jumpers, 40+ of them. | 1 kit | ~$8–10 |
| 4 | LEDs | `5mm LED assortment` | Red, yellow and green included. | 1 pack | ~$5–7 |
| 5 | Resistors | `1/4W resistor assortment kit` | Includes **220 Ω, 1 kΩ, 10 kΩ**. | 1 kit | ~$6–8 |
| 6 | Active buzzer | `active buzzer 5V 12mm` | Says **active** (beeps on plain DC). Two legs, rated 3–5 V. | 1 pack | ~$5–7 |
| 7 | Push buttons | `6x6mm tactile push button` | 4-leg through-hole type. | 1 pack | ~$4–6 |
| 8 | NPN transistors | `PN2222A transistor TO-92` | **PN2222A** (pinout E-B-C with the flat side facing you). Drives the buzzer, which needs more current than a Pico pin can safely give. | 1 pack | ~$5 |
| 9 | Temperature/humidity sensor | `DHT22 AM2302 module` | The **module** version: small board, 3 pins, built-in pull-up resistor. | 1 | ~$7–10 |
| 10 | OLED screen | `SSD1306 0.96 inch OLED I2C 128x64` | **I2C** (4 pins: GND VCC SCL SDA), header already soldered. Check the pin order printed on it. | 1 | ~$6–8 |
| 11 | Micro servo | `SG90 micro servo` | 5 V, 3-wire (brown/red/orange). | 1 | ~$4–6 |
| 12 | Current sensor | `INA219 current sensor module` | Address 0x40 and a 0.1 Ω shunt (marked R100). **Headers usually come unsoldered.** Look for "pre-soldered", or see the note below. | 1 | ~$5–8 |
| 13 | Logic analyzer | `24MHz 8 channel USB logic analyzer` | Works with sigrok/PulseView (driver `fx2lafw`). Comes with grabber clips. **Optional:** only needed for the signal-walk debugger and the `hands bringup` self-test. | 1 | ~$10–13 |
| 14 | Camera | `USB webcam 1080p` | Any UVC webcam. **Optional:** use your phone or laptop camera to save money. | 1 | ~$15–25 (or $0) |

**Total:** about $85–110 with a webcam, or about $70–85 if you use a phone camera. Skipping the
logic analyzer saves another ~$10–13. No API key or paid model is needed: the built-in gadgets
run with the free scripted model, and free local models work through Ollama.

## Notes before you buy

- **Soldering.** The only part that usually needs it is the INA219 header (6 pins). Options:
  (a) buy a pre-soldered listing; (b) use a library or makerspace soldering station, which takes
  10 minutes with help; (c) a basic soldering kit costs about $15–20 extra. Until it's soldered you
  can run everything except current sensing: the agent then has to rely on photos and the logic
  analyzer.
- **Pin orders differ between clones** (INA219 and OLED in particular). Before wiring, compare the
  labels printed on your board with `pins:` in `hands/data/starter_kit.yaml` and edit the YAML if
  they differ. The wiring guide is generated from that order.
- **Bundle option.** Many "Raspberry Pi Pico starter kits" (about $25–40) include items 3–8. Check
  that one includes an *active* buzzer and the three resistor values.
- **Nice to have later:** a USB hub with per-port power switching that works with `uhubctl`. With
  one, `power_cut()` can switch the power off itself instead of asking you to unplug.
- **Before you wire anything, run `hands bringup`.** It checks the Pico, the INA219 and the
  logic analyzer one at a time, so a faulty part shows up before you build a gadget.
- **Pico 2 W (RP2350)** has the same pinout and should work, but it is untested. Hands applies
  the stricter Pico W (RP2040) limits either way. A plain **Pico** (no W) also works, except
  for the on-board LED blink check.
- **Not needed and not allowed:** no mains power, no batteries of any kind, no LiPo cells, no
  motors or relays. Everything in this project runs from the laptop's USB port (5 V). Hands
  refuses designs that need anything else.
