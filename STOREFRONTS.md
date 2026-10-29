# Global POS storefronts and registers

Business registration optionally creates one tenant-owned **private draft storefront**. Registration without that option creates only the business account. Owners sign in at `/merchant.html`, finish branding, location and inventory, activate the store, review store terms and explicitly publish. A published link is `/store.html?slug=STORE-SLUG` (also `/stores/STORE-SLUG`). Edits return the storefront to private draft. Slugs stay stable; company accounts cannot access another company's store. Platform marketplace flags remain authoritative. Online-only stores are excluded from GPS distance results.

## Deploy

On the existing Debian hosting server, from a clean updated `olamidebello/pos` checkout:

```bash
git pull --ff-only
ansible-playbook -i localhost, -c local ansible/storefront-olamidepanel.yml -e pos_public_ip=154.16.170.86 -e pos_ssl_email=dobcalldemo@gmail.com
```

The storefront hostname is `pos.africandroid.world`. Check DNS first. Existing deployment backups, conflict detection, ACME verification and rollback checks still apply. Unrelated virtual hosts are preserved. The former hostname is no longer accepted by the Business Suite connection.

Then update DOBHRAP and run its `ansible/store-pos-oob.yml` on inventory host `dobhrap`. That deploys the Suite connection, creates a persistent Ed25519 signer and publishes only its verification key locally to POS. Opted-in registrations enter a bounded retry queue (three workspaces per minute). A company draft is created once even if the acknowledgement is lost. Source user/workspace IDs define identity, never email matching. Business Suite admin maps to tenant administrator, editor to cashier, viewer to auditor; no connected identity becomes POS super admin. A signed browser challenge and a single-use, 60-second assertion exchange create a 15-minute POS session. Reopen from Suite after expiry. Suspending the POS user/workspace blocks access immediately; Suite account changes take effect on reconnect or grant expiry. Source role changes do not create platform privileges.

Enable the **pos**, **restaurant**, **inventory**, **marketplace** and other necessary global feature switches through the POS super-admin screen. Tenant/store switches can restrict them further. Merchant signing does not grant permission to enable platform features. Configure store payment gateways separately. Real hosted payments remain disabled until the existing live-payment operator setting and credentials are configured.

## Implemented core

- Tenant-bound storefront builder, private drafts, explicit publish/hide, branding, catalog, stock and stable public storefront URLs.
- Full-viewport POS Only: SKU/barcode/name lookup, supported browser camera scanning, cart hold/resume, authoritative tax/discount quotes, cash shifts, cash/split/partial collection, receipts, one-use exit verification, stock transfers and adjustment journals.
- Restaurant: menus/modifiers, recipes and ingredient deductions, tables/floor coordinates, reservations and waitlist, kitchen queue/stations, preparation workflow, restaurant bill payments, manual dispatch, and staff food-safety logs. Public QR and online ordering create requests that staff must accept. Restaurant bills use stored prices and do not deduct stock twice.
- Role-scoped operational views; isolated demo roles; sales/stock exports in CSV and real XLSX, with browser print/save-to-PDF.
- Existing hosted payment adapters remain separate from register records. External-terminal/bank-transfer register entries are manager attestations, not verified gateway charges. Refund records do not transfer money through external providers.

## Explicit remaining work

The extensive retail and restaurant catalogs are a roadmap, not an implementation claim. Full offline settlement/synchronization, production native mobile builds, printer/drawer/terminal drivers, automatic AI image recognition/forecasting, external retailer connectors, automatic courier dispatch/GPS tracking, voice ordering, automated customer messaging, hardware sensor feeds, franchise fees, certified country tax engines, and comprehensive accounting integrations are not provided here. Local offline support saves cart drafts only; final checkout requires a live server quote. Tips require explicit consent. No medical, food-safety, tax or payment compliance certification is implied by configured fields or logs. Split payments are supported; item-level bill splitting and bill merging are not implemented.

Browser camera/fullscreen APIs depend on device support and require a user gesture. Browser print requires the operator to choose printer/PDF. Live refund/receipt delivery integrations must be implemented and verified before advertising those capabilities. POS financial reports are operational summaries, not certified accounts. Server-side checks fail closed when features, identity, price, inventory, terms or provider configuration are unavailable.
