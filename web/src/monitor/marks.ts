import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { EscalationMark } from "./types";

export type MarkAction = "escalate" | "responded" | "reopen";
export type Marks = {
  marks: Map<string, EscalationMark>;
  err: string | null;
  act: (caseId: string, action: MarkAction, note?: string) => Promise<void>;
  clear: (caseId: string) => Promise<void>;
};

/** Cases you escalated and whether their team responded. Case Monitor's own table; Salesforce isn't written. */
export function useEscalationMarks(tenantId?: string): Marks {
  const [marks, setMarks] = useState<Map<string, EscalationMark>>(new Map());
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.monitor.escalationMarks(tenantId)
      .then((r) => setMarks(new Map(r.marks.map((m) => [m.case_id, m]))))
      .catch((e: ApiError) => setErr(e.message));
  }, [tenantId]);
  const act = useCallback(async (caseId: string, action: MarkAction, note?: string) => {
    setErr(null);
    try {
      const saved = await api.monitor.setEscalationMark(caseId, action, note, tenantId);
      setMarks((m) => new Map(m).set(caseId, saved));
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [tenantId]);
  const clear = useCallback(async (caseId: string) => {
    setErr(null);
    try {
      await api.monitor.clearEscalationMark(caseId, tenantId);
      setMarks((m) => { const next = new Map(m); next.delete(caseId); return next; });
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, [tenantId]);
  return { marks, err, act, clear };
}

/** Escalated by you and not yet marked as responded. */
export function isWaiting(m: EscalationMark | undefined): boolean {
  return !!(m?.manual && m.escalated_at && !m.responded_at);
}
