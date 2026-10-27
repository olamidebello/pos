# POS deployment automation

These playbooks target Debian 12+ or Ubuntu 22.04+ servers using systemd and the standard Nginx `sites-enabled` layout. They deploy the repository's **agreement-foundation status page and agreement documents**. A complete shopping application, merchant accounts, authentication, checkout and payments are not implemented by these scripts.

## 1. Push reviewed local changes to GitHub

Install Ansible Core and Git on your development machine. Authenticate Git using a credential helper or an SSH key; do not put tokens or private keys in YAML or the repository. Configure your Git author name and email. Run from the repository root, on `main`:

```sh
ansible-playbook ansible/push.yml \
  -e "pos_push_checkout=$PWD" \
  -e '{"pos_push_paths":["README.md",".gitignore","agreement_gate.py","agreements","tests","ansible","deploy","web",".github"]}' \
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
