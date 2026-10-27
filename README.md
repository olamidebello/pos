# Global POS

**Every store. One platform.** Platform and copyright attribution: **Olamide Olatayo Bello**.

Global POS combines location-based store discovery, store-bound inventory, a marketplace, tenant administration and hosted payment-gateway integrations. This is a pre-launch implementation, not a claim of production readiness or universal country/payment coverage. Live charging is disabled by default.

## Available interfaces

| Page | Purpose |
| --- | --- |
| `/` | Automatically suggest the nearest registered active store with location permission; manually search store addresses otherwise |
| `/marketplace.html` | Search published stores/products/categories, maintain a basket for one store, prepare hosted checkout, and inspect verified order status |
| `/admin.html` | Super admin and scoped staff administration |
| `/demo.html` | Public isolated demonstration with prefilled login |
| `/install.html` | Installable mobile web app instructions and supported-browser install prompt |
| `/terms.html`, `/privacy.html`, `/copyright.html` | Operator attribution, draft terms, data practices and ownership |

The logo, globe/store/phone artwork and install icons are in `web/assets/`. [Branding provenance and generation prompts](docs/branding.md) describe the built-in image generation used for the masters. AI-generated asset rights and third-party rights remain subject to applicable law; no trademark clearance is claimed.

## Tenant and store controls

Super admins can create/update/suspend tenants and stores, provision/disable/promote accounts, assign or revoke tenant/store roles, configure features, publish marketplace stores, manage payment gateways, issue/rotate/revoke integration keys and reconcile orders against provider results. The GUI includes resource selectors, tenant/store/user dropdowns, provider/mode choices, scope selection and one-time key reveal.

Tenant admins manage stores and staff memberships within their tenant. Store managers edit their assigned store, inventory and marketplace listing. Cashier, exit verifier and auditor roles currently provide scoped administration reads; dedicated transaction workflows for those roles remain pending. The full staff directory, gateway credential-reference management, key provisioning and global audit viewer require super admin.

Stores record name, address, coordinates, country, currency, timezone and language. The browser can capture store coordinates with permission, or operators can enter them manually. Country/currency input validates code shape; checkout supports only explicitly configured currency exponents. There is no arbitrary-address geocoder, translated interface pack or automatic exchange-rate conversion.

Inventory records belong to a store and contain barcode, name, available quantity and integer price in the store currency's minor unit. Address and coordinates are derived from the store on every read, so store moves automatically update inventory tags. Identical barcodes can have independent prices and stock in different stores. Store currency changes are refused while inventory exists, to avoid silently relabeling prices. Inventory quantities mean **available stock**, excluding pending-order holds.

`GET /api/stores?lat=...&lon=...` sorts active registered stores by distance. `GET /api/catalog?store_id=ID` shows that store's public inventory. The nearest registered store may be far away; its distance and address are shown, and shoppers can choose another. Manual store-address search runs in the browser against registered store records.

Global, tenant and store feature scopes use `global`, `tenant:ID` and `store:ID`. Features default off. A parent disable cannot be overridden by a child. `marketplace`, `payments` and `integrations` are enforced for new marketplace listings/checkout/API access. Existing orders can still be reconciled after new checkout is disabled. Other feature settings are reserved for future transactional modules and do not implement automatic payments, loyalty or offline checkout.

## Marketplace and payment gateways

Publish a store through the **marketplace** admin resource and enable the global `marketplace` feature. Enable `payments` and configure an enabled store gateway to offer checkout. Cross-store baskets are rejected. Server-side totals use the current inventory prices; client-supplied prices/totals are ignored. A 256-bit client checkout key provides order access and idempotency, so a retry does not create a second order or hold stock twice. Stock holds and order creation use one SQLite write transaction. Orders retain the seller name/address/coordinates and gateway configuration at creation.

Supported hosted adapters: **Stripe Checkout**, **Paystack Transaction Initialize/Verify**, and **Flutterwave v3 Standard/verify-by-reference**. Customers enter card/payment details on the provider's hosted page. The application sends only necessary order/customer fields to the selected provider and validates the returned checkout domain before presenting its link. Supported minor-unit exponents are explicitly listed in `gateways.py`; merchant eligibility and enabled currencies are still determined by the provider.

Browser redirects never mark orders paid. Verification runs on the server with the configured merchant secret, checking reference, amount, currency and successful payment status; Stripe and Paystack also verify the returned live/test mode. Verification is available from the customer order page and the admin **orders → Reconcile payment** action. No signed webhook or background reconciliation worker is included yet, so closed-browser/late payments require explicit reconciliation. Provider-initialization failures remain `initialization_uncertain` and retain their stock holds; do not retry by creating a different order or release uncertain holds without checking the provider. Pending holds are not automatically expired. Refunds, settlement accounting and physical exit-verification receipts remain pending.

Live payments require both a live-mode gateway and the private server flag `GLOBALPOS_ENABLE_LIVE_PAYMENTS=1`. Before enabling it, complete merchant onboarding, store participation acceptance, operator contact details, local tax/fiscal-receipt rules, refunds, reconciliation operations, log/data retention, monitoring and recovery validation. The current drafts are not a substitute for this work. No live provider transaction was executed during development checks.

Official adapter references: [Stripe session creation](https://docs.stripe.com/api/checkout/sessions/create), [Stripe fulfillment](https://docs.stripe.com/checkout/fulfillment), [Paystack transactions](https://paystack.com/docs/api/transaction/), [Flutterwave Standard](https://developer.flutterwave.com/docs/flutterwave-standard-1), [Flutterwave verification](https://developer.flutterwave.com/docs/transaction-verification).

### Provider secrets and GUI setup

The GUI stores **server environment reference names**, never raw merchant secret keys. For example, a Stripe test gateway can reference `GLOBALPOS_STRIPE_TEST_LAGOS`; the actual provider-issued key is installed in `/etc/pos-control/gateways.env` (root-only), loaded by systemd. The admin list reports whether a matching server key is present; this does not prove merchant-account validation. Mode-specific key prefixes are checked before outbound requests.

Use the private Ansible Vault workflow in [ansible/README.md](ansible/README.md) to configure credentials. Never paste secrets into chat, Git, public-key fields, names or descriptions. Existing order gateway/provider/reference bindings cannot be changed; create a new gateway for a different merchant credential reference and retain the old one for reconciliation.

### Scoped integration API keys

The super admin **api_keys** GUI issues a random key for one store, displays it once, and stores only its SHA-256 digest, prefix, scopes and expiry. Keys expire in 1–365 days. Rotation revokes the old key and issues a new one; revocation is immediate. API keys cannot administer users, gateways, roles or other keys. They cannot charge a customer.

| Scope | Endpoint | Behavior |
| --- | --- | --- |
| `inventory:read` | `GET /api/integration/inventory?store_id=ID` | Read the selected store inventory, including inherited address tags |
| `inventory:write` | `POST /api/integration/inventory` | Add/update inventory in the key's store only |
| `orders:read` | `GET /api/integration/orders?store_id=ID` | Read order IDs, totals and status for that store |

Use the `X-API-Key` header and enable the store's effective `integrations` feature. Keys are denied when expired/revoked, outside their store/scope, or when the store/tenant is suspended. Requests are limited to 120/minute per key; public checkout is limited to 10/10 minutes per origin and customer verification to 30/minute. The trusted Nginx proxy overwrites `X-Real-IP`; direct public access to the backend must remain disabled. Request buckets store hashes, not raw origin addresses, and expire automatically on later requests.

## Try demo

- Email: `demo@globalpos.example`
- Password: `GlobalPOS-Demo!2026`
- Role: super admin in an isolated demonstration workspace.

`/admin.html?demo=1` prefills the login. Each sign-in creates a fresh private database under `demo-sessions` beside the production database, with two sample tenants/stores, four products, published marketplace listings and local demo payment simulators. After login, use **Try the sample marketplace** to create a test order and simulate payment/cancellation. `demo_paid` is explicitly a test state, never a production payment receipt.

Visitors can test implemented controls, including gateway configuration and API keys, but demo routes never contact external payment providers, enable live mode or access production data. Tokens and API keys are bound to the separate database. The sample storefront link contains only an unguessable demo ID; private administration still needs a bearer token. Use fictional data only.

Sessions last one hour. Expired demo databases are removed at the next demo login. Limits: 30 concurrent workspaces, 500 administrative audit events and 50 orders per workspace. There is no background purge task. Each new login resets the workspace. Keep the public demo credentials out of real operator accounts.

## Install on phones and desktop

Global POS is an installable web app (PWA), not an APK, native iOS app or app-store listing. `/install.html` offers the browser prompt when supported and manual Android / iPhone / desktop steps. HTTPS (or localhost) is required. The manifest supplies standalone launch, home-screen icons and a store-finder shortcut.

The worker caches only an offline notice, stylesheet and icon. It never caches API results, private administration, inventory, prices, credentials or payment status. Offline navigation shows an explicit connection notice. Authentication stays in page memory; customer order-access keys use tab session storage so a hosted-payment return can reopen the order. Clearing that storage does not delete server records, and losing an order key requires merchant support/reconciliation.

## Agreements, privacy and ownership

`agreement_gate.py` provides version/hash acceptance records, store scoping and optional autopay withdrawal. Existing immutable drafts `store-v1`, `shopper-v1`, `autopay-v1` remain unchanged. Checkout records explicit current shopper-agreement acceptance; it does not infer marketing or automatic-payment consent. Do not modify accepted agreement versions in place. Current general terms and privacy notices are still pre-launch drafts, linked throughout the site.

Olamide Olatayo Bello is the named platform operator/owner. A public operator support/privacy contact, postal details, operational retention schedules and regional processing disclosures remain to be supplied. The privacy page describes current location requests, infrastructure logs, accounts, integration keys, orders, providers and browser storage. Reference: [FTC information-security guidance](https://www.ftc.gov/business-guidance/resources/protecting-personal-information-guide-business).

## Deployment, Apps Store and SSL

[ansible/README.md](ansible/README.md) documents single-server POS deployment, the **separate** Apps Store server, certificate automation, and provider-secret configuration. The deployment installs backend modules under `/opt/pos-control`, an isolated systemd account/service on loopback port 8787, private data under `/var/lib/pos-control`, public assets in versioned releases, and HTTPS proxy routing. It creates no default real administrator.

After deployment, bootstrap once on the POS server:

```sh
sudo -u pos-control python3 /opt/pos-control/control.py --db /var/lib/pos-control/control.db --bootstrap admin@your-real-domain.com
```

Enter a unique 12+ character password at the hidden prompt. Additional admins are created through authenticated management. Editing your own account revokes its session; sign in again.

`ansible/update-and-publish.yml` targets `pos_servers` and `appstore_servers` independently: deploy/update Global POS, ensure both required SSL certificates and automatic renewal, then publish only Global POS to the existing store catalog. Its metadata is in `catalog/global-pos.json`. Existing administrator visibility and other listings/accounts are preserved; the updater uses online SQLite backup and comparison-guarded row/source rollback if verification fails. Store-hosted brand images respect its same-origin security policy. No APK link is invented.

Code must first exist in the repository revision selected by the server. Local commits alone do not deploy anything. No remote push, production deployment, certificate issuance or live Apps Store update is implied by these files.

## Validation and remaining work

```sh
python3 -m pip install 'ansible-core>=2.16,<2.18'
python3 -m unittest discover -s tests -v
ansible-playbook -i localhost, ansible/deploy.yml --syntax-check
ansible-playbook -i ansible/inventory.two-server.example.ini ansible/update-and-publish.yml --syntax-check
ansible-playbook -i ansible/inventory.two-server.example.ini ansible/configure-gateways.yml --syntax-check
node --check web/admin.js
node --check web/marketplace.js
```

Tests cover tenant isolation, account/session controls, demo separation, scoped-key lifecycle, payment verification with mocked provider responses, idempotent/atomic stock holds, catalog preservation/rollback and SSL preflight. Production SSH, DNS/ACME issuance, real merchant accounts, native-device installation and browser rendering require validation in the target environment. This environment has no installed Playwright browser executable.

Remaining work includes MFA/recovery, shopper accounts, camera/OCR scanning, signed payment webhooks, abandoned-order reconciliation/hold lifecycle, tax and refunds, settlement accounting, inventory movement ledgers, merchant onboarding, exit receipts, real translations, monitoring, backups/restore drills, production load tests and regional scaling. SQLite is suitable for this implementation's current deployment size, not an assertion of global-scale throughput. Back up its live database with SQLite's online backup API. Static routing rollback does not roll back backend data or code; schema initialization is additive and future changes require explicit migrations.

### Business signup

`/signup.html` creates a business account with a new isolated tenant and tenant_admin membership. It never grants global super admin rights or joins an existing tenant. Signup requires terms/privacy acknowledgement and a password of at least 12 characters; requests are limited to five per IP per hour. Sign in at `/admin.html` after signup. Email ownership is not verified; signup alone does not authorize a merchant for live payments.

There is no default production super admin password. On the server, run `runuser -u pos-control -- python3 /opt/pos-control/control.py --db /var/lib/pos-control/control.db --bootstrap YOUR_EMAIL` and enter a private password at the prompt. Bootstrap refuses to overwrite an existing super admin. Public demo credentials work only in the isolated demo.

### Online store discovery

After permitted geolocation, when no active registered store is within 500 metres, the storefront posts to `/api/discovery`. This invokes `discover-store.yml` using the unprivileged service account, with numeric coordinates rounded to three decimal places, a fixed Overpass HTTPS endpoint, a 500-metre query, bounded response and runtime, rate limits, one concurrent lookup, and a 15-minute in-memory cache. No root tasks, package installs, arbitrary URLs or shell commands run from browser input. Temporary job files are removed. Public-provider coverage and availability vary.

Online shop names, source links, addresses where available and coordinates populate review cards with Accept/Decline. Accepted locations remain for the current visit only and do not become merchant records. Findings include source attribution and unverified status. OpenStreetMap listings do not establish product stock or prices: inventory remains unavailable until an authorized merchant registers and supplies it through the existing scoped inventory API. Never scrape or guess saleable inventory from business listings.

### Nearby product search and store shopping

The store finder accepts item descriptions and barcodes. `/api/product-search` reads active merchant inventory within 10 km, groups matches by nearest store and shows published stock and currency prices. Search is bounded to 200 nearby stores and ten matching products per result, with at most twenty stores. Catalog reads are read-only; nothing is purchased by searching. Enabled online lookup triggers the restricted `discover-store.yml` Ansible job to find possible sellers within 10 km. Fixed category heuristics identify candidates only; online listings do not supply stock or prices. Public map coverage and bounded results may omit sellers. API failures preserve merchant catalog results.

Entering a registered store opens its Global POS marketplace listing; configured checkout continues through the existing server-verified gateway flow. Stores without marketplace publication still open their public inventory in browse-only mode, with checkout disabled. An online candidate can be selected or declined; a secure source-listed website link opens the actual merchant website in a separate tab. Global POS does not embed websites that prohibit framing, collect merchant website credentials, bypass protections, or automate arbitrary website purchases. External checkout requires the merchant's supported catalog/order integration; no such third-party connector is configured by this change.
