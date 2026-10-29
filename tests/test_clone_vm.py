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
