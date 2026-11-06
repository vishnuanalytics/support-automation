export type Source = { label: string; url?: string | null };
export type Ball = "us" | "engineering" | "third_party" | "ai" | "customer" | "unknown";
export type Level = "critical" | "high" | "medium" | "low";

export type Stage = "act" | "raise" | "engineering" | "third_party" | "customer" | "ai" | "check";
export type QueueCase = {
  id: string;
  stage: Stage;
  signal: { code: string; text: string; source: Source } | null;
  latest: { who: string | null; ours: boolean; at: string; text: string | null; url: string | null; source: string; party?: string } | null;
  overdue_hours: number;
  mine: boolean;
  links: { kind: string; url: string }[];
  related: { id: string; case_number: string | null; record_type: string | null; status: string | null; url: string | null }[];
  source: string;
  case_number: string | null;
  subject: string | null;
  priority: string | null;
  owner: string | null;
  record_type: string | null;
  waiting_raw: string | null;
  sub_status: string | null;
  bu: string | null;
  module: string | null;
  sub_module: string | null;
  link_kinds: string[];
  link_from_post: boolean;
  partner_channel: string | null;
  not_raised: boolean;
  waiting_party: "engineering" | "third_party" | null;
  third_parties: string[];
  url: string | null;
  ball: Ball;
  mismatch: boolean;
  score: number;
  level: Level;
  next_action: string;
  missing_link: boolean;
  overdue: boolean;
  stale_hours: number | null;
  duplicate_of: string | null;
  ai_state: string | null;
  hidden: boolean;
  snooze_until: string | null;
};

export type Tiles = {
  total: number;
  my_turn: number;
  mismatches: number;
  missing_link: number;
  not_raised: number;
  overdue: number;
  critical: number;
};

export type SyncRow = {
  status: string;
  trigger: string;
  started_at: string;
  finished_at: string | null;
  counts: Partial<Tiles>;
  errors: { case_id?: string; case_number?: string; url?: string; error: string }[];
};

export type Queue = {
  cases: QueueCase[];
  labels: { third_party: string; customer: string };
  has_lead: boolean;
  focus_partners: string[];
  tiles: Tiles;
  trend: { at: string; my_turn: number }[];
  last_sync: SyncRow | null;
  running: boolean;
  duplicate_groups: Record<string, string[]>;
};

export type Reason = { code: string; text: string; points: number; source: Source };
export type TimelineItem = {
  at: string | null;
  source: string;
  party: string;
  party_label: string;
  who: string | null;
  text: string;
  url: string | null;
  subject?: string | null;
};
export type Evidence = {
  kind: string;
  url: string;
  label?: string;
  ok: boolean;
  error?: string | null;
  note?: string;
  state?: string;
  title?: string;
  labels?: string[];
  messages?: { who: string; ours: boolean; at: string; text: string; url?: string }[];
};
export type Fact = { label: string; value: string; source: Source };
export type Draft = {
  id: string;
  title: string;
  text: string;
  review_required?: boolean;
  to?: string[];
  cc?: string[];
  facts: Fact[];
};
export type Related = {
  id: string;
  case_number?: string | null;
  status?: string | null;
  record_type?: string | null;
  url?: string | null;
};
export type Partner = {
  name: string;
  channel: string;
  to_emails: string[];
  cc_emails: string[];
  contacts: string[];
  portal_url: string | null;
  escalation_note: string | null;
  notes: string | null;
};
export type CaseDetail = {
  source: string;
  partner: Partner | null;
  case: {
    id: string;
    case_number: string | null;
    subject: string | null;
    url: string | null;
    parent: Related | null;
    children: Related[];
    status?: string | null;
    record_type?: string | null;
    brand?: string | null;
    biz_id?: string | null;
    product?: string | null;
    brand_stats?: { open?: number; last_30_days?: number } | null;
  };
  verdict: {
    ball: Ball;
    mismatch: boolean;
    score: number;
    level: Level;
    reasons: Reason[];
    next_action: string;
  };
  brief: {
    problem: { text: string; source: Source };
    facts: Fact[];
    done_so_far: { text: string; at: string | null; who: string | null; source: Source }[];
    ball: { text: string; mismatch: boolean };
    waiting: { text: string; since: string | null; source: Source } | null;
    next_action: string;
  };
  timeline: TimelineItem[];
  timeline_at: string | null;
  evidence: Evidence[];
  drafts: Draft[];
  similar: {
    id: string;
    case_number: string | null;
    subject: string | null;
    match_count: number;
    hours_to_resolve: number | null;
    resolution_note: string | null;
    url: string | null;
  }[];
  duplicates: { id: string; case_number: string | null; subject: string | null; owner: string | null }[];
  slack_threads: { url: string; channel: string; thread_ts: string }[];
  slack_team_mention: string;
  labels: { third_party: string; customer: string };
  llm_enabled: boolean;
};
export type Digest = {
  since: string | null;
  events: { id: number; case_id: string; case_number: string | null; subject: string | null; kind: string }[];
};
export type SlackStatus = { configured: boolean; connected: boolean; leads_connected: number };
export type AiDraft = {
  enabled: boolean;
  situation?: string;
  draft?: string;
  missing?: string[];
  sources?: { n: number; source: string; url: string | null }[];
  similar_used?: number;
};
export type Summary = {
  enabled: boolean;
  text?: string;
  sources?: { n: number; source: string; url: string | null }[];
};
