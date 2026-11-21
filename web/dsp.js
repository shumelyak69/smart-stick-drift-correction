// Smart Stick Drift Correction — Client-Side Real-Time DSP Engine & Drift Simulator

export const PROFILES = {
  fps: {
    name: "fps",
    label: "FPS",
    description: "Минимальная задержка и высокая точность мелких движений (прицеливание).",
    filterIntensity: 0.35,
    biasCompensation: 1.0,
    microSensitivity: 0.90,
    maxLatencyMs: 2.0,
    minCutoff: 6.0,
    beta: 0.80,
    spikeThreshold: 0.14,
    dynamicDzScale: 2.0,
    medianWindow: 3,
  },
  action: {
    name: "action",
    label: "Action",
    description: "Сбалансированная и плавная коррекция для большинства жанров.",
    filterIntensity: 0.60,
    biasCompensation: 1.0,
    microSensitivity: 0.75,
    maxLatencyMs: 4.0,
    minCutoff: 4.0,
    beta: 0.55,
    spikeThreshold: 0.10,
    dynamicDzScale: 2.5,
    medianWindow: 5,
  },
  racing: {
    name: "racing",
    label: "Racing",
    description: "Максимально плавное управление без резких скачков и дрожания.",
    filterIntensity: 0.78,
    biasCompensation: 1.0,
    microSensitivity: 0.65,
    maxLatencyMs: 6.0,
    minCutoff: 3.0,
    beta: 0.40,
    spikeThreshold: 0.09,
    dynamicDzScale: 2.8,
    medianWindow: 5,
  },
  heavy: {
    name: "heavy",
    label: "Heavy",
    description: "Максимальное подавление провалов контакта и дребезга изношенного стика.",
    filterIntensity: 0.88,
    biasCompensation: 1.0,
    microSensitivity: 0.55,
    maxLatencyMs: 8.0,
    minCutoff: 2.5,
    beta: 0.35,
    spikeThreshold: 0.08,
    dynamicDzScale: 3.2,
    medianWindow: 5,
  },
  custom: {
    name: "custom",
    label: "Custom",
    description: "Ручная настройка параметров адаптивного фильтра.",
    filterIntensity: 0.60,
    biasCompensation: 1.0,
    microSensitivity: 0.75,
    maxLatencyMs: 4.0,
    minCutoff: 4.0,
    beta: 0.55,
    spikeThreshold: 0.10,
    dynamicDzScale: 2.5,
    medianWindow: 5,
  },
};

export function clamp(v, lo = -1, hi = 1) {
  return Math.max(lo, Math.min(hi, v));
}

class OneEuroFilter1D {
  constructor(minCutoff = 4.0, beta = 0.55, dCutoff = 1.0) {
    this.minCutoff = minCutoff;
    this.beta = beta;
    this.dCutoff = dCutoff;
    this.xPrev = null;
    this.dxPrev = 0;
  }

  alpha(cutoff, dt) {
    const tau = 1.0 / (2.0 * Math.PI * Math.max(cutoff, 1e-4));
    return 1.0 / (1.0 + tau / Math.max(dt, 1e-5));
  }

  reset() {
    this.xPrev = null;
    this.dxPrev = 0;
  }

  filter(x, dt) {
    if (this.xPrev === null) {
      this.xPrev = x;
      this.dxPrev = 0;
      return x;
    }
    const safeDt = clamp(dt, 0.002, 0.05);
    const dx = (x - this.xPrev) / safeDt;
    const aD = this.alpha(this.dCutoff, safeDt);
    const dxHat = aD * dx + (1 - aD) * this.dxPrev;

    const cutoff = this.minCutoff + this.beta * Math.abs(dxHat);
    const a = this.alpha(cutoff, safeDt);
    const xHat = a * x + (1 - a) * this.xPrev;

    this.xPrev = xHat;
    this.dxPrev = dxHat;
    return xHat;
  }
}

export class AdaptiveStickCorrector {
  constructor(profile = PROFILES.action) {
    this.profile = { ...profile };
    this.calibration = {
      center_x: 0,
      center_y: 0,
      noise_std_x: 0.005,
      noise_std_y: 0.005,
      stability_score: 100,
      calibrated: false,
    };

    this.euroX = new OneEuroFilter1D(this.profile.minCutoff, this.profile.beta);
    this.euroY = new OneEuroFilter1D(this.profile.minCutoff, this.profile.beta);

    this.historyX = [];
    this.historyY = [];
    this.noiseWindow = [];
    this.prevRawX = 0;
    this.prevRawY = 0;
    this.holdFrames = 0;
    this.spikesSuppressed = 0;
  }

  setProfile(profile) {
    this.profile = { ...profile };
  }

  resetCalibration() {
    this.calibration = {
      center_x: 0,
      center_y: 0,
      noise_std_x: 0.005,
      noise_std_y: 0.005,
      stability_score: 100,
      calibrated: false,
    };
    this.spikesSuppressed = 0;
    this.euroX.reset();
    this.euroY.reset();
    this.historyX = [];
    this.historyY = [];
  }

  calibrateFromSamples(samples) {
    if (samples.length < 20) {
      return { ok: false, message: "Недостаточно данных для калибровки." };
    }
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    let sumX = 0, sumY = 0;
    for (const [x, y] of samples) {
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
      sumX += x;
      sumY += y;
    }

    if (maxX - minX > 0.25 || maxY - minY > 0.25) {
      return {
        ok: false,
        message: "Обнаружено активное движение стика! Отпустите стик и повторите.",
      };
    }

    const meanX = sumX / samples.length;
    const meanY = sumY / samples.length;
    let varX = 0, varY = 0;
    for (const [x, y] of samples) {
      varX += (x - meanX) ** 2;
      varY += (y - meanY) ** 2;
    }
    const stdX = Math.sqrt(varX / samples.length);
    const stdY = Math.sqrt(varY / samples.length);
    const noiseMag = Math.hypot(stdX, stdY);
    const stability = clamp(100 - noiseMag * 800, 0, 100);

    this.calibration = {
      center_x: meanX,
      center_y: meanY,
      noise_std_x: Math.max(stdX, 0.003),
      noise_std_y: Math.max(stdY, 0.003),
      stability_score: Math.round(stability * 10) / 10,
      calibrated: true,
    };
    this.euroX.reset();
    this.euroY.reset();
    this.historyX = [];
    this.historyY = [];
    return { ok: true, message: "Калибровка успешно завершена." };
  }

  process(rawX, rawY, dt = 1 / 60) {
    const t0 = performance.now();

    // 1. Center bias compensation + asymmetric range rescaling
    const comp = this.profile.biasCompensation;
    const bx = this.calibration.center_x * comp;
    const by = this.calibration.center_y * comp;
    let cx = rawX - bx;
    let cy = rawY - by;
    if (comp > 0) {
      if (cx < 0) cx = cx / Math.max(1 + bx, 0.25);
      else if (cx > 0) cx = cx / Math.max(1 - bx, 0.25);
      if (cy < 0) cy = cy / Math.max(1 + by, 0.25);
      else if (cy > 0) cy = cy / Math.max(1 - by, 0.25);
    }
    cx = clamp(cx);
    cy = clamp(cy);

    // 2. Always-on median pre-filter
    const rawCx = cx;
    const rawCy = cy;
    const jump = Math.hypot(rawCx - this.prevRawX, rawCy - this.prevRawY);
    if (jump > this.profile.spikeThreshold && this.historyX.length >= 3) {
      this.spikesSuppressed += 1;
    }

    this.prevRawX = rawCx;
    this.prevRawY = rawCy;
    this.historyX.push(rawCx);
    this.historyY.push(rawCy);
    if (this.historyX.length > 5) this.historyX.shift();
    if (this.historyY.length > 5) this.historyY.shift();

    const win = this.profile.medianWindow >= 5 || this.profile.filterIntensity >= 0.78 ? 5 : 3;
    if (this.historyX.length >= win) {
      const mid = Math.floor(win / 2);
      cx = this.historyX.slice(-win).sort((a, b) => a - b)[mid];
      cy = this.historyY.slice(-win).sort((a, b) => a - b)[mid];
    }

    // 3. Dynamic deadzone
    const baseNoise = Math.hypot(this.calibration.noise_std_x, this.calibration.noise_std_y);
    const biasMag = Math.hypot(this.calibration.center_x, this.calibration.center_y);
    const hysteresisFloor = this.profile.filterIntensity >= 0.6 ? 0.075 : 0.045;
    const dynDz = clamp(
      Math.max(
        baseNoise * this.profile.dynamicDzScale * (1.2 - 0.4 * this.profile.microSensitivity),
        hysteresisFloor
      ),
      0.015,
      0.14
    );

    // 4. Adaptive One-Euro filtering
    const rawMag = Math.hypot(cx, cy);
    const intensity = clamp(this.profile.filterIntensity, 0, 1);
    if (rawMag <= dynDz) {
      this.euroX.minCutoff = 12.0;
      this.euroY.minCutoff = 12.0;
    } else {
      this.euroX.minCutoff = Math.max(0.5, this.profile.minCutoff * (1.25 - 0.75 * intensity));
      this.euroY.minCutoff = this.euroX.minCutoff;
    }
    this.euroX.beta = this.profile.beta * (0.4 + 0.8 * this.profile.microSensitivity);
    this.euroY.beta = this.euroX.beta;

    const fx = this.euroX.filter(cx, dt);
    const fy = this.euroY.filter(cy, dt);

    // 5. Radial deadzone remapping
    const mag = Math.hypot(fx, fy);
    let isIntentional = false;
    let outX = 0;
    let outY = 0;

    if (rawMag <= dynDz && mag <= dynDz * 1.15) {
      this.holdFrames = 0;
      this.euroX.xPrev = 0;
      this.euroY.xPrev = 0;
    } else if (mag > dynDz) {
      this.holdFrames = Math.min(this.holdFrames + 1, 60);
      isIntentional = true;
      const scaled = clamp((mag - dynDz) / Math.max(1 - dynDz, 1e-4), 0, 1);
      outX = (fx / mag) * scaled;
      outY = (fy / mag) * scaled;
    }

    if (!isIntentional) {
      this.noiseWindow.push(rawMag);
      if (this.noiseWindow.length > 60) this.noiseWindow.shift();
    }
    const noiseRms =
      this.noiseWindow.length > 0
        ? Math.sqrt(this.noiseWindow.reduce((acc, v) => acc + v * v, 0) / this.noiseWindow.length)
        : baseNoise;

    const rawRestMag = Math.hypot(rawX, rawY);
    let faultType = "normal";
    if (this.spikesSuppressed > 2 && (biasMag > 0.04 || (!this.calibration.calibrated && rawRestMag > 0.05))) {
      faultType = "complex";
    } else if (this.spikesSuppressed > 2) {
      faultType = "spikes";
    } else if (biasMag > 0.04 || (!this.calibration.calibrated && rawRestMag > 0.05 && !isIntentional)) {
      faultType = "bias";
    } else if (noiseRms > 0.022) {
      faultType = "jitter";
    }

    return {
      rawX,
      rawY,
      outX: clamp(outX),
      outY: clamp(outY),
      biasMag,
      noiseRms,
      dynamicDeadzone: dynDz,
      spikesSuppressed: this.spikesSuppressed,
      isIntentional,
      faultType,
      processingMs: performance.now() - t0,
    };
  }
}

export class DriftSimulator {
  constructor() {
    this.t = 0;
    this.config = {
      bias: true,
      jitter: true,
      spikes: true,
      thermal: false,
      microAim: false,
    };
  }

  sample(dt) {
    this.t += dt;
    const gauss = () => (Math.random() + Math.random() + Math.random() - 1.5) * 0.66;

    let lx = 0;
    let ly = 0;
    if (this.config.bias) {
      lx += 0.085;
      ly -= 0.062;
    }
    if (this.config.jitter) {
      lx += gauss() * 0.018;
      ly += gauss() * 0.018;
    }
    if (this.config.thermal) {
      lx += Math.sin(this.t * 0.25) * 0.025;
      ly += Math.cos(this.t * 0.18) * 0.02;
    }
    if (this.config.spikes && Math.random() < 0.015) {
      lx += (Math.random() > 0.5 ? 1 : -1) * (0.25 + Math.random() * 0.15);
    }

    let rx = this.config.bias ? -0.055 : 0;
    let ry = this.config.bias ? 0.045 : 0;
    if (this.config.jitter) {
      rx += gauss() * 0.014;
      ry += gauss() * 0.014;
    }
    if (this.config.microAim) {
      rx += Math.sin(this.t * 1.4) * 0.11;
      ry += Math.sin(this.t * 2.8) * 0.07;
    }

    return {
      lx: clamp(lx),
      ly: clamp(ly),
      rx: clamp(rx),
      ry: clamp(ry),
    };
  }
}
