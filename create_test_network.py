#!/usr/bin/env python3
"""Create an isolated ESXi vSwitch and same-named port group."""

import argparse
import csv
import io
import re
import sys

import paramiko

from clone_vm import CloneError, load_credentials, progress, remote


def query(client, *args):
    output = remote(client, "esxcli", "--formatter=csv", "network", "vswitch", "standard", *args)
    return list(csv.DictReader(io.StringIO(output.strip())))


def single(rows, name, description):
    matches = [row for row in rows if row.get("Name") == name]
    if len(matches) != 1:
        raise CloneError(f"Expected one {description} named {name!r}; found {len(matches)}")
    return matches[0]


def verify(client, name):
    progress("Verifying switch uplinks and port group attachment")
    switch = single(query(client, "list"), name, "vSwitch")
    if "Uplinks" not in switch or switch["Uplinks"].strip():
        raise CloneError("New vSwitch has unexpected uplinks or unreadable uplink information")
    group = single(query(client, "portgroup", "list"), name, "port group")
    if group.get("VirtualSwitch") != name or group.get("VLANID") != "0":
        raise CloneError("Port group attachment or default VLAN verification failed")
    progress("Verifying all three switch security options are enabled")
    security = query(client, "policy", "security", "get", "--vswitch-name", name)
    fields = ("AllowPromiscuous", "AllowMACAddressChange", "AllowForgedTransmits")
    if len(security) != 1 or any(security[0].get(field) != "true" for field in fields):
        raise CloneError("vSwitch security verification failed")
    progress("Verifying port group security, shaping, and failover policies inherit")
    for policy in ("security", "shaping", "failover"):
        rows = query(client, "portgroup", "policy", policy, "get", "--portgroup-name", name)
        if len(rows) != 1:
            raise CloneError(f"Cannot read port group {policy} policy")
        overrides = {key: value for key, value in rows[0].items()
                     if key and key.lower().startswith("override")}
        if not overrides or any(value != "false" for value in overrides.values()):
            raise CloneError(f"Port group {policy} inheritance verification failed: {overrides}")


def create_network(client, name):
    progress("Checking existing vSwitches and port groups")
    switches = query(client, "list")
    groups = query(client, "portgroup", "list")
    if any(row.get("Name") == name for row in switches):
        raise CloneError(f"vSwitch {name!r} already exists; no changes made")
    if any(row.get("Name") == name for row in groups):
        raise CloneError(f"Port group {name!r} already exists; no changes made")
    created = False
    try:
        progress(f"Creating vSwitch {name!r} with no uplinks")
        remote(client, "esxcli", "network", "vswitch", "standard", "add", "--vswitch-name", name)
        created = True
        progress("Enabling promiscuous mode, MAC address changes, and forged transmits on the vSwitch")
        remote(client, "esxcli", "network", "vswitch", "standard", "policy", "security", "set",
               "--vswitch-name", name, "--allow-promiscuous", "true",
               "--allow-mac-change", "true", "--allow-forged-transmits", "true")
        progress(f"Creating port group {name!r} on vSwitch {name!r}, with inherited policies")
        remote(client, "esxcli", "network", "vswitch", "standard", "portgroup", "add",
               "--portgroup-name", name, "--vswitch-name", name)
        # Do not set port group policy overrides. ESXi defaults inherit the switch.
        verify(client, name)
        progress(f"Created {name!r}: vSwitch with no uplinks; same-named port group; all policies inherit")
    except (Exception, KeyboardInterrupt):
        if created:
            progress(f"Setup incomplete: inspect vSwitch/port group {name!r} before retrying. No resources were automatically deleted.")
        raise


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("name", help="Name for both the vSwitch and port group")
    result.set_defaults(creds_file=None, host=None, user=None, port=None)
    return result


def main():
    arg_parser = parser()
    args = arg_parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,30}", args.name):
        arg_parser.error("name must be 1–31 letters, digits, spaces, '.', '_' or '-', starting with a letter or digit")
    client = paramiko.SSHClient()
    try:
        progress("Loading saved ESXi credentials")
        password = load_credentials(args)
        if password is None:
            raise CloneError("Set host, user, and password in creds.json beside this script")
        client.load_system_host_keys()
        progress(f"Connecting to {args.host}:{args.port} as {args.user}")
        client.connect(args.host, port=args.port, username=args.user, password=password,
                       look_for_keys=False, allow_agent=False, timeout=20,
                       banner_timeout=20, auth_timeout=20)
        client.get_transport().set_keepalive(30)
        create_network(client, args.name)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Error: {exc or 'Interrupted'}", file=sys.stderr, flush=True)
        return 1
    finally:
        client.close()
        progress("SSH connection closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
