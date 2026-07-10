import { useApi } from '../hooks/useApi'
import type { BreakerState } from '../types'

const STATE_COLORS: Record<string, string> = { closed: '#22c55e', open: '#ef4444', half_open: '#f59e0b' }

export default function Breakers() {
  const { data: breakers, loading } = useApi<BreakerState[]>('/api/breakers')

  if (loading) return <p>Loading...</p>
  if (!breakers || breakers.length === 0) return <div data-testid="empty-state">No breakers configured</div>

  return (
    <div>
      <h1>Circuit Breakers</h1>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 16 }}>
        {breakers.map((b) => (
          <div
            key={b.breaker_id}
            data-testid={`breaker-card-${b.breaker_id}`}
            style={{ padding: 16, border: '1px solid #ddd', borderRadius: 8 }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <span
                style={{
                  width: 12,
                  height: 12,
                  borderRadius: '50%',
                  backgroundColor: STATE_COLORS[b.state] || '#999',
                  display: 'inline-block',
                }}
              />
              <strong>{b.breaker_id}</strong>
            </div>
            <div>State: {b.state}</div>
            <div>Counter: {(b.metrics.counter ?? 0).toFixed(1)}</div>
            <div>Recovery: {b.recovery_mode}</div>
            {b.tripped_at && <div>Tripped: {new Date(b.tripped_at).toLocaleString()}</div>}
          </div>
        ))}
      </div>
    </div>
  )
}
