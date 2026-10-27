# Location-based scan-and-go POS

Planned shopping flow: location/address or manual store selection → store-specific landing page → barcode/price-label scan matched to store catalog → basket review → manual or separately authorized automatic payment on Finish shopping → server-confirmed paid receipt → one-time staff exit verification.

## Agreement foundation

This repository currently contains agreement drafts and a tested Python/SQLite acceptance primitive. The shopping app, registration screens, authentication, payment integration and receipt verification are not implemented yet.

- `agreements/store-v1.md`: required store participation agreement.
- `agreements/shopper-v1.md`: required shopper agreement.
- `agreements/autopay-v1.md`: separate optional automatic payment authorization.
- `agreement_gate.py`: version/hash acceptance records, store scoping and automatic-payment withdrawal.

Install Ansible Core (which includes Jinja2 for deployment-template tests), then run `python3 -m unittest discover -s tests -v`.

## Required integration behavior

Display linked, readable, downloadable terms with unchecked acceptance controls. Block store activation until an authorized representative accepts store terms. Block the shopper's first shopping session until shopper terms are accepted. Keep camera/location permissions optional, with manual entry. Do not bundle optional marketing or automatic-payment consent with required terms.

The server must derive actor identity and store access from authenticated sessions, never trust a client-supplied actor/role, and check current acceptance on every protected action. The primitive accepts those trusted values; it is not an authentication layer. Authorization for a store must be verified independently before passing representative authority.

Keep events append-only to application roles, restrict administrator access, and disclose the retention schedule. Protect the production database against modification and retain accepted document snapshots. Publish immutable document versions; material changes require a new version and renewed acceptance. Hash checking also invalidates accidental in-place text changes, but does not substitute for historical snapshots.

Payment consent must also be checked at payment submission, with server-calculated totals, payment-method binding, spending limits and provider authentication. The module's consent flag alone must never authorize a charge. Repeated checkout requests need idempotency. Mark receipts paid only from verified provider results and consume exit tokens atomically once.

Draft terms require operator identity, support contact, privacy notice, store return policy and jurisdiction-specific review before live use. No live service or enforceability claim is made by these drafts.

## GitHub and server deployment

Ansible push, automated server pull, HTTPS, port checks and rollback are documented in [ansible/README.md](ansible/README.md). Deployment currently publishes the honest agreement-foundation status page, not a working shopping/payment application.
