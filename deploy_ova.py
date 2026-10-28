#!/usr/bin/env python3
"""Deploy a local OVA to ESXi, leaving the VM powered off."""

import argparse
import copy
import getpass
import posixpath
import re
import ssl
import sys
import tarfile
import threading
import time
from urllib.parse import unquote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

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
    for member in archive.getmembers():
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
    root = ET.fromstring(descriptor)
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


def configure_nics(spec, network, ge6, expected):
    nics = [change.device for change in spec.deviceChange or []
            if isinstance(change.device, vim.vm.device.VirtualEthernetCard)]
    if len(nics) != expected:
        raise CloneError("OVF and imported NIC counts differ; refusing to guess GE6")
    for index, nic in enumerate(nics):
        nic.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
            deviceName=network.name, network=network)
        nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo(
            startConnected=index == ge6, connected=False, allowGuestControl=False)
        # Leave address generation to ESXi instead of reusing an exported MAC.
        nic.addressType = "generated"
        nic.macAddress = None
        progress(f"NIC {index + 1}: VM Network, {'enabled (GE6)' if index == ge6 else 'disconnected'}")
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


def finish_vm(vm, iso, network, ge6):
    progress("Applying final network and ISO settings")
    if vm.runtime.powerState != "poweredOff":
        raise CloneError("Imported VM unexpectedly powered on; refusing to reconfigure")
    nics = [device for device in vm.config.hardware.device if isinstance(device, vim.vm.device.VirtualEthernetCard)]
    if ge6 >= len(nics):
        raise CloneError("Imported VM is missing GE6")
    spec = vim.vm.ConfigSpec(bootOptions=vim.vm.BootOptions(enterBIOSSetup=False))
    spec.deviceChange = cdrom_change(vm.config.hardware.device, iso)
    for index, device in enumerate(nics):
        nic = copy.deepcopy(device)
        nic.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName=network.name, network=network)
        nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo(
            startConnected=index == ge6, connected=False, allowGuestControl=False)
        spec.deviceChange.append(vim.vm.device.VirtualDeviceSpec(operation="edit", device=nic))
    wait_task(vm.ReconfigVM_Task(spec=spec), "VM configuration")
    config = vm.config
    actual_nics = [device for device in config.hardware.device if isinstance(device, vim.vm.device.VirtualEthernetCard)]
    if len(actual_nics) != len(nics) or any(
            nic.backing.network != network or nic.connectable.startConnected != (index == ge6)
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
            configure_nics(result.importSpec.configSpec, network, ge6, nic_count)
            result.importSpec.configSpec.bootOptions = vim.vm.BootOptions(enterBIOSSetup=False)
            progress(f"Deploying {args.name!r} to {datastore.name}; VM will remain powered off")
            lease = host.parent.resourcePool.ImportVApp(spec=result.importSpec,
                folder=datacenter.vmFolder, host=host)
            vm = upload(lease, archive, members, base, result.fileItem, args.host, args.https_port, verify)
            imported = True
            finish_vm(vm, iso, network, ge6)
            disable_autostart(host, vm)
            progress(f"Deployment complete: {vm.name} ({vm._moId}). Powered off. Only GE6 enabled; all NICs on VM Network.")
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
