# Redesign implementation and verification — 2026-10-14

The shop remains Django/Tailwind 4/HTMX/Alpine, Persian and RTL, with self-hosted Vazirmatn. Anthropic's official frontend-design instructions were read; the direction and components are documented in [design-system.md](design-system.md). No production product, customer, payment or inventory data was changed. Browser fixtures and generated sample images exist exclusively in an isolated SQLite database and media directory under `/tmp`.

## Route and interface inventory

| Existing area | Reviewed and redesigned coverage |
| --- | --- |
| Discovery | `/`, `/store/`, `/categories/`, `/store/category/<slug>/`, `/search/`, `/product/<slug>/`; search, same-variant filters, sorting, pagination, images, stock and unavailable selections |
| Account | `/accounts/login/`, `/accounts/verify/`, `/accounts/complete-profile/`, `/accounts/profile/`, `/accounts/profile/edit/`; OTP resend, logout, validation and guest-cart merge |
| Delivery addresses | Address list, creation, editing, deletion and default-address selection |
| Cart | Guest/customer cart, quantities, removal, totals, empty state, loading, HTMX feedback, network-error recovery and HTML fallback |
| Purchase and orders | `/checkout/`, `/checkout/review/`, `/orders/`, `/orders/<number>/`, payment retry, actual transaction history, shipping/tracking and reservation expiry |
| Payments | Redirect, development mock gateway, signed callbacks, truthful success/failure/cancellation pages; success is never inferred from the requested result URL |
| Errors | Persian 400/403/404/500 and CSRF failure; 500 renders without database-dependent context processors |
| Native admin | Dashboard/app directories, login/logout/password change; groups, users, addresses, OTP challenges, products, categories, colours, sizes, variants, discounts, carts, orders/order items and payments; lists, search/filter/sort, pagination, formsets, available add/change forms, history, allowed deletion confirmations and bulk actions |

The project has no existing wishlist, comparison, dedicated contact/FAQ, customer password-recovery or separate custom CMS/reviews application. Contact links use configured telephone/email values. Customer authentication remains OTP; native admin password workflows remain available.

## Functional and quality changes

- Product JSON uses actual arrays with Django `json_script` escaping. Selecting colour and size resolves a real variant; quantity buttons no longer submit an incomplete product purchase. The gallery, filters, sorting and cart controls work, with a usable purchase path when JavaScript is disabled.
- Combined colour/size/stock filters match one variant, Persian category slugs validate, and category URLs cannot be replaced by a query-string category.
- The guest session key is retained before authentication rotates the session, so its cart is merged correctly. Cart mutations and checkout lock the cart row in the same order. Existing price calculation, reservation, snapshots, atomic stock decrement and payment verification remain server-side.
- Empty POST forms bind and display errors. Address provinces have actual selectable values. Malformed quantities/address identifiers do not produce misleading success or server errors. OTP validation respects the configured code length.
- Failed-payment reservations are released when due, once; expired/cancelled late callbacks still follow the existing refund workflow.
- Staff flags alone no longer expose other customers' order/payment details or dashboard statistics. Forwarded IPs are accepted only from configured `TRUSTED_PROXY_IPS`; the optional setting is passed through Compose and documented in `.env.example`.
- User administration uses Django's native hashed-password forms, password changes and permission widgets. Delegated staff cannot edit/delete privileged users or grant themselves privileges through the user form. Admin order forms reject invalid transitions and manually fabricated payment success.
- Fixed Django 6.1 `format_html()` crashes for out-of-stock rows. Admin cart/customer/item reads and category/product counts avoid per-row queries; cart/review totals reuse already-loaded items. Best sellers count sold units.
- Upload validation reads up to the existing 3 MB allowance rather than truncating valid PNGs at 2 MB, and treats Pillow decompression bombs as validation errors.
- Shared colours/type/spacing/components, visible focus, meaningful labels/error associations, 44 px storefront controls, reduced motion, directional isolation, responsive admin widgets/table scrolling, progressive native mobile navigation, and accurate empty/payment/error states replace the previous decorative layouts.

## Preserving the initial working tree

There were 15 pre-existing modified project files, plus unrelated modifications elsewhere in the parent Git repository. Their original changes were retained and excluded from our commits. Existing modified page templates are inherited by new compatibility templates that override their content; their files were not overwritten. Only our favicon, theme colour and accurate description hunks were staged from the already-modified base template. This lets the current work coexist with the user's Persian formatting, pagination and message changes.

The four baseline failures in `common.tests.TemplateTagTests` expected Latin digits despite the existing Persian formatter. Their exact expected values were updated to Persian; assertions were not removed or weakened. `store/tests.py` and the formatter remain the user's uncommitted work. Validation results below describe the combined working tree. Preserve/include that original work when preparing a release checkout.

## Executed checks

| Check | Result |
| --- | --- |
| Django full test suite using isolated SQLite settings | 260 tests passed, including 47 new regression tests in `common/test_redesign.py` |
| Django `check` | Passed, no issues |
| `makemigrations --check --dry-run` | Passed, no model/schema changes requiring migrations |
| `npm run build` and `npm ls --depth=0` | Passed; no dependency/lockfile changes |
| `uv sync --check --offline --no-dev`, `uv lock --check --offline` | Passed; environment and lock consistent |
| Development `collectstatic --noinput` | Passed, 140 source static files |
| Compressed manifest `collectstatic --noinput` | Passed, 140 copied and 412 post-processed; hashed storefront/admin CSS, shop JS and favicon resolve |
| Rendering with `DEBUG=False` and manifest storage | Homepage, catalogue, customer login and admin login returned 200; real missing route used the custom 404 |
| Both default and production-only `docker compose ... config --quiet` | Passed |
| `docker compose -f docker-compose.yml build web` | Attempted; blocked by missing Docker daemon socket `/home/admin_org/.docker/desktop/docker.sock` |
| Production `manage.py check --deploy` | Attempted again outside the sandbox; blocked by unavailable MySQL at `127.0.0.1` |
| Browser layout/resource sweep | 582 visits across 97 route/role combinations at 320, 390, 430, 768, 1024 and 1440 px: zero horizontal-overflow, HTTP-status, resource or console issues |
| Browser interactions | Passed separately after correcting the test's assumption that publication uses a success-level message (the native action uses info); keyboard skip/menu, gallery/variants/quantity, HTMX update/remove/count, interrupted-network recovery, form errors, address creation/deletion, mock checkout success, bulk publication, native admin password login and purchase with JavaScript disabled |
| Visual inspection | Desktop/mobile screenshots of home/product/authentication, complete-profile and checkout forms, dashboard/product/admin-group forms and custom 404 reviewed; layout defects found were corrected |
| JS syntax and Git whitespace checks | Passed |

Browser sweeps intentionally render test-only error endpoints from the QA URLconf to test real templates even while mock payments require `DEBUG=True`. These endpoints are absent from the application URLconf. Payment states were produced through the existing mock service and signed callbacks in the temporary database.

Docker services, networks, persistent volumes, entrypoint, reverse-proxy assumptions, health checks and database configuration remain intact. Necessary build fixes install the already-locked production extra (Gunicorn) and include authored JS/favicon in the asset stage; Compose only gains the optional trusted-proxy environment variable. No volume cleanup or destructive Docker commands were used.

## Reproduction

Run from the shop directory. QA settings always override the database/media/static paths to `/tmp`; fixture setup refuses any other database path.

```bash
PYTHONPATH=scripts/qa DJANGO_SETTINGS_MODULE=shop_test_settings .venv/bin/python manage.py test --noinput
PYTHONPATH=scripts/qa DJANGO_SETTINGS_MODULE=shop_test_settings .venv/bin/python manage.py check
PYTHONPATH=scripts/qa DJANGO_SETTINGS_MODULE=shop_test_settings .venv/bin/python manage.py makemigrations --check --dry-run
PYTHONPATH=scripts/qa DJANGO_SETTINGS_MODULE=shop_static_settings .venv/bin/python manage.py collectstatic --noinput
npm run build
docker compose config --quiet
docker compose -f docker-compose.yml config --quiet

PYTHONPATH=scripts/qa .venv/bin/python scripts/qa/setup.py
PYTHONPATH=scripts/qa DJANGO_SETTINGS_MODULE=shop_qa_settings .venv/bin/python manage.py runserver 127.0.0.1:8765 --noreload
# In a separate terminal, with optional Playwright installed under /tmp/shop-browser:
NODE_PATH=/tmp/shop-browser/node_modules node scripts/verify_browser.cjs
```

The QA session/password file is `/tmp/shop-qa-auth.json` with mode 0600 and is not committed. Playwright was installed only under `/tmp`, not added to application dependencies. The browser script defaults to the locally available Chrome `/opt/google/chrome/chrome`; use `SHOP_QA_CHROME` for another executable. `SHOP_QA_SKIP_LAYOUT=1` reruns only interactions; `SHOP_QA_OUTPUT` selects the report directory. Restart the `--noreload` server after Python changes. Repeat fixture setup before each complete purchase run.

Evidence from this session: `/tmp/shop-final-tests.log`, `/tmp/shop-assets-build.log`, `/tmp/shop-manifest-collectstatic.log`, `/tmp/shop-browser-layout-report.json`, `/tmp/shop-browser-results/*.png`, and `/tmp/shop-browser-interactions/report.json`. The layout report retains the subsequently corrected message-level assertion failure; its 582 page records have no layout/resource issues. The separate interaction report records the successful rerun.

## Review readiness and remaining limits

Ready for staging review of the combined workspace, with working local UI and regression coverage. Production approval still requires a real Docker build/run, MariaDB concurrency/locking checks, deployed HTTPS/proxy/media-serving checks, and the actual SMS/payment providers and credentials. Only console/memory SMS and the mock gateway are implemented in this repository. The existing process-local cache does not share rate-limit counters across Gunicorn workers; choose a shared cache before a multi-worker public launch. This work does not certify full WCAG compliance, assistive-technology behaviour or production payment settlement.

All new milestone commits use the verified author and committer `Ehsan <ehsanmehram361@gmail.com>`. No co-author trailers were added; no push, amend or history rewrite was performed. The original user changes intentionally remain uncommitted.
