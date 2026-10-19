const studyDefaults = {
  ma21: 21,
  ma65: 65,
  ema5: 5,
  ema9: 9,
  ema10: 10,
  bbLength: 20,
  bbMult: 2,
  stLength: 14,
  stFactor: 4,
  volumeLength: 20,
  rsiLength: 14,
  macdFast: 12,
  macdSlow: 26,
  macdSignal: 9,
  adxSmooth: 25,
  diLength: 14,
};
const studyNames = {
  ma21: "SMA fast",
  ma65: "SMA slow",
  ema5: "EMA fast",
  ema9: "EMA middle",
  ema10: "EMA slow",
  bbLength: "BB length",
  bbMult: "BB deviations",
  stLength: "SuperTrend ATR",
  stFactor: "SuperTrend factor",
  volumeLength: "Volume EMA",
  rsiLength: "RSI length",
  macdFast: "MACD fast",
  macdSlow: "MACD slow",
  macdSignal: "MACD signal",
  adxSmooth: "ADX smoothing",
  diLength: "ADX DI length",
};
const studyColors = {
  ma21: "#f45c6b",
  ma65: "#199c91",
  ema5: "#f6e778",
  ema9: "#7490ff",
  ema10: "#b6dcac",
  bb: "#e7d34b",
  st: "#43bb7f",
  volume: "#38b9a5",
  rsi: "#ae87e9",
  macd: "#57b5ff",
  adx: "#f1727b",
};
let studyConfig = { ...studyDefaults },
  studyVisible = Object.fromEntries(
    Object.keys(studyColors).map((k) => [k, true]),
  );
try {
  const p = JSON.parse(localStorage.getItem("paperkite-studies-v1"));
  if (p) {
    for (const k of Object.keys(studyDefaults)) {
      const v = p.config?.[k];
      if (
        Number.isFinite(v) &&
        v >= 1 &&
        v <= 300 &&
        (k === "stFactor" || k === "bbMult" || Number.isInteger(v))
      )
        studyConfig[k] = v;
    }
    for (const k of Object.keys(studyVisible))
      if (typeof p.visible?.[k] === "boolean") studyVisible[k] = p.visible[k];
  }
} catch {}
function openStudies() {
  document.getElementById("studyFields").innerHTML = Object.entries(studyConfig)
    .map(
      ([k, v]) =>
        `<div><label for="study-${k}">${studyNames[k]}</label><input id="study-${k}" type="number" value="${v}" min="1" max="300" step="${k === "stFactor" || k === "bbMult" ? ".1" : "1"}"></div>`,
    )
    .join("");
  document.getElementById("studySwitches").innerHTML = Object.keys(studyVisible)
    .map(
      (k) =>
        `<label style="display:inline-flex;align-items:center;gap:6px;margin:8px"><input type="checkbox" id="show-${k}" ${studyVisible[k] ? "checked" : ""} style="width:auto">${k === "st" ? "SuperTrend" : k === "bb" ? "Bollinger Bands" : k === "volume" ? "Volume + EMA" : k.toUpperCase()}</label>`,
    )
    .join("");
  document.getElementById("studyError").textContent = "";
  document.getElementById("studies").showModal();
}
function saveStudies(reset = false) {
  const next = { ...studyDefaults };
  if (!reset)
    for (const k of Object.keys(next)) {
      let n = +document.getElementById("study-" + k).value;
      if (
        !Number.isFinite(n) ||
        n < 1 ||
        n > 300 ||
        (!["stFactor", "bbMult"].includes(k) && !Number.isInteger(n))
      ) {
        document.getElementById("studyError").textContent =
          "Use whole periods 1–300 and positive factors 1–300.";
        return;
      }
      next[k] = n;
    }
  if (next.macdFast >= next.macdSlow) {
    document.getElementById("studyError").textContent =
      "MACD fast length must be less than slow length.";
    return;
  }
  studyConfig = next;
  for (const k of Object.keys(studyVisible))
    studyVisible[k] = reset || document.getElementById("show-" + k).checked;
  try {
    localStorage.setItem(
      "paperkite-studies-v1",
      JSON.stringify({ config: studyConfig, visible: studyVisible }),
    );
  } catch {}
  document.getElementById("studies").close();
  draw();
}
function draw() {
  const w = canvas.clientWidth,
    h = 340,
    dpr = window.devicePixelRatio || 1;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  const panels = ["volume", "rsi", "macd", "adx"];
  for (const key of panels)
    document.getElementById(key + "Pane").hidden = !studyVisible[key];
  if (!assets.length) return;
  const a = assets[selected],
    all = a.interval === document.getElementById("tf").value ? a.candles : [];
  if (!all.length) {
    ctx.fillStyle = "#8da1b7";
    ctx.fillText("Waiting for timeframe candles…", 20, 30);
    document.getElementById("studyLegend").textContent =
      "Indicators appear when candle history loads.";
    for (const k of panels)
      document.getElementById(k + "Value").textContent = "Waiting for history";
    return;
  }
  const I = PaperIndicators,
    results = I.calculate(all, studyConfig),
    count = +document.getElementById("barCount").value,
    start = Math.max(0, all.length - count),
    cs = all.slice(start),
    cw = (w - 75) / cs.length;
  const cut = (s) => s.slice(start),
    last = (s) => {
      const v = s.at(-1);
      return I.valid(v) ? v.toFixed(2) : "warming up";
    },
    labels = {
      ma21: "SMA " + studyConfig.ma21,
      ma65: "SMA " + studyConfig.ma65,
      ema5: "EMA " + studyConfig.ema5,
      ema9: "EMA " + studyConfig.ema9,
      ema10: "EMA " + studyConfig.ema10,
      bb: `BB ${studyConfig.bbLength}, ${studyConfig.bbMult}`,
      st: `SuperTrend ${studyConfig.stLength}, ${studyConfig.stFactor}`,
    };
  let overlays = [];
  for (const k of ["ma21", "ma65", "ema5", "ema9", "ema10"])
    if (studyVisible[k]) overlays.push([k, results[k]]);
  if (studyVisible.bb)
    for (const s of ["upper", "mid", "lower"])
      overlays.push(["bb", results.bb[s]]);
  if (studyVisible.st) overlays.push(["st", results.st.value]);
  let vals = cs
    .flatMap((c) => [c.h, c.l])
    .concat(
      overlays.flatMap(([, s]) => cut(s).filter(I.valid)),
      ["entry", "stop", "target"]
        .map((id) => +document.getElementById(id).value)
        .filter((v) => v > 0),
    );
  const lo = Math.min(...vals),
    hi = Math.max(...vals),
    pad = (hi - lo) * 0.12 || 1;
  range = { min: lo - pad, max: hi + pad };
  const y = (p) =>
      h - 25 - ((p - range.min) / (range.max - range.min)) * (h - 45),
    x = (i) => i * cw + cw / 2;
  ctx.font = "11px system-ui";
  for (let i = 0; i < 6; i++) {
    let p = range.min + ((range.max - range.min) * i) / 5,
      yy = y(p);
    ctx.strokeStyle = "#23303d";
    ctx.beginPath();
    ctx.moveTo(0, yy);
    ctx.lineTo(w - 65, yy);
    ctx.stroke();
    ctx.fillStyle = "#8291a3";
    ctx.fillText(p.toFixed(2), w - 60, yy + 4);
  }
  function line(c, s, color, Y, width = 1.3, breaks = null) {
    c.strokeStyle = color;
    c.lineWidth = width;
    c.beginPath();
    let connected = false;
    cut(s).forEach((v, i) => {
      if (!I.valid(v)) {
        connected = false;
        return;
      }
      if (breaks && i && breaks[start + i] !== breaks[start + i - 1])
        connected = false;
      if (connected) c.lineTo(x(i), Y(v));
      else c.moveTo(x(i), Y(v));
      connected = true;
    });
    c.stroke();
    c.lineWidth = 1;
  }
  for (const [key, s] of overlays) {
    if (key === "st") {
      for (const direction of [1, -1])
        line(
          ctx,
          s.map((v, i) => (results.st.direction[i] === direction ? v : null)),
          direction === 1 ? "#3bbd76" : "#c65359",
          y,
          2.5,
          results.st.direction,
        );
    } else line(ctx, s, studyColors[key], y, key === "bb" ? 1 : 1.4);
  }
  cs.forEach((c, i) => {
    ctx.fillStyle = ctx.strokeStyle = c.c >= c.o ? "#50c5ac" : "#e87987";
    ctx.beginPath();
    ctx.moveTo(x(i), y(c.h));
    ctx.lineTo(x(i), y(c.l));
    ctx.stroke();
    ctx.fillRect(
      x(i) - cw * 0.28,
      Math.min(y(c.o), y(c.c)),
      Math.max(1, cw * 0.56),
      Math.max(1, Math.abs(y(c.o) - y(c.c))),
    );
    if (i % Math.max(1, Math.floor(cs.length / 4)) === 0) {
      ctx.fillStyle = "#8291a3";
      ctx.fillText(
        document.getElementById("tf").value === "day"
          ? new Date(c.time * 1000).toLocaleDateString("en-IN", {
              timeZone: "Asia/Kolkata",
              day: "2-digit",
              month: "short",
            })
          : new Date(c.time * 1000).toLocaleTimeString("en-IN", {
              timeZone: "Asia/Kolkata",
              hour: "2-digit",
              minute: "2-digit",
            }),
        Math.max(0, x(i) - 15),
        h - 3,
      );
    }
  });
  for (const [id, color] of [
    ["entry", "#7eafff"],
    ["stop", "#ed8391"],
    ["target", "#55d2b0"],
  ]) {
    const p = +document.getElementById(id).value;
    if (!p) continue;
    ctx.strokeStyle = ctx.fillStyle = color;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(0, y(p));
    ctx.lineTo(w - 65, y(p));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillText(id.toUpperCase() + " " + p.toFixed(2), 8, y(p) - 6);
  }
  const c = cs.at(-1);
  document.getElementById("ohlc").textContent =
    `O ${c.o.toFixed(2)}  H ${c.h.toFixed(2)}  L ${c.l.toFixed(2)}  C ${c.c.toFixed(2)} · ${all.length} candles loaded`;
  document.getElementById("studyLegend").innerHTML = Object.keys(labels)
    .filter((k) => studyVisible[k])
    .map(
      (k) =>
        `<span style="color:${studyColors[k]}">${labels[k]} ${k === "bb" ? last(results.bb.upper) + " / " + last(results.bb.lower) : last(k === "st" ? results.st.value : results[k])}</span>`,
    )
    .join(" · ");
  function pane(key, series, min, max, refs = []) {
    const el = document.getElementById(key + "Chart"),
      p = el.getContext("2d"),
      height = 105;
    el.width = w * dpr;
    el.height = height * dpr;
    p.scale(dpr, dpr);
    p.clearRect(0, 0, w, height);
    const Y = (v) =>
      height - 9 - ((v - min) / (max - min || 1)) * (height - 20);
    p.font = "10px system-ui";
    for (const ref of refs) {
      p.strokeStyle = "#415063";
      p.setLineDash([3, 4]);
      p.beginPath();
      p.moveTo(0, Y(ref));
      p.lineTo(w - 65, Y(ref));
      p.stroke();
      p.setLineDash([]);
      p.fillStyle = "#8291a3";
      p.fillText(ref.toFixed(0), w - 55, Y(ref) + 3);
    }
    for (const [s, color] of series) line(p, s, color, Y);
    return { p, Y, height };
  }
  if (studyVisible.volume) {
    const v = cut(results.volume).filter(I.valid),
      max = Math.max(1, ...v) * 1.2,
      { p, Y } = pane("volume", [[results.volumeEMA, "#6ba9ff"]], 0, max);
    cut(results.volume).forEach((v, i) => {
      if (!I.valid(v)) return;
      p.fillStyle = cs[i].c >= cs[i].o ? "#287d6d" : "#a94b58";
      p.fillRect(x(i) - cw * 0.3, Y(v), Math.max(1, cw * 0.6), Y(0) - Y(v));
    });
    line(p, results.volumeEMA, "#6ba9ff", Y);
    document.getElementById("volumeValue").textContent = v.length
      ? `Volume ${last(results.volume)} · EMA ${studyConfig.volumeLength}: ${last(results.volumeEMA)}`
      : "Volume unavailable for these candles";
  }
  if (studyVisible.rsi) {
    pane("rsi", [[results.rsi, studyColors.rsi]], 0, 100, [30, 50, 70]);
    document.getElementById("rsiValue").textContent =
      `RSI ${studyConfig.rsiLength}: ${last(results.rsi)}`;
  }
  if (studyVisible.macd) {
    const m = results.macd,
      v = [...cut(m.line), ...cut(m.signal), ...cut(m.hist)].filter(I.valid),
      lo = Math.min(0, ...v),
      hi = Math.max(0, ...v),
      pad = (hi - lo) * 0.1 || 1,
      { p, Y } = pane("macd", [], lo - pad, hi + pad, [0]);
    cut(m.hist).forEach((v, i) => {
      if (!I.valid(v)) return;
      p.fillStyle = v >= 0 ? "#55bca7" : "#db7280";
      p.fillRect(
        x(i) - cw * 0.3,
        Math.min(Y(v), Y(0)),
        Math.max(1, cw * 0.6),
        Math.max(1, Math.abs(Y(v) - Y(0))),
      );
    });
    line(p, m.line, "#57b5ff", Y);
    line(p, m.signal, "#f5a15d", Y);
    document.getElementById("macdValue").textContent =
      `MACD ${studyConfig.macdFast}/${studyConfig.macdSlow}/${studyConfig.macdSignal}: ${last(m.line)} · Signal ${last(m.signal)} · Histogram ${last(m.hist)}`;
  }
  if (studyVisible.adx) {
    pane(
      "adx",
      [[results.adx, studyColors.adx]],
      0,
      Math.max(60, ...cut(results.adx).filter(I.valid)) * 1.1,
      [20, 25],
    );
    document.getElementById("adxValue").textContent =
      `ADX smoothing ${studyConfig.adxSmooth} / DI ${studyConfig.diLength}: ${last(results.adx)}`;
  }
}
