import boto3
import json



class EC2Node:
    """A class managing a single EC2 instance.

    Allows you to launch, describe and terminate an EC2 instance. Also has convenience methods for waiting for the
    instance to reach a certain state, e.g. wait for status OK, after which you can SSH to the instance.

    This class is designed for managing long-running jobs without an always-on control plane. In order to do this, each
    ``EC2Node``-managed instance in an AWS region has a unique Name (the value of the 'Name' tag). When you instantiate an
    ``EC2Node``, you pass in this Name, which allows the code to query the EC2 API to see if that instance already exists
    in EC2.

    This is generally an easy, intuitive way to keep track of which node is which across sessions. However, this means
    you have to careful with your node Names to ensure that there aren't accidental collisions, e.g. two teammates pick
    the Name *test-node* and they end up both trying to control the same EC2 instance.

    ``EC2Node`` expects that only one person is trying to control an instance at a time. Behavior is unknown when there
    are multiple EC2 instances with the same Name (that should never happen when using ``EC2Node``).

    ``EC2Node`` only queries the EC2 API for RUNNING or PENDING nodes. That that means nodes outside of those states are
    invisible.
        - ``EC2Node`` will not being able to wait for a node to be in TERMINATED state if you did not query the EC2 API
          for the InstanceId before it entered the SHUTTING-DOWN state.
        - ``EC2Node`` will completely ignore any STOPPED nodes. Can lead to duplicate Names if the STOPPED nodes are then
          started manually.
    """

    def __init__(self, name, region, always_verbose=False):
        """
        Args:
            name: The unique Name of the ``EC2Node``
            region: The AWS region
            always_verbose (bool): True if you want all ``EC2Node`` functions to run in verbose mode.
        """
        self.name = name
        self.region = region

        self.session = boto3.session.Session(region_name=region)
        self.ec2_client = self.session.client("ec2")
        self.ec2_resource = self.session.resource("ec2")

        # Instance information retrieved from EC2 API. Lazy loaded
        self._instance_info = None
        self._always_verbose = always_verbose




    # Retrieves info from AWS APIs
    def _lazy_load_instance_info(self):
        if not self._instance_info:
            instance_info = self.query_for_instance_info()
            if instance_info is None:
                raise RuntimeError("Could not find info for instance. Perhaps it is not in 'RUNNING' "
                                   "or 'PENDING' state?")
            self._instance_info = instance_info


    @property
    def instance_id(self):
        """The EC2 InstanceId.

        Retrieved by calling the EC2 API. Will use a cached response if it has already called the API. Instance must be
        in the RUNNING or PENDING states.
        """
        self._lazy_load_instance_info()
        return self._instance_info["InstanceId"]


    @property
    def private_ip(self):
        """The private IP of the instance.

        Retrieved by calling the EC2 API. Will use a cached response if it has already called the API. Instance must be
        in the RUNNING or PENDING states.
        """
        self._lazy_load_instance_info()
        return self._instance_info["PrivateIpAddress"]

    @property
    def public_ip(self):
        """The public IP of the instance.

        Retrieved by calling the EC2 API. Will use a cached response if it has already called the API. Instance must be
        in the RUNNING or PENDING states.

        Will return None if instance does not have a public IP.
        """
        self._lazy_load_instance_info()
        return self._instance_info["PublicIpAddress"] if "PublicIpAddress" in self._instance_info.keys() else None

    @property
    def security_groups(self):
        """The list of security groups attached to the instance.

        Retrieved by calling the EC2 API. Will use a cached response if it has already called the API. Instance must be
        in the RUNNING or PENDING states.

        Returns a list of security group ids.
        """

        self._lazy_load_instance_info()
        return [sg["GroupId"] for sg in self._instance_info["SecurityGroups"]]



    def detach_security_group(self, sg_id):
        """Remove a security group from the instance.

        Instance must be in the RUNNING or PENDING states. No effect, no exception if the security group is not already
        attached to the instance.
        """

        if not self.is_running_or_pending():
            raise RuntimeError("Cannot remove security group if the instance isn't running")

        new_sgs = [sg for sg in self.security_groups if sg != sg_id]
        self.ec2_client.modify_instance_attribute(InstanceId=self.instance_id, Groups=new_sgs)


    def query_for_instance_info(self):
        """Retrieve instance info for any EC2 node in the RUNNING or PENDING state that has the correct 'Name' tag.

        Returns None if no such instance exists. Otherwise returns information in the form returned by
        `describe_instances <https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html#EC2.Client.describe_instances>`_.
        Specifically, returns ``response["Reservations"][0]["Instances"][0]``
        """

        response = self.ec2_client.describe_instances(
            Filters=[
                {
                    'Name': 'tag:Name',
                    'Values': [
                        self.name,
                    ]
                },
                {
                    'Name': 'instance-state-name',
                    'Values': [
                        'running',
                        'pending'
                    ]
                },
            ]
        )

        exists = len(response["Reservations"]) > 0
        if not exists:
            return None

        instance_info = response['Reservations'][0]['Instances'][0]
        return instance_info


    def is_running_or_pending(self):
        """Check the EC2 API to see if the instance is in the RUNNING or PENDING states"""
        return self.is_in_state(['running', 'pending'])



    def is_in_state(self, states):
        """Call the EC2 API to see if the instance is in any of the given states.

        Args:
            states: The list of states. Options are: 'pending'|'running'|'shutting-down'|'terminated'|'stopping'|'stopped'.
                    Can be a string if only checking a single state
        Returns:
            bool: True if the instance exists and is in any of those states
        """
        if isinstance(states, str):
            states = [states]

        response = self.ec2_client.describe_instances(
                Filters=[
                    {
                        'Name': 'tag:Name',
                        'Values': [
                            self.name,
                        ]
                    },
                    {
                        'Name': 'instance-state-name',
                        'Values': states
                    },
                ]
        )

        exists = len(response["Reservations"]) > 0
        return exists


    def wait_for_instance_to_be_running(self):
        """Block until the the instance reaches the RUNNING state.

        Will raise exception if non-RUNNING terminal state is reached (e.g. the node is TERMINATED) or if it times out.
        Uses the default timeout, which as of 2019-02-11, was 600 seconds.
        """

        waiter = self.ec2_client.get_waiter('instance_running')
        waiter.wait(
            Filters=[
                {
                    'Name': 'tag:Name',
                    'Values': [
                        self.name,
                    ]
                }
            ]
        )


    def wait_for_instance_to_be_status_ok(self):
        """Block until the the instance reaches the OK status.

        Note: status is not the same as state. Status OK means the `health check
        <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/monitoring-system-instance-status-check.html>`_ that EC2
        does showed no issues.


        Status OK is important because it is an indicator that the instance is ready to receive SSH connections, which
        may not be true immediately after entering the RUNNING state, but prior to having Status OK.
        """
        waiter = self.ec2_client.get_waiter('instance_status_ok')
        waiter.wait(InstanceIds=[self.instance_id])

    def wait_for_instance_to_be_terminated(self):
        """Block until the the instance reaches the TERMINATED state.

        Will raise exception if it times out. Uses the default timeout, which as of 2019-02-11, was 600 seconds. May
        raise exception if non-TERMINATED terminal state is reached (e.g. the node is RUNNING). Haven't checked.
        """
        waiter = self.ec2_client.get_waiter('instance_terminated')
        waiter.wait(
            Filters=[
                {
                    'Name': 'instance-id',
                    'Values': [
                        self.instance_id,
                    ]
                }
            ]
        )





    def launch(self,
               az,
               vpc_id,
               subnet_id,
               ami_id,
               ebs_snapshot_id,
               volume_size_gb,
               volume_type,
               key_name,
               security_group_ids,
               iam_ec2_role_name,
               instance_type,
               placement_group_name=None,
               iops=None,
               eia_type=None,
               ebs_optimized=True,
               tags=None,
               dry_run=False):

        """Launch an instance.

        Raises exception is instance with the given Name is already RUNNING or PENDING.

        :param az: The availability zone, e.g. 'us-east-1f'
        :param vpc_id: The id of the VPC, e.g. 'vpc-123456789'
        :param subnet_id: The id of the subnet, e.g. 'subnet-123456789'
        :param ami_id: The id of the AMI, e.g. 'ami-123456789'
        :param ebs_snapshot_id: The id of the EBS snapshot, e.g. 'snapshot-123456789'. May not be required, unconfirmed.
        :param volume_size_gb: The size of the EBS volume in GBs.
        :param volume_type: The type of the EBS volume. If type is 'io1', must pass in iops argument
        :param key_name: The name of the EC2 KeyPair for SSHing into the instance
        :param security_group_ids: A list of security group ids to attach. Must be a non-empty list
        :param iam_ec2_role_name: The name of the EC2 role. The name, not the ARN.
        :param instance_type: The API name of the instance type to launch, e.g. 'p3.16xlarge'
        :param placement_group_name: Optional. The name of a placement group to launch the instance into.
        :param iops: If volume_type == 'io1', the number of provisioned IOPS for the EBS volume.
        :param eia_type: Optional. The Elastic Inference Accelerator type, e.g. 'eia1.large'
        :param ebs_optimized: Whether to use an EBS optimized instance. Should basically always be True. Certain older
                              instance types don't support EBS optimized instance or offer at a small fee.
        :param tags: List of custom tags to attach to the EC2 instance. List of dicts, each with a 'Key' and a 'Value'
                     field. Normal EC2 tag length restrictions apply. Key='Name' is reserved for EC2Node use.
        :param dry_run: True to make test EC2 API call that confirms syntax but doesn't actually launch the instance.
        :return: EC2 API response in format return by `run_instances <https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html#EC2.Client.run_instances>`_
        """

        #############################################
        # Validate input
        #############################################

        assert isinstance(security_group_ids, list), "security_group_ids must be a nonempty list"
        assert len(security_group_ids) > 0, "security_group_ids must be a nonempty list"

        if eia_type is not None:
            assert eia_type.startswith("eia1"), "eia_type must be in the form `eia1.large`"

        if volume_type == 'io1':
            assert iops is not None

        if tags:
            assert isinstance(tags, list), "Tags must be a list if not None"
            assert len(tags) != 0, "Tags cannot be an empty list. Use None instead"
            for tag in tags:
                assert isinstance(tag, dict), "Elements in tags must be dicts"
                assert 'Key' in tag.keys(), "Each tag must have both a 'Key' and a 'Value' field. 'Key' missing"
                assert tag['Key'] != "Name", "'Name' tag cannot be included as a tag. It will be set according to " \
                                             "the Name defined at instantiation"
                assert 'Value' in tag.keys(), "Each tag must have both a 'Key' and a 'Value' field. 'Value' missing"



        #############################################
        # Convert input to match SDK argument syntax
        #############################################

        # Optional placement group
        placement_params = {"AvailabilityZone": az}
        if placement_group_name is not None:
            placement_params["GroupName"] = placement_group_name

        # EIA
        if eia_type is None:
            eia_param_list = []
        else:
            eia_param_list = [{'Type': eia_type}]

        # Tags
        all_tags = [{'Key': 'Name', 'Value': self.name}]
        if tags:
            all_tags += tags

        # EBS
        ebs_params = {
            'SnapshotId': ebs_snapshot_id,
            'VolumeSize': volume_size_gb,
            'VolumeType': volume_type
        }

        if iops:
            ebs_params['Iops'] = iops


        ########################################################
        # Ensure there are never two nodes with the same name
        ########################################################

        if self.is_running_or_pending():
            raise RuntimeError(f'Instance with Name {self.name} already exists')


        #############################################
        # Make the API call
        #############################################

        response = self.ec2_client.run_instances(
            BlockDeviceMappings=[
                {
                    'DeviceName': "/dev/xvda",
                    'Ebs': ebs_params,
                },
            ],
            ImageId=ami_id,
            InstanceType=instance_type,
            KeyName=key_name,
            MaxCount=1,
            MinCount=1,
            Monitoring={
                'Enabled': False
            },
            Placement=placement_params,
            SecurityGroupIds=security_group_ids,
            SubnetId=subnet_id,
            DryRun=dry_run,
            EbsOptimized=ebs_optimized,
            IamInstanceProfile={
                'Name': iam_ec2_role_name
            },
            ElasticInferenceAccelerators=eia_param_list,
            TagSpecifications=[
                {
                    'ResourceType': 'instance',
                    'Tags': all_tags
                },
            ]
        )
        return response

    def terminate(self, dry_run=False):
        """Terminate the instance.

        After triggering termination, removes the 'Name' tag from the instance, which allows you to immediately launch a
        new node with the same Name.

        Args:
            dry_run (bool): Make EC2 API call as a test.
        """
        instance_id = self.instance_id
        response = self.ec2_client.terminate_instances(
            InstanceIds=[
                instance_id,
            ],
            DryRun=dry_run
        )

        instance = self.ec2_resource.Instance(instance_id)

        instance.delete_tags(Tags=[{'Key': 'Name'}])





