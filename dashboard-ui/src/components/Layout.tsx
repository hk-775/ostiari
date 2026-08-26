import { Link, useLocation } from 'react-router'
import type { ReactNode } from 'react'
import ConnectionStatus from './ConnectionStatus'

const NAV_ITEMS = [
  { path: '/', label: 'Dashboard' },
  { path: '/traces', label: 'Traces' },
  { path: '/breakers', label: 'Breakers' },
  { path: '/agents', label: 'Agents' },
  { path: '/reports', label: 'Reports' },
]

export default function Layout({ children }: { children: ReactNode }) {
  const location = useLocation()

  return (
    <div style={{ display: 'flex', height: '100vh' }}>
      <nav style={{ width: 200, padding: 16, borderRight: '1px solid #ddd' }}>
        <h2 style={{ fontSize: 16, marginBottom: 16 }}>AgentGuard</h2>
        <ConnectionStatus />
        <ul style={{ listStyle: 'none', padding: 0 }}>
          {NAV_ITEMS.map((item) => (
            <li key={item.path} style={{ marginBottom: 8 }}>
              <Link
                to={item.path}
                data-testid={`nav-${item.label.toLowerCase()}`}
                style={{
                  fontWeight: location.pathname === item.path ? 'bold' : 'normal',
                  textDecoration: 'none',
                  color: 'inherit',
                }}
              >
                {item.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      <main style={{ flex: 1, padding: 24, overflow: 'auto' }}>{children}</main>
    </div>
  )
}
