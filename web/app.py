"""Single-worker web UI for ESXi deployments and lab actions."""

import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tarfile
import ssl
import shutil
import threading
import time
from urllib.parse import unquote, urlsplit
import uuid

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
import requests
import paramiko

from clone_vm import CloneError, inventory, load_ssh_host_keys
from deploy_ova import ova_descriptor, interface_labels, objects, SmartConnect, Disconnect, vim, validate_nic_mappings


NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,79}\Z")
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,179}\.ova\Z", re.I)


class InputError(Exception):
    pass



def download_directory(root):
    directory = root / "OVA-download"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise InputError("OVA-download must be a real directory inside the repository, not a symbolic link.")
    return directory


def read_credentials(root):
    try:
        data = json.loads((root / "creds.json").read_text())
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        raise InputError("Cannot read creds.json. Check its format and permissions.") from None


def save_credentials(root, data):
    old = read_credentials(root)
    host, user = str(data.get("host", "")).strip(), str(data.get("user", "")).strip()
    if not host or len(host) > 253 or re.search(r"[\s/@?#]", host):
        raise InputError("Enter an ESXi IP address or hostname, without a URL or port.")
    if not user or len(user) > 128 or any(ord(char) < 32 for char in user):
        raise InputError("Enter a valid ESXi username.")
    password = data.get("password", "")
    if not isinstance(password, str):
        raise InputError("Password must be text.")
    if not password:
        if old.get("host") == host and old.get("user") == user:
            password = old.get("password")
        if not password:
            raise InputError("Enter a password for this ESXi host and username.")
    updated = dict(old, host=host, user=user, password=password)
    fd, temporary = tempfile.mkstemp(prefix=".creds-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(updated, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, root / "creds.json")
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"host": host, "user": user, "password_saved": True}


def fetch_vms(credentials):
    if not all(credentials.get(key) for key in ("host", "user", "password")):
        raise InputError("Save the ESXi connection before loading VMs.")
    client = paramiko.SSHClient()
    try:
        load_ssh_host_keys(client)
        client.connect(credentials["host"], port=credentials.get("port", 22),
            username=credentials["user"], password=credentials["password"],
            look_for_keys=False, allow_agent=False, timeout=20, banner_timeout=20, auth_timeout=20)
        vms = [dict(id=vmid, name=name) for vmid, name, location in inventory(client)]
        return sorted(vms, key=lambda vm: (vm["name"].casefold(), int(vm["id"])))
    except paramiko.AuthenticationException:
        raise InputError("ESXi SSH login failed. Check the saved username and password.") from None
    except (paramiko.SSHException, OSError):
        raise InputError("Cannot load ESXi VMs. Check SSH connectivity and the host's trusted SSH key.") from None
    except Exception:
        raise InputError("Could not read the VM inventory from ESXi. Check account permissions.") from None
    finally:
        client.close()


def fetch_networks(credentials):
    service = None
    try:
        if not all(credentials.get(k) for k in ("host", "user", "password")):
            raise InputError("Save the ESXi connection first.")
        service = SmartConnect(host=credentials["host"], user=credentials["user"], pwd=credentials["password"],
                               sslContext=ssl._create_unverified_context())
        hosts = objects(service.RetrieveContent(), vim.HostSystem)
        if len(hosts) != 1:
            raise InputError("Connect directly to a standalone ESXi host.")
        return sorted(net.name for net in hosts[0].network if not isinstance(net, vim.dvs.DistributedVirtualPortgroup))
    except Exception:
        raise InputError("Cannot read ESXi networks. Check the saved connection and account permissions.") from None
    finally:
        if service is not None:
            Disconnect(service)


def nic_input(value):
    try:
        return validate_nic_mappings(value)
    except CloneError as exc:
        raise InputError(str(exc)) from None


def deployment_input(data):
    source = data.get("source", "url")
    if source not in ("url", "downloaded"):
        raise InputError("Select a download link or a saved OVA.")
    url = str(data.get("url", "")).strip()
    filename = str(data.get("filename", "")).strip()
    if source == "url":
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not valid or len(url) > 8192:
            raise InputError("Enter an HTTP or HTTPS OVA download link without embedded credentials.")
        filename = filename or unquote(parsed.path.rsplit("/", 1)[-1])
    if source == "url" and filename and not filename.lower().endswith(".ova"):
        filename += ".ova"
    if not FILENAME.fullmatch(filename):
        raise InputError("Enter a file name using letters, numbers, spaces, '.', '_' or '-'. Paths are not allowed.")
    name = str(data.get("name", "")).strip()
    if not NAME.fullmatch(name):
        raise InputError("VM name must be 1–80 characters, starting with a letter or digit; use letters, numbers, spaces, '.', '_' or '-'.")
    datastore = str(data.get("datastore", "datastore1")).strip()
    iso_folder = str(data.get("iso_folder", "00-ISO")).strip()
    if not datastore or not iso_folder or any(ord(c) < 32 for c in datastore + iso_folder):
        raise InputError("Enter a datastore and ISO folder.")
    ge6 = data.get("ge6_nic")
    if ge6 not in (None, ""):
        try:
            ge6 = int(ge6)
        except (ValueError, TypeError):
            raise InputError("GE6 NIC position must be a positive integer.") from None
        if not 1 <= ge6 <= 128:
            raise InputError("GE6 NIC position must be between 1 and 128.")
    else:
        ge6 = None
    for flag in ("reuse_existing", "download_only"):
        if flag in data and not isinstance(data[flag], bool):
            raise InputError(f"{flag} must be true or false.")
    return dict(source=source, url=url if source == "url" else "", filename=filename, name=name, datastore=datastore, iso_folder=iso_folder,
                ge6_nic=ge6, nic_mappings=nic_input(data.get("nic_mappings", [])),
                network_host=str(data.get("network_host", "")), network_user=str(data.get("network_user", "")),
                download_only=data.get("download_only") is True, insecure=True, reuse_existing=source == "downloaded" or data.get("reuse_existing", False))


def action_input(data):
    kind = data.get("kind", "deployment")
    if kind == "deployment":
        return dict(deployment_input(data), kind=kind)
    name = str(data.get("name", "")).strip()
    if kind == "clone":
        source = str(data.get("source_vm", "")).strip()
        source_id = str(data.get("source_vm_id", "")).strip()
        if source_id and not re.fullmatch(r"[1-9][0-9]{0,9}", source_id):
            raise InputError("Select a valid source VM from the dropdown.")
        if (not source and not source_id) or len(source) > 256 or any(ord(c) < 32 for c in source):
            raise InputError("Enter the exact source VM name.")
        if not NAME.fullmatch(name) or name == source:
            raise InputError("Enter a different clone name using 1–80 letters, digits, spaces, '.', '_' or '-'.")
        datastore = str(data.get("datastore", "")).strip()
        if len(datastore) > 256 or any(ord(c) < 32 for c in datastore):
            raise InputError("Enter a valid datastore name, or leave it blank to use the source datastore.")
        return dict(kind=kind, name=name, source_vm=source, source_vm_id=source_id, datastore=datastore,
                    source_host=str(data.get("source_host", "")), source_user=str(data.get("source_user", "")))
    if kind == "network":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,30}", name):
            raise InputError("Network name must be 1–31 letters, digits, spaces, '.', '_' or '-', starting with a letter or digit.")
        return dict(kind=kind, name=name)
    raise InputError("Select deployment, cloning, or network creation.")


class Job:
    def __init__(self, options, credentials):
        self.id = uuid.uuid4().hex
        self.name = options["name"]
        self.kind = options.get("kind", "deployment")
        self.filename = options.get("filename")
        self.status = "running"
        self.phase = "download" if self.kind == "deployment" else self.kind
        self.percent = 0
        self.lines = []
        self.sequence = 0
        self.condition = threading.Condition()
        self.options = options
        self.credentials = credentials

    def emit(self, text, phase=None, percent=None):
        # Never return saved passwords, including accidental subprocess output.
        password = self.credentials.get("password", "")
        if password:
            text = text.replace(password, "[redacted]")
        with self.condition:
            if phase:
                self.phase = phase
            if percent is not None:
                self.percent = percent
            self.sequence += 1
            self.lines.append({"id": self.sequence, "text": text, "phase": self.phase, "percent": self.percent})
            self.lines = self.lines[-5000:]
            self.condition.notify_all()

    def finish(self, success):
        with self.condition:
            self.status = "success" if success else "failed"
            self.condition.notify_all()

    def summary(self):
        with self.condition:
            return dict(id=self.id, kind=self.kind, name=self.name, filename=self.filename, status=self.status,
                        phase=self.phase, percent=self.percent)


class Jobs:
    def __init__(self, root):
        self.max_download_bytes = int(os.environ.get("MAX_OVA_DOWNLOAD_BYTES", 50 * 1024**3))
        if self.max_download_bytes <= 0:
            raise ValueError("MAX_OVA_DOWNLOAD_BYTES must be positive")
        self.root = root
        self.lock = threading.Lock()
        self.current = None

    def in_use(self, filename):
        return bool(self.current and self.current.status == "running" and self.current.filename == filename)

    def downloads(self):
        result = []
        with self.lock:
            directory = download_directory(self.root)
            for path in directory.iterdir() if directory.exists() else []:
                if not FILENAME.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                result.append(dict(name=path.name, size=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    in_use=self.in_use(path.name)))
        return sorted(result, key=lambda item: item["modified"], reverse=True)

    def delete_download(self, filename):
        if not isinstance(filename, str) or not FILENAME.fullmatch(filename):
            raise InputError("Select a valid OVA file name. Paths are not allowed.")
        with self.lock:
            if self.in_use(filename):
                raise InputError("This OVA is being used by the active deployment and cannot be deleted.")
            path = download_directory(self.root) / filename
            if path.is_symlink() or not path.is_file():
                raise InputError("The selected OVA no longer exists or is not a regular file.")
            path.unlink()

    def start(self, data):
        options = action_input(data)
        with self.lock:
            if self.current and self.current.status == "running":
                raise InputError("An ESXi action is already running. Wait for it to finish.")
            credentials = read_credentials(self.root)
            if not options.get("download_only") and not all(credentials.get(key) for key in ("host", "user", "password")):
                raise InputError("Save the ESXi connection before starting an action.")
            if options["kind"] == "clone" and options["source_vm_id"]:
                if options["source_host"] != credentials["host"] or options["source_user"] != credentials["user"]:
                    raise InputError("The ESXi connection changed. Refresh the source VM list and select a VM again.")
            if options["kind"] == "deployment":
                if options["nic_mappings"] and (options["network_host"] != credentials["host"] or options["network_user"] != credentials["user"]):
                    raise InputError("The ESXi connection changed. Reload the network list before deploying.")
                path = download_directory(self.root) / options["filename"]
                if (path.exists() or path.is_symlink()) and not options["reuse_existing"]:
                    raise InputError("That download already exists. Choose a different file name or enable reuse.")
                if path.is_symlink():
                    raise InputError("The download path must not be a symbolic link.")
                if options["source"] == "downloaded" and not path.is_file():
                    raise InputError("The selected OVA no longer exists. Refresh the downloaded OVA list.")
            job = Job(options, credentials)
            self.current = job
            threading.Thread(target=self.run, args=(job,), daemon=True).start()
            return job

    def download(self, job):
        directory = download_directory(self.root)
        directory.mkdir(exist_ok=True)
        target = directory / job.filename
        if target.exists():
            if not job.options["reuse_existing"] or not target.is_file() or target.is_symlink():
                raise InputError("Download path already exists and cannot be reused.")
            job.emit(f"Using existing download: OVA-download/{job.filename}", percent=100)
            return target
        if job.options.get("source") == "downloaded":
            raise InputError("The selected OVA no longer exists.")
        temporary = directory / (job.filename + "." + job.id + ".part")
        job.emit(f"Downloading OVA to OVA-download/{job.filename}", percent=0)
        try:
            with requests.get(job.options["url"], stream=True, timeout=(20, 120),
                              headers={"Accept-Encoding": "identity"}) as response:
                if not 200 <= response.status_code < 300:
                    raise InputError(f"Download server returned HTTP {response.status_code}.")
                try:
                    total = int(response.headers.get("Content-Length", "0"))
                except ValueError:
                    total = 0
                if total > self.max_download_bytes:
                    raise InputError("OVA exceeds the configured download size limit.")
                received, last_update, last_percent = 0, 0, -1
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        if received + len(chunk) > self.max_download_bytes:
                            raise InputError("OVA exceeds the configured download size limit.")
                        if shutil.disk_usage(directory).free < len(chunk) + 1024**3:
                            raise InputError("Download stopped to preserve 1 GiB of free disk space.")
                        stream.write(chunk)
                        received += len(chunk)
                        percent = min(100, int(received * 100 / total)) if total > 0 else None
                        if time.monotonic() - last_update >= 1 or percent != last_percent:
                            size = f"{received / 1048576:.1f} MiB"
                            if total > 0:
                                size += f" / {total / 1048576:.1f} MiB ({percent}%)"
                            job.emit("Download: " + size, percent=percent)
                            last_update, last_percent = time.monotonic(), percent
                if received == 0 or (total > 0 and received != total):
                    raise InputError("The download is empty or incomplete.")
            # Link instead of replacing: never overwrite an existing download.
            os.link(temporary, target)
            job.emit(f"Download complete: {received / 1048576:.1f} MiB saved as {job.filename}", percent=100)
            return target
        except requests.exceptions.SSLError:
            raise InputError("Download certificate verification failed. Configure HOST_CA_BUNDLE with a CA bundle that trusts the download server, then recreate the GUI container.") from None
        except requests.exceptions.ConnectTimeout:
            raise InputError("Connection to the download server timed out. Check its address and network access.") from None
        except requests.exceptions.ReadTimeout:
            raise InputError("The download server stopped responding. Retry the download.") from None
        except requests.exceptions.ProxyError:
            raise InputError("Cannot reach the download server through the configured proxy.") from None
        except requests.exceptions.ConnectionError as exc:
            detail = str(exc).lower()
            if any(term in detail for term in ("name resolution", "nameresolutionerror", "name or service not known")):
                raise InputError("Cannot resolve the download server hostname. Check DNS and network access.") from None
            raise InputError("Cannot connect to the download server. Check its address and network access.") from None
        except requests.RequestException:
            # Request exceptions can include signed URLs. Keep them out of output.
            raise InputError("Download failed. Check the link, server availability, and free disk space.") from None
        finally:
            temporary.unlink(missing_ok=True)

    def run(self, job):
        try:
            path = self.download(job) if job.kind == "deployment" else None
            if job.kind == "deployment" and job.options.get("download_only"):
                job.emit("Download complete. The OVA is saved in your library and ready for deployment.", phase="done", percent=100)
                job.finish(True)
                return
            labels = {"deployment": "OVA deployment", "clone": "VM cloning", "network": "internal network creation"}
            job.emit("Starting ESXi " + labels[job.kind], phase="deploy" if job.kind == "deployment" else job.kind, percent=0)
            # Isolate credentials for this job if the saved connection changes later.
            with tempfile.TemporaryDirectory(prefix="esxi-gui-") as temporary:
                credentials = Path(temporary) / "creds.json"
                fd = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(job.credentials, stream)
                command = [sys.executable, "-u"]
                if job.kind == "deployment":
                    command.extend([str(self.root / "deploy_ova.py"), str(path),
                        "--name", job.name, "--creds-file", str(credentials),
                        "--datastore", job.options["datastore"], "--iso-folder", job.options["iso_folder"]])
                    mappings = job.options.get("nic_mappings", [])
                    if mappings:
                        # Validate the exact OVA before any ESXi mutation.
                        with tarfile.open(path, "r:*") as archive:
                            _, root, _, _ = ova_descriptor(archive)
                            labels_in_ova = interface_labels(root)
                        if any(m["index"] > len(labels_in_ova) or labels_in_ova[m["index"]-1] != m["label"] for m in mappings):
                            raise InputError("OVA interfaces changed. Reload the interface list.")
                        mapping_file = Path(temporary) / "interfaces.json"
                        mapping_file.write_text(json.dumps(mappings))
                        command.extend(["--nic-config", str(mapping_file)])
                    if job.options["insecure"]:
                        command.append("--insecure")
                    if job.options["ge6_nic"] is not None:
                        command.extend(["--ge6-nic", str(job.options["ge6_nic"])])
                elif job.kind == "clone":
                    source = "--vm-id=" + job.options["source_vm_id"] if job.options.get("source_vm_id") else "--vm=" + job.options["source_vm"]
                    command.extend([str(self.root / "clone_vm.py"), source,
                        "--clone-name", job.name, "--creds-file", str(credentials)])
                    if job.options["datastore"]:
                        command.extend(["--datastore", job.options["datastore"]])
                else:
                    command.extend([str(self.root / "create_test_network.py"), job.name])
                environment = dict(os.environ, ESXI_CREDS_FILE=str(credentials))
                with subprocess.Popen(command, cwd=self.root, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      env=environment, text=True, bufsize=1) as process:
                    for line in process.stdout:
                        line = line.rstrip()
                        match = re.search(r"(?:Uploading .*?: |Clone: )(\d+)%", line)
                        job.emit(line, percent=int(match.group(1)) if match else None)
                    code = process.wait()
                if code:
                    raise InputError(f"{labels[job.kind].capitalize()} exited with code {code}. Review the output above before retrying.")
            completed = {"deployment": "Deployment completed successfully. The VM is powered off.",
                         "clone": "Cloning completed successfully. Source and clone remain powered off.",
                         "network": "Internal network created successfully."}
            job.emit(completed[job.kind], phase="done", percent=100)
            job.finish(True)
        except Exception as exc:
            message = str(exc) if isinstance(exc, InputError) else "Operation failed. Check file permissions, disk space, and ESXi connectivity."
            job.emit("ERROR: " + message)
            job.finish(False)


def create_app(root=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024
    trusted_hosts = [host.strip() for host in os.environ.get("GUI_TRUSTED_HOSTS", "").split(",") if host.strip()]
    app.config["TRUSTED_HOSTS"] = trusted_hosts or None
    root = Path(root or Path(__file__).resolve().parents[1])
    jobs = Jobs(root)
    app.extensions["jobs"] = jobs

    @app.before_request
    def protect_requests():
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if request.headers.get("Sec-Fetch-Site") == "cross-site" or (origin and origin != request.host_url.rstrip("/")):
                return jsonify(error="Cross-origin requests are not allowed."), 403
            if not request.is_json:
                return jsonify(error="Expected a JSON request."), 415

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(InputError)
    def invalid_input(exc):
        return jsonify(error=str(exc)), 400

    def body():
        data = request.get_json()
        if not isinstance(data, dict):
            raise InputError("Expected a JSON object.")
        return data

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/connection")
    def connection():
        data = read_credentials(root)
        return jsonify(host=data.get("host", ""), user=data.get("user", ""), password_saved=bool(data.get("password")))

    @app.post("/api/connection")
    def save_connection():
        with jobs.lock:
            return jsonify(save_credentials(root, body()))

    @app.get("/api/jobs/current")
    def current_job():
        return jsonify(job=jobs.current.summary() if jobs.current else None)

    @app.get("/api/vms")
    def vms():
        with jobs.lock:
            credentials = read_credentials(root)
        return jsonify(host=credentials.get("host", ""), user=credentials.get("user", ""), vms=fetch_vms(credentials))

    @app.get("/api/networks")
    def networks():
        with jobs.lock:
            credentials = read_credentials(root)
        return jsonify(host=credentials.get("host", ""), user=credentials.get("user", ""), networks=fetch_networks(credentials))

    @app.get("/api/ova/interfaces")
    def ova_interfaces():
        filename = request.args.get("filename", "")
        if not FILENAME.fullmatch(filename):
            raise InputError("Select a saved OVA.")
        path = download_directory(root) / filename
        if path.is_symlink() or not path.is_file():
            raise InputError("Saved OVA not found.")
        try:
            with tarfile.open(path, "r:*") as archive:
                _, ovf, _, _ = ova_descriptor(archive)
                labels = interface_labels(ovf)
                from deploy_ova import ge6_index
                try:
                    ge6, _ = ge6_index(ovf)
                except Exception:
                    ge6 = None
            return jsonify(filename=filename, interfaces=[dict(index=i+1, label=label, ge6=i == ge6) for i, label in enumerate(labels)])
        except Exception:
            raise InputError("Cannot read interfaces from this OVA. Check that it contains a valid single-VM OVF descriptor.") from None

    @app.get("/api/downloads")
    def downloads():
        return jsonify(files=jobs.downloads())

    @app.post("/api/downloads/delete")
    def delete_download():
        jobs.delete_download(body().get("filename"))
        return jsonify(deleted=True)

    @app.post("/api/jobs")
    def start_job():
        job = jobs.start(body())
        return jsonify(job=job.summary()), 202

    @app.get("/api/jobs/<job_id>/events")
    def events(job_id):
        job = jobs.current
        if job is None or job.id != job_id:
            return jsonify(error="Job not found."), 404
        try:
            after = max(0, int(request.headers.get("Last-Event-ID", "0")))
        except ValueError:
            after = 0

        @stream_with_context
        def generate():
            cursor = after
            while True:
                with job.condition:
                    lines = [line for line in job.lines if line["id"] > cursor]
                    status = job.status
                    if not lines and status == "running":
                        job.condition.wait(timeout=10)
                        lines = [line for line in job.lines if line["id"] > cursor]
                        status = job.status
                for line in lines:
                    cursor = line["id"]
                    yield f"id: {cursor}\nevent: output\ndata: {json.dumps(line)}\n\n"
                if status != "running":
                    yield f"event: complete\ndata: {json.dumps(job.summary())}\n\n"
                    return
                if not lines:
                    yield ": keepalive\n\n"

        return Response(generate(), mimetype="text/event-stream", headers={"X-Accel-Buffering": "no"})

    return app
