# Answering a privacy request

The page's privacy section tells people to write to info@moritzmarcus.com to
see, correct or delete their data. This is what to do when someone does. Answer
within a month (GDPR Art. 12(3)); keep a short note of what was asked, when, and
what was done, and nothing more.

Nobody needs to ask for an idle account to go: one unused for two years is
deleted with its sign-in by the daily run in `backend/app/retention.py`. This is
for everything sooner than that.

Everything below runs from `backend/` against the live project. `$EMAIL` is the
address the request is about, `$UID` the account id the first step finds.

```sh
PROJECT=koalitionsberegner
DATABASE=main
```

## 1. Make sure it is them

Only act on a request sent from the account's own address, or answered from it
after you write back. Anyone can type someone else's address into an email.

## 2. Find what there is

The account, by address. Addresses are stored as the sign-in gave them, so try
the lower-case spelling too if nothing comes back:

```sh
uv run python - "$EMAIL" <<'PY'
import sys
from google.cloud import firestore

db = firestore.Client(project="koalitionsberegner", database="main")
query = db.collection("accounts").where(filter=firestore.FieldFilter("email", "==", sys.argv[1]))
for doc in query.stream():
    print(doc.id, doc.to_dict())
PY
```

The Firebase sign-in record (address, when it was created, last sign-in):

```sh
curl -s -X POST "https://identitytoolkit.googleapis.com/v1/projects/$PROJECT/accounts:lookup" \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "x-goog-user-project: $PROJECT" -H "content-type: application/json" \
  -d "{\"localId\": [\"$UID\"]}"
```

Imports still linked to the account. A finished import no longer names anyone,
so these are only ones running or waiting for the person to save or discard:

```sh
uv run python - "$UID" <<'PY'
import sys
from google.cloud import firestore

db = firestore.Client(project="koalitionsberegner", database="main")
query = db.collection("import_jobs").where(filter=firestore.FieldFilter("owner", "==", sys.argv[1]))
for doc in query.stream():
    data = doc.to_dict()
    print(doc.id, data.get("status"), data.get("query"), data.get("started_at"))
PY
```

In Stripe, the customer is the account's `stripe_customer_id`: its address,
subscriptions and invoices are on the customer's page in the dashboard.

Active-account markers are a hash of the uid per day and expire by themselves
after 62 days; step 4 deletes them early.

## 3. Access or a copy

Send what steps 2 found: the account document, the Firebase record, any linked
imports, and the Stripe customer with its invoices (the dashboard exports them).
Plain JSON is a fine format for data portability.

## 4. Deletion

In this order, so nothing is left pointing at an account that is gone:

1. **An active subscription** is cancelled first — ask them, since it ends what
   they paid for — in the Stripe dashboard.
2. **Linked imports:** clear `owner` on the jobs step 2 listed (or delete jobs
   still awaiting confirmation; that is what discarding does).
3. **Active-account markers:**

   ```sh
   uv run python - "$UID" <<'PY'
   import sys
   from datetime import date, timedelta
   from google.cloud import firestore
   from app.usage import ACTIVE_RETENTION_DAYS, account_marker

   db = firestore.Client(project="koalitionsberegner", database="main")
   marker, today = account_marker(sys.argv[1]), date.today()
   for n in range(ACTIVE_RETENTION_DAYS + 2):
       day = (today - timedelta(days=n)).isoformat()
       db.collection("usage_daily").document(day).collection("active").document(marker).delete()
   PY
   ```

4. **The account document:** `accounts/$UID` in the Firestore console, or
   `db.collection("accounts").document(uid).delete()`.
5. **The sign-in:**

   ```sh
   curl -s -X POST "https://identitytoolkit.googleapis.com/v1/projects/$PROJECT/accounts:delete" \
     -H "Authorization: Bearer $(gcloud auth print-access-token)" \
     -H "x-goog-user-project: $PROJECT" -H "content-type: application/json" \
     -d "{\"localId\": \"$UID\"}"
   ```

6. **Stripe stays.** Payment records are kept for 5 years from the end of the
   financial year under the Danish Bookkeeping Act, which the privacy section
   says. Tell them so in the reply.

Logs in Cloud Logging and Cloudflare expire on their own within 30 days, and
public election-request issues on GitHub never named anyone.

## 5. Correction

An address is changed in Firebase (`accounts:update` with `localId` and the new
`email`); the account document picks it up at the next sign-in. Change the
Stripe customer's address in the dashboard too.
