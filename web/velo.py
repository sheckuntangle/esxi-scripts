"""Independent Velo device file transfer and software update over SSH/SCP."""

import codecs
from contextlib import contextmanager
import os
import ipaddress
from pathlib import Path
import re
import shlex
import socket
import time
from urllib.parse import unquote, urlsplit

import paramiko
from scp import SCPClient

FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,179}\Z")


class VeloError(Exception):
    pass


def velo_input(data):
    download_only = data.get('download_only', False)
    for key in ('download_only', 'run_update', 'reuse_existing', 'skip_download_tls_verification'):
        if key in data and type(data[key]) is not bool:
            raise ValueError(f'{key} must be true or false.')
    operation = data.get('operation', 'download' if download_only else 'transfer')
    if operation not in ('download', 'transfer', 'update', 'activate') or (download_only and operation != 'download'):
        raise ValueError('Select a download, SCP upload, updater, or activation action.')
    download_only = operation == 'download'
    if data.get('run_update') is True:
        raise ValueError('SCP and swupdater are separate actions. Use the Run swupdater section.')
    url = str(data.get('url', '')).strip()
    filename = str(data.get('filename', '')).strip()
    if download_only:
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not valid or len(url) > 8192:
            raise ValueError('Enter an HTTP or HTTPS download URL without embedded credentials.')
        filename = filename or unquote(parsed.path.rsplit('/', 1)[-1])
    if operation == 'activate':
        filename = ''
    elif not FILE_NAME.fullmatch(filename):
        raise ValueError('Enter a plain file name using letters, digits, spaces, dots, underscores or hyphens.')
    credentials = {}
    if not download_only:
        host, user, password = data.get('host', ''), data.get('user', ''), data.get('password', '')
        if not isinstance(host, str) or not host.strip() or len(host) > 253 or re.search(r'[\s/@?#]', host):
            raise ValueError('Enter a Velo device IP address or hostname, without a URL or port.')
        if not isinstance(user, str) or not user.strip() or len(user) > 128 or any(ord(c) < 32 for c in user):
            raise ValueError('Enter a valid device username.')
        if not isinstance(password, str) or not password:
            raise ValueError('Enter the Velo device password.')
        credentials = dict(host=host.strip(), user=user.strip(), password=password)
    if operation == 'activate':
        vco = data.get('vco', '')
        key = data.get('activation_key', '')
        valid_vco = isinstance(vco, str) and len(vco) <= 253
        if valid_vco:
            try:
                ipaddress.ip_address(vco)
            except ValueError:
                valid_vco = all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in vco.rstrip('.').split('.'))
        if not valid_vco:
            raise ValueError('Enter a VCO hostname or IP address without a URL or port.')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,255}', key):
            raise ValueError('Enter a valid activation key using letters, digits, underscores or hyphens.')
        credentials.update(vco=vco, activation_key=key)
    return dict(kind='velo', name='Download ' + filename if download_only else ({'update': 'Update on ', 'activate': 'Activation on '}.get(operation, 'Transfer to ')) + credentials['host'],
                filename=filename, source='url' if download_only else 'remote' if operation in ('update', 'activate') else 'downloaded', url=url if download_only else '',
                download_only=download_only, operation=operation,
                reuse_existing=data.get('reuse_existing', True),
                skip_download_tls_verification=data.get('skip_download_tls_verification', True),
                device_credentials=credentials)


def command(client, args, emit=None, timeout=120, workdir=None):
    """Combine both streams, quote every argument, and require a real exit status."""
    channel = client.get_transport().open_session(timeout=20)
    output = []
    pending = ''
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    deadline = time.monotonic() + timeout
    try:
        channel.set_combine_stderr(True)
        channel.settimeout(1)
        text = ' '.join(shlex.quote(str(arg)) for arg in args)
        if workdir:
            text = 'cd ' + shlex.quote(workdir) + ' && ' + text
        channel.exec_command(text)
        while True:
            if time.monotonic() > deadline:
                raise VeloError('Device command timed out. It may still be running; inspect the device before retrying.')
            try:
                data = channel.recv(8192)
            except socket.timeout:
                if not client.get_transport().is_active() or channel.closed:
                    raise VeloError('SSH disconnected. The device may be updating or restarting; completion is unconfirmed. Inspect it before retrying.') from None
                continue
            if not data:
                break
            text = decoder.decode(data)
            if emit:
                pending += text.replace('\r', '\n')
                while '\n' in pending:
                    line, pending = pending.split('\n', 1)
                    if line:
                        emit(line)
                if len(pending) >= 8192:
                    emit(pending); pending = ''
            else:
                output.append(text)
                if sum(map(len, output)) > 65536:
                    raise VeloError('Device response was unexpectedly large.')
        tail = decoder.decode(b'', final=True)
        if emit and pending + tail:
            emit(pending + tail)
        status = channel.recv_exit_status()
        if status == -1:
            raise VeloError('SSH closed without an exit status. The device may be updating or restarting; inspect it before retrying.')
        if status:
            raise VeloError(f'Device command failed with exit code {status}. Review the output above before retrying.')
        return ''.join(output) + tail
    finally:
        channel.close()


@contextmanager
def device_session(root, job, phase):
    credentials = job.credentials
    client = paramiko.SSHClient()
    try:
        keys = Path(root) / 'velo_known_hosts'
        if keys.is_symlink() or (keys.exists() and not keys.is_file()):
            raise VeloError('velo_known_hosts must be a regular file, not a symbolic link.')
        fd = os.open(keys, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        client.load_host_keys(str(keys))
        # Automatically trust lab device keys, including reused IPs, as requested.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        job.emit('Connecting to Velo ' + credentials['host'] + ':22 as ' + credentials['user'], phase=phase, percent=0)
        job.emit('Unknown SSH host keys are saved automatically; changed Velo keys are replaced for reused lab IPs.')
        connect_options = dict(username=credentials['user'], password=credentials['password'],
                               look_for_keys=False, allow_agent=False, timeout=20, banner_timeout=20, auth_timeout=20)
        try:
            client.connect(credentials['host'], **connect_options)
        except paramiko.BadHostKeyException:
            # Retry only the SSH handshake, before any remote command is executed.
            client.close()
            job.emit('Velo SSH host key changed for ' + credentials['host'] +
                     '; replacing its saved key automatically and reconnecting once.')
            client = paramiko.SSHClient()
            client.load_host_keys(str(keys))
            host_keys = client.get_host_keys()
            while credentials['host'] in host_keys:
                del host_keys[credentials['host']]
            # Persist removal so Paramiko's save-time reload cannot restore stale key types.
            host_keys.save(str(keys))
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            # AutoAddPolicy records the newly negotiated key and preserves other hosts.
            client.connect(credentials['host'], **connect_options)
        client.get_transport().set_keepalive(30)
        banner = client.get_transport().get_banner()
        if banner:
            job.emit(banner.decode('utf-8', errors='replace') if isinstance(banner, bytes) else banner)
        yield client
    except paramiko.AuthenticationException:
        raise VeloError('Velo SSH authentication failed. Check the username, password, and root SSH login configuration.') from None
    except paramiko.BadHostKeyException:
        raise VeloError('The Velo SSH host key changed again during reconnection. Check device connectivity before retrying.') from None
    except VeloError:
        raise
    except Exception:
        raise VeloError('Velo device action failed. Check SSH/SCP support, permissions, disk space, and device connectivity; inspect the device before retrying.') from None
    finally:
        client.close()


def activate(root, job):
    with device_session(root, job, 'activate') as client:
        job.emit('Checking activation script availability')
        command(client, ['test', '-x', '/opt/vc/bin/activate.py'])
        job.emit('Running device activation against VCO ' + job.credentials['vco'], phase='activate', percent=0)
        command(client, ['/opt/vc/bin/activate.py', '-i', '-f', '-s',
                         job.credentials['vco'], job.credentials['activation_key']],
                emit=job.emit, timeout=600)
        job.emit('Activation command exited successfully. Verify activation status in the VCO.')


def run_update(root, job):
    remote_path = '/tmp/' + job.filename
    with device_session(root, job, 'update') as client:
        job.emit('Checking root access, /root/swupdater, and existing file ' + remote_path)
        if command(client, ['id', '-u']).strip() != '0':
            raise VeloError('Running /root/swupdater requires root. Configure root password login; vcadmin cannot update by default.')
        command(client, ['test', '-x', '/root/swupdater'])
        command(client, ['test', '-f', remote_path])
        command(client, ['test', '-s', remote_path])
        job.emit('Running in /tmp: /root/swupdater ' + shlex.quote(job.filename) + '. The edge may restart; a disconnected session cannot confirm completion.', phase='update', percent=0)
        command(client, ['/root/swupdater', job.filename], emit=job.emit, timeout=7200, workdir='/tmp')
        job.emit('swupdater exited successfully. Check the device for its resulting version and health.')


def transfer(root, job, path):
    remote_path = '/tmp/' + job.filename
    staging_dir = '/tmp/.velo-' + job.id
    staging = staging_dir + '/payload'
    uploaded = False
    staging_created = False
    with device_session(root, job, 'transfer') as client:
        try:
            job.emit('Checking OpenWrt SCP support and free space in /tmp (usually RAM-backed)')
            try:
                command(client, ['command', '-v', 'scp'])
            except VeloError:
                raise VeloError('The edge needs an SCP receiver available in its SSH command path. SFTP is not required; check SCP support on this firmware.') from None
            listing = command(client, ['df', '-Pk', '/tmp']).splitlines()
            try:
                available = int(listing[-1].split()[3]) * 1024
            except (IndexError, ValueError):
                raise VeloError('Could not determine free space in the edge /tmp directory; upload was not started.') from None
            if available < path.stat().st_size + 16 * 1024**2:
                raise VeloError('Not enough free space in edge /tmp for this file plus a 16 MiB reserve. /tmp is usually RAM-backed on OpenWrt.')
            command(client, ['test', '!', '-d', remote_path])
            command(client, ['mkdir', '-m', '700', staging_dir])
            staging_created = True
            last_percent = -1
            def progress(filename, total, sent):
                nonlocal last_percent
                percent = min(100, int(sent * 100 / max(total, 1)))
                if percent != last_percent:
                    job.emit(f'SCP upload: {percent}% ({sent / 1048576:.1f}/{total / 1048576:.1f} MiB)', phase='transfer', percent=percent)
                    last_percent = percent
            job.emit('Uploading ' + job.filename + ' to device /tmp')
            with SCPClient(client.get_transport(), progress=progress, socket_timeout=120) as scp:
                scp.put(str(path), remote_path=staging)
            job.emit('Verifying uploaded file size')
            if command(client, ['wc', '-c', staging]).split()[0] != str(path.stat().st_size):
                raise VeloError('Uploaded size does not match the saved file. Updater was not started.')
            command(client, ['mv', '-f', staging, remote_path])
            uploaded = True
            command(client, ['rmdir', staging_dir])
            staging_created = False
            job.emit('Transfer complete: ' + remote_path, phase='transfer', percent=100)
        finally:
            # Best-effort cleanup of this job's partial upload only; retain completed files.
            if staging_created and client.get_transport() and client.get_transport().is_active():
                try:
                    if not uploaded:
                        command(client, ['rm', '-f', staging], timeout=10)
                    command(client, ['rmdir', staging_dir], timeout=10)
                except Exception:
                    job.emit('Could not remove temporary upload directory ' + staging_dir + '; inspect the device.')
