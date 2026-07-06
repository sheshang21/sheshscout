import { useEffect, useState } from 'react';
import { api } from '../api';

function formatDate(iso) {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

export default function History({ onSelect }) {
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.getHistory().then(setJobs).finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading history…</div>;
  if (jobs.length === 0) return <div className="empty-state">No scans yet. Start one from the dashboard.</div>;

  return (
    <div className="history-list">
      {jobs.map((job) => (
        <div key={job.id} className="card history-row" onClick={() => onSelect(job)}>
          <div>
            <div className="mono" style={{ fontSize: 14 }}>{job.total_stocks} stocks · {job.status}</div>
            <div className="history-meta">{formatDate(job.created_at)}</div>
          </div>
          <div className="history-meta mono">
            {job.scanned_count}/{job.total_stocks} scanned
            {job.failed_count > 0 && ` · ${job.failed_count} failed`}
          </div>
        </div>
      ))}
    </div>
  );
}
