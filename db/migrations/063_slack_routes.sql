-- Phase 27e: category -> Slack channel + usergroup routing for handoffs.
--
-- Extends `notify_targets` (migration 045) so the same table that resolves the
-- Chatter/SF target also carries where in Slack the handoff card goes and which
-- on-call usergroup it @mentions. `interpreter.routing.resolve_slack_route`
-- reads it; most-specific match wins (routed_team < module < case_type), and
-- no match falls back to #cx-unrouted.

alter table notify_targets add column if not exists slack_channel   text;
alter table notify_targets add column if not exists slack_usergroup text;
alter table notify_targets add column if not exists urgency         text
  check (urgency is null or urgency in ('normal', 'high'));

-- allow routing keyed on the pipeline's routed_team, not just Case.Type/Module
alter table notify_targets drop constraint if exists notify_targets_match_kind_check;
alter table notify_targets add  constraint notify_targets_match_kind_check
  check (match_kind in ('case_type', 'module', 'routed_team'));

-- Seed: one row per routed_team -> its #cx-* channel + @cx-*-oncall usergroup.
-- Usergroup handles are workspace-specific; replace after creating them.
insert into notify_targets
  (tenant_id, match_kind, match_value, resolver, sf_queue, sf_target_type,
   label, slack_channel, slack_usergroup, urgency)
values
 ('00000000-0000-0000-0000-000000000000','routed_team','support',    'sf_queue','Team_Support',       'queue','Support L1',      '#cx-l1',          '@cx-l1-oncall',    'normal'),
 ('00000000-0000-0000-0000-000000000000','routed_team','tier2',      'sf_queue','Support_Tier2',      'queue','Support Tier 2',  '#cx-tier2',       '@cx-tier2-oncall', 'high'),
 ('00000000-0000-0000-0000-000000000000','routed_team','csm',        'sf_queue','Team_CSM',           'queue','CSM',             '#cx-csm',         '@cx-csm',          'normal'),
 ('00000000-0000-0000-0000-000000000000','routed_team','sales',      'sf_queue','Team_Sales',         'queue','Sales',           '#cx-sales',       '@cx-sales',        'normal'),
 ('00000000-0000-0000-0000-000000000000','routed_team','offboarding','sf_queue','Team_Offboarding',   'queue','Offboarding/Trust','#cx-offboarding','@trust-oncall',    'high'),
 ('00000000-0000-0000-0000-000000000000','routed_team','billing',    'sf_queue','Billing_Escalations','queue','Billing',         '#cx-billing',     '@billing-oncall',  'high')
on conflict (tenant_id, match_kind, match_value) do nothing;
