import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { UserCheck, Check, X, Clock } from "lucide-react";
import { api, type ApprovalItem } from "../lib/api";

export function Approvals() {
  const qc = useQueryClient();
  const { data: pending } = useQuery({
    queryKey: ["approvals-pending"], queryFn: api.approvals.list, refetchInterval: 3000,
  });
  const { data: history } = useQuery({
    queryKey: ["approvals-all"], queryFn: api.approvals.all, refetchInterval: 5000,
  });

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "deny" }) =>
      api.approvals.decide(id, decision),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["approvals-pending"] });
      qc.invalidateQueries({ queryKey: ["approvals-all"] });
    },
  });

  const decided = (history ?? []).filter((a) => a.status !== "pending").slice(0, 20);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-stone-900">
          <UserCheck className="h-6 w-6 text-amber-500" /> Approvals
        </h1>
        <p className="mt-1 text-sm text-stone-500">
          Medium-risk actions the gateway paused for human review (the
          <span className="font-medium"> intervene</span> tier). Approve to let the call run;
          deny to block it. Every decision is recorded.
        </p>
      </div>

      {/* Pending queue */}
      <div className="card overflow-hidden">
        <div className="flex items-center justify-between border-b border-stone-100 px-6 py-4">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-stone-800">
            <Clock className="h-4 w-4 text-amber-500" /> Pending
          </h2>
          <span className="text-xs text-stone-500">{pending?.length ?? 0} awaiting review</span>
        </div>
        <table className="w-full">
          <thead>
            <tr className="border-b border-stone-100 text-left text-xs font-semibold uppercase tracking-wider text-stone-500">
              <th className="px-6 py-3.5">Agent</th>
              <th className="px-6 py-3.5">Action</th>
              <th className="px-6 py-3.5">Score</th>
              <th className="px-6 py-3.5">Why</th>
              <th className="px-6 py-3.5 text-right">Decision</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-50">
            {(pending ?? []).map((a: ApprovalItem) => (
              <tr key={a.id} className="align-top transition hover:bg-stone-50/50">
                <td className="px-6 py-4 text-sm font-medium text-stone-800">{a.agent_id}</td>
                <td className="px-6 py-4">
                  <div className="font-mono text-sm text-stone-800">{a.action}</div>
                  <div className="mt-0.5 max-w-md truncate font-mono text-xs text-stone-400">
                    {JSON.stringify(a.params)}
                  </div>
                </td>
                <td className="px-6 py-4">
                  <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-semibold text-amber-700">{a.score}</span>
                </td>
                <td className="px-6 py-4 text-xs text-stone-500">{a.reason}</td>
                <td className="px-6 py-4">
                  <div className="flex items-center justify-end gap-2">
                    <button
                      onClick={() => decide.mutate({ id: a.id, decision: "approve" })}
                      disabled={decide.isPending}
                      className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-40"
                    ><Check className="h-3.5 w-3.5" /> Approve</button>
                    <button
                      onClick={() => decide.mutate({ id: a.id, decision: "deny" })}
                      disabled={decide.isPending}
                      className="inline-flex items-center gap-1 rounded-md bg-rose-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-rose-700 disabled:opacity-40"
                    ><X className="h-3.5 w-3.5" /> Deny</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {(pending?.length ?? 0) === 0 && (
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <UserCheck className="h-7 w-7 text-stone-300" />
            <p className="text-sm text-stone-500">Nothing awaiting approval.</p>
          </div>
        )}
      </div>

      {/* Decision history (audit) */}
      {decided.length > 0 && (
        <div className="card overflow-hidden">
          <div className="border-b border-stone-100 px-6 py-4">
            <h2 className="text-sm font-semibold text-stone-800">Recent decisions</h2>
          </div>
          <table className="w-full">
            <tbody className="divide-y divide-stone-50">
              {decided.map((a) => (
                <tr key={a.id} className="text-sm">
                  <td className="px-6 py-3 font-medium text-stone-800">{a.agent_id}</td>
                  <td className="px-6 py-3 font-mono text-xs text-stone-600">{a.action}</td>
                  <td className="px-6 py-3">
                    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                      a.status === "approved" ? "bg-emerald-50 text-emerald-700" : "bg-rose-50 text-rose-700"
                    }`}>{a.status}</span>
                  </td>
                  <td className="px-6 py-3 text-xs text-stone-400">by {a.decided_by || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
