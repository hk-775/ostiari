import { useCallback, useEffect, useRef, useState } from 'react'
import type { WsMessage } from '../types'

export function useWebSocket(url: string) {
  const [connected, setConnected] = useState(false)
  const [messages, setMessages] = useState<WsMessage[]>([])
  const backoff = useRef(1000)
  const wsRef = useRef<WebSocket | null>(null)

  const connect = useCallback(() => {
    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onopen = () => {
      setConnected(true)
      backoff.current = 1000
    }

    ws.onclose = () => {
      setConnected(false)
      wsRef.current = null
      const delay = Math.min(backoff.current, 30000)
      backoff.current *= 2
      setTimeout(connect, delay)
    }

    ws.onmessage = (event) => {
      try {
        const msg: WsMessage = JSON.parse(event.data)
        setMessages((prev) => [...prev.slice(-99), msg])
      } catch {
        // ignore malformed messages
      }
    }
  }, [url])

  useEffect(() => {
    connect()
    return () => {
      wsRef.current?.close()
    }
  }, [connect])

  return { connected, messages }
}
