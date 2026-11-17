import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowUpRight, Check, CircleAlert, Clock3, FileText, Globe2,
  LoaderCircle, Pause, Play, RefreshCw, Save, ShieldCheck, Sparkles, Upload,
} from 'lucide-react';

const API = '/api';
const PROFILE_URL = 'https://www.naukri.com/mnjuser/profile';
const HEADLINE_INTERVALS = [1, 5, 15, 45, 60];
const RESUME_INTERVALS = [1, 3, 6, 24];

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(`${API}${path}`, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || payload.message || `Request failed (${response.status})`);
  }
  return payload;
}

function StatusPill({ active, children }) {
  return <span className={`status-pill ${active ? 'is-active' : ''}`}><i />{children}</span>;
}

function App() {
  const [profile, setProfile] = useState({ full_name: '', experience_years: '', resume_path: '', resume_filename: '' });
  const [browser, setBrowser] = useState({ running: false, current_url: '' });
  const [headline, setHeadline] = useState({ headlines: ['', '', '', ''], interval_minutes: 15, enabled: false, last_status: 'idle', last_message: '', next_index: 0 });
  const [resume, setResume] = useState({ interval_hours: 3, enabled: false, last_status: 'idle', last_message: '', last_filename: '' });
  const [resumeFile, setResumeFile] = useState(null);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState(null);
  const headlineDraftDirty = useRef(false);
  const resumeDraftDirty = useRef(false);
  const profileDraftDirty = useRef(false);

  const refreshRuntime = useCallback(async () => {
    const [browserResult, headlineResult, resumeResult] = await Promise.allSettled([
      request('/browser/status'),
      request('/naukri-refresh/status'),
      request('/naukri-resume-refresh/status'),
    ]);
    if (browserResult.status === 'fulfilled') setBrowser(browserResult.value);
    if (headlineResult.status === 'fulfilled') {
      const value = headlineResult.value;
      setHeadline(current => ({
        ...current,
        ...value,
        headlines: headlineDraftDirty.current
          ? current.headlines
          : [...(value.headlines || []), '', '', '', ''].slice(0, 4),
        interval_minutes: headlineDraftDirty.current ? current.interval_minutes : value.interval_minutes,
      }));
    }
    if (resumeResult.status === 'fulfilled') setResume(current => ({
      ...current,
      ...resumeResult.value,
      interval_hours: resumeDraftDirty.current ? current.interval_hours : resumeResult.value.interval_hours,
    }));
  }, []);

  useEffect(() => {
    let alive = true;
    Promise.allSettled([
      request('/profile'),
      request('/browser/status'),
      request('/naukri-refresh/status'),
      request('/naukri-resume-refresh/status'),
    ]).then(([profileResult, browserResult, headlineResult, resumeResult]) => {
      if (!alive) return;
      if (profileResult.status === 'fulfilled') setProfile(profileResult.value);
      if (browserResult.status === 'fulfilled') setBrowser(browserResult.value);
      if (headlineResult.status === 'fulfilled') {
        const value = headlineResult.value;
        setHeadline(current => ({ ...current, ...value, headlines: [...(value.headlines || []), '', '', '', ''].slice(0, 4) }));
      }
      if (resumeResult.status === 'fulfilled') setResume(current => ({
        ...current,
        ...resumeResult.value,
        interval_hours: resumeDraftDirty.current ? current.interval_hours : resumeResult.value.interval_hours,
      }));
    });
    const timer = window.setInterval(() => { refreshRuntime(); }, 2500);
    return () => { alive = false; window.clearInterval(timer); };
  }, [refreshRuntime]);

  const perform = async (action, task, success) => {
    setBusy(action);
    setNotice(null);
    try {
      const result = await task();
      if (success) setNotice({ kind: 'success', text: typeof success === 'function' ? success(result) : success });
      await refreshRuntime();
      return result;
    } catch (error) {
      setNotice({ kind: 'error', text: error.message || 'Something went wrong.' });
      return null;
    } finally {
      setBusy('');
    }
  };

  const activeNaukri = browser.running && /naukri\.com/i.test(browser.current_url || '');
  const headlineCount = (headline.headlines || []).filter(value => value.trim()).length;
  const canStartHeadline = activeNaukri && headlineCount >= 3 && !headlineDraftDirty.current && !busy;
  const resumeDetailsReady = Boolean(profile.full_name?.trim() && profile.experience_years?.trim()) && !profileDraftDirty.current;
  const resumeFileReady = Boolean(profile.resume_path);
  const resumeReady = activeNaukri && resumeFileReady && resumeDetailsReady;
  const canUseResume = resumeReady && !busy;
  const resumeBlocker = !activeNaukri
    ? 'Open your signed-in Naukri profile in the managed browser.'
    : !resumeDetailsReady
      ? 'Enter your name and experience, then select Save details.'
      : !resumeFileReady
        ? 'Choose your resume above and select Upload resume.'
        : 'Ready. Starting will upload the saved resume immediately.';

  const startBrowser = () => perform('browser', async () => {
    const result = await request('/browser/start', { method: 'POST' });
    setBrowser(result);
    return result;
  }, 'Your separate Naukri browser is ready. Sign in to Naukri once, then return here.');

  const openProfile = () => perform('open-profile', async () => {
    const result = await request('/browser/open', { method: 'POST', body: JSON.stringify({ url: PROFILE_URL }) });
    setBrowser({ running: true, current_url: result.url });
    return result;
  }, 'Naukri profile opened in the managed browser.');

  const saveHeadlines = () => perform('headlines-save', async () => {
    const headlines = (headline.headlines || []).map(value => value.trim()).filter(Boolean);
    if (headlines.length < 3) throw new Error('Add at least 3 distinct headline versions before saving.');
    if (new Set(headlines).size !== headlines.length) throw new Error('Each headline version must be different.');
    const result = await request('/naukri-refresh/configure', {
      method: 'PUT',
      body: JSON.stringify({ headlines, interval_minutes: Number(headline.interval_minutes) }),
    });
    headlineDraftDirty.current = false;
    setHeadline(current => ({ ...current, ...result, headlines: [...result.headlines, '', '', '', ''].slice(0, 4) }));
    return result;
  }, 'Headline versions and interval saved on this computer.');

  const startHeadlines = () => perform('headlines-start', () => request('/naukri-refresh/start', { method: 'POST' }), 'Headline auto-update started. The first saved headline is applied now.');
  const pauseHeadlines = () => perform('headlines-pause', () => request('/naukri-refresh/stop', { method: 'POST' }), 'Headline auto-update paused.');
  const updateHeadlineNow = () => perform('headline-now', async () => {
    const result = await request('/naukri-refresh/run-now', { method: 'POST' });
    if (result.last_status === 'error') throw new Error(result.last_message);
    return result;
  }, result => result.last_message || 'Headline update finished.');

  const saveProfile = () => perform('profile-save', async () => {
    const result = await request('/profile', {
      method: 'PUT',
      body: JSON.stringify({ full_name: profile.full_name || '', experience_years: profile.experience_years || '' }),
    });
    profileDraftDirty.current = false;
    setProfile(result);
    return result;
  }, 'Resume details saved.');

  const uploadResume = () => perform('resume-upload', async () => {
    if (!resumeFile) throw new Error('Choose a PDF, DOCX or TXT resume first.');
    const form = new FormData();
    form.append('file', resumeFile);
    const result = await request('/profile/resume', { method: 'POST', body: form });
    setProfile(current => ({ ...current, resume_path: result.resume_path, resume_filename: result.filename }));
    setResumeFile(null);
    return result;
  }, result => `${result.filename} is saved for this separate project.`);

  const saveResumeInterval = () => perform('resume-interval', async () => {
    const result = await request('/naukri-resume-refresh/configure', {
      method: 'PUT', body: JSON.stringify({ interval_hours: Number(resume.interval_hours) }),
    });
    resumeDraftDirty.current = false;
    setResume(current => ({ ...current, ...result }));
    return result;
  }, result => `Resume refresh interval saved: every ${result.interval_hours} hour${result.interval_hours === 1 ? '' : 's'}.`);

  const startResumeRefresh = () => perform('resume-start', () => request('/naukri-resume-refresh/start', { method: 'POST' }), 'Automatic resume refresh started.');
  const pauseResumeRefresh = () => perform('resume-pause', () => request('/naukri-resume-refresh/stop', { method: 'POST' }), 'Automatic resume refresh paused.');
  const uploadResumeNow = () => perform('resume-now', async () => {
    const result = await request('/naukri-resume-refresh/run-now', { method: 'POST' });
    if (result.last_status === 'error') throw new Error(result.last_message);
    return result;
  }, result => result.last_message || 'Resume upload finished.');

  const setHeadlineField = (index, value) => {
    headlineDraftDirty.current = true;
    setHeadline(current => ({ ...current, headlines: current.headlines.map((item, i) => i === index ? value : item) }));
  };

  const urlLabel = browser.current_url ? browser.current_url.replace(/^https?:\/\//, '').split('/')[0] : 'Not connected';
  const localDate = new Date();
  const filenameDate = `${localDate.getFullYear()}-${String(localDate.getMonth() + 1).padStart(2, '0')}-${String(localDate.getDate()).padStart(2, '0')}`;

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#home" aria-label="Naukri Update home">
          <span className="brand-mark"><Sparkles size={19} /></span>
          <span>Naukri<span className="brand-light">Update</span></span>
        </a>
        <div className="topbar-right">
          <StatusPill active={browser.running}>{browser.running ? 'Browser ready' : 'Browser offline'}</StatusPill>
          <span className="local-label"><ShieldCheck size={14} /> Saved locally</span>
        </div>
      </header>

      <div className="page-wrap" id="home">
        <section className="hero">
          <div className="hero-copy">
            <div className="eyebrow"><span className="eyebrow-line" /> PERSONAL PROFILE TOOLS</div>
            <h1>Keep your Naukri<br /><em>profile fresh.</em></h1>
            <p>Update your resume headline and resume file from one simple dashboard. Your profile data and browser sign-in stay in this separate project.</p>
          </div>
          <div className="hero-orbit" aria-hidden="true"><div className="orbit orbit-a" /><div className="orbit orbit-b" /><div className="orbit-center"><Sparkles size={28} /></div><span className="orbit-dot dot-a" /><span className="orbit-dot dot-b" /><span className="orbit-dot dot-c" /></div>
        </section>

        <section className="browser-strip">
          <div className="browser-info">
            <span className={`browser-icon ${browser.running ? 'online' : ''}`}><Globe2 size={20} /></span>
            <div><b>Managed browser</b><span>{browser.running ? urlLabel : 'Launch a private Naukri session to get started'}</span></div>
          </div>
          <div className="browser-actions">
            <button className="button button-light" onClick={startBrowser} disabled={Boolean(busy) || browser.running}>
              {busy === 'browser' ? <LoaderCircle className="spin" size={16} /> : <Play size={15} />}
              {browser.running ? 'Browser launched' : 'Launch browser'}
            </button>
            <button className="button button-primary" onClick={openProfile} disabled={Boolean(busy)}>
              <ArrowUpRight size={16} /> Open Naukri profile
            </button>
          </div>
        </section>

        <div className="section-heading">
          <div><div className="eyebrow">YOUR UPDATE WORKSPACE</div><h2>Everything in one place</h2></div>
          <span className="section-note"><span className="tiny-dot" /> One page · One private browser</span>
        </div>

        <section className="workspace-grid">
          <article className="card headline-card">
            <div className="card-topline">
              <div className="card-icon purple"><Sparkles size={19} /></div>
              <StatusPill active={headline.enabled}>{headline.enabled ? 'Auto-update on' : 'Paused'}</StatusPill>
            </div>
            <div className="card-title-row"><div><div className="card-kicker">01 / HEADLINE</div><h3>Rotate resume headlines</h3></div></div>
            <p className="card-intro">Add 3–4 honest headline versions. Naukri will show the next version at your selected interval.</p>

            <label className="field-label interval-field">Update every
              <span className="select-wrap"><select value={headline.interval_minutes || 15} onChange={event => { headlineDraftDirty.current = true; setHeadline(current => ({ ...current, interval_minutes: Number(event.target.value) })); }} disabled={headline.enabled || Boolean(busy)}>
                {HEADLINE_INTERVALS.map(value => <option value={value} key={value}>{value} min</option>)}
              </select><Clock3 size={15} /></span>
            </label>

            <div className="headline-list">
              {(headline.headlines || ['', '', '', '']).map((value, index) => (
                <label className="headline-input" key={index}>
                  <span className="headline-index">{String(index + 1).padStart(2, '0')}</span>
                  <textarea aria-label={`Resume headline version ${index + 1}`} maxLength={250} rows={2} placeholder={index < 3 ? `Add headline version ${index + 1}${index === 2 ? ' (required)' : ''}` : 'Optional fourth version'} value={value} onChange={event => setHeadlineField(index, event.target.value)} />
                  <small>{value.length}/250</small>
                </label>
              ))}
            </div>
            <div className="form-footnote"><CircleAlert size={14} /> At least 3 distinct versions · 250 characters max each</div>
            <div className="card-actions">
              <button className="button button-soft" onClick={saveHeadlines} disabled={Boolean(busy)}>{busy === 'headlines-save' ? <LoaderCircle className="spin" size={16} /> : <Save size={15} />} Save headlines</button>
              <button className="button button-quiet" onClick={updateHeadlineNow} disabled={Boolean(busy) || !activeNaukri || headlineCount < 3 || headlineDraftDirty.current}>{busy === 'headline-now' ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={15} />} Update now</button>
              {headline.enabled
                ? <button className="button button-stop" onClick={pauseHeadlines} disabled={Boolean(busy)}><Pause size={15} /> Pause</button>
                : <button className="button button-primary" onClick={startHeadlines} disabled={!canStartHeadline}><Play size={15} /> Start auto-update</button>}
            </div>
            <div className="result-box">
              <div className="result-icon"><Check size={15} /></div>
              <div><b>{headline.last_status === 'success' ? 'Headline updated' : headline.last_status === 'error' ? 'Needs attention' : 'Update status'}</b><span>{headline.last_message || 'Save your versions, open Naukri, then start auto-update.'}</span></div>
              {headline.last_updated && <time>{new Date(headline.last_updated).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time>}
            </div>
          </article>

          <article className="card resume-card">
            <div className="card-topline">
              <div className="card-icon blue"><FileText size={19} /></div>
              <StatusPill active={resume.enabled}>{resume.enabled ? 'Auto-refresh on' : 'Paused'}</StatusPill>
            </div>
            <div className="card-kicker">02 / RESUME FILE</div>
            <h3>Refresh your resume</h3>
            <p className="card-intro">Save your resume here. The updater makes a dated copy and uploads it to your signed-in Naukri profile.</p>

            <div className="profile-fields">
              <label className="field-label">Full name<input value={profile.full_name || ''} onChange={event => { profileDraftDirty.current = true; setProfile(current => ({ ...current, full_name: event.target.value })); }} placeholder="e.g. Asha Sharma" /></label>
              <label className="field-label">Total experience<input value={profile.experience_years || ''} onChange={event => { profileDraftDirty.current = true; setProfile(current => ({ ...current, experience_years: event.target.value })); }} placeholder="e.g. 5 years" /></label>
            </div>
            <div className="file-control">
              <div className="file-preview"><span className="file-icon"><FileText size={17} /></span><div><b>{resumeFile?.name || profile.resume_filename || 'Choose your resume'}</b><span>{resumeFile ? `${(resumeFile.size / (1024 * 1024)).toFixed(2)} MB · ready to upload` : profile.resume_path ? 'Saved in this project' : 'PDF, DOCX or TXT · up to 12 MB'}</span></div></div>
              <label className="button button-file">Browse<input type="file" accept=".pdf,.docx,.txt" onChange={event => setResumeFile(event.target.files?.[0] || null)} /></label>
            </div>
            <div className="card-actions profile-actions">
              <button className="button button-soft" onClick={saveProfile} disabled={Boolean(busy)}>{busy === 'profile-save' ? <LoaderCircle className="spin" size={16} /> : <Save size={15} />} Save details</button>
              <button className="button button-quiet" onClick={uploadResume} disabled={Boolean(busy) || !resumeFile}>{busy === 'resume-upload' ? <LoaderCircle className="spin" size={16} /> : <Upload size={15} />} Upload resume</button>
            </div>
            <div className={`resume-prerequisite ${resumeReady ? 'is-ready' : ''}`}><span>{resumeReady ? <Check size={14} /> : <CircleAlert size={14} />}</span><p><b>{resumeReady ? 'Ready to start' : 'Resume refresh needs one more step'}</b><small>{resumeBlocker}</small></p></div>

            <div className="divider" />
            <div className="resume-schedule-head"><div><b>Automatic refresh</b><span>Re-upload your saved resume on a schedule.</span></div><label className="field-label compact-select"><span className="select-wrap"><select value={resume.interval_hours || 3} onChange={event => { resumeDraftDirty.current = true; setResume(current => ({ ...current, interval_hours: Number(event.target.value) })); }} disabled={resume.enabled || Boolean(busy)}>
              {RESUME_INTERVALS.map(value => <option value={value} key={value}>Every {value} hour{value > 1 ? 's' : ''}</option>)}
            </select><Clock3 size={14} /></span></label></div>
            <div className="filename-note"><span>Next filename</span><code>{profile.full_name?.trim() ? `${profile.full_name.trim().replace(/\s+/g, '_')}_${(String(profile.experience_years || 'YOE').match(/\d+(?:\.\d+)?/) || ['YOE'])[0].replace('.', 'p')}_YOE_${filenameDate}${(profile.resume_filename || '.pdf').match(/\.[^.]+$/)?.[0] || '.pdf'}` : 'Name_Experience_YOE_Date.pdf'}</code></div>
            <div className="card-actions resume-schedule-actions">
              <button className="button button-soft" onClick={saveResumeInterval} disabled={Boolean(busy)}>{busy === 'resume-interval' ? <LoaderCircle className="spin" size={16} /> : <Save size={15} />} Save interval</button>
              <button className="button button-quiet" onClick={uploadResumeNow} disabled={Boolean(busy) || !canUseResume}>{busy === 'resume-now' ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={15} />} Upload now</button>
              {resume.enabled
                ? <button className="button button-stop" onClick={pauseResumeRefresh} disabled={Boolean(busy)}><Pause size={15} /> Pause</button>
                : <button className="button button-primary" onClick={startResumeRefresh} disabled={!canUseResume}><Play size={15} /> Start refresh</button>}
            </div>
            <div className="result-box resume-result">
              <div className="result-icon"><Check size={15} /></div>
              <div><b>{resume.last_status === 'success' ? 'Resume uploaded' : resume.last_status === 'error' ? 'Needs attention' : 'Resume status'}</b><span>{resume.last_message || 'Save your details and resume, then open your Naukri profile.'}</span>{resume.last_filename && <small>Last file: {resume.last_filename}</small>}</div>
              {resume.last_updated && <time>{new Date(resume.last_updated).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time>}
            </div>
          </article>
        </section>

        <aside className="privacy-banner"><span className="privacy-icon"><ShieldCheck size={18} /></span><p><b>Your data stays on this computer.</b> The resume, headline drafts, scheduler settings and Naukri sign-in are stored in this separate project folder. Updates pause if Naukri needs a sign-in or a page check.</p></aside>
        <footer><span>Naukri Update <span className="footer-dot">·</span> Local profile tools</span><span>Browser: {urlLabel}</span></footer>
      </div>

      {notice && <div className={`toast ${notice.kind}`} role="status"><span className="toast-icon">{notice.kind === 'success' ? <Check size={16} /> : <CircleAlert size={16} />}</span><span>{notice.text}</span><button aria-label="Dismiss message" onClick={() => setNotice(null)}>×</button></div>}
    </main>
  );
}

export default App;
