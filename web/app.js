import { AdaptiveStickCorrector, DriftSimulator, PROFILES } from "./dsp.js";

const leftCorr = new AdaptiveStickCorrector(PROFILES.action);
const rightCorr = new AdaptiveStickCorrector(PROFILES.action);
const simulator = new DriftSimulator();

let activeProfileName = "action";
let inputSource = "simulator";
let scopeStick = "left";

let calibrating = false;
let calStartTime = 0;
const CAL_DURATION_MS = 2500;
let calSamplesL = [];
let calSamplesR = [];

const SCOPE_LEN = 220;
const scopeRaw = new Array(SCOPE_LEN).fill(0);
const scopeOut = new Array(SCOPE_LEN).fill(0);

const el = (id) => document.getElementById(id);
const canvasLeft = el("canvasLeft");
const ctxLeft = canvasLeft.getContext("2d");
const canvasRight = el("canvasRight");
const ctxRight = canvasRight.getContext("2d");
const canvasScope = el("canvasScope");
const ctxScope = canvasScope.getContext("2d");

function showToast(msg) {
  const t = el("toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2600);
}

function renderProfiles() {
  const grid = el("profilesGrid");
  grid.innerHTML = "";
  for (const key of Object.keys(PROFILES)) {
    const p = PROFILES[key];
    const btn = document.createElement("button");
    btn.className = "profile-btn" + (key === activeProfileName ? " active" : "");
    btn.innerHTML = `<div class="p-name">${p.label}</div><div class="p-desc">${p.maxLatencyMs} ms</div>`;
    btn.addEventListener("click", () => selectProfile(key));
    grid.appendChild(btn);
  }
  const active = PROFILES[activeProfileName];
  el("profileDesc").textContent = active.description;
  el("modeLabel").textContent = `Mode: ${active.label} (Adaptive)`;
  syncSlidersFromProfile(leftCorr.profile);
}

function selectProfile(name) {
  activeProfileName = name;
  const p = PROFILES[name];
  leftCorr.setProfile(p);
  rightCorr.setProfile(p);
  renderProfiles();
}

function syncSlidersFromProfile(p) {
  el("slIntensity").value = p.filterIntensity;
  el("valIntensity").textContent = Math.round(p.filterIntensity * 100) + "%";
  el("slBias").value = p.biasCompensation;
  el("valBias").textContent = Math.round(p.biasCompensation * 100) + "%";
  el("slMicro").value = p.microSensitivity;
  el("valMicro").textContent = Math.round(p.microSensitivity * 100) + "%";
  el("slLatency").value = p.maxLatencyMs;
  el("valLatency").textContent = p.maxLatencyMs.toFixed(1) + " ms";
}

function applyCustomSlider() {
  activeProfileName = "custom";
  const custom = {
    ...leftCorr.profile,
    name: "custom",
    label: "Custom",
    filterIntensity: parseFloat(el("slIntensity").value),
    biasCompensation: parseFloat(el("slBias").value),
    microSensitivity: parseFloat(el("slMicro").value),
    maxLatencyMs: parseFloat(el("slLatency").value),
  };
  PROFILES.custom = custom;
  leftCorr.setProfile(custom);
  rightCorr.setProfile(custom);
  renderProfiles();
}

["slIntensity", "slBias", "slMicro", "slLatency"].forEach((id) => {
  el(id).addEventListener("input", applyCustomSlider);
});

el("simBias").addEventListener("change", (e) => (simulator.config.bias = e.target.checked));
el("simJitter").addEventListener("change", (e) => (simulator.config.jitter = e.target.checked));
el("simSpikes").addEventListener("change", (e) => (simulator.config.spikes = e.target.checked));
el("simThermal").addEventListener("change", (e) => (simulator.config.thermal = e.target.checked));
el("simMicroAim").addEventListener("change", (e) => (simulator.config.microAim = e.target.checked));

el("inputSource").addEventListener("change", (e) => {
  inputSource = e.target.value;
  el("simPanel").style.display = inputSource === "simulator" ? "block" : "none";
});

el("scopeStick").addEventListener("change", (e) => {
  scopeStick = e.target.value;
  scopeRaw.fill(0);
  scopeOut.fill(0);
});

window.addEventListener("gamepadconnected", (e) => {
  el("gamepadStatus").textContent = `Connected Gamepad: ${e.gamepad.id}`;
  inputSource = "gamepad";
  el("inputSource").value = "gamepad";
  el("simPanel").style.display = "none";
  showToast("Gamepad connected and activated");
});

window.addEventListener("gamepaddisconnected", () => {
  el("gamepadStatus").textContent = "Gamepad disconnected — switched to Drift Simulator";
  inputSource = "simulator";
  el("inputSource").value = "simulator";
  el("simPanel").style.display = "block";
});

el("btnCalibrate").addEventListener("click", () => {
  if (calibrating) return;
  calibrating = true;
  calStartTime = performance.now();
  calSamplesL = [];
  calSamplesR = [];
  el("calBanner").classList.add("visible");
  el("calProgress").style.width = "0%";
});

el("btnResetCal").addEventListener("click", () => {
  leftCorr.resetCalibration();
  rightCorr.resetCalibration();
  showToast("Calibration reset");
});

el("btnSaveConfig").addEventListener("click", async () => {
  const payload = {
    profile: activeProfileName,
    custom_profile: PROFILES.custom,
    calibration: {
      left: leftCorr.calibration,
      right: rightCorr.calibration,
    },
  };
  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.ok) showToast("Saved to config.json");
    else showToast("Save error: " + (data.error || "unknown"));
  } catch {
    showToast("Server unavailable");
  }
});

async function loadSavedConfig() {
  try {
    const res = await fetch("/api/config");
    const data = await res.json();
    if (data.profile && PROFILES[data.profile]) {
      selectProfile(data.profile);
    }
    if (data.calibration?.left?.calibrated) {
      leftCorr.calibration = data.calibration.left;
    }
    if (data.calibration?.right?.calibrated) {
      rightCorr.calibration = data.calibration.right;
    }
  } catch {
    // ignore if opened statically
  }
}

el("btnBenchmark").addEventListener("click", async () => {
  el("btnBenchmark").disabled = true;
  el("btnBenchmark").textContent = "Running...";
  try {
    const res = await fetch(`/api/benchmark?profile=${encodeURIComponent(activeProfileName)}`);
    const data = await res.json();
    el("bResidual").textContent = `${data.residual_drift_pct}%`;
    el("bLatency").textContent = `${data.avg_latency_ms} ms`;
    el("bMicro").textContent = `${data.micro_accuracy_pct}%`;
    el("bSpikes").textContent = `${data.spikes_suppressed}`;
    el("bFalse").textContent = `${data.false_positives}`;
    el("bStability").textContent = `${data.stability_score}%`;
    showToast(`Quality benchmark complete (${data.profile.toUpperCase()})`);
  } catch {
    showToast("Failed to run benchmark");
  } finally {
    el("btnBenchmark").disabled = false;
    el("btnBenchmark").textContent = "Run Quality Benchmark";
  }
});

function drawStick(ctx, diag, cal) {
  const w = ctx.canvas.width;
  const h = ctx.canvas.height;
  const cx = w / 2;
  const cy = h / 2;
  const r = w * 0.43;

  ctx.clearRect(0, 0, w, h);

  ctx.strokeStyle = "#233044";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.stroke();

  ctx.beginPath();
  ctx.moveTo(cx - r, cy);
  ctx.lineTo(cx + r, cy);
  ctx.moveTo(cx, cy - r);
  ctx.lineTo(cx, cy + r);
  ctx.stroke();

  ctx.strokeStyle = "rgba(34, 211, 238, 0.35)";
  ctx.fillStyle = "rgba(34, 211, 238, 0.08)";
  ctx.beginPath();
  ctx.arc(cx, cy, Math.max(4, diag.dynamicDeadzone * r), 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();

  if (cal.calibrated) {
    ctx.strokeStyle = "rgba(245, 158, 11, 0.6)";
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + cal.center_x * r, cy + cal.center_y * r);
    ctx.stroke();
  }

  const rx = cx + diag.rawX * r;
  const ry = cy + diag.rawY * r;
  ctx.fillStyle = "rgba(249, 115, 22, 0.85)";
  ctx.beginPath();
  ctx.arc(rx, ry, 5, 0, Math.PI * 2);
  ctx.fill();

  const ox = cx + diag.outX * r;
  const oy = cy + diag.outY * r;
  ctx.fillStyle = "#22d3ee";
  ctx.beginPath();
  ctx.arc(ox, oy, 6, 0, Math.PI * 2);
  ctx.fill();
}

function drawScope() {
  const w = canvasScope.width;
  const h = canvasScope.height;
  ctxScope.clearRect(0, 0, w, h);

  ctxScope.strokeStyle = "#1c2635";
  ctxScope.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = (h / 4) * i;
    ctxScope.beginPath();
    ctxScope.moveTo(0, y);
    ctxScope.lineTo(w, y);
    ctxScope.stroke();
  }

  const drawTrace = (arr, color, width) => {
    ctxScope.strokeStyle = color;
    ctxScope.lineWidth = width;
    ctxScope.beginPath();
    for (let i = 0; i < arr.length; i++) {
      const x = (i / (arr.length - 1)) * w;
      const y = h / 2 - arr[i] * (h * 0.44);
      if (i === 0) ctxScope.moveTo(x, y);
      else ctxScope.lineTo(x, y);
    }
    ctxScope.stroke();
  };

  drawTrace(scopeRaw, "rgba(249, 115, 22, 0.75)", 1.5);
  drawTrace(scopeOut, "#22d3ee", 2.2);
}

const FAULT_LABELS = {
  normal: { text: "Nominal", cls: "badge-ok" },
  bias: { text: "Center Offset", cls: "badge-warn" },
  jitter: { text: "High-Freq Jitter", cls: "badge-warn" },
  spikes: { text: "Impulse Spikes", cls: "badge-danger" },
  complex: { text: "Compound Drift", cls: "badge-danger" },
};

let lastTime = performance.now();

function tick(now) {
  const dt = Math.min((now - lastTime) / 1000, 0.05);
  lastTime = now;

  let lx = 0, ly = 0, rx = 0, ry = 0;

  if (inputSource === "gamepad") {
    const pads = navigator.getGamepads ? navigator.getGamepads() : [];
    const pad = Array.from(pads).find((p) => p && p.connected);
    if (pad) {
      lx = pad.axes[0] || 0;
      ly = pad.axes[1] || 0;
      rx = pad.axes[2] || 0;
      ry = pad.axes[3] || 0;
    }
  } else {
    const s = simulator.sample(dt);
    lx = s.lx;
    ly = s.ly;
    rx = s.rx;
    ry = s.ry;
  }

  if (calibrating) {
    calSamplesL.push([lx, ly]);
    calSamplesR.push([rx, ry]);
    const elapsed = now - calStartTime;
    const pct = Math.min(100, (elapsed / CAL_DURATION_MS) * 100);
    el("calProgress").style.width = `${pct}%`;

    if (elapsed >= CAL_DURATION_MS) {
      calibrating = false;
      el("calBanner").classList.remove("visible");
      const resL = leftCorr.calibrateFromSamples(calSamplesL);
      const resR = rightCorr.calibrateFromSamples(calSamplesR);
      if (resL.ok && resR.ok) {
        showToast("Auto-calibration completed for both sticks.");
      } else {
        showToast(!resL.ok ? resL.message : resR.message);
      }
    }
  }

  const dL = leftCorr.process(lx, ly, dt);
  const dR = rightCorr.process(rx, ry, dt);

  drawStick(ctxLeft, dL, leftCorr.calibration);
  drawStick(ctxRight, dR, rightCorr.calibration);

  const activeScope = scopeStick === "left" ? dL : dR;
  scopeRaw.push(activeScope.rawX);
  scopeRaw.shift();
  scopeOut.push(activeScope.outX);
  scopeOut.shift();
  drawScope();

  el("lRaw").textContent = `${dL.rawX.toFixed(3)}, ${dL.rawY.toFixed(3)}`;
  el("lOut").textContent = `${dL.outX.toFixed(3)}, ${dL.outY.toFixed(3)}`;
  el("rRaw").textContent = `${dR.rawX.toFixed(3)}, ${dR.rawY.toFixed(3)}`;
  el("rOut").textContent = `${dR.outX.toFixed(3)}, ${dR.outY.toFixed(3)}`;

  const faultInfo = FAULT_LABELS[dL.faultType] || FAULT_LABELS.normal;
  const badge = el("faultBadge");
  badge.textContent = faultInfo.text;
  badge.className = `badge ${faultInfo.cls}`;

  el("mBias").textContent = `${(dL.biasMag * 100).toFixed(1)}%`;
  el("mCalState").textContent = leftCorr.calibration.calibrated
    ? `Calibrated (${leftCorr.calibration.center_x.toFixed(3)}, ${leftCorr.calibration.center_y.toFixed(3)})`
    : "Uncalibrated";
  el("mDynDz").textContent = `${(dL.dynamicDeadzone * 100).toFixed(1)}%`;
  el("mNoise").textContent = `Noise RMS: ${dL.noiseRms.toFixed(4)}`;
  el("mSpikes").textContent = String(dL.spikesSuppressed + dR.spikesSuppressed);
  el("mIntent").textContent = dL.isIntentional || dR.isIntentional ? "State: Active Motion" : "State: At Rest";
  el("mLatency").textContent = `${dL.processingMs.toFixed(2)} ms`;
  el("mStability").textContent = `Stability: ${leftCorr.calibration.stability_score}%`;

  requestAnimationFrame(tick);
}

renderProfiles();
loadSavedConfig();
requestAnimationFrame(tick);
