import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';

const TOP_TIER_RATINGS = new Set(['Exceptional Buy', 'Prime Buy']);

function RatingBadge({ rating }) {
  const stamped = TOP_TIER_RATINGS.has(rating);
  return <span className={`badge ${stamped ? 'stamped' : ''}`}>{rating}</span>;
}

export default function ResultsTable({ jobId, refreshKey }) {
  const [results, setResults] = useState([]);
  const [qualifiedOnly, setQualifiedOnly] = useState(true);
  const [sortKey, setSortKey] = useState('score');
  const [sortDir, setSortDir] = useState('desc');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api.getScanResults(jobId, { qualifiedOnly })
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
        <h3 style={{ fontSize: 15 }}>Results</h3>
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
              <th onClick={() => toggleSort('rating')}>Rating{sortIndicator('rating')}</th>
              <th onClick={() => toggleSort('sector')}>Sector{sortIndicator('sector')}</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r) => (
              <tr key={r.id}>
                <td className="symbol-cell">{r.symbol}</td>
                <td>{r.score?.toFixed(0)}</td>
                <td><RatingBadge rating={r.rating} /></td>
                <td>{r.sector}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
