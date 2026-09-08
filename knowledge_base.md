# Knowledge Transfer Document
## Order Management Backend Service — Onboarding & Handover Guide

**Document Owner:** Backend Platform Team
**Last Updated:** August 2026
**Audience:** Engineers taking over ownership of the Order Management Service (OMS)

---

## 1. Service Overview

The Order Management Service (OMS) is the core backend system responsible for handling the full lifecycle of customer orders across the platform — from cart checkout through payment confirmation, fulfillment, and post-order events like returns and refunds. It is one of the most business-critical services in the platform because every revenue-generating transaction flows through it.

OMS was originally built in 2022 as a monolith and was gradually split into a set of microservices in 2024 during the platform's service decomposition initiative. Today it consists of five cooperating services:

1. **order-api** — the public-facing REST API that receives order creation and update requests.
2. **order-processor** — a background worker that validates, enriches, and transitions orders through their lifecycle states.
3. **payment-gateway-adapter** — a thin integration layer that talks to the third-party payment provider (Stripe) and normalizes webhook events.
4. **inventory-sync** — keeps order line items in sync with the Inventory service so that stock levels never go negative.
5. **notification-dispatcher** — publishes events (order confirmed, shipped, cancelled, refunded) to the Notification service, which handles emails and push notifications.

All five services communicate primarily through a Kafka event bus (topic prefix `oms.*`), with order-api being the only one exposed to external traffic via the API gateway.

---

## 2. Architecture & Data Flow

When a customer completes checkout, the flow is as follows:

1. The frontend calls `POST /v2/orders` on **order-api**.
2. order-api validates the request payload, writes an `order` row in state `PENDING`, and publishes an `oms.order.created` event to Kafka.
3. **order-processor** consumes the event, performs fraud-risk scoring (via a call to the Risk service), and if the order passes, transitions it to `AWAITING_PAYMENT` and emits `oms.order.awaiting_payment`.
4. **payment-gateway-adapter** listens for that event, initiates a payment intent with Stripe, and later receives an asynchronous Stripe webhook confirming success or failure. It publishes `oms.payment.succeeded` or `oms.payment.failed` accordingly.
5. On success, **order-processor** transitions the order to `CONFIRMED`, and **inventory-sync** decrements stock for each line item.
6. **notification-dispatcher** picks up the `CONFIRMED` event and triggers the "Order Confirmed" email/push notification.

The primary datastore is a PostgreSQL cluster (`oms-primary-db`), with one logical database per service to preserve service boundaries, even though they currently share a physical cluster for cost reasons. Each service owns its own schema and no service is permitted to query another service's tables directly — all cross-service reads go through Kafka events or synchronous REST calls, never shared SQL access.

---

## 3. Order State Machine

Orders move through a strict state machine. Understanding this is essential for debugging:

```
PENDING → AWAITING_PAYMENT → CONFIRMED → FULFILLED → COMPLETED
                 ↓                ↓
             CANCELLED        REFUNDED
```

A few important rules:
- An order can only move to `CANCELLED` from `PENDING` or `AWAITING_PAYMENT` — once `CONFIRMED`, cancellation is no longer allowed; a `REFUNDED` flow must be used instead.
- `REFUNDED` can only be reached from `CONFIRMED` or `FULFILLED`, never from `COMPLETED` (once an order is marked completed, refunds go through a separate Finance-owned process outside OMS).
- State transitions are enforced in code inside `order-processor/src/state_machine.py` — do not attempt to update `orders.status` directly via SQL in production; it will desync the event log and cause downstream services to behave inconsistently.

---

## 4. Common Operational Issues & How to Handle Them

**"Stuck" orders in AWAITING_PAYMENT for more than 30 minutes.**
This is almost always caused by a missed or delayed Stripe webhook. Check the `payment-gateway-adapter` logs for the order's payment intent ID. If Stripe's dashboard shows the payment actually succeeded but our webhook was never received, manually replay the webhook via Stripe's dashboard ("Resend webhook") rather than manually editing order state — this keeps the event log consistent.

**Inventory oversell incidents.**
If inventory-sync falls behind (usually due to Kafka consumer lag), it's possible for two orders to be confirmed for the same last unit of stock. The mitigation is a nightly reconciliation job (`inventory-sync/jobs/reconcile.py`) that flags any negative-stock orders for manual review by the Fulfillment team. If you see a spike in reconciliation flags, check Kafka consumer lag on the `oms.order.confirmed` topic first.

**Duplicate order creation on client retries.**
order-api requires an `Idempotency-Key` header on all `POST /v2/orders` calls. If a client retries a request without changing the key, order-api returns the original order instead of creating a duplicate. Bugs here almost always trace back to a frontend client not sending or reusing this header correctly — this is not something to "fix" on the backend, it needs to be fixed on the calling client.

**Refund not reflecting in customer's account.**
Refunds are asynchronous. OMS marks the order `REFUNDED` immediately, but the actual money movement depends on Stripe and the customer's bank, which can take 5–10 business days. This is expected behavior and is the single most common false-positive support escalation — always check the order's `refunded_at` timestamp before assuming something is broken.

---

## 5. Deployment & Environments

All five services deploy via the internal CI/CD pipeline (Jenkins → ArgoCD → Kubernetes). Each service has its own Helm chart under `infra/helm/oms-*`. There are three environments: `dev`, `staging`, and `prod`. Deploys to `prod` require two approvals and are gated behind a canary rollout (10% traffic for 15 minutes before full rollout).

Key environment variables every service needs:
- `KAFKA_BOOTSTRAP_SERVERS`
- `OMS_DB_CONNECTION_STRING` (per-service, injected via Vault)
- `STRIPE_API_KEY` (payment-gateway-adapter only, also via Vault — never hardcode this)

---

## 6. Monitoring & Alerting

Dashboards live in Grafana under the "Order Management" folder. The most important panels to know:

- **Order creation rate** — sudden drops usually indicate an upstream frontend or gateway issue, not an OMS bug.
- **Kafka consumer lag per topic** — the single most useful early-warning signal for almost every incident class described above.
- **Payment webhook latency (p95)** — spikes here correlate strongly with Stripe-side incidents; check Stripe's status page before assuming it's us.
- **State machine transition errors** — any nonzero rate here is worth investigating same-day, since it usually means an order is stuck in an invalid state.

PagerDuty escalation policy: `oms-oncall`. The on-call engineer rotates weekly and is expected to acknowledge Sev1/Sev2 pages within 10 minutes.

---

## 7. Key Contacts & Ownership

- **Backend Platform Team** — owns order-api, order-processor, inventory-sync.
- **Payments Team** — owns payment-gateway-adapter and the Stripe integration contract.
- **Notifications Team** — owns notification-dispatcher.
- **Fulfillment Team** — primary stakeholder for inventory reconciliation and shipping-state issues; they are the escalation point for oversell incidents.
- **Finance Team** — owns post-completion refund exceptions that fall outside the standard `REFUNDED` state flow.

---

## 8. Things a New Owner Should Do in the First Week

1. Get access to the `oms-*` Grafana dashboards, PagerDuty rotation, and the `#oms-eng` Slack channel.
2. Walk through one full order lifecycle end-to-end in the `staging` environment, from checkout to confirmation, tracing the Kafka events at each step.
3. Read `order-processor/src/state_machine.py` in full — it is the single most important file in the whole system.
4. Review the last 90 days of incident postmortems tagged `oms` in the internal wiki; nearly all of Section 4's "common issues" above trace back to real incidents documented there.
5. Shadow the current on-call engineer for at least one full rotation before joining the `oms-oncall` schedule yourself.