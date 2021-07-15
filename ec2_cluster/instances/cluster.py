import boto3
import time
import json
import yaml
from pathlib import Path

from ec2_cluster.utils import humanize_float
from ec2_cluster.instances.instance import EC2Node
from ec2_cluster.config.config import ClusterConfig
from ec2_cluster.shells.control import ClusterShell








class ConfigCluster:
    """Class for defining an EC2NodeCluster in a config file

    Lightweight wrapper around EC2NodeCluster where you define the launch configuration at instantiation time instead
    of at runtime. Configuration values are loaded from a YAML file, with values optionally overwritten in `__init__`.

    Generally exposes the same API as EC2NodeCluster. The underlying EC2NodeCluster is always available via
    `self.cluster`.

    Args:
        config_yaml_path: Path to a yaml configuration file. None to load all params from `other_args`.
        other_args: Dictionary containing additional configuration values which will overwrite values from the
                    config file
    """
    def __init__(self, config_yaml_path=None, other_args=None):
        if other_args is None:
            other_args = {}


        # Pull in list of params
        param_list_yaml_path = Path(__file__).parent/"clusterdef_params.yaml"
        with open(param_list_yaml_path, 'r') as f:
            self.paramdef_list = yaml.safe_load(f)["params"]


        # Use yaml arguments as base if yaml file path was input
        if config_yaml_path is None:
            config_dict = {}
        else:
            with open(Path(config_yaml_path).absolute(), 'r') as yml:
                config_dict = yaml.safe_load(yml)


        # Add the other_args, overwriting the yaml arguments if the param is defined in both
        for param_name, param_val in other_args.items():
            config_dict[param_name] = param_val

        # Find the AZ from the subnet
        ec2_client = boto3.session.Session(region_name=config_dict["region"]).client("ec2")
        subnets = ec2_client.describe_subnets(SubnetIds=[config_dict["subnet_id"]])["Subnets"]
        assert len(subnets) == 1, "There should only be one or zero subnets with that id"
        config_dict["az"] = subnets[0]["AvailabilityZone"]

        # Validate then convert to AttrDict for dot notation member access
        self.validate_config_dict(config_dict)
        self._config = AttrDict(config_dict)


        # Define cluster name from params and instantiate EC2NodeCluster
        template_name = self.config.cluster_template_name
        node_count = self.config.node_count
        cluster_id = self.config.cluster_id
        self.cluster_name = f'{template_name}-{node_count}node-cluster{cluster_id}'
        self.cluster = EC2NodeCluster(node_count=node_count,
                                      cluster_name=self.cluster_name,
                                      region=self.config.region)


    def __enter__(self):
        """Creates a fresh cluster which will be automatically cleaned up.

        Will raise exception if the cluster already exists.
        """
        if self.any_node_is_running_or_pending():
            raise RuntimeError(f"Cluster with name '{self.cluster_name}' already exists.")

        self.launch(verbose=True)
        return self


    def __exit__(self, exc_type, exc_val, exc_tb):
        self.terminate(verbose=True, fast_terminate=True)

    @property
    def config(self):
        """Get config AttrDict"""
        return self._config


    def validate_config_dict(self, config_dict):
        """Validate that a given configuration is valid

        Args:
            config_dict: Dictionary of config values. Descriptions for each config field can be found in
                        `clusterdef_params.yaml`
        """
        # Some params are optional. Handle them separately later
        maybe_nonexistant_params = ["iops", 'ebs_optimized', 'additional_tags', 'placement_group']

        for p in self.paramdef_list:
            param_name = p["param_name"]
            if param_name in maybe_nonexistant_params:
                continue

            assert param_name in config_dict.keys(), f'Mandatory argument {param_name} is missing'
            assert config_dict[param_name] is not None, f'Mandatory argument {param_name} is None'


        # iops special case. Must be set when ebs_type=="io1"
        if config_dict["volume_type"] == "io1":
            assert "iops" in config_dict.keys(), f'When volume_type==io1, iops must be defined. Currently missing'
            assert config_dict["iops"] is not None, f'When volume_type==io1, iops must be defined. Currently None'
        else:
            config_dict["iops"] = None

        # ebs_optimized_instances special case. Defaults to True
        if "ebs_optimized" not in config_dict.keys() or config_dict["ebs_optimized"] is None:
            config_dict["ebs_optimized"] = True

        # additional_tags special case. Defaults to None as expected by EC2NodeCluster
        if "additional_tags" not in config_dict.keys() or config_dict["additional_tags"] is None:
            config_dict["additional_tags"] = None

        # placement_group special case. Defaults to False
        if "placement_group" not in config_dict.keys() or config_dict["placement_group"] is None:
            config_dict["placement_group"] = False



    @property
    def instance_ids(self):
        """A list of InstanceIds for the nodes in the cluster.

        All nodes must be in RUNNING or PENDING stats. Always in the same order: [Master, Worker1, Worker2, etc...]
        """
        return self.cluster.instance_ids

    @property
    def private_ips(self):
        """The list of private IPs for the nodes in the cluster.

        All nodes must be in RUNNING or PENDING stats. Output is always in the same order: [Master, Worker1, Worker2, etc...]
        """
        return self.cluster.private_ips

    @property
    def public_ips(self):
        """A list of public IPs for the nodes in the cluster.

        All nodes must be in RUNNING or PENDING stats. Output is always in the same order: [Master, Worker1, Worker2, etc...]
        """
        return self.cluster.public_ips

    @property
    def cluster_sg_id(self):
        """Return the Id of the ClusterSecurityGroup

        When cluster is launched, a security group is created to allow the nodes to communicate with each other. This
        is deleted when the cluster is terminated.

        Raise exception if the ClusterSecurityGroup doesn't exist.
        """
        return self.cluster.cluster_sg_id


    def any_node_is_running_or_pending(self):
        """Return True if any node is in RUNNING or PENDING states"""
        return self.cluster.any_node_is_running_or_pending()


    def wait_for_all_nodes_to_be_running(self):
        """Blocks until all nodes are in the RUNNING state"""
        return self.cluster.wait_for_all_nodes_to_be_running()

    def wait_for_all_nodes_to_be_status_ok(self):
        """Blocks until all nodes have passed the EC2 health check.

        Once nodes are status OK, you can SSH to them. See EC2Node.wait_for_instance_to_be_status_ok() for details.
        """
        return self.cluster.wait_for_all_nodes_to_be_status_ok()

    def wait_for_all_nodes_to_be_terminated(self):
        """Blocks until all nodes are in the TERMINATED state"""
        return self.cluster.wait_for_all_nodes_to_be_terminated()

    def launch(self, verbose=False):
        """Launch the cluster nodes using the config set in `__init__`

        Will repeatedly try to launch instances until all nodes are launched or the timeout is reached.

        Args:
             verbose: True to print out detailed information about progress
        """

        self.cluster.launch(az=self.config.az,
                            vpc_id=self.config.vpc_id,
                            subnet_id=self.config.subnet_id,
                            ami_id=self.config.ami_id,
                            ebs_snapshot_id=self.config.ebs_snapshot_id,
                            volume_gbs=self.config.volume_gbs,
                            volume_type=self.config.volume_type,
                            key_name=self.config.key_name,
                            security_group_ids=self.config.security_group_ids,
                            iam_ec2_role_name=self.config.iam_ec2_role_name,
                            instance_type=self.config.instance_type,
                            use_placement_group=self.config.placement_group,
                            iops=self.config.iops,
                            ebs_optimized=self.config.ebs_optimized,
                            tags=self.config.additional_tags,
                            timeout_secs=self.config.cluster_create_timeout_secs,
                            verbose=verbose)


    def terminate(self, verbose=False, fast_terminate=False):
        """Terminate all nodes in the cluster and clean up security group and placement group

        Args:
            verbose: True to print out detailed information about progress.
            fast_terminate: If True, will not wait for the nodes to reach the TERMINATED state, instead returning as
                            soon as the node termination has been triggered. NOTE: If this mode is chosen and the nodes
                            were launched into a placement group, the placement group will not be deleted (a placement
                            group is a logical EC2 resource that has no cost associated with it)
        """
        self.cluster.terminate(verbose=verbose, fast_terminate=fast_terminate)

    @property
    def ips(self):
        """Get all public and private IPs for nodes in the cluster

        All nodes must be in RUNNING or PENDING stats.

        Returns:
        ::
            {
                "master_public_ip": MasterPublicIp,
                "worker_public_ips": [Worker1PublicIp, Worker2PublicIp, etc...]
                "master_private_ip": MasterPrivateIp,
                "worker_private_ips": [Worker1PrivateIp, Worker2PrivateIp, etc...]
            }
        """
        if not self.any_node_is_running_or_pending():
            raise RuntimeError("Cluster does not exist. Cannot list ips of cluster that does not exist")

        return {
            "master_public_ip": self.cluster.public_ips[0],
            "worker_public_ips": self.cluster.public_ips[1:],
            "master_private_ip": self.cluster.private_ips[0],
            "worker_private_ips": self.cluster.private_ips[1:]
        }


    def get_shell(self, ssh_key_path=None, use_bastion=False, use_public_ips=True,
                  wait_for_ssh=True, wait_for_ssh_timeout=120):
        """
        Create a ClusterShell from a ConfigCluster.

        :param ssh_key_path: The path to the SSH key required to SSH into the EC2 instances. Often ~/.ssh/something.pem.
                             If param is None, will assume that the key is available at ~/.ssh/${KEY_PAIR_NAME}.pem
        :param use_bastion: Whether or not to use the master node as the bastion host for SSHing to worker nodes.
        :param use_public_ips: Whether to build the ClusterShell from the instances public IPs or private IPs.
                               Typically this should be True when running code on a laptop/local machine and False
                               when running on an EC2 instance
        :param wait_for_ssh: If true, block until commands can be run on all instances. This can be useful when
                             you are launching EC2 instances, because the instances may be in the RUNNING state
                             but the SSH daemon may not yet be running.
        :param wait_for_ssh_timeout: Number of seconds to spend trying to run commands on the instances before failing.
                                     This is NOT the SSH timeout, this upper bounds the amount of time spent retrying
                                     failed SSH connections. Only used if wait_for_ssh=True.
        :return: ClusterShell
        """

        ips = self.public_ips if use_public_ips else self.private_ips

        if ssh_key_path is None:
            ssh_key_path = Path(f"~/.ssh/{self.config.key_name}.pem").expanduser()

        sh = ClusterShell(username=self.config.username,
                          master_ip=ips[0],
                          worker_ips=ips[1:],
                          ssh_key_path=ssh_key_path,
                          use_bastion=use_bastion,
                          wait_for_ssh=wait_for_ssh,
                          wait_for_ssh_timeout=wait_for_ssh_timeout)
        return sh






class EC2NodeCluster:
    """Class for managing a group of EC2 instances as a cluster.

    Layer on top of EC2Node. Allows you to work with instances as a group. For example, create and attach a security
    group that allows all the nodes to communicate or wait for all nodes to reach a certain state.

    In particular for distributed training on the largest GPU instances, there is not always enough capacity to launch
    a large cluster in one go. With EC2NodeCluster, you continue trying to add nodes to the cluster until the entire
    cluster has been created or until the user-set timeout is reached.

    Obviously, that can get expensive as you are paying for the nodes you do have while you wait for all the nodes to
    spawn, but if you need a cluster of a certain size, this is the easiest way to do that.

    EC2NodeCluster names the EC2Nodes based on the cluster_name. Each node gets a number from 1-N and that is postfixed
    to the cluster_name (e.g. 'MyCluster-Node1'). This ensures that the nodes have a definite order. Similar to EC2Node,
    each cluster should have a cluster_name unique in the region. In addition to EC2Node Name collisions, each
    EC2NodeCluster creates a new security group using the cluster_name that can be impacted by Name collisions. This
    is also true for placement groups if using them.

    """
    def __init__(self,
                 node_count,
                 cluster_name,
                 region,
                 always_verbose=False):
        """
        Args:
            node_count: Number of nodes in the cluster
            cluster_name: The unique name of the cluster.
            region: The AWS region
            always_verbose: True to force all EC2NodeCluster methods to run in verbose mode
        """

        self._always_verbose = always_verbose

        self.node_count = node_count
        self.region = region
        self.cluster_name = cluster_name
        self.node_names = [f'{self.cluster_name}-node{i+1}' for i in range(node_count)]
        self.cluster_sg_name = f'{self.cluster_name}-intracluster-ssh'
        self.cluster_placement_group_name = f'{self.cluster_name}-placement-group'  # Defined, but might not be used

        self.nodes = [EC2Node(node_name, self.region)
                      for node_name in self.node_names]

        self.session = boto3.session.Session(region_name=region)
        self.ec2_client = self.session.client("ec2")
        self.ec2_resource = self.session.resource("ec2")

        self._cluster_sg_id = None


    def _get_vlog(self, force_verbose=False, prefix=None):
        def vlog_fn_verbose(s):
            out = "" if prefix is None else f'[{prefix}] '
            out += s
            print(out)

        def vlog_fn_noop(s):
            pass

        vlog_fn = vlog_fn_verbose if self._always_verbose or force_verbose else vlog_fn_noop
        return vlog_fn

    @property
    def instance_ids(self):
        """A list of InstanceIds for the nodes in the cluster.

        All nodes must be in RUNNING or PENDING stats. Always in the same order: [Master, Worker1, Worker2, etc...]
        """
        if not self.any_node_is_running_or_pending():
            raise RuntimeError("No nodes are running for this cluster!")
        return [node.instance_id for node in self.nodes]


    @property
    def private_ips(self):
        """The list of private IPs for the nodes in the cluster.

        All nodes must be in RUNNING or PENDING stats. Output is always in the same order: [Master, Worker1, Worker2, etc...]
        """
        if not self.any_node_is_running_or_pending():
            raise RuntimeError("No nodes are running for this cluster!")
        return [node.private_ip for node in self.nodes]

    @property
    def public_ips(self):
        """A list of public IPs for the nodes in the cluster.

        All nodes must be in RUNNING or PENDING stats. Output is always in the same order: [Master, Worker1, Worker2, etc...]
        """
        if not self.any_node_is_running_or_pending():
            raise RuntimeError("No nodes are running for this cluster!")
        return [node.public_ip for node in self.nodes]


    @property
    def ips(self):
        """Get all public and private IPs for nodes in the cluster

        All nodes must be in RUNNING or PENDING stats.

        Returns:
        ::
            {
                "master_public_ip": MasterPublicIp,
                "worker_public_ips": [Worker1PublicIp, Worker2PublicIp, etc...]
                "master_private_ip": MasterPrivateIp,
                "worker_private_ips": [Worker1PrivateIp, Worker2PrivateIp, etc...]
            }
        """
        if not self.any_node_is_running_or_pending():
            raise RuntimeError("Cluster does not exist. Cannot list ips of cluster that does not exist")

        return {
            "master_public_ip": self.public_ips[0],
            "worker_public_ips": self.public_ips[1:],
            "master_private_ip": self.private_ips[0],
            "worker_private_ips": self.private_ips[1:]
        }

    @property
    def cluster_sg_id(self):
        """Return the Id of the ClusterSecurityGroup

        When cluster is launched, a security group is created to allow the nodes to communicate with each other. This
        is deleted when the cluster is terminated.

        Raise exception if the ClusterSecurityGroup doesn't exist.
        """
        if self._cluster_sg_id is None:
            if not self.security_group_exists(self.cluster_sg_name):
                raise RuntimeError(f'Cluster security group "{self.cluster_sg_name}" does not exist!')
            self._cluster_sg_id = self.get_security_group_id_from_name(self.cluster_sg_name)
        return self._cluster_sg_id


    def create_cluster_sg(self, vpc_id, verbose=False):
        """Create the ClusterSecurityGroup that allows nodes to communicate with each other.

        :param vpc_id: The Id of the VPC that the cluster is in.
        """

        response = self.ec2_client.create_security_group(
            Description=self.cluster_sg_name,
            GroupName=self.cluster_sg_name,
            VpcId=vpc_id,
        )
        self._cluster_sg_id = response['GroupId']

        while not self.security_group_exists(self.cluster_sg_name):
            time.sleep(1)

        sg = self.ec2_resource.SecurityGroup(self.cluster_sg_id)
        sg.authorize_ingress(SourceSecurityGroupName=self.cluster_sg_name)


    def delete_cluster_sg(self):
        """Create the ClusterSecurityGroup that allows nodes to communicate with each other.

        Args:
            vpc_id: The Id of the VPC that the cluster is in.
        """
        response = self.ec2_client.delete_security_group(
            GroupId=self.cluster_sg_id,
            GroupName=self.cluster_sg_name
        )

    def security_group_exists(self, sg_name):
        """Return True if the security group with the given name exists"""
        res = self.ec2_client.describe_security_groups(
            Filters=[
                {
                    'Name': 'group-name',
                    'Values': [
                        sg_name,
                    ]
                },
            ]
        )
        return len(res['SecurityGroups']) > 0

    def get_security_group_id_from_name(self, sg_name):
        """Get the security group id from the name"""
        res = self.ec2_client.describe_security_groups(
            Filters=[
                {
                    'Name': 'group-name',
                    'Values': [
                        sg_name,
                    ]
                },
            ]
        )
        return res['SecurityGroups'][0]['GroupId']



    def list_placement_groups(self):
        """List all placement groups

        Returns:
            placement_groups (list): List of ``{'GroupName': 'string', 'State': 'pending'|'available'|'deleting'|'deleted', 'Strategy': 'cluster'|'spread'}``
        """
        response = self.ec2_client.describe_placement_groups()
        return response["PlacementGroups"]

    def placement_group_exists(self):
        """Return True if cluster placement group exists"""
        return self.cluster_placement_group_name in [pg["GroupName"] for pg in self.list_placement_groups()]

    def create_placement_group_if_doesnt_exist(self):
        """Create the cluster placement group if it doesn't exist. Do nothing if already exists"""
        if not self.placement_group_exists():
            response = self.ec2_client.create_placement_group(
                GroupName=self.cluster_placement_group_name,
                Strategy='cluster'
            )

    def delete_placement_group(self):
        """Delete the cluster placement group"""
        if self.placement_group_exists():
            response = self.ec2_client.delete_placement_group(
                GroupName=self.cluster_placement_group_name
            )



    def any_node_is_running_or_pending(self):
        """Return True if any node is in RUNNING or PENDING states"""
        for ec2_node in self.nodes:
            if ec2_node.is_running_or_pending():
                return True
        return False

    def wait_for_all_nodes_to_be_running(self):
        """Blocks until all nodes are in the RUNNING state"""
        for ec2_node in self.nodes:
            ec2_node.wait_for_instance_to_be_running()

    def wait_for_all_nodes_to_be_status_ok(self):
        """Blocks until all nodes have passed the EC2 health check.

        Once nodes are status OK, you can SSH to them. See EC2Node.wait_for_instance_to_be_status_ok() for details.
        """
        for ec2_node in self.nodes:
            ec2_node.wait_for_instance_to_be_status_ok()

    def wait_for_all_nodes_to_be_terminated(self):
        """Blocks until all nodes are in the TERMINATED state"""
        for ec2_node in self.nodes:
            try:
                ec2_node.wait_for_instance_to_be_terminated()
            except Exception as ex:
                print("[wait_for_all_nodes_to_be_terminated] Some error while waiting for nodes to be terminated")
                print(f'[wait_for_all_nodes_to_be_terminated] {ex}')
                print("[wait_for_all_nodes_to_be_terminated] Assuming non-fatal error. Continuing")
                pass




    def launch(self,
               az,
               vpc_id,
               subnet_id,
               ami_id,
               ebs_snapshot_id,
               volume_gbs,
               volume_type,
               key_name,
               security_group_ids,
               iam_ec2_role_name,
               instance_type,
               use_placement_group=False,
               iops=None,
               eia_type=None,
               ebs_optimized=True,
               tags=None,
               dry_run=False,
               timeout_secs=None,
               wait_secs=10,
               verbose=True):
        """Launch the cluster nodes.

        Will repeatedly try to launch instances until all nodes are launched or the timeout is reached.

        Args:
            az: The az to launch the cluster in, e.g. 'us-east-1f'
            vpc_id: The id of the VPC to launch the cluster in, e.g. 'vpc-123456789'
            subnet_id: The id of the subnet to launch the cluster in, e.g. 'subnet-123456789'
            ami_id: The id AMI, e.g. 'ami-123456789'
            ebs_snapshot_id: The snapshot id of the EBS instance to attach, e.g. 'snapshot-123456789'
            volume_gbs: The size of the volume in GBs
            volume_type: The type of the EBS volume. If 'io1' must include iops argument
            key_name: The name of the EC2 KeyPair for SSHing into the instance
            security_group_ids: A list of security group ids to attach. Must be a non-empty list. The
                                ClusterSecurityGroup id will be added to this list
            iam_ec2_role_name: The name of the EC2 role. The name, not the ARN.
            instance_type: The API name of the instance type to launch, e.g. 'p3.16xlarge'
            use_placement_group: True to launch instances in a placement group
            iops: If volume_type == 'io1', the number of provisioned IOPS for the EBS volume.
            eia_type: Optional. The Elastic Inference Accelerator type, e.g. 'eia1.large'
            ebs_optimized: Whether to use an EBS optimized instance. Should basically always be True. Certain
                                    older instance types don't support EBS optimized instance or offer at a small fee.
            tags: List of custom tags to attach to the EC2 instance. List of dicts, each with a 'Key' and a 'Value'
                  field. Normal EC2 tag length restrictions apply. Key='Name' is reserved for EC2Node use.
            dry_run: True to make test EC2 API call that confirms syntax but doesn't actually launch the instance.
            timeout_secs: The maximum number of seconds to spend launching the cluster nodes before timing out. Pass in
                          None to never time out (None can be either the Python type or the string 'None')
            wait_secs: The number of seconds to wait before retrying launching a node.
            verbose: True to print out detailed information about progress.
        """
        if timeout_secs == 'None':
            timeout_secs = None

        vlog = self._get_vlog(verbose, 'EC2NodeCluster.launch')

        if self.any_node_is_running_or_pending():
            raise RuntimeError("Nodes with names matching this cluster already exist!")


        if self.security_group_exists(self.cluster_sg_name):
            vlog("Cluster security group already exists. No need to recreate")
        else:
            vlog("Creating cluster security group")
            self.create_cluster_sg(vpc_id, verbose=verbose)

        security_group_ids.append(self.cluster_sg_id)

        if use_placement_group:
            vlog("Creating placement group")
            self.create_placement_group_if_doesnt_exist()
        else:
            vlog("No placement group needed")

        start = time.time()
        for launch_ind, ec2_node in enumerate(self.nodes):
            while True:
                try:

                    ec2_node.launch(az,
                                    vpc_id,
                                    subnet_id,
                                    ami_id,
                                    ebs_snapshot_id,
                                    volume_gbs,
                                    volume_type,
                                    key_name,
                                    security_group_ids,
                                    iam_ec2_role_name,
                                    instance_type,
                                    placement_group_name=self.cluster_placement_group_name if use_placement_group else None,
                                    iops=iops,
                                    eia_type=eia_type,
                                    ebs_optimized=ebs_optimized,
                                    tags=tags,
                                    dry_run=dry_run)

                    vlog(f'Node {launch_ind+1} of {self.node_count} successfully launched')
                    break
                except Exception as e:
                    vlog(f'Error launching node: {str(e)}')
                    vlog(f'EC2NodeCluster.launch TODO: Only repeat when the error is insufficient capacity.')

                    if timeout_secs is not None and (time.time() - start) > timeout_secs:
                        vlog(f'Timed out trying to launch node #{launch_ind+1}. Max timeout of {timeout_secs} seconds reached')
                        vlog("Now trying to clean up partially launched cluster")
                        for terminate_ind, ec2_node_to_delete in enumerate(self.nodes):
                            try:
                                if terminate_ind >= launch_ind:
                                    break   # Don't try to shut down nodes that weren't launched.
                                vlog(f'Terminating node #{terminate_ind+1} of {self.node_count}')
                                ec2_node_to_delete.detach_security_group(self.cluster_sg_id)
                                ec2_node_to_delete.terminate()
                                vlog(f'Node #{terminate_ind+1} successfully terminated')
                            except:
                                vlog(f'Error terminating node #{terminate_ind+1}')
                                vlog(str(e))

                        vlog("Deleting cluster SG")
                        self.delete_cluster_sg()
                        vlog("Now waiting for all nodes to reach TERMINATED state. May take some time.")
                        self.wait_for_all_nodes_to_be_terminated()
                        vlog("All nodes have been terminated!")
                        raise RuntimeError(f'EC2NodeCluster failed to launch. Last error while launching node was: "{str(e)}"')
                    else:

                        vlog(f'Retrying launch of node #{launch_ind+1} in {wait_secs} seconds.')
                        if timeout_secs is None:
                            vlog(f'There is no timeout. Elapsed time trying to launch this node: {humanize_float(time.time() - start)} seconds')
                        else:
                            vlog(f'Will time out after {timeout_secs} seconds. Current elapsed time: {humanize_float(time.time() - start)} seconds')
                        time.sleep(wait_secs)

        vlog("Now waiting for all nodes to reach RUNNING state")
        self.wait_for_all_nodes_to_be_running()
        vlog("All nodes are running!")


    def terminate(self, verbose=False, fast_terminate=False):
        """Terminate all nodes in the cluster and clean up security group and placement group

        Args:
            verbose: True to print out detailed information about progress.
            fast_terminate: If True, will not wait for the nodes to be shut down, instead returning as soon as the node
                            termination has been triggered. NOTE: If this mode is chosen and the nodes were launched
                            into a placement group, the placement group will not be deleted (a placement group is a
                            logical EC2 resource that has no cost associated with it)
        """
        vlog = self._get_vlog(verbose, 'EC2NodeCluster.terminate')

        if not self.any_node_is_running_or_pending():
            vlog("No nodes exist to terminate")
        else:
            for i, ec2_node in enumerate(self.nodes):
                ec2_node.detach_security_group(self.cluster_sg_id)
                ec2_node.terminate()
                vlog(f'Node {i + 1} of {self.node_count} successfully triggered deletion')

        if self.security_group_exists(self.cluster_sg_name):
            self.delete_cluster_sg()
            vlog("Cluster SG deleted")
        else:
            vlog(f"Cluster SG ({self.cluster_sg_name}) does not exist")

        if fast_terminate:
            return

        vlog("Waiting for all nodes to reach terminated state")
        self.wait_for_all_nodes_to_be_terminated()


        if self.placement_group_exists():
            self.delete_placement_group()
            vlog("Placement group deleted!")


