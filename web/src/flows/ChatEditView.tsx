import { useState } from "react";
import { Button } from "../ui";

export type ChatLogEntry = {
  id: string;
  prompt: string;
  ok: boolean;
  note: string;
  list?: string[];
};

/**
 * The default landing view for a flow — describe what you want in plain
 * English instead of starting on the node/edge canvas. Occupies the same
 * `canvas-wrap` slot the graph used to own by default; a toggle in the
 * toolbar (FlowEditor.tsx) switches between this and the graph, and a
 * successful edit offers a direct link to go look at it. The log is
 * genuinely session-only — there's no backend multi-turn chat memory here,
 * this reuses the same one-shot "describe a change, get a diff" endpoint
 * (`api.assistEditFlow`) the old buried "✨ AI edit" overlay called; the log
 * just keeps this session's asks visible instead of throwing each one away
 * after it's applied.
 */
export function ChatEditView({
  canEdit,
  busy,
  err,
  log,
  hasNodes,
  onGenerate,
  onViewGraph,
}: {
  canEdit: boolean;
  busy: boolean;
  err: string | null;
  log: ChatLogEntry[];
  hasNodes: boolean;
  onGenerate: (text: string) => void;
  onViewGraph: () => void;
}) {
  const [text, setText] = useState("");

  function submit() {
    if (!text.trim() || busy) return;
    onGenerate(text.trim());
    setText("");
  }

  return (
    <div className="chat-edit">
      <div className="chat-edit__intro">
        <strong>{hasNodes ? "What would you like to change?" : "What should this flow do?"}</strong>
        <p className="muted" style={{ fontSize: 12.5, margin: "4px 0 0" }}>
          Describe it in plain English — e.g. "add a step that asks for the order number
          before replying". The AI rewrites the graph for you to review; nothing saves until
          you hit Save draft. This history is just for this browser tab, not saved anywhere.
        </p>
      </div>

      <div className="chat-edit__log">
        {log.length === 0 && (
          <div className="muted" style={{ fontSize: 12.5 }}>
            {hasNodes
              ? "Nothing asked yet this session — or switch to the graph to look at what's already there."
              : "This flow has no nodes yet — describe what it should do below to get started."}
          </div>
        )}
        {log.map((entry) => (
          <div key={entry.id} className="chat-edit__entry">
            <div className="chat-edit__prompt">{entry.prompt}</div>
            <div className={`chat-edit__result${entry.ok ? "" : " chat-edit__result--err"}`}>
              {entry.note}
            </div>
            {entry.list && entry.list.length > 0 && (
              <ul className="chat-edit__list">
                {entry.list.map((x, i) => <li key={i}>{x}</li>)}
              </ul>
            )}
            {entry.ok && (
              <button type="button" className="link" style={{ fontSize: 11.5 }} onClick={onViewGraph}>
                View the graph to review »
              </button>
            )}
          </div>
        ))}
      </div>

      {canEdit ? (
        <div className="chat-edit__composer">
          <textarea
            rows={2}
            value={text}
            placeholder="describe what you want…"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
            }}
          />
          <Button variant="primary" onClick={submit} loading={busy} disabled={!text.trim()}>
            {hasNodes ? "Generate" : "Create it"}
          </Button>
        </div>
      ) : (
        <div className="muted" style={{ fontSize: 12 }}>
          view-only — switch to the graph to look around
        </div>
      )}
      {err && <div className="err" style={{ fontSize: 12 }}>{err}</div>}
    </div>
  );
}
