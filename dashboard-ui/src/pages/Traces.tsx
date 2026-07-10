import { useState } from 'react'
import { useApi } from '../hooks/useApi'
import type { TraceEntry } from '../types'

const TIER_COLORS: Record<string, string> = { allow: '#22c55e', intervene: '#f59e0b', block: '#ef4444' }

export default function Traces() {
  const [action, setAction] = useState('')
  const [tier, setTier] = useState('')
  const params = new URLSearchParams()
  if (action) params.set('action', action)
  if (tier) params.set('tier', tier)

  const { data, loading, refetch } = useApi<{ data: TraceEntry[] }>(`/api/traces?${params}`)

  return (
    <div>
      <h1>Traces</h1>
      <div style={{ marginBottom: 16, display: 'flex', gap: 8 }}>
        <input
          data-testid="traces-filter-action"
          placeholder="Filter by action..."
          value={action}
          onChange={(e) => setAction(e.target.value)}
        />
        <select data-testid="traces-filter-tier" value={tier} onChange={(e) => setTier(e.target.value)}>
          <option value="">All tiers</option>
          <option value="allow">Allow</option>
          <option value="intervene">Intervene</option>
          <option value="block">Block</option>
        </select>
        <button data-testid="traces-filter-submit" onClick={refetch}>Filter</button>
      </div>

      {loading && <p>Loading...</p>}
      {data && data.data.length === 0 && <div data-testid="empty-state">No traces found</div>}
      {data && data.data.length > 0 && (
        <table data-testid="traces-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th>Time</th>
              <th>Action</th>
              <th>Score</th>
              <th>Tier</th>
              <th>Duration</th>
              <th>Signals</th>
            </tr>
          </thead>
          <tbody>
            {data.data.map((t) => (
              <tr key={t.trace_id} data-testid={`traces-row-${t.trace_id}`}>
                <td>{new Date(t.timestamp).toLocaleTimeString()}</td>
                <td>{t.action}</td>
                <td>{t.risk_score}</td>
                <td style={{ color: TIER_COLORS[t.tier] }}>{t.tier}</td>
                <td>{t.duration_ms.toFixed(0)}ms</td>
                <td>{t.signals.length}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
