# Copyright 2020 Cortex Labs, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import yaml
import os
from copy import deepcopy
import collections


# kublet config schema: https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/kubelet/config/v1beta1/types.go
default_nodegroup = {
    "ami": "auto",
    "iam": {"withAddonPolicies": {"autoScaler": True}},
    "kubeletExtraConfig": {
        "kubeReserved": {"cpu": "150m", "memory": "300Mi", "ephemeral-storage": "1Gi"},
        "kubeReservedCgroup": "/kube-reserved",
        "systemReserved": {"cpu": "150m", "memory": "300Mi", "ephemeral-storage": "1Gi"},
        "evictionHard": {"memory.available": "200Mi", "nodefs.available": "5%"},
    },
}


def merge_override(a, b):
    "merges b into a"
    for key in b:
        if key in a:
            if isinstance(a[key], dict) and isinstance(b[key], dict):
                merge_override(a[key], b[key])
            else:
                a[key] = b[key]
        else:
            a[key] = b[key]
    return a


def apply_worker_settings(nodegroup):
    worker_settings = {
        "name": "ng-cortex-worker-on-demand",
        "labels": {"workload": "true"},
        "taints": {"workload": "true:NoSchedule"},
        "tags": {
            "k8s.io/cluster-autoscaler/enabled": "true",
            "k8s.io/cluster-autoscaler/node-template/label/workload": "true",
        },
    }

    return merge_override(nodegroup, worker_settings)


def apply_clusterconfig(nodegroup, config):
    if config["min_instances"] == 0:
        desired_capacity = 1
    else:
        desired_capacity = config["min_instances"]

    clusterconfig_settings = {
        "instanceType": config["instance_type"],
        "availabilityZones": config["availability_zones"],
        "volumeSize": config["instance_volume_size"],
        "minSize": config["min_instances"],
        "maxSize": config["max_instances"],
        "desiredCapacity": desired_capacity,
    }

    return merge_override(nodegroup, clusterconfig_settings)


def apply_spot_settings(nodegroup, config):
    spot_settings = {
        "name": "ng-cortex-worker-spot",
        "instanceType": "mixed",
        "instancesDistribution": {
            "instanceTypes": config["spot_config"]["instance_distribution"],
            "onDemandBaseCapacity": config["spot_config"]["on_demand_base_capacity"],
            "onDemandPercentageAboveBaseCapacity": config["spot_config"][
                "on_demand_percentage_above_base_capacity"
            ],
            "maxPrice": config["spot_config"]["max_price"],
            "spotInstancePools": config["spot_config"]["instance_pools"],
        },
        "labels": {"lifecycle": "Ec2Spot"},
    }

    return merge_override(nodegroup, spot_settings)


def apply_gpu_settings(nodegroup):
    gpu_settings = {
        "tags": {
            "k8s.io/cluster-autoscaler/node-template/label/nvidia.com/gpu": "true",
            "k8s.io/cluster-autoscaler/node-template/taint/dedicated": "nvidia.com/gpu=true",
        },
        "labels": {"nvidia.com/gpu": "true"},
        "taints": {"nvidia.com/gpu": "true:NoSchedule"},
    }

    return merge_override(nodegroup, gpu_settings)


def is_gpu(instance_type):
    return instance_type.startswith("g") or instance_type.startswith("p")


def apply_asic_settings(nodegroup, instance_type):
    no_chips, hugepages_mem = get_asic_resources(instance_type)
    asic_settings = {
        # custom eks-optimized AMI for inf instances
        # TODO track https://github.com/aws/containers-roadmap/issues/619 ticket
        # such that when an EKS-optimized AMI for inf instances is released,
        # this AMI override can be removed and reverted back to auto
        "ami": "ami-07a7b48058cfe1a73",
        "tags": {
            "k8s.io/cluster-autoscaler/node-template/label/aws.amazon.com/infa": "true",
            "k8s.io/cluster-autoscaler/node-template/taint/dedicated": "aws.amazon.com/infa=true",
            "k8s.io/cluster-autoscaler/node-template/resources/aws.amazon.com/infa": str(no_chips),
            "k8s.io/cluster-autoscaler/node-template/resources/hugepages-2Mi": hugepages_mem,
        },
        "labels": {"aws.amazon.com/infa": "true"},
        "taints": {"aws.amazon.com/infa": "true:NoSchedule"},
    }
    return merge_override(nodegroup, asic_settings)


def is_asic(instance_type):
    return instance_type.startswith("inf")


def get_asic_resources(instance_type):
    no_hugepages_2Mi = 128
    hugepages_mem = lambda no_chips: f"{no_hugepages_2Mi * no_chips}Mi"

    no_chips = 0
    if instance_type in ["inf1.xlarge", "inf1.2xlarge"]:
        no_chips = 1
    elif instance_type == "inf1.6xlarge":
        no_chips = 4
    elif instance_type == "inf1.24xlarge":
        no_chips = 16

    return no_chips, hugepages_mem(no_chips)


def generate_eks(configmap_yaml_path):
    with open(configmap_yaml_path, "r") as f:
        cluster_configmap = yaml.safe_load(f)

    operator_nodegroup = deepcopy(default_nodegroup)
    operator_settings = {
        "name": "ng-cortex-operator",
        "instanceType": "t3.medium",
        "availabilityZones": cluster_configmap["availability_zones"],
        "minSize": 1,
        "maxSize": 1,
        "desiredCapacity": 1,
    }
    operator_nodegroup = merge_override(operator_nodegroup, operator_settings)

    worker_nodegroup = deepcopy(default_nodegroup)
    apply_worker_settings(worker_nodegroup)

    apply_clusterconfig(worker_nodegroup, cluster_configmap)

    if cluster_configmap["spot"]:
        apply_spot_settings(worker_nodegroup, cluster_configmap)

    if is_gpu(cluster_configmap["instance_type"]):
        apply_gpu_settings(worker_nodegroup)

    if is_asic(cluster_configmap["instance_type"]):
        apply_asic_settings(worker_nodegroup, instance_type=cluster_configmap["instance_type"])

    eks = {
        "apiVersion": "eksctl.io/v1alpha5",
        "kind": "ClusterConfig",
        "metadata": {
            "name": cluster_configmap["cluster_name"],
            "region": cluster_configmap["region"],
            "version": "1.16",
        },
        "vpc": {"nat": {"gateway": "Disable"}},
        "availabilityZones": cluster_configmap["availability_zones"],
        "cloudWatch": {"clusterLogging": {"enableTypes": ["*"]}},
        "nodeGroups": [operator_nodegroup, worker_nodegroup],
    }

    if cluster_configmap.get("spot_config") is not None and cluster_configmap["spot_config"].get(
        "on_demand_backup", False
    ):
        backup_nodegroup = deepcopy(default_nodegroup)
        apply_worker_settings(backup_nodegroup)
        apply_clusterconfig(backup_nodegroup, cluster_configmap)
        if is_gpu(cluster_configmap["instance_type"]):
            apply_gpu_settings(backup_nodegroup)
        if is_asic(cluster_configmap["instance_type"]):
            apply_asic_settings(backup_nodegroup, cluster_configmap["instance_type"])

        backup_nodegroup["minSize"] = 0
        backup_nodegroup["desiredCapacity"] = 0

        eks["nodeGroups"].append(backup_nodegroup)

    print(yaml.dump(eks, Dumper=IgnoreAliases, default_flow_style=False, default_style=""))


class IgnoreAliases(yaml.Dumper):
    """By default, yaml dumper tries to compress yaml by annotating collections (lists and maps)
    and replacing subsequent identical collections with aliases. This class overrides the default
    behaviour to preserve the duplication of arrays.
    """

    def ignore_aliases(self, data):
        return True


if __name__ == "__main__":
    generate_eks(configmap_yaml_path=sys.argv[1])
