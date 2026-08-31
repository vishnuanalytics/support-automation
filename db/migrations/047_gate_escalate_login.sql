-- Phase 20p follow-up: widen the confidence_gate's forced-escalation net so a
-- login / account-access outage still goes straight to the rep even when the
-- classifier labels it Type = "Problem / Bug".
--
-- Live scenario C ("Locked out — SSO/Okta not working") landed in `clarify`
-- because: type was "Problem / Bug" (not in escalate_types), topic "sso-login"
-- didn't token-match "account-access", and "Account & Login" wasn't in
-- escalate_modules. Add the module + a few access keywords (a topic token
-- subset match — "sso" ⊆ {sso, login} — fires regardless of the Type).
--
-- Config-only on the email flow gate (e5000007). Re-snapshots the published
-- version. Portable copy: interpreter/flows/flow_email_l0l1.json.

update flow_nodes
   set config = config
       || jsonb_build_object('escalate_modules',
             jsonb_build_array('Billing & Plans', 'Account & Login'))
       || jsonb_build_object('escalate_topics', jsonb_build_array(
             'billing', 'refund', 'pricing', 'legal', 'compliance',
             'account-access', 'data-export', 'partner-api', 'cancellation',
             'sso', 'saml', 'login', 'locked out', 'lockout', '2fa', 'mfa',
             'password reset'))
 where node_id = 'e5000007-5555-4555-8555-555555555555';

-- re-snapshot the current published version from the updated draft graph
update flow_versions v
   set nodes = (select jsonb_agg(jsonb_build_object(
                  'node_id', n.node_id, 'type', n.type, 'label', n.label,
                  'position_x', n.position_x, 'position_y', n.position_y, 'config', n.config))
                from flow_nodes n where n.flow_id = v.flow_id)
 where v.flow_id = 'e5e5e5e5-5555-4555-8555-555555555555'
   and v.version = (select published_version from flows
                    where flow_id = 'e5e5e5e5-5555-4555-8555-555555555555');
