/* Pure indicator calculations. Null means insufficient warm-up/data. */
(function (root) {
  const valid = (v) => typeof v === "number" && Number.isFinite(v);
  function sma(a, n) {
    let out = Array(a.length).fill(null);
    for (let i = n - 1; i < a.length; i++) {
      const w = a.slice(i - n + 1, i + 1);
      if (w.every(valid)) out[i] = w.reduce((x, y) => x + y, 0) / n;
    }
    return out;
  }
  function smooth(a, n, alpha) {
    const seed = sma(a, n),
      out = Array(a.length).fill(null);
    let prev = null;
    for (let i = 0; i < a.length; i++) {
      if (!valid(a[i])) {
        prev = null;
        continue;
      }
      if (prev === null) {
        if (valid(seed[i])) prev = seed[i];
      } else prev = alpha * a[i] + (1 - alpha) * prev;
      out[i] = prev;
    }
    return out;
  }
  const ema = (a, n) => smooth(a, n, 2 / (n + 1)),
    rma = (a, n) => smooth(a, n, 1 / n);
  function bands(a, n, m) {
    const mid = sma(a, n),
      upper = [],
      lower = [];
    for (let i = 0; i < a.length; i++) {
      if (mid[i] === null) {
        upper.push(null);
        lower.push(null);
        continue;
      }
      const variance =
        a.slice(i - n + 1, i + 1).reduce((s, v) => s + (v - mid[i]) ** 2, 0) /
        n;
      upper.push(mid[i] + m * Math.sqrt(variance));
      lower.push(mid[i] - m * Math.sqrt(variance));
    }
    return { mid, upper, lower };
  }
  function rsi(a, n) {
    const gains = a.map((v, i) => (i ? Math.max(0, v - a[i - 1]) : null)),
      losses = a.map((v, i) => (i ? Math.max(0, a[i - 1] - v) : null)),
      g = rma(gains, n),
      l = rma(losses, n);
    return a.map((_, i) =>
      g[i] === null || l[i] === null
        ? null
        : l[i] === 0
          ? g[i] === 0
            ? 50
            : 100
          : g[i] === 0
            ? 0
            : 100 - 100 / (1 + g[i] / l[i]),
    );
  }
  function tr(cs) {
    return cs.map((c, i) =>
      i
        ? Math.max(
            c.h - c.l,
            Math.abs(c.h - cs[i - 1].c),
            Math.abs(c.l - cs[i - 1].c),
          )
        : c.h - c.l,
    );
  }
  function supertrend(cs, n, factor) {
    const atr = rma(tr(cs), n),
      value = Array(cs.length).fill(null),
      direction = Array(cs.length).fill(null);
    let upper = null,
      lower = null,
      up = false;
    for (let i = 0; i < cs.length; i++) {
      if (atr[i] === null) continue;
      let u = (cs[i].h + cs[i].l) / 2 + factor * atr[i],
        l = (cs[i].h + cs[i].l) / 2 - factor * atr[i];
      if (upper === null) {
        upper = u;
        lower = l;
        up = false;
      } else {
        upper = u < upper || cs[i - 1].c > upper ? u : upper;
        lower = l > lower || cs[i - 1].c < lower ? l : lower;
        up = up ? !(cs[i].c < lower) : cs[i].c > upper;
      }
      value[i] = up ? lower : upper;
      direction[i] = up ? 1 : -1;
    }
    return { value, direction };
  }
  function macd(a, fast, slow, signalLength) {
    const f = ema(a, fast),
      s = ema(a, slow),
      line = a.map((_, i) =>
        f[i] !== null && s[i] !== null ? f[i] - s[i] : null,
      ),
      signal = ema(line, signalLength);
    return {
      line,
      signal,
      hist: line.map((v, i) =>
        v !== null && signal[i] !== null ? v - signal[i] : null,
      ),
    };
  }
  function adx(cs, diLength, smoothing) {
    const ranges = tr(cs);
    if (ranges.length) ranges[0] = null;
    const plus = cs.map((c, i) => {
        if (!i) return null;
        const u = c.h - cs[i - 1].h,
          d = cs[i - 1].l - c.l;
        return u > d && u > 0 ? u : 0;
      }),
      minus = cs.map((c, i) => {
        if (!i) return null;
        const u = c.h - cs[i - 1].h,
          d = cs[i - 1].l - c.l;
        return d > u && d > 0 ? d : 0;
      }),
      atr = rma(ranges, diLength),
      p = rma(plus, diLength),
      m = rma(minus, diLength);
    const dx = cs.map((_, i) =>
      atr[i] === null
        ? null
        : atr[i] === 0
          ? 0
          : (100 * Math.abs(p[i] - m[i])) / (p[i] + m[i] || 1),
    );
    return rma(dx, smoothing);
  }
  function calculate(cs, s) {
    const close = cs.map((c) => c.c),
      volume = cs.map((c) => (valid(c.v) ? c.v : null));
    return {
      ma21: sma(close, s.ma21),
      ma65: sma(close, s.ma65),
      ema5: ema(close, s.ema5),
      ema9: ema(close, s.ema9),
      ema10: ema(close, s.ema10),
      bb: bands(close, s.bbLength, s.bbMult),
      st: supertrend(cs, s.stLength, s.stFactor),
      rsi: rsi(close, s.rsiLength),
      macd: macd(close, s.macdFast, s.macdSlow, s.macdSignal),
      adx: adx(cs, s.diLength, s.adxSmooth),
      volume,
      volumeEMA: ema(volume, s.volumeLength),
    };
  }
  root.PaperIndicators = {
    valid,
    sma,
    ema,
    rma,
    bands,
    rsi,
    tr,
    supertrend,
    macd,
    adx,
    calculate,
  };
  if (typeof module !== "undefined") module.exports = root.PaperIndicators;
})(typeof window !== "undefined" ? window : globalThis);
