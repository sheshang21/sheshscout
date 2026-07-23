import { useEffect, useState } from 'react';
import { api } from '../api';

function formatDate(iso) {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

const SCAN_TYPE_LABEL = {
  positional: 'Positional',
  intraday_long: 'Intraday · Long',
  intraday_short: 'Intraday · Short',
};

export default function History({ onSelect }) {
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    // Two separate history endpoints (positional vs intraday, see
    // app/routers/scans.py and app/routers/intraday_scans.py) -- merged
    // here so History reads as one combined timeline instead of the
    // intraday screeners silently missing from it.
    Promise.all([api.getHistory(), api.getIntradayHistory()])
      .then(([positional, intraday]) => {
        const merged = [...positional, ...intraday].sort(
          (a, b) => new Date(b.created_at) - new Date(a.created_at)
        );
        setJobs(merged);
      })
      .finally(() => setLoading(false));
  }, []);

  async function handleClear() {
    if (!window.confirm('Clear all scan history (positional + intraday)? This cannot be undone.')) return;
    setError(null);
    setClearing(true);
    try {
      await Promise.all([api.clearHistory(), api.clearIntradayHistory()]);
      setJobs([]);
    } catch (err) {
      setError(err?.detail ? String(err.detail) : 'Could not clear history.');
    } finally {
      setClearing(false);
    }
  }

  if (loading) return <div className="empty-state">Loading history…</div>;

  return (
    <div>
      <div className="results-toolbar">
        <h3 style={{ fontSize: 15 }}>History</h3>
        {jobs.length > 0 && (
          <button type="button" onClick={handleClear} disabled={clearing}>
            {clearing ? 'Clearing…' : '🗑 Clear history'}
          </button>
        )}
      </div>
      {error && <div className="error-text">{error}</div>}
      {jobs.length === 0 ? (
        <div className="empty-state">No scans yet. Start one from the dashboard.</div>
      ) : (
        <div className="history-list">
          {jobs.map((job) => (
            <div key={job.id} className="card history-row" onClick={() => onSelect(job)}>
              <div>
                <div className="mono" style={{ fontSize: 14 }}>
                  {SCAN_TYPE_LABEL[job.scan_type] || job.scan_type} · {job.total_stocks} stocks · {job.status}
                </div>
                <div className="history-meta">{formatDate(job.created_at)}</div>
              </div>
              <div className="history-meta mono">
                {job.scanned_count}/{job.total_stocks} scanned
                {job.failed_count > 0 && ` · ${job.failed_count} failed`}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
