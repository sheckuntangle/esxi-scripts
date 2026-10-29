import io
import tarfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from pyVmomi import vim
import deploy_ova as d


def descriptor(labels):
    items = ''.join(f'''<Item><rasd:ResourceType>10</rasd:ResourceType>
        <rasd:ElementName>{label}</rasd:ElementName></Item>''' for label in labels)
    return f'''<Envelope xmlns="{d.OVF}" xmlns:rasd="{d.RASD}">
        <VirtualSystem><VirtualHardwareSection>{items}</VirtualHardwareSection></VirtualSystem></Envelope>'''


class DeployTests(unittest.TestCase):
    def test_ova_rejects_dtd_and_excessive_archive_members(self):
        payload = ('<!DOCTYPE Envelope [<!ENTITY x "expanded">]>' + descriptor(['GE6'])).encode()
        archive = Mock()
        member = tarfile.TarInfo('vm.ovf'); member.size = len(payload)
        archive.__iter__ = Mock(return_value=iter([member]))
        archive.extractfile.return_value = io.BytesIO(payload)
        with self.assertRaises(Exception) as caught:
            d.ova_descriptor(archive)
        self.assertIn('DTDForbidden', type(caught.exception).__name__)
        archive.__iter__ = Mock(return_value=(tarfile.TarInfo(f'{i}.txt') for i in range(10001)))
        with self.assertRaisesRegex(d.CloneError, 'too many'):
            d.ova_descriptor(archive)

    @patch('deploy_ova.SmartConnect')
    @patch('deploy_ova.load_credentials', return_value='secret')
    def test_cli_rejects_invalid_mapping_before_esxi_connection(self, credentials, connect):
        import tempfile
        from pathlib import Path
        import json
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'interfaces.json'
            path.write_text(json.dumps([dict(index=True, label='GE1', network='lab', connected=True)]))
            with self.assertRaises(d.CloneError):
                d.deploy(SimpleNamespace(nic_config=path))
        connect.assert_not_called()

    @patch('deploy_ova.requests.Session')
    def test_upload_error_hides_temporary_lease_token(self, session):
        lease = Mock(); lease.state = vim.HttpNfcLease.State.ready
        lease.info.deviceUrl = [SimpleNamespace(importKey='disk', url='https://esxi.test/upload?token=private-lease-token')]
        session.return_value.__enter__.return_value.request.side_effect = d.requests.ConnectionError('https://esxi.test/upload?token=private-lease-token')
        archive = Mock(); archive.extractfile.return_value = io.BytesIO(b'abc')
        member = tarfile.TarInfo('disk.vmdk'); member.size=3
        item = SimpleNamespace(path='disk.vmdk',deviceId='disk',size=3,create=True)
        with self.assertRaises(d.CloneError) as caught:
            d.upload(lease, archive, {'disk.vmdk':member}, '', [item], 'esxi.test', 443, False)
        self.assertNotIn('private-lease-token', str(caught.exception))
        lease.HttpNfcLeaseComplete.assert_not_called()

    def test_ge6_uses_label_not_assumed_sixth_slot(self):
        self.assertEqual(d.ge6_index(ET.fromstring(descriptor(['GE1', 'GE6', 'GE2']))), (1, 3))

    def test_interface_labels_use_ge_names_in_metadata_without_assuming_order(self):
        root = ET.fromstring(descriptor(['Network adapter 1', 'Network adapter 2']))
        items = root.findall('.//ovf:Item', d.NS)
        ET.SubElement(items[0], '{'+d.RASD+'}Description').text = 'VmxNet3 ethernet adapter GE6'
        ET.SubElement(items[0], '{'+d.RASD+'}Connection').text = 'GE6'
        ET.SubElement(items[1], '{'+d.RASD+'}Connection').text = 'GE1'
        self.assertEqual(d.interface_labels(root), ['GE6', 'GE1'])

    def test_unlabelled_and_duplicate_ge6_require_override(self):
        for labels in (['Ethernet 1', 'Ethernet 2'], ['GE6', 'GE6']):
            root = ET.fromstring(descriptor(labels))
            with self.assertRaises(d.CloneError):
                d.ge6_index(root)
            self.assertEqual(d.ge6_index(root, 2), (1, 2))
            with self.assertRaises(d.CloneError):
                d.ge6_index(root, 3)

    def test_all_nics_mapped_only_ge6_enabled_and_mac_reset(self):
        network = vim.Network('network-test')
        nics = [vim.vm.device.VirtualVmxnet3(key=4000+i, addressType='manual',
                 macAddress=f'00:50:56:00:00:0{i}') for i in range(3)]
        spec = vim.vm.ConfigSpec(deviceChange=[vim.vm.device.VirtualDeviceSpec(operation='add', device=nic) for nic in nics])
        # Avoid an API round-trip on this test managed object.
        with patch.object(vim.Network, 'name', new_callable=lambda: property(lambda self: 'VM Network')):
            d.configure_nics(spec, network, 1, 3)
        for index, nic in enumerate(nics):
            self.assertEqual(nic.backing.network, network)
            self.assertEqual(nic.connectable.startConnected, index == 1)
            self.assertFalse(nic.connectable.allowGuestControl)
            self.assertFalse(nic.connectable.connected)
            self.assertEqual(nic.addressType, 'generated')
            self.assertIsNone(nic.macAddress)

    def test_per_interface_network_and_connection_overrides(self):
        root = ET.fromstring(descriptor(['GE1', 'GE6', 'GE2']))
        default, internal = vim.Network('default'), vim.Network('internal')
        with patch.object(vim.Network, 'name', new_callable=lambda: property(lambda n: 'VM Network' if n == default else 'lab')):
            settings = d.resolve_nic_settings(root, 1, default, [default, internal],
                [dict(index=1, label='GE1', network='lab', connected=True)])
            self.assertEqual(settings, [(internal, True), (default, True), (default, False)])
            nics = [vim.vm.device.VirtualVmxnet3(key=4000+i) for i in range(3)]
            spec = vim.vm.ConfigSpec(deviceChange=[vim.vm.device.VirtualDeviceSpec(operation='add', device=n) for n in nics])
            d.configure_nics(spec, default, 1, 3, settings)
            self.assertEqual([n.backing.network for n in nics], [internal, default, default])
            self.assertEqual([n.connectable.startConnected for n in nics], [True, True, False])
            self.assertFalse(any(n.connectable.connected for n in nics))

    def test_mapping_rejects_changed_ova_and_unknown_network_before_mutation(self):
        root = ET.fromstring(descriptor(['GE1', 'GE6']))
        for mapping in (dict(index=3, label='GE1', network='missing', connected=True),
                        dict(index=1, label='Wrong label', network='missing', connected=True),
                        dict(index=1, label='GE1', network='missing', connected=True)):
            with self.assertRaises(d.CloneError):
                d.resolve_nic_settings(root, 1, Mock(), [], [mapping])

    @patch('deploy_ova.create_network')
    @patch('deploy_ova.query')
    @patch('deploy_ova.load_ssh_host_keys')
    @patch('deploy_ova.paramiko.SSHClient')
    def test_new_network_reuses_script_and_deduplicates_shared_targets(self, ssh, keys, query, create):
        query.return_value = []
        mappings = [dict(network='lab', create=True), dict(network='lab', create=True)]
        args = SimpleNamespace(host='esxi.test', port=22, user='test')
        d.create_nic_networks(args, 'secret', mappings)
        create.assert_called_once_with(ssh.return_value, 'lab')
        ssh.return_value.close.assert_called_once()
        create.reset_mock()
        query.return_value = [dict(Name='lab')]
        with self.assertRaises(d.CloneError):
            d.create_nic_networks(args, 'secret', mappings)
        create.assert_not_called()

    def test_nic_count_mismatch_aborts(self):
        with self.assertRaises(d.CloneError):
            d.configure_nics(vim.vm.ConfigSpec(), Mock(), 5, 6)

    def test_iso_adds_cdrom_and_controller_when_missing(self):
        changes = d.cdrom_change([], '[ds] 00-ISO/cloud-init.iso')
        self.assertEqual(len(changes), 2)
        cdrom = changes[1].device
        self.assertEqual(cdrom.controllerKey, changes[0].device.key)
        self.assertEqual(cdrom.backing.fileName, '[ds] 00-ISO/cloud-init.iso')
        self.assertTrue(cdrom.connectable.startConnected)

    def test_existing_cdrom_edited_without_changing_original(self):
        original = vim.vm.device.VirtualCdrom(key=3000,
            backing=vim.vm.device.VirtualCdrom.IsoBackingInfo(fileName='[ds] old.iso'))
        changes = d.cdrom_change([original], '[ds] 00-ISO/cloud-init.iso')
        self.assertEqual(changes[0].operation, 'edit')
        self.assertEqual(changes[0].device.key, 3000)
        self.assertEqual(original.backing.fileName, '[ds] old.iso')

    def test_no_free_cdrom_slot_aborts(self):
        devices = [vim.vm.device.VirtualIDEController(key=200+bus, busNumber=bus) for bus in (0, 1)]
        devices += [vim.vm.device.VirtualDisk(key=300+bus*2+unit, controllerKey=200+bus, unitNumber=unit)
                    for bus in (0, 1) for unit in (0, 1)]
        with self.assertRaises(d.CloneError):
            d.cdrom_change(devices, '[ds] 00-ISO/cloud-init.iso')

    def test_ova_read_does_not_extract_and_rejects_traversal(self):
        for name in ('appliance.ovf', '../appliance.ovf'):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode='w') as archive:
                content = descriptor(['GE6']).encode()
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            data.seek(0)
            with tarfile.open(fileobj=data, mode='r') as archive:
                if name.startswith('..'):
                    with self.assertRaises(d.CloneError):
                        d.ova_descriptor(archive)
                else:
                    text, root, members, base = d.ova_descriptor(archive)
                    self.assertEqual(d.ge6_index(root), (0, 1))

    def test_upload_progress_and_byte_count(self):
        sent = Mock()
        with patch('deploy_ova.progress') as progress:
            reader = d.UploadReader(io.BytesIO(b'abcdef'), 6, 'disk.vmdk', sent)
            self.assertEqual(len(reader), 6)
            self.assertEqual(reader.read(3), b'abc')
            self.assertEqual(reader.read(3), b'def')
            self.assertEqual(reader.read(3), b'')
            self.assertEqual(reader.position, 6)
            self.assertIn('50%', progress.call_args_list[0].args[0])
            self.assertIn('100%', progress.call_args_list[1].args[0])
            self.assertEqual(sent.call_count, 2)

    def test_lease_url_requires_https_and_expands_wildcard(self):
        self.assertEqual(d.lease_url('https://*/upload/id', '10.0.0.1', 443),
                         'https://10.0.0.1:443/upload/id')
        with self.assertRaises(d.CloneError):
            d.lease_url('http://*/upload/id', '10.0.0.1', 443)

    def test_autostart_preserves_other_vm_entries(self):
        vm = vim.VirtualMachine('new')
        other = vim.host.AutoStartManager.AutoPowerInfo(key=vim.VirtualMachine('existing'), startAction='powerOn')
        manager = Mock()
        manager.config.powerInfo = [other]
        host = SimpleNamespace(configManager=SimpleNamespace(autoStartManager=manager))
        d.disable_autostart(host, vm)
        entries = manager.ReconfigureAutostart.call_args.kwargs['spec'].powerInfo
        self.assertEqual(entries[0], other)
        self.assertEqual(entries[1].key, vm)
        self.assertEqual(entries[1].startAction, 'none')

    def test_task_failure_is_reported(self):
        with self.assertRaises(d.CloneError):
            d.wait_task(SimpleNamespace(info=SimpleNamespace(state='error', error='no space')), 'import')

    @patch('deploy_ova.wait_task')
    def test_final_configuration_mounts_iso_without_forcing_setup_or_power_on(self, wait):
        network = vim.Network('network-test')
        nics = [vim.vm.device.VirtualVmxnet3(key=4000+i) for i in range(2)]
        vm = Mock()
        vm.runtime.powerState = 'poweredOff'
        vm.config.hardware.device = nics
        def reconfigure(spec):
            vm.config.bootOptions = spec.bootOptions
            vm.config.hardware.device = [change.device for change in spec.deviceChange]
            return Mock()
        vm.ReconfigVM_Task.side_effect = reconfigure
        with patch.object(vim.Network, 'name', new_callable=lambda: property(lambda self: 'VM Network')):
            d.finish_vm(vm, '[ds] 00-ISO/cloud-init.iso', network, 1)
        self.assertFalse(vm.config.bootOptions.enterBIOSSetup)
        vm.PowerOnVM_Task.assert_not_called()
        vm.PowerOffVM_Task.assert_not_called()

    @patch('deploy_ova.wait_task')
    def test_final_configuration_preserves_custom_networks_and_connection_flags(self, wait):
        default, lab = vim.Network('default'), vim.Network('lab')
        vm = Mock(); vm.runtime.powerState = 'poweredOff'
        vm.config.hardware.device = [vim.vm.device.VirtualVmxnet3(key=4000+i) for i in range(3)]
        def reconfigure(spec):
            vm.config.bootOptions = spec.bootOptions
            vm.config.hardware.device = [change.device for change in spec.deviceChange]
        vm.ReconfigVM_Task.side_effect = reconfigure
        settings = [(lab, True), (default, True), (lab, False)]
        with patch.object(vim.Network, 'name', new_callable=lambda: property(lambda n: 'VM Network' if n == default else 'lab')):
            d.finish_vm(vm, '[ds] cloud-init.iso', default, 1, settings)
        nics = [n for n in vm.config.hardware.device if isinstance(n, vim.vm.device.VirtualEthernetCard)]
        self.assertEqual([(n.backing.network, n.connectable.startConnected) for n in nics], settings)
        vm.PowerOnVM_Task.assert_not_called()

    def test_final_configuration_refuses_running_vm(self):
        vm = Mock()
        vm.runtime.powerState = 'poweredOn'
        with self.assertRaises(d.CloneError):
            d.finish_vm(vm, '[ds] cloud-init.iso', Mock(), 0)
        vm.ReconfigVM_Task.assert_not_called()

    @patch('deploy_ova.wait_task')
    def test_iso_lookup_handles_actual_folder_spaces(self, wait):
        wait.side_effect = [SimpleNamespace(file=[SimpleNamespace(path='00 - ISO')]),
            [SimpleNamespace(folderPath='[datastore1] 00 - ISO/', file=[SimpleNamespace(path='cloud-init.iso')])]]
        datastore = Mock()
        datastore.name = 'datastore1'
        self.assertEqual(d.find_iso(datastore, '00-ISO'), '[datastore1] 00 - ISO/cloud-init.iso')

    @patch('deploy_ova.wait_task')
    def test_ambiguous_iso_is_rejected(self, wait):
        wait.side_effect = [SimpleNamespace(file=[SimpleNamespace(path='00-ISO')]),
            [SimpleNamespace(folderPath='[ds] 00-ISO/', file=[SimpleNamespace(path='cloud-init.iso'), SimpleNamespace(path='cloud-init.iso')])]]
        datastore = Mock()
        datastore.name = 'ds'
        with self.assertRaises(d.CloneError):
            d.find_iso(datastore, '00-ISO')

    @patch('deploy_ova.requests.Session')
    def test_upload_streams_and_completes_lease_after_acceptance(self, session):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w') as archive:
            member = tarfile.TarInfo('disk.vmdk')
            member.size = 6
            archive.addfile(member, io.BytesIO(b'abcdef'))
        data.seek(0)
        lease = Mock()
        lease.state = vim.HttpNfcLease.State.ready
        lease.info.deviceUrl = [SimpleNamespace(importKey='disk0', url='https://*/upload/disk0')]
        response = Mock(status_code=200)
        def request(method, url, **kwargs):
            self.assertEqual(method, 'POST')
            self.assertEqual(url, 'https://10.0.0.1:443/upload/disk0')
            self.assertEqual(kwargs['headers']['Content-Length'], '6')
            reader = kwargs['data']
            while reader.read(3):
                pass
            lease.HttpNfcLeaseComplete.assert_not_called()
            return response
        session.return_value.__enter__.return_value.request.side_effect = request
        with tarfile.open(fileobj=data, mode='r') as archive:
            vm = d.upload(lease, archive, {'disk.vmdk': archive.getmember('disk.vmdk')}, '',
                [SimpleNamespace(path='disk.vmdk', deviceId='disk0', size=6, create=False)], '10.0.0.1', 443, True)
        lease.HttpNfcLeaseComplete.assert_called_once()
        self.assertEqual(vm, lease.info.entity)

    @patch('deploy_ova.requests.Session')
    def test_rejected_upload_never_completes_lease(self, session):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w') as archive:
            member = tarfile.TarInfo('disk.vmdk')
            member.size = 1
            archive.addfile(member, io.BytesIO(b'x'))
        data.seek(0)
        lease = Mock()
        lease.state = vim.HttpNfcLease.State.ready
        lease.info.deviceUrl = [SimpleNamespace(importKey='disk0', url='https://*/upload/id')]
        session.return_value.__enter__.return_value.request.return_value.status_code = 500
        with tarfile.open(fileobj=data, mode='r') as archive:
            with self.assertRaises(d.CloneError):
                d.upload(lease, archive, {'disk.vmdk': archive.getmember('disk.vmdk')}, '',
                    [SimpleNamespace(path='disk.vmdk', deviceId='disk0', size=1, create=False)], 'host', 443, True)
        lease.HttpNfcLeaseComplete.assert_not_called()


if __name__ == '__main__':
    unittest.main()
