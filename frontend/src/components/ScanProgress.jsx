import { useEffect, useRef, useState } from 'react';
import { api } from '../api';

const TERMINAL_STATES = new Set(['completed', 'failed', 'cancelled']);

// A scan job can go 'failed' for two very different reasons:
//   1. The backend hit MEMORY_CEILING_MB (app/scan_runner.py) and stopped
//      itself ON PURPOSE, with all progress saved -- this is not a crash,
//      it's a safety net for Render's free-tier 512MB limit. /resume picks
//      up exactly where it left off. For a full-universe scan this can
//      legitimately happen many times in a row, so it's auto-resumed below
//      instead of making the user notice and click through it repeatedly.
//   2. Something actually went wrong (DB error, unexpected exception, etc)
//      -- these still need a human to look, so they get a manual Resume
//      button instead of being silently retried forever.
const MAX_AUTO_RESUMES = 40; // generous ceiling for a full NSE+BSE universe
                              // scanned in ~100-stock chunks; a real repeat
                              // failure will hit this and surface itself
                              // instead of looping invisibly forever
function isMemoryCeilingStop(job) {
  return job.status === 'failed' && /memory|ceiling/i.test(job.error_message || '');
}

function notifyDone(job) {
  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
  const title = job.status === 'completed' ? 'Scan finished' : 'Scan failed';
  const body = job.status === 'completed'
    ? `${job.scanned_count} stocks scanned, ${job.failed_count} failed to fetch.`
    : job.error_message || 'The scan did not complete.';
  new Notification(title, { body });
}

// `cancelFn`/`resumeFn`/`eventsUrlFn`/`getFn` default to the positional
// /scans endpoints so existing callers don't need to change; App.jsx's
// intraday view passes the /intraday-scans equivalents so a "Stop scan"
// click (or the SSE stream / fallback poll below) hits the router that
// actually owns that job's scan_type instead of crossing into /scans by
// accident.
export default function ScanProgress({
  job,
  onUpdate,
  cancelFn = api.cancelScan,
  resumeFn = api.resumeScan,
  eventsUrlFn = api.eventsUrl,
  getFn = api.getScan,
}) {
  const [snapshot, setSnapshot] = useState(job);
  const [stopping, setStopping] = useState(false);
  const [resuming, setResuming] = useState(false);
  const notifiedRef = useRef(false);
  const autoResumeCountRef = useRef(0);
  // Bumped every time we kick off a resume, so the SSE effect below
  // re-subscribes even though job.id itself never changes across a resume.
  const [resumeGen, setResumeGen] = useState(0);

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

  async function doResume() {
    setResuming(true);
    try {
      const fresh = await resumeFn(job.id);
      setSnapshot(fresh);
      onUpdate?.(fresh);
      notifiedRef.current = false;
      setResumeGen((g) => g + 1);
    } catch {
      // Leave the failed state showing -- the manual Resume button (if
      // this wasn't an auto-resume case) stays available to retry.
    } finally {
      setResuming(false);
    }
  }

  useEffect(() => {
    setSnapshot(job);
    notifiedRef.current = false;
    autoResumeCountRef.current = 0;

    if (TERMINAL_STATES.has(job.status)) return;

    // Ask once per session, not on every scan -- avoids a permission
    // prompt firing every time someone clicks "Start scan."
    if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
      Notification.requestPermission();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id]);

  useEffect(() => {
    if (TERMINAL_STATES.has(snapshot.status) && snapshot.status !== 'failed') return;
    if (snapshot.status === 'failed' && !isMemoryCeilingStop(snapshot)) return;

    const source = new EventSource(eventsUrlFn(job.id), { withCredentials: true });

    source.onmessage = (event) => {
      const data = JSON.parse(event.data);
      setSnapshot((prev) => ({ ...prev, ...data }));

      if (data.status === 'failed' && isMemoryCeilingStop(data)) {
        source.close();
        if (autoResumeCountRef.current < MAX_AUTO_RESUMES) {
          autoResumeCountRef.current += 1;
          doResume();
        } else if (!notifiedRef.current) {
          // Bailed out of auto-resuming -- surface it like any other
          // terminal failure rather than looping silently forever.
          notifiedRef.current = true;
          notifyDone({ ...job, ...data });
          onUpdate?.({ ...job, ...data });
        }
        return;
      }

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
        if (fresh.status === 'failed' && isMemoryCeilingStop(fresh) &&
            autoResumeCountRef.current < MAX_AUTO_RESUMES) {
          autoResumeCountRef.current += 1;
          doResume();
        }
      }).catch(() => {});
    };

    return () => source.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id, resumeGen]);

  const pct = snapshot.total_stocks > 0
    ? Math.round((snapshot.scanned_count / snapshot.total_stocks) * 100)
    : 0;
  const needsManualResume = snapshot.status === 'failed' && !isMemoryCeilingStop(snapshot);
  const autoResuming = snapshot.status === 'failed' && isMemoryCeilingStop(snapshot);

  return (
    <div className="card progress-card">
      <div className="progress-header">
        <span className="progress-status">
          {autoResuming || resuming ? 'resuming…' : snapshot.status}
        </span>
        <span className="progress-counts">
          <span className="tick" key={snapshot.scanned_count}>{snapshot.scanned_count}</span>
          <span className="of"> / {snapshot.total_stocks}</span>
        </span>
        {!TERMINAL_STATES.has(snapshot.status) && (
          <button type="button" className="stop-scan-btn" onClick={handleStop} disabled={stopping}>
            {stopping ? 'Stopping…' : '■ Stop scan'}
          </button>
        )}
        {needsManualResume && (
          <button type="button" className="stop-scan-btn" onClick={doResume} disabled={resuming}>
            {resuming ? 'Resuming…' : '▶ Resume scan'}
          </button>
        )}
      </div>
      <div className="progress-bar-track">
        <div className="progress-bar-fill" style={{ width: `${pct}%` }} />
      </div>
      <div className="progress-meta">
        <span>{snapshot.failed_count} fetch failures</span>
        {needsManualResume && snapshot.error_message && (
          <span className="loss">{snapshot.error_message}</span>
        )}
      </div>
    </div>
  );
}
