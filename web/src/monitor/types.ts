export type Source = { label: string; url?: string | null };
export type Ball = "us" | "engineering" | "third_party" | "ai" | "customer" | "unknown";
export type Level = "critical" | "high" | "medium" | "low";

export type Stage = "act" | "close" | "raise" | "engineering" | "third_party" | "customer" | "ai" | "check";
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
  ready_to_close?: number;
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
  slack_via_claude: boolean;
  slack_read_at: string | null;
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
export type CaseCheck = {
  id: string;
  check_name: string;
  card_id: string | null;
  params: Record<string, unknown>;
  row_count: number | null;
  summary: string | null;
  saved_by: string | null;
  created_at: string;
  rows: unknown[];
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
  slack_via_claude: boolean;
  labels: { third_party: string; customer: string };
  llm_enabled: boolean;
  checks?: CaseCheck[];
};
export type Digest = {
  since: string | null;
  events: { id: number; case_id: string; case_number: string | null; subject: string | null; kind: string }[];
};
export type SlackStatus = { configured: boolean; connected: boolean; leads_connected: number };
export type AiDraft = {
  enabled: boolean;
  error?: string;
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

export type FollowUpCase = {
  id: string;
  action: "reply" | "raise" | "chase";
  case_number: string | null;
  subject: string | null;
  next_action: string;
  third_parties: string[];
  overdue: boolean;
  overdue_hours: number;
  record_type: string | null;
  latest: QueueCase["latest"];
};
export type FollowUpOwner = {
  owner: string;
  mine: boolean;
  counts: { reply: number; raise: number; chase: number };
  total: number;
  overdue: number;
  cases: FollowUpCase[];
  message: string;
};
export type Escalation = {
  business?: string | null;
  biz_id?: string | null;
  id: string;
  case_number: string | null;
  url: string | null;
  subject: string | null;
  record_type: string | null;
  owner: string | null;
  priority: string | null;
  waiting_on: string[];
  days_silent: number;
  silent_since: string;
  last_update_at: string | null;
  last_update_via: string | null;
  last_ack_at: string | null;
  our_last_at: string | null;
  our_follow_ups: number;
  checked: string[];
  not_read: string[];
  support_cases: string[];
};
export type TechIssue = {
  businesses?: { business: string | null; biz_id: string | null }[];
  squad: string;
  issue: string;
  url: string;
  title: string | null;
  last_reply_by: string | null;
  days_open: number;
  case_id: string | null;
  case_number: string | null;
  other_cases: string[];
  priority: string | null;
  sla_due: string | null;
  sla_status: string | null;
  tech_status: string;
  status: string;
  waiting_on: string | null;
  reason: string;
  over_days: boolean;
};
export type TechPending = {
  days: number;
  total: number;
  unread: number;
  statuses: Record<string, string>;
  squads: { squad: string; issues: TechIssue[] }[];
};
export type LastResponse = QueueCase["latest"];
export type CardCase = {
  case_id: string;
  case_number: string | null;
  subject: string | null;
  owner: string | null;
  latest: LastResponse;
  step?: string | null;
};
export type TodayCard = {
  key: string;
  kind: "help" | "act" | "reply" | "logs" | "chase" | "close" | "follow" | "escalate";
  group: "replies" | "sla" | "agents";
  cases: CardCase[];
  title: string;
  why: string | null;
  meta?: string;
  chip: string;
  tone: "exception" | "warn" | "neutral";
  url?: string;
  url_label?: string;
  case_id?: string | null;
  draft?: string;
  draft_label?: string;
  to?: string[];
  cc?: string[];
  channel?: string | null;
  contacts?: string[];
};
export type CoachAgent = {
  agent: string;
  raised: number;
  wrong: number;
  message: string;
  issues: { title: string | null; url: string; reasons: string[] }[];
};
export type Today = {
  cards: TodayCard[];
  done: number;
  clear: number;
  open_cases: number;
  github: { open: number; need_you: number; with_tech: number };
  dsp: { total: number; breached: number; escalate: number; on_track: number };
  help: { away: { total: number; answered: number }; modules: [string, number][] };
  coaching: {
    days: number;
    closed: number;
    wrong: number;
    share: number;
    reasons: [string, number][];
    agents: CoachAgent[];
  };
};
export type SlaRow = {
  new: boolean;
  business?: string | null;
  biz_id?: string | null;
  case_id: string;
  case_number: string | null;
  subject: string | null;
  owner: string | null;
  partner: string;
  priority: string | null;
  rule: string;
  raised_at: string;
  deadline: string;
  state: "breached" | "escalate" | "on_track";
  hours_left: number;
  last_update_at: string | null;
};
export type SlaReport = {
  since: string;
  sla_start: string | null;
  progress: {
    since: string | null;
    days: number;
    measures: { key: string; label: string; start: number | null; now: number | null; change: "too_early" | "not_judged" | "same" | "better" | "worse" }[];
  };
  partners: { name: string; open: number; open_late: number; old_open: number; old_late: number; closed: number; closed_timed: number; closed_in_time: number }[];
  rules: {
    name: string;
    escalate_hours: number;
    resolve_hours: number;
    goal_resolve_hours?: number;
    closed: number;
    in_time: number;
    share: number;
    ready: boolean;
  }[];
  rows: SlaRow[];
};
export type Escalations = {
  days: number;
  total: number;
  summary: { name: string; cases: number; longest_days: number }[];
  cases: Escalation[];
};
export type FollowUps = {
  owners: FollowUpOwner[];
  total: number;
  counts: { reply: number; raise: number; chase: number };
};
export type Alert = {
  id: number;
  case_id: string;
  case_number: string | null;
  subject: string | null;
  kind: string;
  detail: string | null;
  created_at: string;
  unread: boolean;
};
export type Alerts = { alerts: Alert[]; unread: number };

export type WeeklyIssue = {
  issue: string;
  title: string | null;
  url: string;
  squad: string;
  last_reply_by: string | null;
  last_activity_at: string | null;
  days_open: number;
  case_id: string;
  case_number: string;
  tech_status: string;
  businesses?: { business?: string | null; biz_id?: string | null }[];
};
export type WeeklyDspRow = {
  case_id: string;
  case_number: string;
  subject: string | null;
  business?: string | null;
  biz_id?: string | null;
  quiet_days: number;
  new: boolean;
  latest: LastResponse;
};
export type Weekly = {
  week_start: string;
  week_label: string;
  as_of: string;
  engineering: {
    total: number;
    message: string | null;
    left_out: Record<string, number>;
    squads: { squad: string; message: string; issues: WeeklyIssue[] }[];
  };
  dsp: { total: number; partners: { name: string; message: string; rows: WeeklyDspRow[] }[] };
};
export type AgentCase = {
  case_id: string;
  case_number: string | null;
  subject: string | null;
  raised_at: string;
  problems: { kind: string; text: string }[];
  check: string[];
};
export type AgentRow = {
  agent: string;
  raised: number;
  wrong: number;
  main_mistake: string | null;
  message: string | null;
  cases: AgentCase[];
  check: AgentCase[];
  github: { title: string; url: string; reasons: string[] }[];
};
export type AgentsReport = { days: number; raised: number; wrong: number; share: number; agents: AgentRow[] };
