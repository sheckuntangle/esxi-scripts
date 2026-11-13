import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

import paramiko
from web.app import Job, Jobs, create_app, action_input
from web import velo


class VeloTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'update image.tar'
        self.path.write_bytes(b'update')
        self.client = create_app(self.root).test_client()

    def job(self, **updates):
        data = dict(kind='velo', filename=self.path.name, host='edge.test', user='root', password='device-secret')
        data.update(updates)
        options = action_input(data)
        return Job(options, options.pop('device_credentials'))

    def response(self, client, args, **kwargs):
        if args == ['id', '-u']:
            return '0\n'
        if args[0] == 'df':
            return 'Filesystem 1024-blocks Used Available Capacity Mounted on\ntmpfs 900000 1000 899000 1% /tmp\n'
        if args[0] == 'wc':
            return '6 ' + args[-1] + '\n'
        if kwargs.get('emit'):
            kwargs['emit']('Updater: processing image')
        return ''

    def test_validation_rejects_paths_bad_urls_and_nonboolean_flags(self):
        for data in (dict(download_only=True,url='file:///etc/passwd',filename='test'),
                     dict(download_only=True,url='https://user:secret@example.com/a',filename='a'),
                     dict(filename='../escape',host='edge',user='root',password='s'),
                     dict(filename='image;reboot',host='edge',user='root',password='s'),
                     dict(filename='image',host='ssh://edge',user='root',password='s'),
                     dict(filename='image',host='edge',user='root',password='s',run_update='true')):
            with self.assertRaises(ValueError):
                velo.velo_input(data)
        options=velo.velo_input(dict(download_only=True,url='https://example.com/update.tar.gz'))
        self.assertEqual(options['filename'],'update.tar.gz')
        self.assertTrue(options['reuse_existing'])
        self.assertTrue(options['skip_download_tls_verification'])

    @patch('web.velo.SCPClient')
    @patch('web.velo.command')
    @patch('web.velo.paramiko.SSHClient')
    def test_openwrt_scp_and_root_update_order_progress_and_host_key_policy(self, ssh, command, scp):
        command.side_effect=self.response
        job=self.job()
        def upload(*args, **kwargs):
            callback=scp.call_args.kwargs['progress']; callback(b'image',6,3);callback(b'image',6,6)
        scp.return_value.__enter__.return_value.put.side_effect=upload
        velo.transfer(self.root,job,self.path)
        commands=[call.args[1] for call in command.call_args_list]
        self.assertEqual(commands[0],['id','-u'])
        self.assertIn(['command','-v','scp'],commands)
        self.assertIn(['df','-Pk','/tmp'],commands)
        self.assertEqual(commands[-1],['/root/swupdater',self.path.name])
        self.assertEqual(command.call_args.kwargs['workdir'],'/tmp')
        self.assertTrue(any(c[0]=='wc' for c in commands))
        self.assertTrue(any(c[0]=='mv' and c[-1]=='/tmp/'+self.path.name for c in commands))
        self.assertFalse(any(c[0]=='stat' for c in commands))
        self.assertIsInstance(ssh.return_value.set_missing_host_key_policy.call_args.args[0],paramiko.AutoAddPolicy)
        self.assertEqual((self.root/'velo_known_hosts').stat().st_mode & 0o777,0o600)
        self.assertTrue(any(line['percent']==50 for line in job.lines))
        self.assertTrue(any('processing image' in line['text'] for line in job.lines))
        self.assertFalse(any(c[0]=='rm' for c in commands))
        ssh.return_value.close.assert_called_once()

    @patch('web.velo.SCPClient')
    @patch('web.velo.command')
    @patch('web.velo.paramiko.SSHClient')
    def test_transfer_only_skips_updater_and_root_requirement(self, ssh, command, scp):
        command.side_effect=self.response
        velo.transfer(self.root,self.job(user='vcadmin',run_update=False),self.path)
        commands=[call.args[1] for call in command.call_args_list]
        self.assertFalse(any(c[0] in ('id','/root/swupdater') for c in commands))
        scp.return_value.__enter__.return_value.put.assert_called_once()

    @patch('web.velo.SCPClient')
    @patch('web.velo.command')
    @patch('web.velo.paramiko.SSHClient')
    def test_nonroot_or_insufficient_tmp_space_stops_before_upload(self, ssh, command, scp):
        for failure in ('root','space','scp'):
            def respond(client,args,**kwargs):
                if failure=='root' and args==['id','-u']:return '1000\n'
                if failure=='space' and args[0]=='df':return 'Filesystem Blocks Used Available Use Mounted\ntmpfs 100 99 1 99% /tmp\n'
                if failure=='scp' and args==['command','-v','scp']:raise velo.VeloError('missing')
                return self.response(client,args,**kwargs)
            command.side_effect=respond
            with self.assertRaises(velo.VeloError):velo.transfer(self.root,self.job(),self.path)
        scp.assert_not_called()

    @patch('web.velo.SCPClient')
    @patch('web.velo.command')
    @patch('web.velo.paramiko.SSHClient')
    def test_failed_scp_cleans_partial_file_and_never_runs_updater(self, ssh, command, scp):
        command.side_effect=self.response
        scp.return_value.__enter__.return_value.put.side_effect=Exception('device-secret')
        with self.assertRaises(velo.VeloError) as caught:velo.transfer(self.root,self.job(),self.path)
        self.assertNotIn('device-secret',str(caught.exception))
        commands=[call.args[1] for call in command.call_args_list]
        self.assertTrue(any(c[0]=='rm' and c[-1].startswith('/tmp/.velo-') for c in commands))
        self.assertFalse(any(c[0]=='/root/swupdater' for c in commands))

    @patch('web.velo.SCPClient')
    @patch('web.velo.command')
    @patch('web.velo.paramiko.SSHClient')
    def test_size_mismatch_does_not_finalize_or_update(self, ssh, command, scp):
        command.side_effect=lambda client,args,**kw: '5 image\n' if args[0]=='wc' else self.response(client,args,**kw)
        with self.assertRaisesRegex(velo.VeloError,'size'):velo.transfer(self.root,self.job(),self.path)
        self.assertFalse(any(call.args[1][0] in ('mv','/root/swupdater') for call in command.call_args_list))

    @patch('web.velo.paramiko.SSHClient')
    def test_changed_host_key_is_rejected_and_authentication_failure_sanitized(self, ssh):
        for error in (paramiko.AuthenticationException('device-secret'),paramiko.BadHostKeyException('edge',Mock(),Mock())):
            ssh.return_value.connect.side_effect=error
            ssh.return_value.get_transport.return_value=None
            with self.assertRaises(velo.VeloError) as caught:velo.transfer(self.root,self.job(),self.path)
            self.assertNotIn('device-secret',str(caught.exception))

    def test_command_quotes_busybox_arguments_streams_output_and_rejects_missing_exit_status(self):
        client=Mock(); channel=client.get_transport.return_value.open_session.return_value
        channel.recv.side_effect=[b'work\r\ncomplete\n',b''];channel.recv_exit_status.return_value=0
        emit=Mock()
        velo.command(client,['/root/swupdater','update image.tar'],emit=emit,workdir='/tmp')
        channel.exec_command.assert_called_once_with("cd /tmp && /root/swupdater 'update image.tar'")
        self.assertEqual([c.args[0] for c in emit.call_args_list],['work','complete'])
        channel.recv.side_effect=[b''];channel.recv_exit_status.return_value=-1
        with self.assertRaisesRegex(velo.VeloError,'without an exit status'):
            velo.command(client,['/root/swupdater','update.tar'],emit=Mock())
        channel.recv.side_effect=socket.timeout();channel.closed=True
        with self.assertRaisesRegex(velo.VeloError,'unconfirmed'):
            velo.command(client,['/root/swupdater','update.tar'],emit=Mock())

    @patch('web.app.threading.Thread')
    def test_velo_jobs_do_not_need_or_store_esxi_credentials_and_share_lock(self, thread):
        folder=self.root/'Velo-download';folder.mkdir();(folder/self.path.name).write_bytes(b'update')
        data=dict(kind='velo',filename=self.path.name,host='edge.test',user='root',password='device-secret')
        response=self.client.post('/api/jobs',json=data)
        self.assertEqual(response.status_code,202)
        self.assertNotIn('device-secret',response.get_data(as_text=True))
        job=self.client.application.extensions['jobs'].current
        self.assertNotIn('device_credentials',job.options)
        self.assertEqual(job.credentials['host'],'edge.test')
        self.assertFalse((self.root/'creds.json').exists())
        self.assertEqual(self.client.post('/api/jobs',json=dict(kind='network',name='test')).status_code,400)
        self.assertEqual(self.client.post('/api/velo/downloads/delete',json=dict(filename=self.path.name)).status_code,400)

    def test_library_is_separate_and_rejects_symlinks_paths_and_hidden_partials(self):
        folder=self.root/'Velo-download';folder.mkdir()
        (folder/'image.tar.gz').write_bytes(b'update')
        (folder/'.image.partial.part').write_bytes(b'partial')
        (folder/'linked.tar').symlink_to(self.path)
        listing=self.client.get('/api/velo/downloads').json['files']
        self.assertEqual([f['name'] for f in listing],['image.tar.gz'])
        self.assertEqual(self.client.get('/api/downloads').json['files'],[])
        self.assertEqual(self.client.post('/api/velo/downloads/delete',json={'filename':'../outside'}).status_code,400)
        self.assertEqual(self.client.post('/api/velo/downloads/delete',json={'filename':'linked.tar'}).status_code,400)
        self.assertEqual(self.client.post('/api/velo/downloads/delete',json={'filename':'image.tar.gz'}).status_code,200)

    @patch('web.app.transfer_velo', side_effect=velo.VeloError('SSH closed; update completion is unconfirmed. Inspect before retrying.'))
    def test_update_disconnect_is_not_reported_as_success(self, transfer):
        folder=self.root/'Velo-download';folder.mkdir();(folder/self.path.name).write_bytes(b'update')
        job=self.job();Jobs(self.root).run(job)
        self.assertEqual(job.status,'failed')
        self.assertIn('unconfirmed',job.lines[-1]['text'])
        self.assertFalse(any('action completed' in line['text'] for line in job.lines))

    @patch('web.app.requests.get')
    @patch('web.app.transfer_velo')
    def test_velo_download_progress_and_transfer_job_route(self, transfer, get):
        response=get.return_value.__enter__.return_value
        response.status_code=200;response.headers={'Content-Length':'6'};response.iter_content.return_value=[b'update']
        options=action_input(dict(kind='velo',download_only=True,url='https://example.com/update.tar'))
        job=Job(options,options.pop('device_credentials'));jobs=Jobs(self.root);jobs.run(job)
        self.assertEqual(job.status,'success');transfer.assert_not_called()
        self.assertEqual((self.root/'Velo-download'/'update.tar').read_bytes(),b'update')
        job=self.job(filename='update.tar');jobs.run(job)
        self.assertEqual(job.status,'success')
        transfer.assert_called_once_with(self.root,job,self.root/'Velo-download'/'update.tar')


if __name__=='__main__':unittest.main()
