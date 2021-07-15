import os
import random
import shlex
import subprocess
import time
import fabric2
from fabric2 import Connection, ThreadingGroup

from pathlib import Path


# NOTE 1: Not certain I actually need this, but was a proposed fix for 'error reading SSH banner' and I haven't seen
#         that error since. https://github.com/paramiko/paramiko/issues/673#issuecomment-436815430

def humanize_float(num):
    return "{0:,.2f}".format(num)


MAX_CONNS_PER_GROUP = 10

class ClusterShell:
    """
    ClusterShell lets you run commands on multiple EC2 instances.

    ClusterShell takes in information about a set of EC2 instances that exist and allows you to run commands on
    some or all of the nodes. It also has convenience methods for copying files between the local filesystem and
    the cluster.
    """


    def __init__(self, username, master_ip, worker_ips, ssh_key_path, use_bastion=False,
                 wait_for_ssh=True, wait_for_ssh_timeout=120):
        """
        Args:
            username: The username used to ssh to the instance. Often 'ubuntu' or 'ec2-user'
            master_ip: A single IP for the master node. Typically should be the public IP if the location this code is
                       running is outside of the VPC and the private IP if running from another EC2 node in the same
                       VPC. In many cases, the distinction between master and workers is arbitrary. If use_bastion is
                       True, the master node will be the bastion host.
            worker_ips: A possibly empty list of ips for the worker nodes. If there is only a single worker, a string
                        can be passed in instead of a list.
            ssh_key_path: The path to the SSH key required to SSH into the EC2 instances. Often ~/.ssh/something.pem
            use_bastion (bool): Whether or not to use the master node as the bastion host for SSHing to worker nodes.
            wait_for_ssh (bool): If true, block until commands can be run on all instances. This can be useful when you
                                 are launching EC2 instances, because the instances may be in the RUNNING state but the
                                 SSH daemon may not yet be running.
            wait_for_ssh_timeout: Number of seconds to spend trying to run commands on the instances before failing.
                                  This is NOT the SSH timeout, this upper bounds the amount of time spent retrying
                                  failed SSH connections. Only used if wait_for_ssh=True.
        """
        if not isinstance(worker_ips, list):
            worker_ips = [worker_ips]

        self._username = username
        self._master_ip = master_ip
        self._worker_ips = worker_ips
        self._all_ips = [self._master_ip] + self._worker_ips
        self.use_bastion = use_bastion


        connect_kwargs = {
            "key_filename": [os.path.expanduser(ssh_key_path)],
            "banner_timeout": 30    # NOTE 1 above
        }

        self._master_conn = Connection(user=self._username,
                                       host=self._master_ip,
                                       forward_agent=True,
                                       connect_kwargs=connect_kwargs)

        worker_conns = []
        for worker_ip in self._worker_ips:
            if self.use_bastion:
                c = Connection(user=self._username,
                               host=worker_ip,
                               connect_kwargs=connect_kwargs,
                               gateway=Connection(user=self._username,
                                                  host=master_ip,
                                                  forward_agent=True,
                                                  connect_kwargs=connect_kwargs))
            else:
                c = Connection(user=self._username, host=worker_ip, connect_kwargs=connect_kwargs)

            worker_conns.append(c)

        self._individual_worker_conns = worker_conns
        self._worker_conns = ThreadingGroup.from_connections(worker_conns)
        self._all_conns = ThreadingGroup.from_connections([self._master_conn] + worker_conns)

        if wait_for_ssh:
            self.wait_for_ssh_ready(wait_timeout=wait_for_ssh_timeout)

    def wait_for_ssh_ready(self, wait_timeout=120):
        """Repeatedly try to run commands on all instances until successful or until timeout is reached."""

        start_time = time.time()
        exceptions = []
        while True:

            try:
                self.run_on_all("hostname", hide=True)
                break
            except fabric2.exceptions.GroupException as e:
                exceptions.append(e)

                elapsed_time = time.time() - start_time
                if elapsed_time > wait_timeout:
                    exceptions_str = "\n".join([str(e) for e in exceptions])
                    raise RuntimeError(
                            f"[ClusterShell.wait_for_ssh_ready] Unable to establish an SSH connection after "
                            f"{wait_timeout} seconds. On EC2 this is often due to a problem with the security group, "
                            f"although there are many potential causes."
                            f"\nExceptions encountered:\n{exceptions_str}")

                secs_to_timeout = int(wait_timeout - elapsed_time)
                print(f"ClusterShell.wait_for_ssh_ready] Exception when SSHing to instances. Retrying until timeout in "
                      f"{secs_to_timeout} seconds")
                time.sleep(1)

    def run_local(self, cmd):
        """Run a shell command on the local machine.

        Will wait for the command to finish and raise an exception if the return code is non-zero.

        Args:
            cmd: The shell command to run

        Returns:
             The stdout of the command as a byte string.
        """
        return subprocess.check_output(shlex.split(cmd))


    def run_on_master(self, cmd, **kwargs):
        """Run a shell command on the master node.

        Args:
            cmd: The shell command to run
            kwargs: http://docs.fabfile.org/en/2.4/api/connection.html#fabric.connection.Connection.run

        Returns:
            Result: An invoke Result object. `http://docs.pyinvoke.org/en/latest/api/runners.html#invoke.runners.Result`
        """
        return self._master_conn.run(cmd, **kwargs)


    def run_on_all(self, cmd, **run_kwargs):
        """Run a shell command on every node.

        Args:
            cmd: The shell command to run
            run_kwargs: Keyword args to pass to fabric.run(). Fabric passes them through to Invoke, which are
                        documented here: http://docs.pyinvoke.org/en/latest/api/runners.html#invoke.runners.Runner.run.
                        Potentially useful args:
                            hide=True will prevent run output from being output locally

        Returns:
            List of invoke.Result objects. Order is not guaranteed. http://docs.pyinvoke.org/en/latest/api/runners.html#invoke.runners.Result
        """

        if self.use_bastion:
            if len(self._worker_ips) >= (MAX_CONNS_PER_GROUP - 1):
                results = self._run_on_all_workaround(cmd, MAX_CONNS_PER_GROUP, **run_kwargs)
                return list(results)

        results = self._all_conns.run(cmd, **run_kwargs)
        return list(results.values())


    # TODO: Confirm this is required with (10+ nodes)
    def _run_on_all_workaround(self, cmd, group_size, **run_kwargs):
        total_conns = len(self._worker_conns) + 1
        print(f'{total_conns} Nodes')
        groups = []

        group_conns = []
        for i, worker_conn in enumerate(self._individual_worker_conns):
            if i % group_size == 0 and i != 0:
                groups.append(ThreadingGroup.from_connections(group_conns))
                group_conns = []
            group_conns.append(worker_conn)

        flattened_results = []
        # Either add the master to one of the groups or create a group for it (if groups are all full or no workers)
        if len(group_conns) != 0 and len(group_conns) != group_size:
            group_conns.append(self._master_conn)
            groups.append(ThreadingGroup.from_connections(group_conns))

        else:
            if len(group_conns) != 0:
                groups.append(ThreadingGroup.from_connections(group_conns))
            master_result = self.run_on_master(cmd, **run_kwargs)
            flattened_results.append(master_result)

        for i, worker_conn_group in enumerate(groups):
            group_results = worker_conn_group.run(cmd, **run_kwargs)
            flattened_results.extend(group_results.values())

        return flattened_results


    def copy_from_master_to_local(self, remote_path, local_path):
        """Copy a file from the master node to the local node.

        Args:
            remote_path: The path of the file on the master node. If not an absolute path, will be relative to the
                         working directory, typically the home directory. Will not expand tilde (~).
            local_path: The path to save the file to on the local file system.
        """
        local_abs_path = Path(local_path).absolute()
        return self._master_conn.get(remote_path, local_abs_path)


    def copy_from_all_to_local(self, remote_abs_path, local_path):
        """Copy files from all nodes to the local filesystem.

        There will be one directory per node containing the file.

        Args:
            remote_abs_path: The absolute path of the file to download. Can be a directory or a cp/scp string including
                             wildcards
            local_path: The absolute path of a directory on the local filesystem to download the files into. The path
                        must not point to a file.
        """
        if self.use_bastion:
            raise NotImplementedError("Copying has not yet been implemented for bastion mode. Please open a ticket at "
                                      "https://github.com/armandmcqueen/ec2-cluster if you would like to see this "
                                      "feature implemented")
        local_abs_path = Path(local_path).absolute()

        if not local_abs_path.exists():
            local_abs_path.mkdir(parents=True)
        else:
            if local_abs_path.is_file():
                raise RuntimeError(f'[ClusterShell.copy_from_all_to_local] local_path points to a file: '
                                   f'{local_abs_path}')

        master_dir = local_abs_path / "0"
        master_dir.mkdir()
        master_ip_path = master_dir / "ip.txt"

        with open(master_ip_path, 'w') as f:
            f.write(self.master_ip)

        self.run_local(f'scp '
                       f'-o StrictHostKeyChecking=no '
                       f'-o "UserKnownHostsFile /dev/null" '
                       f'-o "LogLevel QUIET" '
                       f'-r '
                       f'{self._username}@{self.master_ip}:{remote_abs_path} {master_dir}/')

        # Create and populate staging folder for each worker's data
        for ind, worker_ip in enumerate(self._worker_ips):
            worker_id = ind + 1
            worker_node_dir = local_abs_path / str(worker_id)
            worker_node_dir.mkdir()
            worker_ip_path = worker_node_dir / "ip.txt"
            with open(worker_ip_path, 'w') as f:
                f.write(worker_ip)
            self.run_local(f'scp '
                           f'-o StrictHostKeyChecking=no '
                           f'-o "UserKnownHostsFile /dev/null" '
                           f'-o "LogLevel QUIET" '
                           f'-r '
                           f'{self._username}@{worker_ip}:{remote_abs_path} {worker_node_dir}/')



    def copy_from_local_to_master(self, local_path, remote_path):
        """Copy a file from the local filesystem to the master node.

        Args:
            local_path: The path of the file to send to the master node
            remote_path: The path where the file will be saved on the master node. Does not expand tilde (~), but if not
                         an absolute path, will usually interpret the path as relative to the home directory.
        """
        local_abs_path = Path(local_path).absolute()
        return self._master_conn.put(local_abs_path, remote_path)

    def copy_from_local_to_all(self, local_path, remote_path):
        """Copy a file from the local filesystem to every node in the cluster.

        Args:
            local_path: The path of the file to send to the master and worker nodes
            remote_path: The path where the file will be saved on the master and worker nodes. Does not expand tilde (~),
                         but if not an absolute path, will usually interpret the path as relative to the home directory.
        """
        if self.use_bastion:
            raise NotImplementedError("Copying has not yet been implemented for bastion mode. Please open a ticket at "
                                      "https://github.com/armandmcqueen/ec2-cluster if you would like to see this "
                                      "feature implemented")

        local_abs_path = Path(local_path).absolute()
        self.copy_from_local_to_master(local_abs_path, remote_path)
        for worker_conn in self._individual_worker_conns:
            worker_conn.put(local_abs_path, remote_path)

    @property
    def username(self):
        """The username used to instantiate the ClusterShell"""
        return self._username

    @property
    def master_ip(self):
        """The master IP used to instantiate the ClusterShell"""
        return self._master_ip

    @property
    def non_master_ips(self):
        """All IPs other than the master node. May be an empty list"""
        return self._worker_ips

    @property
    def all_ips(self):
        """A list of master and worker IPs"""
        return self._all_ips








