import { useEffect, useRef, useState } from 'react';
import { api } from '../api';

const TERMINAL_STATES = new Set(['completed', 'failed', 'cancelled']);

function notifyDone(job) {
  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
  const title = job.status === 'completed' ? 'Scan finished' : 'Scan failed';
  const body = job.status === 'completed'
    ? `${job.scanned_count} stocks scanned, ${job.failed_count} failed to fetch.`
    : job.error_message || 'The scan did not complete.';
  new Notification(title, { body });
}

// `cancelFn`/`eventsUrlFn`/`getFn` default to the positional /scans
// endpoints so existing callers don't need to change; App.jsx's intraday
// view passes the /intraday-scans equivalents so a "Stop scan" click (or
// the SSE stream / fallback poll below) hits the router that actually
// owns that job's scan_type instead of crossing into /scans by accident.
export default function ScanProgress({
  job,
  onUpdate,
  cancelFn = api.cancelScan,
  eventsUrlFn = api.eventsUrl,
  getFn = api.getScan,
}) {
  const [snapshot, setSnapshot] = useState(job);
  const [stopping, setStopping] = useState(false);
  const notifiedRef = useRef(false);

  async function handleStop() {
    setStopping(true);
    try {
      const fresh = await cancelFn(job.id);
      setSnapshot(fresh);
      onUpdate?.(fresh);
    } catch {
      // ignore -- the poll loop / SSE stream will reflect the real state either way
    } finally {
      setStopping(false);
    }
  }

  useEffect(() => {
    setSnapshot(job);
    notifiedRef.current = false;

    if (TERMINAL_STATES.has(job.status)) return;

    // Ask once per session, not on every scan -- avoids a permission
    // prompt firing every time someone clicks "Start scan."
    if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
      Notification.requestPermission();
    }

    const source = new EventSource(eventsUrlFn(job.id), { withCredentials: true });

    source.onmessage = (event) => {
      const data = JSON.parse(event.data);
      setSnapshot((prev) => ({ ...prev, ...data }));
      if (TERMINAL_STATES.has(data.status) && !notifiedRef.current) {
        notifiedRef.current = true;
        notifyDone({ ...job, ...data });
        onUpdate?.({ ...job, ...data });
        source.close();
      }
    };

    source.onerror = () => {
      // Connection dropped (e.g. server restart) -- fall back to a single
      // status poll rather than leaving the UI stuck on a stale snapshot.
      source.close();
      getFn(job.id).then((fresh) => {
        setSnapshot(fresh);
        onUpdate?.(fresh);
      }).catch(() => {});
    };

    return () => source.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id]);

  const pct = snapshot.total_stocks > 0
    ? Math.round((snapshot.scanned_count / snapshot.total_stocks) * 100)
    : 0;

  return (
    <div className="card progress-card">
      <div className="progress-header">
        <span className="progress-status">{snapshot.status}</span>
        <span className="progress-counts">
          <span className="tick" key={snapshot.scanned_count}>{snapshot.scanned_count}</span>
          <span className="of"> / {snapshot.total_stocks}</span>
        </span>
        {!TERMINAL_STATES.has(snapshot.status) && (
          <button type="button" className="stop-scan-btn" onClick={handleStop} disabled={stopping}>
            {stopping ? 'Stopping…' : '■ Stop scan'}
          </button>
        )}
      </div>
      <div className="progress-bar-track">
        <div className="progress-bar-fill" style={{ width: `${pct}%` }} />
      </div>
      <div className="progress-meta">
        <span>{snapshot.failed_count} fetch failures</span>
        {snapshot.error_message && <span className="loss">{snapshot.error_message}</span>}
      </div>
    </div>
  );
}
