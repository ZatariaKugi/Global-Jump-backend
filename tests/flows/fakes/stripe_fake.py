"""An in-memory stand-in for the Stripe SDK calls the app makes, plus a signed-webhook helper.

Nothing here talks to the network. Every ``stripe.X.method_async`` the services call is
replaced by a method on :class:`FakeStripe` that records the call and answers with the
shape the service reads (``_stripe_get`` reads ``obj[key]`` on a ``StripeObject`` or a
dict, so answers are real ``StripeObject`` instances built from dicts).

A payment is "completed" the way Stripe completes it: :meth:`FakeStripe.complete_checkout`
turns the session into a paid one with a payment intent, charge, transfer, application
fee and balance transaction, and :meth:`FakeStripe.deliver` posts the matching event to
our own webhook endpoint with a valid ``Stripe-Signature``.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import time
import uuid
from typing import Any

import stripe
from stripe import StripeObject

WEBHOOK_SECRET = "whsec_flows_0123456789abcdef0123456789"
SECRET_KEY = "sk_test_flows_0123456789"
PUBLISHABLE_KEY = "pk_test_flows_0123456789"
PLATFORM_ACCOUNT_ID = "acct_platform_flows"


def _obj(data: dict[str, Any]) -> StripeObject:
    return StripeObject.construct_from(data, SECRET_KEY)


class FakeStripe:
    """State and behaviour of the fake. One instance per test (see ``conftest``)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sessions: dict[str, dict[str, Any]] = {}
        self.payment_intents: dict[str, dict[str, Any]] = {}
        self.charges: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.transfers: dict[str, dict[str, Any]] = {}
        self.reversals: dict[str, dict[str, Any]] = {}
        self.accounts: dict[str, dict[str, Any]] = {}
        self.customers: dict[str, dict[str, Any]] = {}
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.schedules: dict[str, dict[str, Any]] = {}
        self.invoices: dict[str, dict[str, Any]] = {}
        self.products: dict[str, dict[str, Any]] = {}
        self.prices: dict[str, dict[str, Any]] = {}
        self.delivered: list[dict[str, Any]] = []
        self._seq = itertools.count(1)
        # Stripe's processing fee charged on the next completed checkout, in cents.
        self.next_fee_cents = 320

    # ------------------------------------------------------------------ plumbing
    def _id(self, prefix: str) -> str:
        return f"{prefix}_flows{next(self._seq):05d}"

    def _rec(self, call: str, **kw: Any) -> None:
        self.calls.append((call, kw))

    def called(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]

    # ------------------------------------------------------------------ checkout
    async def checkout_session_create(self, **kw: Any) -> StripeObject:
        self._rec("checkout.Session.create", **kw)
        sid = self._id("cs")
        amount = 0
        for item in kw.get("line_items") or []:
            pd = item.get("price_data") or {}
            amount += int(pd.get("unit_amount") or 0) * int(item.get("quantity") or 1)
        data = {
            "id": sid,
            "object": "checkout.session",
            "url": f"https://checkout.stripe.test/pay/{sid}",
            "mode": kw.get("mode", "payment"),
            "status": "open",
            "payment_status": "unpaid",
            "amount_total": amount,
            "amount_subtotal": amount,
            "currency": "usd",
            "customer": kw.get("customer"),
            "customer_email": kw.get("customer_email"),
            "metadata": dict(kw.get("metadata") or {}),
            "payment_intent": None,
            "subscription": None,
            "success_url": kw.get("success_url"),
            "cancel_url": kw.get("cancel_url"),
            "total_details": {"amount_tax": 0, "amount_discount": 0, "amount_shipping": 0},
            "_payment_intent_data": dict(kw.get("payment_intent_data") or {}),
            "_line_items": list(kw.get("line_items") or []),
            "_subscription_data": dict(kw.get("subscription_data") or {}),
        }
        self.sessions[sid] = data
        return _obj(data)

    async def checkout_session_retrieve(self, session_id: str, **kw: Any) -> StripeObject:
        self._rec("checkout.Session.retrieve", id=session_id, **kw)
        data = dict(self.sessions[session_id])
        expand = kw.get("expand") or []
        if any(str(e).startswith("payment_intent") for e in expand) and data.get("payment_intent"):
            data["payment_intent"] = self._expanded_intent(str(data["payment_intent"]))
        return _obj(data)

    def _expanded_intent(self, pi_id: str) -> dict[str, Any]:
        pi = dict(self.payment_intents[pi_id])
        charge_id = pi.get("latest_charge")
        if isinstance(charge_id, str) and charge_id in self.charges:
            pi["latest_charge"] = dict(self.charges[charge_id])
        return pi

    def complete_checkout(self, session_id: str, *, fee_cents: int | None = None) -> dict[str, Any]:
        """Pay the session as Stripe would; return the ``checkout.session.completed`` event."""
        data = self.sessions[session_id]
        pid = kw_meta = data.get("_payment_intent_data") or {}
        pi_id, ch_id, bt_id = self._id("pi"), self._id("ch"), self._id("txn")
        fee = self.next_fee_cents if fee_cents is None else fee_cents
        app_fee_cents = int(pid.get("application_fee_amount") or 0)
        transfer_id: str | None = None
        destination = (pid.get("transfer_data") or {}).get("destination")
        if destination:
            transfer_id = self._id("tr")
            self.transfers[transfer_id] = {
                "id": transfer_id,
                "object": "transfer",
                "amount": data["amount_total"] - app_fee_cents,
                "currency": "usd",
                "destination": destination,
                "source_transaction": ch_id,
                "reversed": False,
                "amount_reversed": 0,
                "metadata": dict(kw_meta.get("metadata") or {}),
            }
        charge = {
            "id": ch_id,
            "object": "charge",
            "amount": data["amount_total"],
            "amount_refunded": 0,
            "currency": "usd",
            "paid": True,
            "status": "succeeded",
            "refunded": False,
            "payment_intent": pi_id,
            "transfer": transfer_id,
            "application_fee": self._id("fee") if app_fee_cents else None,
            "application_fee_amount": app_fee_cents or None,
            "balance_transaction": {
                "id": bt_id,
                "object": "balance_transaction",
                "fee": fee,
                "amount": data["amount_total"],
                "net": data["amount_total"] - fee,
                "currency": "usd",
            },
            "metadata": dict(kw_meta.get("metadata") or {}),
        }
        self.charges[ch_id] = charge
        self.payment_intents[pi_id] = {
            "id": pi_id,
            "object": "payment_intent",
            "amount": data["amount_total"],
            "currency": "usd",
            "status": "succeeded",
            "latest_charge": ch_id,
            "metadata": dict(kw_meta.get("metadata") or {}),
            "transfer_data": dict(pid.get("transfer_data") or {}),
            "application_fee_amount": app_fee_cents or None,
        }
        data.update({"status": "complete", "payment_status": "paid", "payment_intent": pi_id})
        public = {k: v for k, v in data.items() if not k.startswith("_")}
        return self.event("checkout.session.completed", public)

    # ------------------------------------------------ intents, charges, refunds, transfers
    async def payment_intent_retrieve(self, pi_id: str, **kw: Any) -> StripeObject:
        self._rec("PaymentIntent.retrieve", id=pi_id, **kw)
        return _obj(self._expanded_intent(pi_id))

    async def payment_intent_modify(self, pi_id: str, **kw: Any) -> StripeObject:
        self._rec("PaymentIntent.modify", id=pi_id, **kw)
        self.payment_intents[pi_id].setdefault("metadata", {}).update(kw.get("metadata") or {})
        return _obj(self._expanded_intent(pi_id))

    async def charge_retrieve(self, charge_id: str, **kw: Any) -> StripeObject:
        self._rec("Charge.retrieve", id=charge_id, **kw)
        return _obj(dict(self.charges[charge_id]))

    async def refund_create(self, **kw: Any) -> StripeObject:
        self._rec("Refund.create", **kw)
        rid = self._id("re")
        charge_id = kw.get("charge")
        pi_id = kw.get("payment_intent")
        if not charge_id and pi_id:
            charge_id = self.payment_intents[pi_id]["latest_charge"]
        charge = self.charges[str(charge_id)]
        amount = int(kw.get("amount") or (charge["amount"] - charge["amount_refunded"]))
        charge["amount_refunded"] += amount
        charge["refunded"] = charge["amount_refunded"] >= charge["amount"]
        data = {
            "id": rid,
            "object": "refund",
            "amount": amount,
            "charge": charge_id,
            "payment_intent": charge["payment_intent"],
            "status": "succeeded",
            "currency": "usd",
            "metadata": dict(kw.get("metadata") or {}),
        }
        self.refunds[rid] = data
        return _obj(data)

    async def transfer_create(self, **kw: Any) -> StripeObject:
        self._rec("Transfer.create", **kw)
        tid = self._id("tr")
        data = {
            "id": tid,
            "object": "transfer",
            "amount": int(kw.get("amount") or 0),
            "currency": kw.get("currency", "usd"),
            "destination": kw.get("destination"),
            "source_transaction": kw.get("source_transaction"),
            "reversed": False,
            "amount_reversed": 0,
            "metadata": dict(kw.get("metadata") or {}),
        }
        self.transfers[tid] = data
        return _obj(data)

    async def transfer_create_reversal(self, transfer_id: str, **kw: Any) -> StripeObject:
        self._rec("Transfer.create_reversal", id=transfer_id, **kw)
        tr = self.transfers[transfer_id]
        amount = int(kw.get("amount") or (tr["amount"] - tr["amount_reversed"]))
        tr["amount_reversed"] += amount
        tr["reversed"] = tr["amount_reversed"] >= tr["amount"]
        rid = self._id("trr")
        data = {
            "id": rid,
            "object": "transfer_reversal",
            "amount": amount,
            "transfer": transfer_id,
            "metadata": dict(kw.get("metadata") or {}),
        }
        self.reversals[rid] = data
        return _obj(data)

    # ------------------------------------------------------------------ connect accounts
    async def account_create(self, **kw: Any) -> StripeObject:
        self._rec("Account.create", **kw)
        aid = self._id("acct")
        data = {
            "id": aid,
            "object": "account",
            "type": kw.get("type", "express"),
            "email": kw.get("email"),
            "charges_enabled": False,
            "payouts_enabled": False,
            "details_submitted": False,
            "requirements": {
                "currently_due": ["external_account"],
                "disabled_reason": "requirements.past_due",
            },
            "capabilities": {},
            "metadata": dict(kw.get("metadata") or {}),
        }
        self.accounts[aid] = data
        return _obj(data)

    async def account_retrieve(self, account_id: str | None = None, **kw: Any) -> StripeObject:
        self._rec("Account.retrieve", id=account_id, **kw)
        if account_id is None:
            return _obj(
                {
                    "id": PLATFORM_ACCOUNT_ID,
                    "object": "account",
                    "charges_enabled": True,
                    "payouts_enabled": True,
                    "details_submitted": True,
                    "country": "US",
                    "default_currency": "usd",
                }
            )
        return _obj(dict(self.accounts[account_id]))

    async def account_create_login_link(self, account_id: str, **kw: Any) -> StripeObject:
        self._rec("Account.create_login_link", id=account_id, **kw)
        return _obj(
            {"object": "login_link", "url": f"https://connect.stripe.test/express/{account_id}"}
        )

    async def account_link_create(self, **kw: Any) -> StripeObject:
        self._rec("AccountLink.create", **kw)
        return _obj(
            {
                "object": "account_link",
                "url": f"https://connect.stripe.test/onboarding/{kw.get('account')}",
                "expires_at": int(time.time()) + 300,
            }
        )

    def enable_account(self, account_id: str) -> dict[str, Any]:
        """The advisor finished Stripe onboarding: flags on, and the ``account.updated`` event."""
        acct = self.accounts[account_id]
        acct.update(
            {
                "charges_enabled": True,
                "payouts_enabled": True,
                "details_submitted": True,
                "requirements": {"currently_due": [], "disabled_reason": None},
            }
        )
        return self.event("account.updated", dict(acct))

    # ------------------------------------------------------------------ customers, products, prices
    async def customer_create(self, **kw: Any) -> StripeObject:
        self._rec("Customer.create", **kw)
        cid = self._id("cus")
        data = {
            "id": cid,
            "object": "customer",
            "email": kw.get("email"),
            "name": kw.get("name"),
            "metadata": dict(kw.get("metadata") or {}),
        }
        self.customers[cid] = data
        return _obj(data)

    async def product_create(self, **kw: Any) -> StripeObject:
        self._rec("Product.create", **kw)
        pid = self._id("prod")
        data = {
            "id": pid,
            "object": "product",
            "name": kw.get("name"),
            "active": True,
            "metadata": dict(kw.get("metadata") or {}),
        }
        self.products[pid] = data
        return _obj(data)

    async def product_retrieve(self, product_id: str, **kw: Any) -> StripeObject:
        self._rec("Product.retrieve", id=product_id, **kw)
        return _obj(dict(self.products[product_id]))

    async def product_modify(self, product_id: str, **kw: Any) -> StripeObject:
        self._rec("Product.modify", id=product_id, **kw)
        self.products[product_id].update({k: v for k, v in kw.items() if k != "metadata"})
        return _obj(dict(self.products[product_id]))

    async def price_create(self, **kw: Any) -> StripeObject:
        self._rec("Price.create", **kw)
        pid = self._id("price")
        data = {
            "id": pid,
            "object": "price",
            "product": kw.get("product"),
            "unit_amount": kw.get("unit_amount"),
            "currency": kw.get("currency", "usd"),
            "recurring": dict(kw.get("recurring") or {}),
            "active": True,
            "metadata": dict(kw.get("metadata") or {}),
        }
        self.prices[pid] = data
        return _obj(data)

    async def price_retrieve(self, price_id: str, **kw: Any) -> StripeObject:
        self._rec("Price.retrieve", id=price_id, **kw)
        return _obj(dict(self.prices[price_id]))

    async def price_modify(self, price_id: str, **kw: Any) -> StripeObject:
        self._rec("Price.modify", id=price_id, **kw)
        self.prices[price_id].update(kw)
        return _obj(dict(self.prices[price_id]))

    def complete_subscription_checkout(
        self, session_id: str, *, amount_cents: int
    ) -> list[dict[str, Any]]:
        """A plan was bought: the subscription, its first paid invoice, and the three events
        Stripe sends for it (checkout completed, subscription created, invoice paid)."""
        data = self.sessions[session_id]
        price_id = str((data["_line_items"][0] or {}).get("price") or "")
        meta = dict((data.get("_subscription_data") or {}).get("metadata") or data["metadata"])
        now = int(time.time())
        sub_id, inv_id = self._id("sub"), self._id("in")
        customer = data.get("customer") or self._id("cus")
        sub = {
            "id": sub_id,
            "object": "subscription",
            "status": "active",
            "customer": customer,
            "current_period_start": now,
            "current_period_end": now + 30 * 86400,
            "cancel_at_period_end": False,
            "canceled_at": None,
            "metadata": meta,
            "items": {
                "object": "list",
                "data": [
                    {
                        "id": self._id("si"),
                        "object": "subscription_item",
                        "price": {
                            "id": price_id,
                            "object": "price",
                            "product": self.prices.get(price_id, {}).get("product"),
                        },
                        "quantity": 1,
                        "current_period_start": now,
                        "current_period_end": now + 30 * 86400,
                    }
                ],
            },
            "default_payment_method": "pm_card_flows",
            "schedule": None,
            "latest_invoice": inv_id,
        }
        self.subscriptions[sub_id] = sub
        invoice = {
            "id": inv_id,
            "object": "invoice",
            "customer": customer,
            "subscription": sub_id,
            "status": "paid",
            "paid": True,
            "amount_paid": amount_cents,
            "amount_due": amount_cents,
            "total": amount_cents,
            "subtotal": amount_cents,
            "currency": "usd",
            "billing_reason": "subscription_create",
            "period_start": now,
            "period_end": now + 30 * 86400,
            "hosted_invoice_url": f"https://invoice.stripe.test/{inv_id}",
            "invoice_pdf": f"https://invoice.stripe.test/{inv_id}.pdf",
            "number": f"FLOWS-{inv_id[-5:]}",
            "created": now,
            "metadata": meta,
            "lines": {
                "object": "list",
                "data": [
                    {
                        "id": self._id("il"),
                        "object": "line_item",
                        "amount": amount_cents,
                        "quantity": 1,
                        "price": {"id": price_id, "object": "price"},
                        "period": {"start": now, "end": now + 30 * 86400},
                        "subscription": sub_id,
                        "metadata": meta,
                    }
                ],
            },
        }
        self.invoices[inv_id] = invoice
        data.update(
            {
                "status": "complete",
                "payment_status": "paid",
                "subscription": sub_id,
                "customer": customer,
            }
        )
        public = {k: v for k, v in data.items() if not k.startswith("_")}
        return [
            self.event("checkout.session.completed", public),
            self.event("customer.subscription.created", dict(sub)),
            self.event("invoice.paid", dict(invoice)),
        ]

    async def payment_method_retrieve(self, pm_id: str, **kw: Any) -> StripeObject:
        self._rec("PaymentMethod.retrieve", id=pm_id, **kw)
        return _obj(
            {
                "id": pm_id,
                "object": "payment_method",
                "type": "card",
                "card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030},
            }
        )

    # ------------------------------------------------ subscriptions (echoed; phase 2)
    def _sub(self, sub_id: str) -> dict[str, Any]:
        return self.subscriptions[sub_id]

    async def subscription_retrieve(self, sub_id: str, **kw: Any) -> StripeObject:
        self._rec("Subscription.retrieve", id=sub_id, **kw)
        return _obj(dict(self._sub(sub_id)))

    async def subscription_list(self, **kw: Any) -> StripeObject:
        self._rec("Subscription.list", **kw)
        rows = [
            dict(s)
            for s in self.subscriptions.values()
            if not kw.get("customer") or s.get("customer") == kw.get("customer")
        ]
        return _obj({"object": "list", "data": rows, "has_more": False})

    async def subscription_modify(self, sub_id: str, **kw: Any) -> StripeObject:
        self._rec("Subscription.modify", id=sub_id, **kw)
        sub = self._sub(sub_id)
        for key in (
            "cancel_at_period_end",
            "metadata",
            "default_payment_method",
            "proration_behavior",
        ):
            if key in kw:
                sub[key] = kw[key]
        if "items" in kw:
            for item in kw["items"]:
                if item.get("price"):
                    sub["items"]["data"][0]["price"] = {"id": item["price"]}
        return _obj(dict(sub))

    async def subscription_schedule_create(self, **kw: Any) -> StripeObject:
        self._rec("SubscriptionSchedule.create", **kw)
        sid = self._id("sub_sched")
        data = {
            "id": sid,
            "object": "subscription_schedule",
            "subscription": kw.get("from_subscription"),
            "phases": list(kw.get("phases") or []),
            "status": "active",
            "metadata": dict(kw.get("metadata") or {}),
        }
        self.schedules[sid] = data
        return _obj(data)

    async def subscription_schedule_retrieve(self, sid: str, **kw: Any) -> StripeObject:
        self._rec("SubscriptionSchedule.retrieve", id=sid, **kw)
        return _obj(dict(self.schedules[sid]))

    async def subscription_schedule_modify(self, sid: str, **kw: Any) -> StripeObject:
        self._rec("SubscriptionSchedule.modify", id=sid, **kw)
        self.schedules[sid].update(dict(kw))
        return _obj(dict(self.schedules[sid]))

    async def subscription_schedule_release(self, sid: str, **kw: Any) -> StripeObject:
        self._rec("SubscriptionSchedule.release", id=sid, **kw)
        self.schedules[sid]["status"] = "released"
        return _obj(dict(self.schedules[sid]))

    async def invoice_list(self, **kw: Any) -> StripeObject:
        self._rec("Invoice.list", **kw)
        rows = [
            dict(i)
            for i in self.invoices.values()
            if not kw.get("customer") or i.get("customer") == kw.get("customer")
        ]
        return _obj({"object": "list", "data": rows, "has_more": False})

    async def invoice_list_lines(self, invoice_id: str, **kw: Any) -> StripeObject:
        self._rec("Invoice.list_lines", id=invoice_id, **kw)
        return _obj(
            {
                "object": "list",
                "data": list(self.invoices[invoice_id].get("lines") or []),
                "has_more": False,
            }
        )

    async def invoice_item_create(self, **kw: Any) -> StripeObject:
        self._rec("InvoiceItem.create", **kw)
        return _obj(
            {
                "id": self._id("ii"),
                "object": "invoiceitem",
                **{k: v for k, v in kw.items() if k != "metadata"},
            }
        )

    async def invoice_create(self, **kw: Any) -> StripeObject:
        self._rec("Invoice.create", **kw)
        iid = self._id("in")
        data = {
            "id": iid,
            "object": "invoice",
            "customer": kw.get("customer"),
            "subscription": kw.get("subscription"),
            "status": "draft",
            "amount_paid": 0,
            "amount_due": 0,
            "lines": [],
            "metadata": dict(kw.get("metadata") or {}),
        }
        self.invoices[iid] = data
        return _obj(data)

    async def invoice_pay(self, invoice_id: str, **kw: Any) -> StripeObject:
        self._rec("Invoice.pay", id=invoice_id, **kw)
        self.invoices[invoice_id]["status"] = "paid"
        return _obj(dict(self.invoices[invoice_id]))

    async def billing_portal_session_create(self, **kw: Any) -> StripeObject:
        self._rec("billing_portal.Session.create", **kw)
        return _obj(
            {
                "id": self._id("bps"),
                "object": "billing_portal.session",
                "url": "https://billing.stripe.test/portal",
            }
        )

    async def webhook_endpoint_list(self, **kw: Any) -> StripeObject:
        self._rec("WebhookEndpoint.list", **kw)
        return _obj(
            {
                "object": "list",
                "data": [
                    {
                        "id": "we_flows",
                        "object": "webhook_endpoint",
                        "url": "http://test/api/v1/payments/webhook",
                        "status": "enabled",
                        "enabled_events": ["*"],
                    }
                ],
                "has_more": False,
            }
        )

    # ------------------------------------------------------------------ events and webhooks
    def event(self, event_type: str, obj: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": f"evt_{uuid.uuid4().hex[:24]}",
            "object": "event",
            "type": event_type,
            "api_version": "2026-08-26",
            "created": int(time.time()),
            "livemode": False,
            "data": {"object": obj},
        }

    @staticmethod
    def sign(payload: bytes, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> str:
        ts = timestamp or int(time.time())
        mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
        return f"t={ts},v1={mac}"

    async def deliver(self, client: Any, event: dict[str, Any]) -> Any:
        """Post ``event`` to our webhook like Stripe would (signed); returns the HTTP response."""
        payload = json.dumps(event, separators=(",", ":")).encode()
        self.delivered.append(event)
        return await client.post(
            "/api/v1/payments/webhook",
            content=payload,
            headers={"Stripe-Signature": self.sign(payload), "Content-Type": "application/json"},
        )

    # ------------------------------------------------------------------ install
    def install(self, monkeypatch: Any) -> None:
        pairs = [
            (stripe.checkout.Session, "create_async", self.checkout_session_create),
            (stripe.checkout.Session, "retrieve_async", self.checkout_session_retrieve),
            (stripe.PaymentIntent, "retrieve_async", self.payment_intent_retrieve),
            (stripe.PaymentIntent, "modify_async", self.payment_intent_modify),
            (stripe.Charge, "retrieve_async", self.charge_retrieve),
            (stripe.Refund, "create_async", self.refund_create),
            (stripe.Transfer, "create_async", self.transfer_create),
            (stripe.Transfer, "create_reversal_async", self.transfer_create_reversal),
            (stripe.Account, "create_async", self.account_create),
            (stripe.Account, "retrieve_async", self.account_retrieve),
            (stripe.Account, "create_login_link_async", self.account_create_login_link),
            (stripe.AccountLink, "create_async", self.account_link_create),
            (stripe.Customer, "create_async", self.customer_create),
            (stripe.PaymentMethod, "retrieve_async", self.payment_method_retrieve),
            (stripe.Product, "create_async", self.product_create),
            (stripe.Product, "retrieve_async", self.product_retrieve),
            (stripe.Product, "modify_async", self.product_modify),
            (stripe.Price, "create_async", self.price_create),
            (stripe.Price, "retrieve_async", self.price_retrieve),
            (stripe.Price, "modify_async", self.price_modify),
            (stripe.Subscription, "retrieve_async", self.subscription_retrieve),
            (stripe.Subscription, "list_async", self.subscription_list),
            (stripe.Subscription, "modify_async", self.subscription_modify),
            (stripe.SubscriptionSchedule, "create_async", self.subscription_schedule_create),
            (stripe.SubscriptionSchedule, "retrieve_async", self.subscription_schedule_retrieve),
            (stripe.SubscriptionSchedule, "modify_async", self.subscription_schedule_modify),
            (stripe.SubscriptionSchedule, "release_async", self.subscription_schedule_release),
            (stripe.Invoice, "list_async", self.invoice_list),
            (stripe.Invoice, "list_lines_async", self.invoice_list_lines),
            (stripe.Invoice, "create_async", self.invoice_create),
            (stripe.Invoice, "pay_async", self.invoice_pay),
            (stripe.InvoiceItem, "create_async", self.invoice_item_create),
            (stripe.billing_portal.Session, "create_async", self.billing_portal_session_create),
            (stripe.WebhookEndpoint, "list_async", self.webhook_endpoint_list),
        ]
        for owner, name, fn in pairs:
            monkeypatch.setattr(owner, name, fn)
