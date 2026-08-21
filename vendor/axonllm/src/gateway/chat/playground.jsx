const { useState, useEffect, useRef, useCallback } = React;

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

function getRoutingReason(modelConfig, provider) {
  if (!modelConfig) return '';
  const providers = modelConfig.providers || [];
  const strategy = modelConfig.routing_strategy || 'round-robin';
  if (providers.length <= 1) return 'Only one provider configured — routed directly.';
  if (strategy === 'round-robin') return `Round-robin: cycled through ${providers.length} providers. Selected ${provider}.`;
  if (strategy === 'weighted') {
    const pi = providers.find(p => p.provider === provider);
    return `Weighted: ${provider} (weight ${pi?.weight || '?'}) from ${providers.map(p => p.provider + '(' + p.weight + ')').join(', ')}.`;
  }
  if (strategy === 'least-latency') return `Least-latency: ${provider} had lowest response time.`;
  if (strategy === 'cost-optimized') return `Cost-optimized: ${provider} is the cheapest healthy provider.`;
  return `${strategy}: selected ${provider}.`;
}

function Playground() {
  const [messages, setMessages] = useState([]);
  const [selectedModel, setSelectedModel] = useState(null);
  const [models, setModels] = useState([]);
  const [loading, setLoading] = useState(false);
  const bottomRef = useRef(null);
  const [text, setText] = useState('');
  const textareaRef = useRef(null);

  useEffect(() => {
    appFetch('/api/models')
      .then(r => r.json())
      .then(data => {
        setModels(data);
        if (data.length > 0) setSelectedModel(data[0].name);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (bottomRef.current) bottomRef.current.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  const handleSend = useCallback(async () => {
    const trimmed = text.trim();
    if (!trimmed || !selectedModel || loading) return;
    setText('');
    if (textareaRef.current) textareaRef.current.style.height = 'auto';
    const userMsg = { role: 'user', content: trimmed };
    const history = [...messages, userMsg]
      .filter(m => m.role === 'user' || m.role === 'assistant')
      .map(m => ({ role: m.role, content: m.content }));
    setMessages(prev => [...prev, userMsg]);
    setLoading(true);
    try {
      const resp = await appFetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: selectedModel, messages: history })
      });
      const data = await resp.json();
      if (data.error) {
        setMessages(prev => [...prev, { role: 'error', content: data.error.message || JSON.stringify(data.error) }]);
      } else {
        const mc = models.find(m => m.name === selectedModel);
        const reason = getRoutingReason(mc, data.provider);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: data.content,
          provider: data.provider,
          model: data.model,
          reason,
          usage: data.usage
        }]);
      }
    } catch (err) {
      setMessages(prev => [...prev, { role: 'error', content: 'Connection error.' }]);
    }
    setLoading(false);
  }, [text, selectedModel, loading, messages, models]);

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
          <a href="/playground" className="active">Playground</a>
          <a href="/routing">Routing</a>
        </nav>
        <div className="topbar-right">
          <div className="topbar-status"><div className="dot"></div>System Online</div>
        </div>
      </header>

      <div className="chat-wrapper">
        <div className="chat-header">
          <div className="chat-header-left">
            <span style={{fontSize: '13px', color: 'var(--color-text-secondary)'}}>Type Prompt - see latency, tokens, and provider selection.</span>
            <select
              className="model-select"
              value={selectedModel || ''}
              onChange={e => setSelectedModel(e.target.value)}
              aria-label="Select model"
            >
              {models.map(m => (
                <option key={m.name} value={m.name}>{m.name}</option>
              ))}
            </select>
          </div>
          <button className="btn-new-chat" onClick={() => setMessages([])} aria-label="New chat">
            ✦ New Chat
          </button>
        </div>

        <div className="input-area">
          <div style={{fontSize: '12px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.5px', color: 'var(--color-text-secondary)', marginBottom: '0.5rem'}}>Your Prompt</div>
          <div className="input-row">
            <textarea
              ref={textareaRef}
              rows="1"
              placeholder={loading ? 'Waiting...' : 'Type a message...'}
              value={text}
              onChange={e => setText(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  handleSend();
                }
              }}
              onInput={e => {
                e.target.style.height = 'auto';
                e.target.style.height = Math.min(e.target.scrollHeight, 150) + 'px';
              }}
              disabled={loading}
              aria-label="Message input"
            />
            <button
              className="btn-send"
              onClick={handleSend}
              disabled={loading || !text.trim()}
              aria-label="Send message"
              title="Send message"
            >&#10148;</button>
          </div>
        </div>

        <div className="message-list">
          {messages.length === 0 ? (
            <div className="welcome">
            </div>
          ) : (
            <>
              {messages.map((m, i) => (
                <div key={i} className={`message-bubble ${m.role}`}>
                  <div className="message-avatar">
                    {m.role === 'user' ? 'U' : m.role === 'assistant' ? 'AI' : '!'}
                  </div>
                  <div className="message-content-wrap">
                    <div className="message-content">{m.content || '\u00A0'}</div>
                    {m.role === 'assistant' && (
                      <div className="message-meta">
                        {m.provider && <span className="model-badge">🏢 {m.provider}</span>}
                        {m.model && <span className="model-badge">🤖 {m.model}</span>}
                        {m.usage && <span className="model-badge">📊 {m.usage.total_tokens} tokens</span>}
                      </div>
                    )}
                    {m.role === 'assistant' && m.reason && (
                      <div className="routing-reason">🔀 {m.reason}</div>
                    )}
                  </div>
                </div>
              ))}
              {loading && (
                <div className="message-bubble assistant">
                  <div className="message-avatar">AI</div>
                  <div className="message-content-wrap">
                    <div className="message-content">
                      <div className="loading-dots"><span></span><span></span><span></span></div>
                    </div>
                  </div>
                </div>
              )}
              <div ref={bottomRef} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<Playground />);
