#!/usr/bin/env python3
"""Deploy a local OVA to ESXi, leaving the VM powered off."""

import argparse
import copy
import getpass
import json
from pathlib import Path
import paramiko
from create_test_network import create_network, query
from clone_vm import load_ssh_host_keys
import posixpath
import re
import ssl
import sys
import tarfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit, urlunsplit
from defusedxml import ElementTree as ET

import requests
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

from clone_vm import CloneError, load_credentials, progress


OVF = "http://schemas.dmtf.org/ovf/envelope/1"
RASD = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
NS = {"ovf": OVF, "rasd": RASD}


def wait_task(task, description, timeout=300):
    deadline = time.monotonic() + timeout
    last_update = 0
    while task.info.state not in ("success", "error"):
        if time.monotonic() >= deadline:
            raise CloneError(f"Timed out waiting for {description}; inspect ESXi before retrying")
        if time.monotonic() - last_update >= 10:
            progress(f"Waiting for {description}")
            last_update = time.monotonic()
        time.sleep(1)
    if task.info.state == "error":
        raise CloneError(f"{description} failed: {task.info.error}")
    return task.info.result


def objects(content, kind):
    view = content.viewManager.CreateContainerView(content.rootFolder, [kind], True)
    try:
        return list(view.view)
    finally:
        view.Destroy()


def unique_named(items, name, description):
    matches = [item for item in items if item.name == name]
    if len(matches) != 1:
        raise CloneError(f"Expected one {description} named {name!r}; found {len(matches)}")
    return matches[0]


def ova_descriptor(archive):
    members = {}
    for count, member in enumerate(archive, 1):
        if count > 10000:
            raise CloneError("OVA contains too many archive members")
        name = posixpath.normpath(member.name)
        if name.startswith("/") or name == ".." or name.startswith("../"):
            raise CloneError(f"Unsafe OVA member: {member.name}")
        if member.isfile():
            if name in members:
                raise CloneError(f"Duplicate OVA member: {name}")
            members[name] = member
    descriptors = [name for name in members if name.lower().endswith(".ovf")]
    if len(descriptors) != 1:
        raise CloneError("OVA must contain exactly one OVF descriptor")
    name = descriptors[0]
    if members[name].size > 16 * 1024 * 1024:
        raise CloneError("OVF descriptor is unexpectedly large")
    with archive.extractfile(members[name]) as stream:
        descriptor = stream.read().decode("utf-8-sig")
    root = ET.fromstring(descriptor, forbid_dtd=True)
    if len(root.findall("ovf:VirtualSystem", NS)) != 1 or root.find("ovf:VirtualSystemCollection", NS) is not None:
        raise CloneError("Only single-VM OVAs are supported")
    if any(item.get(f"{{{OVF}}}compression", "identity") not in ("identity", "")
           for item in root.findall("ovf:References/ovf:File", NS)):
        raise CloneError("Compressed OVF file references are not supported")
    return descriptor, root, members, posixpath.dirname(name)


def ge6_index(root, override=None):
    nics = [item for item in root.findall(".//ovf:VirtualHardwareSection/ovf:Item", NS)
            if item.findtext("rasd:ResourceType", namespaces=NS) == "10"]
    labels = []
    for item in nics:
        labels.append(" / ".join((item.findtext(f"rasd:{field}", default="", namespaces=NS) or "")
                                  for field in ("ElementName", "Caption", "Description", "Connection")))
    for number, label in enumerate(labels, 1):
        progress(f"OVA NIC {number}: {label}")
    if override is not None:
        if not 1 <= override <= len(nics):
            raise CloneError(f"--ge6-nic must be between 1 and {len(nics)}")
        return override - 1, len(nics)
    matches = [i for i, label in enumerate(labels) if re.search(r"\bGE6\b", label, re.I)]
    if len(matches) != 1:
        raise CloneError("Cannot uniquely identify GE6 in the OVF. Use --ge6-nic N (1-based OVF NIC order shown above)")
    return matches[0], len(nics)


def interface_labels(root):
    labels = []
    for item in root.findall(".//ovf:VirtualHardwareSection/ovf:Item", NS):
        if item.findtext("rasd:ResourceType", namespaces=NS) != "10":
            continue
        metadata = " / ".join(item.findtext(f"rasd:{field}", default="", namespaces=NS) or ""
                              for field in ("ElementName", "Caption", "Description", "Connection"))
        matches = set(re.findall(r"\bGE[1-6]\b", metadata, re.I))
        matches = {match.upper() for match in matches}
        labels.append(next(iter(matches)) if len(matches) == 1 else
                      item.findtext("rasd:ElementName", default=f"NIC {len(labels)+1}", namespaces=NS))
    return labels


def validate_nic_mappings(value):
    if not isinstance(value, list) or len(value) > 128:
        raise CloneError("Invalid interface mappings.")
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise CloneError("Invalid interface mapping.")
        index = item.get("index")
        label, network = item.get("label"), item.get("network")
        if type(index) is not int or not 1 <= index <= 128 or index in seen:
            raise CloneError("Invalid or duplicate interface position.")
        if not all(isinstance(v, str) and 0 < len(v) <= 256 and not any(ord(c) < 32 for c in v) for v in (label, network)):
            raise CloneError("Invalid interface label or network name.")
        if type(item.get("connected")) is not bool or type(item.get("create", False)) is not bool:
            raise CloneError("Interface connection settings must be booleans.")
        if item.get("create") and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,30}", network):
            raise CloneError("New network names must be 1–31 letters, digits, spaces, '.', '_' or '-'.")
        seen.add(index)
        result.append(dict(index=index, label=label, network=network, connected=item["connected"], create=item.get("create", False)))
    return result


def create_nic_networks(args, password, overrides):
    names = sorted({item["network"] for item in overrides if item.get("create")})
    if not names:
        return
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,30}", name) for name in names):
        raise CloneError("Invalid new network name")
    client = paramiko.SSHClient()
    try:
        load_ssh_host_keys(client)
        client.connect(args.host, port=args.port or 22, username=args.user, password=password,
                       look_for_keys=False, allow_agent=False, timeout=20, banner_timeout=20, auth_timeout=20)
        existing = {row.get("Name") for row in query(client, "list")} | {row.get("Name") for row in query(client, "portgroup", "list")}
        if any(name in existing for name in names):
            raise CloneError("A requested new switch or port group already exists; select its existing network instead")
        for name in names:
            progress(f"Creating deployment network {name!r}; created networks are retained if deployment fails")
            create_network(client, name)
    finally:
        client.close()


def resolve_nic_settings(root, ge6, network, networks, overrides):
    labels = interface_labels(root)
    settings = [(network, i == ge6) for i in range(len(labels))]
    seen = set()
    for item in overrides:
        index = item["index"] - 1
        if index in seen or not 0 <= index < len(labels) or item["label"] != labels[index]:
            raise CloneError("OVA interfaces changed; reload the interface list before deploying")
        seen.add(index)
        target = unique_named(networks, item["network"], "network")
        if isinstance(target, vim.dvs.DistributedVirtualPortgroup):
            raise CloneError("Only standard ESXi port groups are supported")
        settings[index] = (target, item["connected"])
    return settings


def configure_nics(spec, network, ge6, expected, settings=None):
    nics = [change.device for change in spec.deviceChange or []
            if isinstance(change.device, vim.vm.device.VirtualEthernetCard)]
    if len(nics) != expected:
        raise CloneError("OVF and imported NIC counts differ; refusing to guess GE6")
    settings = settings or [(network, i == ge6) for i in range(expected)]
    for index, nic in enumerate(nics):
        target, connected = settings[index]
        nic.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
            deviceName=target.name, network=target)
        nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo(
            startConnected=connected, connected=False, allowGuestControl=False)
        # Leave address generation to ESXi instead of reusing an exported MAC.
        nic.addressType = "generated"
        nic.macAddress = None
        progress(f"NIC {index + 1}: {target.name}, {'connected at power on' if connected else 'disconnected'}")
    return [nic.key for nic in nics]


def find_iso(datastore, folder):
    folder = folder.strip('/')
    if '/' not in folder:
        listing = wait_task(datastore.browser.SearchDatastore_Task(
            datastorePath=f"[{datastore.name}]",
            searchSpec=vim.host.DatastoreBrowser.SearchSpec(
                query=[vim.host.DatastoreBrowser.FolderQuery()])), "datastore folder lookup")
        names = [entry.path.rstrip('/') for entry in listing.file or []]
        if folder not in names:
            matches = [name for name in names if re.sub(r"\s+", "", name).casefold()
                       == re.sub(r"\s+", "", folder).casefold()]
            if len(matches) == 1:
                progress(f"Using datastore folder {matches[0]!r} for {folder!r}")
                folder = matches[0]
    path = f"[{datastore.name}] {folder.strip('/')}"
    progress(f"Finding cloud-init.iso under {path}")
    results = wait_task(datastore.browser.SearchDatastoreSubFolders_Task(
        datastorePath=path,
        searchSpec=vim.host.DatastoreBrowser.SearchSpec(matchPattern=["cloud-init.iso"])),
        "ISO search")
    paths = [result.folderPath.rstrip("/") + "/" + file.path
             for result in results or [] for file in result.file or [] if file.path == "cloud-init.iso"]
    if len(paths) != 1:
        raise CloneError(f"Expected one cloud-init.iso under {path}; found {len(paths)}")
    progress(f"Found ISO: {paths[0]}")
    return paths[0]


class UploadReader:
    def __init__(self, stream, size, label, sent):
        self.stream, self.size, self.label, self.sent = stream, size, label, sent
        self.position = 0
        self.last_percent = -1

    def __len__(self):
        return self.size

    def read(self, size=-1):
        chunk = self.stream.read(min(size, 1024 * 1024) if size and size > 0 else 1024 * 1024)
        if chunk:
            self.position += len(chunk)
            self.sent(len(chunk))
            percent = int(self.position * 100 / max(self.size, 1))
            if percent != self.last_percent:
                progress(f"Uploading {self.label}: {percent}% ({self.position / 1048576:.1f}/{self.size / 1048576:.1f} MiB)")
                self.last_percent = percent
        return chunk


def lease_url(url, host, port):
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise CloneError("ESXi returned a non-HTTPS upload URL")
    if parsed.hostname == "*":
        address = f"[{host}]" if ":" in host else host
        url = urlunsplit(parsed._replace(netloc=f"{address}:{parsed.port or port}"))
    return url


def upload(lease, archive, members, base, file_items, host, port, verify):
    progress("Waiting for ESXi upload lease")
    deadline = time.monotonic() + 120
    while lease.state == vim.HttpNfcLease.State.initializing:
        if time.monotonic() >= deadline:
            raise CloneError("Timed out waiting for ESXi upload lease")
        time.sleep(1)
    if lease.state != vim.HttpNfcLease.State.ready:
        raise CloneError(f"Upload lease is not ready: {lease.error}")
    urls = {item.importKey: item.url for item in lease.info.deviceUrl}
    files = []
    for item in file_items:
        path = posixpath.normpath(posixpath.join(base, unquote(item.path)))
        member = members.get(path)
        if member is None or item.deviceId not in urls:
            raise CloneError(f"Missing OVA payload or upload target for {item.path}")
        if item.size and member.size != item.size:
            raise CloneError(f"OVA payload size does not match OVF: {item.path}")
        files.append((item, member))
    total = sum(member.size for _, member in files)
    sent_bytes = 0
    errors = []
    stop = threading.Event()

    def sent(count):
        nonlocal sent_bytes
        sent_bytes += count
        if errors:
            raise CloneError(f"Upload lease heartbeat failed: {errors[0]}")

    def heartbeat():
        while not stop.wait(5):
            try:
                lease.HttpNfcLeaseProgress(min(99, int(sent_bytes * 100 / max(total, 1))))
            except Exception as exc:
                errors.append(exc)
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        with requests.Session() as session:
            # Upload URLs carry ESXi lease authorization; do not send account credentials.
            session.trust_env = False
            for index, (item, member) in enumerate(files, 1):
                progress(f"Uploading payload {index}/{len(files)}: {item.path}")
                with archive.extractfile(member) as stream:
                    reader = UploadReader(stream, member.size, item.path, sent)
                    response = session.request("PUT" if item.create else "POST",
                        lease_url(urls[item.deviceId], host, port), data=reader,
                        headers={"Content-Type": "application/x-vnd.vmware-streamVmdk",
                                 "Content-Length": str(member.size)},
                        verify=verify, timeout=(30, 300), allow_redirects=False)
                    if not 200 <= response.status_code < 300:
                        raise CloneError(f"ESXi rejected {item.path}: HTTP {response.status_code}")
                    if reader.position != member.size:
                        raise CloneError(f"Incomplete upload: {item.path}")
                    response.close()
                progress(f"ESXi accepted {item.path}")
        if errors:
            raise CloneError(f"Upload lease heartbeat failed: {errors[0]}")
    except requests.RequestException:
        # Lease URLs contain temporary authorization; raw HTTP errors can reveal them.
        raise CloneError("ESXi payload upload failed. Check connectivity and inspect the VM before retrying.") from None
    finally:
        stop.set()
        thread.join()
    progress("Completing OVA import")
    vm = lease.info.entity
    lease.HttpNfcLeaseProgress(100)
    lease.HttpNfcLeaseComplete()
    return vm


def cdrom_change(devices, iso):
    existing = [device for device in devices if isinstance(device, vim.vm.device.VirtualCdrom)]
    changes = []
    if existing:
        cdrom = copy.deepcopy(existing[0])
        operation = "edit"
    else:
        # Use a free IDE unit, adding a controller if the appliance has none.
        controllers = [device for device in devices if isinstance(device, vim.vm.device.VirtualIDEController)]
        slots = [(controller.key, unit) for controller in controllers for unit in (0, 1)
                 if not any(getattr(device, "controllerKey", None) == controller.key
                            and getattr(device, "unitNumber", None) == unit for device in devices)]
        if not slots:
            used = {controller.busNumber for controller in controllers}
            bus = next((bus for bus in (0, 1) if bus not in used), None)
            if bus is None:
                raise CloneError("No free IDE CD-ROM slot; cannot mount cloud-init.iso")
            controller = vim.vm.device.VirtualIDEController(key=-200, busNumber=bus)
            changes.append(vim.vm.device.VirtualDeviceSpec(operation="add", device=controller))
            slots = [(controller.key, 0)]
        controller, unit = slots[0]
        cdrom = vim.vm.device.VirtualCdrom(key=-201, controllerKey=controller, unitNumber=unit)
        operation = "add"
    cdrom.backing = vim.vm.device.VirtualCdrom.IsoBackingInfo(fileName=iso)
    cdrom.connectable = vim.vm.device.VirtualDevice.ConnectInfo(
        startConnected=True, connected=False, allowGuestControl=False)
    changes.append(vim.vm.device.VirtualDeviceSpec(operation=operation, device=cdrom))
    return changes


def finish_vm(vm, iso, network, ge6, settings=None):
    progress("Applying final network and ISO settings")
    if vm.runtime.powerState != "poweredOff":
        raise CloneError("Imported VM unexpectedly powered on; refusing to reconfigure")
    nics = [device for device in vm.config.hardware.device if isinstance(device, vim.vm.device.VirtualEthernetCard)]
    if ge6 >= len(nics):
        raise CloneError("Imported VM is missing GE6")
    spec = vim.vm.ConfigSpec(bootOptions=vim.vm.BootOptions(enterBIOSSetup=False))
    spec.deviceChange = cdrom_change(vm.config.hardware.device, iso)
    settings = settings or [(network, i == ge6) for i in range(len(nics))]
    for index, device in enumerate(nics):
        target, connected = settings[index]
        nic = copy.deepcopy(device)
        nic.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName=target.name, network=target)
        nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo(
            startConnected=connected, connected=False, allowGuestControl=False)
        spec.deviceChange.append(vim.vm.device.VirtualDeviceSpec(operation="edit", device=nic))
    wait_task(vm.ReconfigVM_Task(spec=spec), "VM configuration")
    config = vm.config
    actual_nics = [device for device in config.hardware.device if isinstance(device, vim.vm.device.VirtualEthernetCard)]
    if len(actual_nics) != len(nics) or any(
            nic.backing.network != settings[index][0] or nic.connectable.startConnected != settings[index][1]
            or nic.connectable.allowGuestControl for index, nic in enumerate(actual_nics)):
        raise CloneError("Network configuration verification failed")
    if not any(isinstance(device, vim.vm.device.VirtualCdrom)
               and getattr(device.backing, "fileName", None) == iso and device.connectable.startConnected
               for device in config.hardware.device):
        raise CloneError("ISO attachment verification failed")
    if config.bootOptions.enterBIOSSetup or vm.runtime.powerState != "poweredOff":
        raise CloneError("Power/normal boot configuration verification failed")


def disable_autostart(host, vm):
    progress("Disabling ESXi automatic startup for this VM")
    manager = host.configManager.autoStartManager
    entries = [entry for entry in manager.config.powerInfo or [] if entry.key != vm]
    entries.append(vim.host.AutoStartManager.AutoPowerInfo(key=vm,
        startOrder=-1, startDelay=-1, waitForHeartbeat="systemDefault",
        startAction="none", stopDelay=-1, stopAction="none"))
    manager.ReconfigureAutostart(spec=vim.host.AutoStartManager.Config(powerInfo=entries))
    if any(entry.key == vm and entry.startAction != "none" for entry in manager.config.powerInfo or []):
        raise CloneError("VM automatic startup was not disabled")


def deploy(args):
    password = load_credentials(args)
    if password is None:
        password = getpass.getpass(f"Password for {args.user}@{args.host}: ")
    overrides = []
    if getattr(args, "nic_config", None):
        with args.nic_config.open("rb") as stream:
            raw = stream.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise CloneError("Interface configuration is too large")
        overrides = validate_nic_mappings(json.loads(raw))
    context = ssl._create_unverified_context() if args.insecure else ssl.create_default_context(cafile=args.ca_file)
    verify = False if args.insecure else args.ca_file or True
    service, lease, imported = None, None, False
    try:
        progress(f"Reading OVA: {args.ova}")
        with tarfile.open(args.ova, "r:*") as archive:
            descriptor, root, members, base = ova_descriptor(archive)
            ge6, nic_count = ge6_index(root, args.ge6_nic)
            progress(f"Connecting to ESXi {args.host}:{args.https_port} as {args.user}")
            service = SmartConnect(host=args.host, port=args.https_port, user=args.user,
                                   pwd=password, sslContext=context)
            content = service.RetrieveContent()
            hosts = objects(content, vim.HostSystem)
            if len(hosts) != 1:
                raise CloneError("Connect directly to a standalone ESXi host, not vCenter")
            host = hosts[0]
            if any(vm.name == args.name for vm in objects(content, vim.VirtualMachine)):
                raise CloneError(f"VM name already exists: {args.name}")
            datastore = unique_named(host.datastore, args.datastore, "datastore")
            network = unique_named(host.network, "VM Network", "network")
            if isinstance(network, vim.dvs.DistributedVirtualPortgroup):
                raise CloneError("This script requires a standard VM Network port group")
            # Validate every mapping and existing target before creating networks.
            pending = {item["network"] for item in overrides if item.get("create")}
            resolve_nic_settings(root, ge6, network,
                list(host.network) + [SimpleNamespace(name=name) for name in pending], overrides)
            iso_datastore = unique_named(host.datastore, args.iso_datastore or args.datastore, "ISO datastore")
            iso = find_iso(iso_datastore, args.iso_folder)
            datacenter = host.parent
            while datacenter and not isinstance(datacenter, vim.Datacenter):
                datacenter = datacenter.parent
            if datacenter is None:
                raise CloneError("Cannot locate the host's VM folder")
            progress("Validating OVA and mapping all networks to VM Network")
            mappings = [vim.OvfManager.NetworkMapping(name=item.get(f"{{{OVF}}}name"), network=network)
                        for item in root.findall("ovf:NetworkSection/ovf:Network", NS)]
            result = content.ovfManager.CreateImportSpec(
                ovfDescriptor=descriptor, resourcePool=host.parent.resourcePool, datastore=datastore,
                cisp=vim.OvfManager.CreateImportSpecParams(entityName=args.name, hostSystem=host,
                    diskProvisioning=args.disk_format, networkMapping=mappings))
            for warning in result.warning or []:
                progress(f"OVA warning: {warning.localizedMessage}")
            if result.error:
                raise CloneError("OVA validation failed: " + "; ".join(error.localizedMessage for error in result.error))
            create_nic_networks(args, password, overrides)
            settings = resolve_nic_settings(root, ge6, network, host.network, overrides)
            configure_nics(result.importSpec.configSpec, network, ge6, nic_count, settings)
            result.importSpec.configSpec.bootOptions = vim.vm.BootOptions(enterBIOSSetup=False)
            progress(f"Deploying {args.name!r} to {datastore.name}; VM will remain powered off")
            lease = host.parent.resourcePool.ImportVApp(spec=result.importSpec,
                folder=datacenter.vmFolder, host=host)
            vm = upload(lease, archive, members, base, result.fileItem, args.host, args.https_port, verify)
            imported = True
            finish_vm(vm, iso, network, ge6, settings)
            disable_autostart(host, vm)
            progress(f"Deployment complete: {vm.name} ({vm._moId}). Powered off. Interface mappings and connection settings applied.")
            progress(f"CD/DVD: {iso}.")
    except BaseException:
        if lease is not None and not imported:
            try:
                lease.HttpNfcLeaseAbort(vim.fault.SystemError(reason="OVA deployment interrupted or failed"))
            except Exception:
                pass
        if lease is not None:
            progress(f"Deployment did not finish. Inspect VM {args.name!r} and its datastore files before retrying; no existing VM is deleted.")
        raise
    finally:
        if service is not None:
            Disconnect(service)
            progress("ESXi session closed")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("ova", help="Local OVA file path")
    result.add_argument("--name", required=True, help="VM inventory and directory name")
    result.add_argument("--creds-file", help="JSON connection credentials; defaults to creds.json beside this script")
    result.add_argument("--host", help="Override ESXi host from credentials")
    result.add_argument("--user", help="Override ESXi user from credentials")
    result.add_argument("--https-port", type=int, default=443)
    result.add_argument("--datastore", default="datastore1")
    result.add_argument("--iso-datastore", help="ISO datastore, defaults to deployment datastore")
    result.add_argument("--iso-folder", default="00-ISO")
    result.add_argument("--nic-config", type=Path, help="JSON per-interface mappings (index, label, network, connected)")
    result.add_argument("--ge6-nic", type=int, help="Override GE6 detection using 1-based OVF NIC order")
    result.add_argument("--disk-format", choices=("thin", "thick", "eagerZeroedThick"), default="thin")
    tls = result.add_mutually_exclusive_group()
    tls.add_argument("--ca-file", help="CA PEM file to verify ESXi HTTPS certificate")
    tls.add_argument("--insecure", action="store_true", help="Explicitly allow a self-signed/untrusted ESXi HTTPS certificate")
    # Shared credentials loader handles an optional SSH port; HTTPS uses --https-port.
    result.set_defaults(port=None)
    return result


def main():
    arg_parser = parser()
    args = arg_parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,79}", args.name):
        arg_parser.error("--name must be 1–80 letters, digits, spaces, '.', '_' or '-', starting with a letter or digit")
    try:
        deploy(args)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Error: {exc or 'Interrupted'}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
