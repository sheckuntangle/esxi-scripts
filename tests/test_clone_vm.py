import unittest
import json
import io
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import clone_vm


class CloneTests(unittest.TestCase):
    @patch.dict('os.environ', {'ESXI_KNOWN_HOSTS': '/mounted/known_hosts'})
    def test_host_keys_loaded_from_mounted_file(self):
        client = Mock()
        clone_vm.load_ssh_host_keys(client)
        client.load_system_host_keys.assert_called_once()
        client.load_host_keys.assert_called_once_with('/mounted/known_hosts')

    def test_credentials_file_and_identity_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps({"host": "test-host", "user": "test-user",
                                        "password": "test-password", "port": 2222}))
            base = ["--creds-file", str(path), "--vm", "Source", "--clone-name", "Clone"]
            args = clone_vm.parser().parse_args(base)
            self.assertEqual(clone_vm.load_credentials(args), "test-password")
            self.assertEqual((args.host, args.user, args.port), ("test-host", "test-user", 2222))
            args = clone_vm.parser().parse_args(base + ["--host", "other-host", "--port", "22"])
            self.assertIsNone(clone_vm.load_credentials(args))
            self.assertEqual((args.host, args.port), ("other-host", 22))

    def test_invalid_credentials_do_not_disclose_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text('{"password": "secret-value", invalid}')
            args = clone_vm.parser().parse_args(["--creds-file", str(path), "--vm", "Source", "--clone-name", "Clone"])
            with self.assertRaises(clone_vm.CloneError) as caught:
                clone_vm.load_credentials(args)
            self.assertNotIn("secret-value", str(caught.exception))

    def test_snapshot_disk_and_identity(self):
        settings = clone_vm.read_vmx('''
displayName = "Source"
scsi0:0.fileName = "Source-000003.vmdk"
scsi0:0.present = "TRUE"
ethernet0.present = "TRUE"
ethernet0.addressType = "static"
ethernet0.address = "00:50:56:00:00:01"
uuid.bios = "old"
sched.swap.derivedName = "/old/file"
nvram = "Source.nvram"
''')
        result, disks = clone_vm.prepare_vmx(settings, "Clone")
        self.assertEqual(disks, [("Source-000003.vmdk", "disk-1.vmdk")])
        self.assertEqual(result["scsi0:0.filename"], "disk-1.vmdk")
        self.assertEqual(result["ethernet0.startconnected"], "TRUE")
        self.assertEqual(result["ethernet0.connected"], "TRUE")
        self.assertEqual(result["ethernet0.addresstype"], "generated")
        for key in ("uuid.bios", "ethernet0.address", "sched.swap.derivedname", "nvram"):
            self.assertNotIn(key, result)
        self.assertEqual(settings["displayname"], "Source")

    def test_interfaces_follow_actual_vmx_slots_and_keep_clone_connected_default(self):
        settings = clone_vm.read_vmx('ethernet2.present = "TRUE"\nethernet0.present = "TRUE"\nethernet0.networkName = "lab|22one|22"\nethernet0.startConnected = "FALSE"\nethernet1.present = "FALSE"\n')
        self.assertEqual(clone_vm.vmx_interfaces(settings), [dict(index=1,label='GE1',network='lab"one"',connected=True), dict(index=3,label='GE3',network='VM Network',connected=True)])

    def test_clone_network_overrides_escape_vmx_and_preserve_source_and_new_mac(self):
        source = dict({'scsi0:0.filename':'source.vmdk', 'ethernet0.present':'TRUE', 'ethernet0.address':'00:50:56:00:00:01', 'ethernet0.dvs.switchid':'old-switch', 'ethernet0.networkname':'old'})
        prepared, _ = clone_vm.prepare_vmx(source, 'clone')
        mappings = clone_vm.validate_nic_mappings([dict(index=1,label='GE1',network='lab "a" |22',connected=False)])
        actual = clone_vm.apply_nic_mappings(prepared,mappings)
        self.assertEqual(actual['ethernet0.networkname'], 'lab |22a|22 |7C22')
        self.assertEqual(actual['ethernet0.startconnected'], 'FALSE')
        self.assertEqual(actual['ethernet0.connected'], 'FALSE')
        self.assertEqual(actual['ethernet0.addresstype'], 'generated')
        self.assertNotIn('ethernet0.address', actual)
        self.assertNotIn('ethernet0.dvs.switchid', actual)
        self.assertEqual(source['ethernet0.networkname'], 'old')
        self.assertEqual(prepared['ethernet0.networkname'], 'old')
        with self.assertRaises(clone_vm.CloneError):
            clone_vm.apply_nic_mappings(prepared,[dict(index=2,label='GE2',network='lab',connected=True)])

    @patch('create_test_network.create_network')
    @patch('create_test_network.query')
    def test_clone_creates_shared_network_once_and_rejects_missing_or_conflicting_targets(self, query, create):
        client = Mock(); query.return_value = []
        mappings = clone_vm.validate_nic_mappings([dict(index=i,label=f'GE{i}',network='shared-lab',connected=True,create=True) for i in (1,2)])
        clone_vm.prepare_clone_networks(client,mappings)
        create.assert_called_once_with(client,'shared-lab')
        create.reset_mock(); query.return_value = [dict(Name='shared-lab')]
        with self.assertRaises(clone_vm.CloneError):
            clone_vm.prepare_clone_networks(client,mappings)
        create.assert_not_called()
        query.return_value = []
        with self.assertRaises(clone_vm.CloneError):
            clone_vm.prepare_clone_networks(client,[dict(index=1,label='GE1',network='missing',connected=True,create=False)])
        create.assert_not_called()

    @patch('clone_vm.ensure_off')
    @patch('clone_vm.prepare_clone_networks')
    def test_stale_clone_mapping_rejected_before_network_creation_or_source_shutdown(self, networks, shutdown):
        client = MagicMock(); sftp = client.open_sftp.return_value.__enter__.return_value
        sftp.stat.side_effect = FileNotFoundError(2,'missing')
        sftp.open.return_value.__enter__.return_value.read.return_value = b'scsi0:0.fileName = "source.vmdk"\nethernet0.present = "TRUE"\n'
        args = clone_vm.parser().parse_args(['--vm-id','1','--clone-name','Clone'])
        args.nic_mappings = [dict(index=2,label='GE2',network='lab',connected=True,create=False)]
        with patch('clone_vm.inventory',return_value=[('1','Source','[ds] Source/Source.vmx')]):
            with self.assertRaises(clone_vm.CloneError):
                clone_vm.clone(client,args)
        networks.assert_not_called(); shutdown.assert_not_called()

    def test_paths_and_quoting_data(self):
        self.assertEqual(clone_vm.datastore_path("[Data store] folder/VM.vmx"),
                         "/vmfs/volumes/Data store/folder/VM.vmx")
        self.assertEqual(clone_vm.resolve_disk("/vmfs/volumes/ds/vm", "../disk.vmdk"),
                         "/vmfs/volumes/ds/disk.vmdk")
        for value in ("[../ds] vm.vmx", "[ds] ../vm.vmx", "[ds] /vm.vmx"):
            with self.assertRaises(clone_vm.CloneError):
                clone_vm.datastore_path(value)

    def test_reject_tpm_and_missing_disks(self):
        for settings in ({"vtpm.present": "TRUE"}, {"encryption.keySafe": "secret"}, {}):
            with self.assertRaises(clone_vm.CloneError):
                clone_vm.prepare_vmx(settings, "Clone")

    @patch("clone_vm.remote", return_value="Retrieved runtime info\nPowered on\n")
    def test_refuse_powered_on(self, remote):
        with self.assertRaises(clone_vm.CloneError):
            clone_vm.assert_off(Mock(), "42")

    @patch("clone_vm.remote")
    def test_existing_destination_never_copied_into(self, remote):
        client = MagicMock()
        stream = client.open_sftp.return_value.__enter__.return_value.open.return_value.__enter__.return_value
        stream.read.return_value = b'scsi0:0.fileName = "source.vmdk"\n'
        args = clone_vm.parser().parse_args(["--host", "esxi", "--vm", "Source", "--clone-name", "Clone"])
        with patch("clone_vm.inventory", return_value=[("1", "Source", "[ds] nested/Source/Source.vmx")]):
            with self.assertRaises(clone_vm.CloneError):
                clone_vm.clone(client, args)
        remote.assert_not_called()

    @patch("clone_vm.remote")
    def test_already_off_has_no_shutdown(self, remote):
        remote.return_value = "Powered off\n"
        clone_vm.ensure_off(Mock(), "42", 120)
        self.assertEqual(remote.call_count, 1)

    @patch("clone_vm.wait_off", return_value=True)
    @patch("clone_vm.remote")
    def test_graceful_shutdown(self, remote, wait):
        remote.side_effect = ["Powered on\n", ""]
        clone_vm.ensure_off(Mock(), "42", 120)
        self.assertEqual(remote.call_args.args[1:], ("vim-cmd", "vmsvc/power.shutdown", "42"))
        wait.assert_called_once()

    @patch("clone_vm.wait_off", side_effect=[False, True])
    @patch("clone_vm.remote")
    def test_shutdown_timeout_forces_off(self, remote, wait):
        remote.side_effect = ["Powered on\n", "", ""]
        clone_vm.ensure_off(Mock(), "42", 120)
        self.assertEqual(remote.call_args.args[1:], ("vim-cmd", "vmsvc/power.off", "42"))

    @patch("clone_vm.wait_off", return_value=False)
    @patch("clone_vm.remote")
    def test_failed_poweroff_aborts(self, remote, wait):
        remote.side_effect = ["Powered on\n", clone_vm.CloneError("Tools unavailable"), ""]
        with self.assertRaises(clone_vm.CloneError):
            clone_vm.ensure_off(Mock(), "42", 120)

    @patch("clone_vm.progress")
    def test_disk_progress_streamed_on_carriage_returns(self, progress):
        output = b"Clone: 10%\rClone: 100%\n"
        self.assertEqual(clone_vm.read_output(io.BytesIO(output), True), output.decode())
        self.assertEqual(progress.call_count, 2)

    @patch('clone_vm.prepare_clone_networks')
    @patch('clone_vm.ensure_off')
    @patch('clone_vm.remote')
    def test_clone_writes_selected_network_and_disconnected_state_to_destination_only(self, remote, shutdown, networks):
        client=MagicMock(); sftp=client.open_sftp.return_value.__enter__.return_value
        sftp.stat.side_effect=FileNotFoundError(2,'missing')
        sftp.open.return_value.__enter__.return_value.read.return_value=b'scsi0:0.fileName = "source.vmdk"\nethernet0.present = "TRUE"\nethernet0.networkName = "source-lab"\nethernet0.address = "00:50:56:00:00:01"\n'
        remote.side_effect=lambda client,*args,**kw: 'Powered off\n' if args[1]=='vmsvc/power.getstate' else ('99\n' if args[1]=='solo/registervm' else '')
        args=clone_vm.parser().parse_args(['--vm-id','1','--clone-name','Clone'])
        args.nic_mappings=clone_vm.validate_nic_mappings([dict(index=1,label='GE1',network='clone-lab',connected=False,create=True)])
        with patch('clone_vm.inventory',return_value=[('1','Source','[ds] Source/Source.vmx')]):
            clone_vm.clone(client,args)
        text=sftp.open.return_value.__enter__.return_value.write.call_args.args[0].decode()
        self.assertIn('ethernet0.networkname = "clone-lab"',text)
        self.assertIn('ethernet0.startconnected = "FALSE"',text)
        self.assertIn('ethernet0.connected = "FALSE"',text)
        self.assertNotIn('00:50:56:00:00:01',text)
        writes=[call.args for call in sftp.open.call_args_list if call.args[1]=='wb']
        self.assertEqual(writes,[('/vmfs/volumes/ds/Clone/Clone.vmx','wb')])
        networks.assert_called_once_with(client,args.nic_mappings)

    @patch("clone_vm.ensure_off")
    @patch("clone_vm.remote")
    def test_clone_copies_then_registers_and_verifies(self, remote, shutdown):
        client = MagicMock()
        sftp = client.open_sftp.return_value.__enter__.return_value
        sftp.stat.side_effect = FileNotFoundError(2, "missing")
        sftp.open.return_value.__enter__.return_value.read.return_value = b'scsi0:0.fileName = "source.vmdk"\n'
        def command(client, *args, **kwargs):
            if args[1] == "vmsvc/power.getstate":
                return "Powered off\n"
            if args[1] == "solo/registervm":
                return "99\n"
            return ""
        remote.side_effect = command
        args = clone_vm.parser().parse_args(["--vm", "Source", "--clone-name", "Clone"])
        with patch("clone_vm.inventory", return_value=[("1", "Source", "[ds] Source/Source.vmx")]):
            clone_vm.clone(client, args)
        shutdown.assert_called_once_with(client, "1", 120)
        commands = [call.args[1:] for call in remote.call_args_list]
        self.assertLess(next(i for i,c in enumerate(commands) if c[0] == "vmkfstools"),
                        next(i for i,c in enumerate(commands) if c[1] == "solo/registervm"))
        self.assertEqual(commands[-1], ("vim-cmd", "vmsvc/power.getstate", "99"))


if __name__ == "__main__":
    unittest.main()
