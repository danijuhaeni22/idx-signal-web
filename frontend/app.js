// === CONFIG ===
// Jalankan backend lokal: http://127.0.0.1:8000
// Deploy backend Render: ganti ke URL render kamu, contoh: https://idx-signal-api.onrender.com
const API_BASE = localStorage.getItem("API_BASE") || "http://127.0.0.1:8000";

const $ = (id) => document.getElementById(id);

const tickerInput = $("tickerInput");
const btnLoad = $("btnLoad");
const btnWatch = $("btnWatch");
const btnRefreshRadar = $("btnRefreshRadar");

let chart, candleSeries, ma20Series, ma50Series, volumeSeries;

function fmt(n){
  if (n === null || n === undefined || Number.isNaN(n)) return "-";
  return Number(n).toLocaleString("id-ID", { maximumFractionDigits: 2 });
}

function setupChart(){
  const el = $("chart");
  el.innerHTML = "";
  chart = LightweightCharts.createChart(el, {
    layout: {
      textColor: "rgba(255,255,255,0.9)",
      background: { type: "solid", color: "rgba(0,0,0,0)" }
    },
    grid: {
      vertLines: { color: "rgba(255,255,255,0.06)" },
      horzLines: { color: "rgba(255,255,255,0.06)" }
    },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.10)" },
    timeScale: { borderColor: "rgba(255,255,255,0.10)" }
  });

  candleSeries = chart.addCandlestickSeries();
  ma20Series = chart.addLineSeries({ lineWidth: 2 });
  ma50Series = chart.addLineSeries({ lineWidth: 2 });

  volumeSeries = chart.addHistogramSeries({
    priceFormat: { type: "volume" },
    priceScaleId: "",
    scaleMargins: { top: 0.8, bottom: 0 }
  });

  window.addEventListener("resize", () => {
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
  });
}

async function getJSON(path){
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(txt || `HTTP ${res.status}`);
  }
  return res.json();
}

async function loadRegime(){
  const r = await getJSON(`/api/market-regime`);
  const el = $("regime");
  el.innerHTML = `
    <div class="kv"><div class="k">Status</div><div class="v">${r.status}</div></div>
    <div class="kv"><div class="k">As of</div><div class="v">${r.asof}</div></div>
    <div class="kv"><div class="k">IHSG Close</div><div class="v">${fmt(r.close)}</div></div>
    <div class="kv"><div class="k">MA20 / MA50</div><div class="v">${fmt(r.ma20)} / ${fmt(r.ma50)}</div></div>
    <div class="kv"><div class="k">Day Change</div><div class="v">${fmt(r.day_change_pct)}%</div></div>
    <div class="kv"><div class="k">ATR% (14)</div><div class="v">${fmt(r.atr14_pct)}%</div></div>
    <div class="muted" style="margin-top:8px">${(r.note || []).join(" • ")}</div>
  `;
  return r.status;
}

function calcMA(bars, n){
  const out = [];
  for (let i = 0; i < bars.length; i++){
    if (i < n-1) continue;
    let sum = 0;
    for (let j = i-(n-1); j <= i; j++){
      sum += bars[j].close;
    }
    out.push({ time: bars[i].time, value: sum / n });
  }
  return out;
}

async function loadTicker(ticker){
  const t = ticker.trim().toUpperCase();
  if (!t) return;

  const regime = await loadRegime();

  const o = await getJSON(`/api/ohlcv?ticker=${encodeURIComponent(t)}&days=260`);
  const bars = o.bars;

  candleSeries.setData(bars);
  ma20Series.setData(calcMA(bars, 20));
  ma50Series.setData(calcMA(bars, 50));

  const vols = bars.map(b => ({ time: b.time, value: b.volume }));
  volumeSeries.setData(vols);

  chart.timeScale().fitContent();

  const s = await getJSON(`/api/signal?ticker=${encodeURIComponent(t)}&days=260`);
  renderSignalCard(s.signal, regime);
}

function renderSignalCard(sig, regime){
  const el = $("signalCard");
  const disabled = (regime === "NO_TRADE_DAY");
  const badge = sig.setup || "NONE";

  const rr = sig.rr || {};
  const rr1 = rr.r_multiple_tp1 ?? null;
  const rr2 = rr.r_multiple_tp2 ?? null;

  el.innerHTML = `
    <div class="box">
      <div class="badge">${disabled ? "NO TRADE DAY • " : ""}${badge}</div>
      <div class="kv"><div class="k">As of</div><div class="v">${sig.asof}</div></div>
      <div class="kv"><div class="k">Close</div><div class="v">${fmt(sig.close)}</div></div>
      <div class="kv"><div class="k">Support / Resistance</div><div class="v">${fmt(sig.support)} / ${fmt(sig.resistance)}</div></div>
      <div class="kv"><div class="k">MA20 / MA50</div><div class="v">${fmt(sig.ma20)} / ${fmt(sig.ma50)}</div></div>
      <div class="kv"><div class="k">Trend OK</div><div class="v">${sig.trend_ok ? "YA" : "TIDAK"}</div></div>
      <div class="muted" style="margin-top:8px">${(sig.reason || []).join(" • ")}</div>
    </div>

    <div class="box">
      <div class="kv"><div class="k">Entry</div><div class="v">${fmt(sig.entry)}</div></div>
      <div class="kv"><div class="k">Stop Loss</div><div class="v">${fmt(sig.sl)}</div></div>
      <div class="kv"><div class="k">TP1 / TP2</div><div class="v">${fmt(sig.tp1)} / ${fmt(sig.tp2)}</div></div>
      <div class="kv"><div class="k">R(TP1) / R(TP2)</div><div class="v">${fmt(rr1)} / ${fmt(rr2)}</div></div>
      <div class="muted" style="margin-top:8px">
        ${disabled ? "Regime NO TRADE DAY: abaikan entry, fokus watchlist & tunggu market membaik." : "Gunakan plan ini sebagai template. Tetap disiplin cut loss."}
      </div>
    </div>
  `;
}

function loadWatchlist(){
  const el = $("watchlist");
  const w = JSON.parse(localStorage.getItem("WATCHLIST") || "[]");
  if (!w.length){
    el.innerHTML = `<div class="muted">Belum ada watchlist. Klik ⭐ Watch di atas.</div>`;
    return;
  }
  el.innerHTML = `
    <table class="table">
      <thead><tr><th>Ticker</th><th>Aksi</th></tr></thead>
      <tbody>
        ${w.map(t => `
          <tr class="click" data-t="${t}">
            <td>${t}</td>
            <td><button data-del="${t}">Hapus</button></td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;

  el.querySelectorAll("tr[data-t]").forEach(tr => {
    tr.addEventListener("click", (e) => {
      const del = e.target?.getAttribute?.("data-del");
      if (del) return;
      const t = tr.getAttribute("data-t");
      tickerInput.value = t;
      loadTicker(t);
    });
  });

  el.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => {
      const t = btn.getAttribute("data-del");
      const nw = w.filter(x => x !== t);
      localStorage.setItem("WATCHLIST", JSON.stringify(nw));
      loadWatchlist();
    });
  });
}

function addWatch(){
  const t = tickerInput.value.trim().toUpperCase();
  if (!t) return;
  const w = JSON.parse(localStorage.getItem("WATCHLIST") || "[]");
  if (!w.includes(t)) w.unshift(t);
  localStorage.setItem("WATCHLIST", JSON.stringify(w.slice(0, 50)));
  loadWatchlist();
}

async function loadRadar(){
  const el = $("radar");
  el.innerHTML = `<div class="muted">Loading radar…</div>`;
  const r = await getJSON(`/api/screener?universe=LQ45&days=260`);
  const rows = r.top || [];

  el.innerHTML = `
    <div class="muted" style="margin-bottom:10px">
      Market: <b>${r.market_regime.status}</b> (asof ${r.market_regime.asof})
    </div>
    <table class="table">
      <thead>
        <tr>
          <th>Rank</th><th>Ticker</th><th>Setup</th><th>Score</th><th>Close</th><th>Alasan</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map((x, i) => `
          <tr class="click" data-t="${x.ticker.replace(".JK","")}">
            <td>${i+1}</td>
            <td><b>${x.ticker.replace(".JK","")}</b></td>
            <td>${x.setup}</td>
            <td>${x.score}</td>
            <td>${fmt(x.close)}</td>
            <td class="muted">${(x.reason || []).join(" • ")}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;

  el.querySelectorAll("tr[data-t]").forEach(tr => {
    tr.addEventListener("click", () => {
      const t = tr.getAttribute("data-t");
      tickerInput.value = t;
      loadTicker(t);
    });
  });
}

btnLoad.addEventListener("click", () => loadTicker(tickerInput.value));
btnWatch.addEventListener("click", addWatch);
btnRefreshRadar.addEventListener("click", loadRadar);

setupChart();
loadWatchlist();
loadRegime().then(() => loadTicker(tickerInput.value)).catch(console.error);
loadRadar().catch(console.error);
