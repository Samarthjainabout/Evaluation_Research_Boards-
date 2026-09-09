const state = {
  selectedRun: "",
  activeKey: "",
  commandsEnabled: false,
  manualRun: false,
  currentMin_uA: 0,
  currentMax_uA: 200,
};

const READ_VCC_SET_V = 0.5;
const CURRENT_DISPLAY_SCALE = 1 / READ_VCC_SET_V;
const SET_TRACE_MARKER_US = 100;
const HEATMAP_SCALE_MIN_UA = 0;
const HEATMAP_SCALE_MAX_UA = 300;
const LOG_DISPLAY_FLOOR_US = 0.2;
const DEFAULT_WB_READ_VALUE = "0x4002AA82";
const DEFAULT_WB_WRITE_VALUE = "0x500888FF";

const els = {
  runSelect: document.getElementById("runSelect"),
  followBtn: document.getElementById("followBtn"),
  apiSignal: document.getElementById("apiSignal"),
  tickerText: document.getElementById("tickerText"),
  lastCell: document.getElementById("lastCell"),
  lastCurrent: document.getElementById("lastCurrent"),
  heatmap: document.getElementById("heatmap"),
  scaleMin: document.getElementById("scaleMin"),
  scaleMax: document.getElementById("scaleMax"),
  currentMinRange: document.getElementById("currentMinRange"),
  currentMaxRange: document.getElementById("currentMaxRange"),
  currentMinInput: document.getElementById("currentMinInput"),
  currentMaxInput: document.getElementById("currentMaxInput"),
  activeCell: document.getElementById("activeCell"),
  opDot: document.getElementById("opDot"),
  opCompact: document.getElementById("opCompact"),
  packetCompact: document.getElementById("packetCompact"),
  chart: document.getElementById("chart"),
  commandForm: document.getElementById("commandForm"),
  operationInput: document.getElementById("operationInput"),
  rowField: document.getElementById("rowField"),
  colField: document.getElementById("colField"),
  rowInput: document.getElementById("rowInput"),
  colInput: document.getElementById("colInput"),
  wbValueField: document.getElementById("wbValueField"),
  wbValueInput: document.getElementById("wbValueInput"),
  zynqPasswordInput: document.getElementById("zynqPasswordInput"),
  dryRunInput: document.getElementById("dryRunInput"),
  resumeColField: document.getElementById("resumeColField"),
  resumeColInput: document.getElementById("resumeColInput"),
  commandBtn: document.getElementById("commandBtn"),
  resumeArrayBtn: document.getElementById("resumeArrayBtn"),
  killBtn: document.getElementById("killBtn"),
  commandNote: document.getElementById("commandNote"),
};

function formatCurrent(value) {
  return formatScaledCurrent(scaleCurrent(value));
}

function formatScaledCurrent(value) {
  return Number.isFinite(value) ? `${value.toFixed(1)}` : "--";
}

function formatVoltage(value) {
  return Number.isFinite(value) ? value.toFixed(2) : "--";
}

function scaleCurrent(value) {
  return Number.isFinite(value) ? value * CURRENT_DISPLAY_SCALE : value;
}

function formatCell(cell) {
  return cell ? `r${String(cell.row).padStart(2, "0")} c${String(cell.col).padStart(2, "0")}` : "--";
}

function cellKey(cell) {
  return cell ? `${cell.row}_${cell.col}` : "";
}

function colorFor(value, min, max) {
  if (!Number.isFinite(value)) return "#2a2d34";
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) return "#4cc9a6";
  const clamped = Math.max(min, Math.min(max, value));
  const t = (clamped - min) / (max - min);
  const hue = 205 - t * 170;
  const light = 38 + t * 20;
  return `hsl(${hue}, 78%, ${light}%)`;
}

function currentRange() {
  const requestedMin = Number(state.currentMin_uA);
  const requestedMax = Number(state.currentMax_uA);
  const min = Math.max(HEATMAP_SCALE_MIN_UA, Math.min(HEATMAP_SCALE_MAX_UA, requestedMin));
  const max = Math.max(HEATMAP_SCALE_MIN_UA, Math.min(HEATMAP_SCALE_MAX_UA, requestedMax));
  return {
    min: Number.isFinite(min) ? min : HEATMAP_SCALE_MIN_UA,
    max: Number.isFinite(max) && max > min ? max : min + 5,
  };
}

function syncCurrentRangeControls() {
  const { min, max } = currentRange();
  els.currentMinRange.value = String(min);
  els.currentMinInput.value = String(min);
  els.currentMaxRange.value = String(max);
  els.currentMaxInput.value = String(max);
}

function setCurrentRange(part, value) {
  const next = Number(value);
  if (!Number.isFinite(next)) return;
  const bounded = Math.max(HEATMAP_SCALE_MIN_UA, Math.min(HEATMAP_SCALE_MAX_UA, next));
  if (part === "min") {
    state.currentMin_uA = Math.min(bounded, state.currentMax_uA - 5);
  } else {
    state.currentMax_uA = Math.max(bounded, state.currentMin_uA + 5);
  }
  syncCurrentRangeControls();
  repaintHeatmap();
  renderChart(state.lastSummary);
}

function ensureGrid() {
  if (els.heatmap.children.length === 1024) return;
  els.heatmap.innerHTML = "";
  for (let row = 0; row < 32; row += 1) {
    for (let col = 0; col < 32; col += 1) {
      const button = document.createElement("button");
      button.className = "cell";
      button.type = "button";
      button.dataset.key = `${row}_${col}`;
      button.title = `r${row} c${col}: no reading`;
      button.addEventListener("click", () => {
        els.rowInput.value = row;
        els.colInput.value = col;
      });
      els.heatmap.appendChild(button);
    }
  }
}

function renderRuns(runs, currentRunId) {
  const previous = state.manualRun ? state.selectedRun || els.runSelect.value : currentRunId;
  els.runSelect.innerHTML = "";
  for (const run of runs) {
    const option = document.createElement("option");
    option.value = run.id;
    option.textContent = run.id;
    els.runSelect.appendChild(option);
  }
  if (runs.some((run) => run.id === previous)) {
    els.runSelect.value = previous;
  }
  state.selectedRun = els.runSelect.value;
  renderFollowMode();
}

function renderFollowMode() {
  els.followBtn.textContent = state.manualRun ? "PINNED" : "FOLLOWING";
  els.followBtn.title = state.manualRun ? "Pinned to selected run. Click to follow latest." : "Following latest active run. Click to pin current run.";
  els.followBtn.classList.toggle("active", !state.manualRun);
  els.followBtn.classList.toggle("pinned", state.manualRun);
}

function renderMetrics(summary) {
  const last = summary?.last;
  const wishboneResult = summary?.wishboneResult;
  const activeCell = summary?.lastCell;
  const lastReadCell = summary?.lastReadCell;
  els.lastCell.textContent = formatCell(lastReadCell);
  els.lastCurrent.textContent = formatCurrent(summary?.lastCurrent_uA);

  const op = last?.operation || wishboneResult?.operation || "--";
  els.activeCell.textContent = formatCell(activeCell);
  els.opCompact.textContent = op;
  els.packetCompact.textContent = last?.packet || "--";
  els.opDot.className = `dot ${op === "read" ? "read" : op === "--" ? "" : "program"}`;
  const resultState = last || wishboneResult;
  els.apiSignal.className = `signal ${summary?.activeError ? "bad" : resultState ? (resultState.ok ? "good" : "bad") : ""}`;
  renderTicker(summary);
}

function renderTicker(summary) {
  const history = summary?.history || [];
  const logEvents = summary?.logEvents || [];
  const progressEvents = summary?.progressEvents || [];
  const wishboneResult = summary?.wishboneResult;
  const running = (state.lastCommands || []).find((command) => command.running);
  const rows = [
    ...history,
    ...logEvents,
    ...progressEvents,
    ...(wishboneResult ? [{ ...wishboneResult, source: "wishbone-result", eventOrder: Number.MAX_SAFE_INTEGER - 1 }] : []),
    ...(running ? [activeCommandEvent(running, summary)] : []),
  ]
    .sort((a, b) => eventOrder(a) - eventOrder(b))
    .slice(-2)
    .reverse()
    .map(formatApiEvent);
  const first = rows[0] || "Waiting for the first API result.";
  const second = rows[1] || "--";
  els.tickerText.innerHTML = "";
  for (const [index, text] of [first, second].entries()) {
    const line = document.createElement("div");
    line.className = `ticker-line ${index === 1 ? "muted-line" : ""}`;
    line.textContent = text;
    els.tickerText.appendChild(line);
  }
}

function activeCommandEvent(command, summary) {
  const last = summary?.last;
  const cell = Number.isFinite(command.row) ? { row: command.row, col: command.col ?? 0 } : last?.cellAddress;
  // History may contain earlier runs. Never borrow their voltage as live telemetry.
  const sameRun = String(command.runDir || "").replaceAll("\\", "/").split("/").pop() === summary?.run?.id;
  const pulse = sameRun ? summary?.last : null;
  const liveRails = Number.isFinite(command.activeVccSet_V) && Number.isFinite(command.activeVccWlSet_V);
  return {
    source: "active-command",
    operation: command.operation || "api",
    cellAddress: cell,
    vcc_set_V: liveRails ? command.activeVccSet_V : pulse?.vcc_set_V,
    vcc_wl_set_V: liveRails ? command.activeVccWlSet_V : pulse?.vcc_wl_set_V,
    voltageSource: liveRails ? "requested" : "last recorded",
    voltageOperation: liveRails ? "" : pulse?.operation,
    eventOrder: Number.MAX_SAFE_INTEGER,
  };
}

function latestPulseForOperation(history, operation) {
  if (!(operation === "set" || operation === "reset")) return null;
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const row = history[index];
    if (row?.operation === operation && !isRead(row)) return row;
  }
  return null;
}

function eventOrder(row) {
  if (Number.isFinite(row.eventOrder)) return row.eventOrder;
  if (Number.isFinite(row.updated)) return row.updated * 1000;
  if (Number.isFinite(row.index)) return row.index;
  const match = String(row.index || "").match(/capture_(\d+)/);
  return match ? Number(match[1]) : 0;
}

function formatApiEvent(row) {
  if (row.source === "wishbone-result") {
    if (row.dry_run) return `${String(row.operation || "WB").toUpperCase()}: dry run; no UART value`;
    const readbacks = Array.isArray(row.readbacks) && row.readbacks.length
      ? ` — ${row.readbacks.length} reads: ${row.readbacks.join(", ")}`
      : "";
    return `${String(row.operation || "WB").toUpperCase()} RETURN: ${row.return_value || "no value"} via FPGA UART${readbacks}`;
  }
  if (row.source === "log") {
    return `ERROR: ${formatApiMessage(row.message)}`;
  }
  if (row.source === "progress") {
    const hasCount = Number.isFinite(row.cells) && row.cells > 0;
    const count = hasCount && Number.isFinite(row.total)
      ? ` (${row.cells}/${row.total})`
      : hasCount
        ? ` (${row.cells})`
        : "";
    const targetConductance = Number.isFinite(row.target_conductance_uS)
      ? row.target_conductance_uS
      : Number.isFinite(row.target_uA)
        ? scaleCurrent(row.target_uA)
        : null;
    const bitTarget = Number.isFinite(row.code) && Number.isFinite(targetConductance)
      ? ` — code ${row.code} target ${formatScaledCurrent(targetConductance)} uS`
      : "";
    return `${String(row.operation || "API").toUpperCase()}: ${formatApiMessage(row.message)}${count}${bitTarget}`;
  }
  if (row.source === "active-command") {
    const cell = formatCell(row.cellAddress);
    const op = String(row.operation || "API").toUpperCase();
    const rails = Number.isFinite(row.vcc_set_V) && Number.isFinite(row.vcc_wl_set_V)
      ? `${row.voltageSource || "last recorded"}${row.voltageOperation ? ` ${row.voltageOperation.toUpperCase()}` : ""}: Vcc ${formatVoltage(row.vcc_set_V)} V / WL ${formatVoltage(row.vcc_wl_set_V)} V`
      : "voltage telemetry unavailable";
    return `${op} running at ${cell}: ${rails}`;
  }
  const cell = formatCell(row.cellAddress);
  const packet = row.packet ? `packet ${row.packet}` : "packet unknown";
  const rails = Number.isFinite(row.vcc_set_V) && Number.isFinite(row.vcc_wl_set_V)
    ? `rails ${formatVoltage(row.vcc_set_V)} V / ${formatVoltage(row.vcc_wl_set_V)} V`
    : "rails unknown";
  const status = row.ok ? "decoded OK" : "needs check";
  if (isRead(row)) {
    return `Read ${cell}: ${formatCurrent(row.current_uA)} uS, ${packet}, ${status}`;
  }
  return `${String(row.operation || "Program").toUpperCase()} pulse at ${cell}: ${rails}, ${packet}, ${status}`;
}

function formatApiMessage(message) {
  return String(message || "").replace(/\bFPGA\s+/gi, "");
}

function renderHeatmap(summary) {
  ensureGrid();
  state.heatmapActiveKey = cellKey(summary?.lastCell);
  state.heatmapLatest = new Map();
  for (const item of summary?.cells || []) state.heatmapLatest.set(cellKey(item.cellAddress), item);
  repaintHeatmap();
}

function repaintHeatmap() {
  ensureGrid();
  const { min, max } = currentRange();
  const latest = state.heatmapLatest || new Map();
  for (const node of els.heatmap.children) {
    const item = latest.get(node.dataset.key);
    const value = scaleCurrent(item?.current_uA);
    node.style.background = colorFor(value, min, max);
    node.classList.toggle("active", node.dataset.key === state.heatmapActiveKey);
    node.classList.toggle("invalid", Boolean(item && !item.ok));
    const measuredAt = item?.measurementTime ? new Date(item.measurementTime * 1000).toLocaleString() : "time unknown";
    node.title = item
      ? `${formatCell(item.cellAddress)} ${formatScaledCurrent(value)} uS (${item.measurementMode || "read"})\n${measuredAt}\nFeedback: ${item.ok ? "valid" : "invalid"}\nSource: ${item.sourceRun || "unknown"}`
      : `${node.dataset.key}: no read`;
  }
  els.scaleMin.textContent = "";
  els.scaleMax.textContent = "";
}

function renderChart(summary) {
  const canvas = els.chart;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.floor(rect.width * dpr);
  canvas.height = Math.floor(rect.height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, rect.width, rect.height);
  ctx.fillStyle = "#111318";
  ctx.fillRect(0, 0, rect.width, rect.height);

  const pulses = buildPulseSeries(summary?.history || []);
  if (!pulses.length) return;

  const padLeft = 72;
  const padRight = 18;
  const padTop = 24;
  const gap = 14;
  const topH = Math.max(120, rect.height * 0.56);
  const bottomY = padTop + topH + gap;
  const bottomH = rect.height - bottomY - 28;
  const plotW = rect.width - padLeft - padRight;
  const xFor = (index) => padLeft + (plotW * index) / Math.max(1, pulses.length - 1);

  const { min: requestedCurrentMin, max: requestedCurrentMax } = currentRange();
  // Log plots cannot represent zero or negative conductance. Keep the
  // user-selected range unchanged, but use the 0.2 uS display floor that
  // corresponds to 0.1 uA at the fixed 0.5 V read voltage.
  const currentMin = Math.max(LOG_DISPLAY_FLOOR_US, requestedCurrentMin);
  const currentMax = Math.max(requestedCurrentMax, currentMin * 10);
  const logCurrentMin = Math.log10(currentMin);
  const logCurrentSpan = Math.log10(currentMax) - logCurrentMin;
  const yCurrent = (value) => {
    const clamped = Math.max(currentMin, Math.min(currentMax, value));
    return padTop + topH - ((Math.log10(clamped) - logCurrentMin) / logCurrentSpan) * topH;
  };

  const voltageMax = Math.max(2, ...pulses.map((item) => Math.abs(item.voltage || 0)));
  const yZero = bottomY + bottomH / 2;
  const yVoltage = (value) => yZero - (value / voltageMax) * (bottomH / 2 - 6);

  drawPhaseBands(ctx, pulses, xFor, padLeft, plotW, bottomY, bottomH);
  drawAxes(ctx, padLeft, padTop, plotW, topH, bottomY, bottomH, rect.width, rect.height);
  drawLogCurrentGrid(ctx, padLeft, plotW, yCurrent, currentMin, currentMax);
  const conductanceThresholds = Object.fromEntries(
    Object.entries(summary?.thresholds_uA || {}).map(([key, value]) => [key, scaleCurrent(Number(value))])
  );
  conductanceThresholds.set = SET_TRACE_MARKER_US;
  drawThresholds(ctx, padLeft, plotW, yCurrent, currentMin, currentMax, conductanceThresholds);
  drawTransition(ctx, pulses, xFor, padTop, topH, bottomY, bottomH);
  drawCurrentTrace(ctx, pulses, xFor, yCurrent);
  drawVoltageBars(ctx, pulses, xFor, yZero, yVoltage);
  drawActiveVoltageBadge(ctx, summary?.last, padLeft, plotW, bottomY);
  drawLabels(ctx, pulses, currentMin, currentMax, voltageMax, padLeft, padTop, topH, bottomY, bottomH, rect.height);
}

function isRead(row) {
  return row?.operation === "read" || String(row?.stage || "").startsWith("read");
}

function isBeforeReadForPulse(read, pulse) {
  const match = String(read?.stage || "").match(/(?:^|_)read_before_(set|reset)$/);
  return Boolean(match && isRead(read) && read.ok !== false && pulse?.ok !== false
    && pulse?.operation === match[1] && read.cellAddress && pulse.cellAddress
    && cellKey(read.cellAddress) === cellKey(pulse.cellAddress));
}

function buildPulseSeries(history) {
  const pulses = [];
  let pending = null;
  const rows = history.filter((row) => row?.cellAddress);
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    // Match the old pulse + after-read spacing. A paired before-read is
    // metadata for the upcoming pulse, not another X-axis position. Keep an
    // unpaired/latest before-read visible until that pulse actually exists.
    if (isBeforeReadForPulse(row, rows[index + 1])) continue;
    if (!isRead(row)) {
      pending = {
        cell: row.cellAddress,
        op: row.operation,
        packet: row.packet,
        voltage: signedVoltage(row),
        wl: row.vcc_wl_set_V,
        vcc: row.vcc_set_V,
        current: null,
        beforeCurrent: isBeforeReadForPulse(rows[index - 1], row)
          ? scaleCurrent(rows[index - 1].current_uA) : null,
      };
      pulses.push(pending);
      continue;
    }
    if (pending && pending.current === null
      && cellKey(pending.cell) === cellKey(row.cellAddress)
      && !/(?:^|_)read_before_(set|reset)$/.test(String(row.stage || ""))) {
      pending.current = scaleCurrent(row.current_uA);
      pending.readStage = row.stage;
    } else {
      pulses.push({
        cell: row.cellAddress,
        op: "read",
        packet: row.packet,
        voltage: 0,
        current: scaleCurrent(row.current_uA),
      });
    }
  }
  return pulses.slice(-80);
}

function signedVoltage(row) {
  const value = Number.isFinite(row.vcc_wl_set_V) ? row.vcc_wl_set_V : row.vcc_set_V;
  if (!Number.isFinite(value)) return 0;
  return row.operation === "set" ? -value : value;
}

function drawAxes(ctx, left, top, width, topH, bottomY, bottomH, totalW, totalH) {
  ctx.strokeStyle = "#d9dee6";
  ctx.lineWidth = 1.2;
  ctx.strokeRect(left, top, width, topH);
  ctx.strokeRect(left, bottomY, width, bottomH);
  ctx.strokeStyle = "#8b949e";
  ctx.beginPath();
  ctx.moveTo(left, bottomY + bottomH / 2);
  ctx.lineTo(left + width, bottomY + bottomH / 2);
  ctx.stroke();
  ctx.fillStyle = "#d9dee6";
  ctx.font = "12px system-ui";
  ctx.save();
  ctx.translate(14, top + topH / 2 + 34);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("G (µS, log)", 0, 0);
  ctx.restore();
  ctx.save();
  ctx.translate(14, bottomY + bottomH / 2 + 28);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("Voltage (V)", 0, 0);
  ctx.restore();
  ctx.fillText("Pulse", totalW / 2 - 12, totalH - 8);
}

function drawLogCurrentGrid(ctx, left, width, yCurrent, currentMin, currentMax) {
  const firstDecade = Math.floor(Math.log10(currentMin));
  const lastDecade = Math.ceil(Math.log10(currentMax));
  const ticks = [];
  for (let decade = firstDecade; decade <= lastDecade; decade += 1) {
    const base = 10 ** decade;
    for (const multiplier of [1, 2, 5]) {
      const value = multiplier * base;
      if (value >= currentMin - 1e-12 && value <= currentMax + 1e-12) {
        ticks.push({ value, major: multiplier === 1 });
      }
    }
  }

  ctx.save();
  ctx.font = "11px system-ui";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (const tick of ticks) {
    const y = yCurrent(tick.value);
    ctx.strokeStyle = tick.major ? "rgba(154, 164, 175, 0.34)" : "rgba(154, 164, 175, 0.16)";
    ctx.lineWidth = tick.major ? 1 : 0.7;
    ctx.setLineDash(tick.major ? [] : [2, 4]);
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + width, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = tick.major ? "#d7dde3" : "#9aa4af";
    const label = tick.value < 1 ? tick.value.toFixed(1) : Number(tick.value.toPrecision(3)).toString();
    ctx.fillText(label, left - 8, y);
  }
  ctx.restore();
}

function drawThresholds(ctx, left, width, yCurrent, currentMin, currentMax, thresholds) {
  const visible = [
    ["SET", Number(thresholds.set), "#ff3b4f"],
    ["RESET", Number(thresholds.reset), "#6ab6df"],
  ].filter(([, rawThreshold]) => Number.isFinite(rawThreshold));
  const usedY = [];
  visible.forEach(([label, rawThreshold, color]) => {
    const threshold = rawThreshold;
    if (threshold < currentMin || threshold > currentMax) return;
    const y = yCurrent(threshold);
    let labelY = y - 7;
    for (const prevY of usedY) {
      if (Math.abs(labelY - prevY) < 16) labelY = prevY + 16;
    }
    usedY.push(labelY);
    ctx.strokeStyle = color;
    ctx.setLineDash([6, 5]);
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + width, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = "12px system-ui";
    const text = `${label} ${threshold.toFixed(2)} uS`;
    const textW = ctx.measureText(text).width;
    const textX = left + width - textW - 8;
    ctx.fillStyle = "rgba(17, 19, 24, 0.88)";
    ctx.fillRect(textX - 4, labelY - 11, textW + 8, 15);
    ctx.fillStyle = color;
    ctx.fillText(text, textX, labelY);
  });
}

function drawTransition(ctx, pulses, xFor, top, topH, bottomY, bottomH) {
  const index = pulses.findIndex((item, i) => item.op === "reset" && pulses.slice(0, i).some((prev) => prev.op === "set"));
  if (index < 0) return;
  const x = xFor(index);
  ctx.strokeStyle = "#d9dee6";
  ctx.setLineDash([6, 5]);
  ctx.beginPath();
  ctx.moveTo(x, top);
  ctx.lineTo(x, bottomY + bottomH);
  ctx.stroke();
  ctx.setLineDash([]);
}

function drawPhaseBands(ctx, pulses, xFor, left, width, bottomY, bottomH) {
  const bands = [];
  let start = null;
  let op = null;
  pulses.forEach((item, index) => {
    const nextOp = item.op === "set" || item.op === "reset" ? item.op : op;
    if (nextOp !== op) {
      if (op && start !== null) bands.push({ op, start, end: index - 1 });
      op = nextOp;
      start = index;
    }
  });
  if (op && start !== null) bands.push({ op, start, end: pulses.length - 1 });

  bands.forEach((band) => {
    const x1 = band.start <= 0 ? left : xFor(band.start) - 4;
    const x2 = band.end >= pulses.length - 1 ? left + width : xFor(band.end) + 4;
    ctx.fillStyle = band.op === "set" ? "rgba(255, 59, 79, 0.08)" : "rgba(106, 182, 223, 0.1)";
    ctx.fillRect(x1, bottomY, Math.max(2, x2 - x1), bottomH);
    ctx.fillStyle = band.op === "set" ? "#ff6c79" : "#8bcced";
    ctx.font = "600 11px system-ui";
    ctx.fillText(band.op.toUpperCase(), x1 + 6, bottomY + 14);
  });
}

function drawCurrentTrace(ctx, pulses, xFor, yCurrent) {
  const points = pulses
    .map((item, index) => ({ ...item, index }))
    .filter((item) => Number.isFinite(item.current));
  ctx.strokeStyle = "#4fb3ad";
  ctx.lineWidth = 1.7;
  ctx.beginPath();
  points.forEach((item, i) => {
    const x = xFor(item.index);
    const y = yCurrent(item.current);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  for (const item of points) {
    const x = xFor(item.index);
    const y = yCurrent(item.current);
    ctx.fillStyle = "#111318";
    ctx.strokeStyle = "#6bd2cc";
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }
}

function drawVoltageBars(ctx, pulses, xFor, yZero, yVoltage) {
  const barW = Math.max(2, Math.min(8, 460 / Math.max(20, pulses.length)));
  pulses.forEach((item, index) => {
    if (!Number.isFinite(item.voltage) || item.voltage === 0) return;
    const x = xFor(index);
    const y = yVoltage(item.voltage);
    ctx.strokeStyle = item.op === "set" ? "#ff3b4f" : "#6ab6df";
    ctx.lineWidth = barW;
    ctx.beginPath();
    ctx.moveTo(x, yZero);
    ctx.lineTo(x, y);
    ctx.stroke();
  });
}

function drawActiveVoltageBadge(ctx, row, left, width, bottomY) {
  if (!row || !(row.operation === "set" || row.operation === "reset")) return;
  const op = row.operation.toUpperCase();
  const color = row.operation === "set" ? "#ff6c79" : "#8bcced";
  const text = `${op}  Vcc ${formatVoltage(row.vcc_set_V)}V  WL ${formatVoltage(row.vcc_wl_set_V)}V`;
  ctx.font = "600 12px system-ui";
  const textW = ctx.measureText(text).width;
  const x = left + width - textW - 10;
  const y = bottomY + 17;
  ctx.fillStyle = "rgba(17, 19, 24, 0.9)";
  ctx.fillRect(x - 6, y - 13, textW + 12, 18);
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
}

function drawLabels(ctx, pulses, currentMin, currentMax, voltageMax, left, top, topH, bottomY, bottomH, totalH) {
  const cell = pulses.find((item) => item.cell)?.cell;
  ctx.fillStyle = "#f3f5f7";
  ctx.font = "600 13px system-ui";
  ctx.fillText(cell ? `Cell (${cell.row},${cell.col})` : "Cell", left, 16);
  ctx.font = "12px system-ui";
  ctx.fillStyle = "#9aa4af";
  ctx.textAlign = "right";
  ctx.fillText(`+${voltageMax.toFixed(2)}V`, left - 8, bottomY + 11);
  ctx.fillText("0.00V", left - 8, bottomY + bottomH / 2 + 4);
  ctx.fillText(`-${voltageMax.toFixed(2)}V`, left - 8, bottomY + bottomH - 4);
  ctx.textAlign = "left";
  ctx.fillStyle = "#ff3b4f";
  ctx.fillRect(left + 250, totalH - 42, 28, 5);
  ctx.fillText("SET", left + 286, totalH - 36);
  ctx.fillStyle = "#6ab6df";
  ctx.fillRect(left + 340, totalH - 42, 28, 5);
  ctx.fillText("RESET", left + 376, totalH - 36);
}

let refreshInFlight = false;

function mergeCharacterizationFeed(data, feed, nowSeconds = Date.now() / 1000) {
  if (!feed?.state?.characterization) return data;
  const info = feed.state.characterization;
  const feedRun = feed.state.run;
  const age = Math.max(0, nowSeconds - feed.published);
  const active = info.status === 'running';
  const fresh = age < 20;
  const choices = data.runs || [];
  if (!choices.some(run => run.id === feedRun.id)) choices.unshift(feedRun);
  data.runs = choices;
  // Native server data takes precedence once that server supports this run.
  if (!data.state?.characterization &&
      (state.manualRun ? state.selectedRun === feedRun.id : feedRun.updated >= (data.state?.run?.updated || 0))) {
    data.state = feed.state;
    if (active && !fresh) {
      info.ageSeconds = Math.max(info.ageSeconds, age);
      info.error = 'Live GUI feed is stale. Last saved data shown; check the experiment process.';
    }
  }
  if (active && !(data.runningCommands || []).some(c => c.operation === 'characterization')) {
    data.runningCommands = [...(data.runningCommands || []), {
      id: `characterization-${feedRun.id}`, running: true, canKill: false, external: true,
      operation: 'characterization', row: info.cell[0], col: info.cell[1], runDir: feedRun.path,
    }];
  }
  return data;
}

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  const query = state.manualRun && state.selectedRun ? `?run=${encodeURIComponent(state.selectedRun)}` : "";
  try {
    const response = await fetch(`/api/state${query}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`GUI state request failed (${response.status})`);
    const data = await response.json();
    // Optional read-only feed lets an existing server show the pilot without a restart.
    try {
      const live = await fetch('/characterization_live.json', { cache: 'no-store' });
      if (live.ok) mergeCharacterizationFeed(data, await live.json());
    } catch (_) { /* Ordinary GUI operation does not depend on this optional feed. */ }
    state.lastSummary = data.state || null;
    state.currentRunId = data.state?.run?.id || "";
    state.arrayResume = data.state?.arrayResume || null;
    state.sweepResume = data.state?.sweepResume || null;
    renderRuns(data.runs || [], data.state?.run?.id || "");
    state.commandsEnabled = Boolean(data.commandsEnabled);
    state.lastCommands = data.runningCommands || [];
    renderCommandState(data.runningCommands || []);
    renderMetrics(data.state);
    renderHeatmap(data.state);
    renderChart(data.state);
    // Keep experiment errors in the original status area, without an extra panel.
    if (data.state?.characterization?.error) {
      els.commandNote.textContent = data.state.characterization.error;
    }
  } catch (error) {
    els.commandNote.textContent = `GUI refresh error: ${error.message}`;
  } finally {
    refreshInFlight = false;
  }
}

function renderCommandState(commands) {
  const running = commands.find((command) => command.running);
  state.runningCommandId = running?.id || "";
  els.commandBtn.disabled = !state.commandsEnabled || Boolean(running);
  const canContinueSelectedSweep = !running
    && state.manualRun
    && ["set", "reset"].includes(els.operationInput.value)
    && state.sweepResume?.operation === els.operationInput.value
    && Boolean(state.sweepResume?.canResume);
  els.commandBtn.textContent = running ? "Processing..." : canContinueSelectedSweep ? "Continue" : "Start";
  els.killBtn.disabled = !running || !running.canKill;
  if (!state.commandsEnabled) {
    els.commandNote.textContent = "--allow-commands";
  } else if (running) {
    if (running.operation && [...els.operationInput.options].some((option) => option.value === running.operation)) {
      els.operationInput.value = running.operation;
    }
    syncOperationFields();
    if (Number.isFinite(running.row)) els.rowInput.value = running.row;
    if (Number.isFinite(running.col)) els.colInput.value = running.col;
    if (running.wbValue) els.wbValueInput.value = running.wbValue;
    const target = running.operation === "burst-read"
      ? "full array burst"
      : running.operation === "read-array"
      ? `array from col ${running.colStart ?? running.col ?? 0}`
      :
      Number.isFinite(running.row) ? `r${running.row} c${running.col ?? 0}` : "API";
    const source = running.external ? "external " : "";
    els.commandNote.textContent = `Processing ${source}${running.operation} ${target}`;
  } else {
    const showArrayResume = els.operationInput.value === "read-array" && Boolean(state.arrayResume?.canResume);
    const showSweepResume = ["set", "reset"].includes(els.operationInput.value)
      && state.sweepResume?.operation === els.operationInput.value
      && Boolean(state.sweepResume?.canResume);
    els.commandNote.textContent = showArrayResume
      ? `Ready. Suggested resume column ${state.arrayResume.colStart}.`
      : showSweepResume
        ? `Ready. Continue ${state.sweepResume.operation} after ${state.sweepResume.completedPulses} completed pulses.`
        : "Ready";
  }
  const showArrayResume = els.operationInput.value === "read-array" && Boolean(state.arrayResume?.canResume);
  const showSweepResume = ["set", "reset"].includes(els.operationInput.value)
    && state.sweepResume?.operation === els.operationInput.value
    && Boolean(state.sweepResume?.canResume);
  const showResume = showArrayResume || showSweepResume;
  els.resumeColField.hidden = !showArrayResume;
  els.resumeColInput.disabled = !showArrayResume;
  if (showArrayResume && document.activeElement !== els.resumeColInput) {
    els.resumeColInput.value = String(state.arrayResume.colStart);
  }
  els.resumeArrayBtn.hidden = !showResume;
  els.resumeArrayBtn.disabled = !state.commandsEnabled || Boolean(running) || !showResume;
  els.resumeArrayBtn.textContent = showArrayResume ? "Resume array" : `Resume ${state.sweepResume?.operation || "sweep"}`;
}

async function sendCommand(payload, targetText, extraText = "") {
  if (!payload.dryRun) {
    const ok = window.confirm(
      `Send ${payload.operation.toUpperCase()} to hardware for ${targetText}?\n\n` +
      "This will run the API against the connected bench." + extraText
    );
    if (!ok) {
      els.commandNote.textContent = "Cancelled";
      return;
    }
    payload.confirmHardware = true;
  }
  const response = await fetch("/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  els.commandNote.textContent = data.error || `Started ${payload.operation} in ${data.runDir}`;
  if (!data.error) {
    state.runningCommandId = data.id;
    // A newly started command must become the visible run. Otherwise a user
    // who was inspecting an older failed run keeps seeing its stale API error
    // even while the new hardware operation succeeds.
    state.manualRun = false;
    state.selectedRun = "";
    renderFollowMode();
  }
  setTimeout(refresh, 900);
}

function syncOperationFields() {
  const operation = els.operationInput.value;
  const wishbone = operation === "wb-read" || operation === "wb-write";
  els.rowField.hidden = wishbone;
  els.colField.hidden = wishbone;
  els.rowInput.disabled = wishbone;
  els.colInput.disabled = wishbone;
  els.wbValueField.hidden = !wishbone;
  els.wbValueInput.disabled = !wishbone;
  if (wishbone) {
    const nextDefault = operation === "wb-read" ? DEFAULT_WB_READ_VALUE : DEFAULT_WB_WRITE_VALUE;
    const staleDefault = operation === "wb-read" ? DEFAULT_WB_WRITE_VALUE : DEFAULT_WB_READ_VALUE;
    const current = els.wbValueInput.value.trim().toUpperCase();
    if (!current || current === staleDefault.toUpperCase()) {
      els.wbValueInput.value = nextDefault;
    }
  }
}

els.runSelect.addEventListener("change", () => {
  state.manualRun = true;
  state.selectedRun = els.runSelect.value;
  refresh();
});
els.followBtn.addEventListener("click", () => {
  state.manualRun = !state.manualRun;
  if (!state.manualRun) state.selectedRun = "";
  renderFollowMode();
  refresh();
});
els.killBtn.addEventListener("click", async () => {
  if (!state.runningCommandId) return;
  const external = state.runningCommandId.startsWith("pid-");
  const ok = window.confirm(
    external
      ? `Kill external API process ${state.runningCommandId}?\n\nOnly do this if you are sure this scan_debug_cli.py run should stop.`
      : "Kill the GUI-started command that is processing now?"
  );
  if (!ok) return;
  const response = await fetch("/api/command/kill", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: state.runningCommandId }),
  });
  const data = await response.json();
  els.commandNote.textContent = data.error || data.message || "Kill signal sent";
  setTimeout(refresh, 500);
});
els.operationInput.addEventListener("change", () => {
  syncOperationFields();
  renderCommandState(state.lastCommands || []);
});
for (const eventName of ["input", "change"]) {
  els.currentMinRange.addEventListener(eventName, () => setCurrentRange("min", els.currentMinRange.value));
  els.currentMinInput.addEventListener(eventName, () => setCurrentRange("min", els.currentMinInput.value));
  els.currentMaxRange.addEventListener(eventName, () => setCurrentRange("max", els.currentMaxRange.value));
  els.currentMaxInput.addEventListener(eventName, () => setCurrentRange("max", els.currentMaxInput.value));
}
els.resumeArrayBtn.addEventListener("click", async () => {
  if (!state.currentRunId) return;
  if (els.operationInput.value === "read-array" && state.arrayResume?.canResume) {
    const requestedCol = Number(els.resumeColInput.value);
    const resumeCol = Number.isFinite(requestedCol) ? Math.max(0, Math.min(31, requestedCol)) : state.arrayResume.colStart;
    await sendCommand(
      {
        operation: "read-array",
        resumeRun: state.currentRunId,
        resumeCol,
        zynqPassword: els.zynqPasswordInput.value,
        dryRun: els.dryRunInput.checked,
      },
      `remaining array columns starting at column ${resumeCol}`,
      "\n\nThis will append to the selected unfinished run."
    );
    return;
  }
  if (["set", "reset"].includes(els.operationInput.value) && state.sweepResume?.canResume) {
    await sendCommand(
      {
        operation: els.operationInput.value,
        resumeRun: state.currentRunId,
        row: state.sweepResume.row,
        col: state.sweepResume.col,
        zynqPassword: els.zynqPasswordInput.value,
        dryRun: els.dryRunInput.checked,
      },
      `row ${state.sweepResume.row}, col ${state.sweepResume.col}`,
      "\n\nThis will append to the selected unfinished sweep and skip completed pulse voltages."
    );
  }
});
els.commandForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    operation: els.operationInput.value,
    row: Number(els.rowInput.value),
    col: Number(els.colInput.value),
    zynqPassword: els.zynqPasswordInput.value,
    dryRun: els.dryRunInput.checked,
  };
  if (["wb-read", "wb-write"].includes(payload.operation)) payload.wbValue = els.wbValueInput.value.trim();
  const continueSelectedSweep = state.manualRun
    && ["set", "reset"].includes(payload.operation)
    && state.sweepResume?.operation === payload.operation
    && Boolean(state.sweepResume?.canResume);
  if (continueSelectedSweep) {
    payload.resumeRun = state.currentRunId;
    payload.row = state.sweepResume.row;
    payload.col = state.sweepResume.col;
  }
  const target = payload.operation === "wb-read"
    ? `WB read command ${payload.wbValue || DEFAULT_WB_READ_VALUE} at 0x30000004`
    : payload.operation === "wb-write"
    ? `0x30000004 with ${payload.wbValue || DEFAULT_WB_WRITE_VALUE}`
    : payload.operation === "burst-read"
    ? "the full 32x32 array in one burst"
    : payload.operation === "read-array" ? `the array starting at column ${payload.col}` : `row ${payload.row}, col ${payload.col}`;
  const extra = payload.operation === "wb-read" || payload.operation === "wb-write"
    ? "\n\nRequires Caravel GPIO6/UART TX wired to FPGA J10-10 and RESET wired from FPGA J10-3 to Caravel. The permanent Caravel firmware accepts the operation and 32-bit packet at runtime, so normal WB requests do not rebuild or reflash it. DL carries the checked pulse-width startup command and returns to high-impedance before Wishbone access; TM and DR stay high-impedance. The external 2 MHz clock and all existing DAC/PLL values are preserved. If the permanent image is missing, the API installs it once and retries."
    : payload.operation === "burst-read"
    ? "\n\nThis uses one FPGA full-array stream and one reduced-resolution Saleae capture. FPGA reset/scan/hold timing and the ScanInDR-rise-through-TM-fall measurement window match single-cell read timing."
    : payload.operation === "read-array"
    ? "\n\nThis will read columns from the selected start column through column 31."
    : continueSelectedSweep
      ? "\n\nThis will append to the selected run and skip pulse voltages already completed in its manifest."
      : "";
  await sendCommand(payload, target, extra);
});

ensureGrid();
syncOperationFields();
syncCurrentRangeControls();
refresh();
setInterval(refresh, 2500);
