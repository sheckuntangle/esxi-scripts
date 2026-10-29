# Security notes and review

Reviewed on 2026-10-29. Scope: the current Python scripts, Flask API, JavaScript,
HTML/CSS, Docker configuration, dependency versions in the rebuilt image, and
source/config files eligible for publication. This is a local code review and
automated scan, not a penetration test or a guarantee against vulnerabilities.
The workspace currently has no Git commits, so Git history could not be audited.

## Changes from this review

- Parse OVF XML with `defusedxml`, rejecting DTDs and entity declarations.
  Limit descriptors to 16 MiB and archives to 10,000 members. Archive members
  are read without extraction; traversal paths and duplicate regular files
  are rejected.
- Use the same interface mapping validation in deployment, cloning, and the GUI. Reject invalid
  positions, duplicate positions, nonboolean connection flags, control characters,
  and invalid new network names. CLI validation happens before connecting to ESXi.
  Clone mappings are checked against present VMX adapters before network creation
  or source shutdown, and checked again after shutdown. Network names are
  pipe-hex escaped when written to VMX, protecting configuration boundaries.
  The source network configuration is never edited; the clone retains generated MACs.
- Sanitize upload HTTP exceptions so temporary lease authorization URLs do not
  appear in script output. Download errors also omit signed URLs; job output
  redacts the job's saved ESXi password.
- Limit each GUI download to 50 GiB by default, checking both the declared size
  and bytes actually received. Stop before consuming the last 1 GiB of free
  disk space and remove partial files on failure. Reject a symlinked
  `OVA-download` directory as well as symlinked individual files.
- Upgrade Paramiko to 5.1.0 and the image's pip to 26.2.2. The previous image's
  Paramiko 4.0.0 and pip 25.0.1 had published advisories.
- Add no-referrer and same-origin resource headers. Expand Git and Docker
  exclusions for environment files, private keys, and VM artifacts.

## Verification results

- 84 unit/regression tests passed both locally with updated dependencies and
  inside the rebuilt Python 3.12 Docker image. The subsequent network-access
  update passed 86 tests, including remote hostname/IP and optional host-allowlist checks.
  The clone-interface update passes 96 tests, including safe VMX serialization,
  network preflight, unchanged source settings, and private mapping-file handling.
- Browser checks passed for downloads, saved OVA deployment, interface mappings,
  cloning/network fixtures, progress output, reconnect, and desktop/mobile layouts.
  Tests simulate mutations; no VM was deployed, cloned, powered off, or deleted.
- `pip-audit` found **no known vulnerabilities** in the installed packages of
  the rebuilt image, including pip. Dependency ranges are not a lockfile;
  rebuilds can resolve different versions, and future advisories may change this.
- Bandit reported no high-severity findings. The remaining seven flags were
  reviewed: two deliberate unverified ESXi TLS contexts; one Paramiko command
  execution site whose arguments are individually shell-quoted; subprocess
  import/call sites using argument arrays without a shell; a false positive on
  `password_saved=True`; and best-effort lease-abort cleanup that preserves the
  original failure. The TLS flags represent a retained security tradeoff.
- Source/config scans found no copy of the saved ESXi password, private key
  blocks, or matching GitHub/AWS access-token patterns. `creds.json` has mode
  `0600`. Ignore checks passed for credentials, environment files, keys, and
  VM artifacts. Pattern scanning is not proof that every possible secret is absent.

To repeat the static check with Bandit installed:

```bash
bandit -r clone_vm.py deploy_ova.py create_test_network.py web
```

Run `pip-audit` against the versions installed in the image after each dependency
update. Inspect remaining static findings rather than treating every flag as
an exploitable defect or silently suppressing them.

## Trust and deployment boundaries

- The GUI has **no application login**. At the user's request, Compose now
  publishes port 8080 on all IPv4 interfaces (`0.0.0.0:8080`). Anyone who can
  reach that port can use the saved ESXi account, change credentials,
  download/delete OVAs, clone VMs, or create networks. Restrict access with
  network/firewall rules, and use authentication and HTTPS for untrusted networks.
  Browser cross-origin write checks, JSON-only writes, and CSP remain enabled;
  these do not provide user authentication. Hostnames/IPs are accepted by default.
  Set `GUI_TRUSTED_HOSTS` to a comma-separated list of known server hostnames and
  IPs to restrict Host headers and reduce DNS-rebinding exposure. Include
  `localhost,127.0.0.1` for local access and the container healthcheck. This
  allowlist checks the requested server name, not the client's source IP.
- GUI ESXi HTTPS connections intentionally skip certificate and hostname
  verification, per the lab requirement. Traffic is encrypted, but an active
  attacker on that path could impersonate ESXi and capture credentials.
  Use a trusted management network. The deployment CLI supports verification
  with `--ca-file`; the GUI does not currently expose this option.
- SSH host-key verification remains enabled. Trust host fingerprints out of
  band. Paramiko 5 may reject legacy SSH algorithms; update the server rather
  than enabling weak algorithms just to bypass a connection error.
- OVA downloads can reach internal HTTP/HTTPS servers and follow redirects.
  This is intentional for internal Jenkins links. A trusted local user controls
  the URL; the downloader is not suitable as an unauthenticated network service.
  At the user’s request, **Skip download certificate verification** is checked
  by default. This skips HTTPS certificate and hostname verification, including
  HTTPS redirects, so an active network attacker could supply a malicious OVA.
  Uncheck it to verify HTTPS certificates using the configured CA bundle. HTTP
  provides no server authentication or transfer integrity. Use trusted sources;
  OVA publisher signatures/manifests are not cryptographically verified.
- Credentials are stored as plaintext JSON protected by filesystem permissions,
  not encryption. Backups of the workspace can contain credentials and OVAs.
  Git/Docker exclusions prevent accidental inclusion through normal workflows,
  but cannot prevent someone explicitly force-adding secrets.
- The container runs without root privileges or Linux capabilities, with
  `no-new-privileges`. The repository is mounted writable to persist credentials
  and downloads. Container compromise could therefore modify repository files
  and read the saved ESXi credentials; the mount is not a sandbox for untrusted
  users or untrusted code.
- New testing networks intentionally permit promiscuous mode, MAC changes, and
  forged transmits. They have no physical uplinks; keep this policy confined to
  testing networks. Connecting VM adapters can expose guests to their selected
  port groups. Cloning automatically shuts down the source VM and may force
  power-off if graceful shutdown fails.
