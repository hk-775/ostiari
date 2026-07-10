import { useWebSocket } from '../hooks/useWebSocket'

const WS_URL = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/live`

export default function ConnectionStatus() {
  const { connected } = useWebSocket(WS_URL)

  return (
    <div data-testid="ws-status" style={{ marginBottom: 16, fontSize: 12 }}>
      <span
        style={{
          display: 'inline-block',
          width: 8,
          height: 8,
          borderRadius: '50%',
          backgroundColor: connected ? '#22c55e' : '#ef4444',
          marginRight: 6,
        }}
      />
      {connected ? 'Connected' : 'Disconnected'}
    </div>
  )
}
