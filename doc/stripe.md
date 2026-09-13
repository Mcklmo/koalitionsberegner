# A bare minimum Stripe account

> **Checkout is closed right now.** `PAYMENTS_PAUSED` defaults to `true` while
> payments are being fixed, so nothing below sells anything until it is set to
> `false` — the app answers `503` to every checkout, offers no purchasable
> tier, and asks people to come back tomorrow and request the election they
> wanted instead. Existing subscriptions are untouched and Stripe's portal
> stays open. Set up Stripe by all means; just expect to flip that variable
> before a single card is charged.

Everything needed to make `STRIPE_API_KEY`, `STRIPE_PRICE_BASIC`,
`STRIPE_PRICE_PREMIUM` and `STRIPE_WEBHOOK_SECRET` real, in about fifteen
minutes. Stay in **test mode** throughout: test mode is fully functional
without a registered business or a bank account, and the paid flow — checkout,
webhook, tier change, a quota that is actually spendable — works end to end in
it. Only taking real money needs an activated account, and that is the last
section.

## 1. The account

Sign up at <https://dashboard.stripe.com/register> with an email and a
password. Skip the "activate your account" business questionnaire it offers
afterwards; nothing below needs it.

Check that the **Test mode** toggle in the Dashboard is on. Every id you copy
from here on begins `sk_test_`, `price_` or `whsec_` and is worthless in
production — which is the point, and also the thing to remember when you later
wonder why the live site sells nothing.

## 2. One recurring price per paid tier

The price on a subscription is what decides the tier, so there is one price per
tier and the ids are what the app is configured with.

In **Product catalogue → Add product**, twice:

| Product | Price | Billing period |
| --- | --- | --- |
| Basic | whatever you like, e.g. €5 | Monthly, recurring |
| Premium | e.g. €25 | Monthly, recurring |

Open each product and copy the price id under its pricing entry — `price_…`,
not the `prod_…` above it. Those two become `STRIPE_PRICE_BASIC` and
`STRIPE_PRICE_PREMIUM`. One of the two is enough if you only want to sell one
tier; the other can stay unset.

## 3. The secret key

**Developers → API keys → Secret key → Reveal.** That `sk_test_…` is
`STRIPE_API_KEY`. It is a real credential even in test mode: keep it out of the
repo and out of anything the browser loads. This app never sends it to the
page — `/api/config` serves only the Firebase web config.

## 4. The webhook

A tier only ever changes because a signed webhook said so, so this step is not
optional: without it checkout succeeds and the user stays on free.

**Locally**, install the [Stripe CLI](https://docs.stripe.com/stripe-cli),
`stripe login`, then:

```sh
stripe listen --forward-to localhost:8000/api/billing/webhook
```

It prints a `whsec_…` on startup — that is `STRIPE_WEBHOOK_SECRET` for as long
as that process runs. No Dashboard endpoint is needed for local work, and none
would reach `localhost` anyway.

**Deployed**, under **Developers → Webhooks → Add endpoint**, point it at
`https://your-host/api/billing/webhook` and subscribe to exactly four events:

- `checkout.session.completed`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`

The endpoint's own signing secret (again `whsec_…`, and different from the
CLI's) is `STRIPE_WEBHOOK_SECRET` there. Everything else Stripe sends is
acknowledged and dropped.

## 5. Activate the customer portal

**Settings → Billing → Customer portal → Save/Activate**, once. Until a
configuration is saved, `POST /api/billing/portal` fails and a subscriber has
no way to cancel. Allowing "cancel subscription" and "update payment method" is
enough; the defaults are fine.

## 6. Run it

```sh
cd backend
ELECTION_STORE=sqlite AUTH_MODE=sqlite \
STRIPE_API_KEY=sk_test_… \
STRIPE_PRICE_BASIC=price_… STRIPE_PRICE_PREMIUM=price_… \
STRIPE_WEBHOOK_SECRET=whsec_… \
PUBLIC_BASE_URL=http://localhost:8000 \
BASIC_MONTHLY_IMPORTS=2 \
uv run uvicorn app.main:app --reload
```

The same five lines work in a `.env` at the repo root instead, which saves
retyping them — but keep them together: a `STRIPE_API_KEY` with no price, or no
`PUBLIC_BASE_URL`, is a startup error rather than a half-configured shop.

`PUBLIC_BASE_URL` is where Stripe returns the user after checkout, and billing
refuses to start without it. `BASIC_MONTHLY_IMPORTS=2` makes the allowance
small enough to spend, so the `429` at the end of a month is a minute away
rather than ten.

Then sign up in the page, buy Basic, and pay with the test card
`4242 4242 4242 4242` — any future expiry, any CVC, any postcode. Watch the
`stripe listen` window: `checkout.session.completed` ties the Stripe customer
to the uid, and `customer.subscription.created` is what actually raises the
tier. `stripe:test-cards` lists cards for the unhappy paths (declines, 3DS
challenges) if you want them.

Half-configured billing is refused at startup rather than silently disabled, so
a missing piece here is a boot failure naming the variable, not a mystery at
first purchase.

## Going live, later

Test mode and live mode share nothing but the login. When you actually want
money:

1. Complete activation (business details, bank account, identity) under
   **Settings → Business**.
2. Recreate the two products/prices in live mode — the `price_` ids differ.
3. Take the `sk_live_…` key and create the live webhook endpoint; its signing
   secret differs too.
4. Put the secret key and the signing secret in Secret Manager and mount them
   into the Cloud Run revision, as in
   [contribute.md](contribute.md#cloud-setup-deployment-only). Neither belongs
   in the image.
