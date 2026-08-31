-- Phase 20o: `notify_targets` — the central routing table for the `notify` node.
--
-- The Phase 20n `notify` node pinged an internal rep whose id lived in each
-- flow's node config (`target_by_type` / `target_by_module`), so every flow
-- editor had to paste Salesforce ids. This table holds the mapping once, per
-- tenant, and `interpreter.routing.resolve_notify_target` reads it at run time
-- (`interpreter/registry.h_notify` consults it when the node's own maps don't
-- match). A row resolves one of three ways:
--
--   resolver='static'        sf_target_id is a fixed User / Group id
--   resolver='sf_team_role'  look up the current member of Team_<sf_team>
--                            (a Phase 20i roster queue) LIVE from Salesforce
--   resolver='sf_queue'      resolve a Queue by DeveloperName / Name
--
-- RLS: read = any tenant member; write = owner/editor (mirrors 032's pattern
-- via public.is_tenant_member / public.is_tenant_editor). The worker uses the
-- service key and bypasses RLS.

create table if not exists notify_targets (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null,
  match_kind     text not null check (match_kind in ('case_type', 'module')),
  match_value    text not null,
  resolver       text not null default 'static'
                   check (resolver in ('static', 'sf_team_role', 'sf_queue')),
  sf_target_id   text,                       -- resolver='static': 15/18-char User or Group id
  sf_target_type text check (sf_target_type in ('user', 'group', 'queue')),
  sf_team        text,                       -- resolver='sf_team_role': e.g. 'Support'
  sf_role        text default 'Manager',
  sf_queue       text,                       -- resolver='sf_queue': e.g. 'Billing_Escalations'
  label          text,                       -- shown in the Chatter note
  active         boolean not null default true,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (tenant_id, match_kind, match_value)
);

alter table notify_targets enable row level security;

create policy notify_targets_read on notify_targets for select to authenticated
  using (public.is_tenant_member(tenant_id));
create policy notify_targets_write on notify_targets for all to authenticated
  using (public.is_tenant_editor(tenant_id))
  with check (public.is_tenant_editor(tenant_id));

-- Seed: tenant 00000000… — one row per Case.Type. The Phase 20i roster has no
-- "Billing"/"Product" team, so those point at the matching Queue; the rest go
-- to the Support team's current lead, resolved live from Salesforce.
insert into notify_targets
  (tenant_id, match_kind, match_value, resolver, sf_team, sf_role, sf_queue, sf_target_type, label)
values
 ('00000000-0000-0000-0000-000000000000', 'case_type', 'Billing',         'sf_queue',     null,      null,      'Billing_Escalations', 'queue', 'Billing team'),
 ('00000000-0000-0000-0000-000000000000', 'case_type', 'Account / Login', 'sf_team_role', 'Support', 'Manager', null,                  null,    'Login & identity'),
 ('00000000-0000-0000-0000-000000000000', 'case_type', 'Problem / Bug',   'sf_team_role', 'Support', 'Manager', null,                  null,    'Support engineering lead'),
 ('00000000-0000-0000-0000-000000000000', 'case_type', 'Feature Request', 'sf_queue',     null,      null,      'Support_Tier2',       'queue', 'Product'),
 ('00000000-0000-0000-0000-000000000000', 'case_type', 'How-to',          'sf_team_role', 'Support', 'Manager', null,                  null,    'Support lead'),
 ('00000000-0000-0000-0000-000000000000', 'case_type', 'Question',        'sf_team_role', 'Support', 'Manager', null,                  null,    'Support lead'),
 ('00000000-0000-0000-0000-000000000000', 'case_type', 'Other',           'sf_team_role', 'Support', 'Manager', null,                  null,    'Support lead')
on conflict (tenant_id, match_kind, match_value) do nothing;
