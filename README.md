# ec2-cluster

Quickly spin up EC2 instances, run commands/scripts on them, and then (optionally) automatically shut them down. Minus all the boilerplate of `boto3` and manual SSH/`pdsh` commands.

```python
with ec3.Cluster("ec3_config.yaml", instances=5) as cluster:
    cluster.upload("perf_test.py")  # copy local file to every instance
    results = cluster.run("python perf_test.py")
    hostnames = [result.stdout for result in results]

# Cluster is torn down when context is exited
```

`ec3` can also be used without a context manager and then the instances won't be shut down until shutdown is manually triggered. This can be useful for long-running jobs.

\
## Quickstart

This code will launch a cluster of EC2 instances, run the command `hostname` on all of them, return the results of the command, and then tear down the cluster.

```python
import ec2_cluster as ec3

with ec3.Cluster("cluster.yaml") as cluster:
    results = cluster.run("hostname")
    hostnames = [result.stdout for result in results]

# Cluster is torn down when context is exited
```

The `cluster.yaml` 

## Long-running tasks

`ec2-cluster` is also designed for long-running tasks where you may not want to keep your local machine awake for the full duration. The library relies on EC2 tags to keep track of EC2 instances, letting you interact with a cluster across sessions without needing an always-on control plane.

Below is one way to launch complicated, long-running jobs and download the results at some later time. `check_on_job()` can be run in a different session or even a different machine than `launch_job()` as long as they have the same `cluster.yaml` and are using the same AWS account.

```python
import ec2_cluster as ec3

def launch_job():
    cluster = ec3.infra.ConfigCluster("cluster.yaml")
    cluster.launch(verbose=True)
    sh = cluster.get_shell()
    sh.copy_from_local_to_all("job_script.py", "job_script.py")
    sh.run_on_all("python job_script.py > job.log 2>&1 &")  # Launch script as background process


def check_on_job():
    cluster = ec3.infra.ConfigCluster("cluster.yaml")
    sh = cluster.get_shell()
    statuses = sh.run_on_all("tail -n 1 job.log", hide=True)
    for status in statuses:
        if status.stdout.rstrip("\n") != "job_script.py complete":
            print("Job is not yet complete on all instances")
            return
    
    print("Job is finally done on all instances!")
     
    sh.copy_from_all_to_local("job.log", "./results/")
    cluster.terminate(verbose=True)
```
This will create a directory on your local machine:
```
results/
├── 0
│   ├── ip.txt
│   └── job.log
├── 1
│   ├── ip.txt
│   └── job.log
└── 3
    ├── ip.txt
    └── job.log
```




## Overview


`ec2-cluster` is designed for simple distributed tasks where Kubernetes is overkill. There is no setup required other than the ability to launch EC2 instances with `boto3` and the ability to SSH to those instances (only a requirement if you want to run commands on them). 

`ec2-cluster` provides the ability to launch a cluster, to retrieve IP addresses for all nodes/nodes in the cluster, to delete the cluster and to execute commands on some or all of the instances. 

Unlike like most cluster management tools, all cluster management in `ec2-cluster` happens client-side. `ec2-cluster` creates EC2 instances and gives them globally unique names (using the EC2 Name tag) which is later used to identify which instances are part of a given cluster. `ec2-cluster` then provides convenience classes to run commands on some or all of the instances via SSH (using the [`fabric`](https://github.com/fabric/fabric) library).

## Other benefits

- Resilient to EC2 capacity limits. If instances are not available, `ec2-cluster` will retry until the all nodes in the cluster are created or until the user-set timeout is reached.
- Easy to quickly launch duplicate clusters for parallel training runs.
- Can write orchestration logic that needs to be run when launching a cluster, e.g. enabling passwordless ssh between all instances for Horovod-based training
- Iterative, not disruptive, improvements on the common methodology of manually launching EC2 instances, ssh-ing to them, configuring environments by hand and running scripts






### Usage

`ec2-cluster` can be consumed in two ways:

- A CLI for launching, describing and deleting clusters. 
- A python library for scripting.  

This library has three main components:L
- **infra**: creating cluster infrastructure
- **orch**: orchestrating simple runtime cluster configuration (e.g. generate a hostfile with runtime IPs)
- **control**: running commands on the cluster

### CLI Quick Start

### Library Quick Start





