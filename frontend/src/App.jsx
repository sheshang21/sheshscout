import { useEffect, useState } from 'react';
import './App.css';
import { api } from './api';
import AuthForm from './components/AuthForm';
import ScanForm from './components/ScanForm';
import ScanProgress from './components/ScanProgress';
import ResultsTable from './components/ResultsTable';
import History from './components/History';

export default function App() {
  const [user, setUser] = useState(undefined); // undefined = checking, null = logged out
  const [view, setView] = useState('dashboard'); // 'dashboard' | 'history'
  const [activeJob, setActiveJob] = useState(null);
  const [resultsRefreshKey, setResultsRefreshKey] = useState(0);

  useEffect(() => {
    api.me().then(setUser).catch(() => setUser(null));
  }, []);

  async function handleLogout() {
    await api.logout().catch(() => {});
    setUser(null);
    setActiveJob(null);
  }

  function handleJobUpdate(job) {
    setActiveJob(job);
    setResultsRefreshKey((k) => k + 1); // pull fresh results once the scan finishes
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
            <button className={view === 'history' ? 'active' : ''} onClick={() => setView('history')}>History</button>
          </div>
          <span>{user.email}</span>
          <button onClick={handleLogout}>Sign out</button>
        </div>
      </div>

      {view === 'history' ? (
        <div className="dashboard" style={{ gridTemplateColumns: '1fr' }}>
          <History onSelect={(job) => { setActiveJob(job); setView('dashboard'); }} />
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
