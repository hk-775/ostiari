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

/* ── ModelSelector Component ── */
function ModelSelector({ models, selectedModel, onSelect, loading, error }) {
  if (error) {
    return <select className="model-select" disabled><option>No models available</option></select>;
  }
  if (loading) {
    return <select className="model-select" disabled><option>Loading models...</option></select>;
  }
  if (!models || models.length === 0) {
    return <select className="model-select" disabled><option>No models available</option></select>;
  }
  return (
    <select
      className="model-select"
      value={selectedModel || ''}
      onChange={(e) => onSelect(e.target.value)}
      aria-label="Select model"
    >
      {models.map((m) => (
        <option key={m.name} value={m.name}>
          {m.name} [{m.providers ? m.providers.join(', ') : ''}]
        </option>
      ))}
    </select>
  );
}

/* ── MessageBubble Component ── */
function MessageBubble({ message }) {
  const roleLabel = message.role === 'user' ? 'U' : message.role === 'assistant' ? 'AI' : '!';
  const timeStr = message.timestamp
    ? new Date(message.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : '';

  return (
    <div className={`message-bubble ${message.role}`}>
      <div className="message-avatar">{roleLabel}</div>
      <div className="message-content-wrap">
        <div className="message-content">{message.content || '\u00A0'}</div>
        <div className="message-meta">
          {timeStr && <span>{timeStr}</span>}
          {message.role === 'assistant' && message.provider && (
            <span className="model-badge">
              {message.provider}{message.model ? ` / ${message.model}` : ''}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── MessageList Component ── */
function MessageList({ messages, loading }) {
  const listRef = useRef(null);
  const bottomRef = useRef(null);

  useEffect(() => {
    if (bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, loading]);

  if (messages.length === 0) {
    return (
      <div className="welcome">
      </div>
    );
  }

  return (
    <div className="message-list" ref={listRef}>
      {messages.map((msg, i) => (
        <MessageBubble key={i} message={msg} />
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
    </div>
  );
}

/* ── MessageInput Component ── */
function MessageInput({ onSend, disabled }) {
  const [text, setText] = useState('');
  const textareaRef = useRef(null);

  const handleSend = useCallback(() => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  }, [text, disabled, onSend]);

  const handleKeyDown = useCallback((e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }, [handleSend]);

  const handleInput = useCallback((e) => {
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 150) + 'px';
  }, []);

  return (
    <div className="input-area">
      <div style={{fontSize: '12px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.5px', color: 'var(--color-text-secondary)', marginBottom: '0.5rem'}}>Your Prompt</div>
      <div className="input-row">
        <textarea
          ref={textareaRef}
          rows="1"
          placeholder={disabled ? 'Waiting for response...' : 'Type a message...'}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          onInput={handleInput}
          disabled={disabled}
          aria-label="Message input"
        />
        <button
          className="btn-send"
          onClick={handleSend}
          disabled={disabled || !text.trim()}
          aria-label="Send message"
          title="Send message"
        >
          &#10148;
        </button>
      </div>
    </div>
  );
}

/* ── ChatApp Root Component ── */
function ChatApp() {
  const [messages, setMessages] = useState([]);
  const [selectedModel, setSelectedModel] = useState(null);
  const [selectedProvider, setSelectedProvider] = useState(null);
  const [models, setModels] = useState([]);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [modelsError, setModelsError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [users, setUsers] = useState([]);
  const [selectedUser, setSelectedUser] = useState(null);

  // Fetch models and users on mount
  useEffect(() => {
    appFetch('/api/models')
      .then((res) => {
        if (!res.ok) throw new Error('Failed to fetch models');
        return res.json();
      })
      .then((data) => {
        setModels(data);
        if (data.length > 0) {
          setSelectedModel(data[0].name);
        }
        setModelsLoading(false);
      })
      .catch(() => {
        setModelsError('Failed to load models');
        setModelsLoading(false);
      });
    appFetch('/api/users')
      .then((res) => res.ok ? res.json() : [])
      .then((data) => {
        setUsers(data);
        if (data.length > 0) setSelectedUser(data[0]);
      })
      .catch(() => {});
  }, []);

  const handleNewChat = useCallback(() => {
    setMessages([]);
  }, []);

  const handleSend = useCallback(async (text) => {
    if (!selectedModel) return;

    const userMessage = {
      role: 'user',
      content: text,
      timestamp: Date.now(),
    };

    // Build conversation history for the API (only role + content)
    const conversationHistory = [...messages, userMessage].map((m) => ({
      role: m.role === 'error' ? 'assistant' : m.role,
      content: m.content,
    })).filter((m) => m.role === 'user' || m.role === 'assistant');

    setMessages((prev) => [...prev, userMessage]);
    setLoading(true);

    // Create placeholder assistant message
    const assistantMessage = {
      role: 'assistant',
      content: '',
      model: '',
      provider: '',
      timestamp: Date.now(),
    };

    setMessages((prev) => [...prev, assistantMessage]);

    try {
      const response = await appFetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: selectedModel,
          messages: conversationHistory,
          user_id: selectedUser,
          provider: selectedProvider,
          // Empty model = "Auto (router picks)" → enable smart routing so the
          // backend classifies the prompt and selects a model.
          context: selectedModel ? {} : { smart_routing: true },
        }),
      });

      // Handle non-stream error responses
      if (!response.ok) {
        let errorMsg = 'An unexpected error occurred.';
        try {
          const errData = await response.json();
          if (response.status === 429) {
            errorMsg = 'Rate limit exceeded. Please wait before retrying.';
          } else if (response.status === 502) {
            errorMsg = 'The model provider is currently unavailable.';
          } else if (errData.error && errData.error.message) {
            errorMsg = errData.error.message;
          }
        } catch (e) {
          if (response.status === 429) {
            errorMsg = 'Rate limit exceeded. Please wait before retrying.';
          } else if (response.status === 502) {
            errorMsg = 'The model provider is currently unavailable.';
          }
        }

        // Replace the placeholder assistant message with error
        setMessages((prev) => {
          const updated = [...prev];
          updated[updated.length - 1] = {
            role: 'error',
            content: errorMsg,
            timestamp: Date.now(),
          };
          return updated;
        });
        setLoading(false);
        return;
      }

      // Stream the response
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed || !trimmed.startsWith('data:')) continue;

          const payload = trimmed.slice(5).trim();

          // [DONE] sentinel
          if (payload === '[DONE]') {
            setLoading(false);
            return;
          }

          try {
            const chunk = JSON.parse(payload);

            // Stream error event
            if (chunk.error) {
              const errMsg = chunk.error.message || 'A streaming error occurred.';
              setMessages((prev) => {
                const updated = [...prev];
                const last = updated[updated.length - 1];
                updated[updated.length - 1] = {
                  ...last,
                  role: 'error',
                  content: last.content ? last.content + '\n' + errMsg : errMsg,
                };
                return updated;
              });
              continue;
            }

            // Normal content chunk — append token
            setMessages((prev) => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              updated[updated.length - 1] = {
                ...last,
                content: (last.content || '') + (chunk.content || ''),
                model: chunk.model || last.model,
                provider: chunk.provider || last.provider,
              };
              return updated;
            });
          } catch (e) {
            // Ignore malformed JSON lines
          }
        }
      }

      // If we exit the loop without [DONE], mark as complete
      setLoading(false);

    } catch (err) {
      // Network error
      setMessages((prev) => {
        const updated = [...prev];
        updated[updated.length - 1] = {
          role: 'error',
          content: 'Connection error. Please check your network.',
          timestamp: Date.now(),
        };
        return updated;
      });
      setLoading(false);
    }
  }, [selectedModel, selectedUser, selectedProvider, messages]);

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
          <a href="/chat" className="active">Chat</a>
          <a href="/playground">Playground</a>
          <a href="/routing">Routing</a>
        </nav>
        <div className="topbar-right">
          <div className="topbar-status"><div className="dot"></div>System Online</div>
        </div>
      </header>

      <div className="chat-wrapper">
        <p style={{fontSize: '12px', color: 'var(--color-text-secondary)', padding: '0.5rem 0 0'}}>Select a model and start a conversation — routed to the provider selected.</p>
        <div className="chat-header">
          <div className="chat-header-left">
            <ModelSelector
              models={models}
              selectedModel={selectedModel}
              onSelect={setSelectedModel}
              loading={modelsLoading}
              error={modelsError}
            />
            {users.length > 0 && (
              <select
                className="model-select"
                value={selectedUser || ''}
                onChange={(e) => setSelectedUser(e.target.value)}
                aria-label="Select user"
                style={{marginLeft: '0.25rem'}}
              >
                {users.map((u) => (
                  <option key={u} value={u}>👤 {u}</option>
                ))}
              </select>
            )}
            {(() => {
              const mc = models.find(m => m.name === selectedModel);
              const provs = mc ? mc.providers : [];
              return provs.length > 0 ? (
                <select
                  className="model-select"
                  value={selectedProvider || ''}
                  onChange={(e) => setSelectedProvider(e.target.value || null)}
                  aria-label="Select provider"
                  style={{marginLeft: '0.25rem'}}
                >
                  <option value="">🔀 Auto (router picks)</option>
                  {provs.map((p) => (
                    <option key={p} value={p}>🏢 {p}</option>
                  ))}
                </select>
              ) : null;
            })()}
          </div>
          <button className="btn-new-chat" onClick={handleNewChat} aria-label="New chat">
            ✦ New Chat
          </button>
        </div>

        <MessageInput onSend={handleSend} disabled={loading} />

        <MessageList messages={messages} loading={loading && messages.length > 0 && messages[messages.length - 1].role !== 'assistant'} />
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<ChatApp />);
