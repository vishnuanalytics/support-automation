/**
 * "How to use this editor" — a condensed, in-context how-to shown from the
 * editor's own toolbar (❓ Help), so the answer to "how do I do X here" is
 * one click away instead of a search through an external doc. Deliberately
 * short per section — this is a quick-reference, not the full manual; see
 * docs/DASHBOARD_GUIDE.md for the complete walkthrough (onboarding, every
 * tab, troubleshooting) shared with new teammates.
 *
 * Static content, same reasoning as FlowGuideView.tsx: no backend endpoint
 * to keep in sync, just prose that follows the app's own light/dark tokens.
 */
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 22 }}>
      <div style={{ font: "600 13px/1.4 var(--font-body)", marginBottom: 8 }}>{title}</div>
      <div style={{ fontSize: 12.5, lineHeight: 1.6, color: "var(--text)" }}>{children}</div>
    </div>
  );
}

function Item({ children }: { children: React.ReactNode }) {
  return <li style={{ marginBottom: 8 }}>{children}</li>;
}

export function EditorHelp() {
  return (
    <div className="col" style={{ gap: 0 }}>
      <p className="muted" style={{ fontSize: 12, marginTop: 0, marginBottom: 20 }}>
        A quick reference for this screen. For the full walkthrough — connecting a
        case system, publishing, monitoring runs, and a worked example for every
        node and edge type — see the written guide (link at the bottom).
      </p>

      <Section title="Three ways to build a flow">
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          <Item>
            <strong>💬 Chat</strong> — describe what you want in plain English (e.g.
            "add a step that checks the customer's tier before replying"); the AI
            edits your draft. Switch to <strong>🗺️ Graph</strong> to review what changed.
          </Item>
          <Item>
            <strong>🗺️ Graph</strong> — the visual canvas. "+ Add node" drops a new
            step; drag from a node's edge handle to another node to connect them.
          </Item>
          <Item>
            Faster starts, from the flow list (before opening a specific flow):{" "}
            <strong>🧭 Set up a flow</strong> (answer a few questions, no blank canvas),{" "}
            <strong>📋 From template</strong>, <strong>✨ From prompt</strong> (one sentence,
            AI builds the whole thing), or <strong>⬇ From Mermaid</strong> (paste a
            flowchart diagram).
          </Item>
        </ul>
      </Section>

      <Section title="Reading the canvas">
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          <Item>
            A node's border color means something — blue is where the flow starts,
            amber is where a path ends, red means it's invalid and won't build. The
            legend under the canvas is always visible as a reminder.
          </Item>
          <Item>
            Click a node to configure it and give it a name you'll recognize later
            (its type, like <code>confidence_gate</code>, stays visible above the name).
          </Item>
          <Item>
            Click an edge (the line between two nodes) to decide when that path is
            taken.
          </Item>
        </ul>
      </Section>

      <Section title="Conditions, without writing code">
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          <Item>Leave "conditional" unchecked and that path is always taken.</Item>
          <Item>
            Check it, then pick a field (like Tier or Case channel), an operator,
            and a value — add more rows to require several things at once.
          </Item>
          <Item>
            "Advanced" switches to typing a raw expression, for anything the picker
            can't express (or/not/nested logic).
          </Item>
          <Item>
            Give the edge its own <strong>name</strong> (e.g. "VIP customers") in its
            side panel — the canvas shows that name instead of the raw condition, so
            you don't have to read code to understand your own flow later. Hover the
            pill on the canvas to see the real condition underneath.
          </Item>
          <Item>
            The toolbar's <strong>Conditions</strong> button lists every branching edge
            in the flow in one place, instead of clicking through each one.
          </Item>
        </ul>
      </Section>

      <Section title="Test before you publish">
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          <Item>
            <strong>Test run</strong> runs one real case through your current draft
            and shows exactly what happened at each step — no guessing.
          </Item>
          <Item>
            <strong>Validate</strong> checks the flow's structure (no orphan nodes,
            no cycles, exactly one entry point) without actually running anything.
          </Item>
        </ul>
      </Section>

      <Section title="Saving, publishing, undoing">
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          <Item>
            <strong>Save draft</strong> stores your edits — nothing customers see
            changes yet.
          </Item>
          <Item>
            <strong>Publish</strong> makes this version the one that actually runs
            for real cases.
          </Item>
          <Item>
            Made a mistake after publishing? <strong>More ▾</strong> has a rollback to
            an earlier published version.
          </Item>
        </ul>
      </Section>

      <Section title="After you publish, check">
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          <Item><strong>Runs</strong> — every time this flow executed, and what it decided.</Item>
          <Item><strong>Trace</strong> — the step-by-step detail of one specific run.</Item>
          <Item><strong>Approvals</strong> — anything waiting on a person (Slack/task approvals).</Item>
          <Item><strong>Activity</strong> — a feed across the whole workspace.</Item>
        </ul>
      </Section>

      <div className="muted" style={{ fontSize: 11.5, borderTop: "1px solid var(--border)", paddingTop: 14 }}>
        Full written guide (onboarding, connecting a case system, every tab in the
        dashboard): <code>docs/DASHBOARD_GUIDE.md</code> in the repo.
      </div>
    </div>
  );
}
