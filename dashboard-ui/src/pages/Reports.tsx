import { useState } from 'react'
import { useApi } from '../hooks/useApi'

export default function Reports() {
  const [period, setPeriod] = useState(7)
  const [format, setFormat] = useState<'json' | 'csv'>('json')
  const { data: preview, loading, refetch } = useApi<Record<string, unknown>>(
    `/api/report?period=${period}&format=json`
  )

  const handleDownload = () => {
    window.open(`/api/report?period=${period}&format=${format}`, '_blank')
  }

  return (
    <div>
      <h1>Reports</h1>
      <div style={{ marginBottom: 16, display: 'flex', gap: 12, alignItems: 'center' }}>
        <label>
          Period (days):
          <input
            data-testid="report-period"
            type="number"
            min={1}
            max={30}
            value={period}
            onChange={(e) => setPeriod(Number(e.target.value))}
            style={{ width: 60, marginLeft: 8 }}
          />
        </label>
        <label>
          Format:
          <select data-testid="report-format" value={format} onChange={(e) => setFormat(e.target.value as 'json' | 'csv')} style={{ marginLeft: 8 }}>
            <option value="json">JSON</option>
            <option value="csv">CSV</option>
          </select>
        </label>
        <button data-testid="report-generate-button" onClick={refetch}>Generate</button>
        <button data-testid="report-download-button" onClick={handleDownload}>Download</button>
      </div>

      {loading && <p>Generating...</p>}
      {preview && (
        <pre data-testid="report-preview" style={{ background: '#f5f5f5', padding: 16, borderRadius: 4, overflow: 'auto', maxHeight: 400 }}>
          {JSON.stringify(preview, null, 2)}
        </pre>
      )}
    </div>
  )
}
