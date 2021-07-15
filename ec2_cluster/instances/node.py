import boto3




class EC2Node:
    """A class managing a single EC2 instance.

        This is a class for working with individual instances as objects. It can be user-facing, but it is
        not the primary way to interact with instances.

        We are changing how tags work to make unintentional conflicts less frequent.
        - Name is an important tag for AWS console usability, but shouldn't be relied on for queries
        - One tag is cluster-specific that can be used to find all instances created by ec3
        - One set of tags is cluster-specific and can be used to query all instances in the cluster
        - One tag is instance specific

        A cluster is defined as a set of identical instances in the same location. They must have identical:
        - Region
        - VPC
        - KeyPair
        - AMI
        - Username (dependent on the AMI)
        - InstanceType
        - IAM role
        - Security group ids
        - Cluster Name
        Possibly the same, maybe not:
        - Subnet ID
        - EBS Snapshot ID
        Things that may change over time
        - Number of instances
        - Everything else requires a tear-down and recreation

        ## Tags

        managed-by: ec3
        ec3-cluster-name:
        ec3-node-name:
        Name:
        """



    def __init__(
            self,
            name,
            region,
            vpc,
            subnet,
            ami,
            instance_type,
            keypair,
            security_group_ids,
            iam_role_name,
            placement_group_name=None,
            volume_size_gb=200,
            volume_type='gp3',
            volume_iops=3000,
            # TODO: volume_throughput for gp3
            # eia_type=None,
            # ebs_optimized=True,
            # tags=None,
            # ebs_snapshot_id,
            always_verbose=False
    ):
        """Launch an instance.

            Raises exception if instance with the given Name is already RUNNING or PENDING.

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

        self.name = name
        self.region = region
        self.vpc = vpc
        self.subnet = subnet
        self.ami = ami
        self.instance_type = instance_type
        self.keypair = keypair
        self.security_group_ids = security_group_ids
        self.iam_role_name = iam_role_name
        self.placement_group_name = placement_group_name
        self.volume_size_gb = volume_size_gb
        self.volume_type = volume_type
        self.volume_iops = volume_iops
        self.ebs_optimized = True

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
               tags=None,
               dry_run=False):


        security_group_ids = self.security_group_ids
        subnet_id = self.subnet
        ami_id = self.ami
        volume_size_gb = self.volume_size_gb
        volume_type = self.volume_type
        key_name = self.keypair
        iam_ec2_role_name = self.iam_role_name
        instance_type = self.instance_type
        placement_group_name = self.placement_group_name
        iops = self.volume_iops
        ebs_optimized = self.ebs_optimized

        #############################################
        # Validate input
        #############################################

        assert isinstance(security_group_ids, list), "security_group_ids must be a nonempty list"
        assert len(security_group_ids) > 0, "security_group_ids must be a nonempty list"

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
        # placement_params = {"AvailabilityZone": az}
        placement_params = {}
        if placement_group_name is not None:
            placement_params["GroupName"] = placement_group_name

        # EIA
        # if eia_type is None:
        #     eia_param_list = []
        # else:
        #     eia_param_list = [{'Type': eia_type}]

        # Tags
        all_tags = [{'Key': 'Name', 'Value': self.name}]
        if tags:
            all_tags += tags

        # EBS
        ebs_params = {
            # 'SnapshotId': ebs_snapshot_id,
            'VolumeSize': volume_size_gb,
            'VolumeType': volume_type
        }

        if iops:
            ebs_params['Iops'] = iops

        iam_instance_profile = {}
        if iam_ec2_role_name is not None:
            iam_instance_profile['Name'] = iam_ec2_role_name


        ########################################################
        # Ensure there are never two nodes with the same name
        ########################################################

        if self.is_running_or_pending():
            raise RuntimeError(f'Instance with Name {self.name} already exists')


        #############################################
        # Make the API call
        #############################################

        response = self.ec2_client.run_instances(
            # BlockDeviceMappings=[
            #     {
            #         'DeviceName': "/dev/xvda",
            #         'Ebs': ebs_params,
            #     },
            # ],
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
            IamInstanceProfile=iam_instance_profile,
            # ElasticInferenceAccelerators=eia_param_list,
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


import json
import dataclasses
from typing import List


@dataclasses.dataclass
class CidrBlockAssociationSet:
    association_id: str
    cidr_block: str
    cidr_block_state: str

@dataclasses.dataclass
class VpcInfo:
    cidr_block: str
    dhcp_options_id: str
    state: str
    vpc_id: str
    owner_id: str
    instance_tenancy: str
    cidr_block_association_sets: List[CidrBlockAssociationSet]
    is_default: bool

    @staticmethod
    def from_vpc_description_json(vpc_description_json):
        cidr_block_associations = [
            CidrBlockAssociationSet(
                association_id=association["AssociationId"],
                cidr_block=association["CidrBlock"],
                cidr_block_state=association["CidrBlockState"]["State"]
            )
            for association
            in vpc_description_json["CidrBlockAssociationSet"]
        ]
        return VpcInfo(
            cidr_block=vpc_description_json["CidrBlock"],
            dhcp_options_id=vpc_description_json["DhcpOptionsId"],
            state=vpc_description_json["State"],
            vpc_id=vpc_description_json["VpcId"],
            owner_id=vpc_description_json["OwnerId"],
            instance_tenancy=vpc_description_json["InstanceTenancy"],
            is_default=vpc_description_json["IsDefault"],
            cidr_block_association_sets=cidr_block_associations
        )


def jprint(j):
    print(json.dumps(j, indent=4))


def get_default_vpc(region: str) -> VpcInfo:
    ec2_client = boto3.client('ec2', region_name=region)
    # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html#EC2.Client.describe_vpcs
    response = ec2_client.describe_vpcs(
        Filters=[
            {
                'Name': 'isDefault',
                'Values': [
                    'true',
                ]
            },
        ],
    )

    default_vpc = VpcInfo.from_vpc_description_json(response["Vpcs"][0])
    print(default_vpc)
    return default_vpc



@dataclasses.dataclass
class SubnetInfo:
    availability_zone: str
    availability_zone_id: str
    available_ip_address_count: int
    cidr_block: str
    default_for_az: bool
    map_public_ip_on_launch: bool
    map_customer_owned_id_on_launch: bool
    state: str
    subnet_id: str
    vpc_id: str
    owner_id: str
    assign_ipv6_address_on_creation: bool
    subnet_arn: str
    # Skipped (unused and requires additional class)
    # ipv6_cidr_block_association_set: List

    @staticmethod
    def from_subnet_description_json(j):
        return SubnetInfo(
            availability_zone=j["AvailabilityZone"],
            availability_zone_id=j["AvailabilityZoneId"],
            available_ip_address_count=j["AvailableIpAddressCount"],
            cidr_block=j["CidrBlock"],
            default_for_az=j["DefaultForAz"],
            map_public_ip_on_launch=j["MapPublicIpOnLaunch"],
            map_customer_owned_id_on_launch=j["MapCustomerOwnedIpOnLaunch"],
            state=j["State"],
            subnet_id=j["SubnetId"],
            vpc_id=j["VpcId"],
            owner_id=j["OwnerId"],
            assign_ipv6_address_on_creation=j["AssignIpv6AddressOnCreation"],
            subnet_arn=j["SubnetArn"],
        )

def get_default_subnets(region: str, vpc_id: str) -> List[SubnetInfo]:
    ec2_client = boto3.client('ec2', region_name=region)
    # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html#EC2.Client.describe_subnets
    response = ec2_client.describe_subnets(
        Filters=[
            {
                'Name': 'vpc-id',
                'Values': [
                    vpc_id,
                ]
            }
        ],
    )
    subnets = [SubnetInfo.from_subnet_description_json(subnet_description) for subnet_description in response["Subnets"]]
    subnets.sort(key=lambda x: x.availability_zone_id)
    return subnets

import enum
class ImageType(enum.Enum):
    Ubuntu = "Ubuntu"
    AmazonLinux = "Amazon Linux"

class ImageArchitecture(enum.Enum):
    i386 = "i386"
    x86_64 = "x86_64"
    arm64 = "arm64"


@dataclasses.dataclass
class BlockDeviceMapping:
    device_name: str
    ebs_delete_on_termination: bool
    ebs_snapshot_id: str
    ebs_volume_size: int
    ebs_volume_type: str
    ebs_encrypted: bool

@dataclasses.dataclass
class ImageInfo:
    architecture: str
    creation_date: str
    image_id: str
    image_location: str
    image_type: str
    public: bool
    owner_id: str
    platform_details: str
    usage_operations: str
    state: str
    block_device_mappings: List[BlockDeviceMapping]
    description: str
    ena_support: bool
    hypervisor: str
    image_owner_alias: str
    name: str
    root_device_name: str
    root_device_type: str
    sriov_net_support: str
    virtualization_type: str

    @staticmethod
    def from_image_description_json(j):
        block_device_mappings = [
            BlockDeviceMapping(
                device_name=block_device_mapping_json["DeviceName"],
                ebs_delete_on_termination=block_device_mapping_json["Ebs"]["DeleteOnTermination"],
                ebs_snapshot_id=block_device_mapping_json["Ebs"]["SnapshotId"],
                ebs_volume_size=block_device_mapping_json["Ebs"]["VolumeSize"],
                ebs_volume_type=block_device_mapping_json["Ebs"]["VolumeType"],
                ebs_encrypted=block_device_mapping_json["Ebs"]["Encrypted"],
            )
            for block_device_mapping_json in j["BlockDeviceMappings"]
        ]
        return ImageInfo(
            architecture=j["Architecture"],
            creation_date=j["CreationDate"],  # "2021-06-04T03:55:37.000Z",
            image_id=j["ImageId"],
            image_location=j["ImageLocation"],
            image_type=j["ImageType"],
            public=j["Public"],
            owner_id=j["OwnerId"],
            platform_details=j["PlatformDetails"],
            usage_operations=j["UsageOperation"],
            state=j["State"],
            block_device_mappings=block_device_mappings,
            description=j["Description"],
            ena_support=j["EnaSupport"],
            hypervisor=j["Hypervisor"],
            image_owner_alias=j["ImageOwnerAlias"],
            name=j["Name"],
            root_device_name=j["RootDeviceName"],
            root_device_type=j["RootDeviceType"],
            sriov_net_support=j["SriovNetSupport"],
            virtualization_type=j["VirtualizationType"]
        )

def get_default_ami(region: str, architecture: ImageArchitecture):
    ec2_client = boto3.client("ec2", region_name=region)

    filters = []
    if architecture == ImageArchitecture.x86_64:
        filters.append({
            'Name': 'description',
            # Several image variants created for each version. This one is what the EC2 launcher offers
            'Values': [f'Amazon Linux 2 AMI 2.0.* {architecture.value} HVM ebs']
        })
    elif architecture == ImageArchitecture.arm64:
        filters.append({
            'Name': 'description',
            # Several image variants created for each version. This one is what the EC2 launcher offers
            'Values': [f'Amazon Linux 2 LTS Arm64 AMI 2.0.* {architecture.value} HVM gp2']
        })

    filters.append({
        'Name': 'architecture',
        'Values': [architecture.value]
    })


    # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html#EC2.Client.describe_images
    response = ec2_client.describe_images(
        ExecutableUsers=[
            'all',
        ],
        Filters=filters,
        Owners=[
            'amazon',  # 'self' to get AMIs created by you
        ]
    )
    images = [ImageInfo.from_image_description_json(i) for i in response["Images"]]
    images.sort(reverse=True, key=lambda i: i.creation_date)
    most_recent_image = images[0]
    return most_recent_image



def main():
    REGION = "us-east-1"
    DEFAULT_INSTANCE_TYPE = "m5.large"
    KEYPAIR = "armand-personal-dev-us-east-1"
    SECURITY_GROUPS = ["sg-0b3ada0862ce7ef94"]
    IAM_ROLE_NAME = None  # "admin"

    vpc = get_default_vpc(REGION)
    subnets = get_default_subnets(REGION, vpc.vpc_id)

    for subnet in subnets:
        print(subnet.subnet_id, subnet.availability_zone, subnet.availability_zone_id, subnet.default_for_az)

    ami = get_default_ami(region=REGION, architecture=ImageArchitecture.x86_64)
    print(ami)

    node = EC2Node(
        name="armand-test-ec2node",
        region="us-east-1",
        vpc=vpc.vpc_id,
        subnet=subnets[0].subnet_id,
        ami=ami.image_id,
        instance_type=DEFAULT_INSTANCE_TYPE,
        keypair=KEYPAIR,
        security_group_ids=SECURITY_GROUPS,
        iam_role_name=IAM_ROLE_NAME,
        placement_group_name=None,
        volume_size_gb=200,
        volume_type='gp2',
        volume_iops=None,
        # eia_type=None,
        # ebs_optimized=True,
        # tags=None,
        # ebs_snapshot_id,
        always_verbose=False
    )
    print("Node launching")
    node.launch()

    print("Waiting for node to be OK")
    node.wait_for_instance_to_be_status_ok()

    print("Node terminating")
    node.terminate()

    print("Waiting for node to be terminated")
    node.wait_for_instance_to_be_terminated()


if __name__ == '__main__':
    main()