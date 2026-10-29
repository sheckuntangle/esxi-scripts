# ESXi scripts

Security model, review results, and retained lab tradeoffs: [SECURITY.md](SECURITY.md).

## Web GUI with Docker

Start the OVA deployment GUI from this repository:

```bash
docker compose up -d --build
```

Open **http://localhost:8080**. In **Downloaded OVAs**, supply an HTTP/HTTPS
OVA download link and click **Download OVA**. An optional **Save file as** field
renames the download; otherwise the file name comes from the link. Links
without an OVA filename need an explicit name. `.ova` is added if omitted.
Downloads do not require an ESXi connection or a VM name. Each download
is limited to 50 GiB by default; set `MAX_OVA_DOWNLOAD_BYTES` when recreating
the container to change it. Downloads stop before using the last 1 GiB of free
disk space. This limit applies to the downloaded file, not its virtual disks.

The GUI saves downloads into **`OVA-download/` in this repository**. The library
lists completed files with their size and saved date, and refreshes automatically.
Existing files are never overwritten; **Reuse this file if already downloaded**
lets you keep a previously saved file. **Delete** removes the saved file after
confirmation, without modifying any deployed VM. Files in use cannot be deleted;
partial downloads and symbolic links are excluded from the library.

To deploy, save the ESXi connection, choose **Use for deployment** in the library
or select a file in the deployment form's **Saved OVA** dropdown, and enter the
new VM name. **Deploy an appliance** accepts only previously downloaded OVAs.
Downloading and deploying are separate actions. Both show progress and output
in the live output window, and you can return to a running action after refreshing
the browser. Only one action runs at a time across both tabs.

Expand **Deployment options** for datastore, ISO folder, and optional GE6 NIC
position. The GUI automatically accepts self-signed ESXi certificates. OVA
download certificates are still verified. The existing deployment defaults apply:
all NICs use `VM Network`, only GE6 connects at startup, the cloud-init ISO is
attached, automatic startup is disabled, and the VM remains off with normal
boot settings.

Expand **Interface networks** to map each OVA adapter to an ESXi standard port
group and choose whether it connects at power on. The tool reads the actual
interface labels from the selected saved OVA. Click **Load / refresh interfaces and networks**
to reload the available port groups from the saved ESXi connection. Initially,
all adapters use `VM Network`, with only GE6 checked as connected. Changing a
network does not automatically check Connected.

Choose **Create new…** beside any interface and enter a name to create an
isolated vSwitch and same-named port group during deployment. This reuses the
network script: no uplinks, all three security options enabled on the switch,
and inherited port group policies. Multiple interfaces can share the same new
name; it is created once. Creating networks requires SSH access and trusted
host keys. Created networks remain if deployment later fails; select the
existing network when retrying. The VM stays powered off.

`creds.json` and `OVA-download/` are bind-mounted from this repository, so they
survive container restart, recreation, and image rebuilds. Credentials are
saved atomically with owner-only permissions. The saved password is never sent
back to the browser; leave it blank to retain it for the same host/user. Each
job uses a private temporary credential snapshot. Credentials and downloads
are excluded from Git and Docker image builds.

The **Deployment** tab contains the OVA library, deployment form, and its live
output window. The **Misc ESXi Actions** tab adds:

- **Clone a VM**: select a source VM from the configured ESXi host and enter a new clone name, with an
  optional destination datastore. The list loads automatically; use **Refresh** to reload it.
  VM IDs distinguish machines with duplicate names. SSH access to ESXi is required.
  The existing cloning script automatically
  shuts down the source (graceful first, forced if needed), copies disks, and
  leaves source and clone off. Clone adapters connect at startup.
- **Create an internal network**: enter a name for both vSwitch and port group.
  The switch has no uplinks and enables all three security options. Port group
  policies inherit.

Both actions use the shared connection and show live script output on the Misc
tab. One action runs at a time across both tabs; changing tabs or refreshing
does not stop it. These two actions require ESXi SSH access. Docker mounts your
`~/.ssh/known_hosts` read-only, so trusted host keys are shared without mounting
private SSH keys. Set `HOST_KNOWN_HOSTS=/path/to/known_hosts` if stored elsewhere.
If the file does not exist, first connect with `ssh user@esxi-host` and verify
the host fingerprint before accepting it. New/changed keys remain rejected.

```bash
docker compose logs -f gui
docker compose restart gui
docker compose down
```

Restarting/stopping the container during a deployment interrupts the job.
Inspect ESXi before retrying an interrupted import. The live log and job status
are held in memory and reset on a server restart; saved credentials and OVA
files remain on disk. Partial failed downloads are removed automatically.

The container runs as UID/GID 1000 by default. If your checkout is owned by a
different user, set the matching IDs when starting it:

```bash
LOCAL_UID=$(id -u) LOCAL_GID=$(id -g) docker compose up -d --build
```

Image builds use host networking to fetch Python dependencies. The running GUI
uses its own Docker network and publishes only the localhost port above.
The GUI has no application login: access to this port grants control through
the saved ESXi account. Keep it local; see [SECURITY.md](SECURITY.md) before
changing its network exposure.

The GUI trusts the Docker host's CA bundle, mounted read-only from
`/etc/ssl/certs/ca-certificates.crt`. This lets internal download servers use the
same corporate CAs trusted by your machine, with HTTPS verification enabled.
If the bundle is elsewhere on your host, set `HOST_CA_BUNDLE` when starting it:

```bash
HOST_CA_BUNDLE=/path/to/ca-bundle.pem docker compose up -d --build
```

No CA certificates are copied into the image or repository. This setting applies
to downloads; the GUI accepts ESXi certificates automatically.

The GUI is intended for a single local administrator and binds only to
`127.0.0.1:8080`. It has no login screen. For access from another machine, use
an SSH tunnel (`ssh -L 8080:127.0.0.1:8080 user@docker-host`) and open
`http://localhost:8080` locally. Keep it behind authentication and HTTPS if
you choose to expose it more broadly. The application accepts localhost host
headers only and rejects browser cross-origin writes.

For local development and the complete test suite:

```bash
python3 -m pip install -r requirements-web.txt
gunicorn --bind 127.0.0.1:8080 --workers 1 --threads 16 --timeout 0 'web.app:create_app()'
python3 -m unittest discover -s tests -v
```

Use one web worker: its background job and live log are shared between threads
within that process.

## Command-line scripts

`clone_vm.py` creates an independent, full disk clone on a standalone ESXi
server using SSH, `vmkfstools`, and `vim-cmd`. It does not require vCenter.
The clone is registered on the same server and left powered off, with its
network adapters set to connect at startup.

## Setup

Use Python 3.10 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Enable SSH on ESXi and use an account with permission to read the source VM,
create datastore files, run `vmkfstools`, and register VMs (typically `root`).
Add the server to your SSH known_hosts file using `ssh root@esxi.example.com`,
verifying the host key fingerprint against a trusted source before accepting.
The script rejects unknown or changed host keys. Use `--known-hosts FILE` if
you maintain a separate known_hosts file.

## Usage

The script automatically loads `creds.json` beside `clone_vm.py` when present:

```json
{
  "host": "esxi.example.com",
  "user": "root",
  "password": "your-password",
  "port": 22
}
```

Keep this plaintext file private (`chmod 600 creds.json`). It is excluded from
Git. `creds.example.json` is a shareable template. Select a different file with
`--creds-file /path/to/creds.json`. Command-line host, user, and port override
file values; changing the host or user causes a password prompt rather than
reusing the saved password.

With the credentials file configured:

```bash
python3 clone_vm.py --vm "Source VM" --clone-name "Source VM Clone"
```

Or specify connection options explicitly:

```bash
python clone_vm.py --host esxi.example.com --user root \
  --vm "Source VM" --clone-name "Source VM Clone"
```

Without a saved password, the password is prompted securely. For SSH key authentication, add
`--key ~/.ssh/id_ed25519` with a key that does not require a passphrase.
Passwords are never accepted as command-line arguments.

The destination defaults to a new directory at the root of the source VM's
datastore. Select another datastore on the same server with
`--datastore datastore2`. The default disk format is thin; use
`--disk-format zeroedthick` or `--disk-format eagerzeroedthick` to change it.
Ensure sufficient datastore capacity for all cloned disks.

If VM names are duplicated, use `--vm-id 42` instead of `--vm`.
Find inventory IDs over SSH with `vim-cmd vmsvc/getallvms`.
Existing VM names and destination directories are refused.

The script automatically shuts down a running source VM. It first requests a
graceful guest shutdown through VMware Tools and waits up to 120 seconds
(`--shutdown-timeout SECONDS` changes this). If guest shutdown fails or times
out, it forces power off through ESXi, which can lose unsaved guest data.
Suspended VMs are powered off directly. Both source and clone remain off afterward.
Destination and configuration checks run before shutdown.

Timestamped messages show connection, inventory lookup, shutdown, each disk copy,
NVRAM copying, configuration writing, registration, and completion. Disk copying
streams `vmkfstools` progress as ESXi emits it.

Keep the source powered off throughout the operation; do not modify its
configuration, snapshots, or files while cloning. The script checks power
state before each disk copy and before registration, but cannot prevent
another administrator or automation from starting the source during a copy.
The currently attached VMDK descriptors are cloned, including their snapshot
chains; snapshot history itself is not copied.

Review the clone's network settings and guest hostname, static IP, and any
OS-specific identity requirements before powering it on. ESXi creates
new VM UUIDs and MAC addresses; guest OS identity is unchanged.

## Limits and recovery

- Intended for ordinary file-backed VMDKs. RDMs, shared/multi-writer disks,
  passthrough devices, linked-clone workflows, and encrypted/vTPM VMs are not
  supported. Encrypted/vTPM configurations are rejected when detected.
- UEFI NVRAM is copied when configured. ISO images and other removable media
  are not copied; CD-ROM devices are disconnected at startup.
- No guest customization, source restart, clone startup, or automatic deletion
  is performed.
- On failure or interruption, partial files are retained and their directory
  is printed. Inspect the ESXi inventory and directory before manually cleaning
  up or retrying. A lost connection during registration can leave a registered
  VM even if the script reports failure.
- Requires SSH/SFTP and ESXi's built-in shell tools. Actual compatibility must
  be verified against your ESXi version and VM hardware configuration.

Run local unit tests with `python -m unittest discover -s tests -v`.

## Deploy an OVA

`deploy_ova.py` uploads a local OVA directly through the ESXi HTTPS API. It uses
the same `creds.json` as the cloning script. Install the updated requirements:

```bash
python3 -m pip install -r requirements.txt
python3 deploy_ova.py /path/to/appliance.ova --name appliance-01
```

For a host with a self-signed certificate, explicitly add `--insecure`, or use
`--ca-file /path/to/ca.pem` for a trusted CA certificate. HTTPS defaults to port
443 (`--https-port` overrides it); the `port` field in `creds.json` is for SSH.

The script prints timestamped steps and streams upload percentages and MiB for
each OVA payload. It reads directly from the archive without unpacking disks
locally. HTTP timeouts, lease heartbeats, and upload failures are handled; a
failed import is aborted, and a failure after import leaves the VM for inspection.
Inspect inventory and datastore files before retrying rather than deleting an
existing VM. Single-VM OVAs with uncompressed file references are supported.

Deployment behavior:

- Name the VM with `--name`; existing VM names are refused.
- Use thin disks on `datastore1` by default; select another with `--datastore`.
- Map every NIC to the standard `VM Network` port group.
- Enable only GE6 at startup; all other NICs remain disconnected. Guest control
  of cable connection is disabled. Exported MAC addresses are cleared so ESXi
  generates new addresses.
- Leave the VM powered off and disable its ESXi automatic startup entry while
  preserving startup settings for other VMs.
- Attach `cloud-init.iso` found under `00-ISO` on the destination datastore.
  Folder spelling with spaces, such as `00 - ISO`, is also recognized.
  Use `--iso-datastore` or `--iso-folder` to override the location. Missing or
  ambiguous ISO matches stop deployment before uploading.
- Boot normally when manually powered on, preserving the appliance's BIOS/UEFI
  firmware type and boot order. Firmware setup on next boot is disabled.

GE6 is identified from the OVF's NIC labels, descriptions, or network names.
If the OVA does not identify GE6 unambiguously, the script prints its NIC order
and stops before contacting ESXi. Specify the corresponding position explicitly:

```bash
python3 deploy_ova.py /path/to/appliance.ova --name appliance-01 \
  --ge6-nic 6 --insecure
```

`--ge6-nic 6` means the sixth NIC in the OVF descriptor; confirm that it actually
corresponds to GE6 for your appliance. Required permissions include OVF import,
datastore browsing/upload, network assignment, VM configuration, and editing the
VM's host automatic startup entry. SSH is not required for OVA deployment.

## Create an internal testing network

```bash
python3 create_test_network.py lab-01
```

The name is the only argument. Connection settings come from `creds.json`;
SSH and a trusted host key are required, as for the cloning script. The ESXi
account needs permission to configure standard vSwitches and port groups.

The script creates a standard vSwitch named `lab-01` with **no physical uplinks**.
On the switch, promiscuous mode, MAC address changes, and forged transmits are
all enabled. A same-named port group attaches to that switch, with the default
VLAN ID of 0 and all policies left to inherit. It verifies security, traffic
shaping, and failover inheritance after creation and prints progress at each step.
No VMkernel interface or physical adapter is added.

Names must be 1–31 characters using letters, digits, spaces, `.`, `_`, or `-`,
starting with a letter or digit. Existing vSwitch or port group names are refused
before any changes. If creation fails midway, inspect the named switch and
port group before retrying; the script does not automatically delete resources.
