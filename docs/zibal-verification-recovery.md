# Zibal verification incident and production recovery

This change is prepared and tested in the local workspace. The owner explicitly
retained responsibility for transferring it to production. No live payment,
order, inventory or refund record was modified during this work. The production
commands below are an operator runbook, not a record of executed recovery.

## A. Root cause and correction

`ZibalGateway._verified_result` required `type(refNumber) is int`. The incident's
Inquiry responses contained positive ASCII numeric strings, so valid payments
raised `PaymentGatewayError` and the callback's atomic transaction rolled back.

The validator now accepts an exact positive Python integer below `10**64`, or
a string matching `[0-9]{1,64}` whose numeric value is positive. Booleans, zero,
negative values, floats, whitespace, signs, decimal points, Unicode digits and
oversized references remain invalid. Numeric strings retain their leading
zeros; integers become strings. The existing 64-character database field fits
both formats and needs no migration.

The audit also reproduced a separate defect: an order past `expires_at` could
be marked paid before the expiry job cancelled it. `mark_order_paid` now refuses
that order. The existing callback service records the verified receipt with
`needs_refund=True`, preserves the order deadline/state and reserves fulfillment
for eligible orders. The expiry job continues to handle cancellation and stock
release; the correction does not manually revive or cancel orders.

## B. Files and behavior

| File | Change and purpose |
| --- | --- |
| `payments/gateways/zibal.py` | Strictly accept both reference formats without relaxing amount, order, track ID, status or timestamp checks. |
| `orders/services.py` | Reject elapsed order deadlines, including the interval before the expiry job runs. |
| `payments/tests.py` | Exercise both formats, invalid references, Verify/Inquiry errors, chronological recovery, refunds and idempotency through actual services and mocked HTTP. |
| `scripts/qa/zibal_fixtures.py` | Return a string reference in the offline browser fixture. |
| `payments/management/__init__.py`, `payments/management/commands/__init__.py` | Register the recovery management command. |
| `payments/management/commands/reconcile_zibal_payments.py` | Inspect all order payments; independently validate Inquiry evidence; optionally replay selected payments after backup validation. |
| `scripts/backup-production-db` | Create a private, unique, consistent compressed dump through `scripts/compose-production`; reject dump/compression failures and produce a SHA256 sidecar. |
| `.gitignore` | Keep compressed database dumps, partial dumps, sidecars and incident reports out of Git. |
| `docs/zibal-payment.md`, this document | Link the correction to the original gateway report and document verified local results and operator recovery. |

No Payment model, migration, callback URL, gateway configuration, frontend,
Enamad file or production Compose configuration was changed.

## C. Automated validation

All gateway responses in automated tests are simulated. No real track ID is
used to initiate a gateway request. Databases are isolated from shop data.

| Run | Discovered | Passed | Skipped | Failed |
| --- | ---: | ---: | ---: | ---: |
| Existing payment baseline, SQLite | 82 | 79 | 3 | 0 |
| Complete final Django suite, SQLite | 335 | 332 | 3 | 0 |
| Final payment and order suites, MariaDB 11.4.13 | 148 | 148 | 0 | 0 |

The SQLite skips are the three payment row-lock concurrency tests. They all
executed successfully on a new MariaDB instance with networking disabled and
a private Unix socket/data directory under `/tmp`. MariaDB test database
creation explicitly used `utf8mb4`; its initial default Latin1 configuration
failed during permission creation before tests ran and was corrected.

Before applying the fix, these two regression tests reproduced the defects:

- `CallbackTests.test_numeric_string_reference_is_persisted`
- `CallbackTests.test_success_after_deadline_before_expiry_job_requires_refund`

The final suites have no failed tests. The reference rejection test includes
every requested invalid value and additional Unicode, float, object and integer
length cases on both direct Verify success and `201`/Inquiry recovery. String
references are now the default mocked successful response. Tests also cover
`202` handling, forged callbacks, repeated callbacks, three actual simulated
charges with one fulfillment and two refund flags, previously paid/cancelled/
expired orders, transport/JSON failures and sensitive-error redaction.

Recovery tests cover chronological selection, local-creation-time ties,
existing primary receipts, mixed timestamp formats, other gateway-paid attempts,
invalid backups, partial failures and changes between preflight and replay.
Backup tests use a stub Docker executable and synthetic SQL only.

The Django system check, `makemigrations --check --dry-run`, shell syntax and
`git diff --check` passed. There are no database migrations.

The complete isolated SQLite suite can be repeated with:

```sh
PYTHONPATH=scripts/qa .venv/bin/python manage.py test --settings=shop_test_settings --noinput
```

Do not run the tests using production settings or the production database.

## D. Real transaction evidence and outstanding recovery

Order: `ORD-20261114-9BQ4H2`.

| Track ID | Incident-reported local status | Incident-reported Inquiry | Amount/order matching | This work's live recovery |
| --- | --- | --- | --- | --- |
| `4911955398` | Pending | `result=100`, `status=1`, numeric string reference, `paidAt` present | Both true | Not executed |
| `4911974825` | Pending | `result=100`, `status=1`, numeric string reference, `paidAt` present | Both true | Not executed |
| `4911986194` | Pending | `result=100`, `status=1`, numeric string reference, `paidAt` present | Both true | Not executed |

The evidence above was supplied with the incident. It was not re-queried with
live merchant credentials here. Actual amounts, reference values, gateway
payment times, current statuses, primary selection and refund flags remain to
be read on production. No primary transaction was selected from local IDs or
callback arrival order, and no real refund was requested or confirmed here.

## E. Order, inventory and architecture audit

The order's exact production status, payment status, payment history and stock
were not read. Tests confirm that `create_order` reserves stock once: one unit
reduces the fixture's stock from 5 to 4. Processing three verified payments and
repeating their callbacks leaves stock at 4, calls `mark_order_paid` once and
creates exactly one primary receipt plus two refund flags. A cancelled order's
released stock remains 5 and all additional receipts require refund review.

The existing lifecycle is preserved:

- The backend order supplies the amount. Only the gateway adapter multiplies
  Toman by 10 for Rial. Payment credentials remain in backend settings.
- A validated `trackId` is stored as the unique `authority`. The payment URL is
  constructed on the official Zibal origin, and the configured callback URL is
  used independently of client host/amount data.
- The unauthenticated GET callback checks four required parameters, duplicate
  values, track ID bounds, statuses and the order relation. Callback success
  never establishes payment success.
- Verify `100` needs fully matching verified details. Verify `201` needs an
  independently valid Inquiry `100`/status `1`. Verify `202` also uses Inquiry;
  changed paid states remain retryable instead of being guessed successful.
- HTTP requests retain configured finite timeouts, TLS, redirect protection,
  safe error messages and a narrow audit whitelist without merchant/card data.
- Each callback locks Order then Payment and commits atomically. Successful
  repeated callbacks perform no new verification/fulfillment. Additional paid
  attempts are tracked with `needs_refund`, and errors roll back local writes.
- Success/failed/cancelled result pages read the actual order payment status
  and enforce ownership. A forged return or success-page URL does not display
  an unverified success.

`Payment.paid_at` remains the existing local processing timestamp. Recovery
ordering uses the independently validated gateway `paidAt`, retained separately
in the private recovery report. Offset-free gateway timestamps are compared on
the same API clock. Mixed offset-bearing/offset-free values block application
unless the operator supplies a confirmed `--gateway-timezone`; no timezone is
silently invented.

The before/after stock snapshot checks changes during recovery. It cannot by
itself establish historical stock movements or attribute simultaneous purchases
of the same product; preserve the snapshots and investigate any difference.

## F. Production deployment and recovery runbook

Production deployment, a new production backup, live revalidation, recovery and
container/log inspection remain operator actions. An initial read-only public
liveness request returned the expected plain-text `ok`; that endpoint deliberately
does not query the database and does not establish container or Redis health.
The earlier healthy service states in the incident are historical reports.

### Preserve the server repository

In `/root/Online-Clothing-Store`, record branch, commit, staged/unstaged file
names and stash metadata before importing the mission changes:

```sh
git status --short --branch
git log -1 --format='%H %s'
git diff --name-only
git diff --cached --name-only
git stash list --format='%gd %h %s'
scripts/compose-production ps --all
```

Inspect relevant diffs privately. Preserve Enamad, CSP, site settings, all
stashes including `server-local-enamad-and-production-changes`, and the local
Nginx healthcheck modification in `docker-compose.production.yml`. Import only
reviewed mission changes. Do not reset the working tree, blindly pull over it,
pop/drop stashes or stage unrelated production configuration. Leave
`.env.production` intact and never print its contents or rendered Compose secrets.

Read the initial financial state using the existing image before deployment:

```sh
scripts/compose-production exec -T web python manage.py shell -c '
import json
from orders.models import Order
from payments.models import Payment
order = Order.objects.get(order_number="ORD-20261114-9BQ4H2")
track_ids = ["4911955398", "4911974825", "4911986194"]
selected = list(Payment.objects.filter(authority__in=track_ids).values("authority", "order_id", "gateway"))
assert len(selected) == 3 and all(p["order_id"] == order.pk and p["gateway"] == "zibal" for p in selected), "Payment membership mismatch; stop recovery."
print(json.dumps({
    "order": Order.objects.filter(pk=order.pk).values("id", "order_number", "total", "status", "payment_status", "expires_at", "paid_at").get(),
    "payments": list(order.payments.order_by("created_at", "pk").values("id", "authority", "gateway", "amount", "status", "reference_id", "transaction_id", "callback_count", "order_id", "created_at", "verified_at", "paid_at", "needs_refund")),
    "inventory": list(order.items.values("id", "variant_id", "quantity", "variant__stock")),
}, default=str, ensure_ascii=False, indent=2))
'
```

This shell command reads records only. Keep the output in private incident
storage and include every payment for the order in the review.

### Create and verify a new backup

```sh
scripts/backup-production-db /root/minikandoo-backups
```

The script preserves `/root/minikandoo-backups/db-20261114-190647.sql.gz` and
creates a unique `db-<UTC time>-<random>.sql.gz` with mode 0600. It uses a
consistent MariaDB dump, checks gzip integrity and writes a SHA256 sidecar.
A failed dump stays marked `.partial` and cannot be used as a completed backup.
Run `sha256sum --check <new-backup>.sha256` and retain the actual new filename.
No password is passed as a command argument.

The apply command additionally checks decompression/CRC, the exact configured
database header, Payment/Order/ProductVariant table definitions, a completion
footer and a modification time within the last hour. These are readability
checks, not a claim of a completed restore drill. If deployment takes more than
an hour, create another fresh backup before replay; preserve both older files.

### Build and deploy only the corrected web application

```sh
scripts/compose-production config --quiet
scripts/compose-production build web
scripts/compose-production up -d --no-deps web
scripts/compose-production exec -T nginx nginx -t
scripts/compose-production exec -T nginx nginx -s reload
scripts/compose-production ps --all
scripts/compose-production exec -T web python manage.py check --deploy
```

Keep `MIGRATE_ON_START=0` and `SEED_ON_START=0` as in the existing production
configuration. No migration is required. Verify web/Nginx/database/Redis health,
public `/healthz/` and `/readyz/`, and relevant sanitized payment errors. Confirm
the container's mission source files match the intended Git version; `.git` is
excluded from the image, so source hashes must be compared independently. Do
not claim the deployment corresponds to the mission commit without this check.

### Inspect authoritative evidence without local writes

```sh
scripts/compose-production exec -T web python manage.py reconcile_zibal_payments \
  --order-number ORD-20261114-9BQ4H2 \
  --track-id 4911955398 4911974825 4911986194
```

The default mode reads all order payments and calls Inquiry only. It validates
amount in Rial, local amount versus order total, order/optional track ID, verified
status, reference and payment timestamp. It revalidates prior successful
receipts and reports other attempts. It emits selected financial fields rather
than raw API responses, credentials or customer/card details.

Review `gateway`, `blocking_issues`, `gateway_time_order` and `planned_primary`.
An existing paid order must have one independently validated primary receipt.
A mismatched prior ledger, historical gateway needing independent review,
uncertain unselected history, a locally terminal cancelled paid transaction or
another gateway-verified attempt needing explicit selection stops application.
Never force fields to resolve those discrepancies. A selected transaction that
fails fresh validation is excluded and reported for review.

### Replay sequentially through server-side verification

Replace `NEW_BACKUP_FILENAME.sql.gz` below with the actual fresh backup name;
the placeholder is intentionally not an existing or historical backup. Use
the one-off container as root only to read the mode-0600 host backup. Override
the entrypoint to avoid migrations, static collection or seeding. The directory
is mounted read-only and the application still uses its configured database user.

```sh
umask 077
set -o noclobber
scripts/compose-production run --rm --no-deps --user 0:0 --entrypoint python \
  --volume /root/minikandoo-backups:/run/minikandoo-backups:ro \
  web manage.py reconcile_zibal_payments \
  --order-number ORD-20261114-9BQ4H2 \
  --track-id 4911955398 4911974825 4911986194 \
  --apply --backup /run/minikandoo-backups/NEW_BACKUP_FILENAME.sql.gz \
  > /root/minikandoo-backups/zibal-recovery-20261114-after.json
```

Use a new report filename for each run. Keep reports private and outside Git.
The command performs a fresh Inquiry preflight again, sorts validated receipts
by gateway `paidAt` then local creation time, and calls `process_callback` with
each receipt's own original-shaped callback. Verify and any required Inquiry
execute again inside the service. There are no direct assignments to financial
statuses and no new payment requests or refunds.

Each replay has its own atomic transaction. A network/verification failure
leaves that receipt retryable and does not roll back another receipt's valid
processing. Such a run exits nonzero with a sanitized partial report. The
actual primary can differ from the planned primary if the earliest replay fails
or an existing callback finishes first; preserve the actual ledger and investigate
the difference. Repeated runs preserve successful receipts and stock.

Check `after`, `actual_primary`, `successful_count`, `refund_required`,
`inventory_unchanged` and each replay's stored Verify result and reference.
Stored `amount` fields are **Toman**; validated `gateway.amount_rial` fields
are **Rial**. If the order is payable and has no prior primary, expect one
primary and two refund flags. If already paid, all three new charges need refund
review; if cancelled/expired, all three do and the order remains unfulfilled.
These are conditional expectations, never instructions to force database state.

Reinspect all order/payment records and service health/logs after replay. Preserve
the private before/after reports, actual backup path/checksum and gateway evidence.

## G. Git and preservation

The initial local checkout was clean on `main` at
`d25ce7e6ed321952e9f6245dc9acdaa4075e8c8e`, with no staged changes or local stashes.
The reported production modifications/stash were not inspected or altered here.
Only mission source, tests, tools and documentation belong in this commit; the
final response records the verified commit hash, file list and push state.
Check the final commit using `git show --stat HEAD` and `git status --short`.
Production synchronization and deployment correspondence remain unverified until
the owner executes and checks the runbook.

## H. Refunds and remaining owner actions

The three live transactions have not been revalidated or recovered by this local
work. Create a new production backup, deploy the corrected source, run inspection
and verified replay, and record actual order/stock/container results. The tool
identifies refund-required receipts and their reference/amount, but performs no
refund and records no refund completion.

The [official Zibal refund service page](https://zibal.ir/ipg/refund), inspected
for this incident, describes full/partial return to the original paying card
through the merchant panel or its separately enabled service. It requires
corporate banking setup/activation. The normal IPG OpenAPI lists Verify/Inquiry
but no refund endpoint; this change does not invent one. Confirm the merchant's
enabled official workflow or contact Zibal support before issuing a refund.
No customer card information is needed by the described service.

After explicit authorization, distinguish refund **required**, **requested** and
**confirmed by Zibal**. A `needs_refund=True` record establishes only a required
refund review. Neither local flags nor a refund request prove completed transfer;
retain actual dashboard/service confirmation before recording completion.
