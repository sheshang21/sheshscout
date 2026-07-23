import { useEffect, useState } from 'react';
import * as XLSX from 'xlsx';
import { api } from '../api';

function SignalBadge({ signal }) {
  const cls = signal === 'STRONG' ? 'stamped' : '';
  return <span className={`badge ${cls}`}>{signal}</span>;
}

function Change({ value, digits = 2 }) {
  if (value == null) return <span className="text-dim">—</span>;
  return <span className={value > 0 ? 'gain' : value < 0 ? 'loss' : ''}>{value > 0 ? '+' : ''}{value.toFixed(digits)}%</span>;
}

function toExportRow(r) {
  const raw = r.raw_result || {};
  return {
    Ticker: raw.ticker ?? r.symbol,
    Direction: raw.direction ?? r.sector,
    'Price (₹)': raw.price ?? null,
    'Change (%)': raw.change_pct ?? null,
    'Dist from Low (%)': raw.dist_from_low ?? null,
    'Dist from High (%)': raw.dist_from_high ?? null,
    '5D Trend (%)': raw.recent_trend ?? null,
    'Vol Ratio (x)': raw.volume_ratio ?? null,
    RSI: raw.rsi ?? null,
    'ATR (%)': raw.atr_pct ?? null,
    Score: r.score ?? raw.score ?? null,
    Signal: r.rating ?? raw.signal_strength ?? null,
    'Stop Loss (₹)': raw.stop_loss ?? null,
    'Target (₹)': raw.target ?? null,
    Conditions: raw.conditions ?? null,
  };
}

function exportToExcel(rows, filename) {
  const ws = XLSX.utils.json_to_sheet(rows.map(toExportRow));
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, 'Intraday Results');
  XLSX.writeFile(wb, filename);
}

export default function IntradayResultsTable({ jobId, refreshKey, live }) {
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(true);
  const [strongOnly, setStrongOnly] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    setLoading(true);
    api.getIntradayScanResults(jobId, { qualifiedOnly: strongOnly })
      .then((rows) => { if (!cancelled) setResults(rows); })
      .catch((err) => { if (!cancelled) setError(err?.detail ? String(err.detail) : 'Could not load results.'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, refreshKey, strongOnly, live]);

  if (loading && results.length === 0) return <div className="empty-state">Loading results…</div>;
  if (error) return <div className="error-text">{error}</div>;

  const sorted = [...results].sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
  const direction = sorted[0]?.sector; // repurposed column holds "long"/"short"

  return (
    <div className="card">
      <div className="results-toolbar">
        <h3 style={{ fontSize: 15 }}>
          {sorted.length} {direction === 'short' ? 'SHORT' : 'BUY'} opportunit{sorted.length === 1 ? 'y' : 'ies'}
        </h3>
        <div className="checkbox-row">
          <input id="strong-only" type="checkbox" checked={strongOnly} onChange={(e) => setStrongOnly(e.target.checked)} />
          <label htmlFor="strong-only">STRONG only</label>
        </div>
        {sorted.length > 0 && (
          <button type="button" onClick={() => exportToExcel(sorted, `intraday_${direction || 'scan'}_${jobId}.xlsx`)}>
            ⬇ Export Excel
          </button>
        )}
      </div>

      {sorted.length === 0 ? (
        <div className="empty-state">No stocks found matching criteria{live ? ' yet…' : '.'}</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="results-table">
            <thead>
              <tr>
                <th>Ticker</th>
                <th>Price</th>
                <th>Change</th>
                <th>{direction === 'short' ? 'Dist from High' : 'Dist from Low'}</th>
                <th>5D Trend</th>
                <th>Vol Ratio</th>
                <th>RSI</th>
                <th>ATR%</th>
                <th>Score</th>
                <th>Signal</th>
                <th>Stop / Target</th>
                <th>Conditions</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((r) => {
                const raw = r.raw_result || {};
                const dist = raw.dist_from_low ?? raw.dist_from_high;
                return (
                  <tr key={r.id}>
                    <td className="mono">{raw.ticker ?? r.symbol}</td>
                    <td className="mono">₹{raw.price?.toFixed(2) ?? '—'}</td>
                    <td><Change value={raw.change_pct} /></td>
                    <td className="mono">{dist != null ? `${dist.toFixed(2)}%` : '—'}</td>
                    <td><Change value={raw.recent_trend} /></td>
                    <td className="mono">{raw.volume_ratio != null ? `${raw.volume_ratio.toFixed(2)}x` : '—'}</td>
                    <td className="mono">{raw.rsi != null ? raw.rsi.toFixed(1) : '—'}</td>
                    <td className="mono">{raw.atr_pct != null ? `${raw.atr_pct.toFixed(2)}%` : '—'}</td>
                    <td className="mono">{r.score ?? raw.score ?? '—'}</td>
                    <td><SignalBadge signal={r.rating ?? raw.signal_strength} /></td>
                    <td className="mono">
                      {raw.stop_loss != null && raw.target != null
                        ? `₹${raw.stop_loss.toFixed(2)} / ₹${raw.target.toFixed(2)}`
                        : '—'}
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--text-dim, #888)' }}>{raw.conditions}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
