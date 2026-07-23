import { useEffect, useState } from 'react';
import './App.css';
import { api } from './api';
import AuthForm from './components/AuthForm';
import ScanForm from './components/ScanForm';
import ScanProgress from './components/ScanProgress';
import ResultsTable from './components/ResultsTable';
import IntradayScanForm from './components/IntradayScanForm';
import IntradayResultsTable from './components/IntradayResultsTable';
import History from './components/History';

export default function App() {
  const [user, setUser] = useState(undefined); // undefined = checking, null = logged out
  const [view, setView] = useState('dashboard'); // 'dashboard' | 'intraday' | 'history'
  const [activeJob, setActiveJob] = useState(null);
  const [activeIntradayJob, setActiveIntradayJob] = useState(null);
  const [resultsRefreshKey, setResultsRefreshKey] = useState(0);
  const [intradayRefreshKey, setIntradayRefreshKey] = useState(0);

  useEffect(() => {
    api.me().then(setUser).catch(() => setUser(null));
  }, []);

  async function handleLogout() {
    await api.logout().catch(() => {});
    setUser(null);
    setActiveJob(null);
    setActiveIntradayJob(null);
  }

  function handleJobUpdate(job) {
    setActiveJob(job);
    setResultsRefreshKey((k) => k + 1); // pull fresh results once the scan finishes
  }

  function handleIntradayJobUpdate(job) {
    setActiveIntradayJob(job);
    setIntradayRefreshKey((k) => k + 1);
  }

  if (user === undefined) {
    return <div className="app-shell" />; // brief auth check, avoid a flash of the login form
  }

  if (!user) {
    return <AuthForm onAuthed={setUser} />;
  }

  return (
    <div className="app-shell">
      <div className="topbar">
        <div className="wordmark">Stock<span className="accent-dot">·</span>Scout</div>
        <div className="topbar-right">
          <div className="nav-tabs">
            <button className={view === 'dashboard' ? 'active' : ''} onClick={() => setView('dashboard')}>Scan</button>
            <button className={view === 'intraday' ? 'active' : ''} onClick={() => setView('intraday')}>Intraday</button>
            <button className={view === 'history' ? 'active' : ''} onClick={() => setView('history')}>History</button>
          </div>
          <span>{user.email}</span>
          <button onClick={handleLogout}>Sign out</button>
        </div>
      </div>

      {view === 'history' ? (
        <div className="dashboard">
          <History
            onSelect={(job) => {
              if (job.scan_type === 'intraday_long' || job.scan_type === 'intraday_short') {
                setActiveIntradayJob(job);
                setView('intraday');
              } else {
                setActiveJob(job);
                setView('dashboard');
              }
            }}
          />
        </div>
      ) : view === 'intraday' ? (
        <div className="dashboard">
          <div className="sidebar">
            <IntradayScanForm onStarted={setActiveIntradayJob} disabled={activeIntradayJob?.status === 'running'} />
          </div>
          <div>
            {activeIntradayJob ? (
              <>
                <ScanProgress
                  job={activeIntradayJob}
                  onUpdate={handleIntradayJobUpdate}
                  cancelFn={api.cancelIntradayScan}
                  eventsUrlFn={api.intradayEventsUrl}
                  getFn={api.getIntradayScan}
                />
                <IntradayResultsTable
                  jobId={activeIntradayJob.id}
                  refreshKey={intradayRefreshKey}
                  live={activeIntradayJob.status === 'pending' || activeIntradayJob.status === 'running'}
                />
              </>
            ) : (
              <div className="card empty-state">
                Pick a direction, a stock range, and start a scan to see live progress and results here.
              </div>
            )}
          </div>
        </div>
      ) : (
        <div className="dashboard">
          <div className="sidebar">
            <ScanForm onStarted={setActiveJob} disabled={activeJob?.status === 'running'} />
          </div>
          <div>
            {activeJob ? (
              <>
                <ScanProgress job={activeJob} onUpdate={handleJobUpdate} />
                <ResultsTable
                  jobId={activeJob.id}
                  refreshKey={resultsRefreshKey}
                  live={activeJob.status === 'pending' || activeJob.status === 'running'}
                />
              </>
            ) : (
              <div className="card empty-state">
                Set your filters and start a scan to see live progress and results here.
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
