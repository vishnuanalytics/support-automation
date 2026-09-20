-- Self-serve "cancel subscription" (Billing tab): cancellation is always
-- scheduled for the end of the current billing period, never immediate
-- (2026-09-20 product decision -- no partial-refund mechanism exists here,
-- so an immediate cut-off would mean paying for days never used). The
-- provider's own cancel API (Stripe: update with cancel_at_period_end,
-- Razorpay: /cancel with cancel_at_cycle_end) is called for real -- this
-- column is purely a local mirror for the UI to show "cancels on <date>"
-- and to stop the Cancel button being clicked twice, not the source of
-- truth (the provider webhook, unchanged, still flips billing_status to
-- 'canceled' for real once the period actually ends).

alter table subscriptions add column if not exists cancel_at_period_end boolean not null default false;
