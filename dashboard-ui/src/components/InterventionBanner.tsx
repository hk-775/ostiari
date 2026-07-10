import { useState } from 'react'
import type { InterventionRequest } from '../types'

interface Props {
  intervention: InterventionRequest
  onDismiss: () => void
}

export default function InterventionBanner({ intervention, onDismiss }: Props) {
  const [responding, setResponding] = useState(false)

  const respond = async (approved: boolean) => {
    setResponding(true)
    try {
      await fetch(`/api/intervention/${intervention.request_id}/respond`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approved }),
      })
    } catch {
      // ignore errors
    }
    onDismiss()
  }

  return (
    <div
      data-testid="intervention-banner"
      style={{
        padding: 16,
        backgroundColor: '#fef3cd',
        border: '1px solid #ffc107',
        borderRadius: 4,
        marginBottom: 16,
      }}
    >
      <strong>Intervention Required</strong>
      <p>Action: {intervention.action} (risk: {intervention.risk_score})</p>
      <p>{intervention.question}</p>
      <button
        data-testid="intervention-allow"
        onClick={() => respond(true)}
        disabled={responding}
        style={{ marginRight: 8 }}
      >
        Allow
      </button>
      <button
        data-testid="intervention-deny"
        onClick={() => respond(false)}
        disabled={responding}
      >
        Deny
      </button>
    </div>
  )
}
