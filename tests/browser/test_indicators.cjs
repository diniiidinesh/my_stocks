const assert = require("node:assert/strict");
const I = require("../../src/nse_alert/paper_web/indicators.js");
const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} != ${b}`);
assert.deepEqual(I.sma([1, 2, 3, 4], 3), [null, null, 2, 3]);
assert.deepEqual(I.ema([1, 2, 3, 4, 5], 3), [null, null, 2, 3, 4]);
const b = I.bands([1, 2, 3], 3, 2);
near(b.mid[2], 2);
near(b.upper[2], 2 + 2 * Math.sqrt(2 / 3));
const up = Array.from({ length: 100 }, (_, i) => ({
  o: 100 + i,
  h: 102 + i,
  l: 99 + i,
  c: 101 + i,
  v: 1000 + i,
}));
assert.equal(
  I.rsi(
    up.map((c) => c.c),
    14,
  ).at(-1),
  100,
);
const flat = up.map(() => ({ o: 100, h: 100, l: 100, c: 100, v: 1000 }));
assert.equal(
  I.rsi(
    flat.map((c) => c.c),
    14,
  ).at(-1),
  50,
);
assert.equal(I.adx(flat, 14, 25).at(-1), 0);
near(I.adx(up, 14, 25).at(-1), 100);
assert.equal(I.adx(up.slice(0, 38), 14, 25).at(-1), null);
assert.notEqual(I.adx(up.slice(0, 39), 14, 25).at(-1), null);
const m = I.macd(
  flat.map((c) => c.c),
  12,
  26,
  9,
);
assert.equal(m.line.at(-1), 0);
assert.equal(m.signal.at(-1), 0);
assert.equal(m.hist.at(-1), 0);
assert.equal(I.ema([null, null, 1, 2, 3, 4], 3)[4], 2);
const st = I.supertrend(up, 14, 4);
assert.equal(st.value[12], null);
assert.equal(st.direction.at(-1), 1);
const falling = up.concat(
  Array.from({ length: 40 }, (_, i) => ({
    o: 200 - i * 5,
    h: 201 - i * 5,
    l: 194 - i * 5,
    c: 195 - i * 5,
    v: 1000,
  })),
);
assert.equal(I.supertrend(falling, 14, 4).direction.at(-1), -1);
assert.ok(
  I.calculate([], {
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
  }).adx.length === 0,
);
console.log(
  "Indicator fixtures passed: SMA, EMA, BB, RSI, ADX warm-up, MACD, SuperTrend reversal, empty input.",
);
