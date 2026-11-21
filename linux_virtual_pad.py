#!/usr/bin/env python3
"""
Smart Stick Drift Correction — Linux Virtual Gamepad Bridge (evdev -> uinput).

Features:
- Automatic cleanup of any previous linux_virtual_pad.py processes
- EVIOCGRAB double-input protection
- Strict 125 Hz (8.0 ms) fixed-step DSP clock (never runs faster on event bursts,
  so median & One-Euro windows always span true 8 ms intervals)
- Automatic startup center validation & self-healing against corrupted config.json
- Zeroes kernel fuzz/flat on the virtual UInput stick axes for exact 0.00 centering
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict

from dsp_engine import AdaptiveStickCorrector, PROFILES, clamp

try:
    import evdev
    from evdev import AbsInfo, InputDevice, UInput, ecodes
except ImportError:
    evdev = None  # type: ignore


LEFT_AXES = (0, 1)   # ABS_X, ABS_Y
RIGHT_AXES = (3, 4)  # ABS_RX, ABS_RY
STICK_AXES = (*LEFT_AXES, *RIGHT_AXES)
DSP_DT = 0.008       # Strict 125 Hz fixed-step DSP interval


def kill_stale_instances() -> None:
    """Terminate any older linux_virtual_pad.py processes so they don't hold the device."""
    my_pid = os.getpid()
    parent_pid = os.getppid()
    try:
        out = subprocess.check_output(["pgrep", "-f", "linux_virtual_pad.py"], text=True)
        for line in out.splitlines():
            pid = int(line.strip())
            if pid not in (my_pid, parent_pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
    except Exception:
        pass


def list_devices() -> None:
    if evdev is None:
        print("Модуль 'evdev' не установлен. Установите его: sudo pacman -S python-evdev")
        sys.exit(1)

    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    if not devices:
        print("Устройства ввода не найдены.")
        return

    print("Доступные устройства ввода:")
    for dev in devices:
        caps = dev.capabilities()
        has_abs = ecodes.EV_ABS in caps
        marker = "[GAMEPAD/ABS]" if has_abs else "             "
        print(f"  {dev.path:20s} {marker}  {dev.name}")


def normalize_axis(value: int, info: "AbsInfo") -> float:
    span = info.max - info.min
    if span == 0:
        return 0.0
    center = (info.max + info.min) / 2.0
    half = span / 2.0
    return clamp((value - center) / half, -1.0, 1.0)


def denormalize_axis(norm: float, info: "AbsInfo") -> int:
    center = int(round((info.max + info.min) / 2.0))
    if abs(norm) < 1e-4:
        return center
    span = info.max - info.min
    return int(round(((clamp(norm, -1.0, 1.0) + 1.0) * 0.5) * span + info.min))


def calibrate_evdev(
    dev: "InputDevice",
    abs_caps: Dict[int, "AbsInfo"],
    left_corr: AdaptiveStickCorrector,
    right_corr: AdaptiveStickCorrector,
    cfg_file: Path,
    profile_name: str,
    duration_sec: float = 1.5,
) -> None:
    print(f"Калибровка через evdev ({duration_sec:.1f} сек)... Не трогайте стики!")
    samples_l = []
    samples_r = []
    t_end = time.perf_counter() + duration_sec
    while time.perf_counter() < t_end:
        lx = normalize_axis(dev.absinfo(LEFT_AXES[0]).value, abs_caps[LEFT_AXES[0]]) if LEFT_AXES[0] in abs_caps else 0.0
        ly = normalize_axis(dev.absinfo(LEFT_AXES[1]).value, abs_caps[LEFT_AXES[1]]) if LEFT_AXES[1] in abs_caps else 0.0
        rx = normalize_axis(dev.absinfo(RIGHT_AXES[0]).value, abs_caps[RIGHT_AXES[0]]) if RIGHT_AXES[0] in abs_caps else 0.0
        ry = normalize_axis(dev.absinfo(RIGHT_AXES[1]).value, abs_caps[RIGHT_AXES[1]]) if RIGHT_AXES[1] in abs_caps else 0.0
        samples_l.append((lx, ly))
        samples_r.append((rx, ry))
        time.sleep(0.012)

    ok_l, msg_l, cal_l = left_corr.calibrate_from_samples(samples_l)
    ok_r, msg_r, cal_r = right_corr.calibrate_from_samples(samples_r)
    print(f"  Left  stick: {msg_l} center=({cal_l.center_x:+.4f}, {cal_l.center_y:+.4f})")
    print(f"  Right stick: {msg_r} center=({cal_r.center_x:+.4f}, {cal_r.center_y:+.4f})")

    if ok_l and ok_r:
        data = {
            "profile": profile_name,
            "calibration": {
                "left": asdict(cal_l),
                "right": asdict(cal_r),
            },
        }
        cfg_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Калибровка сохранена в {cfg_file}")


def create_clean_uinput(dev: "InputDevice") -> "UInput":
    """
    Create a virtual UInput gamepad cloning dev, with fuzz=0 and flat=0
    on the stick axes so the Linux kernel never drops small smooth steps near center.
    """
    caps = dev.capabilities(absinfo=True)
    caps.pop(ecodes.EV_SYN, None)
    caps.pop(ecodes.EV_FF, None)

    if ecodes.EV_ABS in caps:
        new_abs = []
        for code, info in caps[ecodes.EV_ABS]:
            if code in STICK_AXES:
                clean_info = AbsInfo(
                    value=0,
                    min=info.min,
                    max=info.max,
                    fuzz=0,
                    flat=0,
                    resolution=info.resolution,
                )
                new_abs.append((code, clean_info))
            else:
                new_abs.append((code, info))
        caps[ecodes.EV_ABS] = new_abs

    return UInput(
        events=caps,
        name=f"SmartDrift Virtual ({dev.name})",
        vendor=dev.info.vendor,
        product=dev.info.product,
        version=dev.info.version,
        bustype=dev.info.bustype,
    )


def run_bridge(
    device_path: str,
    profile_name: str,
    grab: bool,
    config_path: str,
    do_calibrate: bool = False,
) -> None:
    if evdev is None:
        print("Ошибка: установите пакет evdev (sudo pacman -S python-evdev).")
        sys.exit(1)

    kill_stale_instances()
    time.sleep(0.15)

    dev = InputDevice(device_path)
    print(f"Подключён физический контроллер: {dev.name} ({dev.path})")

    profile = PROFILES.get(profile_name, PROFILES["action"])
    left_corr = AdaptiveStickCorrector(profile)
    right_corr = AdaptiveStickCorrector(profile)

    abs_caps: Dict[int, AbsInfo] = {}
    for code, info in dev.capabilities().get(ecodes.EV_ABS, []):
        abs_caps[code] = info

    cfg_file = Path(config_path)
    loaded_ok = False
    if not do_calibrate and cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            for key, corr, axes in (("left", left_corr, LEFT_AXES), ("right", right_corr, RIGHT_AXES)):
                cal = data.get("calibration", {}).get(key)
                if cal and cal.get("calibrated"):
                    cx = float(cal["center_x"])
                    cy = float(cal["center_y"])
                    live_x = normalize_axis(dev.absinfo(axes[0]).value, abs_caps[axes[0]]) if axes[0] in abs_caps else 0.0
                    live_y = normalize_axis(dev.absinfo(axes[1]).value, abs_caps[axes[1]]) if axes[1] in abs_caps else 0.0
                    # If saved center is bogus (>0.20) or differs wildly from live resting stick, auto-recalibrate
                    if abs(cx) > 0.20 or abs(cy) > 0.20 or (abs(live_x) < 0.15 and abs(cx - live_x) > 0.08):
                        loaded_ok = False
                        break
                    corr.calibration.center_x = cx
                    corr.calibration.center_y = cy
                    corr.calibration.noise_std_x = float(cal.get("noise_std_x", 0.003))
                    corr.calibration.noise_std_y = float(cal.get("noise_std_y", 0.003))
                    corr.calibration.calibrated = True
                    loaded_ok = True
            if loaded_ok:
                print(
                    f"Калибровка загружена: L=({left_corr.calibration.center_x:+.4f}, {left_corr.calibration.center_y:+.4f}) "
                    f"R=({right_corr.calibration.center_x:+.4f}, {right_corr.calibration.center_y:+.4f})"
                )
        except Exception as exc:
            print(f"Предупреждение: не удалось загрузить {cfg_file}: {exc}")

    if do_calibrate or not loaded_ok:
        calibrate_evdev(dev, abs_caps, left_corr, right_corr, cfg_file, profile_name, duration_sec=1.2)

    if grab:
        try:
            dev.grab()
            print("EVIOCGRAB активен — физический контроллер скрыт от игр (защита от двойного ввода).")
        except OSError as exc:
            print(f"Не удалось захватить устройство (EVIOCGRAB): {exc}")

    ui = create_clean_uinput(dev)
    print(f"Создан виртуальный контроллер: {ui.name} | Профиль: {profile.label}")
    print("Нажмите Ctrl+C для остановки.")

    raw_state: Dict[int, float] = {c: 0.0 for c in STICK_AXES}
    last_out: Dict[int, int] = {}
    next_dsp_tick = time.perf_counter()

    try:
        while True:
            now = time.perf_counter()
            wait_sec = max(0.0, next_dsp_tick - now)
            r, _, _ = select.select([dev.fd], [], [], wait_sec)
            need_syn = False

            if r:
                try:
                    for event in dev.read():
                        if event.type == ecodes.EV_ABS and event.code in STICK_AXES:
                            continue
                        if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT:
                            continue
                        ui.write_event(event)
                        need_syn = True
                except BlockingIOError:
                    pass

            now = time.perf_counter()
            if now >= next_dsp_tick:
                # Advance strict 125 Hz clock
                next_dsp_tick = now + DSP_DT

                # Poll authoritative hardware stick positions directly from kernel (EVIOCGABS)
                for c in STICK_AXES:
                    if c in abs_caps:
                        raw_state[c] = normalize_axis(dev.absinfo(c).value, abs_caps[c])

                for axes, corr in ((LEFT_AXES, left_corr), (RIGHT_AXES, right_corr)):
                    if axes[0] in abs_caps and axes[1] in abs_caps:
                        diag = corr.process(raw_state[axes[0]], raw_state[axes[1]], DSP_DT)
                        vx = denormalize_axis(diag.out_x, abs_caps[axes[0]])
                        vy = denormalize_axis(diag.out_y, abs_caps[axes[1]])
                        if last_out.get(axes[0]) != vx:
                            ui.write(ecodes.EV_ABS, axes[0], vx)
                            last_out[axes[0]] = vx
                            need_syn = True
                        if last_out.get(axes[1]) != vy:
                            ui.write(ecodes.EV_ABS, axes[1], vy)
                            last_out[axes[1]] = vy
                            need_syn = True

            if need_syn:
                ui.syn()
    except KeyboardInterrupt:
        print("\nОстановка виртуального контроллера...")
    finally:
        if grab:
            try:
                dev.ungrab()
            except OSError:
                pass
        ui.close()
        dev.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart Stick Drift Correction — Linux Virtual Gamepad")
    parser.add_argument("--list", action="store_true", help="Показать доступные устройства ввода")
    parser.add_argument("--device", type=str, help="Путь к устройству (например, /dev/input/event21)")
    parser.add_argument("--profile", type=str, default="action", choices=list(PROFILES.keys()))
    parser.add_argument("--no-grab", action="store_true", help="Не скрывать физический геймпад (отключить EVIOCGRAB)")
    parser.add_argument("--config", type=str, default="config.json", help="Путь к файлу калибровки config.json")
    parser.add_argument("--calibrate", action="store_true", help="Выполнить автокалибровку стиков через evdev при запуске")
    args = parser.parse_args()

    if args.list:
        list_devices()
        return

    if not args.device:
        parser.error("Укажите --device /dev/input/eventX или --list")

    run_bridge(
        args.device,
        args.profile,
        grab=not args.no_grab,
        config_path=args.config,
        do_calibrate=args.calibrate,
    )


if __name__ == "__main__":
    main()
