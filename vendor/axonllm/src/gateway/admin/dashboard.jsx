const { useState, useEffect, useCallback, useRef } = React;

/* ── API key (for ENFORCE mode) ──
   The gateway defaults to ENFORCE, so admin calls need a Bearer token.
   Stored in sessionStorage (cleared on tab close). Get one via `axon issue-key`. */
const AUTH_KEY = 'axon_admin_api_key';
const CSRF_COOKIE = '__Host-axon-csrf';
const getApiKey = () => sessionStorage.getItem(AUTH_KEY) || '';
const setApiKey = (k) => k ? sessionStorage.setItem(AUTH_KEY, k) : sessionStorage.removeItem(AUTH_KEY);
const getCsrfToken = () => {
  for (const part of document.cookie.split(';')) {
    const cookie = part.trim();
    if (cookie.startsWith(CSRF_COOKIE + '=')) {
      return cookie.slice(CSRF_COOKIE.length + 1);
    }
  }
  return '';
};
let browserSessionMode = Boolean(getCsrfToken());
const promptForKey = () => {
  const k = window.prompt(
    'This gateway requires an API key (ENFORCE mode).\n' +
    'Paste an admin API key (create one with: axon issue-key):'
  );
  if (k) setApiKey(k.trim());
  return getApiKey();
};

const authHeaders = (extra) => {
  const h = Object.assign({}, extra || {});
  const key = browserSessionMode ? '' : getApiKey();
  if (key) h['Authorization'] = 'Bearer ' + key;
  return h;
};

// Turn any failed response into a readable Error (status + server message),
// instead of rejecting with an opaque object. Application browser sessions
// return a login URL; custom-domain/API-key deployments retain the key prompt.
async function request(method, url, body, _retried) {
  const headers = authHeaders(body != null ? { 'Content-Type': 'application/json' } : {});
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase())) {
    const csrfToken = getCsrfToken();
    if (csrfToken) headers['X-Axon-CSRF-Token'] = csrfToken;
  }
  const opts = { method, headers, credentials: 'same-origin' };
  if (body != null) opts.body = JSON.stringify(body);
  let r;
  try {
    r = await fetch(url, opts);
  } catch (netErr) {
    throw new Error('Network error: ' + (netErr && netErr.message ? netErr.message : 'request failed'));
  }
  let data = null;
  try { data = await r.json(); } catch (e) { /* non-JSON body */ }
  if (r.status === 401 && !_retried) {
    const loginUrl = data && data.error && data.error.login_url;
    if (typeof loginUrl === 'string' && loginUrl.startsWith('/auth/login')) {
      browserSessionMode = true;
      setApiKey('');
      window.location.assign(loginUrl);
      throw new Error('Authentication required (401). Redirecting to sign in.');
    }
    browserSessionMode = false;
    if (promptForKey()) return request(method, url, body, true);
    throw new Error('Authentication required (401). Provide an admin API key.');
  }
  if (r.ok) return data;
  const msg = (data && data.error && (data.error.message || data.error.type)) ||
              (data && data.message) || ('HTTP ' + r.status);
  const err = new Error(msg);
  err.status = r.status;
  throw err;
}

const api = {
  get: (url) => request('GET', url),
  post: (url, body) => request('POST', url, body),
  put: (url, body) => request('PUT', url, body),
  del: (url) => request('DELETE', url),
};

const sleep = (milliseconds) => new Promise(resolve => {
  window.setTimeout(resolve, milliseconds);
});

const sameOriginAdminPath = (value) => (
  typeof value === 'string' &&
  value.startsWith('/admin/') &&
  !value.startsWith('//')
);

async function authorizedDownloadRequest(url, _retried) {
  if (!sameOriginAdminPath(url)) {
    throw new Error('The export service returned an invalid download path.');
  }
  let response;
  try {
    response = await fetch(url, {
      method: 'GET',
      headers: authHeaders({ 'Accept': 'application/json, text/csv' }),
      credentials: 'same-origin',
      redirect: 'follow',
    });
  } catch (netErr) {
    throw new Error('Network error: ' + (netErr && netErr.message ? netErr.message : 'request failed'));
  }
  if (response.status === 401 && !_retried) {
    let payload = null;
    try { payload = await response.clone().json(); } catch (e) { /* non-JSON body */ }
    const loginUrl = payload && payload.error && payload.error.login_url;
    if (typeof loginUrl === 'string' && loginUrl.startsWith('/auth/login')) {
      browserSessionMode = true;
      setApiKey('');
      window.location.assign(loginUrl);
      throw new Error('Authentication required (401). Redirecting to sign in.');
    }
    browserSessionMode = false;
    if (promptForKey()) return authorizedDownloadRequest(url, true);
    throw new Error('Authentication required (401). Provide an admin API key.');
  }
  if (!response.ok) {
    let payload = null;
    try { payload = await response.clone().json(); } catch (e) { /* non-JSON body */ }
    const message = (payload && payload.error && (payload.error.message || payload.error.type)) ||
                    (payload && payload.message) || ('HTTP ' + response.status);
    throw new Error(message);
  }
  return response;
}

function saveResponseFile(response, fallbackFilename) {
  return response.blob().then(blob => {
    const disposition = response.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const filename = match && match[1] ? match[1] : fallbackFilename;
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = filename;
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    return filename;
  });
}

async function downloadExport(url, fallbackFilename, onProgress) {
  const initial = await authorizedDownloadRequest(url);
  if (initial.status !== 202) {
    return saveResponseFile(initial, fallbackFilename);
  }

  const created = await initial.json();
  if (!sameOriginAdminPath(created.statusUrl)) {
    throw new Error('The export service returned an invalid status path.');
  }
  onProgress('Preparing export...');
  for (let attempt = 0; attempt < 150; attempt += 1) {
    await sleep(2000);
    const statusResponse = await authorizedDownloadRequest(created.statusUrl);
    const job = await statusResponse.json();
    if (job.status === 'failed') {
      throw new Error('The export job failed. Try again or contact an administrator.');
    }
    if (job.status === 'complete') {
      if (!sameOriginAdminPath(job.downloadUrl)) {
        throw new Error('The export service returned an invalid download path.');
      }
      onProgress('Downloading export...');
      const download = await authorizedDownloadRequest(job.downloadUrl);
      return saveResponseFile(download, fallbackFilename);
    }
    onProgress(job.status === 'processing' ? 'Generating export...' : 'Export queued...');
  }
  throw new Error('The export is still running. Try again shortly.');
}

const fmt = {
  cost: (v) => `$${(v || 0).toFixed(4)}`,
  num: (v) => (v || 0).toLocaleString(),
  pct: (v) => `${(v || 0).toFixed(1)}%`,
};

/* ── Buttons (Cloudscape-style) ── */
const btnStyles = {
  base: { display: 'inline-flex', alignItems: 'center', gap: '0.4rem', padding: '0.5rem 1rem', border: 'none', borderRadius: '12px', cursor: 'pointer', fontSize: '13px', fontWeight: 600, fontFamily: 'inherit', transition: 'all 120ms', boxShadow: '0 1px 2px rgba(0,0,0,0.05)' },
  primary: { background: '#7c3aed', color: '#fff', border: 'none' },
  normal: { background: '#fff', color: '#1c1917', border: '1.5px solid #e7e5e4' },
  link: { background: 'none', color: '#7c3aed', border: 'none', padding: '0.2rem 0', fontWeight: 600, boxShadow: 'none' },
  danger: { background: '#fef2f2', color: '#dc2626', border: 'none' },
};

function Btn({ variant = 'normal', children, style, ...props }) {
  const s = { ...btnStyles.base, ...btnStyles[variant], ...style };
  return <button style={s} {...props}>{children}</button>;
}

/* ── Breadcrumb ── */
function Breadcrumb({ items }) {
  return (
    <nav style={{ fontSize: '14px', marginBottom: '0.5rem', color: '#5f6b7a' }}>
      {items.map((item, i) => (
        <span key={i}>
          {i > 0 && <span style={{ margin: '0 0.35rem' }}>/</span>}
          {item.onClick
            ? <span onClick={item.onClick} style={{ color: '#0972d3', cursor: 'pointer', fontWeight: 400 }}>{item.label}</span>
            : <span style={{ color: '#000716', fontWeight: 400 }}>{item.label}</span>}
        </span>
      ))}
    </nav>
  );
}

/* ── Flash Message ── */
function Flash({ type, children, onDismiss }) {
  const colors = {
    success: { bg: '#f2fcf3', border: '#037f0c', color: '#037f0c', icon: '✓' },
    error: { bg: '#fff7f7', border: '#d91515', color: '#d91515', icon: '✕' },
    info: { bg: '#f2f8fd', border: '#0972d3', color: '#0972d3', icon: 'ℹ' },
    // Degraded-but-serving, as distinct from error (failed) and info
    // (normal). Without this, type="warning" silently rendered as info.
    warning: { bg: '#fffdf5', border: '#855900', color: '#855900', icon: '⚠' },
  };
  const c = colors[type] || colors.info;
  return (
    <div style={{ background: c.bg, borderLeft: `4px solid ${c.border}`, color: c.color, padding: '0.65rem 1rem', borderRadius: '4px', marginBottom: '1rem', fontSize: '14px', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
      <span style={{ fontWeight: 700 }}>{c.icon}</span>
      <span style={{ flex: 1 }}>{children}</span>
      {onDismiss && <span onClick={onDismiss} style={{ cursor: 'pointer', opacity: 0.6 }}>✕</span>}
    </div>
  );
}

/* ── Loading Spinner ── */
function Loading({ text = 'Loading...' }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '3rem', color: '#5f6b7a' }}>
      <svg width="20" height="20" viewBox="0 0 20 20" style={{ animation: 'spin 0.8s linear infinite', marginRight: '0.6rem' }}>
        <circle cx="10" cy="10" r="8" fill="none" stroke="#e9ebed" strokeWidth="2.5" />
        <circle cx="10" cy="10" r="8" fill="none" stroke="#0972d3" strokeWidth="2.5" strokeDasharray="20 32" strokeLinecap="round" />
      </svg>
      {text}
    </div>
  );
}

/* ── Empty State ── */
function EmptyState({ title, subtitle, action }) {
  return (
    <div style={{ textAlign: 'center', padding: '3rem 2rem', color: '#5f6b7a' }}>
      <div style={{ fontSize: '2rem', marginBottom: '0.5rem', opacity: 0.4 }}>📋</div>
      <div style={{ fontSize: '16px', fontWeight: 700, color: '#000716', marginBottom: '0.25rem' }}>{title}</div>
      {subtitle && <div style={{ fontSize: '14px', marginBottom: '1rem' }}>{subtitle}</div>}
      {action}
    </div>
  );
}

/* ── Progress Bar ── */
function ProgressBar({ value, color }) {
  return (
    <div style={{ height: '6px', background: '#e9ebed', borderRadius: '3px', overflow: 'hidden', marginTop: '0.3rem' }}>
      <div style={{ height: '100%', borderRadius: '3px', width: `${Math.min(value, 100)}%`, background: color || '#0972d3', transition: 'width 0.3s ease' }} />
    </div>
  );
}

/* ================================================================== */
/*  Overview Page                                                      */
/* ================================================================== */
function OverviewPage() {
  const [data, setData] = useState(null);
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    Promise.all([api.get('/admin/overview'), api.get('/admin/health')])
      .then(([o, h]) => { setData(o); setHealth(h); })
      .catch(e => setError('Failed to load overview data: ' + (e && e.message ? e.message : 'unknown error')));
  }, []);

  if (error) return <Flash type="error">{error}</Flash>;
  if (!data) return <Loading text="Loading overview..." />;

  const providers = health ? Object.entries(health.providers || {}) : [];
  const healthyCount = providers.filter(([,s]) => s === 'healthy').length;

  return (
    <div>
      <div className="page-header"><h1>Overview</h1><p>Real-time gateway metrics and system health</p></div>
      <div className="stat-grid">
        <div className="stat-card"><div className="stat-label">Total Requests</div><div className="stat-value">{fmt.num(data.total_requests)}</div></div>
        <div className="stat-card"><div className="stat-label">Total Cost</div><div className="stat-value">{fmt.cost(data.total_cost)}</div></div>
        <div className="stat-card"><div className="stat-label">Active Projects</div><div className="stat-value">{data.active_projects}</div></div>
        <div className="stat-card"><div className="stat-label">Cache Hit Rate</div><div className="stat-value">{data.cache_hit_rate != null ? (data.cache_hit_rate * 100).toFixed(1) + '%' : '—'}</div></div>
        <div className="stat-card"><div className="stat-label">Active Users</div><div className="stat-value">{data.active_users}</div></div>
      </div>

      {health && (
        <div className="container">
          <div className="container-header">
            <h2>Provider Health <span className="counter">({providers.length})</span></h2>
            <span className="badge badge-green"><span className="badge-dot"></span>{healthyCount}/{providers.length} healthy</span>
          </div>
          <div className="container-body no-pad">
            <table>
              <thead><tr><th>Provider</th><th>Status</th></tr></thead>
              <tbody>
                {providers.map(([name, status]) => (
                  <tr key={name}>
                    <td><strong>{name}</strong></td>
                    <td><span className={`badge ${status === 'healthy' ? 'badge-green' : 'badge-red'}`}><span className="badge-dot"></span>{status}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

/* ================================================================== */
/*  Projects List                                                      */
/* ================================================================== */
function ProjectsPage({ onSelect, onCreateNew }) {
  const [projects, setProjects] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => { api.get('/admin/projects').then(setProjects).catch(e => setError('Failed to load projects: ' + (e && e.message ? e.message : 'unknown error'))); }, []);

  if (error) return <Flash type="error">{error}</Flash>;
  if (!projects) return <Loading text="Loading projects..." />;

  return (
    <div>
      <div className="page-header page-header-actions">
        <div><h1>Projects</h1><p>Manage project configurations and budgets</p></div>
        <Btn variant="primary" onClick={onCreateNew}>Create project</Btn>
      </div>
      {projects.length === 0 ? (
        <div className="container"><EmptyState title="No projects" subtitle="Create a project to get started." action={<Btn variant="primary" onClick={onCreateNew}>Create project</Btn>} /></div>
      ) : (
        <div className="container">
          <div className="container-header"><h2>Projects <span className="counter">({projects.length})</span></h2></div>
          <div className="container-body no-pad">
            <table>
              <thead><tr><th>Project</th><th>Spend</th><th>Budget</th><th>Utilization</th><th>Requests</th></tr></thead>
              <tbody>
                {projects.map(p => (
                  <tr key={p.project_id} className="clickable" onClick={() => onSelect(p.project_id)}>
                    <td>
                      <strong style={{ color: '#0972d3' }}>{p.name}</strong>
                      <div className="sub-text">{p.project_id}</div>
                    </td>
                    <td>{fmt.cost(p.current_spend)}</td>
                    <td>{p.budget_limit != null ? `$${p.budget_limit.toFixed(2)}` : <span style={{ color: '#5f6b7a' }}>—</span>}</td>
                    <td style={{ minWidth: '130px' }}>
                      {p.budget_utilization_pct != null ? (
                        <div>
                          <span style={{ fontSize: '13px', fontWeight: 600 }}>{fmt.pct(p.budget_utilization_pct)}</span>
                          <ProgressBar value={p.budget_utilization_pct} color={p.budget_utilization_pct > 90 ? '#d91515' : p.budget_utilization_pct > 70 ? '#8d6605' : '#037f0c'} />
                        </div>
                      ) : <span style={{ color: '#5f6b7a' }}>—</span>}
                    </td>
                    <td>{p.request_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

/* ================================================================== */
/*  Project Detail                                                     */
/* ================================================================== */
function ProjectDetailPage({ projectId, onBack, onEdit }) {
  const [project, setProject] = useState(null);
  const [error, setError] = useState(null);
  const [newMember, setNewMember] = useState('');
  const [memberMsg, setMemberMsg] = useState(null);
  const [allModels, setAllModels] = useState([]);
  const [projectModels, setProjectModels] = useState([]);
  const [modelMsg, setModelMsg] = useState(null);
  const [modelError, setModelError] = useState(null);
  const [addingModel, setAddingModel] = useState('');

  const loadProject = useCallback(() => {
    api.get(`/admin/projects/${projectId}`).then(setProject).catch(e => setError('Failed to load project: ' + (e && e.message ? e.message : 'unknown error')));
  }, [projectId]);

  const loadProjectModels = useCallback(() => {
    api.get(`/admin/projects/${projectId}/models`)
      .then(data => setProjectModels(data.allowed_models || []))
      .catch(() => setModelError('Failed to load project models'));
  }, [projectId]);

  const loadAllModels = useCallback(() => {
    api.get('/admin/models')
      .then(models => setAllModels(models.map(m => m.name)))
      .catch(() => {});
  }, []);

  useEffect(() => { loadProject(); loadProjectModels(); loadAllModels(); }, [loadProject, loadProjectModels, loadAllModels]);

  const handleAddModel = (modelName) => {
    if (!modelName) return;
    setModelMsg(null); setModelError(null);
    api.post(`/admin/projects/${projectId}/models`, { model: modelName })
      .then(data => {
        setProjectModels(data.allowed_models || []);
        setModelMsg(`Added "${modelName}"`);
        setAddingModel('');
        setProject(prev => prev ? { ...prev, allowed_models: data.allowed_models || [] } : prev);
      })
      .catch(() => setModelError(`Failed to add model "${modelName}"`));
  };

  const handleRemoveModel = (modelName) => {
    setModelMsg(null); setModelError(null);
    api.del(`/admin/projects/${projectId}/models/${encodeURIComponent(modelName)}`)
      .then(data => {
        setProjectModels(data.allowed_models || []);
        setModelMsg(`Removed "${modelName}"`);
        setProject(prev => prev ? { ...prev, allowed_models: data.allowed_models || [] } : prev);
      })
      .catch(() => setModelError(`Failed to remove model "${modelName}"`));
  };

  if (error) return <div><Breadcrumb items={[{ label: 'Projects', onClick: onBack }, { label: projectId }]} /><Flash type="error">{error}</Flash></div>;
  if (!project) return <Loading text="Loading project..." />;

  const modelEntries = Object.entries(project.usage_by_model || {});
  const providerEntries = Object.entries(project.usage_by_provider || {});
  const userEntries = Object.entries(project.usage_by_user || {});

  return (
    <div>
      <Breadcrumb items={[{ label: 'Projects', onClick: onBack }, { label: project.name }]} />
      <div className="page-header page-header-actions">
        <div><h1>{project.name}</h1><p>{project.project_id}</p></div>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <Btn variant="normal" onClick={() => onEdit(projectId)}>Edit</Btn>
        </div>
      </div>

      <div className="container">
        <div className="container-header"><h2>Configuration</h2></div>
        <div className="container-body">
          <div className="column-layout">
            <div className="kv-pair"><div className="kv-label">Budget limit</div><div className="kv-value">{project.budget_limit != null ? `$${project.budget_limit}` : '—'}</div></div>
            <div className="kv-pair"><div className="kv-label">Alert threshold</div><div className="kv-value">{project.alert_threshold != null ? `$${project.alert_threshold}` : '—'}</div></div>
            <div className="kv-pair"><div className="kv-label">Cache</div><div className="kv-value">{project.cache_enabled ? `Enabled (${project.cache_ttl_seconds}s TTL)` : 'Disabled'}</div></div>
            <div className="kv-pair"><div className="kv-label">Semantic cache</div><div className="kv-value">{project.semantic_cache_enabled ? `Enabled (${project.semantic_cache_threshold != null ? project.semantic_cache_threshold : 'default'} threshold)` : 'Disabled'}</div></div>
            <div className="kv-pair"><div className="kv-label">Prompt caching</div><div className="kv-value">{project.prompt_caching_enabled ? 'Enabled' : 'Disabled'}</div></div>
            <div className="kv-pair"><div className="kv-label">Cache hit rate</div><div className="kv-value">{project.cache_hit_rate != null ? (project.cache_hit_rate * 100).toFixed(1) + '%' : '—'}</div></div>
            <div className="kv-pair"><div className="kv-label">Log level</div><div className="kv-value"><span className="badge badge-neutral">{project.log_level}</span></div></div>
          </div>
          <div style={{ marginTop: '1rem' }}>
            <div className="kv-label" style={{ fontSize: '12px', fontWeight: 600, color: '#5f6b7a', marginBottom: '0.3rem' }}>Allowed models</div>
            {project.allowed_models && project.allowed_models.length > 0
              ? project.allowed_models.map(m => <span key={m} className="tag">{m}</span>)
              : <span style={{ color: '#5f6b7a', fontSize: '14px' }}>All models</span>}
          </div>
        </div>
      </div>

      <div className="container">
        <div className="container-header">
          <h2>Members <span className="counter">({(project.members || []).length})</span></h2>
        </div>
        <div className="container-body">
          {memberMsg && <Flash type="success" onDismiss={() => setMemberMsg(null)}>{memberMsg}</Flash>}
          <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '0.75rem', alignItems: 'center' }}>
            <input
              style={{ flex: 1, padding: '0.4rem 0.75rem', background: '#fff', border: '2px solid #7d8998', borderRadius: '8px', fontSize: '14px', fontFamily: 'inherit' }}
              placeholder="Enter user ID to add..."
              value={newMember}
              onChange={e => setNewMember(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && newMember.trim()) { e.preventDefault(); const uid = newMember.trim(); api.post(`/admin/projects/${projectId}/members`, { user_id: uid }).then(() => { setMemberMsg(`Added ${uid}`); setNewMember(''); setProject(prev => ({ ...prev, members: [...(prev.members || []).filter(m => m !== uid), uid] })); }).catch(() => setMemberMsg('Failed to add member')); }}}
            />
            <Btn variant="primary" style={{ fontSize: '13px', padding: '0.4rem 0.75rem' }} onClick={() => { if (newMember.trim()) { const uid = newMember.trim(); api.post(`/admin/projects/${projectId}/members`, { user_id: uid }).then(() => { setMemberMsg(`Added ${uid}`); setNewMember(''); setProject(prev => ({ ...prev, members: [...(prev.members || []).filter(m => m !== uid), uid] })); }).catch(() => setMemberMsg('Failed to add member')); }}}>Add</Btn>
          </div>
          {project.members && project.members.length > 0
            ? project.members.map(u => (
              <span key={u} className="tag" style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3rem' }}>
                {u}
                <span style={{ cursor: 'pointer', color: '#d91515', fontWeight: 700, fontSize: '11px' }} onClick={() => { api.del(`/admin/projects/${projectId}/members/${u}`).then(() => { setMemberMsg(`Removed ${u}`); setProject(prev => ({ ...prev, members: (prev.members || []).filter(m => m !== u) })); }).catch(() => setMemberMsg('Failed to remove member')); }}>✕</span>
              </span>
            ))
            : <span style={{ color: '#5f6b7a', fontSize: '14px' }}>No members added yet</span>}
        </div>
      </div>

      <div className="container">
        <div className="container-header">
          <h2>Model Access <span className="counter">({projectModels.length})</span></h2>
        </div>
        <div className="container-body">
          {modelMsg && <Flash type="success" onDismiss={() => setModelMsg(null)}>{modelMsg}</Flash>}
          {modelError && <Flash type="error" onDismiss={() => setModelError(null)}>{modelError}</Flash>}
          <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '0.75rem', alignItems: 'center' }}>
            <select
              style={{ flex: 1, padding: '0.4rem 0.75rem', background: '#fff', border: '2px solid #7d8998', borderRadius: '8px', fontSize: '14px', fontFamily: 'inherit', appearance: 'none', backgroundImage: "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%235f6b7a' viewBox='0 0 16 16'%3E%3Cpath d='M8 11L3 6h10z'/%3E%3C/svg%3E\")", backgroundRepeat: 'no-repeat', backgroundPosition: 'right 0.75rem center', paddingRight: '2rem' }}
              value={addingModel}
              onChange={e => setAddingModel(e.target.value)}
            >
              <option value="">Select a model to add...</option>
              {allModels.filter(m => !projectModels.includes(m)).map(m => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
            <Btn variant="primary" style={{ fontSize: '13px', padding: '0.4rem 0.75rem' }} onClick={() => handleAddModel(addingModel)} disabled={!addingModel}>Add</Btn>
          </div>
          {projectModels.length > 0
            ? projectModels.map(m => (
              <span key={m} className="tag" style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3rem' }}>
                {m}
                <span style={{ cursor: 'pointer', color: '#d91515', fontWeight: 700, fontSize: '11px' }} onClick={() => handleRemoveModel(m)}>✕</span>
              </span>
            ))
            : <span style={{ color: '#5f6b7a', fontSize: '14px' }}>All models allowed</span>}
        </div>
      </div>

      <div className="container">
        <div className="container-header"><h2>Active Users <span className="counter">({(project.users || []).length})</span></h2></div>
        <div className="container-body">
          {project.users && project.users.length > 0
            ? project.users.map(u => <span key={u} className="tag">{u}</span>)
            : <span style={{ color: '#5f6b7a', fontSize: '14px' }}>No usage recorded</span>}
        </div>
      </div>

      {userEntries.length > 0 && <BreakdownTable title="Usage by user" entries={userEntries} keyLabel="User" />}
      {modelEntries.length > 0 && <BreakdownTable title="Usage by model" entries={modelEntries} keyLabel="Model" />}
      {providerEntries.length > 0 && <BreakdownTable title="Usage by provider" entries={providerEntries} keyLabel="Provider" />}

      {project.guardrail_rules && project.guardrail_rules.length > 0 && (
        <div className="container">
          <div className="container-header"><h2>Guardrail rules</h2></div>
          <div className="container-body no-pad">
            <table>
              <thead><tr><th>Name</th><th>Type</th><th>Pattern</th><th>Action</th><th>Applies to</th></tr></thead>
              <tbody>
                {project.guardrail_rules.map((g, i) => (
                  <tr key={i}>
                    <td><strong>{g.name}</strong></td>
                    <td><span className="badge badge-neutral">{g.rule_type}</span></td>
                    <td><code>{g.pattern || '—'}</code></td>
                    <td><span className={`badge ${g.action === 'block' ? 'badge-red' : 'badge-yellow'}`}>{g.action}</span></td>
                    <td>{g.applies_to}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function BreakdownTable({ title, entries, keyLabel }) {
  return (
    <div className="container">
      <div className="container-header"><h2>{title} <span className="counter">({entries.length})</span></h2></div>
      <div className="container-body no-pad">
        <table>
          <thead><tr><th>{keyLabel}</th><th>Requests</th><th>Tokens</th><th>Cost</th></tr></thead>
          <tbody>
            {entries.map(([key, d]) => (
              <tr key={key}><td><strong>{key}</strong></td><td>{d.requests}</td><td>{fmt.num(d.tokens)}</td><td>{fmt.cost(d.cost)}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Project Form                                                       */
/* ================================================================== */
function ProjectFormPage({ projectId, onBack, onSaved }) {
  const isEdit = !!projectId;
  const [form, setForm] = useState({ name: '', budget_limit: '', alert_threshold: '', allowed_models: '', cache_enabled: false, cache_ttl_seconds: '300', semantic_cache_enabled: false, semantic_cache_threshold: '', log_level: 'INFO', rate_limit_rpm: '', guardrail_rules: [] });
  const [loading, setLoading] = useState(isEdit);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (isEdit) {
      api.get(`/admin/projects/${projectId}`).then(p => {
        setForm({ name: p.name || '', budget_limit: p.budget_limit != null ? String(p.budget_limit) : '', alert_threshold: p.alert_threshold != null ? String(p.alert_threshold) : '', allowed_models: (p.allowed_models || []).join(', '), cache_enabled: p.cache_enabled || false, cache_ttl_seconds: String(p.cache_ttl_seconds || 300), semantic_cache_enabled: p.semantic_cache_enabled || false, semantic_cache_threshold: p.semantic_cache_threshold != null ? String(p.semantic_cache_threshold) : '', log_level: p.log_level || 'INFO', rate_limit_rpm: '', guardrail_rules: p.guardrail_rules || [] });
        setLoading(false);
      }).catch(() => { setError('Failed to load project'); setLoading(false); });
    }
  }, [projectId, isEdit]);

  const handleChange = (field, value) => setForm(prev => ({ ...prev, [field]: value }));

  const handleSubmit = async (e) => {
    e.preventDefault(); setSaving(true); setMsg(null); setError(null);
    const body = { name: form.name, budget_limit: form.budget_limit ? parseFloat(form.budget_limit) : null, alert_threshold: form.alert_threshold ? parseFloat(form.alert_threshold) : null, allowed_models: form.allowed_models ? form.allowed_models.split(',').map(s => s.trim()).filter(Boolean) : null, cache_enabled: form.cache_enabled, cache_ttl_seconds: parseInt(form.cache_ttl_seconds) || 300, semantic_cache_enabled: form.semantic_cache_enabled,
      /* Blank means "use the gateway default", which is null and not 0 —
         a threshold of 0 would make every stored entry a match. */
      semantic_cache_threshold: form.semantic_cache_threshold.trim() ? parseFloat(form.semantic_cache_threshold) : null, log_level: form.log_level, rate_limit_rpm: form.rate_limit_rpm ? parseInt(form.rate_limit_rpm) : null, guardrail_rules: form.guardrail_rules };
    try {
      if (isEdit) { await api.put(`/admin/projects/${projectId}`, body); setMsg('Project updated successfully.'); }
      else { await api.post('/admin/projects', body); setMsg('Project created successfully.'); }
      if (onSaved) setTimeout(onSaved, 800);
    } catch { setError('Failed to save project.'); }
    setSaving(false);
  };

  if (loading) return <Loading />;

  const formLabelStyle = { display: 'block', fontSize: '14px', fontWeight: 600, marginBottom: '0.3rem', color: '#000716' };
  const formInputStyle = { width: '100%', padding: '0.5rem 0.75rem', background: '#fff', border: '2px solid #7d8998', borderRadius: '8px', fontSize: '14px', color: '#000716', fontFamily: 'inherit', outline: 'none' };
  const formSelectStyle = { ...formInputStyle, appearance: 'none', backgroundImage: "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%235f6b7a' viewBox='0 0 16 16'%3E%3Cpath d='M8 11L3 6h10z'/%3E%3C/svg%3E\")", backgroundRepeat: 'no-repeat', backgroundPosition: 'right 0.75rem center', paddingRight: '2rem' };

  return (
    <div>
      <Breadcrumb items={[{ label: 'Projects', onClick: onBack }, { label: isEdit ? 'Edit' : 'Create' }]} />
      <div className="page-header"><h1>{isEdit ? 'Edit project' : 'Create project'}</h1></div>
      {msg && <Flash type="success">{msg}</Flash>}
      {error && <Flash type="error">{error}</Flash>}
      <div className="container">
        <div className="container-header"><h2>Project settings</h2></div>
        <div className="container-body">
          <form onSubmit={handleSubmit}>
            <div style={{ marginBottom: '1rem' }}><label style={formLabelStyle}>Project name</label><input style={formInputStyle} value={form.name} onChange={e => handleChange('name', e.target.value)} required placeholder="My Project" /></div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', marginBottom: '1rem' }}>
              <div><label style={formLabelStyle}>Budget limit ($)</label><input style={formInputStyle} type="number" step="0.01" value={form.budget_limit} onChange={e => handleChange('budget_limit', e.target.value)} placeholder="No limit" /></div>
              <div><label style={formLabelStyle}>Alert threshold ($)</label><input style={formInputStyle} type="number" step="0.01" value={form.alert_threshold} onChange={e => handleChange('alert_threshold', e.target.value)} placeholder="No alert" /></div>
            </div>
            <div style={{ marginBottom: '1rem' }}><label style={formLabelStyle}>Allowed models</label><input style={formInputStyle} value={form.allowed_models} onChange={e => handleChange('allowed_models', e.target.value)} placeholder="gpt-4, claude-3 (leave empty for all)" /><div style={{ fontSize: '12px', color: '#5f6b7a', marginTop: '0.2rem' }}>Comma-separated list of model names</div></div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', marginBottom: '1rem' }}>
              <div><label style={{ ...formLabelStyle, display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}><input type="checkbox" checked={form.cache_enabled} onChange={e => handleChange('cache_enabled', e.target.checked)} style={{ width: '16px', height: '16px', accentColor: '#0972d3' }} /> Enable caching</label></div>
              <div><label style={formLabelStyle}>Cache TTL (seconds)</label><input style={formInputStyle} type="number" value={form.cache_ttl_seconds} onChange={e => handleChange('cache_ttl_seconds', e.target.value)} /></div>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', marginBottom: '1rem' }}>
              <div><label style={{ ...formLabelStyle, display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}><input type="checkbox" checked={form.semantic_cache_enabled} onChange={e => handleChange('semantic_cache_enabled', e.target.checked)} style={{ width: '16px', height: '16px', accentColor: '#0972d3' }} /> Enable semantic caching</label><div style={{ fontSize: '12px', color: '#5f6b7a', marginTop: '0.2rem' }}>Also serves a reworded question its earlier answer. Needs <code>AXON_SEMANTIC_CACHE=true</code> on the gateway.</div></div>
              <div><label style={formLabelStyle}>Similarity threshold</label><input style={formInputStyle} type="number" step="0.01" min="0.01" max="1" value={form.semantic_cache_threshold} onChange={e => handleChange('semantic_cache_threshold', e.target.value)} placeholder="Gateway default (0.90)" /><div style={{ fontSize: '12px', color: '#5f6b7a', marginTop: '0.2rem' }}>Between 0 and 1, exclusive of 0. Lower means more hits and more wrong ones.</div></div>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', marginBottom: '1rem' }}>
              <div><label style={formLabelStyle}>Log level</label><select style={formSelectStyle} value={form.log_level} onChange={e => handleChange('log_level', e.target.value)}><option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option></select></div>
              <div><label style={formLabelStyle}>Rate limit (RPM)</label><input style={formInputStyle} type="number" value={form.rate_limit_rpm} onChange={e => handleChange('rate_limit_rpm', e.target.value)} placeholder="Default" /></div>
            </div>
            <div style={{ marginTop: '1.25rem', marginBottom: '1rem' }}>
              <h3 style={{ fontSize: '16px', fontWeight: 700, color: '#000716', marginBottom: '0.75rem' }}>Guardrail rules</h3>
              <GuardrailEditor rules={form.guardrail_rules} onChange={rules => handleChange('guardrail_rules', rules)} />
            </div>
            <div style={{ marginTop: '1.5rem', display: 'flex', gap: '0.5rem', borderTop: '1px solid #e9ebed', paddingTop: '1.25rem' }}>
              <Btn variant="primary" type="submit" disabled={saving}>{saving ? 'Saving...' : isEdit ? 'Save changes' : 'Create project'}</Btn>
              <Btn variant="normal" type="button" onClick={onBack}>Cancel</Btn>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}

function GuardrailEditor({ rules, onChange }) {
  const addRule = () => onChange([...rules, { name: '', rule_type: 'keyword_block', pattern: '', action: 'block', applies_to: 'both' }]);
  const removeRule = (i) => onChange(rules.filter((_, idx) => idx !== i));
  const updateRule = (i, field, value) => { const u = [...rules]; u[i] = { ...u[i], [field]: value }; onChange(u); };
  const inputStyle = { width: '100%', padding: '0.4rem 0.6rem', background: '#fff', border: '2px solid #7d8998', borderRadius: '8px', fontSize: '13px', color: '#000716', fontFamily: 'inherit' };
  const selectStyle = { ...inputStyle, appearance: 'none' };
  const labelStyle = { display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '0.2rem', color: '#5f6b7a' };

  return (
    <div>
      {rules.map((rule, i) => (
        <div key={i} style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 0.8fr 0.8fr auto', gap: '0.5rem', marginBottom: '0.5rem', alignItems: 'end' }}>
          <div><label style={labelStyle}>Name</label><input style={inputStyle} value={rule.name} onChange={e => updateRule(i, 'name', e.target.value)} /></div>
          <div><label style={labelStyle}>Type</label><select style={selectStyle} value={rule.rule_type} onChange={e => updateRule(i, 'rule_type', e.target.value)}><option value="keyword_block">Keyword</option><option value="regex_match">Regex</option><option value="content_category">Category</option></select></div>
          <div><label style={labelStyle}>Pattern</label><input style={inputStyle} value={rule.pattern || ''} onChange={e => updateRule(i, 'pattern', e.target.value)} /></div>
          <div><label style={labelStyle}>Action</label><select style={selectStyle} value={rule.action} onChange={e => updateRule(i, 'action', e.target.value)}><option value="block">Block</option><option value="warn">Warn</option><option value="redact">Redact</option></select></div>
          <div><label style={labelStyle}>Scope</label><select style={selectStyle} value={rule.applies_to} onChange={e => updateRule(i, 'applies_to', e.target.value)}><option value="both">Both</option><option value="request">Request</option><option value="response">Response</option></select></div>
          <Btn variant="danger" style={{ marginBottom: '0.1rem', padding: '0.35rem 0.5rem', fontSize: '12px' }} onClick={() => removeRule(i)}>✕</Btn>
        </div>
      ))}
      <Btn variant="normal" style={{ fontSize: '13px', padding: '0.3rem 0.75rem' }} onClick={addRule}>+ Add rule</Btn>
    </div>
  );
}

/* ================================================================== */
/*  Users Page                                                         */
/* ================================================================== */
function UsersPage({ onSelect }) {
  const [users, setUsers] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => { api.get('/admin/users').then(setUsers).catch(e => setError('Failed to load users: ' + (e && e.message ? e.message : 'unknown error'))); }, []);

  if (error) return <Flash type="error">{error}</Flash>;
  if (!users) return <Loading text="Loading users..." />;

  return (
    <div>
      <div className="page-header"><h1>Users</h1><p>All users with aggregated usage metrics and budgets</p></div>
      {users.length === 0 ? (
        <div className="container"><EmptyState title="No users" subtitle="No user activity recorded yet." /></div>
      ) : (
        <div className="container">
          <div className="container-header"><h2>Users <span className="counter">({users.length})</span></h2></div>
          <div className="container-body no-pad">
            <table>
              <thead><tr><th>User</th><th>Projects</th><th>Spend</th><th>Budget</th><th>Utilization</th><th>Requests</th></tr></thead>
              <tbody>
                {users.map(u => (
                  <tr key={u.user_id} className="clickable" onClick={() => onSelect(u.user_id)}>
                    <td><strong style={{ color: '#0972d3' }}>{u.user_id}</strong></td>
                    <td>{u.projects.map(p => <span key={p} className="tag">{p}</span>)}</td>
                    <td>{fmt.cost(u.cost)}</td>
                    <td>{u.budget_limit != null ? `$${u.budget_limit.toFixed(2)}` : <span style={{ color: '#5f6b7a' }}>—</span>}</td>
                    <td style={{ minWidth: '130px' }}>
                      {u.budget_utilization_pct != null ? (
                        <div>
                          <span style={{ fontSize: '13px', fontWeight: 600 }}>{fmt.pct(u.budget_utilization_pct)}</span>
                          <ProgressBar value={u.budget_utilization_pct} color={u.budget_utilization_pct > 90 ? '#d91515' : u.budget_utilization_pct > 70 ? '#8d6605' : '#037f0c'} />
                        </div>
                      ) : <span style={{ color: '#5f6b7a' }}>—</span>}
                    </td>
                    <td>{u.requests}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

/* ================================================================== */
/*  User Detail                                                        */
/* ================================================================== */
function UserDetailPage({ userId, onBack }) {
  const [user, setUser] = useState(null);
  const [error, setError] = useState(null);
  const [editBudget, setEditBudget] = useState(false);
  const [budgetForm, setBudgetForm] = useState({ budget_limit: '', alert_threshold: '' });
  const [budgetMsg, setBudgetMsg] = useState(null);
  const [editAccess, setEditAccess] = useState(false);
  const [accessModels, setAccessModels] = useState([]);
  const [allModels, setAllModels] = useState([]);
  const [accessMsg, setAccessMsg] = useState(null);

  const loadUser = useCallback(() => {
    api.get(`/admin/users/${userId}`).then(u => {
      setUser(u);
      setBudgetForm({
        budget_limit: u.budget_limit != null ? String(u.budget_limit) : '',
        alert_threshold: u.alert_threshold != null ? String(u.alert_threshold) : '',
      });
      setAccessModels(u.allowed_models || []);
    }).catch(e => setError('Failed to load user: ' + (e && e.message ? e.message : 'unknown error')));
    api.get('/admin/models').then(models => {
      setAllModels(models.map(m => m.name));
    }).catch(() => {});
  }, [userId]);

  useEffect(() => { loadUser(); }, [loadUser]);

  const saveBudget = async () => {
    setBudgetMsg(null);
    const body = {
      budget_limit: budgetForm.budget_limit ? parseFloat(budgetForm.budget_limit) : null,
      alert_threshold: budgetForm.alert_threshold ? parseFloat(budgetForm.alert_threshold) : null,
    };
    try {
      await api.put(`/admin/users/${userId}/budget`, body);
      setBudgetMsg('Budget updated successfully.');
      setEditBudget(false);
      loadUser();
    } catch { setBudgetMsg('Failed to update budget.'); }
  };

  const saveAccess = async () => {
    setAccessMsg(null);
    const allowed = accessModels.length > 0 ? accessModels : null;
    try {
      await api.put(`/admin/users/${userId}/allowed-models`, { allowed_models: allowed });
      setAccessMsg('Model access updated successfully.');
      setEditAccess(false);
      loadUser();
    } catch { setAccessMsg('Failed to update model access.'); }
  };

  const toggleModel = (modelName) => {
    setAccessModels(prev =>
      prev.includes(modelName)
        ? prev.filter(m => m !== modelName)
        : [...prev, modelName]
    );
  };

  if (error) return <div><Breadcrumb items={[{ label: 'Users', onClick: onBack }, { label: userId }]} /><Flash type="error">{error}</Flash></div>;
  if (!user) return <Loading text="Loading user..." />;

  const projectEntries = Object.entries(user.usage_by_project || {});
  const modelEntries = Object.entries(user.usage_by_model || {});
  const providerEntries = Object.entries(user.usage_by_provider || {});
  const budgetLimit = user.budget_limit;
  const utilization = budgetLimit ? (user.total_cost / budgetLimit * 100) : null;

  const formInputStyle = { width: '100%', padding: '0.5rem 0.75rem', background: '#fff', border: '2px solid #7d8998', borderRadius: '8px', fontSize: '14px', color: '#000716', fontFamily: 'inherit' };
  const formLabelStyle = { display: 'block', fontSize: '14px', fontWeight: 600, marginBottom: '0.3rem', color: '#000716' };

  return (
    <div>
      <Breadcrumb items={[{ label: 'Users', onClick: onBack }, { label: user.user_id }]} />
      <div className="page-header"><h1>{user.user_id}</h1><p>Member of {user.projects.length} project{user.projects.length !== 1 ? 's' : ''}</p></div>

      {budgetMsg && <Flash type="success">{budgetMsg}</Flash>}

      <div className="stat-grid">
        <div className="stat-card"><div className="stat-label">Requests</div><div className="stat-value">{user.total_requests}</div></div>
        <div className="stat-card"><div className="stat-label">Tokens</div><div className="stat-value">{fmt.num(user.total_tokens)}</div></div>
        <div className="stat-card"><div className="stat-label">Total Spend</div><div className="stat-value">{fmt.cost(user.total_cost)}</div></div>
        <div className="stat-card">
          <div className="stat-label">Budget</div>
          <div className="stat-value">{budgetLimit != null ? `$${budgetLimit}` : '—'}</div>
          {utilization != null && <ProgressBar value={utilization} color={utilization > 90 ? '#d91515' : utilization > 70 ? '#8d6605' : '#037f0c'} />}
        </div>
      </div>

      <div className="container">
        <div className="container-header">
          <h2>Budget configuration</h2>
          {!editBudget && <Btn variant="normal" style={{ fontSize: '13px', padding: '0.3rem 0.75rem' }} onClick={() => setEditBudget(true)}>Edit budget</Btn>}
        </div>
        <div className="container-body">
          {editBudget ? (
            <div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', marginBottom: '1rem' }}>
                <div><label style={formLabelStyle}>Budget limit ($)</label><input style={formInputStyle} type="number" step="0.01" value={budgetForm.budget_limit} onChange={e => setBudgetForm(f => ({ ...f, budget_limit: e.target.value }))} placeholder="No limit" /></div>
                <div><label style={formLabelStyle}>Alert threshold ($)</label><input style={formInputStyle} type="number" step="0.01" value={budgetForm.alert_threshold} onChange={e => setBudgetForm(f => ({ ...f, alert_threshold: e.target.value }))} placeholder="No alert" /></div>
              </div>
              <div style={{ display: 'flex', gap: '0.5rem' }}>
                <Btn variant="primary" onClick={saveBudget}>Save budget</Btn>
                <Btn variant="normal" onClick={() => setEditBudget(false)}>Cancel</Btn>
              </div>
            </div>
          ) : (
            <div className="column-layout">
              <div className="kv-pair"><div className="kv-label">Budget limit</div><div className="kv-value">{budgetLimit != null ? `$${budgetLimit}` : '—'}</div></div>
              <div className="kv-pair"><div className="kv-label">Alert threshold</div><div className="kv-value">{user.alert_threshold != null ? `$${user.alert_threshold}` : '—'}</div></div>
              <div className="kv-pair"><div className="kv-label">Current spend</div><div className="kv-value">{fmt.cost(user.total_cost)}</div></div>
              <div className="kv-pair"><div className="kv-label">Utilization</div><div className="kv-value">{utilization != null ? fmt.pct(utilization) : '—'}</div></div>
            </div>
          )}
        </div>
      </div>

      <div className="container">
        <div className="container-header"><h2>Projects <span className="counter">({user.projects.length})</span></h2></div>
        <div className="container-body">{user.projects.map(p => <span key={p} className="tag">{p}</span>)}</div>
      </div>

      <div className="container">
        <div className="container-header">
          <h2>Model access</h2>
          {!editAccess && <Btn variant="normal" style={{ fontSize: '13px', padding: '0.3rem 0.75rem' }} onClick={() => { setEditAccess(true); setAccessModels(user.allowed_models || []); }}>Edit access</Btn>}
        </div>
        <div className="container-body">
          {accessMsg && <Flash type="success" onDismiss={() => setAccessMsg(null)}>{accessMsg}</Flash>}
          {editAccess ? (
            <div>
              <p style={{ fontSize: '13px', color: '#5f6b7a', marginBottom: '0.75rem' }}>
                Select which models this user can access. Leave all unchecked to allow access to all models.
              </p>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem', marginBottom: '1rem' }}>
                {allModels.map(m => (
                  <label key={m} style={{ display: 'inline-flex', alignItems: 'center', gap: '0.35rem', padding: '0.35rem 0.75rem', background: accessModels.includes(m) ? '#f2f8fd' : '#f4f6f8', border: accessModels.includes(m) ? '2px solid #0972d3' : '2px solid #e9ebed', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', fontWeight: accessModels.includes(m) ? 600 : 400, color: accessModels.includes(m) ? '#0972d3' : '#5f6b7a', transition: 'all 100ms' }}>
                    <input type="checkbox" checked={accessModels.includes(m)} onChange={() => toggleModel(m)} style={{ display: 'none' }} />
                    {accessModels.includes(m) ? '✓' : ''} {m}
                  </label>
                ))}
              </div>
              <div style={{ display: 'flex', gap: '0.5rem' }}>
                <Btn variant="primary" onClick={saveAccess}>Save access</Btn>
                <Btn variant="normal" onClick={() => setEditAccess(false)}>Cancel</Btn>
                {accessModels.length > 0 && <Btn variant="link" style={{ color: '#d91515' }} onClick={() => setAccessModels([])}>Clear all (allow all)</Btn>}
              </div>
            </div>
          ) : (
            <div>
              {user.allowed_models && user.allowed_models.length > 0 ? (
                <div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.25rem' }}>
                    {user.allowed_models.map(m => <span key={m} className="tag">{m}</span>)}
                  </div>
                </div>
              ) : (
                <span style={{ color: '#5f6b7a', fontSize: '14px' }}>All models (no restrictions)</span>
              )}
            </div>
          )}
        </div>
      </div>

      {projectEntries.length > 0 && <BreakdownTable title="Usage by project" entries={projectEntries} keyLabel="Project" />}
      {modelEntries.length > 0 && <BreakdownTable title="Usage by model" entries={modelEntries} keyLabel="Model" />}
      {providerEntries.length > 0 && <BreakdownTable title="Usage by provider" entries={providerEntries} keyLabel="Provider" />}
    </div>
  );
}

/* ================================================================== */
/*  Models Page                                                        */
/* ================================================================== */
function ModelsPage() {
  const [models, setModels] = useState(null);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [msg, setMsg] = useState(null);

  const loadModels = useCallback(() => { api.get('/admin/models').then(setModels).catch(e => setError('Failed to load models: ' + (e && e.message ? e.message : 'unknown error'))); }, []);
  useEffect(() => { loadModels(); }, [loadModels]);

  const strategies = ['round-robin', 'weighted', 'least-latency', 'cost-optimized'];

  const handleStrategyChange = async (modelName, newStrategy) => {
    setMsg(null);
    try {
      await api.put('/admin/models/' + modelName, { routing_strategy: newStrategy });
      setMsg('Routing strategy for "' + modelName + '" updated to ' + newStrategy + '.');
      loadModels();
    } catch { setMsg(null); }
  };

  useEffect(() => { loadModels(); }, [loadModels]);

  if (error) return <Flash type="error">{error}</Flash>;
  if (!models) return <Loading text="Loading models..." />;

  return (
    <div>
      <div className="page-header"><h1>Models</h1><p>Model routing configuration and provider mappings</p></div>
      {msg && <Flash type="success">{msg}</Flash>}
      {models.length === 0 ? (
        <div className="container"><EmptyState title="No models" subtitle="No models configured yet." /></div>
      ) : (
        <div className="container">
          <div className="container-header"><h2>Models <span className="counter">({models.length})</span></h2></div>
          <div className="container-body no-pad">
            <table>
              <thead><tr><th>Model</th><th>Strategy</th><th>Providers</th><th>Requests</th><th>Tokens</th><th>Cost</th></tr></thead>
              <tbody>
                {models.map(m => (
                  <React.Fragment key={m.name}>
                    <tr className="clickable" onClick={() => setExpanded(expanded === m.name ? null : m.name)}>
                      <td>
                        <strong style={{ color: '#0972d3' }}>{m.name}</strong>
                        <div className="sub-text">{m.description}</div>
                      </td>
                      <td><select value={m.routing_strategy} onChange={(e) => { e.stopPropagation(); handleStrategyChange(m.name, e.target.value); }} onClick={(e) => e.stopPropagation()} style={{ padding: '4px 8px', borderRadius: '6px', border: '2px solid #7d8998', fontSize: '13px', fontFamily: 'inherit', background: '#fff', color: '#000716', cursor: 'pointer' }}>
                        {strategies.map(s => <option key={s} value={s}>{s}</option>)}
                      </select></td>
                      <td>{m.providers.map(p => p.provider).join(', ')}</td>
                      <td>{m.total_requests}</td>
                      <td>{fmt.num(m.total_tokens)}</td>
                      <td>{fmt.cost(m.total_cost)}</td>
                    </tr>
                    {expanded === m.name && (
                      <tr><td colSpan="6" style={{ background: '#fafafa', padding: '1.25rem' }}>
                        <div style={{ marginBottom: '0.75rem' }}>
                          <div style={{ fontSize: '12px', fontWeight: 700, color: '#5f6b7a', marginBottom: '0.3rem' }}>CAPABILITIES</div>
                          <div>{m.capabilities.length > 0 ? m.capabilities.map(c => <span key={c} className="tag">{c}</span>) : <span style={{ color: '#5f6b7a', fontSize: '13px' }}>None</span>}</div>
                        </div>
                        <div style={{ fontSize: '12px', fontWeight: 700, color: '#5f6b7a', marginBottom: '0.3rem' }}>PROVIDER MAPPINGS</div>
                        <table style={{ marginTop: '0.25rem' }}>
                          <thead><tr><th>Provider</th><th>Model ID</th><th>Weight</th><th>Fallback order</th></tr></thead>
                          <tbody>
                            {m.providers.map((p, i) => (
                              <tr key={i}><td><strong>{p.provider}</strong></td><td><code>{p.model_id}</code></td><td>{p.weight}</td><td>{p.fallback_order}</td></tr>
                            ))}
                          </tbody>
                        </table>
                      </td></tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

/* ================================================================== */
/*  Policies Page                                                      */
/* ================================================================== */
function PoliciesPage() {
  const [policies, setPolicies] = useState(null);
  const [error, setError] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [editPolicy, setEditPolicy] = useState(null);
  const [msg, setMsg] = useState(null);

  const loadPolicies = useCallback(() => { api.get('/admin/policies').then(setPolicies).catch(e => setError('Failed to load policies: ' + (e && e.message ? e.message : 'unknown error'))); }, []);
  useEffect(() => { loadPolicies(); }, [loadPolicies]);

  const handleSave = async (policy) => {
    setMsg(null);
    try { const r = await api.post('/admin/policies', policy); setMsg(`Policy "${policy.name}" ${r.status}.`); setShowForm(false); setEditPolicy(null); loadPolicies(); }
    catch { setMsg(null); }
  };

  if (error) return <Flash type="error">{error}</Flash>;
  if (!policies) return <Loading text="Loading policies..." />;

  const formLabelStyle = { display: 'block', fontSize: '14px', fontWeight: 600, marginBottom: '0.3rem', color: '#000716' };
  const formInputStyle = { width: '100%', padding: '0.5rem 0.75rem', background: '#fff', border: '2px solid #7d8998', borderRadius: '8px', fontSize: '14px', color: '#000716', fontFamily: 'inherit' };

  return (
    <div>
      <div className="page-header page-header-actions">
        <div><h1>Cedar policies</h1><p>Authorization policies for access control</p></div>
        <Btn variant="primary" onClick={() => { setEditPolicy(null); setShowForm(true); }}>Create policy</Btn>
      </div>
      {msg && <Flash type="success">{msg}</Flash>}
      {showForm && (
        <div className="container" style={{ marginBottom: '1.25rem' }}>
          <div className="container-header"><h2>{editPolicy ? 'Edit policy' : 'Create policy'}</h2></div>
          <div className="container-body">
            <PolicyFormInline initial={editPolicy} onSave={handleSave} onCancel={() => { setShowForm(false); setEditPolicy(null); }} />
          </div>
        </div>
      )}
      {policies.length === 0 ? (
        <div className="container"><EmptyState title="No policies" subtitle="Create a policy to get started." action={<Btn variant="primary" onClick={() => setShowForm(true)}>Create policy</Btn>} /></div>
      ) : (
        <div className="container">
          <div className="container-header"><h2>Policies <span className="counter">({policies.length})</span></h2></div>
          <div className="container-body no-pad">
            <table>
              <thead><tr><th>Name</th><th>Description</th><th>Mode</th><th>Actions</th></tr></thead>
              <tbody>
                {policies.map((p, i) => (
                  <tr key={i}>
                    <td><strong>{p.name}</strong></td>
                    <td>{p.description || <span style={{ color: '#5f6b7a' }}>—</span>}</td>
                    <td><span className={`badge ${p.mode === 'ENFORCE' ? 'badge-blue' : 'badge-yellow'}`}>{p.mode}</span></td>
                    <td><Btn variant="link" onClick={() => { setEditPolicy(p); setShowForm(true); }}>Edit</Btn></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function PolicyFormInline({ initial, onSave, onCancel }) {
  const [form, setForm] = useState({ name: initial?.name || '', description: initial?.description || '', policy_text: initial?.policy_text || '', mode: initial?.mode || 'LOG_ONLY' });
  const handleChange = (field, value) => setForm(prev => ({ ...prev, [field]: value }));
  const formLabelStyle = { display: 'block', fontSize: '14px', fontWeight: 600, marginBottom: '0.3rem', color: '#000716' };
  const formInputStyle = { width: '100%', padding: '0.5rem 0.75rem', background: '#fff', border: '2px solid #7d8998', borderRadius: '8px', fontSize: '14px', color: '#000716', fontFamily: 'inherit' };
  const formSelectStyle = { ...formInputStyle, appearance: 'none' };

  return (
    <div>
      <div style={{ marginBottom: '1rem' }}><label style={formLabelStyle}>Policy name</label><input style={formInputStyle} value={form.name} onChange={e => handleChange('name', e.target.value)} disabled={!!initial} /></div>
      <div style={{ marginBottom: '1rem' }}><label style={formLabelStyle}>Description</label><input style={formInputStyle} value={form.description} onChange={e => handleChange('description', e.target.value)} /></div>
      <div style={{ marginBottom: '1rem' }}><label style={formLabelStyle}>Cedar policy text</label><textarea style={{ ...formInputStyle, minHeight: '80px', resize: 'vertical', fontFamily: "'SF Mono', 'Fira Code', monospace", fontSize: '13px' }} value={form.policy_text} onChange={e => handleChange('policy_text', e.target.value)} placeholder="permit(principal, action, resource) when { ... };" /></div>
      <div style={{ marginBottom: '1rem' }}><label style={formLabelStyle}>Mode</label><select style={formSelectStyle} value={form.mode} onChange={e => handleChange('mode', e.target.value)}><option value="LOG_ONLY">LOG_ONLY (Test)</option><option value="ENFORCE">ENFORCE (Production)</option></select></div>
      <div style={{ display: 'flex', gap: '0.5rem' }}>
        <Btn variant="primary" onClick={() => onSave(form)} disabled={!form.name}>Save policy</Btn>
        <Btn variant="normal" onClick={onCancel}>Cancel</Btn>
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Traces Page                                                        */
/* ================================================================== */
function TracesPage() {
  const [traces, setTraces] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [filter, setFilter] = useState('');
  const [sortKey, setSortKey] = useState('timestamp');
  const [sortDir, setSortDir] = useState('desc');

  const loadTraces = () => {
    api.get('/admin/traces?limit=200')
      .then(d => { setTraces(d.traces || []); setTotal(d.total || 0); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(() => { loadTraces(); }, []);
  useEffect(() => {
    if (!autoRefresh) return;
    const interval = setInterval(loadTraces, 3000);
    return () => clearInterval(interval);
  }, [autoRefresh]);

  const toggleSort = (key) => {
    if (sortKey === key) setSortDir(sortDir === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('desc'); }
  };

  const filtered = (filter
    ? traces.filter(t => t.model.includes(filter) || t.provider.includes(filter) || t.user_id.includes(filter))
    : traces
  ).sort((a, b) => {
    let av = a[sortKey], bv = b[sortKey];
    if (av == null) av = '';
    if (bv == null) bv = '';
    if (typeof av === 'string') { av = av.toLowerCase(); bv = (bv || '').toLowerCase(); }
    if (av < bv) return sortDir === 'asc' ? -1 : 1;
    if (av > bv) return sortDir === 'asc' ? 1 : -1;
    return 0;
  });

  const tierColor = (latency) => {
    if (latency < 500) return '#16a34a';
    if (latency < 2000) return '#d97706';
    return '#dc2626';
  };

  const stats = {
    total: traces.length,
    avgLatency: traces.length > 0 ? Math.round(traces.reduce((s, t) => s + (t.latency_ms || 0), 0) / traces.length) : 0,
    totalCost: traces.reduce((s, t) => s + t.cost, 0),
    providers: [...new Set(traces.map(t => t.provider))].length,
  };

  if (loading) return <Loading text="Loading traces..." />;

  return (
    <div>
      <div className="page-header" style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between'}}>
        <div>
          <h1>Request Traces</h1>
          <p style={{fontSize: '13px', color: '#78716c', marginTop: '0.15rem'}}>Live view of all requests flowing through the gateway</p>
        </div>
        <div style={{display: 'flex', gap: '0.5rem', alignItems: 'center'}}>
          <span style={{fontSize: '11px', color: '#78716c'}}>{total} total requests</span>
          <label style={{display: 'flex', alignItems: 'center', gap: '0.3rem', fontSize: '12px', color: autoRefresh ? '#16a34a' : '#78716c', cursor: 'pointer'}}>
            <input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} />
            {autoRefresh ? 'Live' : 'Paused'}
          </label>
          <button onClick={loadTraces} className="btn" style={{fontSize: '11px', padding: '0.3rem 0.6rem'}}>Refresh</button>
        </div>
      </div>

      <div className="stat-grid" style={{marginBottom: '1rem'}}>
        <div className="stat-card"><div className="stat-label">Requests</div><div className="stat-value">{stats.total}</div></div>
        <div className="stat-card"><div className="stat-label">Avg Latency</div><div className="stat-value">{stats.avgLatency}ms</div></div>
        <div className="stat-card"><div className="stat-label">Total Cost</div><div className="stat-value">{fmt.cost(stats.totalCost)}</div></div>
        <div className="stat-card"><div className="stat-label">Providers</div><div className="stat-value">{stats.providers}</div></div>
      </div>

      <div className="container">
        <div className="container-header" style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between'}}>
          <h2>Live Traces <span className="counter">({filtered.length})</span></h2>
          <input value={filter} onChange={e => setFilter(e.target.value)} placeholder="Filter by model, provider, or user..."
            style={{padding: '0.35rem 0.75rem', borderRadius: '8px', border: '1px solid #e7e5e4', fontSize: '12px', width: '250px'}} />
        </div>
        <div className="container-body no-pad" style={{maxHeight: 'calc(100vh - 380px)', overflow: 'auto'}}>
          {filtered.length === 0 ? (
            <div style={{textAlign: 'center', padding: '3rem', color: '#a8a29e'}}>
              <p style={{fontSize: '13px'}}>No traces yet. Send a request to see it here.</p>
            </div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th style={{cursor:'pointer'}} onClick={() => toggleSort('timestamp')}>Time {sortKey==='timestamp' && (sortDir==='asc' ? '↑' : '↓')}</th>
                  <th style={{cursor:'pointer'}} onClick={() => toggleSort('model')}>Model {sortKey==='model' && (sortDir==='asc' ? '↑' : '↓')}</th>
                  <th style={{cursor:'pointer'}} onClick={() => toggleSort('provider')}>Provider {sortKey==='provider' && (sortDir==='asc' ? '↑' : '↓')}</th>
                  <th style={{cursor:'pointer'}} onClick={() => toggleSort('user_id')}>User {sortKey==='user_id' && (sortDir==='asc' ? '↑' : '↓')}</th>
                  <th style={{cursor:'pointer'}} onClick={() => toggleSort('latency_ms')}>Latency {sortKey==='latency_ms' && (sortDir==='asc' ? '↑' : '↓')}</th>
                  <th style={{cursor:'pointer'}} onClick={() => toggleSort('total_tokens')}>Tokens {sortKey==='total_tokens' && (sortDir==='asc' ? '↑' : '↓')}</th>
                  <th style={{cursor:'pointer'}} onClick={() => toggleSort('cost')}>Cost {sortKey==='cost' && (sortDir==='asc' ? '↑' : '↓')}</th>
                  <th style={{cursor:'pointer'}} onClick={() => toggleSort('status')}>Status {sortKey==='status' && (sortDir==='asc' ? '↑' : '↓')}</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((t, i) => (
                  <tr key={t.request_id || i}>
                    <td style={{fontSize: '11px', color: '#78716c', whiteSpace: 'nowrap'}}>{t.timestamp ? new Date(t.timestamp).toLocaleTimeString() : '-'}</td>
                    <td><strong>{t.model}</strong></td>
                    <td><span className="badge badge-purple">{t.provider}</span></td>
                    <td style={{fontSize: '12px', color: '#78716c'}}>{t.user_id}</td>
                    <td style={{color: tierColor(t.latency_ms), fontWeight: 600, fontSize: '12px'}}>{t.latency_ms ? `${Math.round(t.latency_ms)}ms` : '-'}</td>
                    <td style={{fontSize: '12px'}}>{t.total_tokens}</td>
                    <td style={{fontSize: '12px'}}>{fmt.cost(t.cost)}</td>
                    <td><span className={`badge ${t.status === 'success' ? 'badge-green' : 'badge-red'}`}>{t.status}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Health Page                                                        */
/* ================================================================== */
function HealthPage() {
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => { api.get('/admin/health').then(setHealth).catch(e => setError('Failed to load health: ' + (e && e.message ? e.message : 'unknown error'))); }, []);

  if (error) return <Flash type="error">{error}</Flash>;
  if (!health) return <Loading text="Loading health..." />;

  const providers = Object.entries(health.providers || {});

  return (
    <div>
      <div className="page-header"><h1>System health</h1><p>Provider connectivity and runtime status</p></div>
      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-label">Runtime</div>
          <div className="stat-value" style={{ fontSize: '18px', marginTop: '0.4rem' }}><span className="badge badge-green" style={{ fontSize: '14px', padding: '0.25rem 0.65rem' }}><span className="badge-dot"></span>{health.runtime || 'unknown'}</span></div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Overall Status</div>
          <div className="stat-value" style={{ fontSize: '18px', marginTop: '0.4rem' }}><span className={`badge ${health.status === 'ok' ? 'badge-green' : 'badge-red'}`} style={{ fontSize: '14px', padding: '0.25rem 0.65rem' }}><span className="badge-dot"></span>{health.status}</span></div>
        </div>
      </div>
      <div className="container">
        <div className="container-header"><h2>Provider status <span className="counter">({providers.length})</span></h2></div>
        <div className="container-body no-pad">
          {providers.length === 0 ? (
            <EmptyState title="No providers" subtitle="No providers configured." />
          ) : (
            <table>
              <thead><tr><th>Provider</th><th>Status</th></tr></thead>
              <tbody>
                {providers.map(([name, status]) => (
                  <tr key={name}>
                    <td><strong>{name}</strong></td>
                    <td><span className={`badge ${status === 'healthy' ? 'badge-green' : 'badge-red'}`}><span className="badge-dot"></span>{status}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Configuration Page                                                 */
/* ================================================================== */
function ConfigurationPage() {
  const [models, setModels] = useState(null);
  const [catalog, setCatalog] = useState(null);
  const [error, setError] = useState(null);
  const [msg, setMsg] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [editModel, setEditModel] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);

  const loadModels = useCallback(() => {
    api.get('/admin/models').then(setModels).catch(e => setError('Failed to load models: ' + (e && e.message ? e.message : 'unknown error')));
  }, []);

  const loadCatalog = useCallback(() => {
    api.get('/admin/catalog').then(setCatalog).catch(() => {});
  }, []);

  useEffect(() => { loadModels(); loadCatalog(); }, [loadModels, loadCatalog]);

  const handleDelete = async (name) => {
    setMsg(null);
    try {
      await api.del('/admin/models/' + encodeURIComponent(name));
      setMsg('Model "' + name + '" deleted successfully.');
      setDeleteTarget(null);
      loadModels();
    } catch { setMsg(null); setError('Failed to delete model.'); setDeleteTarget(null); }
  };

  const handleFormSave = async (formData, isEdit) => {
    setMsg(null); setError(null);
    const body = {
      name: formData.name,
      description: formData.description,
      routing_strategy: formData.routing_strategy,
      providers: formData.providers,
    };
    try {
      if (isEdit) {
        const res = await api.put('/admin/models/' + encodeURIComponent(formData.name), body);
        if (res.errors) { return { errors: res.errors }; }
        setMsg('Model "' + formData.name + '" updated successfully.');
      } else {
        const res = await api.post('/admin/models', body);
        if (res.errors) { return { errors: res.errors }; }
        setMsg('Model "' + formData.name + '" created successfully.');
      }
      setShowForm(false);
      setEditModel(null);
      loadModels();
      return null;
    } catch (e) {
      if (e && e.errors) return { errors: e.errors };
      return { errors: [{ message: 'Failed to save model.' }] };
    }
  };

  if (error && !models) return <Flash type="error">{error}</Flash>;
  if (!models) return <Loading text="Loading configuration..." />;

  return (
    <div>
      <div className="page-header page-header-actions">
        <div><h1>Configuration</h1><p>Manage virtual model configurations and provider mappings</p></div>
        {!showForm && <Btn variant="primary" onClick={() => { setEditModel(null); setShowForm(true); setMsg(null); setError(null); }}>Add model</Btn>}
      </div>
      {msg && <Flash type="success" onDismiss={() => setMsg(null)}>{msg}</Flash>}
      {error && models && <Flash type="error" onDismiss={() => setError(null)}>{error}</Flash>}

      {showForm && (
        <ModelForm
          model={editModel}
          catalog={catalog}
          onSave={handleFormSave}
          onCancel={() => { setShowForm(false); setEditModel(null); }}
        />
      )}

      {deleteTarget && (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,7,22,0.5)', zIndex: 200, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div style={{ background: '#fff', borderRadius: '12px', padding: '1.5rem', maxWidth: '420px', width: '100%', boxShadow: '0 4px 20px rgba(0,0,0,0.15)' }}>
            <h3 style={{ fontSize: '18px', fontWeight: 700, marginBottom: '0.5rem' }}>Delete model</h3>
            <p style={{ fontSize: '14px', color: '#5f6b7a', marginBottom: '1.25rem' }}>
              Are you sure you want to delete <strong>"{deleteTarget}"</strong>? This action cannot be undone.
            </p>
            <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}>
              <Btn variant="normal" onClick={() => setDeleteTarget(null)}>Cancel</Btn>
              <Btn variant="danger" onClick={() => handleDelete(deleteTarget)}>Delete</Btn>
            </div>
          </div>
        </div>
      )}

      {models.length === 0 && !showForm ? (
        <div className="container"><EmptyState title="No models" subtitle="Add a model to get started." action={<Btn variant="primary" onClick={() => { setEditModel(null); setShowForm(true); }}>Add model</Btn>} /></div>
      ) : (
        <div className="container">
          <div className="container-header"><h2>Models <span className="counter">({models.length})</span></h2></div>
          <div className="container-body no-pad">
            <table>
              <thead><tr><th>Name</th><th>Description</th><th>Routing Strategy</th><th>Providers</th><th>Requests</th><th>Tokens</th><th>Cost</th><th>Actions</th></tr></thead>
              <tbody>
                {models.map(m => (
                  <React.Fragment key={m.name}>
                    <tr className="clickable" onClick={() => setExpanded(expanded === m.name ? null : m.name)}>
                      <td><strong style={{ color: '#0972d3' }}>{m.name}</strong></td>
                      <td>{m.description || <span style={{ color: '#5f6b7a' }}>—</span>}</td>
                      <td><span className="badge badge-blue">{m.routing_strategy}</span></td>
                      <td>{m.providers.map(p => p.provider).join(', ')}</td>
                      <td>{m.total_requests || 0}</td>
                      <td>{(m.total_tokens || 0).toLocaleString()}</td>
                      <td>${(m.total_cost || 0).toFixed(4)}</td>
                      <td onClick={e => e.stopPropagation()} style={{ whiteSpace: 'nowrap' }}>
                        <Btn variant="link" onClick={() => { setEditModel(m); setShowForm(true); setMsg(null); setError(null); }}>Edit</Btn>
                        <Btn variant="link" style={{ color: '#d91515', marginLeft: '0.5rem' }} onClick={() => setDeleteTarget(m.name)}>Delete</Btn>
                      </td>
                    </tr>
                    {expanded === m.name && (
                      <tr><td colSpan="8" style={{ background: '#fafafa', padding: '1.25rem' }}>
                        <div style={{ fontSize: '12px', fontWeight: 700, color: '#5f6b7a', marginBottom: '0.3rem' }}>PROVIDER DETAILS</div>
                        <table style={{ marginTop: '0.25rem' }}>
                          <thead><tr><th>Provider</th><th>Model ID</th><th>Weight</th><th>Fallback Order</th></tr></thead>
                          <tbody>
                            {m.providers.map((p, i) => (
                              <tr key={i}>
                                <td><strong>{p.provider}</strong></td>
                                <td><code>{p.model_id}</code></td>
                                <td>{p.weight}</td>
                                <td>{p.fallback_order}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </td></tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function ModelForm({ model, catalog, onSave, onCancel }) {
  const isEdit = !!model;
  const [form, setForm] = useState({
    name: model ? model.name : '',
    description: model ? (model.description || '') : '',
    routing_strategy: model ? model.routing_strategy : 'round-robin',
    providers: model && model.providers.length > 0
      ? model.providers.map(p => ({ provider: p.provider, model_id: p.model_id, weight: String(p.weight), fallback_order: String(p.fallback_order) }))
      : [{ provider: '', model_id: '', weight: '1.0', fallback_order: '0' }],
  });
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState(null);

  const strategies = ['round-robin', 'weighted', 'least-latency', 'cost-optimized'];
  const catalogProviders = catalog ? Object.keys(catalog) : [];

  const handleChange = (field, value) => setForm(prev => ({ ...prev, [field]: value }));

  const updateProvider = (i, field, value) => {
    const updated = [...form.providers];
    updated[i] = { ...updated[i], [field]: value };
    if (field === 'provider') {
      updated[i].model_id = '';
    }
    setForm(prev => ({ ...prev, providers: updated }));
  };

  const addProvider = () => {
    setForm(prev => ({ ...prev, providers: [...prev.providers, { provider: '', model_id: '', weight: '1.0', fallback_order: String(prev.providers.length) }] }));
  };

  const removeProvider = (i) => {
    setForm(prev => ({ ...prev, providers: prev.providers.filter((_, idx) => idx !== i) }));
  };

  const selectCatalogModel = (i, modelId) => {
    const updated = [...form.providers];
    updated[i] = { ...updated[i], model_id: modelId };
    setForm(prev => ({ ...prev, providers: updated }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSaving(true); setErrors(null);
    const body = {
      ...form,
      providers: form.providers.map(p => ({
        provider: p.provider,
        model_id: p.model_id,
        weight: parseFloat(p.weight) || 1.0,
        fallback_order: parseInt(p.fallback_order) || 0,
      })),
    };
    const result = await onSave(body, isEdit);
    if (result && result.errors) {
      setErrors(result.errors);
    }
    setSaving(false);
  };

  const formLabelStyle = { display: 'block', fontSize: '14px', fontWeight: 600, marginBottom: '0.3rem', color: '#000716' };
  const formInputStyle = { width: '100%', padding: '0.5rem 0.75rem', background: '#fff', border: '2px solid #7d8998', borderRadius: '8px', fontSize: '14px', color: '#000716', fontFamily: 'inherit', outline: 'none' };
  const formSelectStyle = { ...formInputStyle, appearance: 'none', backgroundImage: "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%235f6b7a' viewBox='0 0 16 16'%3E%3Cpath d='M8 11L3 6h10z'/%3E%3C/svg%3E\")", backgroundRepeat: 'no-repeat', backgroundPosition: 'right 0.75rem center', paddingRight: '2rem' };
  const provLabelStyle = { display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '0.2rem', color: '#5f6b7a' };
  const provInputStyle = { width: '100%', padding: '0.4rem 0.6rem', background: '#fff', border: '2px solid #7d8998', borderRadius: '8px', fontSize: '13px', color: '#000716', fontFamily: 'inherit' };
  const provSelectStyle = { ...provInputStyle, appearance: 'none', backgroundImage: "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%235f6b7a' viewBox='0 0 16 16'%3E%3Cpath d='M8 11L3 6h10z'/%3E%3C/svg%3E\")", backgroundRepeat: 'no-repeat', backgroundPosition: 'right 0.6rem center', paddingRight: '1.8rem' };

  const getCatalogModels = (providerKey) => {
    if (!catalog || !catalog[providerKey]) return [];
    return catalog[providerKey].models || [];
  };

  return (
    <div className="container" style={{ marginBottom: '1.25rem' }}>
      <div className="container-header"><h2>{isEdit ? 'Edit model' : 'Add model'}</h2></div>
      <div className="container-body">
        {errors && (
          <Flash type="error" onDismiss={() => setErrors(null)}>
            {errors.map((err, i) => <div key={i}>{err.message || err.msg || JSON.stringify(err)}</div>)}
          </Flash>
        )}
        <form onSubmit={handleSubmit}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', marginBottom: '1rem' }}>
            <div>
              <label style={formLabelStyle}>Model name</label>
              <input style={formInputStyle} value={form.name} onChange={e => handleChange('name', e.target.value)} required placeholder="e.g. gpt-4" disabled={isEdit} />
            </div>
            <div>
              <label style={formLabelStyle}>Routing strategy</label>
              <select style={formSelectStyle} value={form.routing_strategy} onChange={e => handleChange('routing_strategy', e.target.value)}>
                {strategies.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
          </div>
          <div style={{ marginBottom: '1rem' }}>
            <label style={formLabelStyle}>Description</label>
            <input style={formInputStyle} value={form.description} onChange={e => handleChange('description', e.target.value)} placeholder="Model description" />
          </div>

          <div style={{ marginTop: '1.25rem', marginBottom: '1rem' }}>
            <h3 style={{ fontSize: '16px', fontWeight: 700, color: '#000716', marginBottom: '0.75rem' }}>Providers</h3>
            {form.providers.map((prov, i) => {
              const catalogModels = getCatalogModels(prov.provider);
              return (
                <div key={i} style={{ display: 'grid', gridTemplateColumns: '1.2fr 1.2fr 1.2fr 0.6fr 0.6fr auto', gap: '0.5rem', marginBottom: '0.5rem', alignItems: 'end' }}>
                  <div>
                    <label style={provLabelStyle}>Provider</label>
                    <select style={provSelectStyle} value={prov.provider} onChange={e => updateProvider(i, 'provider', e.target.value)}>
                      <option value="">Select provider...</option>
                      {catalogProviders.map(k => (
                        <option key={k} value={k}>{catalog[k].display_name || k}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label style={provLabelStyle}>Catalog model</label>
                    <select style={provSelectStyle} value={catalogModels.some(cm => cm.model_id === prov.model_id) ? prov.model_id : ''} onChange={e => selectCatalogModel(i, e.target.value)} disabled={!prov.provider || catalogModels.length === 0}>
                      <option value="">Select or type below...</option>
                      {catalogModels.map(cm => (
                        <option key={cm.model_id} value={cm.model_id}>
                          {cm.name} {cm.capabilities ? '[' + cm.capabilities.join(', ') + ']' : ''}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label style={provLabelStyle}>Model ID</label>
                    <input style={provInputStyle} value={prov.model_id} onChange={e => updateProvider(i, 'model_id', e.target.value)} placeholder="model-id" />
                  </div>
                  <div>
                    <label style={provLabelStyle}>Weight</label>
                    <input style={provInputStyle} type="number" step="0.1" min="0" value={prov.weight} onChange={e => updateProvider(i, 'weight', e.target.value)} />
                  </div>
                  <div>
                    <label style={provLabelStyle}>Fallback</label>
                    <input style={provInputStyle} type="number" min="0" value={prov.fallback_order} onChange={e => updateProvider(i, 'fallback_order', e.target.value)} />
                  </div>
                  <Btn variant="danger" style={{ marginBottom: '0.1rem', padding: '0.35rem 0.5rem', fontSize: '12px' }} onClick={() => removeProvider(i)} disabled={form.providers.length <= 1}>✕</Btn>
                </div>
              );
            })}
            <Btn variant="normal" style={{ fontSize: '13px', padding: '0.3rem 0.75rem' }} onClick={addProvider}>+ Add provider</Btn>
          </div>

          <div style={{ marginTop: '1.5rem', display: 'flex', gap: '0.5rem', borderTop: '1px solid #e9ebed', paddingTop: '1.25rem' }}>
            <Btn variant="primary" type="submit" disabled={saving}>{saving ? 'Saving...' : isEdit ? 'Save changes' : 'Add model'}</Btn>
            <Btn variant="normal" type="button" onClick={onCancel}>Cancel</Btn>
          </div>
        </form>
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Efficiency Page                                                     */
/* ================================================================== */
function EfficiencyPage({ onSelectUser }) {
  const [overview, setOverview] = useState(null);
  const [selectedUser, setSelectedUser] = useState(null);
  const [userReport, setUserReport] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [exportMessage, setExportMessage] = useState(null);
  const [exportError, setExportError] = useState(null);

  useEffect(() => {
    api.get('/admin/efficiency')
      .then(d => { setOverview(d); setLoading(false); })
      .catch(() => { setError('Failed to load efficiency data.'); setLoading(false); });
  }, []);

  const loadUserReport = (userId) => {
    setSelectedUser(userId);
    setUserReport(null);
    api.get(`/admin/users/${encodeURIComponent(userId)}/efficiency`)
      .then(setUserReport)
      .catch(() => setUserReport({ error: 'Failed to load user efficiency report.' }));
  };

  const exportUsage = async () => {
    setExporting(true);
    setExportError(null);
    setExportMessage('Starting export...');
    try {
      const filename = await downloadExport(
        '/admin/usage/export?format=csv&level=records',
        'axonllm-usage-records.csv',
        setExportMessage,
      );
      setExportMessage(`Downloaded ${filename}`);
    } catch (exportFailure) {
      setExportMessage(null);
      setExportError(
        exportFailure && exportFailure.message
          ? exportFailure.message
          : 'Usage export failed.',
      );
    } finally {
      setExporting(false);
    }
  };

  if (error) return <Flash type="error">{error}</Flash>;
  if (loading) return <Loading text="Analyzing token efficiency..." />;

  const gradeColor = (grade) => {
    const colors = { excellent: '#037f0c', good: '#0972d3', fair: '#8d6605', poor: '#d91515', wasteful: '#d91515' };
    return colors[grade] || '#5f6b7a';
  };
  const gradeBg = (grade) => {
    const colors = { excellent: '#f2fcf3', good: '#f2f8fd', fair: '#fffce9', poor: '#fff7f7', wasteful: '#fff7f7' };
    return colors[grade] || '#f2f3f3';
  };
  const scoreBar = (score) => {
    const color = score >= 85 ? '#037f0c' : score >= 70 ? '#0972d3' : score >= 50 ? '#8d6605' : '#d91515';
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <div style={{ flex: 1, height: '6px', background: '#e9ebed', borderRadius: '3px', overflow: 'hidden' }}>
          <div style={{ height: '100%', borderRadius: '3px', width: `${score}%`, background: color, transition: 'width 0.3s ease' }} />
        </div>
        <span style={{ fontSize: '13px', fontWeight: 600, color }}>{score}</span>
      </div>
    );
  };
  const severityBadge = (severity) => {
    const cls = severity === 'critical' ? 'badge-red' : severity === 'warning' ? 'badge-orange' : 'badge-blue';
    const bg = severity === 'critical' ? '#d91515' : severity === 'warning' ? '#8d6605' : '#0972d3';
    return <span className="badge" style={{ background: `${bg}15`, color: bg, border: `1px solid ${bg}40`, fontSize: '11px' }}>{severity}</span>;
  };

  if (selectedUser && userReport) {
    if (userReport.error) return <div><Breadcrumb items={[{ label: 'Efficiency', onClick: () => setSelectedUser(null) }, { label: selectedUser }]} /><Flash type="error">{userReport.error}</Flash></div>;
    const m = userReport.metrics;
    const hasSemanticProfile = userReport.semantic && userReport.semantic.profile;
    return (
      <div>
        <Breadcrumb items={[{ label: 'Efficiency', onClick: () => setSelectedUser(null) }, { label: selectedUser }]} />
        <div className="page-header">
          <h1>Efficiency Report: {selectedUser}</h1>
          <p>Token utilization analysis and optimization recommendations</p>
        </div>

        <div style={{ display: 'flex', gap: '0.75rem', marginBottom: '1.25rem', flexWrap: 'wrap' }}>
          <div style={{ flex: '1 1 200px', background: gradeBg(m.grade), border: `1px solid ${gradeColor(m.grade)}40`, borderRadius: '12px', padding: '1rem 1.25rem', textAlign: 'center' }}>
            <div style={{ fontSize: '11px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.5px', color: '#5f6b7a', marginBottom: '0.25rem' }}>Grade</div>
            <div style={{ fontSize: '28px', fontWeight: 700, color: gradeColor(m.grade), textTransform: 'uppercase' }}>{m.grade}</div>
            <div style={{ fontSize: '12px', color: '#5f6b7a', marginTop: '0.15rem' }}>Score: {m.score}/100</div>
          </div>
          <div className="stat-card" style={{ flex: '1 1 140px' }}><div className="stat-label">Avg Cost/Request</div><div className="stat-value">{fmt.cost(m.avg_cost_per_request)}</div></div>
          <div className="stat-card" style={{ flex: '1 1 140px' }}><div className="stat-label">Total Cost</div><div className="stat-value">{fmt.cost(m.total_cost)}</div></div>
          <div className="stat-card" style={{ flex: '1 1 140px' }}><div className="stat-label">Requests</div><div className="stat-value">{fmt.num(m.total_requests)}</div></div>
        </div>

        <div className="container" style={{ marginBottom: '1.25rem' }}>
          <div className="container-header"><h2>Efficiency Metrics</h2></div>
          <div className="container-body no-pad">
            <table>
              <thead><tr><th>Metric</th><th>Value</th><th>Status</th></tr></thead>
              <tbody>
                <tr><td>Completion/Prompt Ratio</td><td>{(m.completion_prompt_ratio * 100).toFixed(1)}%</td><td>{m.completion_prompt_ratio < 0.05 ? <span style={{color:'#d91515'}}>Low</span> : <span style={{color:'#037f0c'}}>OK</span>}</td></tr>
                <tr><td>Cache Utilization</td><td>{(m.cache_utilization_rate * 100).toFixed(1)}%</td><td>{m.cache_utilization_rate < 0.1 ? <span style={{color:'#8d6605'}}>Low</span> : <span style={{color:'#037f0c'}}>OK</span>}</td></tr>
                <tr><td>Expensive Model Usage</td><td>{(m.expensive_model_ratio * 100).toFixed(0)}%</td><td>{m.expensive_model_ratio > 0.8 ? <span style={{color:'#d91515'}}>High</span> : <span style={{color:'#037f0c'}}>OK</span>}</td></tr>
                <tr><td>Duplicate Request Rate</td><td>{(m.duplicate_request_rate * 100).toFixed(1)}%</td><td>{m.duplicate_request_rate > 0.15 ? <span style={{color:'#d91515'}}>High</span> : <span style={{color:'#037f0c'}}>OK</span>}</td></tr>
                <tr><td>Token Velocity</td><td>{fmt.num(Math.round(m.token_velocity_per_hour))} tok/hr</td><td>{m.token_velocity_per_hour > 50000 ? <span style={{color:'#d91515'}}>High</span> : <span style={{color:'#037f0c'}}>OK</span>}</td></tr>
                <tr><td>Avg Prompt Tokens</td><td>{fmt.num(Math.round(m.avg_prompt_tokens))}</td><td>{m.avg_prompt_tokens > 4000 ? <span style={{color:'#8d6605'}}>Large</span> : <span style={{color:'#037f0c'}}>OK</span>}</td></tr>
                <tr><td>Avg Completion Tokens</td><td>{fmt.num(Math.round(m.avg_completion_tokens))}</td><td>—</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        {userReport.alerts && userReport.alerts.length > 0 && (
          <div className="container" style={{ marginBottom: '1.25rem' }}>
            <div className="container-header"><h2>Alerts <span className="counter">({userReport.alerts.length})</span></h2></div>
            <div className="container-body" style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
              {userReport.alerts.map((a, i) => (
                <div key={i} style={{ padding: '0.75rem 1rem', borderRadius: '8px', background: a.severity === 'critical' ? '#fff7f7' : a.severity === 'warning' ? '#fffce9' : '#f2f8fd', border: `1px solid ${a.severity === 'critical' ? '#d9151530' : a.severity === 'warning' ? '#8d660530' : '#0972d330'}` }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.25rem' }}>
                    {severityBadge(a.severity)}
                    <strong style={{ fontSize: '13px' }}>{a.alert_type.replace(/_/g, ' ')}</strong>
                  </div>
                  <div style={{ fontSize: '13px', color: '#5f6b7a' }}>{a.message}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        {userReport.recommendations && userReport.recommendations.length > 0 && (
          <div className="container" style={{ marginBottom: '1.25rem' }}>
            <div className="container-header"><h2>Model Recommendations</h2></div>
            <div className="container-body no-pad">
              <table>
                <thead><tr><th>Current Model</th><th>Recommended</th><th>Est. Savings</th><th>Quality Impact</th><th>Reason</th></tr></thead>
                <tbody>
                  {userReport.recommendations.map((r, i) => (
                    <tr key={i}>
                      <td><strong>{r.current_model}</strong></td>
                      <td><span style={{ color: '#037f0c', fontWeight: 600 }}>{r.recommended_model}</span></td>
                      <td><span style={{ color: '#037f0c', fontWeight: 600 }}>{r.estimated_savings_pct}%</span></td>
                      <td>{r.quality_impact}</td>
                      <td style={{ fontSize: '12px', color: '#5f6b7a', maxWidth: '300px' }}>{r.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {userReport.peer_comparison && userReport.peer_comparison.peers_found > 0 && (
          <div className="container" style={{ marginBottom: '1.25rem' }}>
            <div className="container-header"><h2>Peer Comparison</h2></div>
            <div className="container-body">
              <div style={{ display: 'flex', gap: '1.5rem', flexWrap: 'wrap' }}>
                <div><span style={{ fontSize: '12px', color: '#5f6b7a' }}>Your Avg Cost/Req</span><div style={{ fontSize: '18px', fontWeight: 700 }}>{fmt.cost(userReport.peer_comparison.user_avg_cost_per_request)}</div></div>
                <div><span style={{ fontSize: '12px', color: '#5f6b7a' }}>Peer Avg Cost/Req</span><div style={{ fontSize: '18px', fontWeight: 700 }}>{fmt.cost(userReport.peer_comparison.peer_avg_cost_per_request)}</div></div>
                <div><span style={{ fontSize: '12px', color: '#5f6b7a' }}>vs Peers</span><div style={{ fontSize: '18px', fontWeight: 700, color: userReport.peer_comparison.vs_avg_pct > 0 ? '#d91515' : '#037f0c' }}>{userReport.peer_comparison.vs_avg_pct > 0 ? '+' : ''}{userReport.peer_comparison.vs_avg_pct}%</div></div>
                <div><span style={{ fontSize: '12px', color: '#5f6b7a' }}>Efficiency Percentile</span><div style={{ fontSize: '18px', fontWeight: 700 }}>{userReport.peer_comparison.percentile}th</div></div>
              </div>
            </div>
          </div>
        )}

        {hasSemanticProfile && (
          <div className="container" style={{ marginBottom: '1.25rem' }}>
            <div className="container-header"><h2>Usage Profile (Semantic Analysis)</h2></div>
            <div className="container-body">
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: '1rem' }}>
                <div><span style={{ fontSize: '12px', color: '#5f6b7a' }}>Avg Complexity</span><div style={{ fontSize: '16px', fontWeight: 700, textTransform: 'capitalize' }}>{userReport.semantic.profile.avg_complexity}</div></div>
                <div><span style={{ fontSize: '12px', color: '#5f6b7a' }}>Typical Model</span><div style={{ fontSize: '16px', fontWeight: 700 }}>{userReport.semantic.profile.typical_model}</div></div>
                <div><span style={{ fontSize: '12px', color: '#5f6b7a' }}>Optimal Model</span><div style={{ fontSize: '16px', fontWeight: 700, color: '#037f0c' }}>{userReport.semantic.profile.optimal_model}</div></div>
                <div><span style={{ fontSize: '12px', color: '#5f6b7a' }}>Est. Monthly Savings</span><div style={{ fontSize: '16px', fontWeight: 700, color: '#037f0c' }}>${userReport.semantic.profile.estimated_monthly_savings.toFixed(2)}</div></div>
              </div>
              {userReport.semantic.profile.patterns && userReport.semantic.profile.patterns.length > 0 && (
                <div style={{ marginTop: '1rem' }}>
                  <div style={{ fontSize: '12px', color: '#5f6b7a', marginBottom: '0.35rem' }}>Detected Patterns</div>
                  <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
                    {userReport.semantic.profile.patterns.map((p, i) => (
                      <span key={i} className="badge" style={{ background: '#fff7f7', color: '#d91515', border: '1px solid #d9151530', fontSize: '11px' }}>{p.replace(/_/g, ' ')}</span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {userReport.semantic && userReport.semantic.waste_summary && userReport.semantic.waste_summary.estimated_wasted_cost > 0 && (
          <div className="container">
            <div className="container-header"><h2>Waste Breakdown</h2></div>
            <div className="container-body">
              <div style={{ display: 'flex', gap: '1.5rem', flexWrap: 'wrap' }}>
                <div><span style={{ fontSize: '12px', color: '#5f6b7a' }}>Total Cost</span><div style={{ fontSize: '18px', fontWeight: 700 }}>{fmt.cost(userReport.semantic.waste_summary.total_cost)}</div></div>
                <div><span style={{ fontSize: '12px', color: '#5f6b7a' }}>Estimated Waste</span><div style={{ fontSize: '18px', fontWeight: 700, color: '#d91515' }}>{fmt.cost(userReport.semantic.waste_summary.estimated_wasted_cost)}</div></div>
                <div><span style={{ fontSize: '12px', color: '#5f6b7a' }}>Waste %</span><div style={{ fontSize: '18px', fontWeight: 700, color: '#d91515' }}>{userReport.semantic.waste_summary.waste_pct}%</div></div>
              </div>
            </div>
          </div>
        )}
      </div>
    );
  }

  if (selectedUser) return <Loading text={`Loading report for ${selectedUser}...`} />;

  return (
    <div>
      <div className="page-header page-header-actions">
        <div><h1>Token Efficiency</h1><p>Analyze token utilization, detect waste patterns, and optimize costs</p></div>
        <Btn onClick={exportUsage} disabled={exporting}>
          {exporting ? 'Preparing CSV...' : 'Export usage CSV'}
        </Btn>
      </div>
      {exportError && <Flash type="error" onDismiss={() => setExportError(null)}>{exportError}</Flash>}
      {exportMessage && <Flash type="info" onDismiss={() => setExportMessage(null)}>{exportMessage}</Flash>}

      <div className="stat-grid">
        <div className="stat-card"><div className="stat-label">Users Analyzed</div><div className="stat-value">{overview.total_users_analyzed}</div></div>
        <div className="stat-card"><div className="stat-label">Avg Efficiency Score</div><div className="stat-value">{overview.avg_efficiency_score}/100</div></div>
        <div className="stat-card"><div className="stat-label">Total Cost</div><div className="stat-value">{fmt.cost(overview.total_cost)}</div></div>
      </div>

      {overview.grade_distribution && (
        <div className="container" style={{ marginBottom: '1.25rem' }}>
          <div className="container-header"><h2>Grade Distribution</h2></div>
          <div className="container-body">
            <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap' }}>
              {['excellent', 'good', 'fair', 'poor', 'wasteful'].map(grade => {
                const count = overview.grade_distribution[grade] || 0;
                if (count === 0) return null;
                return (
                  <div key={grade} style={{ flex: '1 1 100px', textAlign: 'center', padding: '0.75rem', borderRadius: '8px', background: gradeBg(grade), border: `1px solid ${gradeColor(grade)}30` }}>
                    <div style={{ fontSize: '24px', fontWeight: 700, color: gradeColor(grade) }}>{count}</div>
                    <div style={{ fontSize: '12px', fontWeight: 600, textTransform: 'uppercase', color: gradeColor(grade) }}>{grade}</div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}

      {overview.wasteful_users && overview.wasteful_users.length > 0 && (
        <div className="container" style={{ marginBottom: '1.25rem' }}>
          <div className="container-header"><h2>Users Needing Attention <span className="counter">({overview.wasteful_users.length})</span></h2></div>
          <div className="container-body no-pad">
            <table>
              <thead><tr><th>User</th><th>Score</th><th>Grade</th><th>Cost</th><th></th></tr></thead>
              <tbody>
                {overview.wasteful_users.map(u => (
                  <tr key={u.user_id}>
                    <td><strong>{u.user_id}</strong></td>
                    <td>{scoreBar(u.score)}</td>
                    <td><span style={{ color: gradeColor(u.grade), fontWeight: 600, textTransform: 'uppercase', fontSize: '12px' }}>{u.grade}</span></td>
                    <td>{fmt.cost(u.cost)}</td>
                    <td><Btn variant="link" onClick={() => loadUserReport(u.user_id)}>View report</Btn></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="container">
        <div className="container-header"><h2>All Users <span className="counter">({overview.users.length})</span></h2></div>
        <div className="container-body no-pad">
          <table>
            <thead><tr><th>User</th><th>Score</th><th>Grade</th><th>C/P Ratio</th><th>Cache Util</th><th>Expensive %</th><th>Dup Rate</th><th>Requests</th><th>Cost</th><th></th></tr></thead>
            <tbody>
              {overview.users.map(u => (
                <tr key={u.user_id}>
                  <td><strong>{u.user_id}</strong></td>
                  <td style={{ minWidth: '100px' }}>{scoreBar(u.score)}</td>
                  <td><span style={{ color: gradeColor(u.grade), fontWeight: 600, textTransform: 'uppercase', fontSize: '12px' }}>{u.grade}</span></td>
                  <td>{(u.completion_prompt_ratio * 100).toFixed(1)}%</td>
                  <td>{(u.cache_utilization_rate * 100).toFixed(1)}%</td>
                  <td>{(u.expensive_model_ratio * 100).toFixed(0)}%</td>
                  <td>{(u.duplicate_request_rate * 100).toFixed(1)}%</td>
                  <td>{fmt.num(u.total_requests)}</td>
                  <td>{fmt.cost(u.total_cost)}</td>
                  <td><Btn variant="link" onClick={() => loadUserReport(u.user_id)}>Details</Btn></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Regions Page                                                        */
/* ================================================================== */
function RegionsPage() {
  const [topology, setTopology] = useState(null);
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);
  const [msg, setMsg] = useState(null);
  const [showAddForm, setShowAddForm] = useState(false);
  const [newSpoke, setNewSpoke] = useState({ region: '', role: 'active', weight: 50, data_residency_zones: '' });
  const [editingWeight, setEditingWeight] = useState({});

  const load = useCallback(() => {
    api.get('/admin/regions').then(setTopology).catch(e => setError('Failed to load topology: ' + (e && e.message ? e.message : 'unknown error')));
    api.get('/admin/regions/health').then(setHealth).catch(() => {});
  }, []);

  useEffect(() => { load(); }, [load]);

  const triggerHealthCheck = async () => {
    setMsg(null);
    try {
      const res = await api.post('/admin/regions/health/check');
      setMsg(`Checked ${res.checked} spoke(s)`);
      load();
    } catch { setError('Health check failed'); }
  };

  const triggerFailover = async () => {
    try {
      const res = await api.post('/admin/regions/failover');
      setMsg(`Failover to ${res.failover_to}`);
      load();
    } catch (e) { setError(e.error || 'Failover failed'); }
  };

  const setStatus = async (region, status) => {
    try {
      await api.put(`/admin/regions/${region}/status`, { status });
      setMsg(`${region} marked ${status}`);
      load();
    } catch { setError('Status update failed'); }
  };

  const addSpoke = async (e) => {
    e.preventDefault();
    try {
      const payload = { ...newSpoke, weight: parseInt(newSpoke.weight) || 50, data_residency_zones: newSpoke.data_residency_zones ? newSpoke.data_residency_zones.split(',').map(z => z.trim()) : [] };
      await api.post('/admin/regions/spokes', payload);
      setMsg(`Added spoke: ${newSpoke.region}`);
      setShowAddForm(false);
      setNewSpoke({ region: '', role: 'active', weight: 50, data_residency_zones: '' });
      load();
    } catch (err) { setError(err.error || 'Failed to add spoke'); }
  };

  const removeSpoke = async (region) => {
    if (!confirm(`Remove spoke ${region}?`)) return;
    try {
      await api.del(`/admin/regions/spokes/${region}`);
      setMsg(`Removed spoke: ${region}`);
      load();
    } catch (err) { setError(err.error || 'Failed to remove spoke'); }
  };

  const updateWeight = async (region, weight) => {
    try {
      await api.put(`/admin/regions/spokes/${region}`, { weight: parseInt(weight) });
      setEditingWeight({});
      load();
    } catch { setError('Failed to update weight'); }
  };

  const updateRole = async (region, role) => {
    try {
      await api.put(`/admin/regions/spokes/${region}`, { role });
      setMsg(`${region} role changed to ${role}`);
      load();
    } catch { setError('Failed to update role'); }
  };

  const toggleStrictResidency = async () => {
    try {
      await api.put('/admin/regions/config', { data_residency_strict: !topology.data_residency_strict });
      load();
    } catch { setError('Failed to update config'); }
  };

  if (error) return <Flash type="error">{error}<Btn variant="link" onClick={() => setError(null)}>dismiss</Btn></Flash>;
  if (!topology) return <Loading text="Loading regions..." />;

  const modeColors = { single_region: 'badge-blue', active_passive: 'badge-green', active_active: 'badge-blue' };

  return (
    <div>
      <div className="page-header"><h1>Multi-Region Topology</h1><p>Hub-and-spoke routing, failover, and data residency — fully configurable</p></div>
      {msg && <Flash type="success">{msg}<Btn variant="link" onClick={() => setMsg(null)}>dismiss</Btn></Flash>}
      <div className="stat-grid">
        <div className="stat-card"><div className="stat-label">Mode</div><div className="stat-value"><span className={`badge ${modeColors[topology.mode] || 'badge-blue'}`}>{topology.mode.replace(/_/g, '-')}</span></div></div>
        <div className="stat-card"><div className="stat-label">Hub Region</div><div className="stat-value">{topology.hub_region}</div></div>
        <div className="stat-card"><div className="stat-label">Spokes</div><div className="stat-value">{topology.healthy_spokes}/{topology.total_spokes} healthy</div></div>
      </div>
      <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '1rem', flexWrap: 'wrap' }}>
        <Btn variant="primary" onClick={() => setShowAddForm(!showAddForm)}>{showAddForm ? 'Cancel' : '+ Add Spoke'}</Btn>
        <Btn variant="normal" onClick={triggerHealthCheck}>Run Health Check</Btn>
        <Btn variant="danger" onClick={triggerFailover}>Trigger Failover</Btn>
      </div>
      {showAddForm && (
        <div className="container" style={{ marginBottom: '1rem' }}>
          <div className="container-header"><h2>Add New Spoke</h2></div>
          <div className="container-body">
            <form onSubmit={addSpoke} style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: '0.75rem', alignItems: 'end' }}>
              <label style={{ fontSize: '12px' }}>Region<input className="input" value={newSpoke.region} onChange={e => setNewSpoke({...newSpoke, region: e.target.value})} placeholder="us-west-2" required /></label>
              <label style={{ fontSize: '12px' }}>Role<select className="input" value={newSpoke.role} onChange={e => setNewSpoke({...newSpoke, role: e.target.value})}><option value="primary">Primary</option><option value="failover">Failover</option><option value="active">Active</option></select></label>
              <label style={{ fontSize: '12px' }}>Weight<input className="input" type="number" min="0" max="100" value={newSpoke.weight} onChange={e => setNewSpoke({...newSpoke, weight: e.target.value})} /></label>
              <label style={{ fontSize: '12px' }}>Data Zones (comma-sep)<input className="input" value={newSpoke.data_residency_zones} onChange={e => setNewSpoke({...newSpoke, data_residency_zones: e.target.value})} placeholder="us, eu" /></label>
              <Btn type="submit" variant="primary">Add Spoke</Btn>
            </form>
          </div>
        </div>
      )}
      <div className="container">
        <div className="container-header"><h2>Spokes <span className="counter">({topology.spokes.length})</span></h2></div>
        <div className="container-body no-pad">
          <table>
            <thead><tr><th>Region</th><th>Role</th><th>Status</th><th>Weight</th><th>Data Zones</th><th>Actions</th></tr></thead>
            <tbody>
              {topology.spokes.map(s => (
                <tr key={s.region}>
                  <td><strong>{s.region}</strong></td>
                  <td>
                    <select value={s.role} onChange={e => updateRole(s.region, e.target.value)} style={{ border: 'none', background: 'transparent', fontWeight: 600, color: s.role === 'primary' ? '#147EBA' : s.role === 'failover' ? '#666' : '#248814', cursor: 'pointer' }}>
                      <option value="primary">primary</option>
                      <option value="failover">failover</option>
                      <option value="active">active</option>
                    </select>
                  </td>
                  <td><span className={`badge ${s.status === 'healthy' ? 'badge-green' : s.status === 'draining' ? 'badge-grey' : 'badge-red'}`}><span className="badge-dot"></span>{s.status}</span></td>
                  <td>
                    {editingWeight[s.region] !== undefined
                      ? <input type="number" min="0" max="100" value={editingWeight[s.region]} onChange={e => setEditingWeight({...editingWeight, [s.region]: e.target.value})} onBlur={() => updateWeight(s.region, editingWeight[s.region])} onKeyDown={e => e.key === 'Enter' && updateWeight(s.region, editingWeight[s.region])} autoFocus style={{ width: '50px' }} />
                      : <span onClick={() => setEditingWeight({...editingWeight, [s.region]: s.weight})} style={{ cursor: 'pointer', borderBottom: '1px dashed #999' }}>{s.weight}%</span>
                    }
                  </td>
                  <td>{s.data_residency_zones.length > 0 ? s.data_residency_zones.join(', ') : '-'}</td>
                  <td style={{ display: 'flex', gap: '0.3rem', flexWrap: 'wrap' }}>
                    {s.status !== 'draining' && <Btn variant="link" onClick={() => setStatus(s.region, 'draining')}>Drain</Btn>}
                    {s.status === 'unhealthy' && <Btn variant="link" onClick={() => setStatus(s.region, 'healthy')}>Recover</Btn>}
                    {s.status === 'healthy' && <Btn variant="link" onClick={() => setStatus(s.region, 'unhealthy')}>Force Down</Btn>}
                    <Btn variant="link" onClick={() => removeSpoke(s.region)} style={{ color: '#d13212' }}>Remove</Btn>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

/* ================================================================== */
/*  API Keys Page                                                       */
/* ================================================================== */
/* initialProjectId lets a caller land on this page with a project already
   selected. The guided tour uses it: this page lists nothing until a project
   is named, so a scene about API keys would otherwise narrate an empty form. */
function ApiKeysPage({ initialProjectId }) {
  const [keys, setKeys] = useState([]);
  const [projectId, setProjectId] = useState(initialProjectId || '');
  const [showForm, setShowForm] = useState(false);
  const [newKey, setNewKey] = useState(null);
  const [error, setError] = useState(null);
  const [form, setForm] = useState({ name: '', scopes: 'chat:invoke', project_id: '' });

  /* A direct sidebar visit has no project in its navigation state. Select the
     first visible project so the seeded demo shows its keys immediately; the
     input remains editable for operators who want a different project. */
  useEffect(() => {
    if (initialProjectId) return;
    api.get('/admin/projects')
      .then(projects => {
        if (projects && projects.length) {
          setProjectId(current => current || projects[0].project_id);
        }
      })
      .catch(e => setError('Failed to load projects: ' + (e && e.message ? e.message : 'unknown error')));
  }, [initialProjectId]);

  const loadKeys = useCallback(() => {
    if (!projectId) return;
    api.get(`/admin/projects/${encodeURIComponent(projectId)}/keys`).then(setKeys).catch(e => setError('Failed to load keys: ' + (e && e.message ? e.message : 'unknown error')));
  }, [projectId]);

  useEffect(() => { loadKeys(); }, [loadKeys]);

  const issueKey = async () => {
    setError(null); setNewKey(null);
    try {
      const res = await api.post(`/admin/projects/${encodeURIComponent(form.project_id || projectId)}/keys`, {
        name: form.name,
        scopes: form.scopes.split(',').map(s => s.trim()),
      });
      setNewKey(res);
      setShowForm(false);
      loadKeys();
    } catch (e) { setError(e.error || 'Failed to issue key'); }
  };

  const revokeKey = async (keyId) => {
    await api.del(`/admin/keys/${keyId}`);
    loadKeys();
  };

  const rotateKey = async (keyId) => {
    const res = await api.post(`/admin/keys/${keyId}/rotate`);
    setNewKey(res);
    loadKeys();
  };

  return (
    <div>
      <div className="page-header"><h1>API Keys</h1><p>Issue, revoke, and rotate project-scoped API keys</p></div>
      {error && <Flash type="error">{error}</Flash>}
      {newKey && (
        <Flash type="success">
          <strong>New key issued:</strong> <code style={{ fontSize: '12px', wordBreak: 'break-all' }}>{newKey.key}</code>
          <br /><small>Copy this now — it will not be shown again.</small>
        </Flash>
      )}
      <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '1rem', alignItems: 'center' }}>
        <input style={{ padding: '0.4rem 0.75rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '8px', fontSize: '14px', width: '250px' }} placeholder="Project ID to view keys..." value={projectId} onChange={e => setProjectId(e.target.value)} />
        <Btn variant="primary" onClick={() => setShowForm(true)}>Issue New Key</Btn>
      </div>
      {showForm && (
        <div className="container" style={{ marginBottom: '1rem' }}>
          <div className="container-header"><h2>Issue API Key</h2></div>
          <div className="container-body">
            <div style={{ marginBottom: '0.5rem' }}><label style={{ fontWeight: 600, fontSize: '13px' }}>Project ID</label><input style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.project_id || projectId} onChange={e => setForm({ ...form, project_id: e.target.value })} /></div>
            <div style={{ marginBottom: '0.5rem' }}><label style={{ fontWeight: 600, fontSize: '13px' }}>Key Name</label><input style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="e.g. CI/CD Pipeline" /></div>
            <div style={{ marginBottom: '0.5rem' }}><label style={{ fontWeight: 600, fontSize: '13px' }}>Scopes (comma-separated)</label><input style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.scopes} onChange={e => setForm({ ...form, scopes: e.target.value })} /></div>
            <div style={{ display: 'flex', gap: '0.5rem' }}><Btn variant="primary" onClick={issueKey} disabled={!form.name}>Issue</Btn><Btn variant="normal" onClick={() => setShowForm(false)}>Cancel</Btn></div>
          </div>
        </div>
      )}
      {keys.length > 0 && (
        <div className="container">
          <div className="container-header"><h2>Keys for {projectId} <span className="counter">({keys.length})</span></h2></div>
          <div className="container-body no-pad">
            <table>
              <thead><tr><th>Key ID</th><th>Name</th><th>Scopes</th><th>Created</th><th>Status</th><th>Last Used</th><th>Actions</th></tr></thead>
              <tbody>
                {keys.map(k => (
                  <tr key={k.key_id}>
                    <td><code style={{ fontSize: '12px' }}>{k.key_id}</code></td>
                    <td><strong>{k.name}</strong></td>
                    <td>{k.scopes.join(', ')}</td>
                    <td>{new Date(k.created_at).toLocaleDateString()}</td>
                    <td><span className={`badge ${k.revoked ? 'badge-red' : 'badge-green'}`}><span className="badge-dot"></span>{k.revoked ? 'Revoked' : 'Active'}</span></td>
                    <td>{k.last_used_at ? new Date(k.last_used_at).toLocaleString() : '-'}</td>
                    <td style={{ display: 'flex', gap: '0.3rem' }}>
                      {!k.revoked && <Btn variant="link" onClick={() => rotateKey(k.key_id)}>Rotate</Btn>}
                      {!k.revoked && <Btn variant="link" onClick={() => revokeKey(k.key_id)}>Revoke</Btn>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

/* ================================================================== */
/*  PII Redaction Preview                                              */
/* ================================================================== */
/* The audit trail can show THAT redaction happened and how many items it
   replaced, but never what the provider actually received — storing that
   would mean storing the PII the feature exists to keep out of storage. So
   this recomputes on demand against the real engine.

   Two columns because the difference is the point: the regex column leaves
   a name untouched (PII_PATTERNS has no name pattern — a name has no shape
   to match), the NER column catches it. The default example is chosen to
   show exactly that. */
const PII_SAMPLE = "Hi, I'm Alice Smith from Seattle. My email is alice.smith@example.com, "
  + "SSN 123-45-6789, and you can reach me at 555-234-5678. "
  + "Card on file 4111 1111 1111 1111, deployed from 10.0.0.7.";

function PiiDiff({ label, sub, text, count, tone }) {
  /* Tokens highlighted so the substitution is visible at a glance rather
     than something the reader has to diff by eye. */
  const parts = String(text || '').split(/(\[[A-Z_]+_\d+\])/g);
  return (
    <div style={{ flex: '1 1 320px', minWidth: '280px' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: '0.5rem', marginBottom: '0.35rem' }}>
        <strong style={{ fontSize: '13px' }}>{label}</strong>
        {typeof count === 'number' && (
          <span className={`badge ${tone || 'badge-grey'}`}>{count} redacted</span>
        )}
      </div>
      {sub && <div style={{ fontSize: '11px', color: 'var(--awsui-color-text-secondary, #666)', marginBottom: '0.35rem' }}>{sub}</div>}
      <pre style={{
        margin: 0, padding: '0.6rem', borderRadius: '4px', fontSize: '12px',
        whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.5,
        background: '#fafaf9', border: '1px solid #e7e5e4', minHeight: '5.5rem',
      }}>
        {parts.map((p, i) => /^\[[A-Z_]+_\d+\]$/.test(p)
          ? <mark key={i} style={{ background: '#fde68a', fontWeight: 600, borderRadius: '2px', padding: '0 2px' }}>{p}</mark>
          : <span key={i}>{p}</span>)}
      </pre>
    </div>
  );
}

function PiiPreviewPanel() {
  const [text, setText] = useState(PII_SAMPLE);
  const [useNer, setUseNer] = useState(false);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const run = useCallback((withNer) => {
    setBusy(true); setErr(null);
    api.post('/admin/pii/preview', { text, ner: !!withNer })
      .then(r => { setResult(r); setBusy(false); })
      .catch(e => { setErr(e && e.message ? e.message : 'Preview failed'); setBusy(false); });
  }, [text]);

  /* Run once on mount so the panel shows the point immediately rather than
     an empty box the visitor has to interact with to understand. NER is off
     on that first call — it bills per request, so it stays explicit. */
  useEffect(() => { run(false); }, []);

  const ner = result && result.ner;

  return (
    <div className="container" style={{ marginBottom: '1rem' }}>
      <div className="container-header">
        <h2>PII Redaction Preview</h2>
      </div>
      <div className="container-body">
        <div style={{ fontSize: '12px', color: 'var(--awsui-color-text-secondary, #666)', marginBottom: '0.75rem' }}>
          What the provider actually receives. Nothing here is stored — the audit
          trail records that redaction happened and how many items it replaced,
          never the values themselves.
        </div>
        <textarea
          value={text}
          onChange={e => setText(e.target.value)}
          rows={4}
          maxLength={4000}
          style={{ width: '100%', fontSize: '12px', fontFamily: 'monospace', padding: '0.5rem', boxSizing: 'border-box' }}
        />
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', margin: '0.6rem 0' }}>
          <button className="btn btn-primary" disabled={busy || !text.trim()} onClick={() => run(useNer)}>
            {busy ? 'Redacting…' : 'Redact'}
          </button>
          <label style={{ fontSize: '12px', display: 'flex', alignItems: 'center', gap: '0.35rem' }}>
            <input type="checkbox" checked={useNer} onChange={e => { setUseNer(e.target.checked); run(e.target.checked); }} />
            Add entity detection (names, addresses) — billed per request
          </label>
          <button className="btn" style={{ fontSize: '11px' }} onClick={() => setText(PII_SAMPLE)}>Reset example</button>
        </div>
        {err && <Flash type="error">{err}</Flash>}
        {result && (
          <div>
            <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap' }}>
              <PiiDiff
                label="Pattern matching"
                sub="Regex only — structured values with a fixed shape"
                text={result.redacted}
                count={result.redacted_count}
                tone="badge-blue"
              />
              {useNer && (
                ner && ner.available ? (
                  <PiiDiff
                    label="Pattern matching + entity detection"
                    sub={`Adds ${(result.supported_ner_types || []).join(', ')} — values with no fixed shape`}
                    text={ner.redacted}
                    count={ner.redacted_count}
                    tone="badge-green"
                  />
                ) : (
                  <div style={{ flex: '1 1 320px', minWidth: '280px' }}>
                    <strong style={{ fontSize: '13px' }}>Pattern matching + entity detection</strong>
                    <div style={{ marginTop: '0.35rem' }}>
                      <Flash type="warning">
                        Entity detection unavailable{ner && ner.reason ? `: ${ner.reason}` : ''}.
                        Pattern matching is unaffected — the request path degrades the same way.
                      </Flash>
                    </div>
                  </div>
                )
              )}
            </div>
            {/* The finding, stated rather than left to be inferred. */}
            {useNer && ner && ner.available && ner.additional_count > 0 && (
              <div style={{ marginTop: '0.75rem' }}>
                <Flash type="info">
                  Entity detection caught {ner.additional_count} additional item{ner.additional_count === 1 ? '' : 's'}
                  {ner.types_found && ner.types_found.length > 0 ? ` (${ner.types_found.filter(t => (result.types_found || []).indexOf(t) === -1).join(', ')})` : ''} that
                  pattern matching cannot express. A name or a city has no fixed shape to match,
                  which is why the left column leaves them in place. The tradeoff: entity
                  detection also redacts public figures and place names in ordinary questions,
                  so it is off unless a policy enables it.
                </Flash>
              </div>
            )}
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem', marginTop: '0.75rem', alignItems: 'center' }}>
              {(result.types_found || []).map(t => (
                <span key={t} className="badge badge-blue">{t}</span>
              ))}
              {useNer && ner && ner.available && (ner.types_found || [])
                .filter(t => (result.types_found || []).indexOf(t) === -1)
                .map(t => <span key={t} className="badge badge-green">{t}</span>)}
              {/* Round-trip status is reported, not asserted: a pattern whose
                  match cannot be restored should show up here rather than
                  silently mangle the caller's answer. */}
              <span className={`badge ${result.round_trip_exact ? 'badge-green' : 'badge-red'}`}>
                <span className="badge-dot"></span>
                {result.round_trip_exact ? 'Re-injection lossless' : 'Re-injection mismatch'}
              </span>
            </div>
            <div style={{ fontSize: '11px', color: 'var(--awsui-color-text-secondary, #666)', marginTop: '0.6rem' }}>
              The caller gets the original values back on the way out; the provider only
              ever saw the tokens.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Security / Audit Page                                              */
/* ================================================================== */
function SecurityPage() {
  const [tab, setTab] = useState('events');
  const [records, setRecords] = useState([]);
  const [stats, setStats] = useState(null);
  const [integrity, setIntegrity] = useState(null);
  const [error, setError] = useState(null);
  const [exporting, setExporting] = useState(false);
  const [exportMessage, setExportMessage] = useState(null);
  const [exportError, setExportError] = useState(null);

  const loadEvents = useCallback(() => {
    api.get('/admin/audit/security?limit=50').then(r => setRecords(r.records)).catch(e => setError('Failed to load events: ' + (e && e.message ? e.message : 'unknown error')));
  }, []);
  const loadStats = useCallback(() => {
    api.get('/admin/audit/stats').then(setStats).catch(() => {});
  }, []);
  const loadIntegrity = useCallback(() => {
    api.get('/admin/audit/verify').then(setIntegrity).catch(() => {});
  }, []);

  useEffect(() => { loadEvents(); loadStats(); loadIntegrity(); }, [loadEvents, loadStats, loadIntegrity]);

  const exportAudit = async () => {
    setExporting(true);
    setExportError(null);
    setExportMessage('Starting export...');
    try {
      const filename = await downloadExport(
        '/admin/audit/export',
        'axonllm-audit-records.json',
        setExportMessage,
      );
      setExportMessage(`Downloaded ${filename}`);
    } catch (exportFailure) {
      setExportMessage(null);
      setExportError(
        exportFailure && exportFailure.message
          ? exportFailure.message
          : 'Audit export failed.',
      );
    } finally {
      setExporting(false);
    }
  };

  const severityColor = (type) => {
    if (type.includes('blocked')) return 'badge-red';
    if (type.includes('detected') || type.includes('failure')) return 'badge-grey';
    return 'badge-blue';
  };

  return (
    <div>
      <div className="page-header page-header-actions">
        <div><h1>Security & Audit</h1><p>Injection attempts, PII redaction events, audit trail integrity</p></div>
        <Btn onClick={exportAudit} disabled={exporting}>
          {exporting ? 'Preparing JSON...' : 'Export audit JSON'}
        </Btn>
      </div>
      {error && <Flash type="error">{error}</Flash>}
      {exportError && <Flash type="error" onDismiss={() => setExportError(null)}>{exportError}</Flash>}
      {exportMessage && <Flash type="info" onDismiss={() => setExportMessage(null)}>{exportMessage}</Flash>}
      <div className="stat-grid">
        <div className="stat-card"><div className="stat-label">Total Events</div><div className="stat-value">{stats ? fmt.num(stats.total) : '-'}</div></div>
        <div className="stat-card"><div className="stat-label">Chain Integrity</div><div className="stat-value">{integrity ? <span className={`badge ${integrity.chain_valid ? 'badge-green' : 'badge-red'}`}><span className="badge-dot"></span>{integrity.status}</span> : '-'}</div></div>
        <div className="stat-card"><div className="stat-label">Security Events</div><div className="stat-value">{records.length}</div></div>
      </div>
      <PiiPreviewPanel />
      {stats && stats.by_type && Object.keys(stats.by_type).length > 0 && (
        <div className="container" style={{ marginBottom: '1rem' }}>
          <div className="container-header"><h2>Event Breakdown</h2></div>
          <div className="container-body">
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem' }}>
              {Object.entries(stats.by_type).map(([type, count]) => (
                <span key={type} className="badge badge-blue" style={{ padding: '0.3rem 0.6rem' }}>{type}: {count}</span>
              ))}
            </div>
          </div>
        </div>
      )}
      <div className="container">
        <div className="container-header"><h2>Recent Security Events <span className="counter">({records.length})</span></h2></div>
        <div className="container-body no-pad">
          {records.length === 0 ? (
            <EmptyState title="No security events" subtitle="No injection attempts or PII redactions recorded yet." />
          ) : (
            <table>
              <thead><tr><th>Time</th><th>Type</th><th>User</th><th>Project</th><th>Details</th></tr></thead>
              <tbody>
                {records.map(r => (
                  <tr key={r.record_id}>
                    <td style={{ fontSize: '12px', whiteSpace: 'nowrap' }}>{new Date(r.timestamp).toLocaleString()}</td>
                    <td><span className={`badge ${severityColor(r.event_type)}`}>{r.event_type}</span></td>
                    <td>{r.user_id}</td>
                    <td>{r.project_id}</td>
                    <td style={{ fontSize: '12px', maxWidth: '300px', overflow: 'hidden', textOverflow: 'ellipsis' }}>{JSON.stringify(r.data)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Webhooks Page                                                       */
/* ================================================================== */
function WebhooksPage() {
  const [destinations, setDestinations] = useState([]);
  const [stats, setStats] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState(null);
  const [msg, setMsg] = useState(null);
  const [form, setForm] = useState({ name: '', type: 'webhook', url: '', event_filter: '' });

  const load = useCallback(() => {
    api.get('/admin/webhooks').then(d => { setDestinations(d.destinations); setStats(d.stats); }).catch(e => setError('Failed to load: ' + (e && e.message ? e.message : 'unknown error')));
  }, []);

  useEffect(() => { load(); }, [load]);

  const addDestination = async () => {
    setError(null); setMsg(null);
    const config = { url: form.url };
    const eventFilter = form.event_filter ? form.event_filter.split(',').map(s => s.trim()) : null;
    try {
      await api.post('/admin/webhooks', { name: form.name, type: form.type, config, event_filter: eventFilter });
      setMsg(`Destination "${form.name}" added`);
      setShowForm(false);
      setForm({ name: '', type: 'webhook', url: '', event_filter: '' });
      load();
    } catch (e) { setError(e.error || 'Failed to add'); }
  };

  const removeDestination = async (name) => {
    await api.del(`/admin/webhooks/${encodeURIComponent(name)}`);
    load();
  };

  const testDestination = async (name) => {
    try {
      const res = await api.post(`/admin/webhooks/${encodeURIComponent(name)}/test`);
      setMsg(`Test event sent to "${name}": ${res.status}`);
    } catch (e) { setError(e.error || `Test failed for "${name}"`); }
  };

  return (
    <div>
      <div className="page-header"><h1>Webhooks & Events</h1><p>Push security events to external systems (Slack, SNS, CloudWatch, SIEM)</p></div>
      {error && <Flash type="error">{error}</Flash>}
      {msg && <Flash type="success">{msg}</Flash>}
      <div className="stat-grid">
        <div className="stat-card"><div className="stat-label">Destinations</div><div className="stat-value">{stats ? stats.destinations : 0}</div></div>
        <div className="stat-card"><div className="stat-label">Events Dispatched</div><div className="stat-value">{stats ? fmt.num(stats.dispatched) : 0}</div></div>
        <div className="stat-card"><div className="stat-label">Errors</div><div className="stat-value" style={{ color: stats && stats.errors > 0 ? 'var(--awsui-color-status-error)' : undefined }}>{stats ? stats.errors : 0}</div></div>
      </div>
      <div style={{ marginBottom: '1rem' }}><Btn variant="primary" onClick={() => setShowForm(true)}>Add Destination</Btn></div>
      {showForm && (
        <div className="container" style={{ marginBottom: '1rem' }}>
          <div className="container-header"><h2>New Destination</h2></div>
          <div className="container-body">
            <div style={{ marginBottom: '0.5rem' }}><label style={{ fontWeight: 600, fontSize: '13px' }}>Name</label><input style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="e.g. slack-security" /></div>
            <div style={{ marginBottom: '0.5rem' }}><label style={{ fontWeight: 600, fontSize: '13px' }}>Type</label><select style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.type} onChange={e => setForm({ ...form, type: e.target.value })}><option value="webhook">Webhook</option><option value="sns">SNS</option><option value="cloudwatch">CloudWatch</option></select></div>
            <div style={{ marginBottom: '0.5rem' }}><label style={{ fontWeight: 600, fontSize: '13px' }}>URL / Topic ARN</label><input style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.url} onChange={e => setForm({ ...form, url: e.target.value })} placeholder="https://hooks.slack.com/..." /></div>
            <div style={{ marginBottom: '0.5rem' }}><label style={{ fontWeight: 600, fontSize: '13px' }}>Event Filter (comma-separated, empty = all)</label><input style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.event_filter} onChange={e => setForm({ ...form, event_filter: e.target.value })} placeholder="injection_blocked, auth_failure" /></div>
            <div style={{ display: 'flex', gap: '0.5rem' }}><Btn variant="primary" onClick={addDestination} disabled={!form.name}>Add</Btn><Btn variant="normal" onClick={() => setShowForm(false)}>Cancel</Btn></div>
          </div>
        </div>
      )}
      <div className="container">
        <div className="container-header"><h2>Destinations <span className="counter">({destinations.length})</span></h2></div>
        <div className="container-body no-pad">
          {destinations.length === 0 ? (
            <EmptyState title="No destinations" subtitle="Add a webhook or SNS topic to receive security events." />
          ) : (
            <table>
              <thead><tr><th>Name</th><th>Type</th><th>Status</th><th>Event Filter</th><th>Actions</th></tr></thead>
              <tbody>
                {destinations.map(d => (
                  <tr key={d.name}>
                    <td><strong>{d.name}</strong></td>
                    <td><span className="badge badge-blue">{d.type}</span></td>
                    <td><span className={`badge ${d.enabled ? 'badge-green' : 'badge-grey'}`}><span className="badge-dot"></span>{d.enabled ? 'Active' : 'Disabled'}</span></td>
                    <td>{d.event_filter ? d.event_filter.join(', ') : 'All events'}</td>
                    <td style={{ display: 'flex', gap: '0.3rem' }}>
                      <Btn variant="link" onClick={() => testDestination(d.name)}>Test</Btn>
                      <Btn variant="link" onClick={() => removeDestination(d.name)}>Remove</Btn>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Policy Hierarchy Page (enhanced)                                    */
/* ================================================================== */
function PolicyHierarchyPage() {
  const [nodes, setNodes] = useState([]);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState(null);
  const [msg, setMsg] = useState(null);
  const [form, setForm] = useState({ node_id: '', node_type: 'org', parent_id: '', display_name: '', rate_limit_rpm: '', budget_limit: '', max_tokens_per_request: '', pii_redaction_enabled: false, pii_redact_types: '' });

  const load = useCallback(() => {
    api.get('/admin/policies/hierarchy').then(setNodes).catch(e => setError('Failed to load: ' + (e && e.message ? e.message : 'unknown error')));
  }, []);

  useEffect(() => { load(); }, [load]);

  const createNode = async () => {
    setError(null); setMsg(null);
    const limits = {};
    if (form.rate_limit_rpm) limits.rate_limit_rpm = parseInt(form.rate_limit_rpm);
    if (form.budget_limit) limits.budget_limit = parseFloat(form.budget_limit);
    if (form.max_tokens_per_request) limits.max_tokens_per_request = parseInt(form.max_tokens_per_request);
    if (form.pii_redaction_enabled) limits.pii_redaction_enabled = true;
    if (form.pii_redact_types) limits.pii_redact_types = form.pii_redact_types.split(',').map(s => s.trim());
    try {
      await api.post('/admin/policies/hierarchy', {
        node_id: form.node_id,
        node_type: form.node_type,
        parent_id: form.parent_id || null,
        display_name: form.display_name || form.node_id,
        limits,
      });
      setMsg(`Node "${form.node_id}" created`);
      setShowForm(false);
      load();
    } catch (e) { setError(e.error || 'Failed to create node'); }
  };

  const nodeTypeColor = (t) => {
    switch (t) { case 'org': return 'badge-blue'; case 'business_unit': return 'badge-green'; case 'project': return 'badge-grey'; default: return 'badge-grey'; }
  };

  /* Order the flat /admin/policies/hierarchy list into parent-before-child
     and record each node's depth, so the table can indent.

     The point of this page is that a child's limits are narrowed by its
     parent's. A flat list with a parent_id column leaves the reader to
     reconstruct that from ids; depth-first order with indentation shows it.

     Roots are anything whose parent is absent, not just parent_id === null:
     a project seeded without a hierarchy has no parent, and so does one
     whose parent_id points at a node that was deleted. Both must still
     appear — a node silently missing from this table reads as a limit that
     does not exist, which is the opposite of the truth. */
  const flatten = (all) => {
    const known = {};
    all.forEach(n => { known[n.node_id] = true; });
    // Roots keyed under null rather than a sentinel id, so no reserved
    // string can ever collide with a real node_id.
    const byParent = new Map();
    all.forEach(n => {
      const key = (n.parent_id && known[n.parent_id]) ? n.parent_id : null;
      if (!byParent.has(key)) byParent.set(key, []);
      byParent.get(key).push(n);
    });
    const out = [];
    // Guards against a parent_id cycle. The resolver tolerates one (its own
    // walk keeps a visited set) so the API will happily serve it, and
    // unguarded recursion here would hang the page instead of showing it.
    const seen = {};
    const walk = (key, depth) => {
      const kids = byParent.get(key) || [];
      // Sorted by id so the order is stable across reloads; the API returns
      // dict insertion order, which shifts as nodes are added.
      kids.slice().sort((a, b) => a.node_id.localeCompare(b.node_id)).forEach(n => {
        if (seen[n.node_id]) return;
        seen[n.node_id] = true;
        out.push({ node: n, depth: depth });
        walk(n.node_id, depth + 1);
      });
    };
    walk(null, 0);
    // Anything a cycle kept out of the walk still gets a row, unindented.
    all.forEach(n => { if (!seen[n.node_id]) out.push({ node: n, depth: 0 }); });
    return out;
  };

  const rows = flatten(nodes);

  return (
    <div>
      <div className="page-header"><h1>Policy Hierarchy</h1><p>Org &gt; Business Unit &gt; Project &gt; Environment. Each row inherits from the row it is indented under, and the most restrictive value wins — a child can never raise a limit its parent set. An em dash means the node sets no value of its own and takes its parent's.</p></div>
      {error && <Flash type="error">{error}</Flash>}
      {msg && <Flash type="success">{msg}</Flash>}
      <div style={{ marginBottom: '1rem' }}><Btn variant="primary" onClick={() => setShowForm(true)}>Add Node</Btn></div>
      {showForm && (
        <div className="container" style={{ marginBottom: '1rem' }}>
          <div className="container-header"><h2>New Policy Node</h2></div>
          <div className="container-body">
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.5rem' }}>
              <div><label style={{ fontWeight: 600, fontSize: '13px' }}>Node ID</label><input style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.node_id} onChange={e => setForm({ ...form, node_id: e.target.value })} placeholder="org:acme" /></div>
              <div><label style={{ fontWeight: 600, fontSize: '13px' }}>Type</label><select style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.node_type} onChange={e => setForm({ ...form, node_type: e.target.value })}><option value="org">Org</option><option value="business_unit">Business Unit</option><option value="project">Project</option><option value="environment">Environment</option></select></div>
              <div><label style={{ fontWeight: 600, fontSize: '13px' }}>Parent</label><select style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.parent_id} onChange={e => setForm({ ...form, parent_id: e.target.value })}>
                {/* Chosen from what exists, because create_node accepts any
                    parent_id and one naming no node yields a second root
                    whose limits are bounded by nothing. */}
                <option value="">(none — root org)</option>
                {rows.map(({ node: n, depth }) => (
                  <option key={n.node_id} value={n.node_id}>{'\u00A0'.repeat(depth * 2)}{n.node_id}</option>
                ))}
              </select></div>
              <div><label style={{ fontWeight: 600, fontSize: '13px' }}>Display Name</label><input style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.display_name} onChange={e => setForm({ ...form, display_name: e.target.value })} /></div>
              <div><label style={{ fontWeight: 600, fontSize: '13px' }}>Rate Limit (RPM)</label><input type="number" style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.rate_limit_rpm} onChange={e => setForm({ ...form, rate_limit_rpm: e.target.value })} /></div>
              <div><label style={{ fontWeight: 600, fontSize: '13px' }}>Budget Limit ($)</label><input type="number" style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.budget_limit} onChange={e => setForm({ ...form, budget_limit: e.target.value })} /></div>
              <div><label style={{ fontWeight: 600, fontSize: '13px' }}>Max Tokens / Request</label><input type="number" style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.max_tokens_per_request} onChange={e => setForm({ ...form, max_tokens_per_request: e.target.value })} placeholder="(inherit)" /></div>
            </div>
            <div style={{ marginTop: '0.75rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <input type="checkbox" id="pii-enabled" checked={form.pii_redaction_enabled} onChange={e => setForm({ ...form, pii_redaction_enabled: e.target.checked })} />
              <label htmlFor="pii-enabled" style={{ fontWeight: 600, fontSize: '13px' }}>Enable PII Redaction</label>
            </div>
            {form.pii_redaction_enabled && (
              <div style={{ marginTop: '0.5rem' }}><label style={{ fontWeight: 600, fontSize: '13px' }}>PII Types (comma-separated)</label><input style={{ display: 'block', width: '100%', padding: '0.4rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '6px', marginTop: '0.25rem' }} value={form.pii_redact_types} onChange={e => setForm({ ...form, pii_redact_types: e.target.value })} placeholder="email, ssn, phone, credit_card, ip_address" /></div>
            )}
            <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.75rem' }}><Btn variant="primary" onClick={createNode} disabled={!form.node_id}>Create</Btn><Btn variant="normal" onClick={() => setShowForm(false)}>Cancel</Btn></div>
          </div>
        </div>
      )}
      <div className="container">
        <div className="container-header"><h2>Policy Nodes <span className="counter">({nodes.length})</span></h2></div>
        <div className="container-body no-pad">
          {nodes.length === 0 ? (
            <EmptyState title="No policy nodes" subtitle="Create an org node to start building your hierarchy." />
          ) : (
            <table>
              <thead><tr><th>Node</th><th>Type</th><th>Display Name</th><th>Rate Limit</th><th>Budget</th><th>Max Tokens</th><th>PII</th></tr></thead>
              <tbody>
                {rows.map(({ node: n, depth }) => (
                  <tr key={n.node_id}>
                    {/* Indent by depth instead of showing a Parent column.
                        The parent is the row above; a column repeating its
                        id costs width without adding anything the position
                        does not already say. */}
                    <td style={{ paddingLeft: `calc(0.75rem + ${depth * 1.25}rem)` }}>
                      {depth > 0 && (
                        <span aria-hidden="true" style={{ color: 'var(--awsui-color-text-secondary)', marginRight: '0.4rem' }}>└</span>
                      )}
                      <strong>{n.node_id}</strong>
                    </td>
                    <td><span className={`badge ${nodeTypeColor(n.node_type)}`}>{n.node_type}</span></td>
                    <td>{n.display_name}</td>
                    {/* An em dash, not a zero: a node that sets no limit of
                        its own inherits its parent's, which is the opposite
                        of a limit of nothing. */}
                    <td>{n.limits.rate_limit_rpm ? `${n.limits.rate_limit_rpm} RPM` : '—'}</td>
                    <td>{n.limits.budget_limit ? `$${n.limits.budget_limit.toLocaleString()}` : '—'}</td>
                    <td>{n.limits.max_tokens_per_request ? n.limits.max_tokens_per_request.toLocaleString() : '—'}</td>
                    <td>
                      {n.limits.pii_redaction_enabled
                        ? <span className="badge badge-green">{(n.limits.pii_redact_types || []).join(', ') || 'ON'}</span>
                        : (n.limits.pii_reinject === false
                            /* Not a redaction toggle: this node keeps
                               redaction as inherited but makes it
                               permanent, so the response comes back with
                               placeholders rather than the originals. */
                            ? <span className="badge badge-orange">no re-inject</span>
                            : '—')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Quotas Page                                                         */
/* ================================================================== */
function QuotasPage() {
  const [projectId, setProjectId] = useState('');
  const [quota, setQuota] = useState(null);
  const [simResult, setSimResult] = useState(null);
  const [error, setError] = useState(null);
  const [msg, setMsg] = useState(null);
  const [simForm, setSimForm] = useState({ project_id: '', model: '', provider: '', max_tokens: '', estimated_cost: '' });

  const loadQuota = async () => {
    if (!projectId.trim()) return;
    setError(null); setQuota(null);
    try {
      const data = await api.get(`/admin/quotas/${encodeURIComponent(projectId)}`);
      setQuota(data);
    } catch (e) { setError(e.detail || 'Failed to load quota'); }
  };

  const resetSpend = async () => {
    if (!projectId.trim()) return;
    setMsg(null);
    try {
      const res = await api.post(`/admin/quotas/${encodeURIComponent(projectId)}/reset`);
      setMsg(`Reset successful. Previous spend: ${fmt.cost(res.previous_spend)}`);
      loadQuota();
    } catch (e) { setError('Reset failed'); }
  };

  const simulate = async () => {
    setSimResult(null); setError(null);
    try {
      const body = {
        project_id: simForm.project_id,
        model: simForm.model,
        provider: simForm.provider || undefined,
        max_tokens: simForm.max_tokens ? parseInt(simForm.max_tokens) : undefined,
        estimated_cost: simForm.estimated_cost ? parseFloat(simForm.estimated_cost) : 0,
      };
      const res = await api.post('/admin/quotas/simulate', body);
      setSimResult(res);
    } catch (e) { setError('Simulation failed'); }
  };

  const inputStyle = { padding: '0.4rem 0.75rem', border: '2px solid var(--awsui-color-border-input)', borderRadius: '8px', fontSize: '14px', fontFamily: 'inherit', width: '100%' };

  return (
    <div>
      <div className="page-header"><h1>Quota & Usage Controls</h1><p>Policy-hierarchy-driven budget, rate, and model enforcement</p></div>
      {error && <Flash type="error" onDismiss={() => setError(null)}>{error}</Flash>}
      {msg && <Flash type="success" onDismiss={() => setMsg(null)}>{msg}</Flash>}

      {/* Lookup Section */}
      <div className="container" style={{ marginBottom: '1.5rem' }}>
        <div className="container-header"><h2>Project Quota Lookup</h2></div>
        <div className="container-body">
          <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'flex-end' }}>
            <div style={{ flex: 1 }}>
              <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '0.25rem' }}>Project ID</label>
              <input style={inputStyle} placeholder="e.g. proj:ml-team" value={projectId} onChange={e => setProjectId(e.target.value)} onKeyDown={e => e.key === 'Enter' && loadQuota()} />
            </div>
            <Btn variant="primary" onClick={loadQuota}>Lookup</Btn>
          </div>

          {quota && (
            <div style={{ marginTop: '1.5rem' }}>
              <div className="stat-grid">
                <div className="stat-card"><div className="stat-label">Rate Limit</div><div className="stat-value">{quota.policy_limits.rate_limit_rpm ?? 'unlimited'} <span style={{fontSize:'12px',color:'var(--awsui-color-text-secondary)'}}>RPM</span></div></div>
                <div className="stat-card"><div className="stat-label">Budget Limit</div><div className="stat-value">{quota.policy_limits.budget_limit ? fmt.cost(quota.policy_limits.budget_limit) : 'unlimited'}</div></div>
                <div className="stat-card"><div className="stat-label">Current Spend</div><div className="stat-value">{fmt.cost(quota.usage.current_spend)}</div></div>
                <div className="stat-card"><div className="stat-label">Budget Used</div><div className="stat-value">{quota.usage.budget_utilization_pct != null ? fmt.pct(quota.usage.budget_utilization_pct) : 'N/A'}</div></div>
              </div>

              <div style={{ marginTop: '1rem' }}>
                <h3 style={{ fontSize: '14px', fontWeight: 600, marginBottom: '0.5rem' }}>Policy Limits</h3>
                <table>
                  <tbody>
                    <tr><td style={{fontWeight:600}}>Max Tokens/Request</td><td>{quota.policy_limits.max_tokens_per_request ?? 'unlimited'}</td></tr>
                    <tr><td style={{fontWeight:600}}>Allowed Models</td><td>{quota.policy_limits.allowed_models ? quota.policy_limits.allowed_models.join(', ') : 'all'}</td></tr>
                    <tr><td style={{fontWeight:600}}>Allowed Providers</td><td>{quota.policy_limits.allowed_providers ? quota.policy_limits.allowed_providers.join(', ') : 'all'}</td></tr>
                    <tr><td style={{fontWeight:600}}>PII Redaction</td><td><span className={`badge ${quota.policy_limits.pii_redaction_enabled ? 'badge-green' : 'badge-grey'}`}>{quota.policy_limits.pii_redaction_enabled ? 'enabled' : 'disabled'}</span></td></tr>
                    {quota.policy_limits.pii_redact_types && quota.policy_limits.pii_redact_types.length > 0 && (
                      <tr><td style={{fontWeight:600}}>PII Types</td><td>{quota.policy_limits.pii_redact_types.join(', ')}</td></tr>
                    )}
                  </tbody>
                </table>
              </div>

              <div style={{ marginTop: '1rem', display: 'flex', gap: '0.5rem' }}>
                <Btn variant="danger" onClick={resetSpend}>Reset Spend</Btn>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Simulate Section */}
      <div className="container">
        <div className="container-header"><h2>Request Simulator</h2></div>
        <div className="container-body">
          <p style={{ fontSize: '13px', color: 'var(--awsui-color-text-secondary)', marginBottom: '1rem' }}>Test whether a request would pass quota enforcement without sending it.</p>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.75rem', marginBottom: '1rem' }}>
            <div>
              <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '0.25rem' }}>Project ID *</label>
              <input style={inputStyle} placeholder="proj:ml-team" value={simForm.project_id} onChange={e => setSimForm({...simForm, project_id: e.target.value})} />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '0.25rem' }}>Model *</label>
              <input style={inputStyle} placeholder="claude-opus" value={simForm.model} onChange={e => setSimForm({...simForm, model: e.target.value})} />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '0.25rem' }}>Provider</label>
              <input style={inputStyle} placeholder="anthropic (optional)" value={simForm.provider} onChange={e => setSimForm({...simForm, provider: e.target.value})} />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '0.25rem' }}>Max Tokens</label>
              <input style={inputStyle} type="number" placeholder="4096 (optional)" value={simForm.max_tokens} onChange={e => setSimForm({...simForm, max_tokens: e.target.value})} />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '0.25rem' }}>Estimated Cost ($)</label>
              <input style={inputStyle} type="number" step="0.001" placeholder="0.05" value={simForm.estimated_cost} onChange={e => setSimForm({...simForm, estimated_cost: e.target.value})} />
            </div>
          </div>
          <Btn variant="primary" onClick={simulate}>Simulate Request</Btn>

          {simResult && (
            <div style={{ marginTop: '1rem', padding: '1rem', borderRadius: '8px', background: simResult.allowed ? 'var(--awsui-color-background-status-success)' : 'var(--awsui-color-background-status-error)', border: `1px solid ${simResult.allowed ? 'var(--awsui-color-status-success)' : 'var(--awsui-color-status-error)'}` }}>
              <div style={{ fontSize: '16px', fontWeight: 700, marginBottom: '0.5rem', color: simResult.allowed ? 'var(--awsui-color-status-success)' : 'var(--awsui-color-status-error)' }}>
                {simResult.allowed ? '✓ Request Allowed' : '✗ Request Blocked'}
              </div>
              {!simResult.allowed && (
                <div>
                  <div style={{ fontSize: '13px' }}><strong>Reason:</strong> {simResult.reason}</div>
                  <div style={{ fontSize: '13px' }}><strong>Limit Type:</strong> {simResult.limit_type}</div>
                  {simResult.limit_value != null && <div style={{ fontSize: '13px' }}><strong>Limit Value:</strong> {simResult.limit_value}</div>}
                  {simResult.current_value != null && <div style={{ fontSize: '13px' }}><strong>Current Value:</strong> {simResult.current_value}</div>}
                </div>
              )}
              <div style={{ marginTop: '0.75rem', fontSize: '12px', color: 'var(--awsui-color-text-secondary)' }}>
                <strong>Resolved Policy:</strong> RPM={simResult.resolved_policy.rate_limit_rpm ?? '∞'}, Budget={simResult.resolved_policy.budget_limit ? fmt.cost(simResult.resolved_policy.budget_limit) : '∞'}, MaxTokens={simResult.resolved_policy.max_tokens_per_request ?? '∞'}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ================================================================== */
/*  Sandbox Page                                                       */
/* ================================================================== */
function SandboxPage() {
  const [tab, setTab] = useState('chat');

  const tabStyle = (t) => ({
    padding: '0.5rem 1.25rem', fontSize: '13px', fontWeight: 600,
    cursor: 'pointer', transition: 'all 120ms', border: 'none', fontFamily: 'inherit',
    background: tab === t ? '#7c3aed' : 'transparent', color: tab === t ? '#fff' : '#78716c',
    borderRadius: '10px',
  });

  return (
    <div>
      <div className="page-header">
        <h1>Sandbox</h1>
        <p>Run live chat, direct model prompts, and intent-aware routing through this gateway</p>
      </div>
      <div style={{ display: 'flex', gap: '0.35rem', background: '#f5f5f4', padding: '0.3rem', borderRadius: '12px', marginBottom: '1rem', width: 'fit-content' }}>
        <button style={tabStyle('chat')} onClick={() => setTab('chat')}>Chat</button>
        <button style={tabStyle('playground')} onClick={() => setTab('playground')}>Playground</button>
        <button style={tabStyle('routing')} onClick={() => setTab('routing')}>Routing</button>
      </div>

      <iframe src={tab === 'chat' ? '/chat' : tab === 'playground' ? '/playground' : '/routing'}
        title={'Sandbox ' + tab}
        style={{ width: '100%', height: 'calc(100vh - 215px)', minHeight: '520px', border: '1.5px solid #e7e5e4', borderRadius: '16px', background: '#fff' }} />
    </div>
  );
}

/* ================================================================== */
/*  Report Pages (Architecture / Pricing / Readiness)                  */
/* ================================================================== */

/* These three are rendered server-side by page_style.py rather than as
   React components, because they are also standalone URLs an operator opens
   from an alert with the gateway in a bad state -- when the dashboard's own
   API calls may be exactly what is failing.

   They used to be plain sidebar links, which navigated the browser away and
   took the shell with it: the sidebar vanished and the only way back was
   the ribbon. Framed here instead, so they behave like Configuration --
   sidebar stays, the main pane swaps.

   An iframe rather than injecting the HTML: those pages set global body,
   table, th, td and code rules, which would restyle every table in the
   dashboard. ?embed=1 drops their ribbon and page framing, since this
   shell already supplies both. */
function ReportFrame({ src, title, subtitle }) {
  return (
    <div>
      <div className="page-header">
        <h1>{title}</h1><p>{subtitle}</p>
      </div>
      <iframe
        src={src + '?embed=1'}
        title={title}
        /* The report pages vary from one banner to a long table, and a
           fixed height would either clip the long ones or leave the short
           ones floating in whitespace. Sized to the pane and scrolled
           internally, matching SandboxPage. */
        style={{ width: '100%', height: 'calc(100vh - 150px)', border: '1.5px solid #e7e5e4', borderRadius: '16px', background: '#fff' }}
      />
    </div>
  );
}

/* ================================================================== */
/*  Guided Tour                                                        */
/* ================================================================== */

/* A narrated walkthrough of the dashboard: ten scenes, each one a view of
   the shell plus a Polly track explaining it. The script and the audio come
   from static/tour/, fetched once when the tour starts rather than bundled
   here, so editing the narration is editing JSON and re-running
   scripts/build_narration_audio.sh -- no change to this file.

   Scene order and the view each scene talks about live in that JSON too
   (the `view` field is a key of the App switch below). A scene and the page
   it describes therefore cannot drift apart, which is the failure mode of
   every demo script kept in a separate document.

   Advance is driven by the audio element's own timeupdate/ended events, not
   by a setInterval over a declared duration. The durations in the JSON are
   measured by ffprobe at synthesis time and are honest, but a timer racing
   an audio element still diverges -- buffering, a background tab throttling
   timers, or the user seeking all break it. Reading currentTime means the
   bar cannot disagree with the voice. */
function GuidedTour({ onNavigate, onEnd }) {
  const [scenes, setScenes] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [i, setI] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [paused, setPaused] = useState(false);
  /* Autoplay is permitted here because the tour only ever starts from a
     click, but a browser can still refuse (an audio-blocking policy, a
     muted device profile). Surfaced rather than swallowed: without it the
     tour looks broken -- slides advancing in silence with no explanation. */
  const [audioBlocked, setAudioBlocked] = useState(false);
  const audioRef = useRef(null);

  useEffect(() => {
    fetch('/admin/static/tour/tour-narration.json')
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(d => setScenes(d.tracks || []))
      .catch(e => setLoadError(e && e.message ? e.message : 'unknown error'));
  }, []);

  const scene = scenes && scenes[i];
  const total = scenes ? scenes.length : 0;

  /* Navigate the shell to this scene's page. Separate from the audio effect
     so that seeking within a scene does not re-navigate.

     `project` is optional and only some pages read it: the API-keys page
     lists nothing until a project is named, so the scene about keys names
     one rather than narrating an empty form. */
  useEffect(() => {
    if (scene) onNavigate(scene.view, scene.project ? { projectId: scene.project } : {});
  }, [scene && scene.view, scene && scene.project, onNavigate]);

  /* One Audio per scene, torn down on scene change so two tracks can never
     overlap -- the bug you get from reusing one element and racing a load
     against a play. */
  useEffect(() => {
    if (!scene) return;
    const audio = new Audio('/admin/static/tour/' + scene.id + '.mp3');
    audioRef.current = audio;
    setElapsed(0);
    setPaused(false);

    const onTime = () => setElapsed(audio.currentTime);
    const onEnded = () => {
      if (i < total - 1) setI(i + 1);
      else onEnd();
    };
    audio.addEventListener('timeupdate', onTime);
    audio.addEventListener('ended', onEnded);
    audio.play().then(() => {
      setAudioBlocked(false);
      setPaused(false);
    }).catch(() => {
      setAudioBlocked(true);
      setPaused(true);
    });

    return () => {
      audio.removeEventListener('timeupdate', onTime);
      audio.removeEventListener('ended', onEnded);
      audio.pause();
      audioRef.current = null;
    };
  }, [scene && scene.id, i, total, onEnd]);

  const togglePause = () => {
    const a = audioRef.current;
    if (!a) return;
    if (a.paused) { a.play().catch(() => {}); setPaused(false); }
    else { a.pause(); setPaused(true); }
  };

  if (loadError) {
    /* The narration is missing or unreadable. Say so and offer the way out
       rather than sitting on a blank bar: a checkout without the MP3s
       synthesized is the likely cause, and the message names the fix. */
    return (
      <div style={tourShellStyle}>
        <div style={{ ...tourCardStyle, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '1rem' }}>
          <span style={{ fontSize: '13px' }}>
            Narration unavailable ({loadError}). Run <code style={{ background: '#f5f5f4', padding: '0 4px', borderRadius: '3px' }}>scripts/build_narration_audio.sh tour</code> to synthesize it.
          </span>
          <button onClick={onEnd} style={tourBtnStyle(false)}>Close</button>
        </div>
      </div>
    );
  }
  if (!scene) return null;

  /* Prefer the element's real duration; fall back to the measured value in
     the JSON until the metadata has loaded, so the bar has a scale from the
     first frame instead of jumping once loadedmetadata fires. */
  const a = audioRef.current;
  const dur = (a && isFinite(a.duration) && a.duration > 0) ? a.duration : (scene.duration || 1);
  const pct = Math.min(100, (elapsed / dur) * 100);
  const actColor = scene.act.indexOf('ACT 1') === 0 ? '#7c3aed'
                 : scene.act.indexOf('ACT 2') === 0 ? '#2563eb'
                 : '#16a34a';

  return (
    <div style={tourShellStyle}>
      <div style={{ height: '3px', background: '#e7e5e4', borderRadius: '3px 3px 0 0', overflow: 'hidden' }}>
        <div style={{ height: '100%', width: pct + '%', background: actColor, transition: 'width 200ms linear' }} />
      </div>
      <div style={tourCardStyle}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
          <div style={{ display: 'flex', gap: '0.6rem', alignItems: 'center' }}>
            <span style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.05em', color: actColor, background: actColor + '18', padding: '2px 8px', borderRadius: '4px' }}>
              {scene.act}
            </span>
            <span style={{ fontSize: '11px', color: '#a8a29e' }}>Scene {i + 1} of {total}</span>
            {audioBlocked && (
              <span style={{ fontSize: '11px', color: '#d97706' }}>
                audio blocked by the browser — press Play
              </span>
            )}
          </div>
          <button onClick={onEnd} style={{ background: 'none', border: 'none', color: '#a8a29e', cursor: 'pointer', fontSize: '12px' }}>
            ✕ End tour
          </button>
        </div>

        <div style={{ fontSize: '15px', fontWeight: 700, color: '#0c0a09', marginBottom: '0.35rem' }}>{scene.title}</div>
        <div style={{ fontSize: '13px', lineHeight: 1.55, color: '#57534e', maxHeight: '5.6rem', overflowY: 'auto', marginBottom: '0.5rem' }}>
          {scene.text}
        </div>
        {scene.callout && (
          <div style={{ fontSize: '12px', color: actColor, borderLeft: '2px solid ' + actColor, paddingLeft: '0.6rem', marginBottom: '0.7rem' }}>
            {scene.callout}
          </div>
        )}

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ display: 'flex', gap: '5px' }}>
            {scenes.map((s, n) => (
              <button key={s.id} onClick={() => setI(n)} title={s.title}
                aria-label={'Scene ' + (n + 1) + ': ' + s.title}
                style={{ width: '8px', height: '8px', padding: 0, borderRadius: '50%', border: 'none', cursor: 'pointer',
                         background: n === i ? actColor : n < i ? '#d6d3d1' : '#f5f5f4' }} />
            ))}
          </div>
          <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center' }}>
            <span style={{ fontSize: '11px', color: '#a8a29e', fontVariantNumeric: 'tabular-nums' }}>
              {Math.floor(elapsed)}s / {Math.round(dur)}s
            </span>
            <button onClick={() => setI(Math.max(0, i - 1))} disabled={i === 0} style={tourBtnStyle(false, i === 0)}>← Prev</button>
            <button onClick={togglePause} style={{ ...tourBtnStyle(false), minWidth: '68px' }}>{paused ? '▶ Play' : '⏸ Pause'}</button>
            <button onClick={() => { if (i < total - 1) setI(i + 1); else onEnd(); }} style={tourBtnStyle(true)}>
              {i === total - 1 ? '✓ Finish' : 'Next →'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* Pinned bottom-centre and above the sidebar's stacking context, offset by
   the sidebar so it centres on the content rather than the viewport. */
const tourShellStyle = {
  position: 'fixed', bottom: '1.25rem', left: 'calc(var(--sidebar-w) + (100vw - var(--sidebar-w)) / 2)',
  transform: 'translateX(-50%)', width: 'min(760px, calc(100vw - var(--sidebar-w) - 3rem))', zIndex: 500,
};
const tourCardStyle = {
  background: 'rgba(255,255,255,0.97)', backdropFilter: 'blur(12px)',
  border: '1.5px solid #e7e5e4', borderTop: 'none', borderRadius: '0 0 16px 16px',
  padding: '0.85rem 1.1rem', boxShadow: '0 12px 32px rgba(12,10,9,0.13)',
};
const tourBtnStyle = (primary, disabled) => ({
  background: disabled ? '#f5f5f4' : primary ? '#7c3aed' : '#fff',
  color: disabled ? '#d6d3d1' : primary ? '#fff' : '#57534e',
  border: primary ? 'none' : '1px solid #d6d3d1',
  padding: '0.3rem 0.7rem', borderRadius: '8px', fontSize: '12px', fontWeight: 600,
  fontFamily: 'inherit', cursor: disabled ? 'default' : 'pointer',
});

/* ================================================================== */
/*  App Shell                                                          */
/* ================================================================== */
function App() {
  const [view, setView] = useState('overview');
  const [selectedProject, setSelectedProject] = useState(null);
  const [editProjectId, setEditProjectId] = useState(null);
  const [selectedUser, setSelectedUser] = useState(null);
  /* The landing-page showcase deep-links here with ?tour=1. Starting the
     player from initial state makes the overlay available on the first render
     rather than flashing the Overview page and waiting for a later effect.
     Browsers may still block narration after a navigation; GuidedTour surfaces
     that state and turns its control into Play instead of claiming to pause
     audio that never started. */
  const [tourActive, setTourActive] = useState(() => {
    const value = new URLSearchParams(window.location.search).get('tour');
    return value === '1' || value === 'true';
  });
  const [browserAuth, setBrowserAuth] = useState(null);

  useEffect(() => {
    let active = true;
    fetch('/auth/config', {
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Accept': 'application/json' },
    })
      .then((response) => response.ok ? response.json() : null)
      .then((payload) => {
        if (active && payload && payload.browser_auth && payload.browser_auth.enabled) {
          browserSessionMode = true;
          setApiKey('');
          setBrowserAuth(payload.browser_auth);
        }
      })
      .catch(() => {});
    return () => { active = false; };
  }, []);

  const handleSignOut = useCallback(async () => {
    if (!browserAuth) return;
    try {
      const payload = await request('POST', browserAuth.logout_url);
      if (payload && typeof payload.logout_url === 'string') {
        window.location.assign(payload.logout_url);
      }
    } catch (error) {
      window.alert(error && error.message ? error.message : 'Sign out failed.');
    }
  }, [browserAuth]);

  const navigate = (v, opts = {}) => {
    setView(v);
    setSelectedProject(opts.projectId || null);
    setEditProjectId(opts.editProjectId || null);
    setSelectedUser(opts.userId || null);
  };

  /* Passed to GuidedTour, which calls it from an effect on every scene
     change. Memoised on nothing: an unstable identity would re-run that
     effect on each render of the shell and re-navigate mid-scene. */
  const tourNavigate = useCallback((v, opts) => navigate(v, opts || {}), []);
  const endTour = useCallback(() => setTourActive(false), []);

  let content;
  switch (view) {
    case 'overview': content = <OverviewPage />; break;
    case 'efficiency': content = <EfficiencyPage />; break;
    case 'traces': content = <TracesPage />; break;
    case 'projects': content = <ProjectsPage onSelect={(id) => navigate('project-detail', { projectId: id })} onCreateNew={() => navigate('project-form')} />; break;
    case 'project-detail': content = <ProjectDetailPage projectId={selectedProject} onBack={() => navigate('projects')} onEdit={(id) => navigate('project-form', { editProjectId: id })} />; break;
    case 'project-form': content = <ProjectFormPage projectId={editProjectId} onBack={() => editProjectId ? navigate('project-detail', { projectId: editProjectId }) : navigate('projects')} onSaved={() => navigate('projects')} />; break;
    case 'users': content = <UsersPage onSelect={(id) => navigate('user-detail', { userId: id })} />; break;
    case 'user-detail': content = <UserDetailPage userId={selectedUser} onBack={() => navigate('users')} />; break;
    case 'models': content = <ModelsPage />; break;
    case 'policies': content = <PoliciesPage />; break;
    case 'policy-hierarchy': content = <PolicyHierarchyPage />; break;
    case 'api-keys': content = <ApiKeysPage key={selectedProject || ''} initialProjectId={selectedProject} />; break;
    case 'security': content = <SecurityPage />; break;
    case 'webhooks': content = <WebhooksPage />; break;
    case 'quotas': content = <QuotasPage />; break;
    case 'regions': content = <RegionsPage />; break;
    case 'configuration': content = <ConfigurationPage />; break;
    case 'health': content = <HealthPage />; break;
    case 'sandbox': content = <SandboxPage />; break;
    case 'architecture': content = <ReportFrame src="/admin/architecture" title="Architecture" subtitle="How a request moves through the gateway" />; break;
    case 'pricing-drift': content = <ReportFrame src="/admin/pricing-drift" title="Pricing Coverage" subtitle="Provider mappings with no price, and prices nothing uses" />; break;
    case 'catalog-drift': content = <ReportFrame src="/admin/catalog-drift" title="Catalogue Coverage" subtitle="Declared, described, and observed models, and where the three disagree" />; break;
    case 'production-checklist': content = <ReportFrame src="/admin/production-checklist" title="Production Readiness" subtitle="States this deployment runs in without complaint" />; break;
    default: content = <OverviewPage />;
  }

  const navSections = [
    { label: 'Sandbox', items: [
      { key: 'sandbox', icon: '🧪', label: 'Sandbox' },
    ]},
    { label: 'Observe', items: [
      { key: 'overview', icon: '📊', label: 'Overview' },
      { key: 'traces', icon: '📡', label: 'Traces' },
      { key: 'efficiency', icon: '⚡', label: 'Efficiency' },
      { key: 'security', icon: '🛡', label: 'Audit Log' },
    ]},
    { label: 'Configure', items: [
      { key: 'models', icon: '🤖', label: 'Models' },
      { key: 'projects', icon: '📁', label: 'Projects', activeViews: ['projects', 'project-detail', 'project-form'] },
      { key: 'users', icon: '👤', label: 'Users', activeViews: ['users', 'user-detail'] },
      { key: 'api-keys', icon: '🔑', label: 'API Keys' },
    ]},
    { label: 'Govern', items: [
      { key: 'policies', icon: '🔒', label: 'Policies' },
      /* The org > BU > project > environment tree. PolicyHierarchyPage was
         built and wired into the view switch, but nothing ever linked to it
         — the only way in was to set the view by hand. */
      { key: 'policy-hierarchy', icon: '🏛', label: 'Hierarchy' },
      { key: 'quotas', icon: '📏', label: 'Quotas' },
      { key: 'regions', icon: '🌐', label: 'Regions' },
      { key: 'webhooks', icon: '🔔', label: 'Webhooks' },
    ]},
    { label: 'System', items: [
      { key: 'health', icon: '💚', label: 'Health' },
      { key: 'configuration', icon: '⚙', label: 'Configuration' },
      /* No href: these render in the main pane like every other item, so
         the sidebar survives the click and stays highlighted. */
      { key: 'architecture', icon: '🗺', label: 'Architecture' },
      { key: 'pricing-drift', icon: '💲', label: 'Pricing' },
      { key: 'catalog-drift', icon: '📇', label: 'Catalogue' },
      { key: 'production-checklist', icon: '✅', label: 'Readiness' },
    ]},
  ];

  const isActive = (item) => {
    if (item.activeViews) return item.activeViews.includes(view);
    return view === item.key;
  };

  return (
    <div style={{display: 'flex', minHeight: '100vh'}}>
      {/* Sidebar */}
      <aside className="sidebar" role="navigation" aria-label="Main navigation">
        <div className="sidebar-logo">
          <svg width="28" height="28" viewBox="0 0 32 32" fill="none">
            <rect width="32" height="32" rx="8" fill="url(#axon-grad)"/>
            <path d="M8 22L16 10L24 22" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/>
            <circle cx="16" cy="10" r="2.5" fill="white"/>
            <circle cx="8" cy="22" r="2" fill="white" opacity="0.7"/>
            <circle cx="24" cy="22" r="2" fill="white" opacity="0.7"/>
            <line x1="12" y1="16" x2="20" y2="16" stroke="white" strokeWidth="1.5" opacity="0.5"/>
            <defs><linearGradient id="axon-grad" x1="0" y1="0" x2="32" y2="32"><stop stopColor="#8b5cf6"/><stop offset="1" stopColor="#6d28d9"/></linearGradient></defs>
          </svg>
          <span>AxonLLM</span>
        </div>
        <nav style={{flex: 1, padding: '0.5rem 0.5rem', overflow: 'auto'}}>
          {navSections.map(section => (
            <div key={section.label} className="sidebar-section">
              <div className="sidebar-section-label">{section.label}</div>
              {/* Every item navigates in-shell. The `item.href ?
                  window.location.href = ...` escape hatch this used to have
                  was the whole bug -- it left the sidebar behind -- and is
                  gone rather than left for the next item to rediscover. */}
              {section.items.map(item => (
                <button key={item.key} className={`nav-item ${isActive(item) ? 'active' : ''}`} onClick={() => navigate(item.key)}>
                  <span className="nav-icon">{item.icon}</span>
                  {item.label}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div style={{borderTop: '1px solid #f5f5f4', padding: '0.75rem 1.25rem'}}>
          <p style={{fontSize: '11px', color: '#a8a29e'}}>v0.2.0 · 13 providers</p>
        </div>
      </aside>

      {/* Main */}
      <div style={{flex: 1, marginLeft: 'var(--sidebar-w)', display: 'flex', flexDirection: 'column'}}>
        {/* Top ribbon */}
        <header className="topbar" role="banner">
          <div className="topbar-brand">
            <div className="topbar-status"><div className="dot"></div>AxonLLM — The neural control plane for enterprise LLMs</div>
          </div>
          <div className="topbar-right">
            {browserAuth && (
              <button className="topbar-pill"
                      onClick={handleSignOut}
                      title="Sign out">
                Sign out
              </button>
            )}
            {/* Hidden while the tour runs: its own End button is the way
                out, and a second control that restarts from scene one is a
                trap next to a Pause. */}
            {!tourActive && (
              <button className="topbar-pill" onClick={() => setTourActive(true)}
                      title="A narrated walkthrough of the dashboard — ten scenes, about six minutes">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
                     strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <polygon points="6 4 20 12 6 20 6 4"></polygon>
                </svg>
                Guided Demo
              </button>
            )}
            {/* Absolute, not relative: the dashboard is served from
                /admin/dashboard, so "architecture.html" would resolve to
                /admin/architecture.html and 404. Leading slash hits the
                site route, which serves it on any host the gateway runs on
                — no origin baked in. */}
            <a className="topbar-pill" href="/architecture.html"
               target="_blank" rel="noopener"
               title="Interactive architecture diagrams — infrastructure, request pipeline, components">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
                   strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <rect x="3" y="3" width="7" height="7" rx="1"></rect>
                <rect x="14" y="3" width="7" height="7" rx="1"></rect>
                <rect x="14" y="14" width="7" height="7" rx="1"></rect>
                <rect x="3" y="14" width="7" height="7" rx="1"></rect>
              </svg>
              Interactive Architecture
            </a>
            <div className="topbar-tagline" style={{borderRadius: '999px', background: 'rgba(255,255,255,0.6)', border: '1px solid rgba(214,211,209,0.5)', padding: '0.2rem 0.75rem'}}>
              <span style={{fontSize: '10px', color: '#78716c'}}>Multi-provider routing · Real-time analytics</span>
            </div>
          </div>
        </header>
        <main className="main" role="main">
          {content}
        </main>
      </div>
      {/* Outside <main> so it is not replaced when the tour navigates the
          view it is narrating. Fixed-position, so its position in the tree
          only decides what it survives. */}
      {tourActive && <GuidedTour onNavigate={tourNavigate} onEnd={endTour} />}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
