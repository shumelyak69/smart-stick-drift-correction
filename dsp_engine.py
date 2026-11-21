"""
Smart Stick Drift Correction — Core DSP & Diagnostic Engine.

Designed for 125 Hz fixed-step processing (dt = 0.008s):
1. Center bias compensation with sanity limits & asymmetric range rescaling
2. Worn-potentiometer contact-dropout bridge (holds across 1-4 frame open-circuit / short glitches)
3. 5-point median pre-filter
4. Adaptive One-Euro + EMA smoothing (no derivative blowup on potentiometer noise)
5. Dynamic deadzone with sustained-rest zero lock (never jerks toward center mid-move,
   always settles to exact 0.00, 0.00 within ~60ms of releasing the stick)
"""

from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Deque, Dict, List, Optional, Tuple


@dataclass
class FilterProfile:
    name: str
    label: str
    description: str
    filter_intensity: float = 0.65      # 0.0 .. 1.0
    bias_compensation: float = 1.0      # 0.0 .. 1.0
    micro_sensitivity: float = 0.75     # 0.0 .. 1.0
    max_latency_ms: float = 4.0
    min_cutoff: float = 2.5             # One-Euro min cutoff (Hz)
    beta: float = 0.15                  # One-Euro speed coefficient
    spike_threshold: float = 0.18       # per-tick (8ms) glitch threshold
    dynamic_dz_scale: float = 2.5
    median_window: int = 5


PROFILES: Dict[str, FilterProfile] = {
    "fps": FilterProfile(
        name="fps",
        label="FPS",
        description="Minimal latency and high precision for fine micro-aiming movements.",
        filter_intensity=0.45,
        bias_compensation=1.0,
        micro_sensitivity=0.88,
        max_latency_ms=2.5,
        min_cutoff=3.8,
        beta=0.25,
        spike_threshold=0.22,
        dynamic_dz_scale=2.0,
        median_window=3,
    ),
    "action": FilterProfile(
        name="action",
        label="Action",
        description="Balanced smoothing and wiper dropout protection for general gameplay.",
        filter_intensity=0.72,
        bias_compensation=1.0,
        micro_sensitivity=0.75,
        max_latency_ms=4.5,
        min_cutoff=2.2,
        beta=0.12,
        spike_threshold=0.16,
        dynamic_dz_scale=2.5,
        median_window=5,
    ),
    "racing": FilterProfile(
        name="racing",
        label="Racing",
        description="Smooth steering control without sudden twitches or high-frequency chatter.",
        filter_intensity=0.85,
        bias_compensation=1.0,
        micro_sensitivity=0.65,
        max_latency_ms=6.5,
        min_cutoff=1.6,
        beta=0.08,
        spike_threshold=0.14,
        dynamic_dz_scale=2.8,
        median_window=5,
    ),
    "heavy": FilterProfile(
        name="heavy",
        label="Heavy (Worn Stick)",
        description="Maximum stabilization for heavily worn potentiometers with contact dropouts.",
        filter_intensity=0.92,
        bias_compensation=1.0,
        micro_sensitivity=0.60,
        max_latency_ms=8.0,
        min_cutoff=1.3,
        beta=0.06,
        spike_threshold=0.12,
        dynamic_dz_scale=3.0,
        median_window=5,
    ),
    "custom": FilterProfile(
        name="custom",
        label="Custom",
        description="Manual configuration of all adaptive filter parameters.",
        filter_intensity=0.72,
        bias_compensation=1.0,
        micro_sensitivity=0.75,
        max_latency_ms=4.5,
        min_cutoff=2.2,
        beta=0.12,
        spike_threshold=0.16,
        dynamic_dz_scale=2.5,
        median_window=5,
    ),
}


def clamp(val: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, val))


class OneEuroFilter1D:
    """Adaptive low-pass filter with bounded derivative so contact noise cannot cause jerks."""

    def __init__(self, min_cutoff: float = 2.2, beta: float = 0.12, d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x_prev: Optional[float] = None
        self._dx_prev: float = 0.0

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * max(cutoff, 1e-4))
        return 1.0 / (1.0 + tau / max(dt, 1e-5))

    def reset(self) -> None:
        self._x_prev = None
        self._dx_prev = 0.0

    def filter(self, x: float, dt: float) -> float:
        if self._x_prev is None:
            self._x_prev = x
            self._dx_prev = 0.0
            return x

        dt = clamp(dt, 0.004, 0.05)
        dx = (x - self._x_prev) / dt
        dx = clamp(dx, -12.0, 12.0)

        a_d = self._alpha(self.d_cutoff, dt)
        dx_hat = a_d * dx + (1.0 - a_d) * self._dx_prev

        cutoff = self.min_cutoff + self.beta * min(abs(dx_hat), 10.0)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1.0 - a) * self._x_prev

        self._x_prev = x_hat
        self._dx_prev = dx_hat
        return x_hat


@dataclass
class CalibrationData:
    center_x: float = 0.0
    center_y: float = 0.0
    noise_std_x: float = 0.005
    noise_std_y: float = 0.005
    stability_score: float = 100.0
    calibrated: bool = False


@dataclass
class StickDiagnostics:
    raw_x: float = 0.0
    raw_y: float = 0.0
    out_x: float = 0.0
    out_y: float = 0.0
    bias_magnitude: float = 0.0
    noise_rms: float = 0.0
    dynamic_deadzone: float = 0.01
    spikes_suppressed: int = 0
    is_intentional: bool = False
    fault_type: str = "normal"
    processing_ms: float = 0.0


class AdaptiveStickCorrector:
    """Full adaptive stick drift & potentiometer glitch correction pipeline for one 2-axis stick."""

    def __init__(self, profile: Optional[FilterProfile] = None):
        self.profile: FilterProfile = profile or PROFILES["action"]
        self.calibration = CalibrationData()

        self._euro_x = OneEuroFilter1D(self.profile.min_cutoff, self.profile.beta)
        self._euro_y = OneEuroFilter1D(self.profile.min_cutoff, self.profile.beta)

        self._history_x: Deque[float] = deque(maxlen=5)
        self._history_y: Deque[float] = deque(maxlen=5)
        self._noise_window: Deque[float] = deque(maxlen=60)

        self._clean_x: float = 0.0
        self._clean_y: float = 0.0
        self._glitch_streak: int = 0
        self._rest_frames: int = 0
        self._hold_frames: int = 0
        self.spikes_suppressed: int = 0

    def set_profile(self, profile: FilterProfile) -> None:
        self.profile = profile
        self._euro_x.min_cutoff = profile.min_cutoff
        self._euro_x.beta = profile.beta
        self._euro_y.min_cutoff = profile.min_cutoff
        self._euro_y.beta = profile.beta

    def calibrate_from_samples(self, samples: List[Tuple[float, float]]) -> Tuple[bool, str, CalibrationData]:
        if len(samples) < 20:
            return False, "Insufficient samples collected for calibration.", self.calibration

        xs = [s[0] for s in samples]
        ys = [s[1] for s in samples]

        span_x = max(xs) - min(xs)
        span_y = max(ys) - min(ys)
        if span_x > 0.20 or span_y > 0.20:
            return (
                False,
                "Stick movement detected during calibration. Release the stick and retry.",
                self.calibration,
            )

        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)

        # Reject bogus calibration if stick was held far off-center (> 22%)
        if abs(mean_x) > 0.22 or abs(mean_y) > 0.22:
            return (
                False,
                f"Stick offset too large ({mean_x:+.2f}, {mean_y:+.2f}). Release the stick to center.",
                self.calibration,
            )

        var_x = sum((x - mean_x) ** 2 for x in xs) / len(xs)
        var_y = sum((y - mean_y) ** 2 for y in ys) / len(ys)
        std_x = math.sqrt(var_x)
        std_y = math.sqrt(var_y)

        noise_mag = math.hypot(std_x, std_y)
        stability = clamp(100.0 - noise_mag * 800.0, 0.0, 100.0)

        self.calibration = CalibrationData(
            center_x=mean_x,
            center_y=mean_y,
            noise_std_x=max(std_x, 0.003),
            noise_std_y=max(std_y, 0.003),
            stability_score=round(stability, 1),
            calibrated=True,
        )
        self._euro_x.reset()
        self._euro_y.reset()
        self._history_x.clear()
        self._history_y.clear()
        self._clean_x = 0.0
        self._clean_y = 0.0
        self._glitch_streak = 0
        return True, "Calibration completed.", self.calibration

    def process(self, raw_x: float, raw_y: float, dt: float = 0.008) -> StickDiagnostics:
        t0 = time.perf_counter()

        # 1. Center bias compensation (clamped to ±0.22 so corrupted configs never break the stick)
        comp = self.profile.bias_compensation
        bx = clamp(self.calibration.center_x * comp, -0.22, 0.22)
        by = clamp(self.calibration.center_y * comp, -0.22, 0.22)
        cx = raw_x - bx
        cy = raw_y - by
        if comp > 0.0:
            if cx < 0.0:
                cx = cx / max(1.0 + bx, 0.5)
            elif cx > 0.0:
                cx = cx / max(1.0 - bx, 0.5)
            if cy < 0.0:
                cy = cy / max(1.0 + by, 0.5)
            elif cy > 0.0:
                cy = cy / max(1.0 - by, 0.5)
        cx = clamp(cx)
        cy = clamp(cy)

        # 2. Worn-potentiometer contact-dropout bridge:
        # Holds across 1-3 frame (8-24ms) open-circuit/short glitches,
        # but immediately accepts sustained moves/releases on frame 4 (32ms).
        raw_cx, raw_cy = cx, cy
        raw_mag = math.hypot(raw_cx, raw_cy)
        jump = math.hypot(raw_cx - self._clean_x, raw_cy - self._clean_y)
        if jump > self.profile.spike_threshold:
            if self._glitch_streak < 2:
                self._glitch_streak += 1
                self.spikes_suppressed += 1
                cx = self._clean_x
                cy = self._clean_y
            else:
                # Sustained for 3+ frames -> genuine fast move or stick release!
                self._glitch_streak = 0
                self._clean_x = raw_cx
                self._clean_y = raw_cy
                self._history_x.clear()
                self._history_y.clear()
        else:
            self._glitch_streak = 0
            self._clean_x = raw_cx
            self._clean_y = raw_cy

        self._history_x.append(cx)
        self._history_y.append(cy)

        # 3. 5-point (or 3-point) median pre-filter
        win = 5 if (self.profile.median_window >= 5 or self.profile.filter_intensity >= 0.65) else 3
        if len(self._history_x) >= win:
            mid = win // 2
            cx = sorted(list(self._history_x)[-win:])[mid]
            cy = sorted(list(self._history_y)[-win:])[mid]

        # 4. Dynamic deadzone calculation
        base_noise = math.hypot(self.calibration.noise_std_x, self.calibration.noise_std_y)
        bias_mag = math.hypot(bx, by)
        hysteresis_floor = 0.075 if self.profile.filter_intensity >= 0.6 else 0.045
        dyn_dz = clamp(
            max(
                base_noise * self.profile.dynamic_dz_scale * (1.2 - 0.4 * self.profile.micro_sensitivity),
                hysteresis_floor,
            ),
            0.015,
            0.14,
        )

        # 5. Sustained rest detector (only speeds up return-to-zero after 5 consecutive frames in deadzone,
        # so mid-move potentiometer dropouts toward 0 NEVER jerk the stick toward center!)
        raw_mag = math.hypot(cx, cy)
        if raw_mag <= dyn_dz:
            self._rest_frames = min(self._rest_frames + 1, 60)
        else:
            self._rest_frames = 0

        intensity = clamp(self.profile.filter_intensity, 0.0, 1.0)
        if self._rest_frames >= 5:
            self._euro_x.min_cutoff = 10.0
            self._euro_y.min_cutoff = 10.0
        else:
            self._euro_x.min_cutoff = max(0.8, self.profile.min_cutoff * (1.25 - 0.65 * intensity))
            self._euro_y.min_cutoff = self._euro_x.min_cutoff

        self._euro_x.beta = self.profile.beta * (0.4 + 0.8 * self.profile.micro_sensitivity)
        self._euro_y.beta = self._euro_x.beta

        fx = self._euro_x.filter(cx, dt)
        fy = self._euro_y.filter(cy, dt)

        # 6. Radial deadzone remapping
        mag = math.hypot(fx, fy)
        if self._rest_frames >= 6 and mag <= dyn_dz * 1.15:
            self._hold_frames = 0
            is_intentional = False
            out_x, out_y = 0.0, 0.0
            self._euro_x._x_prev = 0.0
            self._euro_y._x_prev = 0.0
        elif mag <= dyn_dz:
            self._hold_frames = max(self._hold_frames - 1, 0)
            is_intentional = False
            out_x, out_y = 0.0, 0.0
        else:
            self._hold_frames = min(self._hold_frames + 1, 60)
            is_intentional = True
            scaled = (mag - dyn_dz) / max(1.0 - dyn_dz, 1e-4)
            scaled = clamp(scaled, 0.0, 1.0)
            out_x = (fx / mag) * scaled
            out_y = (fy / mag) * scaled

        if not is_intentional:
            self._noise_window.append(raw_mag)
        noise_rms = (
            math.sqrt(sum(v * v for v in self._noise_window) / len(self._noise_window))
            if self._noise_window
            else base_noise
        )

        if self.spikes_suppressed > 3 and bias_mag > 0.04:
            fault = "complex"
        elif self.spikes_suppressed > 3:
            fault = "spikes"
        elif bias_mag > 0.04:
            fault = "bias"
        elif noise_rms > 0.02:
            fault = "jitter"
        else:
            fault = "normal"

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return StickDiagnostics(
            raw_x=raw_x,
            raw_y=raw_y,
            out_x=clamp(out_x),
            out_y=clamp(out_y),
            bias_magnitude=bias_mag,
            noise_rms=noise_rms,
            dynamic_deadzone=dyn_dz,
            spikes_suppressed=self.spikes_suppressed,
            is_intentional=is_intentional,
            fault_type=fault,
            processing_ms=elapsed_ms,
        )


def run_quality_benchmark(profile_name: str = "fps") -> Dict[str, object]:
    rng = random.Random(42)
    profile = PROFILES.get(profile_name, PROFILES["action"])
    corrector = AdaptiveStickCorrector(profile)

    true_bias_x, true_bias_y = 0.075, -0.052
    noise_amp = 0.012

    cal_samples = [
        (true_bias_x + rng.gauss(0, noise_amp), true_bias_y + rng.gauss(0, noise_amp))
        for _ in range(120)
    ]
    cal_ok, _, cal_data = corrector.calibrate_from_samples(cal_samples)

    rest_outputs = []
    latencies = []
    false_positives = 0
    for _ in range(240):
        rx = true_bias_x + rng.gauss(0, noise_amp)
        ry = true_bias_y + rng.gauss(0, noise_amp)
        diag = corrector.process(rx, ry, dt=0.008)
        mag = math.hypot(diag.out_x, diag.out_y)
        rest_outputs.append(mag)
        latencies.append(diag.processing_ms)
        if mag > 0.005:
            false_positives += 1

    residual_drift_pct = (sum(rest_outputs) / len(rest_outputs)) * 100.0

    micro_errors = []
    for i in range(120):
        target_x = 0.06 + 0.14 * (i / 119.0)
        target_y = 0.0
        rx = target_x + true_bias_x + rng.gauss(0, noise_amp * 0.5)
        ry = target_y + true_bias_y + rng.gauss(0, noise_amp * 0.5)
        diag = corrector.process(rx, ry, dt=0.008)
        micro_errors.append(math.hypot(diag.out_x - target_x, diag.out_y - target_y))
        latencies.append(diag.processing_ms)

    micro_rmse = math.sqrt(sum(e * e for e in micro_errors) / len(micro_errors))
    micro_accuracy_pct = clamp((1.0 - micro_rmse / 0.20) * 100.0, 0.0, 100.0)

    spikes_before = corrector.spikes_suppressed
    for i in range(100):
        rx = true_bias_x + rng.gauss(0, noise_amp * 0.5)
        ry = true_bias_y + rng.gauss(0, noise_amp * 0.5)
        if i % 10 == 5:
            rx += 0.35
        corrector.process(rx, ry, dt=0.008)
    spikes_caught = corrector.spikes_suppressed - spikes_before

    avg_latency_ms = sum(latencies) / max(len(latencies), 1)
    stability_score = clamp(100.0 - residual_drift_pct * 10.0 - false_positives * 0.2, 0.0, 100.0)

    return {
        "profile": profile.name,
        "calibrated": cal_ok,
        "residual_drift_pct": round(residual_drift_pct, 3),
        "avg_latency_ms": round(avg_latency_ms, 4),
        "micro_accuracy_pct": round(micro_accuracy_pct, 1),
        "spikes_suppressed": spikes_caught,
        "false_positives": false_positives,
        "stability_score": round(stability_score, 1),
        "calibration": asdict(cal_data),
    }


if __name__ == "__main__":
    for pname in ("fps", "action", "racing", "heavy"):
        print(pname, "->", run_quality_benchmark(pname))
