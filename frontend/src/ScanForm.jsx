import { useEffect, useState } from 'react';
import { api, ApiError } from '../api';

const DEFAULT_THRESHOLDS = {
  threshold_exceptional: 180,
  threshold_prime: 160,
  threshold_excellent: 140,
  threshold_strong: 120,
  rsi_low: 32,
  rsi_high: 38,
};

export default function ScanForm({ onStarted, disabled }) {
  const [nse, setNse] = useState(true);
  const [bse, setBse] = useState(false);
  const [minMarketCap, setMinMarketCap] = useState(5000);
  const [thresholds, setThresholds] = useState(DEFAULT_THRESHOLDS);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const [scanMode, setScanMode] = useState('full'); // 'full' | 'quick' | 'range' | 'custom'
  const [counts, setCounts] = useState({ NSE: 0, BSE: 0 });
  const [nseFrom, setNseFrom] = useState(1);
  const [nseTo, setNseTo] = useState(100);
  const [bseFrom, setBseFrom] = useState(1);
  const [bseTo, setBseTo] = useState(100);
  const [customList, setCustomList] = useState('');

  useEffect(() => {
    api.universeCounts().then((c) => {
      setCounts(c);
      setNseTo((v) => Math.min(v, c.NSE || v));
      setBseTo((v) => Math.min(v, c.BSE || v));
    }).catch(() => {});
  }, []);

  function updateThreshold(key, value) {
    setThresholds((t) => ({ ...t, [key]: Number(value) }));
  }

  function parseCustomList() {
    return customList
      .split('\n')
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

    const payload = { min_market_cap: minMarketCap, thresholds };

    if (scanMode === 'custom') {
      const symbols = parseCustomList();
      if (symbols.length === 0) {
        setError('Enter at least one symbol.');
        return;
      }
      payload.symbols = symbols;
    } else if (scanMode === 'range') {
      if (nse && nseFrom > nseTo) { setError("NSE 'From' must be ≤ 'To'."); return; }
      if (bse && bseFrom > bseTo) { setError("BSE 'From' must be ≤ 'To'."); return; }
      payload.exchanges = exchanges;
      payload.range = {
        ...(nse ? { NSE: [nseFrom, nseTo] } : {}),
        ...(bse ? { BSE: [bseFrom, bseTo] } : {}),
      };
    } else if (scanMode === 'quick') {
      payload.exchanges = exchanges;
      payload.range = {
        ...(nse ? { NSE: [1, Math.min(50, counts.NSE || 50)] } : {}),
        ...(bse ? { BSE: [1, Math.min(50, counts.BSE || 50)] } : {}),
      };
    } else {
      payload.exchanges = exchanges; // full scan
    }

    setBusy(true);
    try {
      const job = await api.startScan(payload);
      onStarted(job);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : 'Could not start the scan.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card scan-form" onSubmit={handleSubmit}>
      <h3 style={{ marginBottom: 16, fontSize: 15 }}>Scan setup</h3>

      <div className="field-row">
        <label>Exchanges</label>
        <div className="checkbox-row">
          <input id="nse" type="checkbox" checked={nse} onChange={(e) => setNse(e.target.checked)} />
          <label htmlFor="nse">NSE</label>
        </div>
        <div className="checkbox-row">
          <input id="bse" type="checkbox" checked={bse} onChange={(e) => setBse(e.target.checked)} />
          <label htmlFor="bse">BSE</label>
        </div>
      </div>

      <div className="field-row">
        <label htmlFor="scan-mode">Scan mode</label>
        <select id="scan-mode" value={scanMode} onChange={(e) => setScanMode(e.target.value)}>
          <option value="full">Full scan (all stocks)</option>
          <option value="quick">Quick scan (first 50)</option>
          <option value="range">Range scan (row numbers)</option>
          <option value="custom">Custom list</option>
        </select>
      </div>

      {scanMode === 'range' && (
        <div className="field-row">
          <label>Row range (1-based, from nse.txt / bse.txt)</label>
          {nse && (
            <div className="threshold-grid" style={{ marginBottom: 8 }}>
              <div>
                <label htmlFor="nse-from">NSE from ({counts.NSE || '?'} total)</label>
                <input id="nse-from" type="number" min={1} max={counts.NSE || undefined} value={nseFrom}
                  onChange={(e) => setNseFrom(Number(e.target.value))} />
              </div>
              <div>
                <label htmlFor="nse-to">NSE to</label>
                <input id="nse-to" type="number" min={1} max={counts.NSE || undefined} value={nseTo}
                  onChange={(e) => setNseTo(Number(e.target.value))} />
              </div>
            </div>
          )}
          {bse && (
            <div className="threshold-grid">
              <div>
                <label htmlFor="bse-from">BSE from ({counts.BSE || '?'} total)</label>
                <input id="bse-from" type="number" min={1} max={counts.BSE || undefined} value={bseFrom}
                  onChange={(e) => setBseFrom(Number(e.target.value))} />
              </div>
              <div>
                <label htmlFor="bse-to">BSE to</label>
                <input id="bse-to" type="number" min={1} max={counts.BSE || undefined} value={bseTo}
                  onChange={(e) => setBseTo(Number(e.target.value))} />
              </div>
            </div>
          )}
        </div>
      )}

      {scanMode === 'custom' && (
        <div className="field-row">
          <label htmlFor="custom-list">Symbols (one per line)</label>
          <textarea id="custom-list" rows={5} placeholder={'RELIANCE.NS\nTCS.BO\nINFY'}
            value={customList} onChange={(e) => setCustomList(e.target.value)} />
        </div>
      )}

      <div className="field-row">
        <label htmlFor="mcap">Minimum market cap (₹ Cr)</label>
        <input
          id="mcap"
          type="number"
          min={0}
          step={500}
          value={minMarketCap}
          onChange={(e) => setMinMarketCap(Number(e.target.value))}
        />
      </div>

      <details className="advanced">
        <summary>Advanced thresholds</summary>
        <div className="threshold-grid">
          <div>
            <label htmlFor="th-strong">Strong buy score</label>
            <input id="th-strong" type="number" value={thresholds.threshold_strong}
              onChange={(e) => updateThreshold('threshold_strong', e.target.value)} />
          </div>
          <div>
            <label htmlFor="th-excellent">Excellent buy score</label>
            <input id="th-excellent" type="number" value={thresholds.threshold_excellent}
              onChange={(e) => updateThreshold('threshold_excellent', e.target.value)} />
          </div>
          <div>
            <label htmlFor="th-prime">Prime buy score</label>
            <input id="th-prime" type="number" value={thresholds.threshold_prime}
              onChange={(e) => updateThreshold('threshold_prime', e.target.value)} />
          </div>
          <div>
            <label htmlFor="th-exceptional">Exceptional score</label>
            <input id="th-exceptional" type="number" value={thresholds.threshold_exceptional}
              onChange={(e) => updateThreshold('threshold_exceptional', e.target.value)} />
          </div>
          <div>
            <label htmlFor="rsi-low">RSI low</label>
            <input id="rsi-low" type="number" value={thresholds.rsi_low}
              onChange={(e) => updateThreshold('rsi_low', e.target.value)} />
          </div>
          <div>
            <label htmlFor="rsi-high">RSI high</label>
            <input id="rsi-high" type="number" value={thresholds.rsi_high}
              onChange={(e) => updateThreshold('rsi_high', e.target.value)} />
          </div>
        </div>
      </details>

      {error && <div className="error-text">{error}</div>}

      <button type="submit" className="primary" disabled={busy || disabled} style={{ width: '100%', marginTop: 16 }}>
        {busy ? 'Starting…' : 'Start scan'}
      </button>
    </form>
  );
}
