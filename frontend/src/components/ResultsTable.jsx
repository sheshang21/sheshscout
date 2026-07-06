import { Fragment, useEffect, useMemo, useState } from 'react';
import * as XLSX from 'xlsx';
import { api } from '../api';

const RATING_ORDER = ['Exceptional Buy', 'Prime Buy', 'Excellent Buy', 'Strong Buy', 'Good Buy', 'Watchlist', 'Skip', 'Operated - Avoid'];
const TOP_TIER_RATINGS = new Set(['Exceptional Buy', 'Prime Buy']);

function RatingBadge({ rating }) {
  const stamped = TOP_TIER_RATINGS.has(rating);
  return <span className={`badge ${stamped ? 'stamped' : ''}`}>{rating}</span>;
}

function Change({ value, digits = 2 }) {
  if (value == null) return <span className="text-dim">—</span>;
  return <span className={value > 0 ? 'gain' : value < 0 ? 'loss' : ''}>{value > 0 ? '+' : ''}{value.toFixed(digits)}%</span>;
}

function num(v, digits = 2, suffix = '') {
  return v == null ? '—' : `${v.toFixed(digits)}${suffix}`;
}

function exchangeOf(symbol) {
  if (symbol?.endsWith('.NS')) return 'NSE';
  if (symbol?.endsWith('.BO')) return 'BSE';
  return 'N/A';
}

/* Flattens a result row to the same column set/order as the original
   Streamlit dataframe, for Excel export. */
function toExportRow(r) {
  const raw = r.raw_result || {};
  return {
    Symbol: r.symbol,
    Exchange: exchangeOf(r.symbol),
    'Price (₹)': raw.price ?? null,
    'Today (%)': raw.change ?? null,
    'Weekly (%)': raw.weekly_change ?? null,
    'Monthly (%)': raw.monthly_change ?? null,
    '3M (%)': raw.three_month_change ?? null,
    'Market Cap (₹Cr)': raw.market_cap ?? null,
    'Cash on Hand (₹Cr)': raw.total_cash ? raw.total_cash / 10000000 : null,
    'Cash/MCap (%)': raw.cash_on_hand_to_mcap ?? null,
    'LatestFY Rev/MCap': raw.latest_fy_revenue_to_mcap ?? null,
    'Rev YoY (%)': raw.yoy_revenue_growth ?? null,
    'Rev QoQ (%)': raw.qoq_revenue_growth ?? null,
    'Profit YoY (%)': raw.yoy_profit_growth ?? null,
    'Profit QoQ (%)': raw.qoq_profit_growth ?? null,
    'Margin (%)': raw.profit_margin ?? null,
    RSI: raw.rsi ?? null,
    MACD: raw.macd ?? null,
    'BB (%)': raw.bb ?? null,
    'Vol (x)': raw.vol ?? null,
    'Upside (%)': raw.potential_pct ?? null,
    Score: r.score ?? null,
    Rating: r.rating,
    Status: raw.status ?? null,
    Sector: r.sector,
    Operated: raw.is_operated ? 'YES' : 'Safe',
    Risk: raw.operator_risk ?? null,
  };
}

function exportToExcel(rows, filename) {
  const ws = XLSX.utils.json_to_sheet(rows.map(toExportRow));
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, 'Results');
  XLSX.writeFile(wb, filename);
}

/* ── Tiny dependency-free SVG bar/line charts, styled to match the app's
   ledger theme rather than pulling in a charting library for 3 sparklines. ── */
function HistoricalCharts({ historical }) {
  if (!historical?.years?.length) {
    return <div className="empty-state" style={{ padding: '16px 0' }}>Historical data not available for this stock.</div>;
  }
  const { years, revenues = [], cash_amounts = [], sales_to_mcap = [] } = historical;
  const W = 560, H = 120, PAD = 28;

  function BarChart({ title, values, color }) {
    const cr = values.map((v) => v / 10000000);
    const max = Math.max(...cr, 1);
    const bw = (W - PAD * 2) / cr.length;
    return (
      <div className="chart-block">
        <label>{title}</label>
        <svg viewBox={`0 0 ${W} ${H}`} className="mini-chart">
          {cr.map((v, i) => {
            const h = (v / max) * (H - PAD - 16);
            const x = PAD + i * bw + bw * 0.15;
            const w = bw * 0.7;
            const y = H - PAD - h;
            return (
              <g key={i}>
                <rect x={x} y={y} width={w} height={h} fill={color} rx="2" />
                <text x={x + w / 2} y={y - 4} textAnchor="middle" className="chart-label">₹{v.toFixed(0)}Cr</text>
                <text x={x + w / 2} y={H - 10} textAnchor="middle" className="chart-axis">{years[i]}</text>
              </g>
            );
          })}
        </svg>
      </div>
    );
  }

  function LineChart({ title, values }) {
    const max = Math.max(...values, 0.1);
    const step = (W - PAD * 2) / Math.max(values.length - 1, 1);
    const pts = values.map((v, i) => [PAD + i * step, H - PAD - (v / max) * (H - PAD - 16)]);
    const path = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0]},${p[1]}`).join(' ');
    return (
      <div className="chart-block">
        <label>{title}</label>
        <svg viewBox={`0 0 ${W} ${H}`} className="mini-chart">
          <path d={path} fill="none" stroke="var(--accent)" strokeWidth="2" />
          {pts.map(([x, y], i) => (
            <g key={i}>
              <circle cx={x} cy={y} r="3.5" fill="var(--accent)" />
              <text x={x} y={y - 8} textAnchor="middle" className="chart-label">{values[i].toFixed(2)}x</text>
              <text x={x} y={H - 10} textAnchor="middle" className="chart-axis">{years[i]}</text>
            </g>
          ))}
        </svg>
      </div>
    );
  }

  return (
    <div className="charts-grid">
      {revenues.length > 0 && <BarChart title="Revenue (₹ Cr)" values={revenues} color="#5b8bb0" />}
      {cash_amounts.length > 0 && <BarChart title="Cash (₹ Cr)" values={cash_amounts} color="var(--gain)" />}
      {sales_to_mcap.length > 0 && <LineChart title="Sales / Market Cap" values={sales_to_mcap} />}
    </div>
  );
}

function DetailPanel({ r }) {
  const raw = r.raw_result;
  if (!raw) return <div className="detail-panel"><span className="text-dim">No breakdown available for this result.</span></div>;

  return (
    <div className="detail-panel">
      {(raw.is_operated || raw.operator_risk >= 12) && (
        <div className="operator-warning">
          🚨 Operator risk: {raw.operator_risk}/100 — {raw.status}
          {raw.operator_flags?.length > 0 && <ul>{raw.operator_flags.map((f, i) => <li key={i}>{f}</li>)}</ul>}
        </div>
      )}

      <div className="metric-row">
        <div><label>Score</label><span className="metric-big">{raw.score}</span></div>
        <div><label>Price</label><span className="metric-big">₹{num(raw.price)}</span></div>
        <div><label>Market cap</label><span className="metric-big">₹{num(raw.market_cap, 0)}Cr</span></div>
        <div><label>Rev YoY</label><span className="metric-big"><Change value={raw.yoy_revenue_growth} digits={1} /></span></div>
        <div><label>Profit YoY</label><span className="metric-big"><Change value={raw.yoy_profit_growth} digits={1} /></span></div>
      </div>

      <label className="section-label">Financial ratios</label>
      <div className="metric-row">
        <div><label>Cash on hand</label>₹{num((raw.total_cash || 0) / 10000000, 0)}Cr</div>
        <div><label>Cash / Mkt cap</label>{num(raw.cash_on_hand_to_mcap)}%</div>
        <div><label>Latest FY rev / Mkt cap</label>{num(raw.latest_fy_revenue_to_mcap)}x</div>
        <div><label>Profit margin</label>{num(raw.profit_margin, 1)}%</div>
        <div><label>Upside potential</label>{num(raw.potential_pct, 1)}% (₹{num(raw.potential_rs, 0)})</div>
      </div>

      <label className="section-label">Technicals</label>
      <div className="metric-row">
        <div><label>Weekly</label><Change value={raw.weekly_change} /></div>
        <div><label>Monthly</label><Change value={raw.monthly_change} /></div>
        <div><label>3-Month</label><Change value={raw.three_month_change} /></div>
        <div><label>RSI</label>{num(raw.rsi, 0)}</div>
        <div><label>MACD</label>{num(raw.macd)}</div>
        <div><label>Bollinger %B</label>{num(raw.bb, 0)}%</div>
        <div><label>Volume</label>{num(raw.vol, 1)}x</div>
        <div><label>Trend</label>{raw.trend ?? '—'}</div>
      </div>

      <label className="section-label">3-year historical trends</label>
      <HistoricalCharts historical={raw.historical_data} />

      {raw.criteria?.length > 0 && (
        <>
          <label className="section-label">Score breakdown ({raw.met_count ?? 0} criteria met)</label>
          <ul className="criteria-list">
            {raw.criteria.map((c, i) => (
              <li key={i} className={c.includes('🚨') ? 'crit-danger' : c.includes('✅') ? 'crit-good' : c.includes('⚠') ? 'crit-warn' : 'crit-bad'}>
                {c}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function StatsBar({ results }) {
  const stats = useMemo(() => {
    const total = results.length;
    const operated = results.filter((r) => r.raw_result?.is_operated).length;
    const byRating = Object.fromEntries(RATING_ORDER.map((rt) => [rt, results.filter((r) => r.rating === rt).length]));
    const nse = results.filter((r) => exchangeOf(r.symbol) === 'NSE').length;
    const bse = results.filter((r) => exchangeOf(r.symbol) === 'BSE').length;
    const qualified = results.filter((r) => r.qualified).length;
    return { total, operated, byRating, nse, bse, qualified };
  }, [results]);

  if (results.length === 0) return null;

  const pct = stats.total ? ((stats.qualified / stats.total) * 100).toFixed(1) : '0.0';

  return (
    <div className="stats-bar mono">
      <span>Total <b>{stats.total}</b></span>
      <span className="loss">Operated <b>{stats.operated}</b></span>
      <span>Exceptional <b>{stats.byRating['Exceptional Buy']}</b></span>
      <span>Prime <b>{stats.byRating['Prime Buy']}</b></span>
      <span>Excellent <b>{stats.byRating['Excellent Buy']}</b></span>
      <span>Strong <b>{stats.byRating['Strong Buy']}</b></span>
      <span>NSE <b>{stats.nse}</b></span>
      <span>BSE <b>{stats.bse}</b></span>
      <span className="gain">Qualified <b>{stats.qualified}</b> ({pct}%)</span>
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

  const [ratingFilter, setRatingFilter] = useState(new Set(RATING_ORDER));
  const [exchangeFilter, setExchangeFilter] = useState(new Set(['NSE', 'BSE']));
  const [safetyFilter, setSafetyFilter] = useState(new Set(['Safe', 'Operated']));
  const [sectorFilter, setSectorFilter] = useState(null); // null = all
  const [minScore, setMinScore] = useState(0);

  useEffect(() => {
    setLoading(true);
    api.getScanResults(jobId, { qualifiedOnly, detailed: true })
      .then((r) => { setResults(r); setExpanded(null); })
      .finally(() => setLoading(false));
  }, [jobId, qualifiedOnly, refreshKey]);

  const sectors = useMemo(() => [...new Set(results.map((r) => r.sector).filter(Boolean))].sort(), [results]);

  const filtered = useMemo(() => {
    return results.filter((r) =>
      ratingFilter.has(r.rating) &&
      exchangeFilter.has(exchangeOf(r.symbol)) &&
      safetyFilter.has(r.raw_result?.is_operated ? 'Operated' : 'Safe') &&
      (sectorFilter == null || sectorFilter.has(r.sector)) &&
      (r.score ?? 0) >= minScore
    );
  }, [results, ratingFilter, exchangeFilter, safetyFilter, sectorFilter, minScore]);

  const sorted = useMemo(() => {
    const copy = [...filtered];
    copy.sort((a, b) => {
      const av = a[sortKey] ?? a.raw_result?.[sortKey];
      const bv = b[sortKey] ?? b.raw_result?.[sortKey];
      if (av == null) return 1;
      if (bv == null) return -1;
      const cmp = typeof av === 'string' ? av.localeCompare(bv) : av - bv;
      return sortDir === 'asc' ? cmp : -cmp;
    });
    return copy;
  }, [filtered, sortKey, sortDir]);

  function toggleSort(key) {
    if (sortKey === key) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    else { setSortKey(key); setSortDir('desc'); }
  }
  function sortIndicator(key) {
    return sortKey !== key ? '' : sortDir === 'asc' ? ' ↑' : ' ↓';
  }
  function toggleSetValue(setFn, set, value) {
    const next = new Set(set);
    next.has(value) ? next.delete(value) : next.add(value);
    setFn(next);
  }

  const columns = [
    ['score', 'Score'], ['rating', 'Rating'], ['sector', 'Sector'],
    ['price', 'Price'], ['change', 'Today'], ['weekly_change', 'Week'], ['monthly_change', 'Month'], ['three_month_change', '3M'],
    ['market_cap', 'Mkt Cap'], ['cash_on_hand_to_mcap', 'Cash/MCap'], ['latest_fy_revenue_to_mcap', 'Rev/MCap'],
    ['yoy_revenue_growth', 'Rev YoY'], ['qoq_revenue_growth', 'Rev QoQ'], ['yoy_profit_growth', 'Profit YoY'], ['qoq_profit_growth', 'Profit QoQ'],
    ['profit_margin', 'Margin'], ['rsi', 'RSI'], ['macd', 'MACD'], ['bb', 'BB'], ['vol', 'Vol'], ['potential_pct', 'Upside'],
    ['operator_risk', 'Risk'],
  ];

  return (
    <div className="card">
      <div className="results-toolbar">
        <h3 style={{ fontSize: 15 }}>Results{results.length > 0 ? ` (${sorted.length}/${results.length})` : ''}</h3>
        <div className="checkbox-row" style={{ margin: 0 }}>
          <input id="qualified-only" type="checkbox" checked={qualifiedOnly} onChange={(e) => setQualifiedOnly(e.target.checked)} />
          <label htmlFor="qualified-only">Qualified only</label>
        </div>
      </div>

      <StatsBar results={results} />

      {results.length > 0 && (
        <details className="advanced" open>
          <summary>Filters</summary>
          <div className="filters-bar">
            <div className="filter-group">
              <label>Rating</label>
              {RATING_ORDER.map((rt) => (
                <div className="checkbox-row" key={rt}>
                  <input id={`flt-rating-${rt}`} type="checkbox" checked={ratingFilter.has(rt)}
                    onChange={() => toggleSetValue(setRatingFilter, ratingFilter, rt)} />
                  <label htmlFor={`flt-rating-${rt}`}>{rt}</label>
                </div>
              ))}
            </div>
            <div className="filter-group">
              <label>Exchange</label>
              {['NSE', 'BSE'].map((ex) => (
                <div className="checkbox-row" key={ex}>
                  <input id={`flt-ex-${ex}`} type="checkbox" checked={exchangeFilter.has(ex)}
                    onChange={() => toggleSetValue(setExchangeFilter, exchangeFilter, ex)} />
                  <label htmlFor={`flt-ex-${ex}`}>{ex}</label>
                </div>
              ))}
            </div>
            <div className="filter-group">
              <label>Safety</label>
              {['Safe', 'Operated'].map((s) => (
                <div className="checkbox-row" key={s}>
                  <input id={`flt-safety-${s}`} type="checkbox" checked={safetyFilter.has(s)}
                    onChange={() => toggleSetValue(setSafetyFilter, safetyFilter, s)} />
                  <label htmlFor={`flt-safety-${s}`}>{s === 'Safe' ? '✅ Safe' : '🚨 Operated'}</label>
                </div>
              ))}
            </div>
            <div className="filter-group">
              <label htmlFor="flt-sector">Sector</label>
              <select id="flt-sector" value={sectorFilter ? [...sectorFilter][0] ?? '__all' : '__all'}
                onChange={(e) => setSectorFilter(e.target.value === '__all' ? null : new Set([e.target.value]))}>
                <option value="__all">All sectors</option>
                {sectors.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            <div className="filter-group">
              <label htmlFor="flt-minscore">Min score</label>
              <input id="flt-minscore" type="number" min={0} max={250} step={10} value={minScore}
                onChange={(e) => setMinScore(Number(e.target.value))} />
            </div>
            <div className="filter-group">
              <label>&nbsp;</label>
              <button type="button" onClick={() => {
                setRatingFilter(new Set(RATING_ORDER));
                setExchangeFilter(new Set(['NSE', 'BSE']));
                setSafetyFilter(new Set(['Safe', 'Operated']));
                setSectorFilter(null);
                setMinScore(0);
              }}>Reset filters</button>
            </div>
          </div>
        </details>
      )}

      {results.length > 0 && (
        <div className="export-row">
          <button type="button" onClick={() => exportToExcel(sorted, `sheshscout-filtered-${jobId}.xlsx`)} disabled={sorted.length === 0}>
            ⬇ Export filtered ({sorted.length})
          </button>
          <button type="button" onClick={() => exportToExcel(results, `sheshscout-all-${jobId}.xlsx`)} disabled={results.length === 0}>
            ⬇ Export all loaded ({results.length})
          </button>
        </div>
      )}

      {loading ? (
        <div className="empty-state">Loading results…</div>
      ) : sorted.length === 0 ? (
        <div className="empty-state">
          {qualifiedOnly ? 'Nothing qualified yet. Uncheck "Qualified only" to see everything scanned so far.' : 'No results match the current filters.'}
        </div>
      ) : (
        <div className="table-scroll">
          <table className="results-table">
            <thead>
              <tr>
                <th className="sticky-col" onClick={() => toggleSort('symbol')}>Symbol{sortIndicator('symbol')}</th>
                {columns.map(([key, label]) => (
                  <th key={key} onClick={() => toggleSort(key)}>{label}{sortIndicator(key)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.map((r) => {
                const raw = r.raw_result || {};
                const isOpen = expanded === r.id;
                return (
                  <Fragment key={r.id}>
                    <tr className="clickable-row" onClick={() => setExpanded(isOpen ? null : r.id)}>
                      <td className="symbol-cell sticky-col">{r.symbol} <span className="text-faint">{exchangeOf(r.symbol)}</span></td>
                      <td>{r.score?.toFixed(0)}</td>
                      <td><RatingBadge rating={r.rating} /></td>
                      <td>{r.sector}</td>
                      <td>₹{num(raw.price)}</td>
                      <td><Change value={raw.change} /></td>
                      <td><Change value={raw.weekly_change} /></td>
                      <td><Change value={raw.monthly_change} /></td>
                      <td><Change value={raw.three_month_change} /></td>
                      <td>₹{num(raw.market_cap, 0)}Cr</td>
                      <td>{num(raw.cash_on_hand_to_mcap)}%</td>
                      <td>{num(raw.latest_fy_revenue_to_mcap)}x</td>
                      <td><Change value={raw.yoy_revenue_growth} digits={1} /></td>
                      <td><Change value={raw.qoq_revenue_growth} digits={1} /></td>
                      <td><Change value={raw.yoy_profit_growth} digits={1} /></td>
                      <td><Change value={raw.qoq_profit_growth} digits={1} /></td>
                      <td>{num(raw.profit_margin, 1)}%</td>
                      <td>{num(raw.rsi, 0)}</td>
                      <td>{num(raw.macd)}</td>
                      <td>{num(raw.bb, 0)}%</td>
                      <td>{num(raw.vol, 1)}x</td>
                      <td>{num(raw.potential_pct, 1)}%</td>
                      <td className={raw.is_operated ? 'loss' : raw.operator_risk >= 20 ? 'loss' : raw.operator_risk >= 12 ? '' : 'gain'}>
                        {raw.is_operated ? '🚨' : raw.operator_risk ?? 0}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr className="detail-row"><td colSpan={columns.length + 1}><DetailPanel r={r} /></td></tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
