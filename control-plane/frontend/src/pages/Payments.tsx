import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Wallet as WalletIcon, Coins, Ban, Play, Pause, Upload, Plus, Check, AlertTriangle } from "lucide-react";
import { api } from "../lib/api";

const fmt = (n: number) => `$${n.toFixed(4)}`;

export function Payments() {
  const qc = useQueryClient();
  const { data: wallets } = useQuery({ queryKey: ["pay-wallets"], queryFn: api.payments.wallets, refetchInterval: 5000 });
  const { data: summary } = useQuery({ queryKey: ["pay-summary"], queryFn: api.payments.summary, refetchInterval: 5000 });
  const { data: ledger } = useQuery({ queryKey: ["pay-ledger"], queryFn: () => api.payments.ledger(), refetchInterval: 5000 });
  const { data: pricing } = useQuery({ queryKey: ["pay-pricing"], queryFn: () => api.payments.pricing() });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["pay-wallets"] });
    qc.invalidateQueries({ queryKey: ["pay-summary"] });
    qc.invalidateQueries({ queryKey: ["pay-ledger"] });
  };

  const fund = useMutation({
    mutationFn: ({ agent, amount }: { agent: string; amount: number }) => api.payments.fund(agent, amount),
    onSuccess: invalidate,
  });
  const toggle = useMutation({
    mutationFn: ({ agent, status }: { agent: string; status: "active" | "paused" }) =>
      api.payments.patchWallet(agent, { status }),
    onSuccess: invalidate,
  });
  const push = useMutation({ mutationFn: () => api.payments.push(), onSuccess: invalidate });
  const upsert = useMutation({
    mutationFn: (data: { agent_id: string; balance_usdc: number }) => api.payments.upsert(data),
    onSuccess: () => { invalidate(); setNewAgent(""); setNewBalance(1.0); },
  });
  const setLimit = useMutation({
    mutationFn: ({ agent, field, value }: { agent: string; field: "daily_limit_usdc" | "per_call_limit_usdc"; value: number | null }) =>
      api.payments.patchWallet(agent, { [field]: value }),
    onSuccess: invalidate,
  });

  const [newAgent, setNewAgent] = useState("");
  const [newBalance, setNewBalance] = useState(1.0);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-stone-900">
            <WalletIcon className="h-6 w-6 text-emerald-500" /> Payments
          </h1>
          <p className="mt-1 text-sm text-stone-500">
            x402 pay-per-tool-call. Agents pay in USDC from a wallet you control — within limits you set.
            {pricing && <> Mode: <span className="font-mono text-stone-700">{pricing.mode}</span>.</>}
          </p>
        </div>
        <button onClick={() => push.mutate()} disabled={push.isPending} className="btn-sky">
          <Upload className="h-4 w-4" /> {push.isPending ? "Pushing…" : "Push to gateway"}
        </button>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-4 gap-4">
        <div className="card p-5">
          <p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Total settled</p>
          <p className="mt-2 text-3xl font-bold text-stone-900">{fmt(summary?.total_settled_usdc ?? 0)}</p>
          <p className="mt-1 text-xs text-stone-400">{summary?.settled_count ?? 0} paid calls</p>
        </div>
        <div className="card p-5">
          <p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Fees captured</p>
          <p className="mt-2 text-3xl font-bold text-emerald-600">{fmt(summary?.fees_captured_usdc ?? 0)}</p>
          <p className="mt-1 text-xs text-stone-400">at {((summary?.fee_rate ?? 0) * 100).toFixed(0)}% per txn</p>
        </div>
        <div className="card p-5">
          <p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Not settled</p>
          <p className="mt-2 text-3xl font-bold text-rose-600">{summary?.blocked_count ?? 0}</p>
          <p className="mt-1 text-xs text-stone-400">blocked or unconfirmed</p>
        </div>
        <div className="card p-5">
          <p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Funded wallets</p>
          <p className="mt-2 text-3xl font-bold text-stone-900">{wallets?.length ?? 0}</p>
          <p className="mt-1 text-xs text-stone-400">
            {wallets?.filter((w) => w.status === "paused").length ?? 0} paused
          </p>
        </div>
      </div>

      {/* Wallets */}
      <div className="card overflow-hidden">
        <div className="border-b border-stone-100 px-6 py-4">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-stone-800">
            <Coins className="h-4 w-4 text-amber-500" /> Agent wallets
          </h2>
        </div>
        <table className="w-full">
          <thead>
            <tr className="border-b border-stone-100 text-left text-xs font-semibold uppercase tracking-wider text-stone-500">
              <th className="px-6 py-3.5">Agent</th>
              <th className="px-6 py-3.5">Balance</th>
              <th className="px-6 py-3.5">Spent today</th>
              <th className="px-6 py-3.5">Daily limit</th>
              <th className="px-6 py-3.5">Per-call</th>
              <th className="px-6 py-3.5">Status</th>
              <th className="px-6 py-3.5 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-50">
            {(wallets ?? []).map((w) => (
              <tr key={w.agent_id} className="transition hover:bg-stone-50/50">
                <td className="px-6 py-4 text-sm font-medium text-stone-800">{w.agent_id}</td>
                <td className="px-6 py-4 font-mono text-sm text-stone-800">{fmt(w.balance_usdc)}</td>
                <td className="px-6 py-4 font-mono text-xs text-stone-500">{fmt(w.spent_today_usdc)}</td>
                <td className="px-6 py-4 text-sm text-stone-500">
                  <LimitInput
                    value={w.daily_limit_usdc}
                    onCommit={(v) => setLimit.mutate({ agent: w.agent_id, field: "daily_limit_usdc", value: v })}
                  />
                </td>
                <td className="px-6 py-4 text-sm text-stone-500">
                  <LimitInput
                    value={w.per_call_limit_usdc}
                    onCommit={(v) => setLimit.mutate({ agent: w.agent_id, field: "per_call_limit_usdc", value: v })}
                  />
                </td>
                <td className="px-6 py-4">
                  <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                    w.status === "active" ? "bg-emerald-50 text-emerald-700" : "bg-rose-50 text-rose-700"
                  }`}>{w.status}</span>
                </td>
                <td className="px-6 py-4">
                  <div className="flex items-center justify-end gap-2">
                    <button
                      onClick={() => fund.mutate({ agent: w.agent_id, amount: 1.0 })}
                      className="rounded-md border border-stone-200 px-2 py-1 text-xs text-stone-600 hover:bg-stone-50"
                      title="Fund $1.00"
                    >+ $1</button>
                    <button
                      onClick={() => toggle.mutate({ agent: w.agent_id, status: w.status === "active" ? "paused" : "active" })}
                      className="rounded-md border border-stone-200 px-2 py-1 text-xs text-stone-600 hover:bg-stone-50"
                      title={w.status === "active" ? "Pause" : "Resume"}
                    >{w.status === "active" ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}</button>
                  </div>
                </td>
              </tr>
            ))}
            {/* Create wallet */}
            <tr className="bg-stone-50/40">
              <td className="px-6 py-3">
                <input
                  placeholder="new-agent-id" value={newAgent}
                  onChange={(e) => setNewAgent(e.target.value)}
                  className="w-40 rounded border border-stone-200 px-2 py-1 text-sm"
                />
              </td>
              <td className="px-6 py-3" colSpan={5}>
                <span className="text-stone-400">$</span>
                <input
                  type="number" min={0} step={0.01} value={newBalance}
                  onChange={(e) => setNewBalance(Number(e.target.value))}
                  className="ml-1 w-28 rounded border border-stone-200 px-2 py-1 text-sm"
                  title="Initial balance (USDC)"
                />
              </td>
              <td className="px-6 py-3 text-right">
                <button
                  onClick={() => newAgent.trim() && upsert.mutate({ agent_id: newAgent.trim(), balance_usdc: newBalance })}
                  disabled={!newAgent.trim() || upsert.isPending}
                  className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-40"
                >
                  <Plus className="h-3.5 w-3.5" /> New wallet
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      {/* Ledger */}
      <div className="card overflow-hidden">
        <div className="border-b border-stone-100 px-6 py-4">
          <h2 className="text-sm font-semibold text-stone-800">Transaction ledger</h2>
        </div>
        <table className="w-full">
          <thead>
            <tr className="border-b border-stone-100 text-left text-xs font-semibold uppercase tracking-wider text-stone-500">
              <th className="px-6 py-3.5">Agent</th>
              <th className="px-6 py-3.5">Action</th>
              <th className="px-6 py-3.5">Amount</th>
              <th className="px-6 py-3.5">Tx</th>
              <th className="px-6 py-3.5">Rail</th>
              <th className="px-6 py-3.5">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-50">
            {(ledger ?? []).map((p) => (
              <tr key={p.id} className="transition hover:bg-stone-50/50">
                <td className="px-6 py-4 text-sm font-medium text-stone-800">{p.agent_id}</td>
                <td className="px-6 py-4 font-mono text-xs text-stone-600">{p.action}</td>
                <td className="px-6 py-4 font-mono text-sm text-stone-800">{fmt(p.amount_usdc)}</td>
                <td className="px-6 py-4 font-mono text-xs text-stone-400">{p.tx_hash || "—"}</td>
                <td className="px-6 py-4 font-mono text-xs text-stone-500">{p.mode}</td>
                <td className="px-6 py-4">
                  <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${
                    p.settled
                      ? "bg-emerald-50 text-emerald-700"
                      : p.wallet_debited
                        ? "bg-amber-50 text-amber-700"
                        : "bg-rose-50 text-rose-700"
                  }`} title={p.reason || undefined}>
                    {p.settled
                      ? "settled"
                      : p.wallet_debited
                        ? <><AlertTriangle className="h-3 w-3" /> unconfirmed</>
                        : <><Ban className="h-3 w-3" /> blocked</>}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {(ledger?.length ?? 0) === 0 && (
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <WalletIcon className="h-7 w-7 text-stone-300" />
            <p className="text-sm text-stone-500">No transactions yet.</p>
          </div>
        )}
      </div>
    </div>
  );
}

/** Inline-editable USD limit cell — commits on blur/Enter; blank clears the limit. */
function LimitInput({ value, onCommit }: { value: number | null; onCommit: (v: number | null) => void }) {
  const [text, setText] = useState(value != null ? String(value) : "");
  const [dirty, setDirty] = useState(false);
  const commit = () => {
    if (!dirty) return;
    const v = text.trim() === "" ? null : Number(text);
    onCommit(Number.isNaN(v as number) ? null : v);
    setDirty(false);
  };
  return (
    <div className="flex items-center gap-1">
      <span className="text-stone-300">$</span>
      <input
        type="number" min={0} step={0.01} placeholder="—"
        value={text}
        onChange={(e) => { setText(e.target.value); setDirty(true); }}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
        className="w-20 rounded border border-stone-200 px-1.5 py-0.5 text-sm"
      />
      {dirty && <Check className="h-3 w-3 text-emerald-500" />}
    </div>
  );
}
