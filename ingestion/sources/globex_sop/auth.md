# Globex SOP — Account access & authentication

## 2FA lockouts

Globex enforces mandatory 2FA. A customer locked out after enabling 2FA
**cannot** be helped by support directly — support has no reset capability.

1. Verify identity: full name, account admin email, and the last invoice
   amount.
2. File a Security queue ticket; Security performs the reset within 4
   business hours.
3. Never disable 2FA on the account, even temporarily.

## SSO

SSO (SAML/OIDC) is Business-plan and up, configured by Globex IT jointly
with the customer's admin. Support does not configure SSO; open a
Provisioning ticket.

## API keys

API keys are issued per-integration and are visible once at creation. If a
customer lost a key, they must rotate — support cannot retrieve it.
