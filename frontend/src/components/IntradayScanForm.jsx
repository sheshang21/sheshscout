import { useEffect, useState } from 'react';
import { api, ApiError } from '../api';

// Mirrors core.intraday_scanner.DEFAULT_PARAMS so the form's starting
// values match the backend defaults exactly (backend re-applies its own
// defaults for anything omitted, so this is just for a sane initial UI).
const DEFAULTS = {
  long: {
    min_price: 20, min_volume: 100000, min_conditions: 4, min_score: 50,
    price_change_threshold: 0.0, dist_threshold: 2.0, trend_threshold: 2.0,
    momentum_threshold: 0.5, volume_ratio_threshold: 1.2, rsi_threshold: 35,
    atr_threshold: 1.0, rsi_period: 14, atr_period: 14, momentum_window: 30,
    strong_score: 70, stop_loss_pct: 0.5, target_pct: 2.0,
  },
  short: {
    min_price: 20, min_volume: 100000, min_conditions: 4, min_score: 50,
    price_change_threshold: 0.0, dist_threshold: 2.0, trend_threshold: -2.0,
    momentum_threshold: -0.5, volume_ratio_threshold: 1.2, rsi_threshold: 65,
    atr_threshold: 1.0, rsi_period: 14, atr_period: 14, momentum_window: 30,
    strong_score: 70, stop_loss_pct: 0.5, target_pct: 2.0,
  },
};

export default function IntradayScanForm({ onStarted, disabled }) {
  const [direction, setDirection] = useState('long'); // 'long' | 'short'
  const [nse, setNse] = useState(true);
  const [bse, setBse] = useState(false);
  const [scanMode, setScanMode] = useState('range'); // 'range' | 'custom' -- intraday scans are usually a subset, not the full universe
  const [counts, setCounts] = useState({ NSE: 0, BSE: 0 });
  const [nseFrom, setNseFrom] = useState(1);
  const [nseTo, setNseTo] = useState(50);
  const [bseFrom, setBseFrom] = useState(1);
  const [bseTo, setBseTo] = useState(50);
  const [customList, setCustomList] = useState('');
  const [params, setParams] = useState(DEFAULTS.long);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // Reuses the positional universe endpoint -- same nse.txt/bse.txt
    // row counts apply regardless of which pipeline scans them.
    api.universeCounts().then((c) => {
      setCounts(c);
      setNseTo((v) => Math.min(v, c.NSE || v));
      setBseTo((v) => Math.min(v, c.BSE || v));
    }).catch(() => {});
  }, []);

  function switchDirection(dir) {
    setDirection(dir);
    setParams(DEFAULTS[dir]);
  }

  function updateParam(key, value) {
    setParams((p) => ({ ...p, [key]: Number(value) }));
  }

  function parseCustomList() {
    return customList
      .split(/[\n,]/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean)
      .map((s) => (s.includes('.NS') || s.includes('.BO') ? s : `${s}.NS`));
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);

    const exchanges = [nse && 'NSE', bse && 'BSE'].filter(Boolean);
    if (exchanges.length === 0 && scanMode !== 'custom') {
      setError('Pick at least one exchange.');
      return;
    }

    const payload = { direction, params };

    if (scanMode === 'custom') {
      const symbols = parseCustomList();
      if (symbols.length === 0) {
        setError('Enter at least one symbol.');
        return;
      }
      payload.symbols = symbols;
    } else {
      if (nse && nseFrom > nseTo) { setError("NSE 'From' must be ≤ 'To'."); return; }
      if (bse && bseFrom > bseTo) { setError("BSE 'From' must be ≤ 'To'."); return; }
      payload.exchanges = exchanges;
      payload.range = {
        ...(nse ? { NSE: [nseFrom, nseTo] } : {}),
        ...(bse ? { BSE: [bseFrom, bseTo] } : {}),
      };
    }

    setBusy(true);
    try {
      const job = await api.startIntradayScan(payload);
      onStarted(job);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : 'Could not start the scan.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card" onSubmit={handleSubmit}>
      <h3 style={{ marginBottom: 16, fontSize: 15 }}>Intraday scan setup</h3>

      <div className="field-row">
        <label>Direction</label>
        <div className="nav-tabs" style={{ marginTop: 4 }}>
          <button type="button" className={direction === 'long' ? 'active' : ''} onClick={() => switchDirection('long')}>
            📈 Long (Buy)
          </button>
          <button type="button" className={direction === 'short' ? 'active' : ''} onClick={() => switchDirection('short')}>
            📉 Short (Sell)
          </button>
        </div>
      </div>

      <div className="field-row">
        <label>Exchanges</label>
        <div className="checkbox-row">
          <input id="intra-nse" type="checkbox" checked={nse} onChange={(e) => setNse(e.target.checked)} />
          <label htmlFor="intra-nse">NSE</label>
        </div>
        <div className="checkbox-row">
          <input id="intra-bse" type="checkbox" checked={bse} onChange={(e) => setBse(e.target.checked)} />
          <label htmlFor="intra-bse">BSE</label>
        </div>
      </div>

      <div className="field-row">
        <label htmlFor="intra-scan-mode">Stock source</label>
        <select id="intra-scan-mode" value={scanMode} onChange={(e) => setScanMode(e.target.value)}>
          <option value="range">Row range</option>
          <option value="custom">Custom list</option>
        </select>
      </div>

      {scanMode === 'range' && (
        <div className="field-row">
          <label>Row range (1-based, from nse.txt / bse.txt)</label>
          {nse && (
            <div className="threshold-grid" style={{ marginBottom: 8 }}>
              <div>
                <label htmlFor="intra-nse-from">NSE from ({counts.NSE || '?'} total)</label>
                <input id="intra-nse-from" type="number" min={1} max={counts.NSE || undefined} value={nseFrom}
                  onChange={(e) => setNseFrom(Number(e.target.value))} />
              </div>
              <div>
                <label htmlFor="intra-nse-to">NSE to</label>
                <input id="intra-nse-to" type="number" min={1} max={counts.NSE || undefined} value={nseTo}
                  onChange={(e) => setNseTo(Number(e.target.value))} />
              </div>
            </div>
          )}
          {bse && (
            <div className="threshold-grid">
              <div>
                <label htmlFor="intra-bse-from">BSE from ({counts.BSE || '?'} total)</label>
                <input id="intra-bse-from" type="number" min={1} max={counts.BSE || undefined} value={bseFrom}
                  onChange={(e) => setBseFrom(Number(e.target.value))} />
              </div>
              <div>
                <label htmlFor="intra-bse-to">BSE to</label>
                <input id="intra-bse-to" type="number" min={1} max={counts.BSE || undefined} value={bseTo}
                  onChange={(e) => setBseTo(Number(e.target.value))} />
              </div>
            </div>
          )}
        </div>
      )}

      {scanMode === 'custom' && (
        <div className="field-row">
          <label htmlFor="intra-custom-list">Symbols (comma or newline separated)</label>
          <textarea id="intra-custom-list" rows={5} placeholder={'RELIANCE, TCS, INFY\nor\nRELIANCE\nTCS'}
            value={customList} onChange={(e) => setCustomList(e.target.value)} />
        </div>
      )}

      <div className="field-row">
        <label>Basic filters</label>
        <div className="threshold-grid">
          <div>
            <label htmlFor="intra-min-price">Min price (₹)</label>
            <input id="intra-min-price" type="number" min={1} step={5} value={params.min_price}
              onChange={(e) => updateParam('min_price', e.target.value)} />
          </div>
          <div>
            <label htmlFor="intra-min-vol">Min volume</label>
            <input id="intra-min-vol" type="number" min={10000} step={10000} value={params.min_volume}
              onChange={(e) => updateParam('min_volume', e.target.value)} />
          </div>
          <div>
            <label htmlFor="intra-min-cond">Min conditions (of 7)</label>
            <input id="intra-min-cond" type="number" min={2} max={7} value={params.min_conditions}
              onChange={(e) => updateParam('min_conditions', e.target.value)} />
          </div>
          <div>
            <label htmlFor="intra-min-score">Min score (0-100)</label>
            <input id="intra-min-score" type="number" min={20} max={90} step={5} value={params.min_score}
              onChange={(e) => updateParam('min_score', e.target.value)} />
          </div>
        </div>
      </div>

      <details className="advanced">
        <summary>Advanced thresholds</summary>
        <div className="threshold-grid">
          <div>
            <label htmlFor="intra-pc">Price change threshold (%)</label>
            <input id="intra-pc" type="number" step={0.5} value={params.price_change_threshold}
              onChange={(e) => updateParam('price_change_threshold', e.target.value)} />
          </div>
          <div>
            <label htmlFor="intra-dist">{direction === 'long' ? 'Dist from low (%)' : 'Dist from high (%)'}</label>
            <input id="intra-dist" type="number" step={0.5} value={params.dist_threshold}
              onChange={(e) => updateParam('dist_threshold', e.target.value)} />
          </div>
          <div>
            <label htmlFor="intra-trend">5-day trend (%)</label>
            <input id="intra-trend" type="number" step={0.5} value={params.trend_threshold}
              onChange={(e) => updateParam('trend_threshold', e.target.value)} />
          </div>
          <div>
            <label htmlFor="intra-mom">Momentum (%)</label>
            <input id="intra-mom" type="number" step={0.1} value={params.momentum_threshold}
              onChange={(e) => updateParam('momentum_threshold', e.target.value)} />
          </div>
          <div>
            <label htmlFor="intra-vr">Volume ratio</label>
            <input id="intra-vr" type="number" step={0.1} value={params.volume_ratio_threshold}
              onChange={(e) => updateParam('volume_ratio_threshold', e.target.value)} />
          </div>
          <div>
            <label htmlFor="intra-rsi">RSI {direction === 'long' ? 'oversold' : 'overbought'}</label>
            <input id="intra-rsi" type="number" value={params.rsi_threshold}
              onChange={(e) => updateParam('rsi_threshold', e.target.value)} />
          </div>
          <div>
            <label htmlFor="intra-atr">ATR % threshold</label>
            <input id="intra-atr" type="number" step={0.1} value={params.atr_threshold}
              onChange={(e) => updateParam('atr_threshold', e.target.value)} />
          </div>
          <div>
            <label htmlFor="intra-strong">Strong signal score</label>
            <input id="intra-strong" type="number" min={60} max={90} step={5} value={params.strong_score}
              onChange={(e) => updateParam('strong_score', e.target.value)} />
          </div>
          <div>
            <label htmlFor="intra-sl">Stop loss %</label>
            <input id="intra-sl" type="number" step={0.1} value={params.stop_loss_pct}
              onChange={(e) => updateParam('stop_loss_pct', e.target.value)} />
          </div>
          <div>
            <label htmlFor="intra-tgt">Target %</label>
            <input id="intra-tgt" type="number" step={0.5} value={params.target_pct}
              onChange={(e) => updateParam('target_pct', e.target.value)} />
          </div>
        </div>
      </details>

      {error && <div className="error-text">{error}</div>}

      <button type="submit" className="primary" disabled={busy || disabled} style={{ width: '100%', marginTop: 16 }}>
        {busy ? 'Starting…' : `🔍 Scan for ${direction === 'long' ? 'BUY' : 'SHORT'} signals`}
      </button>
    </form>
  );
}
