# Payments Update — Stripe Connect Delayed Payout (Frontend Notes)

**Audience:** Frontend team
**Status:** Implemented on branch `frontend-changes` (backend). Requires DB migration + Stripe Dashboard config before it works end-to-end.
**TL;DR:** Advisers now get paid through their own Stripe Connect accounts. A customer's payment goes to the platform, is **held for 15 minutes**, then automatically transferred to the adviser. **Refunds are only possible during that 15-minute window.** An adviser cannot be paid until they finish Stripe payout onboarding — this adds a new blocking state on the booking/checkout path.

---

## What changed, in plain terms

1. **Advisers must complete Stripe payout onboarding before they can receive payments.** This is separate from admin verification. Both gates must be cleared.
2. **Checkout is now blocked** for advisers who aren't payout-ready (new error) and for free/$0 bookings.
3. **A payment is not immediately final.** After a successful payment there is a **2-minute hold**, then the adviser's share transfers automatically. Only during that window can the payment be refunded. After it, refunds are rejected.

No endpoint paths were removed or renamed. The changes are: two new fields on an existing response, and three new error codes to handle.

---

## API changes

### 1. `GET /api/v1/advisors/me/stripe-connect` — new response field

Adviser payout-onboarding status. **New field: `payouts_enabled`.**

```jsonc
{
  "success": true,
  "data": {
    "stripe_account_id": "acct_123" ,      // null if never started onboarding
    "charges_enabled": true,               // can this adviser be paid?  ← gate the checkout on this
    "payouts_enabled": true,               // NEW — can Stripe pay out to their bank?
    "onboarding_complete": true,           // details_submitted && charges_enabled
    "onboarding_url": null                 // only present on POST (see below)
  },
  "meta": { "request_id": "…", "timestamp": "…" }
}
```

### 2. `POST /api/v1/advisors/me/stripe-connect` — start / resume onboarding

Unchanged shape. Returns the same object with an **`onboarding_url`** the frontend should redirect the adviser to (Stripe-hosted onboarding). After they return, re-fetch the `GET` above to see updated flags.

```jsonc
{
  "success": true,
  "data": {
    "stripe_account_id": "acct_123",
    "charges_enabled": false,
    "payouts_enabled": false,
    "onboarding_complete": false,
    "onboarding_url": "https://connect.stripe.com/setup/…"   // redirect the adviser here
  }
}
```

**Suggested adviser UX:**
- If `stripe_account_id` is `null` → show a "Set up payouts" CTA → `POST` → redirect to `onboarding_url`.
- If `charges_enabled` is `false` → show "Payout setup incomplete — finish setup to receive payments" → `POST` again to get a fresh `onboarding_url`.
- If `charges_enabled` and `payouts_enabled` are both `true` → show "Payouts active".

### 3. `POST /api/v1/payments/checkout` — three new blocking errors

All are **HTTP 400** with the standard error envelope:

```jsonc
{ "success": false, "error": { "code": "<code>", "message": "<human message>" }, "meta": { … } }
```

| `error.code` | When | Suggested frontend handling |
|---|---|---|
| `advisor_not_payable` | The adviser hasn't completed Stripe payout onboarding | Don't let the seeker reach checkout for this adviser; if they do, show "This advisor isn't accepting payments yet." Ideally **hide/disable the Pay button** using the adviser's readiness (see below). |
| `not_payable` | The booking has no charge amount (e.g. a free/advisor-created booking) | Hide the Pay action for $0 bookings. |
| `duplicate_checkout` | A non-resumable paid/failed transaction already exists for this booking | Refresh the booking; show its actual payment status. |

> Note: a still-open checkout session is **resumed**, not rejected — calling checkout again returns the existing `checkout_url`. Only terminal duplicates raise `duplicate_checkout`.

**How to know an adviser is payable *before* checkout:** there is no public per-adviser payout-readiness field on the public advisor profile yet. For the adviser's *own* dashboard, use `GET /advisors/me/stripe-connect`. If you need to gate a seeker's Pay button on the adviser's readiness (recommended, to avoid a dead-end at checkout), tell us and we'll expose a `payments_enabled` boolean on the public advisor read schema.

### 4. Refunds — `POST /api/v1/admin/payments/{transaction_id}/refund`

Admin-only. **New error, HTTP 400:**

| `error.code` | When |
|---|---|
| `refund_window_closed` | The 15-minute hold has elapsed and the payout was transferred — the payment is now locked and cannot be refunded (full or partial). |

Existing refund errors (`not_refundable`, `refund_amount_too_large`, `no_charge`) are unchanged. In the admin UI, **only show the Refund action while the payment is inside the hold window**; otherwise expect this error.

---

## New concept: the payment "hold" (for status displays)

A paid transaction now moves through a payout lifecycle in addition to its existing `status`. The frontend does **not** strictly need to render this, but it explains the timing:

- **T+0** — payment succeeds. Money is on the platform. Refund is possible.
- **T+0 to T+15m** — hold window. Refund still possible.
- **T+15m** — the adviser's share is transferred automatically. **Refunds now blocked.**

The customer-facing `status` / `display_status` on payment reads is unchanged (`paid` / `pending` / `refunded` / `failed`). If you want to surface "refundable until HH:MM" or a hold countdown in the UI, we can add a `refund_deadline` timestamp to the transaction read schema — **not currently exposed; ask if you want it.**

The hold length is configurable server-side (`PAYOUT_HOLD_MINUTES`, currently **15** in production intent; may be set lower in staging for testing). Don't hardcode "15 minutes" in the frontend copy if you can avoid it — or if you must, we can expose the value via `GET /payments/config`.

---

## Money split (unchanged math, for reference)

One customer charge splits three ways. Only the adviser's share leaves the platform, and only after the hold.

| Portion | Amount | Where it goes |
|---|---|---|
| Commission | `PLATFORM_COMMISSION_RATE` (15%) | Stays on platform |
| Tax withheld | `TAX_WITHHOLDING_RATE` (8%) | Stays on platform |
| Adviser payout | `advisor_payout_usd` = amount − commission − tax | Transferred to adviser at T+15m |

These fields already exist on the payment read schemas (`commission_usd`, `tax_usd`, `advisor_payout_usd`, `platform_fee_usd`, `consultant_fee_usd`) — no change.

---

## What the frontend needs to do

- [ ] Add a **"Set up payouts"** flow on the adviser dashboard using `POST /advisors/me/stripe-connect` → redirect to `onboarding_url` → on return, re-fetch `GET` and reflect `charges_enabled` / `payouts_enabled`.
- [ ] Handle the three new checkout errors (`advisor_not_payable`, `not_payable`, `duplicate_checkout`) gracefully.
- [ ] In the admin refund UI, handle `refund_window_closed` and prefer to only show Refund inside the hold window.
- [ ] (Optional, recommended) Ask backend to expose a public `payments_enabled` boolean per adviser so the Pay button can be gated before checkout.

## What the backend still needs (not frontend-blocking)

- Apply DB migration `e2f4a6c8b1d3` (done in dev).
- Register the **`account.updated`** webhook event in the Stripe Dashboard (in addition to `checkout.session.completed`, `checkout.session.expired`, `charge.refunded`).
- Set test/live Stripe keys per environment.

---

_Questions? The two optional additions above (`payments_enabled` on public adviser, `refund_deadline` on transaction read) are quick to add if the frontend wants them — just say so._
