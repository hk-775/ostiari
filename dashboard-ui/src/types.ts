export interface TraceEntry {
  trace_id: string;
  correlation_id: string | null;
  timestamp: string;
  action: string;
  params: Record<string, unknown>;
  result: unknown;
  risk_score: number;
  tier: 'allow' | 'intervene' | 'block';
  duration_ms: number;
  signals: RiskSignal[];
  anomalies: AnomalySignal[];
  breaker_state: string | null;
  metadata: Record<string, unknown>;
}

export interface RiskSignal {
  source: string;
  score_contribution: number;
  description: string;
  metadata: Record<string, unknown>;
}

export interface AnomalySignal {
  detector: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  score_contribution: number;
  description: string;
  evidence: Record<string, unknown>;
}

export interface BreakerState {
  breaker_id: string;
  state: 'closed' | 'open' | 'half_open';
  tripped_at: string | null;
  last_checked: string;
  metrics: Record<string, number>;
  recovery_mode: 'auto_retry' | 'notify' | 'terminate';
  recovery_after_seconds: number | null;
}

export interface Stats {
  total_actions: number;
  allowed: number;
  blocked: number;
  intervened: number;
  avg_risk: number;
  unique_agents: number;
}

export interface TimeseriesBucket {
  timestamp: string;
  total: number;
  allowed: number;
  blocked: number;
  avg_risk: number;
}

export interface Agent {
  id: string;
  first_seen: string;
  last_seen: string;
  total: number;
}

export interface InterventionRequest {
  request_id: string;
  action: string;
  risk_score: number;
  question: string;
  timeout: number;
}

export type WsMessage =
  | { type: 'trace'; data: TraceEntry }
  | { type: 'intervention'; data: InterventionRequest }
  | { type: 'intervention_resolved'; data: { request_id: string; approved: boolean } };
