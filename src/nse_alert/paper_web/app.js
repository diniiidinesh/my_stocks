const $ = (id) => document.getElementById(id),
  money = (n) =>
    new Intl.NumberFormat("en-IN", {
      style: "currency",
      currency: "INR",
      maximumFractionDigits: 2,
    }).format(n),
  settings = $("settings");
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
let editingId = null;
let state,
  assets = [],
  selected = 0,
  mode = "entry",
  drag = null,
  csrf = "",
  range = { min: 0, max: 1 },
  chartSymbol = "",
  drawTimer,
  chartBusy = false,
  pollBusy = false,
  chartError = "";
const canvas = $("chart"),
  ctx = canvas.getContext("2d");
function toast(s) {
  $("toast").textContent = s;
}
async function api(path, body) {
  const r = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body
      ? { "Content-Type": "application/json", "X-Paper-CSRF": csrf }
      : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = await r.json();
  if (!r.ok) throw Error(data.error || "Request failed");
  return data;
}
function defaultStop() {
  $("stop").value = (+$("entry").value * 0.97).toFixed(2);
  draw();
  risk();
  saveDrawing();
}
function choose(i) {
  cancelEdit(false);
  selected = i;
  const a = assets[i],
    d = state.drawings[a.symbol];
  $("entry").value = (d?.entry || a.price).toFixed(2);
  $("stop").value = (d?.stop || a.price * 0.97).toFixed(2);
  $("target").value = (d?.target || a.price * 1.06).toFixed(2);
  chartError = "";
  render();
  loadChart();
}
function risk() {
  const e = +$("entry").value,
    s = +$("stop").value,
    t = +$("target").value,
    q = +$("qty").value;
  $("risk").textContent =
    `Planned risk ${money(Math.max(0, e - s) * q)} · Reward ${money(Math.max(0, t - e) * q)} · Costs extra`;
}
function render() {
  if (!state || !assets.length) return;
  const a = assets[selected],
    query = $("search").value.toUpperCase();
  $("watch").innerHTML = assets
    .map((x, i) => [x, i])
    .filter(([x]) => x.symbol.includes(query))
    .map(
      ([x, i]) =>
        `<div class="stock ${i === selected ? "active" : ""}" onclick="choose(${i})"><div>${esc(x.symbol)}<small>NSE · ${x.fresh ? "Fresh" : "Awaiting quote"}</small></div><div>${x.price.toFixed(2)}<small class="${x.price >= x.base ? "green" : "red"}">${x.base ? ((x.price / x.base - 1) * 100).toFixed(2) : "—"}%</small></div></div>`,
    )
    .join("");
  $("symbol").textContent = a.symbol + " · NSE";
  $("quote").textContent =
    money(a.price) +
    " · " +
    (a.updated
      ? new Date(a.updated * 1000).toLocaleTimeString("en-IN", {
          timeZone: "Asia/Kolkata",
        }) + " IST"
      : "Waiting for stream") +
    (chartError ? " · " + chartError : "");
  $("cash").textContent = money(state.cash);
  $("pnl").textContent = money(state.net);
  $("pnl").className = "value " + (state.net >= 0 ? "green" : "red");
  $("cost").textContent = money(state.paid);
  $("streak").textContent = state.streak;
  $("dots").innerHTML = Array.from(
    { length: 7 },
    (_, i) => `<span class="dot ${i < state.streak ? "on" : ""}"></span>`,
  ).join("");
  $("feedBadge").textContent =
    state.mode === "live"
      ? "LIVE PRICES · PAPER TRADES"
      : "MOCK PRICES · PAPER TRADES";
  $("connection").textContent =
    state.feed_error ||
    `${state.entries_open ? "Entries open" : "Entries closed"} · ${state.date} · Square-off ${state.cutoff} IST${state.mode === "mock" ? " · Mock streak only" : ""}`;
  $("place").disabled = !a.fresh || (!editingId && !state.entries_open);
  $("positions").innerHTML =
    state.orders
      .filter((o) => ["open", "pending"].includes(o.status))
      .map((o) => {
        const q = assets.find((x) => x.symbol === o.symbol);
        return `<tr><td>${esc(o.symbol)}</td><td>${o.exit_due ? "Exit waiting for fresh quote" : o.status}</td><td>${o.qty}</td><td>${money(o.fill || o.entry)}</td><td>${money(o.stop)} / ${money(o.target)}</td><td>${o.status === "open" && q ? money((q.price - o.fill) * o.qty) : "—"}</td><td><button onclick="act('${o.id}')">${o.status === "open" ? "Exit" : "Cancel"}</button>${o.status === "open" ? ` <button onclick="editProtection('${o.id}')">Edit levels</button>` : ""}</td></tr>`;
      })
      .join("") ||
    '<tr><td colspan="7" class="empty">Plan your next paper trade on the chart.</td></tr>';
  $("history").innerHTML =
    state.orders
      .filter((o) => !["open", "pending"].includes(o.status))
      .slice(-50)
      .reverse()
      .map(
        (o) =>
          `<tr><td>${esc(o.symbol)}</td><td>${esc(o.status)} · ${esc(o.reason)}</td><td>${o.fill ? money(o.fill) : "—"} → ${o.exit ? money(o.exit) : "—"}</td><td>${o.qty}</td><td>${o.net === undefined ? "—" : money(o.net)}</td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="5" class="empty">Closed, cancelled and rejected orders will appear here.</td></tr>';
  $("ledger").innerHTML = state.sessions
    .slice()
    .reverse()
    .map(
      (s) =>
        `<tr><td>${esc(s.date)} · ${s.finalized ? "Closed" : "Open"}${s.eligible ? "" : " · Ineligible"}</td><td>${s.closed}</td><td>${money(s.gross)}</td><td>${money(s.paid)}</td><td class="${s.net > 0 ? "green" : "red"}">${money(s.net)}</td><td>${s.streak}/7${s.entries || s.closed ? "" : " · paused"}</td></tr>`,
    )
    .join("");
  $("chargeDetails").textContent =
    "Today’s paid charges: " +
    Object.entries(state.charge_breakdown)
      .map(([k, v]) => k + " " + money(v))
      .join(" · ");
  $("alerts").textContent =
    state.alerts
      .map(
        (x) =>
          `${x.symbol} crossed ${x.threshold > 0 ? "+" : ""}${x.threshold}% at ${money(x.price)}`,
      )
      .join(" • ") || "Watching ±7%, ±11%, ±13% from previous close.";
  draw();
  risk();
}
async function refresh() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    const next = await api("/api/state");
    csrf = next.csrf;
    const symbol = assets[selected]?.symbol;
    const old = new Map(assets.map((a) => [a.symbol, a.candles]));
    state = next;
    if (
      editingId &&
      !state.orders.some((o) => o.id === editingId && o.status === "open")
    ) {
      cancelEdit(false);
      toast("Position has closed; level editing ended.");
    }
    assets = state.market.map((a) => ({
      ...a,
      candles: old.get(a.symbol) || [],
    }));
    selected = Math.max(
      0,
      assets.findIndex((a) => a.symbol === symbol),
    );
    if (!symbol) choose(selected);
    else render();
  } catch (e) {
    $("connection").textContent = "Disconnected · " + e.message;
    $("place").disabled = true;
  } finally {
    pollBusy = false;
  }
}
async function loadChart() {
  if (!assets.length || chartBusy) return;
  chartBusy = true;
  const symbol = assets[selected].symbol;
  try {
    const data = await api("/api/chart?symbol=" + encodeURIComponent(symbol));
    const a = assets.find((a) => a.symbol === symbol);
    if (a) a.candles = data.candles;
    chartSymbol = symbol;
    chartError = "";
    if (assets[selected].symbol === symbol) render();
  } catch (e) {
    chartError = e.message;
    render();
  } finally {
    chartBusy = false;
    if (assets[selected]?.symbol !== symbol) loadChart();
  }
}
function saveDrawing() {
  if (!assets.length || editingId) return;
  const symbol = assets[selected].symbol,
    body = {
      symbol,
      ...Object.fromEntries(
        ["entry", "stop", "target"].map((id) => [id, +$(id).value]),
      ),
    };
  clearTimeout(drawTimer);
  drawTimer = setTimeout(async () => {
    try {
      await api("/api/drawing", body);
      state.drawings[symbol] = body;
    } catch (e) {
      toast(e.message);
    }
  }, 300);
}
$("place").onclick = async () => {
  const button = $("place");
  button.disabled = true;
  try {
    if (editingId) {
      await api("/api/protection", {
        id: editingId,
        stop: +$("stop").value,
        target: +$("target").value,
      });
      cancelEdit();
      toast("Paper position levels updated.");
      await refresh();
      return;
    }
    const o = await api("/api/orders", {
      symbol: assets[selected].symbol,
      type: $("type").value,
      qty: +$("qty").value,
      entry: +$("entry").value,
      stop: +$("stop").value,
      target: +$("target").value,
    });
    toast(
      o.status === "rejected"
        ? o.reason
        : o.status === "open"
          ? "Paper position opened."
          : "Paper order queued.",
    );
    await refresh();
  } catch (e) {
    toast(e.message);
  } finally {
    button.disabled =
      !assets[selected]?.fresh || (!editingId && !state.entries_open);
  }
};
function cancelEdit(redraw = true) {
  editingId = null;
  $("place").textContent = "Place paper order";
  $("cancelEdit").hidden = true;
  $("entry").readOnly = $("qty").readOnly = false;
  $("type").disabled = false;
  if (redraw) render();
}
function editProtection(id) {
  const o = state.orders.find((o) => o.id === id);
  if (!o) return;
  choose(assets.findIndex((a) => a.symbol === o.symbol));
  editingId = id;
  $("entry").value = o.fill;
  $("qty").value = o.qty;
  $("stop").value = o.stop;
  $("target").value = o.target;
  $("entry").readOnly = $("qty").readOnly = true;
  $("type").disabled = true;
  $("place").textContent = "Update paper position levels";
  $("cancelEdit").hidden = false;
  draw();
  risk();
  $("place").scrollIntoView({ behavior: "smooth", block: "center" });
}
async function act(id) {
  try {
    await api("/api/action", { id });
    await refresh();
  } catch (e) {
    toast(e.message);
  }
}
async function endSession() {
  try {
    await api("/api/squareoff", {});
    toast(
      "Square-off requested. Stale positions wait for a fresh quote. Trading days finalize at the scheduled cutoff.",
    );
    await refresh();
  } catch (e) {
    toast(e.message);
  }
}
async function saveSettings() {
  try {
    await api("/api/settings", {
      loss_cap: +$("lossCap").value,
      slippage_bps: +$("slippage").value,
    });
    settings.close();
    await refresh();
  } catch (e) {
    toast(e.message);
  }
}
async function exportData() {
  try {
    const data = await api("/api/export"),
      blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json",
      }),
      a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "paperkite-" + state.date + ".json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } catch (e) {
    toast(e.message);
  }
}
function draw() {
  const w = canvas.clientWidth,
    h = 340,
    dpr = window.devicePixelRatio || 1;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  if (!assets.length) return;
  const source = assets[selected].candles,
    seconds = [60, 300, 900][$("tf").selectedIndex],
    groups = new Map();
  for (const c of source) {
    const key = Math.floor(c.time / seconds) * seconds;
    let b = groups.get(key);
    if (!b) groups.set(key, { ...c, time: key });
    else
      Object.assign(b, {
        c: c.c,
        h: Math.max(b.h, c.h),
        l: Math.min(b.l, c.l),
      });
  }
  const cs = [...groups.values()].slice(-65);
  if (!cs.length) {
    ctx.fillStyle = "#8da1b7";
    ctx.fillText("Waiting for chart data…", 20, 30);
    return;
  }
  let vals = cs
    .flatMap((c) => [c.h, c.l])
    .concat(
      ["entry", "stop", "target"]
        .map((id) => +$(id).value)
        .filter((v) => v > 0),
    );
  const lo = Math.min(...vals),
    hi = Math.max(...vals),
    pad = (hi - lo) * 0.15 || 1;
  range = { min: lo - pad, max: hi + pad };
  const y = (p) =>
    h - 25 - ((p - range.min) / (range.max - range.min)) * (h - 45);
  ctx.font = "11px system-ui";
  for (let i = 0; i < 6; i++) {
    const p = range.min + ((range.max - range.min) * i) / 5,
      yy = y(p);
    ctx.strokeStyle = "#23303d";
    ctx.beginPath();
    ctx.moveTo(0, yy);
    ctx.lineTo(w - 65, yy);
    ctx.stroke();
    ctx.fillStyle = "#778a9e";
    ctx.fillText(p.toFixed(2), w - 60, yy + 4);
  }
  const cw = (w - 75) / cs.length;
  cs.forEach((c, i) => {
    const x = i * cw + cw / 2;
    ctx.strokeStyle = ctx.fillStyle = c.c >= c.o ? "#55c9ad" : "#df7886";
    ctx.beginPath();
    ctx.moveTo(x, y(c.h));
    ctx.lineTo(x, y(c.l));
    ctx.stroke();
    ctx.fillRect(
      x - cw * 0.3,
      Math.min(y(c.o), y(c.c)),
      Math.max(2, cw * 0.6),
      Math.max(1, Math.abs(y(c.o) - y(c.c))),
    );
    if (i % Math.max(1, Math.floor(cs.length / 4)) === 0) {
      ctx.fillStyle = "#8291a3";
      ctx.fillText(
        new Date(c.time * 1000).toLocaleTimeString("en-IN", {
          timeZone: "Asia/Kolkata",
          hour: "2-digit",
          minute: "2-digit",
        }),
        x,
        h - 3,
      );
    }
  });
  for (const [id, col] of [
    ["entry", "#7eafff"],
    ["stop", "#ed8391"],
    ["target", "#55d2b0"],
  ]) {
    const p = +$(id).value;
    if (!p) continue;
    ctx.strokeStyle = col;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(0, y(p));
    ctx.lineTo(w - 65, y(p));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = col;
    ctx.fillText(id.toUpperCase() + " " + p.toFixed(2), 8, y(p) - 6);
  }
}
function chartPrice(ev) {
  const y = ev.clientY - canvas.getBoundingClientRect().top;
  return Math.max(
    0.05,
    range.min + ((340 - 25 - y) / (340 - 45)) * (range.max - range.min),
  );
}
canvas.onpointerdown = (e) => {
  const p = chartPrice(e);
  drag =
    (editingId ? ["stop", "target"] : ["entry", "stop", "target"]).find(
      (id) => Math.abs(+$(id).value - p) / (range.max - range.min) < 0.035,
    ) || (editingId && mode === "entry" ? "stop" : mode);
  canvas.setPointerCapture(e.pointerId);
  $(drag).value = p.toFixed(2);
  draw();
  risk();
};
canvas.onpointermove = (e) => {
  if (drag) {
    $(drag).value = chartPrice(e).toFixed(2);
    draw();
    risk();
  }
};
canvas.onpointerup = () => {
  drag = null;
  saveDrawing();
};
canvas.onpointercancel = () => (drag = null);
for (const id of ["entry", "stop", "target", "qty"])
  $(id).oninput = () => {
    risk();
    draw();
    if (id !== "qty") saveDrawing();
  };
$("tf").onchange = draw;
$("search").oninput = render;
window.onresize = draw;
settings.addEventListener("toggle", () => {
  if (settings.open && state) {
    $("capital").value = state.capital;
    $("lossCap").value = state.loss_cap;
    $("slippage").value = state.slippage_bps;
  }
});
refresh();
setInterval(refresh, 2000);
setInterval(loadChart, 10000);
