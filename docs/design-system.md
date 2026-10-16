# Fashion storefront and native Django admin

The current frontend uses a light plum-and-coral identity. It replaces the previous ink-blue design while preserving the Persian locale, actual catalogue data, native Django forms, permissions, payment integration and Docker architecture.

## Design references

[Digikala](https://www.digikala.com/) informed the prominent search, category access, readable previous/current prices, filters and purchase steps. Its branding, catalogue and exact layout were not copied.

Anthropic's official [frontend-design instructions](https://github.com/anthropics/claude-code/blob/main/plugins/frontend-design/skills/frontend-design/SKILL.md) were accessed from the official repository; the skill is not installed in this environment. The composition uses real product media, deliberate typography, distinct collection treatments and restrained motion. No stock photography, fabricated reviews or nonfunctional wishlist buttons were introduced.

## Tokens and typography

`assets/css/tokens.css` is the shared source for both stylesheets. The primary action is `#6D214F`, hover `#54173B`, accent `#F26B5B`, background `#F7F7F8`, surface white, body `#1D1B20`, secondary `#73717A`, border `#E8E5EA`. Success, warning, danger and info use the requested `#16805D`, `#B7791F`, `#D92D45`, `#2563EB`.

For small text on tinted surfaces, supporting text tokens deepen secondary, success, danger and warning colors to maintain AA contrast. Coral is an accent; white body text on a promotional gradient has a dark plum overlay. A badge always has a written state as well as a color.

Vazirmatn is self hosted at weights 400–800. Inter is self hosted for Latin content using the official [Inter font](https://github.com/rsms/inter), with its OFL license beside the source in `assets/fonts`. `scripts/copy_vendor.js` copies authored fonts and npm vendor assets into `static`, including inside the existing Docker assets stage. No CDN font requests or new application dependencies are needed.

The type scale is 12, 14, 16, 18, 24, 32, 40, 48 px. Spacing follows 4 px increments, with shared named tokens through 96 px. Containers reach 1440 px, with 16/24/32 px mobile/tablet/desktop gutters. Controls use 8 px, inputs/buttons 12 px, cards/dropdowns 16 px, prominent panels 24 px radii. Three subtle elevation levels distinguish ordinary surfaces, raised elements and overlays.

## Reusable components

| Component | Implementation and behavior |
| --- | --- |
| Buttons | `btn-primary`, `btn-secondary`, `btn-outline`, `btn-ghost`, `btn-danger`, `btn-icon`; visible focus and disabled states, 44 px minimum control height |
| Forms | `accessible_field.html`, `accessible_fields.html`, `field_feedback.html`; retain existing bound widgets, error/help associations and CSRF |
| Store chrome | `header.html`, `shop_links.html`, `footer.html`; native mobile `details` navigation, Escape to close, Ctrl/Cmd+K search shortcut |
| Products | `product_card.html`; real image, category, title, current/previous price, discount and stock; textual missing-image fallback |
| Collections | Editorial hero, category directory, selected grid, keyboard-scrollable arrivals rail and distinct discounted selection; controls disappear when no scrolling is needed |
| Catalogue | `catalog.html`, `filter_form.html`, `pagination_v2.html`; multi-value filter parameters retained during sorting and paging; invalid forms stay expanded on mobile |
| Product details | Real image gallery and native modal zoom, native select without JavaScript, progressive color/size radios, stock feedback and local quantity control |
| Account | `account_nav.html`, `account_form.html`, `address_entries.html`; desktop side navigation, wrapped mobile navigation, actual POST actions |
| Checkout | `checkout_steps_v2.html`, `order_summary.html`; address choices, actual totals and stable summary rail; single column on smaller screens |
| Orders | Status badges, receipts, payment table and timeline composed only of saved event dates |
| Payment/errors | `payment_result.html`, result symbols, persistent validation feedback, actionable empty/error states; standalone 500 page stays independent of database content |
| Admin | Native `admin/base.html` extension, permission-gated quick navigation, dashboard, sidebar, lists, filters, actions, forms, inlines, history and confirmation pages; optional local upload previews |

The admin stylesheet deliberately avoids Tailwind preflight and maps Django's supported CSS variables. Native JavaScript owns formsets, autocomplete, permissions selectors, calendar widgets and bulk actions. `admin-presentation.js` adds local image previews, keyboard-scrollable table regions and accessible names derived from native field labels/table headings.

## Responsive behavior and motion

Review widths: 360, 640, 768, 1024, 1280 and 1536 px. The header places search on its own mobile row. Catalogue filters collapse only without validation errors. Transactional rails become full-width blocks. Tables and product rails scroll within labelled regions rather than expanding the document. Logical properties maintain RTL; Latin phone numbers and order identifiers use isolated LTR spans.

Motion uses 150/220/320 ms and `cubic-bezier(.2,.8,.2,1)`. Card media transitions respond to hover, and menus/dialogs respond to opening. There is no parallax, auto-rotating carousel or simulated skeleton state. Reduced motion removes transitions/animations and makes rail controls scroll instantly.

## Building and verification

Run `npm run build` after editing sources. Generated `static/css/app.css` and `static/css/admin.css` are committed; do not hand-edit them. Existing backend regressions are run without changes to Python test files. Optional `scripts/verify_browser.cjs` and `scripts/verify_accessibility.cjs` use Playwright and axe-core installed outside this project's dependency tree, with explicitly isolated `/tmp` fixtures. The accessibility audit respects the response CSP nonce and does not weaken application policy.

See `docs/frontend-review.md` for executed checks, limitations and review evidence for this frontend-only iteration.
