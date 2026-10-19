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
    `${state.entries_open ? "Entries open" : "Entries closed"} · ${state.date} · Square-off ${state.cutoff} IST · Telegram: ${state.telegram_status || "disabled"}${state.mode === "mock" ? " · Mock streak only" : ""}`;
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
    const old = new Map(
      assets.map((a) => [
        a.symbol,
        { candles: a.candles, interval: a.interval },
      ]),
    );
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
      candles: old.get(a.symbol)?.candles || [],
      interval: old.get(a.symbol)?.interval,
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
  const symbol = assets[selected].symbol,
    interval = $("tf").value;
  try {
    const data = await api(
      "/api/chart?symbol=" +
        encodeURIComponent(symbol) +
        "&interval=" +
        interval,
    );
    const a = assets.find((a) => a.symbol === symbol);
    if (a) {
      a.candles = data.candles;
      a.interval = interval;
    }
    chartSymbol = symbol;
    chartError = "";
    if (assets[selected].symbol === symbol) render();
  } catch (e) {
    chartError = e.message;
    render();
  } finally {
    chartBusy = false;
    if (assets[selected]?.symbol !== symbol || $("tf").value !== interval)
      loadChart();
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
$("tf").onchange = () => {
  try {
    localStorage.setItem("paperkite-timeframe", $("tf").value);
  } catch {}
  draw();
  loadChart();
};
$("barCount").onchange = draw;
try {
  const saved = localStorage.getItem("paperkite-timeframe");
  if (["minute", "5minute", "15minute", "60minute", "day"].includes(saved))
    $("tf").value = saved;
} catch {}
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
