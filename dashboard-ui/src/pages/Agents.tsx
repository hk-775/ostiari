import { useApi } from '../hooks/useApi'
import type { Agent } from '../types'

export default function Agents() {
  const { data: agents, loading } = useApi<Agent[]>('/api/agents')

  if (loading) return <p>Loading...</p>
  if (!agents || agents.length === 0) return <div data-testid="empty-state">No agents connected</div>

  return (
    <div>
      <h1>Agents</h1>
      <table data-testid="agents-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th>Agent ID</th>
            <th>First Seen</th>
            <th>Last Seen</th>
            <th>Total Actions</th>
          </tr>
        </thead>
        <tbody>
          {agents.map((a) => (
            <tr key={a.id} data-testid={`agent-row-${a.id}`}>
              <td>{a.id}</td>
              <td>{new Date(a.first_seen).toLocaleString()}</td>
              <td>{new Date(a.last_seen).toLocaleString()}</td>
              <td>{a.total}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
