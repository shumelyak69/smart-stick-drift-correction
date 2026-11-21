# Smart Stick Drift Correction

![Version](https://img.shields.io/badge/version-1.0.0-22d3ee)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Web-10b981)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

**Smart Stick Drift Correction** is a diagnostic utility and adaptive DSP pipeline for mitigating analog stick drift on gamepads. Instead of applying a static radial deadzone that degrades fine aiming control, it separates center bias, high-frequency potentiometer noise, mechanical spring hysteresis, and transient wiper contact dropouts while preserving full `-1.0` to `+1.0` stick travel.

---

## Overview

Traditional deadzone implementations zero out all input within a fixed radius around the origin. As a stick degrades, users are forced to increase this radius, creating a wide unresponsive zone and step discontinuities during micro-adjustments.

This project replaces static deadzones with a fixed-rate 125 Hz multi-stage signal processing pipeline:

1. **Center Bias Compensation & Asymmetric Rescaling**
   Measures and subtracts static resting offset, then rescales positive and negative axis ranges independently so the stick still reaches full `100%` deflection in every direction without clipping the circularity envelope.
2. **Wiper Dropout Bridge**
   Detects implausible single-tick velocity jumps caused by transient open-circuit contact dropouts on worn carbon tracks (1 to 3 frames) and holds trajectory continuity instead of snapping toward center mid-sweep.
3. **Median Pre-Filter (3-point / 5-point)**
   Eliminates impulse spikes across the entire deflection range (`0.0` to `1.0`) prior to derivative estimation.
4. **Derivative-Bounded One-Euro Filter**
   Adapts low-pass cutoff frequency dynamically based on movement speed: heavy smoothing at rest or steady hold, minimal phase lag during intentional flicks.
5. **Dynamic Deadzone & Sustained-Rest Zero Lock**
   Sizes the deadzone threshold from the calibrated RMS noise floor and spring return hysteresis, remapping output smoothly from the deadzone boundary and locking to `0.00, 0.00` once the stick remains at rest.

---

## Architecture

```mermaid
flowchart LR
    A["Physical Gamepad<br/>(/dev/input/eventX)"] -->|EVIOCGRAB + EVIOCGABS 125 Hz| B["Bias Compensation<br/>+ Range Rescaling"]
    B --> C["Dropout Bridge<br/>+ 5-pt Median"]
    C --> D["Adaptive One-Euro<br/>Low-Pass Filter"]
    D --> E["Dynamic Deadzone<br/>+ Radial Remapping"]
    E -->|uinput (fuzz=0, flat=0)| F["SmartDrift Virtual Gamepad<br/>(Games / Steam / Wine)"]
```

---

## Comparison: Static Deadzone vs. Adaptive DSP

| Metric | Static Deadzone | Smart Stick Drift Correction |
| :--- | :--- | :--- |
| **Center Offset (Bias)** | Requires enlarging deadzone to cover the offset | Vector-subtracted with full `-1.0 .. +1.0` range restoration |
| **Micro-Aiming (FPS)** | Unresponsive core and abrupt step threshold | Smooth radial remapping starting right above the noise floor |
| **Mid-Sweep Wiper Dropouts** | Unfiltered outside the center radius; causes severe jerks | Suppressed by Dropout Bridge, median filter, and bounded One-Euro |
| **Return to Zero (`0.00, 0.00`)** | Varies with mechanical spring wear | Sustained-rest detector settles output cleanly to `0.00, 0.00` |

---

## Installation

### Dependencies (Linux Virtual Controller)

**Arch Linux / Manjaro / CachyOS:**
```bash
sudo pacman -S python-evdev
```

**Debian / Ubuntu / Fedora (or via pip):**
```bash
pip install -r requirements.txt
```

The web diagnostics interface uses only the Python 3 standard library and requires no third-party packages.

---

## Usage

### 1. Linux Virtual Gamepad Bridge (`evdev` to `uinput`)

List available input devices:
```bash
sudo python3 linux_virtual_pad.py --list
```

Start the virtual controller with automatic startup calibration (leave both sticks untouched for 1.5 seconds):
```bash
sudo python3 linux_virtual_pad.py --device /dev/input/event21 --profile action --calibrate
```

Key behaviors:
- Acquires an exclusive grab (`EVIOCGRAB`) on the physical device so games do not receive duplicate input from both physical and virtual controllers.
- Creates a virtual `uinput` controller (`SmartDrift Virtual (...)`) with kernel `fuzz=0` and `flat=0` on stick axes.
- Polls authoritative hardware axis positions via `EVIOCGABS` on a strict 125 Hz (`8.0 ms`) clock.
- Automatically validates saved `config.json` centers against live resting hardware positions on startup.

Available profiles (`--profile`):
- `fps` — lowest latency (`~2 ms`) and highest sensitivity for fine aiming adjustments.
- `action` — balanced smoothing and wiper dropout protection (recommended default).
- `racing` — higher smoothing for steady steering inputs.
- `heavy` — maximum stabilization for heavily worn potentiometers with frequent contact dropouts.

### 2. Web Diagnostics & Drift Simulator

Start the local diagnostic server:
```bash
python3 server.py
```

Open `http://127.0.0.1:8765` in a browser.
- Supports live controllers via the **Web Gamepad API** as well as a built-in **Drift Simulator** (configurable center bias, potentiometer jitter, impulse spikes, slow thermal drift, and micro-aiming patterns).
- Provides real-time XY vector scopes, an oscilloscope view (`Raw` vs. `Corrected`), manual filter tuning (`Custom` profile), configuration export to `config.json`, and an automated quality benchmark suite.

### 3. CLI Quality Benchmark

Run the synthetic verification benchmark across all profiles:
```bash
python3 dsp_engine.py
```

Evaluated metrics:
- `residual_drift_pct` — remaining drift magnitude at rest after calibration (`0.0%`).
- `avg_latency_ms` — per-frame DSP execution time (`~0.011 ms`).
- `micro_accuracy_pct` — tracking accuracy on fine low-amplitude ramps.
- `spikes_suppressed` and `false_positives`.

---

## Project Structure

- `dsp_engine.py` — Core signal processing pipeline (`OneEuroFilter1D`, `AdaptiveStickCorrector`, calibration validation, quality benchmark).
- `linux_virtual_pad.py` — Linux `evdev` to `uinput` virtual controller daemon (125 Hz fixed-step loop, `EVIOCGRAB`, automatic stale process cleanup).
- `server.py` — Local HTTP server for the web diagnostics interface and JSON configuration API.
- `web/` — Dark-theme hardware diagnostics frontend (`index.html`, `style.css`, `app.js`, `dsp.js`).
- `config.json` — Persisted calibration data and active profile parameters.

---

## License

Released under the [MIT License](LICENSE).
