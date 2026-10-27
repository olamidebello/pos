# POS deployment automation

These playbooks target Debian 12+ or Ubuntu 22.04+ servers using systemd and the standard Nginx `sites-enabled` layout. They deploy **Global POS**, including its administration API, storefront, marketplace, hosted-gateway adapters, isolated demo and installable web app. Live payments remain disabled until explicitly configured. The separate Apps Store and SSL workflows are documented below.

## 1. Push reviewed local changes to GitHub

Install Ansible Core and Git on your development machine. Authenticate Git using a credential helper or an SSH key; do not put tokens or private keys in YAML or the repository. Configure your Git author name and email. Run from the repository root, on `main`:

```sh
ansible-playbook ansible/push.yml \
  -e "pos_push_checkout=$PWD" \
  -e '{"pos_push_paths":["README.md",".gitignore","agreement_gate.py","control.py","commerce.py","gateways.py","agreements","catalog","docs","tests","ansible","deploy","web",".github"]}' \
  -e 'pos_push_message=Add validated POS deployment automation'
```

For an SSH origin add `-e pos_push_remote_url=git@github.com:olamidebello/pos.git`. The remote must already point to that exact repository. The playbook tests the code, checks branch and push destinations, refuses unrelated staged changes and common private files/key material, stages only selected paths, commits if needed and pushes without force. Review changes yourself as well: the checks are not a complete secret scanner. A non-fast-forward push requires you to fetch and reconcile changes first; the playbook never rewrites history.

## 2. Prepare the real server and DNS

Choose a real hostname, point its A record to the server and ensure every published A/AAAA record routes to the same deployment. Remove a stale AAAA record rather than sending certificate requests to another server. Permit inbound TCP **80 and 443** in cloud firewalls, router/NAT and the host firewall, plus outbound HTTPS to GitHub, OS repositories and the certificate authority. Preserve your SSH access.

These scripts have no configured production domain or SSH credentials. `vars.example.yml` deliberately contains placeholders and is rejected unchanged. Port ownership and DNS problems are diagnosed; unrelated services are not stopped or killed.

On the server, as root, with Git available:

```sh
apt-get update
apt-get install -y git python3
install -d -m 0700 /etc/pos-deploy
# Use an empty checkout path; do not overwrite another application.
git clone https://github.com/olamidebello/pos.git /opt/pos-bootstrap
install -m 0600 /opt/pos-bootstrap/ansible/vars.example.yml /etc/pos-deploy/settings.yml
nano /etc/pos-deploy/settings.yml
```

Set `pos_domain`, `pos_public_ip` and `pos_ssl_email` to actual values. The SSL email is used for certificate registration. Choose `pos_auto_update: true` for automatic pulls, or `false` for manual deployment only. `pos_git_version` may be a branch such as `main`, or a pinned commit/tag; a pinned revision will not advance automatically. Public HTTPS read access is currently supported; private repository credentials are not configured by this workflow.

Run:

```sh
cd /opt/pos-bootstrap
sh deploy/bootstrap.sh /etc/pos-deploy/settings.yml
```

The wrapper installs Ansible and holds a process lock. The playbook also holds a server-side deployment guard, so remote invocations cannot overlap it. If it fails, correct the reported issue and rerun the same command.

Alternatively, from your workstation, configure the commented example host in a private inventory and use:

```sh
ansible-playbook -i /secure/path/inventory.ini ansible/deploy.yml \
  --extra-vars @/secure/path/settings.yml --ask-become-pass
```

Use your existing SSH key/agent. Password-based connections also need your normal Ansible SSH-password setup; no password is stored in this repository. Do not disable SSH host verification.

## What deployment checks and repairs

1. Validates configuration and OS support, installs prerequisites and checks DNS and port owners. Fails before Nginx configuration changes if Apache or an unknown service occupies ports 80/443, or another vhost already declares the hostname.
2. Installs missing Nginx/Certbot/Ansible packages without auto-starting a new Nginx service, pulls Git without discarding edits and runs the repository tests.
3. Copies only public assets and agreement documents to a commit-specific release, and records truthful health metadata. No database, source checkout or private settings are served.
4. Saves the previous release/configuration, switches the current release atomically and validates full Nginx syntax before reload. Existing unrelated vhosts and default sites remain in place.
5. Tests the domain's public ACME challenge route, obtains/renews a Let's Encrypt certificate using webroot, enables TLS 1.2/1.3 and redirects HTTP to HTTPS. It verifies origin SNI/hostname/TLS, public HTTPS and the exact deployed commit.
6. Restores the previous release and managed vhost on handled activation or health-check failure, then fails visibly. A first-install failure removes the new vhost and stops Nginx if it was previously inactive. Installed packages, issued certificates and explicitly requested additive firewall rules remain; no user data is deleted. Failures after the activation transaction, such as installing the timer, leave the verified release running and report failure.
7. Enables certificate renewal with a validated Nginx reload hook and a systemd update timer. The timer checks the trusted repository every five minutes by default and reruns tests and health checks on each pull. Repository maintainers therefore control code executed as root on this server. Protect the deployment branch and use pinned revisions when automatic execution is inappropriate.

Optional `pos_manage_ufw: true` adds HTTP/HTTPS rules **only if UFW is already installed and active**. It never enables UFW, resets existing rules or edits cloud firewalls. If UFW is missing, install/configure it explicitly or leave the option false. A firewall change cannot repair a cloud firewall or provider security group.

## Status, diagnostics, updates and renewal

```sh
cat /var/lib/pos-deploy/report.json
systemctl status pos-auto-update.timer
journalctl -u pos-auto-update.service -n 100 --no-pager
systemctl start pos-auto-update.service
ss -ltnp
nginx -t
systemctl status certbot.timer
certbot renew --dry-run
```

The JSON report is written on success; on a later failed attempt it still describes the last successful deployment. Consult the nonzero playbook exit and journal for the latest attempt. Failed release directories are retained for diagnosis. Automatic updates do not discard edits or prune releases; monitor disk space.

HTTP-01 requires port 80 even after HTTPS is enabled. Servers behind a proxy/CDN or without public DNS loopback may require explicit routing changes; the script reports failed verification rather than declaring an unverified deployment healthy. Configure the CDN to forward ACME paths unchanged and bypass caching for `healthz.json`.

To stop automatic updates:

```sh
systemctl disable --now pos-auto-update.timer
```

Also set `pos_auto_update: false` in settings before a later deployment, or it will be enabled again. To roll back to an older known-good commit, disable the timer, set `pos_git_version` to that commit and rerun the bootstrap command. Switch it back to `main` only when ready to resume updates.

If a process is killed or the host loses power, `/run/pos-deploy-active` may remain until reboot. Verify no deployment/ansible-pull process is running before removing that empty directory with `rmdir /run/pos-deploy-active`. Never bypass an active lock. For certificate or DNS errors, check the original task output and fix the cause; the playbook does not replace unrelated web-server configuration.

## Validation and limits

Run agreement/deployment helper tests and Ansible syntax checks before publishing:

```sh
python3 -m unittest discover -s tests -v
ansible-playbook -i localhost, ansible/deploy.yml --syntax-check
ansible-playbook ansible/push.yml --syntax-check
```

These checks cannot prove a particular production server's DNS, firewall, package manager or certificate issuance works. A successful server run must include the live checks above. The playbook makes necessary package and service changes; it does not claim to repair every possible hosting problem.

References: [Ansible Git module](https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/git_module.html), [Certbot webroot and renewal](https://eff-certbot.readthedocs.io/en/stable/using.html).

## 3. Update Global POS and publish its PWA on a different Apps Store server

This repository's targeted store updater is compatible with the existing `africanappsstore.service` and `hosted_apps` schema in `olamidebello/africanappsstore`. It discovers the active process's working directory and `STORE_DATA`/`PORT` values, including values loaded through systemd EnvironmentFile. It does **not** assume the store is on the POS server or that its code/data live at a fixed path.

Copy these examples outside Git and fill both SSH targets, public IPs and certificate contact emails:

- `inventory.two-server.example.ini` → `/secure/path/global-pos-inventory.ini`
- `two-server.vars.example.yml` → `/secure/path/global-pos-settings.yml`

There must be one host under `pos_servers` and a different host under `appstore_servers`. Keep SSH host checking enabled. Use your SSH agent/key, normal Ansible SSH-password handling, or Ansible Vault; do not commit credentials. The POS server requires real DNS for `pos.africandroid.com`; the existing store requires `africanappsstore.space`. Their A/AAAA records must route to their respective servers. If either hostname has valid AAAA records, add that server's public IPv6 addresses in the matching `*_ssl_additional_public_ips` list.

Run the complete workflow from the updated checkout:

```sh
ansible-playbook -i /secure/path/global-pos-inventory.ini \
  ansible/update-and-publish.yml \
  -e @/secure/path/global-pos-settings.yml
```

This runs the existing tested POS deployment only on `pos_servers`, ensures both required certificates, and runs the targeted publisher only on `appstore_servers`. A POS failure aborts subsequent plays. The Apps Store must already be running; the publisher refuses to create/replace an unknown installation. The selected POS Git revision must contain these changes before the server can pull them. A local unpushed branch is not available to either server.

To update **only** the store listing after Global POS is already deployed:

```sh
ansible-playbook -i /secure/path/global-pos-inventory.ini \
  ansible/deploy-appstore.yml
```

The publisher:

1. Confirms this host runs the existing store and verifies Global POS's HTTPS install, demo, legal, icon, worker and manifest resources before advertising them.
2. Copies the logo/artwork into the running store's public `static` directory, using same-origin image URLs required by its security policy.
3. Takes an online SQLite backup and a private frontend backup; inserts/updates only the `pos.africandroid.com` catalog row and adds that card's logo, correct PWA platform label, install link and demo link to the recognized renderer. A targeted backend compatibility patch permits only its two PNG asset paths and the correct PNG MIME type; source is compiled before activation.
4. Preserves all other apps, accounts, releases and administrator visibility settings. New listings are visible by default. An already hidden Global POS listing stays hidden and is reported as hidden, rather than failing an assertion that every listing must be public. Explicitly unhide only Global POS with `-e pos_store_publish=true` if intended.
5. Verifies the local and public hosted-app API. On failure it rolls back only its own catalog row/frontend patch with comparison guards; it does not restore the whole database and discard concurrent user activity. Public asset copies may remain, with Ansible backups retained.
6. Skips new catalog/frontend backups when the listing and patch are already current. The store service restarts only when the PNG allowlist/MIME compatibility patch changes; a failed verification restores the prior targeted source and restarts the restored backend. No unrelated store upgrade, port change, TLS rewrite, APK upload or account promotion is performed by the listing publisher.

The store's future independent frontend updates may replace this card patch; rerun the idempotent publisher after those updates. Review/prune private backups under `/var/lib/global-pos-store-sync` according to your operational retention policy. Store location: `https://africanappsstore.space/#Hosted%20apps`; install link: `https://pos.africandroid.com/install.html`; demo: `https://pos.africandroid.com/demo.html`. These are target URLs, not a report that this checkout has already been published.

## 4. Automatically obtain, install and renew the required SSL certificates

`ssl.yml` handles **pos.africandroid.com on the POS server** and **africanappsstore.space on the separate store server**. POS's normal first deployment already configures its HTTP-01 challenge route, obtains its certificate and enables HTTPS. The reusable certificate playbook can subsequently repair a missing/expiring domain certificate on an otherwise deployed site, and it sets up the store's existing dedicated Nginx vhost with Certbot's Nginx plugin and HTTPS redirects.

```sh
ansible-playbook -i /secure/path/global-pos-inventory.ini \
  ansible/ssl.yml -e @/secure/path/global-pos-settings.yml
```

Before requesting certificates it requires active Nginx, validates every DNS A/AAAA address against the declared public IPs, checks web-port ownership and Nginx syntax, and requires one existing dedicated hostname configuration. It rejects a shared server-name block or a certificate lineage that also covers other names, instead of shrinking other sites' coverage. Split such vhosts or explicitly manage their multi-domain certificates through their own deployment.

It installs missing Certbot tools, saves the affected vhost under `/var/lib/global-pos-ssl`, obtains or renews with `--keep-until-expiring`, validates before reload, enables `certbot.timer`, and installs a renewal hook that runs `nginx -t` before reload. It verifies local origin TLS with SNI, public certificate trust/hostname and a seven-day minimum remaining certificate lifetime. Issued certificates remain managed by Certbot, including any normal renewal configuration it creates.

This script does not invent DNS records, issue unsolicited certificates for unrelated domains, bypass certificate validation, disable other services, replace existing SSL keys, or fix a provider firewall/NAT rule. The certificates must be valid for the actual public names, with inbound TCP 80/443 and outbound CA access. HTTP-01 renewal needs port 80 to remain reachable. A DNS/firewall/CA validation failure is reported clearly; the existing configuration backup is retained. Certbot handles its own installer failure recovery; no broad Nginx tree restore overwrites concurrent unrelated edits.

Verification on each target:

```sh
systemctl status certbot.timer
certbot certificates
certbot renew --dry-run
nginx -t
```

Only initial issuance/needed renewals request a new certificate; repeated playbook runs keep non-expiring certificates. A CDN/proxy may require additional explicit origin routing; the workflow verifies origin and public HTTPS separately and does not claim success solely because a proxy serves a certificate.

## 5. Configure payment-provider credentials privately

The super admin GUI provisions store gateways and Global POS integration API keys. Merchant provider secret values are installed through a private server file; the GUI references them by name. Separate test/live names keep a sandbox gateway from accidentally using a live key. Supported hosted adapters are Stripe, Paystack and Flutterwave v3 Standard. The public demo uses only its local simulator.

Copy `gateways.vault.example.yml` to a private file **outside the checkout**, replace only the provider credentials you actually use, and encrypt it:

```sh
ansible-vault encrypt /secure/path/global-pos-gateways.yml
ansible-playbook -i /secure/path/global-pos-inventory.ini \
  ansible/configure-gateways.yml \
  -e @/secure/path/global-pos-gateways.yml --ask-vault-pass
```

The playbook installs root-only `/etc/pos-control/gateways.env`, suppresses secret values and diffs in logs, and restarts the backend only when it changes. Supply the complete intended mapping on each run: omitted references are removed from this managed file. Keep old merchant credential references available for pending-order reconciliation. No secret value is written to the repository or the public web directory. Do not paste real keys into chat or the GUI's public-key/name fields.

Leave `pos_enable_live_payments: false` while testing. Then, in the GUI:

1. Select **features** and enable the global `marketplace`, `payments`, and/or `integrations` switches required for the intended stores. Tenant/store scopes can further restrict them.
2. Select **marketplace**, choose a store and publish its listing/category/description.
3. Select **gateways**, choose that store/provider/test mode and the matching server reference, such as `GLOBALPOS_STRIPE_TEST_LAGOS`. Enable the gateway. “Server key present” means a matching key exists, not that the merchant has passed a live verification.
4. Test checkout with provider test credentials and sandbox payment details. The demo's own simulator cannot call real providers. Use **orders → Reconcile payment** to verify status on the server.
5. Select **api_keys** to issue a store-scoped integration key, choose allowed scopes and expiry, then copy its one-time secret into your integration's secure configuration. Rotate or revoke through the same GUI. This key is not a merchant payment secret and cannot administer the platform or charge customers.

Live payments require an explicit Vault flag, a live-mode gateway, valid merchant credentials and completed production operating arrangements. Webhooks/background reconciliation, automatic pending-hold expiry, refunds, taxes/fiscal receipts and physical exit verification remain outside this release. Do not enable live operations until those requirements are addressed for the relevant stores and jurisdictions.

## 6. Pull and deploy directly on the Olamidepanel server

Use `deploy-olamidepanel.yml` for Global POS on the server that already runs `olamidepanel.service`. The panel's static-site publisher cannot run this backend, so this workflow creates a dedicated Global POS service and hostname while preserving the panel/MySQL and unrelated websites.

On the Olamidepanel server as root:

```sh
apt-get update
apt-get install -y ansible-core git python3
ansible-pull --url https://github.com/olamidebello/pos.git \
  --checkout main --directory /var/lib/global-pos-panel-bootstrap \
  --inventory localhost, --connection local ansible/deploy-olamidepanel.yml
```

The wrapper requires the existing panel service and reads only the public-IP and ACME-email defaults from `/etc/olamidepanel/provisioning.json` or `/etc/olamide-github-deploy.json`, without logging their private contents. If those files are absent or the defaults are unsuitable, supply private settings explicitly:

```sh
ansible-pull --url https://github.com/olamidebello/pos.git \
  --checkout main --directory /var/lib/global-pos-panel-bootstrap \
  --inventory localhost, --connection local \
  --extra-vars @/etc/pos-deploy/settings.yml ansible/deploy-olamidepanel.yml
```

That file needs `pos_domain: pos.africandroid.com`, the real `pos_public_ip`, and a real `pos_ssl_email`. Optional `pos_auto_update: true` (default) enables the existing five-minute `pos-auto-update.timer` to pull, test, deploy and verify subsequent `main` updates automatically. The initial successful deployment saves these settings privately. For an existing checkout, the equivalent command is `sudo sh deploy/bootstrap.sh /absolute/path/to/private-settings.yml --olamidepanel`.

Before the run, point `pos.africandroid.com` A/AAAA records to this server and allow TCP 80/443. Exact hostname conflicts in an existing panel/static/custom vhost are reported rather than deleted. Keep this domain under the dedicated POS deployment rather than queuing it as a static website in the panel. The separate Apps Store workflow remains separate; running this command does not assume the store is on the panel host.

Useful checks after deployment:

```sh
systemctl status pos-control pos-auto-update.timer certbot.timer
cat /var/lib/pos-deploy/report.json
journalctl -u pos-auto-update.service -n 100 --no-pager
```

Then bootstrap the first real admin using the hidden-password command in the main README. Public demo accounts already work in their isolated sessions; no production password is created or reset by deployment.
