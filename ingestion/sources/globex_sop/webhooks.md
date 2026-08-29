# Globex SOP — Webhook & trigger requests

Internal support playbook. Overrides the public docs where they differ.

## Policy

Globex **does not** hand out raw webhook URLs to customers on the Team plan.
Webhook triggers require the Business plan or above. If a Team-plan customer
asks how to set up a webhook trigger:

1. Confirm their plan in the billing console.
2. If Team plan: reply that webhook triggers are a Business-plan feature and
   link the upgrade page. Do **not** walk them through the setup.
3. If Business+: send the standard setup steps and the test-event guide.

## Testing

Point customers at the shared staging endpoint `hooks.globex.test/inbound`
for test events — never a production URL. Test payloads are retained 24h.

## Escalation

Any webhook question that mentions "production", "PII", or "compliance"
goes straight to the platform on-call, not the standard queue.
