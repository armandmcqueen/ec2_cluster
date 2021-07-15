import yaml
import boto3
import os

def humanize_float(num):
    return "{0:,.2f}".format(num)


class AttrDict(dict):
    """
    Class for working with dicts using dot notation
    """
    def __init__(self, *args, **kwargs):
        super(AttrDict, self).__init__(*args, **kwargs)
        self.__dict__ = self

    def __str__(self):
        return json.dumps(self.__dict__, indent=4)




def get_config_params():
    path_to_containing_dir = os.path.dirname(os.path.realpath(__file__))
    param_list_yaml_abspath = os.path.join(path_to_containing_dir, "clusterdef_params.yaml")
    config_param_list = yaml.safe_load(open(param_list_yaml_abspath, 'r'))["params"]
    return config_param_list


