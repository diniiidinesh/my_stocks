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
  mode = "pan",
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
  $("ticketMessage").textContent = s;
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
  if (!assets[i]) return;
  selected = i;
  viewOffset = 0;
  crosshair = null;
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
  $("planSummary").textContent = `Entry ${money(e)} · Stop ${money(s)} · Target ${money(t)} · Qty ${q}`;
  $("ticketFunds").textContent = `Order value ≈ ${money(e*q)} + charges · Available ${money(state?.cash || 0)}`;
}
function render() {
  if (!state || !assets.length) return;
  const a = assets[selected],
    query = $("search").value.toUpperCase();
  const matches = assets.map((x, i) => [x, i]).filter(([x]) =>
    `${x.symbol} ${x.name || ""}`.toUpperCase().includes(query.trim()));
  $("searchCount").textContent = `${matches.length} of ${assets.length} stocks · liquidity-screened NSE cash`;
  $("watch").innerHTML = matches.map(([x,i]) =>
    `<button class="stock ${i === selected ? "active" : ""}" aria-pressed="${i === selected}" onclick="choose(${i})"><span>${esc(x.symbol)}<small>${esc(x.name || "NSE")} · ${x.fresh ? "Fresh" : "Awaiting quote"}</small></span><span class="stock-price">${x.price.toFixed(2)}<small class="${x.price >= x.base ? "green" : "red"}">${x.base ? ((x.price / x.base - 1) * 100).toFixed(2) : "—"}%</small></span></button>`
  ).join("") || '<p class="notice">No matching stock in today’s screened universe. Try its NSE symbol or company name. The paper desk follows your configured liquidity filters.</p>';
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
  $("ticketSymbol").textContent = (editingId ? "Edit levels · " : "Buy ") + a.symbol;
  $("ticketQuote").textContent = "NSE · " + money(a.price);
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
    if (!symbol && assets.length) choose(selected);
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
  $("chartStatus").textContent = "Loading chart history…";
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
    $("chartStatus").textContent = "";
    if (assets[selected].symbol === symbol) render();
  } catch (e) {
    chartError = e.message;
    $("chartStatus").textContent = "Chart history unavailable: " + e.message + ". Indicators need candle history. Check your daily Kite login and historical-data access.";
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
      $("orderTicket").close();
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
    if (o.status !== "rejected") $("orderTicket").close();
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
  $("place").textContent = "Buy · Paper order";
  $("cancelEdit").hidden = true;
  $("entry").readOnly = $("qty").readOnly = false;
  $("type").disabled = false;
  syncOrderType();
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
  openTicket();
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
  crosshair = null;
  if (mode === "pan") {
    panStart = {x:e.clientX, offset:viewOffset};
    canvas.setPointerCapture(e.pointerId);
    return;
  }
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
  const rect = canvas.getBoundingClientRect();
  crosshair = {x:e.clientX-rect.left, y:e.clientY-rect.top};
  if (panStart) {
    const a = assets[selected];
    const count = +$("barCount").value;
    viewOffset = Math.max(0, Math.min(Math.max(0, (a?.candles.length || 0)-count), Math.round(panStart.offset+(e.clientX-panStart.x)*count/Math.max(1,rect.width-75))));
    draw();
    return;
  }
  if (drag) {
    $(drag).value = chartPrice(e).toFixed(2);
    draw();
    risk();
  } else draw();
};
canvas.onpointerup = () => {
  panStart = null;
  if (drag) saveDrawing();
  drag = null;
};
canvas.onpointercancel = () => { drag = panStart = null; };
canvas.onpointerleave = () => { crosshair = null; if (!drag && !panStart) draw(); };
canvas.addEventListener("wheel", (e) => { e.preventDefault(); zoomChart(e.deltaY > 0 ? 1.15 : 1/1.15); }, {passive:false});
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
$("barCount").onchange = () => zoomChart(1);
try {
  const saved = localStorage.getItem("paperkite-timeframe");
  if (["minute", "5minute", "15minute", "60minute", "day"].includes(saved))
    $("tf").value = saved;
} catch {}
$("search").oninput = render;
$("search").onkeydown = (e) => {
  if (e.key === "Enter") {
    const q = $("search").value.trim().toUpperCase();
    const i = assets.findIndex(a => `${a.symbol} ${a.name || ""}`.toUpperCase().includes(q));
    if (i >= 0) choose(i);
  }
};
$("type").onchange = syncOrderType;
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && $("chartPanel").classList.contains("expanded")) toggleChart();
  if (e.target.matches("input,select,textarea") || document.querySelector("dialog[open]")) return;
  if (e.key === "/") { e.preventDefault(); $("search").focus(); }
  if (e.key.toLowerCase() === "b") openTicket();
});
window.onresize = draw;
settings.addEventListener("toggle", () => {
  if (settings.open && state) {
    $("capital").value = state.capital;
    $("lossCap").value = state.loss_cap;
    $("slippage").value = state.slippage_bps;
  }
});
let viewOffset = 0, crosshair = null, panStart = null;
function openTicket() {
  if (!assets.length) return;
  $("ticketMessage").textContent = "";
  syncOrderType();
  if (!$("orderTicket").open) $("orderTicket").showModal();
}
function setOrderType(type) { $("type").value = type; syncOrderType(); }
function syncOrderType() {
  const type = $("type").value;
  $("entry").readOnly = !!editingId || type === "market";
  $("priceLabel").textContent = type === "stop" ? "Trigger price ₹" : type === "market" ? "Reference price ₹" : "Limit price ₹";
  $("priceHelp").textContent = type === "market" ? "Fills at the fresh quote plus configured slippage." : type === "stop" ? "Triggers at or above this price, then fills at the observed quote." : "Buy at this price or lower.";
  document.querySelectorAll("[data-order]").forEach(b => { b.setAttribute("aria-pressed", b.dataset.order === type); b.disabled = !!editingId; });
}
function setTool(tool) {
  mode = tool;
  document.querySelectorAll("[data-tool]").forEach(b => b.setAttribute("aria-pressed",b.dataset.tool === tool));
  canvas.style.cursor = tool === "pan" ? "grab" : "crosshair";
}
function zoomChart(factor) {
  const raw = +$("barCount").value;
  $("barCount").value = Math.max(20,Math.min(500,Math.round((Number.isFinite(raw) && raw > 0 ? raw : 100)*factor)));
  draw();
}
function resetViewport() { viewOffset = 0; crosshair = null; draw(); }
function toggleChart() {
  const expanded = $("chartPanel").classList.toggle("expanded");
  $("expandChart").textContent = expanded ? "Collapse chart" : "Expand chart";
  draw();
}
syncOrderType();
setTool("pan");
renderStudyChips();
refresh();
setInterval(refresh, 2000);
setInterval(loadChart, 10000);
