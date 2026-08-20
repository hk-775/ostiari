const { useState, useEffect } = React;

const CSRF_COOKIE = '__Host-axon-csrf';

function getCsrfToken() {
  for (const part of document.cookie.split(';')) {
    const cookie = part.trim();
    if (cookie.startsWith(CSRF_COOKIE + '=')) {
      return cookie.slice(CSRF_COOKIE.length + 1);
    }
  }
  return '';
}

function appFetch(url, options) {
  const requestOptions = Object.assign({}, options || {});
  const method = (requestOptions.method || 'GET').toUpperCase();
  const headers = Object.assign({}, requestOptions.headers || {});
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrfToken = getCsrfToken();
    if (csrfToken) headers['X-Axon-CSRF-Token'] = csrfToken;
  }
  requestOptions.headers = headers;
  requestOptions.credentials = 'same-origin';
  return fetch(url, requestOptions);
}

function RoutingExplorer() {
  const [models, setModels] = useState([]);
  const [prompt, setPrompt] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [mode, setMode] = useState('smart');

  useEffect(() => { appFetch('/api/models').then(r => r.json()).then(setModels).catch(() => {}); }, []);

  const handleSend = async () => {
    if (!prompt.trim() || loading || models.length === 0) return;
    setLoading(true); setResult(null);

    const startTime = Date.now();

    try {
      // Send with smart_routing context flag — let the router pick the model
      const resp = await appFetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(
          mode === 'smart'
            ? {
                model: '',
                messages: [{ role: 'user', content: prompt.trim() }],
                context: { smart_routing: true },
              }
            : {
                model: mode, // "ensemble:budget" or "ensemble:quality"
                messages: [{ role: 'user', content: prompt.trim() }],
              }
        ),
      });
      const data = await resp.json();
      const latency = Date.now() - startTime;

      if (data.error) {
        setResult({ error: data.error.message || JSON.stringify(data.error), latency });
      } else {
        const usedProvider = data.provider || 'unknown';
        const usedModel = data.model || 'unknown';
        const smartRouting = data.smart_routing || null;
        const ensemble = data.ensemble || null;

        // Build model selection reason
        let modelReason = '';
        let providerReason = '';

        if (smartRouting) {
          modelReason = `Smart routing classified your prompt as "${smartRouting.task_type}" (confidence: ${Math.round(smartRouting.confidence * 100)}%) and selected "${smartRouting.selected_model}" with benchmark score ${smartRouting.benchmark_score}.`;
          if (smartRouting.used_fallback) {
            modelReason += ' ⚠️ Confidence was below threshold — used fallback model.';
          }
          providerReason = `Provider "${usedProvider}" was selected for model "${usedModel}" using health-aware routing.`;
        } else if (ensemble) {
          modelReason = `Ensemble routing dispatched your prompt to a panel of ${ensemble.panel.length} models (${ensemble.panel.join(', ')}) and "${ensemble.judge}" synthesized the ${ensemble.succeeded_count} surviving response(s) into one answer.`;
          providerReason = `Panel members ran concurrently; the judge model "${ensemble.judge}" produced the final synthesis. Estimated cost ≈ ${ensemble.cost_multiplier}× a single call.`;
        } else {
          modelReason = `The router selected "${usedModel}" from available models.`;
          providerReason = `Provider "${usedProvider}" was selected.`;
        }

        setResult({
          content: data.content,
          provider: usedProvider,
          providerModelId: usedModel,
          virtualModel: smartRouting ? smartRouting.selected_model : (ensemble ? ensemble.judge : usedModel),
          strategy: smartRouting ? 'smart' : (ensemble ? `ensemble (${ensemble.preset})` : 'unknown'),
          modelReason,
          providerReason,
          availableProviders: [],
          usage: data.usage || {},
          latency,
          smartRouting,
          ensemble,
        });
      }
    } catch (err) {
      setResult({ error: 'Connection error: ' + err.message, latency: Date.now() - startTime });
    }
    setLoading(false);
  };

  return (
    <div>
      <header className="topbar" role="banner">
        <div className="topbar-brand">
          <div className="logo-icon">R</div>
          AxonLLM
        </div>
        <div className="topbar-tagline">The neural control plane for enterprise LLMs</div>
        <nav className="topbar-nav">
          <a href="/admin/dashboard">Dashboard</a>
          <a href="/chat">Chat</a>
          <a href="/playground">Playground</a>
          <a href="/routing" className="active">Routing</a>
        </nav>
        <div className="topbar-right">
          <div className="topbar-status"><div className="dot"></div>System Online</div>
        </div>
      </header>

      <div className="page-wrapper">
        <p style={{color: 'var(--color-text-secondary)', fontSize: '13px', marginBottom: 'var(--space-xl)'}}>Type a prompt — AxonLLM picks the model and provider automatically, then shows what it chose and why.</p>

        <div className="container">
          <div className="container-header">Your Prompt</div>
          <div className="container-body">
            <div className="mode-selector" role="radiogroup" aria-label="Routing mode">
              <button
                className={`mode-btn ${mode === 'smart' ? 'active' : ''}`}
                onClick={() => setMode('smart')}
                type="button"
              >🧠 Smart</button>
              <button
                className={`mode-btn ${mode === 'ensemble:budget' ? 'active' : ''}`}
                onClick={() => setMode('ensemble:budget')}
                type="button"
              >🤝 Ensemble · Budget</button>
              <button
                className={`mode-btn ${mode === 'ensemble:quality' ? 'active' : ''}`}
                onClick={() => setMode('ensemble:quality')}
                type="button"
              >🤝 Ensemble · Quality</button>
            </div>
            <textarea
              placeholder="Type anything... The router will pick the best model and provider for you."
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleSend(); }}
              aria-label="Prompt input"
            />
            <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
              <div className="hint">
                {mode === 'smart'
                  ? 'Cmd+Enter to send. Smart routing classifies your prompt and selects the best single model.'
                  : 'Cmd+Enter to send. Ensemble routing fans out to a panel of models, then a judge synthesizes one answer (~N+1× cost).'}
              </div>
              <button className="btn-primary" onClick={handleSend} disabled={loading || !prompt.trim() || models.length === 0}>
                {loading ? <><span className="spinner"></span>Routing...</> : '🔀 Route & Send'}
              </button>
            </div>
          </div>
        </div>

        {result && !result.error && (
          <div>
            {result.smartRouting && (
              <div className="smart-routing-card">
                <div className="card-title">
                  🧠 Smart Routing Decision
                  {result.smartRouting.used_fallback && (
                    <span className="sr-badge sr-badge-fallback">⚠️ Fallback</span>
                  )}
                </div>
                <div className="sr-grid">
                  <div className="sr-item">
                    <div className="sr-label">Task Type</div>
                    <div className="sr-value"><span className="sr-badge">{result.smartRouting.task_type}</span></div>
                  </div>
                  <div className="sr-item">
                    <div className="sr-label">Confidence</div>
                    <div className="sr-value">{Math.round(result.smartRouting.confidence * 100)}%</div>
                  </div>
                  <div className="sr-item">
                    <div className="sr-label">Benchmark Score</div>
                    <div className="sr-value">{result.smartRouting.benchmark_score}</div>
                  </div>
                </div>
                {result.smartRouting.candidates && result.smartRouting.candidates.length > 0 && (
                  <div className="sr-candidates">
                    <div className="sr-label" style={{marginBottom:'4px'}}>Candidates Considered</div>
                    {result.smartRouting.candidates.map((c, i) => (
                      <div key={i} className={`sr-candidate ${c.model === result.smartRouting.selected_model ? 'sr-candidate-selected' : ''} ${c.filtered_reason ? 'sr-candidate-filtered' : ''}`}>
                        <span>{i + 1}.</span>
                        <span>{c.model}</span>
                        <span>— {c.benchmark_score} pts</span>
                        {c.model === result.smartRouting.selected_model && <span>✓ selected</span>}
                        {c.filtered_reason && <span>({c.filtered_reason})</span>}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {result.ensemble && (
              <div className="ensemble-card">
                <div className="card-title">
                  🤝 Ensemble Decision
                  <span className={`en-pill ${result.ensemble.quorum_met ? 'en-pill-met' : 'en-pill-unmet'}`}>
                    {result.ensemble.quorum_met ? 'Quorum met' : 'Quorum not met'} ({result.ensemble.succeeded_count}/{result.ensemble.quorum_threshold})
                  </span>
                  {result.ensemble.fallback_used && (
                    <span className="en-badge-fallback">⚠️ Fallback used</span>
                  )}
                </div>
                <div className="en-grid">
                  <div className="en-item">
                    <div className="en-label">Preset</div>
                    <div className="en-value">{result.ensemble.preset}</div>
                  </div>
                  <div className="en-item">
                    <div className="en-label">Judge Model</div>
                    <div className="en-value">{result.ensemble.judge}</div>
                  </div>
                  <div className="en-item">
                    <div className="en-label">Cost Multiplier</div>
                    <div className="en-value">{result.ensemble.cost_multiplier}× (panel + judge)</div>
                  </div>
                </div>
                <div className="en-members">
                  <div className="en-label">Panel Models Used</div>
                  {(result.ensemble.panel || []).map((m, i) => {
                    const failedEntry = (result.ensemble.failed || []).find(f => f.model === m);
                    const succeeded = (result.ensemble.succeeded || []).indexOf(m) !== -1 && !failedEntry;
                    return (
                      <div key={i} className={`en-member ${succeeded ? 'en-member-ok' : 'en-member-fail'}`}>
                        <span className="en-member-status">{succeeded ? '✓' : '✗'}</span>
                        <span>{i + 1}. {m}</span>
                        {succeeded
                          ? <span className="en-member-note">— responded</span>
                          : (failedEntry && failedEntry.reason && <span className="en-member-reason">— {failedEntry.reason}</span>)}
                      </div>
                    );
                  })}
                  <div className="en-judge-row">
                    <span className="en-judge-arrow">⮕</span>
                    <span className="en-judge-label">Synthesized by judge:</span>
                    <span className="en-judge-model">{result.ensemble.judge}</span>
                  </div>
                </div>
              </div>
            )}

            <div className="decision">
              <div className="decision-box box-blue">
                <div className="label">Model Selected</div>
                <div className="value">{result.virtualModel}</div>
                <div className="sub">Provider model: {result.providerModelId}</div>
              </div>
              <div className="decision-box box-blue">
                <div className="label">Provider Selected</div>
                <div className="value">{result.provider}</div>
                <div className="sub">{result.availableProviders.length > 0 ? `Available: ${result.availableProviders.join(', ')}` : 'Health-aware selection'}</div>
              </div>
            </div>

            <div className="decision">
              <div className="decision-box box-orange">
                <div className="label">Routing Strategy</div>
                <div className="value">{result.strategy}</div>
              </div>
              <div className="decision-box box-green">
                <div className="label">Latency</div>
                <div className="value">{result.latency}ms</div>
                <div className="sub">{result.usage.total_tokens || '?'} tokens used</div>
              </div>
            </div>

            <div className="reason-card">
              <div className="label">🧠 Why This Model + Provider?</div>
              <div className="text">{result.modelReason}<br/><br/>{result.providerReason}</div>
            </div>

            <div className="container">
              <div className="container-header">Response from {result.provider} / {result.virtualModel}</div>
              <div className="container-body">
                <div className="response-text">{result.content}</div>
                <div className="stats">
                  <span>📥 {result.usage.prompt_tokens || '?'} prompt tokens</span>
                  <span>📤 {result.usage.completion_tokens || '?'} completion tokens</span>
                </div>
              </div>
            </div>
          </div>
        )}

        {result && result.error && (
          <div className="error-box">
            <strong>Error:</strong> {result.error}
            <div className="stats"><span>⏱ {result.latency}ms</span></div>
          </div>
        )}
      </div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<RoutingExplorer />);
