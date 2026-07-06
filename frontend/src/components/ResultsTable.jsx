import { Fragment, useEffect, useMemo, useState } from 'react';
import { api } from '../api';

const TOP_TIER_RATINGS = new Set(['Exceptional Buy', 'Prime Buy']);

function RatingBadge({ rating }) {
  const stamped = TOP_TIER_RATINGS.has(rating);
  return <span className={`badge ${stamped ? 'stamped' : ''}`}>{rating}</span>;
}

function ChangeCell({ value }) {
  if (value == null) return <span className="text-dim">—</span>;
  return <span className={value > 0 ? 'gain' : value < 0 ? 'loss' : ''}>{value > 0 ? '+' : ''}{value.toFixed(2)}%</span>;
}

function fmtCr(value) {
  if (value == null) return '—';
  return `₹${value >= 1000 ? (value / 1000).toFixed(1) + 'k' : value.toFixed(0)} Cr`;
}

function DetailPanel({ r }) {
  const raw = r.raw_result;
  if (!raw) return <div className="detail-panel"><span className="text-dim">No breakdown available for this result.</span></div>;

  return (
    <div className="detail-panel">
      <div className="detail-grid">
        <div><label>Weekly</label><ChangeCell value={raw.weekly_change} /></div>
        <div><label>Monthly</label><ChangeCell value={raw.monthly_change} /></div>
        <div><label>3-Month</label><ChangeCell value={raw.three_month_change} /></div>
        <div><label>Upside potential</label>{raw.potential_pct != null ? `${raw.potential_pct.toFixed(1)}%` : '—'}</div>
        <div><label>RSI</label>{raw.rsi != null ? raw.rsi.toFixed(0) : '—'}</div>
        <div><label>MACD</label>{raw.macd != null ? raw.macd.toFixed(2) : '—'}</div>
        <div><label>Bollinger %B</label>{raw.bb != null ? `${raw.bb.toFixed(0)}%` : '—'}</div>
        <div><label>Volume multiple</label>{raw.vol != null ? `${raw.vol.toFixed(1)}x` : '—'}</div>
        <div><label>Trend</label>{raw.trend ?? '—'}</div>
        <div><label>YoY revenue</label>{raw.yoy_revenue_growth != null ? `${raw.yoy_revenue_growth.toFixed(1)}%` : '—'}</div>
        <div><label>YoY profit</label>{raw.yoy_profit_growth != null ? `${raw.yoy_profit_growth.toFixed(1)}%` : '—'}</div>
        <div><label>Profit margin</label>{raw.profit_margin != null ? `${raw.profit_margin.toFixed(1)}%` : '—'}</div>
      </div>

      {raw.is_operated || raw.operator_risk >= 12 ? (
        <div className="operator-warning">
          🚨 Operator risk: {raw.operator_risk}/100
          {raw.operator_flags?.length > 0 && (
            <ul>{raw.operator_flags.map((f, i) => <li key={i}>{f}</li>)}</ul>
          )}
        </div>
      ) : null}

      {raw.criteria?.length > 0 && (
        <div className="criteria-list">
          <label>Score breakdown ({raw.met_count ?? 0} met)</label>
          <ul>
            {raw.criteria.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

export default function ResultsTable({ jobId, refreshKey }) {
  const [results, setResults] = useState([]);
  const [qualifiedOnly, setQualifiedOnly] = useState(true);
  const [sortKey, setSortKey] = useState('score');
  const [sortDir, setSortDir] = useState('desc');
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(null);

  useEffect(() => {
    setLoading(true);
    api.getScanResults(jobId, { qualifiedOnly, detailed: true })
      .then(setResults)
      .finally(() => setLoading(false));
  }, [jobId, qualifiedOnly, refreshKey]);

  const sorted = useMemo(() => {
    const copy = [...results];
    copy.sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      if (av == null) return 1;
      if (bv == null) return -1;
      const cmp = typeof av === 'string' ? av.localeCompare(bv) : av - bv;
      return sortDir === 'asc' ? cmp : -cmp;
    });
    return copy;
  }, [results, sortKey, sortDir]);

  function toggleSort(key) {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  }

  function sortIndicator(key) {
    if (sortKey !== key) return '';
    return sortDir === 'asc' ? ' ↑' : ' ↓';
  }

  return (
    <div className="card">
      <div className="results-toolbar">
        <h3 style={{ fontSize: 15 }}>Results{results.length > 0 ? ` (${results.length})` : ''}</h3>
        <div className="checkbox-row" style={{ margin: 0 }}>
          <input
            id="qualified-only"
            type="checkbox"
            checked={qualifiedOnly}
            onChange={(e) => setQualifiedOnly(e.target.checked)}
          />
          <label htmlFor="qualified-only">Qualified only</label>
        </div>
      </div>

      {loading ? (
        <div className="empty-state">Loading results…</div>
      ) : sorted.length === 0 ? (
        <div className="empty-state">
          {qualifiedOnly
            ? 'Nothing qualified yet. Uncheck "Qualified only" to see everything scanned so far.'
            : 'No results yet.'}
        </div>
      ) : (
        <table className="results-table">
          <thead>
            <tr>
              <th onClick={() => toggleSort('symbol')}>Symbol{sortIndicator('symbol')}</th>
              <th onClick={() => toggleSort('score')}>Score{sortIndicator('score')}</th>
              <th>Rating</th>
              <th>Sector</th>
              <th>Price</th>
              <th>Chg</th>
              <th>Mkt Cap</th>
              <th>RSI</th>
              <th>Vol</th>
              <th>Upside</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r) => {
              const raw = r.raw_result;
              const isOpen = expanded === r.id;
              return (
                <Fragment key={r.id}>
                  <tr
                    className="clickable-row"
                    onClick={() => setExpanded(isOpen ? null : r.id)}
                  >
                    <td className="symbol-cell">{r.symbol}</td>
                    <td>{r.score?.toFixed(0)}</td>
                    <td><RatingBadge rating={r.rating} /></td>
                    <td>{r.sector}</td>
                    <td>{raw?.price != null ? raw.price.toFixed(2) : '—'}</td>
                    <td><ChangeCell value={raw?.change} /></td>
                    <td>{fmtCr(raw?.market_cap)}</td>
                    <td>{raw?.rsi != null ? raw.rsi.toFixed(0) : '—'}</td>
                    <td>{raw?.vol != null ? `${raw.vol.toFixed(1)}x` : '—'}</td>
                    <td>{raw?.potential_pct != null ? `${raw.potential_pct.toFixed(1)}%` : '—'}</td>
                  </tr>
                  {isOpen && (
                    <tr className="detail-row">
                      <td colSpan={10}><DetailPanel r={r} /></td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
