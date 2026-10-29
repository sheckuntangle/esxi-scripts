#!/usr/bin/env python3
"""Cold-clone a VM on a standalone ESXi host over SSH."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import getpass
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import sys
import time

import paramiko


class CloneError(Exception):
    pass


def load_ssh_host_keys(client, extra=None):
    client.load_system_host_keys()
    path = extra or os.environ.get("ESXI_KNOWN_HOSTS")
    if path:
        client.load_host_keys(path)


def progress(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def read_output(stream, live):
    if not live:
        return stream.read().decode("utf-8", errors="replace")
    chunks, line = [], bytearray()
    while True:
        char = stream.read(1)
        if not char:
            break
        chunks.append(char)
        if char in (b"\r", b"\n"):
            if line:
                progress("  " + line.decode("utf-8", errors="replace"))
                line.clear()
        else:
            line.extend(char)
    if line:
        progress("  " + line.decode("utf-8", errors="replace"))
    return b"".join(chunks).decode("utf-8", errors="replace")


def remote(client, *args, live=False):
    command = " ".join(shlex.quote(str(arg)) for arg in args)
    _, stdout, stderr = client.exec_command(command)
    # Drain both streams before waiting for status (vmkfstools reports progress).
    with ThreadPoolExecutor(max_workers=2) as pool:
        out = pool.submit(read_output, stdout, live)
        err = pool.submit(read_output, stderr, live)
        output = out.result()
        error = err.result()
    if stdout.channel.recv_exit_status() != 0:
        raise CloneError(f"Remote command failed: {command}\n{output}{error}")
    return output


def config_field(config, field):
    match = re.search(r'^\s*' + re.escape(field) + r' = ("(?:[^"\\]|\\.)*"),?\s*$', config, re.M)
    if not match:
        raise CloneError(f"ESXi did not return {field} in the VM configuration")
    return json.loads(match.group(1))


def inventory(client):
    listing = remote(client, "vim-cmd", "vmsvc/getallvms")
    result = []
    for line in listing.splitlines():
        match = re.match(r"^\s*(\d+)\s+", line)
        if match:
            vmid = match.group(1)
            config = remote(client, "vim-cmd", "vmsvc/get.config", vmid)
            result.append((vmid, config_field(config, "name"), config_field(config, "vmPathName")))
    return result


def datastore_path(value):
    match = re.fullmatch(r"\[([^\]]+)\] (.+)", value)
    if not match:
        raise CloneError(f"Unexpected datastore path: {value}")
    datastore, relative = match.groups()
    if datastore in (".", "..") or "/" in datastore or relative.startswith("/") or ".." in relative.split("/"):
        raise CloneError(f"Unsafe datastore path: {value}")
    return posixpath.join("/vmfs/volumes", datastore, relative)


def read_vmx(text):
    settings = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r'\s*([^\s=]+)\s*=\s*"(.*)"\s*', line)
        if not match:
            raise CloneError(f"Unsupported VMX line: {line}")
        key, value = match.groups()
        if key.lower() in settings:
            raise CloneError(f"Duplicate VMX setting: {key}")
        settings[key.lower()] = value
    return settings


def prepare_vmx(settings, clone_name):
    settings = dict(settings)
    if any(k.startswith(("encryption.", "vtpm.")) for k in settings) or settings.get("managedvm.autoaddvtpm", "false").lower() == "true":
        raise CloneError("Encrypted VMs and VMs with a virtual TPM are not supported")
    if any((key.endswith(".sharing") and value.lower() == "multi-writer")
           or (key.endswith(".sharedbus") and value.lower() != "none")
           or (key.startswith("pcipassthru") and key.endswith(".present") and value.lower() == "true")
           for key, value in settings.items()):
        raise CloneError("Shared disks and PCI passthrough devices are not supported")
    disks = []
    for key, value in list(settings.items()):
        if re.fullmatch(r"(?:scsi|sata|ide|nvme)\d+:\d+\.filename", key) and value.lower().endswith(".vmdk"):
            device = key.rsplit(".", 1)[0]
            if settings.get(device + ".present", "true").lower() == "false":
                raise CloneError(f"Remove the disconnected disk {device} before cloning")
            target = f"disk-{len(disks) + 1}.vmdk"
            disks.append((value, target))
            settings[key] = target
        if key.startswith(("uuid.", "vc.uuid", "sched.swap.", "migrate.", "snapshot.", "checkpoint.", "vmotion.", "replay.")) or key in ("extendedconfigfile", "nvram", "vm.genid", "vm.genidx", "workingdir") or key.endswith((".ctkenabled", ".redo")):
            settings.pop(key, None)
        if re.fullmatch(r"ethernet\d+\.(?:generatedaddress|generatedaddressoffset|address)", key):
            settings.pop(key, None)
    if not disks:
        raise CloneError("No supported virtual disks found")
    for key in list(settings):
        if re.fullmatch(r"ethernet\d+\.present", key):
            device = key.rsplit(".", 1)[0]
            settings[device + ".addresstype"] = "generated"
            settings[device + ".startconnected"] = "TRUE"
            settings[device + ".connected"] = "TRUE"
        if key.endswith(".devicetype") and "cdrom" in settings[key].lower():
            settings[key.rsplit(".", 1)[0] + ".startconnected"] = "FALSE"
    settings["displayname"] = clone_name
    settings["uuid.action"] = "create"
    return settings, disks


def resolve_disk(source_dir, filename):
    if filename.startswith("["):
        return datastore_path(filename)
    if filename.startswith("/"):
        if not filename.startswith("/vmfs/volumes/"):
            raise CloneError(f"Disk is outside a datastore: {filename}")
        return filename
    return posixpath.normpath(posixpath.join(source_dir, filename))


def power_state(client, vmid):
    state = remote(client, "vim-cmd", "vmsvc/power.getstate", vmid)
    state = state.splitlines()[-1].strip() if state.splitlines() else ""
    if state not in ("Powered off", "Powered on", "Suspended"):
        raise CloneError(f"Unexpected source power state: {state!r}")
    return state


def assert_off(client, vmid):
    if power_state(client, vmid) != "Powered off":
        raise CloneError("The source VM must be powered off before cloning")


def wait_off(client, vmid, timeout):
    deadline = time.monotonic() + timeout
    while True:
        state = power_state(client, vmid)
        if state == "Powered off":
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        progress(f"Waiting for shutdown: {state}; {remaining:.0f}s remaining")
        time.sleep(min(5, remaining))


def ensure_off(client, vmid, timeout):
    state = power_state(client, vmid)
    progress(f"Source VM state: {state}")
    if state == "Powered off":
        return
    if state == "Powered on":
        progress("Requesting graceful guest shutdown")
        try:
            remote(client, "vim-cmd", "vmsvc/power.shutdown", vmid)
        except CloneError as exc:
            progress(f"Graceful shutdown unavailable: {exc}")
        else:
            if wait_off(client, vmid, timeout):
                progress("Source VM is powered off")
                return
    progress("Powering off source through ESXi; unsaved guest data may be lost")
    remote(client, "vim-cmd", "vmsvc/power.off", vmid)
    if not wait_off(client, vmid, 60):
        raise CloneError("Source did not power off; cloning aborted")
    progress("Source VM is powered off")


def clone(client, args):
    progress("Reading ESXi VM inventory")
    vms = inventory(client)
    matches = [vm for vm in vms if (vm[0] == args.vm_id if args.vm_id else vm[1] == args.vm)]
    if len(matches) != 1:
        raise CloneError("Source VM was not found or its name is ambiguous; use --vm-id")
    if any(vm[1] == args.clone_name for vm in vms):
        raise CloneError(f"A VM named {args.clone_name!r} is already registered")
    vmid, name, location = matches[0]
    progress(f"Found source {name!r} (VM ID {vmid})")
    source = datastore_path(location)
    datastore = args.datastore or re.fullmatch(r"\[([^\]]+)\] (.+)", location).group(1)
    destination = datastore_path(f"[{datastore}] {args.clone_name}")
    with client.open_sftp() as sftp:
        progress("Checking destination and source configuration")
        try:
            sftp.stat(destination)
        except OSError as exc:
            if exc.errno != 2:
                raise
        else:
            raise CloneError(f"Destination already exists: {destination}")
        with sftp.open(source, "rb") as stream:
            prepare_vmx(read_vmx(stream.read().decode("utf-8-sig")), args.clone_name)
        ensure_off(client, vmid, args.shutdown_timeout)
        progress("Reading powered-off source configuration")
        with sftp.open(source, "rb") as stream:
            original_settings = read_vmx(stream.read().decode("utf-8-sig"))
        settings, disks = prepare_vmx(original_settings, args.clone_name)
        progress(f"Creating {destination}")
        # mkdir without -p refuses an existing directory, protecting existing files.
        remote(client, "mkdir", destination)
        registered = False
        try:
            for number, (original, target) in enumerate(disks, 1):
                assert_off(client, vmid)
                progress(f"Cloning disk {number}/{len(disks)}: {original} -> {target}")
                remote(client, "vmkfstools", "-i", resolve_disk(posixpath.dirname(source), original), "-d", args.disk_format, posixpath.join(destination, target), live=True)
                progress(f"Disk {number}/{len(disks)} complete")
            assert_off(client, vmid)
            # Preserve UEFI boot entries and Secure Boot variables when present.
            nvram = original_settings.get("nvram")
            if nvram:
                progress("Copying UEFI NVRAM")
                remote(client, "cp", resolve_disk(posixpath.dirname(source), nvram), posixpath.join(destination, "clone.nvram"))
                settings["nvram"] = "clone.nvram"
            vmx = posixpath.join(destination, args.clone_name + ".vmx")
            progress("Writing clone configuration with new identity and connected network adapters")
            with sftp.open(vmx, "wb") as stream:
                stream.write("".join(f'{key} = "{value}"\n' for key, value in settings.items()).encode("utf-8"))
            assert_off(client, vmid)
            progress(f"Registering {args.clone_name!r} on ESXi")
            result = remote(client, "vim-cmd", "solo/registervm", vmx)
            if not result.strip().isdigit():
                raise CloneError(f"Unexpected registration response: {result}")
            registered = True
            assert_off(client, result.strip())
            progress(f"Clone registered: VM ID {result.strip()}. Powered off; network adapters set to connect at power on. Source remains powered off.")
        except (Exception, KeyboardInterrupt):
            print(f"Clone {'registered' if registered else 'incomplete'}; files retained at {destination}. Inspect before removing or retrying.", file=sys.stderr)
            raise


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--creds-file", help="JSON credentials file; defaults to creds.json beside this script if present")
    result.add_argument("--host", help="ESXi hostname or IP; overrides credentials file")
    result.add_argument("--port", type=int, help="SSH port; defaults to 22")
    result.add_argument("--user", help="SSH user; defaults to root")
    result.add_argument("--key", help="SSH private key file (otherwise prompt for password)")
    result.add_argument("--known-hosts", help="Additional known_hosts file for host key verification")
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument("--vm", help="Exact source VM display name")
    source.add_argument("--vm-id", help="Source VM inventory ID")
    result.add_argument("--clone-name", required=True, help="New VM name and directory name")
    result.add_argument("--datastore", help="Destination datastore; defaults to the source VM's datastore")
    result.add_argument("--disk-format", choices=("thin", "zeroedthick", "eagerzeroedthick"), default="thin")
    result.add_argument("--shutdown-timeout", type=int, default=120, help="Seconds to wait for graceful shutdown before forcing power off (default: 120)")
    return result


def load_credentials(args):
    path = Path(args.creds_file) if args.creds_file else Path(__file__).with_name("creds.json")
    credentials = {}
    if args.creds_file or path.exists():
        try:
            credentials = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CloneError(f"Cannot read credentials file as JSON: {path}") from None
        if not isinstance(credentials, dict):
            raise CloneError("Credentials file must contain a JSON object")
        for key in ("host", "user", "password"):
            if key in credentials and (not isinstance(credentials[key], str) or not credentials[key]):
                raise CloneError(f"Credentials field {key!r} must be a nonempty string")
    # Do not reuse a saved password for a different host or account.
    same_identity = all(getattr(args, key) is None or getattr(args, key) == credentials.get(key)
                        for key in ("host", "user"))
    args.host = args.host or credentials.get("host")
    args.user = args.user or credentials.get("user", "root")
    args.port = args.port if args.port is not None else credentials.get("port", 22)
    if not args.host:
        raise CloneError("Provide --host or a host in the credentials file")
    if type(args.port) is not int or not 1 <= args.port <= 65535:
        raise CloneError("SSH port must be an integer between 1 and 65535")
    return credentials.get("password") if same_identity else None


def main():
    arg_parser = parser()
    args = arg_parser.parse_args()
    if args.shutdown_timeout < 0:
        arg_parser.error("--shutdown-timeout must be zero or greater")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,79}", args.clone_name):
        arg_parser.error("--clone-name must be 1–80 characters: letters, digits, spaces, '.', '_' or '-', starting with a letter or digit")
    client = paramiko.SSHClient()
    load_ssh_host_keys(client, args.known_hosts)
    try:
        progress("Loading connection credentials")
        password = load_credentials(args)
        if args.key:
            password = None
        elif password is None:
            password = getpass.getpass(f"Password for {args.user}@{args.host}: ")
        progress(f"Connecting to {args.host}:{args.port} as {args.user}")
        client.connect(args.host, port=args.port, username=args.user, password=password,
                       key_filename=args.key, look_for_keys=False, allow_agent=False,
                       timeout=20, banner_timeout=20, auth_timeout=20)
        client.get_transport().set_keepalive(30)
        progress("SSH connection established")
        clone(client, args)
    except (CloneError, paramiko.SSHException, OSError, ValueError, EOFError, KeyboardInterrupt) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
        progress("SSH connection closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
