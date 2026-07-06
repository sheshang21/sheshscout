import { useState } from 'react';
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

  function updateThreshold(key, value) {
    setThresholds((t) => ({ ...t, [key]: Number(value) }));
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);

    const exchanges = [nse && 'NSE', bse && 'BSE'].filter(Boolean);
    if (exchanges.length === 0) {
      setError('Pick at least one exchange.');
      return;
    }

    setBusy(true);
    try {
      const job = await api.startScan({
        exchanges,
        min_market_cap: minMarketCap,
        thresholds,
      });
      onStarted(job);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : 'Could not start the scan.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card" onSubmit={handleSubmit}>
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
