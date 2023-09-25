import boto3
from botocore.config import Config
from ..config import get_one_of


def _config():
    region = get_one_of("backends.awsrds.region", "backends.aws.region", fail_if_missing=True)
    return Config(
        region_name = region,
    )


def aws_client_rds():
    return boto3.client("rds", config=_config())
