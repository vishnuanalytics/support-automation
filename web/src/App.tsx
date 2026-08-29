import { useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "./supabase";
import { Login } from "./auth/Login";
import { FlowList } from "./flows/FlowList";
import { FlowEditor } from "./flows/FlowEditor";

export function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [flowId, setFlowId] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setSession(data.session));
    const { data: sub } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => sub.subscription.unsubscribe();
  }, []);

  if (session === undefined) return <div style={{ padding: 20 }}>…</div>;
  if (session === null) return <Login />;

  return (
    <div className="shell">
      <div className="sidebar col">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <strong>Flows</strong>
          <button onClick={() => supabase.auth.signOut()} title={session.user.email ?? ""}>
            sign out
          </button>
        </div>
        <FlowList
          key={reloadKey}
          activeId={flowId}
          onSelect={setFlowId}
          onCreated={(id) => {
            setReloadKey((k) => k + 1);
            setFlowId(id);
          }}
        />
      </div>
      <div className="editor">
        {flowId ? (
          <FlowEditor
            key={flowId}
            flowId={flowId}
            onSaved={() => setReloadKey((k) => k + 1)}
            onDeleted={() => {
              setFlowId(null);
              setReloadKey((k) => k + 1);
            }}
          />
        ) : (
          <div style={{ display: "grid", placeItems: "center" }} className="muted">
            select or create a flow
          </div>
        )}
      </div>
    </div>
  );
}
