import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import requests

from web.app import InputError, Job, Jobs, create_app, deployment_input, save_credentials


def options(**updates):
    return dict(url='https://example.com/appliance.ova', name='lab-01', filename='test.ova', **updates)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root)
        self.client = self.app.test_client()

    def credentials(self):
        return save_credentials(self.root, dict(host='esxi.test', user='test-user', password='test-password-value'))

    def test_credentials_persist_across_app_restarts_and_never_return_password(self):
        response = self.client.post('/api/connection', json=dict(host='esxi.test', user='test-user', password='test-password-value'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('test-password-value', response.get_data(as_text=True))
        restarted = create_app(self.root).test_client()
        self.assertEqual(restarted.get('/api/connection').json, dict(host='esxi.test', user='test-user', password_saved=True))
        self.assertEqual((self.root/'creds.json').stat().st_mode & 0o777, 0o600)

    def test_blank_password_keeps_saved_password_only_for_same_identity(self):
        self.credentials()
        save_credentials(self.root, dict(host='esxi.test', user='test-user', password=''))
        self.assertEqual(json.loads((self.root/'creds.json').read_text())['password'], 'test-password-value')
        with self.assertRaises(InputError):
            save_credentials(self.root, dict(host='another-host', user='test-user', password=''))

    def test_cross_origin_and_non_json_writes_rejected(self):
        self.assertEqual(self.client.post('/api/connection', json={}, headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/connection', data='host=test').status_code, 415)

    def test_remote_hostnames_and_ips_allow_same_origin_requests(self):
        for host in ('jumpbox:8080', '192.0.2.10:8080', '[2001:db8::10]:8080'):
            self.assertEqual(self.client.get('/', headers={'Host': host}).status_code, 200)
            response = self.client.post('/api/connection', headers={'Host':host, 'Origin':'http://'+host},
                                        json=dict(host='esxi.test', user='test', password='fixture-password'))
            self.assertEqual(response.status_code, 200)
            response = self.client.post('/api/connection', headers={'Host':host, 'Origin':'http://other.example'}, json={})
            self.assertEqual(response.status_code, 403)

    def test_optional_host_allowlist_rejects_unconfigured_hosts(self):
        with patch.dict('os.environ', {'GUI_TRUSTED_HOSTS':'jumpbox,192.0.2.10,localhost,127.0.0.1'}):
            client = create_app(self.root).test_client()
        self.assertEqual(client.get('/', headers={'Host':'jumpbox:8080'}).status_code, 200)
        self.assertEqual(client.get('/', headers={'Host':'untrusted.example:8080'}).status_code, 400)

    def test_only_http_downloads_and_plain_filenames_allowed(self):
        for updates in (dict(url='file:///etc/passwd'), dict(url='https://user:secret@example.com/file.ova'),
                        dict(filename='../escape.ova'), dict(filename='/root/file.ova'), dict(filename='back\\file.ova'),
                        dict(name='bad/name'), dict(reuse_existing='false')):
            data = options()
            data.update(updates)
            with self.assertRaises(InputError):
                deployment_input(data)

    def test_filename_optional_and_extension_added(self):
        data = options()
        data.pop('filename')
        self.assertEqual(deployment_input(data)['filename'], 'appliance.ova')
        data['filename'] = 'new-file'
        self.assertEqual(deployment_input(data)['filename'], 'new-file.ova')

    def test_gui_accepts_esxi_certificates_without_a_checkbox(self):
        self.assertTrue(deployment_input(options())['insecure'])
        self.assertNotIn('id="insecure"', self.client.get('/').get_data(as_text=True))

    def test_jobs_require_credentials_and_prevent_concurrent_deployments(self):
        self.assertEqual(self.client.post('/api/jobs', json=options()).status_code, 400)
        self.credentials()
        with patch('web.app.threading.Thread'):
            first = self.client.post('/api/jobs', json=options())
            self.assertEqual(first.status_code, 202)
            self.assertEqual(self.client.post('/api/jobs', json=options()).status_code, 400)
        self.assertNotIn('password', first.get_data(as_text=True))

    @patch('web.app.threading.Thread')
    def test_misc_actions_start_without_download_inputs(self, thread):
        self.credentials()
        for data in ({'kind': 'clone', 'source_vm': 'source vm', 'name': 'clone-01'},
                     {'kind': 'network', 'name': 'internal-01'}):
            response = self.client.post('/api/jobs', json=data)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json['job']['kind'], data['kind'])
            self.assertIsNone(response.json['job']['filename'])
            self.app.extensions['jobs'].current.finish(True)

    def test_misc_actions_validate_names_and_source(self):
        self.credentials()
        invalid = [dict(kind='clone', source_vm='', name='clone-01'),
                   dict(kind='clone', source_vm='same', name='same'),
                   dict(kind='clone', source_vm='source', name='../bad'),
                   dict(kind='network', name='x'*32), dict(kind='network', name='--bad'),
                   dict(kind='shell', name='anything')]
        for data in invalid:
            self.assertEqual(self.client.post('/api/jobs', json=data).status_code, 400)

    @patch('web.app.threading.Thread')
    def test_actions_share_the_concurrency_lock(self, thread):
        self.credentials()
        self.assertEqual(self.client.post('/api/jobs', json={'kind':'network', 'name':'lab'}).status_code, 202)
        self.assertEqual(self.client.post('/api/jobs', json={'kind':'clone','source_vm':'a','name':'b'}).status_code, 400)
        self.assertEqual(self.client.post('/api/jobs', json=options()).status_code, 400)

    @patch('web.app.subprocess.Popen')
    @patch('web.app.requests.get')
    def test_misc_workers_use_existing_scripts_private_creds_and_live_progress(self, get, popen):
        from web.app import action_input
        for kind in ('clone', 'network'):
            process = popen.return_value.__enter__.return_value
            process.stdout = io.StringIO('Clone: 42% done.\nFinished\n')
            process.wait.return_value = 0
            job = Job(action_input(dict(kind=kind, source_vm='source VM', name='new-01', datastore='target ds')),
                      dict(host='esxi.test',user='test-user',password='private-value'))
            def inspect(command, **kwargs):
                snapshot = Path(kwargs['env']['ESXI_CREDS_FILE'])
                self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(snapshot.read_text())['password'], 'private-value')
                self.assertNotIn('private-value', ' '.join(command))
                if kind == 'clone':
                    self.assertTrue(command[2].endswith('clone_vm.py'))
                    self.assertIn('--vm=source VM', command)
                    self.assertEqual(command[command.index('--datastore')+1], 'target ds')
                else:
                    self.assertTrue(command[2].endswith('create_test_network.py'))
                    self.assertEqual(command[3:], ['new-01'])
                return popen.return_value
            popen.side_effect = inspect
            Jobs(self.root).run(job)
            self.assertEqual(job.status, 'success')
            self.assertTrue(any(line['percent']==42 for line in job.lines))
        get.assert_not_called()

    @patch('web.app.inventory')
    @patch('web.app.load_ssh_host_keys')
    @patch('web.app.paramiko.SSHClient')
    def test_vm_list_sorts_duplicate_names_and_hides_credentials(self, ssh, keys, inventory):
        self.credentials()
        inventory.return_value = [('12', 'Ubuntu', '/private/path'), ('3', 'Ubuntu', '/other'), ('1', 'alpha', '/path')]
        response = self.client.get('/api/vms')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['vms'], [dict(id='1', name='alpha'), dict(id='3', name='Ubuntu'), dict(id='12', name='Ubuntu')])
        self.assertEqual(response.json['host'], 'esxi.test')
        self.assertNotIn('test-password-value', response.get_data(as_text=True))
        self.assertNotIn('/private/path', response.get_data(as_text=True))
        keys.assert_called_once_with(ssh.return_value)
        self.assertEqual(ssh.return_value.connect.call_args.kwargs['password'], 'test-password-value')
        ssh.return_value.close.assert_called_once()

    @patch('web.app.load_ssh_host_keys')
    @patch('web.app.paramiko.SSHClient')
    def test_vm_list_failure_is_sanitized_and_connection_closed(self, ssh, keys):
        self.credentials()
        ssh.return_value.connect.side_effect = OSError('test-password-value')
        response = self.client.get('/api/vms')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('test-password-value', response.get_data(as_text=True))
        ssh.return_value.close.assert_called_once()

    @patch('web.app.threading.Thread')
    def test_clone_vm_ids_are_validated_and_bound_to_saved_host(self, thread):
        self.credentials()
        data = dict(kind='clone', source_vm_id='34', source_host='other-host', source_user='test-user', name='clone-01')
        self.assertEqual(self.client.post('/api/jobs', json=data).status_code, 400)
        thread.assert_not_called()
        data['source_host'] = 'esxi.test'
        for invalid in ('0', '-1', '1;ls', 'abc'):
            self.assertEqual(self.client.post('/api/jobs', json=dict(data, source_vm_id=invalid)).status_code, 400)
        self.assertEqual(self.client.post('/api/jobs', json=data).status_code, 202)

    @patch('web.app.subprocess.Popen')
    def test_clone_selected_id_is_passed_to_script(self, popen):
        from web.app import action_input
        process = popen.return_value.__enter__.return_value
        process.stdout = io.StringIO('Finished\n')
        process.wait.return_value = 0
        job = Job(action_input(dict(kind='clone', source_vm_id='34', name='clone-01')),
                  dict(host='esxi.test', user='test-user', password='private'))
        Jobs(self.root).run(job)
        command = popen.call_args.args[0]
        self.assertIn('--vm-id=34', command)
        self.assertFalse(any(arg.startswith('--vm=') for arg in command))
        self.assertEqual(job.status, 'success')

    @patch('web.app.fetch_networks', return_value=['VM Network', 'lab'])
    def test_network_inventory_uses_saved_connection_without_exposing_password(self, fetch):
        self.credentials()
        response = self.client.get('/api/networks')
        self.assertEqual(response.json['networks'], ['VM Network', 'lab'])
        self.assertEqual(fetch.call_args.args[0]['host'], 'esxi.test')
        self.assertNotIn('test-password-value', response.get_data(as_text=True))

    def test_interface_inventory_reads_actual_ova_and_rejects_paths(self):
        import tarfile
        from test_deploy_ova import descriptor
        path = self.saved_file()
        content = descriptor(['GE1', 'GE6', 'GE2']).encode()
        with tarfile.open(path, 'w') as archive:
            member = tarfile.TarInfo('vm.ovf'); member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        response = self.client.get('/api/ova/interfaces?filename=saved.ova')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['interfaces'], [dict(index=1,label='GE1',ge6=False), dict(index=2,label='GE6',ge6=True), dict(index=3,label='GE2',ge6=False)])
        self.assertEqual(self.client.get('/api/ova/interfaces?filename=../creds.json').status_code, 400)
        path.unlink(); path.symlink_to(self.root/'creds.json')
        self.assertEqual(self.client.get('/api/ova/interfaces?filename=saved.ova').status_code, 400)

    def test_interface_mapping_validation_and_host_binding(self):
        self.credentials()
        mapping = dict(index=1, label='GE1', network='lab', connected=True, create=True)
        for change in (dict(index=True), dict(index=0), dict(connected='yes'), dict(network='../bad'), dict(create='yes')):
            with self.assertRaises(InputError):
                deployment_input(dict(options(), nic_mappings=[dict(mapping, **change)]))
        with patch('web.app.threading.Thread') as thread:
            data = dict(options(), nic_mappings=[mapping], network_host='old-host', network_user='test-user')
            self.assertEqual(self.client.post('/api/jobs', json=data).status_code, 400)
            thread.assert_not_called()

    @patch('web.app.subprocess.Popen')
    def test_download_for_setup_does_not_deploy(self, popen):
        job = Job(dict(deployment_input(dict(options(), download_only=True)), kind='deployment'), {})
        jobs = Jobs(self.root)
        with patch.object(jobs, 'download', return_value=self.root/'test.ova'):
            jobs.run(job)
        self.assertEqual(job.status, 'success')
        popen.assert_not_called()

    @patch('web.app.threading.Thread')
    def test_library_download_needs_no_esxi_connection_or_vm_name(self, thread):
        response = self.client.post('/api/jobs', json=dict(options(), name='OVA download', download_only=True))
        self.assertEqual(response.status_code, 202)
        self.assertFalse((self.root/'creds.json').exists())
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('id="download-form"', html)
        self.assertNotIn('id="source-url"', html)
        self.assertNotIn('Download for interface setup', html)

    @patch('web.app.requests.get')
    def test_download_limits_enforce_declared_streamed_size_and_free_space(self, get):
        from types import SimpleNamespace
        response = get.return_value.__enter__.return_value
        response.status_code = 200
        response.headers = {'Content-Length': '12'}
        response.iter_content.return_value = [b'abcdef', b'ghijkl']
        jobs = Jobs(self.root); jobs.max_download_bytes=10
        job = Job(deployment_input(options()), {})
        with self.assertRaisesRegex(InputError, 'size limit'):
            jobs.download(job)
        response.headers = {}
        with self.assertRaisesRegex(InputError, 'size limit'):
            jobs.download(job)
        jobs.max_download_bytes=20
        with patch('web.app.shutil.disk_usage', return_value=SimpleNamespace(free=1024)):
            with self.assertRaisesRegex(InputError, 'free disk space'):
                jobs.download(job)
        self.assertEqual(list((self.root/'OVA-download').iterdir()), [])

    def test_symlinked_download_directory_is_rejected(self):
        outside = self.root/'outside'; outside.mkdir()
        (outside/'sensitive.ova').write_bytes(b'sensitive')
        (self.root/'OVA-download').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.client.get('/api/downloads').status_code, 400)
        self.assertEqual(self.client.get('/api/ova/interfaces?filename=sensitive.ova').status_code, 400)
        response = self.client.post('/api/downloads/delete', json={'filename':'sensitive.ova'})
        self.assertEqual(response.status_code, 400)
        self.assertTrue((outside/'sensitive.ova').exists())

    def test_download_only_flag_requires_boolean_and_security_headers_present(self):
        with self.assertRaises(InputError):
            deployment_input(dict(options(), download_only='true'))
        response = self.client.get('/')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(response.headers['Cross-Origin-Resource-Policy'], 'same-origin')

    def test_download_conflicts_and_symlinks_are_refused(self):
        self.credentials()
        directory = self.root/'OVA-download'
        directory.mkdir()
        (directory/'test.ova').write_bytes(b'existing')
        self.assertEqual(self.client.post('/api/jobs', json=options()).status_code, 400)
        (directory/'test.ova').unlink()
        (directory/'test.ova').symlink_to(self.root/'creds.json')
        data = options(reuse_existing=True)
        self.assertEqual(self.client.post('/api/jobs', json=data).status_code, 400)

    def saved_file(self, name='saved.ova', content=b'ova-data'):
        directory = self.root/'OVA-download'
        directory.mkdir(exist_ok=True)
        path = directory/name
        path.write_bytes(content)
        return path

    def test_library_lists_only_completed_regular_ova_files(self):
        self.saved_file()
        self.saved_file('download.part')
        self.saved_file('.gitkeep')
        self.saved_file('upper.OVA')
        self.saved_file('folder.ova').unlink()
        (self.root/'OVA-download'/'folder.ova').mkdir()
        (self.root/'OVA-download'/'link.ova').symlink_to(self.root/'OVA-download'/'saved.ova')
        data = self.client.get('/api/downloads').json['files']
        self.assertEqual({item['name'] for item in data}, {'saved.ova', 'upper.OVA'})
        self.assertEqual(data[0]['size'], 8)
        self.assertIn('modified', data[0])
        self.assertFalse(data[0]['in_use'])

    @patch('web.app.requests.get')
    @patch('web.app.threading.Thread')
    def test_saved_ova_deployment_needs_no_url_and_does_not_download(self, thread, get):
        self.credentials()
        path = self.saved_file()
        response = self.client.post('/api/jobs', json={'source': 'downloaded', 'filename': 'saved.ova', 'name': 'vm-02'})
        self.assertEqual(response.status_code, 202)
        job = self.app.extensions['jobs'].current
        self.assertEqual(job.options['url'], '')
        self.assertEqual(self.app.extensions['jobs'].download(job), path)
        get.assert_not_called()
        self.assertTrue(self.client.get('/api/downloads').json['files'][0]['in_use'])

    def test_missing_saved_file_cannot_be_deployed(self):
        self.credentials()
        response = self.client.post('/api/jobs', json={'source': 'downloaded', 'filename': 'missing.ova', 'name': 'vm-02'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('no longer exists', response.json['error'])

    def test_delete_removes_only_selected_file(self):
        path = self.saved_file()
        keep = self.saved_file('keep.ova')
        self.credentials()
        response = self.client.post('/api/downloads/delete', json={'filename': path.name})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(path.exists())
        self.assertTrue(keep.exists())
        self.assertTrue((self.root/'creds.json').exists())

    def test_delete_rejects_paths_symlinks_and_non_ova_files(self):
        self.credentials()
        directory = self.root/'OVA-download'
        directory.mkdir()
        (directory/'link.ova').symlink_to(self.root/'creds.json')
        for name in ('../creds.json', '/tmp/file.ova', 'creds.json', 'link.ova', 'missing.ova', None):
            response = self.client.post('/api/downloads/delete', json={'filename': name})
            self.assertEqual(response.status_code, 400)
        self.assertTrue((self.root/'creds.json').exists())

    @patch('web.app.threading.Thread')
    def test_active_file_cannot_be_deleted_but_other_file_can(self, thread):
        self.credentials()
        active = self.saved_file()
        other = self.saved_file('other.ova')
        self.client.post('/api/jobs', json={'source': 'downloaded', 'filename': active.name, 'name': 'vm-02'})
        self.assertEqual(self.client.post('/api/downloads/delete', json={'filename': active.name}).status_code, 400)
        self.assertTrue(active.exists())
        self.assertEqual(self.client.post('/api/downloads/delete', json={'filename': other.name}).status_code, 200)
        self.assertFalse(other.exists())
        self.app.extensions['jobs'].current.finish(False)
        self.assertEqual(self.client.post('/api/downloads/delete', json={'filename': active.name}).status_code, 200)

    @patch('web.app.requests.get')
    def test_download_progress_renames_file_and_cleans_partial(self, get):
        response = get.return_value.__enter__.return_value
        response.status_code = 200
        response.headers = {'Content-Length': '6'}
        response.iter_content.return_value = [b'abc', b'def']
        job = Job(deployment_input(options()), {'password': 'do-not-log'})
        result = Jobs(self.root).download(job)
        self.assertEqual(result.read_bytes(), b'abcdef')
        self.assertEqual(job.percent, 100)
        self.assertFalse(list(result.parent.glob('*.part')))

    @patch('web.app.requests.get')
    def test_incomplete_download_cannot_be_used(self, get):
        response = get.return_value.__enter__.return_value
        response.status_code = 200
        response.headers = {'Content-Length': '10'}
        response.iter_content.return_value = [b'abc']
        job = Job(deployment_input(options()), {})
        with self.assertRaises(InputError):
            Jobs(self.root).download(job)
        self.assertFalse(list((self.root/'OVA-download').iterdir()))

    @patch('web.app.requests.get')
    def test_download_errors_explain_cause_without_exposing_signed_link(self, get):
        errors = [
            (requests.exceptions.SSLError('https://example.com/file?token=secret'), 'certificate verification'),
            (requests.exceptions.ConnectTimeout('token=secret'), 'timed out'),
            (requests.exceptions.ReadTimeout('token=secret'), 'stopped responding'),
            (requests.exceptions.ProxyError('token=secret'), 'proxy'),
            (requests.exceptions.ConnectionError('NameResolutionError token=secret'), 'resolve'),
            (requests.exceptions.ConnectionError('token=secret'), 'Cannot connect'),
        ]
        for error, expected in errors:
            get.side_effect = error
            job = Job(deployment_input(options()), {})
            with self.assertRaises(InputError) as caught:
                Jobs(self.root).download(job)
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn('secret', str(caught.exception))

    def test_sse_stream_replays_output_redacts_password_and_reports_completion(self):
        job = Job(deployment_input(options()), {'password': 'secret-password'})
        job.emit('Testing secret-password', percent=30)
        job.emit('Finished', phase='done', percent=100)
        job.finish(True)
        self.app.extensions['jobs'].current = job
        response = self.client.get(f'/api/jobs/{job.id}/events')
        output = response.get_data(as_text=True)
        self.assertIn('event: output', output)
        self.assertIn('[redacted]', output)
        self.assertNotIn('secret-password', output)
        self.assertIn('event: complete', output)
        resumed = self.client.get(f'/api/jobs/{job.id}/events', headers={'Last-Event-ID': '1'}).get_data(as_text=True)
        self.assertNotIn('Testing', resumed)
        self.assertIn('Finished', resumed)

    @patch('web.app.subprocess.Popen')
    def test_worker_uses_script_arguments_and_private_credential_snapshot(self, popen):
        directory = self.root/'OVA-download'
        directory.mkdir()
        (directory/'test.ova').write_bytes(b'placeholder')
        data = deployment_input(options(reuse_existing=True, ge6_nic=6))
        job = Job(data, dict(host='esxi.test', user='test-user', password='private-value'))
        process = popen.return_value.__enter__.return_value
        process.stdout = io.StringIO('Uploading disk.vmdk: 50%\nDone\n')
        process.wait.return_value = 0
        def inspect(command, **kwargs):
            snapshot = Path(command[command.index('--creds-file')+1])
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(snapshot.read_text())['password'], 'private-value')
            self.assertNotIn('private-value', ' '.join(command))
            self.assertIn('--insecure', command)
            self.assertEqual(command[command.index('--ge6-nic')+1], '6')
            return popen.return_value
        popen.side_effect = inspect
        Jobs(self.root).run(job)
        self.assertEqual(job.status, 'success')
        self.assertTrue(any(line['percent'] == 50 for line in job.lines))
        command = popen.call_args.args[0]
        self.assertFalse(Path(command[command.index('--creds-file')+1]).exists())

    @patch('web.app.subprocess.Popen')
    def test_failed_script_marks_job_failed(self, popen):
        (self.root/'OVA-download').mkdir()
        (self.root/'OVA-download'/'test.ova').write_bytes(b'placeholder')
        job = Job(deployment_input(options(reuse_existing=True)), {})
        process = popen.return_value.__enter__.return_value
        process.stdout = io.StringIO('ESXi error\n')
        process.wait.return_value = 1
        Jobs(self.root).run(job)
        self.assertEqual(job.status, 'failed')
        self.assertIn('code 1', job.lines[-1]['text'])


if __name__ == '__main__':
    unittest.main()
