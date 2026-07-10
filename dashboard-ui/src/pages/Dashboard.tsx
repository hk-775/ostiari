import { useApi } from '../hooks/useApi'
import type { Stats, TimeseriesBucket } from '../types'

export default function Dashboard() {
  const { data: stats, loading } = useApi<Stats>('/api/stats?period=24h')
  const { data: timeseries } = useApi<TimeseriesBucket[]>('/api/stats/timeseries?period=24h&bucket=1h')

  if (loading) return <p>Loading...</p>
  if (!stats) return <div data-testid="empty-state">No data available</div>

  return (
    <div>
      <h1>Dashboard</h1>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16, marginBottom: 24 }}>
        <StatCard label="Total Actions" value={stats.total_actions} />
        <StatCard label="Allowed" value={stats.allowed} color="#22c55e" />
        <StatCard label="Blocked" value={stats.blocked} color="#ef4444" />
        <StatCard label="Avg Risk" value={stats.avg_risk} />
      </div>
      {timeseries && timeseries.length > 0 && (
        <div>
          <h2>Activity (24h)</h2>
          <table data-testid="timeseries-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Total</th>
                <th>Allowed</th>
                <th>Blocked</th>
              </tr>
            </thead>
            <tbody>
              {timeseries.map((b) => (
                <tr key={b.timestamp}>
                  <td>{new Date(b.timestamp).toLocaleTimeString()}</td>
                  <td>{b.total}</td>
                  <td>{b.allowed}</td>
                  <td>{b.blocked}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function StatCard({ label, value, color }: { label: string; value: number; color?: string }) {
  return (
    <div style={{ padding: 16, border: '1px solid #ddd', borderRadius: 8 }}>
      <div style={{ fontSize: 12, color: '#666' }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 'bold', color }}>{typeof value === 'number' && value % 1 !== 0 ? value.toFixed(1) : value}</div>
    </div>
  )
}
