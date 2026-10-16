# Fashion frontend review — 2026-10-15

This iteration replaces the previous blue presentation with the requested light plum-and-coral fashion identity. It changes templates, CSS, authored JavaScript, fonts, frontend build copying, browser verification and documentation only. Python, models, forms, views, URLs, migrations, authentication, dependencies/lockfiles, Docker files and Git configuration were not changed by this iteration. Backend changes described in the historical [redesign review](redesign-review.md) belong to earlier commits and are outside this iteration.

## Delivered interfaces

| Area | Coverage |
| --- | --- |
| Shared storefront | Search-focused header, real navigation, native mobile menu, account/cart links, footer, notifications, bound field errors, empty states, pagination and consistent product cards |
| Product discovery | Home, catalogue, category directory/category results and search; editorial hero from actual product media, selected products, horizontally scrollable arrivals, discount showcase, filters, sorting and pagination |
| Product details | Actual gallery with modal enlargement, available colour/size combinations, stock and current/previous prices, quantity controls, description, shipping information and related products |
| Customer account | OTP login/verification, complete/edit profile, account overview, address list/add/edit/delete/default selection and POST logout |
| Purchase | Guest/customer cart, quantity/removal feedback and network-error recovery, address selection, order review, order history/receipt, saved order activity, payment redirect/mock gateway and success/failure/cancellation states |
| Errors | 400, 403, 404 and database-independent 500 presentation; existing CSRF handling retained |
| Native Django admin | Dashboard/app directory; login, password change; all registered model lists, available add/change forms, inline forms, filters, search, pagination, actions, history and permitted deletion confirmations. Products, categories, colours, sizes, variants, discounts, users, groups, addresses, OTP challenges, carts/items, orders/items and payments share the theme |

There are no existing wishlist, comparison, reviews, dedicated contact/FAQ or customer password-recovery pages to redesign. Contact links use configured telephone/email values; customer authentication remains OTP. Admin widgets, permissions, bulk actions, formsets and validation remain native Django behavior.

The shared [design system](design-system.md) documents tokens, self-hosted Vazirmatn/Inter, layout, components and progressive interactions. Mobile search gets its own row, catalogue filters remain open on invalid submissions, account navigation wraps, transactional layouts become a single column and wide admin tables scroll within keyboard-accessible regions. Gallery zoom returns focus on dismissal, animations respect reduced motion and missing media gets a textual fallback. Admin upload previews remain local object URLs; files are submitted through the original input. Final screenshot review also removed stretched empty space from shorter admin directory modules.

Anthropic's official frontend-design instructions were accessed from its official repository; the skill is not installed locally. Digikala informed discovery/navigation usability without copying its branding or exact layout. No product data, photography, reviews, payment outcomes or customer records were fabricated for the application.

## Executed verification

Results describe the combined working tree, including the user's existing uncommitted formatter/template changes.

| Command/check | Observed result |
| --- | --- |
| `PYTHONPATH=scripts/qa DJANGO_SETTINGS_MODULE=shop_test_settings .venv/bin/python manage.py test --noinput` | **260 tests passed**, no test assertions changed in this iteration |
| Same settings: `manage.py check` | No issues |
| Same settings: `manage.py makemigrations --check --dry-run` | No changes detected |
| `npm run build` | Storefront/admin CSS and vendor/font copying succeeded |
| `npm ls --depth=0` | Installed dependencies consistent; no application dependency or lockfile edits |
| `uv lock --check --offline` | Passed |
| `node --check` for shop/admin JavaScript and both browser scripts | Passed |
| `PYTHONPATH=scripts/qa DJANGO_SETTINGS_MODULE=shop_static_settings .venv/bin/python manage.py collectstatic --noinput` | **144 files copied, 420 post-processed** with compressed manifest storage; final CSS refinement: 1 copied, 143 unchanged, 381 post-processed |
| Django test client with `DEBUG=False` and manifest storage | Home, catalogue, customer login and admin login: 200; real missing route: custom 404 |
| `docker compose config --quiet` | Development/override configuration passed |
| `docker compose -f docker-compose.yml config --quiet` | Production configuration passed |
| Latest `docker compose -f docker-compose.yml build web` | Blocked: Docker daemon socket `/home/admin_org/.docker/desktop/docker.sock` absent; default `/var/run/docker.sock` also absent. Image build/run could not be repeated in this environment |
| `scripts/verify_browser.cjs` | **582 visits**, 97 route/role combinations at **360, 640, 768, 1024, 1280, 1536 px**; **zero recorded overflow, unexpected HTTP status, failed asset/request, console or page errors** |
| Browser interaction assertions | Passed: skip link, search shortcut, keyboard menu, arrivals controls, filter validation/sort parameters, gallery/Escape/focus return, variants/quantity/add, HTMX update/remove/count and network recovery, invalid login, address create/delete, checkout/mock callback, admin publication/formset/local image preview/login, reduced motion and purchase without JavaScript |
| `scripts/verify_accessibility.cjs` | **182 route/role/viewport cases**, **zero axe violations** under WCAG 2 A/AA, 2.1 AA and 2.2 AA tags; automated incomplete checks still require human review |
| Visual review | Mobile/desktop home, product, authentication, checkout and native admin dashboard/product/group forms inspected using rendered screenshots |
| Admin layout refinement follow-up | Rebuilt admin CSS; **15 further visits** across dashboard, app directory, product list/add and group add at 360/768/1536 px, zero recorded overflow/resource/console/page errors; updated desktop dashboard screenshot inspected |
| `git diff --check` | Passed |

Browser/axe testing used Chrome with Playwright, with optional Playwright/axe-core installed under `/tmp/shop-browser`, outside application dependencies. Explicitly labelled synthetic shirts, customer/admin sessions and mock payments existed only in the guarded `/tmp` QA database/media directory. No fixture imagery or records were copied into application data. The tests exercise the original server-side mock service; they do not claim real settlement.

Local reports and screenshots for this run are in the ignored `node_modules/.cache/shop-review/` directory (`browser/report.json`, `browser/*.png`, `accessibility.json`, build/server logs). They are reproducible evidence, not release assets; they can disappear when dependencies are reinstalled. Session credentials remain outside Git in the mode-0600 `/tmp/shop-qa-auth.json` file.

## Reproduce browser checks

```bash
npm run build
npm install --prefix /tmp/shop-browser --no-audit --no-fund playwright axe-core
PYTHONPATH=scripts/qa .venv/bin/python scripts/qa/setup.py
PYTHONPATH=scripts/qa DJANGO_SETTINGS_MODULE=shop_qa_settings \
  .venv/bin/python manage.py runserver 127.0.0.1:8765 --noreload
```

In another terminal, run the read-only accessibility audit before the interaction suite, since checkout deliberately empties the temporary customer cart:

```bash
NODE_PATH=/tmp/shop-browser/node_modules node scripts/verify_accessibility.cjs
NODE_PATH=/tmp/shop-browser/node_modules node scripts/verify_browser.cjs
```

Use `SHOP_QA_CHROME` for another installed Chrome executable, `SHOP_QA_OUTPUT` for browser output and `SHOP_QA_A11Y_OUTPUT` for the axe report. Restart the QA server after template changes; recreate fixtures before repeating purchase tests. Fixture setup refuses to use any database other than `/tmp/shop-browser.sqlite3`.

## Git preservation and staging readiness

The 15 previously modified project files and unrelated parent-repository work were preserved. Existing modified page templates are inherited by compatibility templates. Only our theme-color and body-background hunks are committed from `templates/base.html`; its original user edits remain unstaged. The original formatter and Python test changes are also excluded. A release checkout must retain the user's applicable original changes: test results above describe the combined workspace, not an independent clean checkout stripped of that work.

Milestone commits use the verified existing author and committer **Ehsan &lt;ehsanmehram361@gmail.com&gt;**. No `Co-Authored-By` trailers, push, amend, history rewrite or Git configuration changes were added.

The frontend is ready for staging review. Remaining checks include a fresh Docker build/run when its daemon is available, MariaDB-backed runtime/concurrency, deployed HTTPS/media behavior, actual catalogue photography/copy, manual assistive-technology review (including axe incomplete checks), and actual SMS/payment integrations. Automated accessibility checks do not certify complete WCAG compliance. Docker architecture and persistent storage remain intact; no database volumes were deleted.
