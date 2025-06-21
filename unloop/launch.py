#!/usr/bin/env python3
"""
VampNet Launcher: orchestrates EC2 instance management, a single SSH session
(with port-forwarding and real-time remote stdout/stderr), local client startup,
and Max patch loading. Supports a --hold mode to delay cleanup until this script
is killed, even after Max exits.
"""
import argparse
import json
import logging
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
import boto3


def setup_logger():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    return logging.getLogger(__name__)


class VampNetLauncher:
    def __init__(self, config_path: Path, hold: bool = False):
        self.logger = setup_logger()
        self.config = self._load_config(config_path)
        self.ssh_proc = None
        self.client_proc = None
        self.max_proc = None
        self.hold = hold
        self.ec2_started = False

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda s, f: self._handle_exit())

    def _load_config(self, path: Path) -> dict:
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        cfg = json.loads(path.read_text())
        required = ('server', 'python_path_server', 'vampnet_dir_server', 'port', 'maxpat')
        missing = [k for k in required if k not in cfg]
        if missing:
            raise KeyError(f"Missing config keys: {', '.join(missing)}")
        if cfg['server'] == "user@your-server" or cfg['python_path_server'] == "/path/to/python" or cfg[
            'vampnet_dir_server'] == "/path/to/vampnet":
            raise ValueError(f"First update {required} in launch_config.json.")

        # Add EC2 configuration
        cfg['ec2_instance_id'] = cfg.get('ec2_instance_id')
        cfg['ec2_region'] = cfg.get('ec2_region')
        cfg['ec2_client'] = boto3.client('ec2', region_name=cfg['ec2_region']) if cfg['ec2_instance_id'] else None
        return cfg

    @staticmethod
    def _stream_pipe(pipe, prefix, log_fn):
        try:
            for line in iter(pipe.readline, b""):
                log_fn(f"{prefix} {line.decode().rstrip()}")
        finally:
            pipe.close()

    def _get_instance_state(self):
        """Get the current state of the EC2 instance."""
        response = self.config['ec2_client'].describe_instances(InstanceIds=[self.config['ec2_instance_id']])
        instances = response['Reservations'][0]['Instances']
        if instances:
            return instances[0]['State']['Name']
        return None

    def _wait_for_instance_state(self, target_state, timeout=300):
        """Wait for instance to reach target state."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            state = self._get_instance_state()
            if state == target_state:
                return True
            self.logger.info(f"Instance state: {state}, waiting for {target_state}...")
            time.sleep(5)
        return False

    def _start_ec2_instance(self):
        """Start the EC2 instance if instance_id is configured."""
        if not self.config['ec2_instance_id']:
            self.logger.info("No EC2 instance ID configured, skipping EC2 start")
            return

        self.logger.info(
            f"Starting EC2 instance: {self.config['ec2_instance_id']} in region {self.config['ec2_region']}")
        current_state = self._get_instance_state()
        self.logger.info(f"Current instance state: {current_state}")

        if current_state == "running":
            self.logger.info("Instance is already running")
            self.ec2_started = False  # We didn't start it, so don't stop it
            return
        elif current_state in ["stopping", "pending"]:
            self.logger.info(f"Instance is {current_state}, waiting for stable state...")
            # Wait for the instance to finish transitioning
            if current_state == "stopping":
                self._wait_for_instance_state("stopped", timeout=120)
            elif current_state == "pending":
                self._wait_for_instance_state("running", timeout=120)
                if self._get_instance_state() == "running":
                    self.ec2_started = False
                    return
            current_state = self._get_instance_state()

        # Start the instance if it's stopped
        if current_state == "stopped":
            self.config['ec2_client'].start_instances(InstanceIds=[self.config['ec2_instance_id']])
            self.ec2_started = True
            self.logger.info("EC2 start command issued, waiting for instance to be ready...")

            # Wait for instance to be running
            waiter = self.config['ec2_client'].get_waiter('instance_running')
            waiter.wait(InstanceIds=[self.config['ec2_instance_id']])
            self.logger.info("EC2 instance is now running")

            # Get the public IP/DNS if the server config uses a placeholder
            if self.config['server'].endswith('@ec2-instance'):
                response = self.config['ec2_client'].describe_instances(InstanceIds=[self.config['ec2_instance_id']])
                instance = response['Reservations'][0]['Instances'][0]
                public_ip = instance.get('PublicIpAddress') or instance.get('PublicDnsName')
                if public_ip:
                    user = self.config['server'].split('@')[0]
                    self.config['server'] = f"{user}@{public_ip}"
                    self.logger.info(f"Updated server address to: {self.config['server']}")

            # Additional wait for SSH to be ready
            self.logger.info("Waiting for SSH service to be ready...")
            time.sleep(30)  # Adjust based on your instance boot time

    def _stop_ec2_instance(self):
        """Stop the EC2 instance if we started it."""
        if not self.config['ec2_instance_id'] or not self.ec2_started:
            if not self.config['ec2_instance_id']:
                self.logger.info("No EC2 instance ID configured, skipping EC2 stop")
            else:
                self.logger.info("EC2 instance was already running, not stopping it")
            return

        self.logger.info(f"Stopping EC2 instance: {self.config['ec2_instance_id']}")
        self.config['ec2_client'].stop_instances(InstanceIds=[self.config['ec2_instance_id']])
        self.logger.info("EC2 stop command issued")

    def _run_remote(self):
        # test SSH connection before anything else
        self.logger.info(f"Testing SSH connection to {self.config['server']}...")
        max_retries = 3
        for attempt in range(max_retries):
            try:
                subprocess.check_call([
                    "ssh", "-q",
                    "-o", "ConnectTimeout=10",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=no",  # For dynamic IPs
                    self.config['server'], "true"
                ])
                break
            except subprocess.CalledProcessError:
                if attempt < max_retries - 1:
                    self.logger.warning(f"SSH connection attempt {attempt + 1} failed, retrying...")
                    time.sleep(10)
                else:
                    raise RuntimeError(
                        f"SSH connection to {self.config['server']} failed after {max_retries} attempts. "
                        f"Try running `ssh {self.config['server']}` and check for issues.")

        self.logger.info(f"SSH connection to {self.config['server']} is OK")
        port = int(self.config['port'])
        remote_dir = self.config['vampnet_dir_server']
        remote_py = self.config['python_path_server']
        cmd = [
            "ssh",
            "-tt",  # Force pseudo-terminal allocation, so that app.py is killed once the ssh session ends
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=no",
            "-L", f"{port}:localhost:{port}",
            self.config['server'],
            f"bash -lc \"cd {remote_dir} || exit 1; exec {remote_py} -u app.py "
            f"--args.load conf/wham.yml --Interface.device cuda\""
        ]
        self.logger.info(f"SSH+remote cmd: {' '.join(cmd)}")
        self.ssh_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        # Demote stderr to INFO
        threading.Thread(
            target=self._stream_pipe,
            args=(self.ssh_proc.stderr, "[Remote][ERR]", self.logger.info),
            daemon=True
        ).start()

        # Read stdout until readiness marker appears.
        # The logic is that the loop will block until the remote app is ready, or break early (with ready=False)
        # if the remote app fails to start correctly.
        ready_marker = "Running on local URL"
        ready = False
        for raw in self.ssh_proc.stdout:
            line = raw.decode().rstrip()
            self.logger.info(f"[Remote][OUT] {line}")
            if ready_marker in line:
                ready = True
                break
        if not ready:
            raise RuntimeError(
                f"Remote app did not start correctly."
            )
        self.logger.info("Remote app is ready, port-forwarding established.")

        # Drain remaining stdout asynchronously
        threading.Thread(
            target=self._stream_pipe,
            args=(self.ssh_proc.stdout, "[Remote][OUT]", self.logger.info),
            daemon=True
        ).start()

    def _teardown(self):
        self.logger.info("Tearing down processes...")
        # kill SSH + remote app
        if self.ssh_proc and self.ssh_proc.poll() is None:
            self.logger.info(f"Terminating SSH/remote (PID {self.ssh_proc.pid})")
            self.ssh_proc.terminate()
            try:
                self.ssh_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.logger.warning("SSH/remote did not exit; killing")
                self.ssh_proc.kill()

        # kill local client
        if self.client_proc and self.client_proc.poll() is None:
            self.logger.info(f"Terminating client (PID {self.client_proc.pid})")
            self.client_proc.terminate()
            try:
                self.client_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.logger.warning("Client did not exit; killing")
                self.client_proc.kill()

        # kill Max
        if self.max_proc and self.max_proc.poll() is None:
            self.logger.info(f"Terminating Max (PID {self.max_proc.pid})")
            self.max_proc.terminate()
            try:
                self.max_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.logger.warning("Max did not exit; killing")
                self.max_proc.kill()

        # Stop EC2 instance
        self._stop_ec2_instance()

    def _handle_exit(self):
        self._teardown()
        sys.exit(0)

    def run(self):
        root = Path(__file__).resolve().parent.parent
        patch = root / self.config['maxpat']
        client = root / 'unloop' / 'client.py'

        # Preflight checks
        missing = []
        if not patch.exists():   missing.append(f"Max patch not found: {patch}")
        if not client.exists():  missing.append(f"Client script not found: {client}")
        if missing:
            for m in missing:
                self.logger.error(m)
            raise FileNotFoundError("Required files missing, aborting launch.")

        try:
            # 0) Start EC2 instance if configured
            self._start_ec2_instance()

            # 1) SSH + remote app + port-forward + log streaming
            self._run_remote()

            # 2) Launch local client
            port = int(self.config['port'])
            client_cmd = [
                sys.executable, str(client),
                '--vampnet_url', f"http://127.0.0.1:{port}/"
            ]
            self.logger.info(f"Client cmd: {' '.join(client_cmd)}")
            self.client_proc = subprocess.Popen(
                client_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            threading.Thread(
                target=self._stream_pipe,
                args=(self.client_proc.stdout, "[Client]", self.logger.info),
                daemon=True
            ).start()
            threading.Thread(
                target=self._stream_pipe,
                args=(self.client_proc.stderr, "[Client][ERR]", self.logger.info),
                daemon=True
            ).start()

            # 3) Open Max and wait for it to exit
            self.logger.info(f"Opening Max patch and waiting: {patch}")
            self.max_proc = subprocess.Popen(
                ["open", "-n", "-W", "-a", "Max", str(patch)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            threading.Thread(
                target=self._stream_pipe,
                args=(self.max_proc.stdout, "[Max]", self.logger.info),
                daemon=True
            ).start()
            threading.Thread(
                target=self._stream_pipe,
                args=(self.max_proc.stderr, "[Max][ERR]", self.logger.info),
                daemon=True
            ).start()

            self.max_proc.wait()

            # 4) After Max exits, keep the launcher alive until it's killed
            if self.hold:
                # prevent SIGCHLD (child exit) from waking us up
                signal.signal(signal.SIGCHLD, signal.SIG_IGN)
                self.logger.info("Max exited; awaiting SIGINT/SIGTERM to clean up.")
                while True:
                    signal.pause()

        except (subprocess.SubprocessError, RuntimeError, ValueError) as e:
            self.logger.error(f"Launcher error: {e}")
        finally:
            self._teardown()


def main():
    parser = argparse.ArgumentParser(description="Launch VampNet with JSON config.")
    parser.add_argument(
        '--config', required=True, help='Path to JSON config'
    )
    parser.add_argument(
        '--hold', action='store_true',
        help='Delay cleanup until this script is killed, even after Max exits.'
    )
    args = parser.parse_args()
    VampNetLauncher(Path(args.config), hold=args.hold).run()


if __name__ == '__main__':
    main()