import time
import kopf
from .aws_base import AwsBackendBase, calculate_maintenance_window
from ..config import get_one_of, config_get
from ..util.reconcile_helpers import field_from_spec


def _backend_config(key, default=None, fail_if_missing=False):
    return get_one_of(f"backends.awsrds.{key}", f"backends.aws.{key}", default=default, fail_if_missing=fail_if_missing)


def _calc_name(namespace, name):
    # Allow admins to override names so that existing servers not following the schema can still be managed
    name_overrides = _backend_config("name_overrides", default=[])
    for override in name_overrides:
        if override["namespace"] == namespace and override["name"] == name:
            return override["aws_identifier"]
    return _backend_config("name_pattern", "{namespace}-{name}").format(namespace=namespace, name=name)


class AwsRdsBackend(AwsBackendBase):

    def server_spec_valid(self, namespace, name, spec):
        server_name = _calc_name(namespace, name)
        if len(server_name) > 63:
            return (False, f"calculated server name '{server_name}' is longer than 63 characters")
        size = spec.get("size", dict())
        if size.get("storageGB", 20) < 20:
            return (False, f"size.storageGB must be at least 20 GB")
        return (True, "")

    def _get_server(self, namespace, name):
        server_name = _calc_name(namespace, name)
        try:
            result = self._rds_client.describe_db_instances(DBInstanceIdentifier=server_name)
            if not result or "DBInstances" not in result or len(result["DBInstances"]) == 0:
                return None
            return result["DBInstances"][0]
        except:
            return None

    def server_exists(self, namespace, name):
        return self._get_server(namespace, name) is not None

    def create_or_update_server(self, namespace, name, spec, password, admin_password_changed=False):
        server_name = _calc_name(namespace, name)
        instance_class, storage_type, iops = _determine_instance_class(spec.get("size", {}))
        storage_gb = field_from_spec(spec, "size.storageGB", default=20)
        admin_username = _backend_config("admin_username", default="postgres")
        highavailability = field_from_spec(spec, "highavailability.enabled", default=False)
        tags = [{"Key": "hybridcloud-postgresql-operator:namespace", "Value": namespace}, {"Key": "hybridcloud-postgresql-operator:name", "Value": name}]
        for k, v in _backend_config("tags", default={}).items():
            tags.append({"Key": k, "Value": v.format(namespace=namespace, name=name)})

        existing_server = self._get_server(namespace, name)

        # Add optional fields based on configuration
        args = {}
        if iops:
            args["Iops"] = iops
        maintenance_window = calculate_maintenance_window(spec)
        if maintenance_window:
            args["PreferredMaintenanceWindow"] = maintenance_window

        if not existing_server:
            if not highavailability:
                args["AvailabilityZone"] = _backend_config("availability_zone", "eu-central-1a")
            response = self._rds_client.create_db_instance(
                DBInstanceIdentifier=server_name,
                AllocatedStorage=storage_gb,
                DBInstanceClass=instance_class,
                Engine='postgres',
                MasterUsername=admin_username,
                MasterUserPassword=password,
                VpcSecurityGroupIds=_backend_config("vpc_security_group_ids", default=[]),
                DBSubnetGroupName=_backend_config("subnet_group", fail_if_missing=True),
                BackupRetentionPeriod=field_from_spec(spec, "backup.retentionDays", default=7),
                Port=5432,
                MultiAZ=highavailability,
                EngineVersion=_map_version(spec.get("version")),
                AutoMinorVersionUpgrade=True,
                PubliclyAccessible=_backend_config("network.public_access", default=False),
                Tags=tags,
                StorageType=storage_type,
                StorageEncrypted=True,
                CopyTagsToSnapshot=True,
                DeletionProtection=_backend_config("deletion_protection", default=False),
                **args
            )
        else:
            if existing_server.get("DBInstanceStatus") != "available":
                self._logger.info("DB instance is not available. Cannot perform update")
                raise kopf.TemporaryError("Waiting for instance to be available", delay=20)
            if admin_password_changed:
                args["MasterUserPassword"] = password
            response = self._rds_client.modify_db_instance(
                DBInstanceIdentifier=server_name,
                AllocatedStorage=storage_gb,
                DBInstanceClass=instance_class,
                VpcSecurityGroupIds=_backend_config("vpc_security_group_ids", default=[]),
                ApplyImmediately=True,
                BackupRetentionPeriod=field_from_spec(spec, "backup.retentionDays", default=7),
                MultiAZ=highavailability,
                EngineVersion=_map_version(spec.get("version")),
                AllowMajorVersionUpgrade=True,
                AutoMinorVersionUpgrade=True,
                PubliclyAccessible=_backend_config("network.public_access", default=False),
                DeletionProtection=_backend_config("deletion_protection", default=False),
                **args
            )

        # Wait for endpoint to be available
        wait_time = 0
        self._logger.info("Waiting for server to be available")
        response = field_from_spec(response, "DBInstance")
        while not field_from_spec(response, "Endpoint.Address"):
            if wait_time > 10*60:
                raise kopf.TemporaryError("Timed out waiting for DB Instance to be available", delay=30)
            time.sleep(10)
            wait_time += 10
            response = self._get_server(namespace, name)

        # Prepare credentials
        data = {
            "username": admin_username,
            "password": password,
            "dbname": "postgres",
            "host": response['Endpoint']['Address'],
            "port": "5432",
            "sslmode": "require"
        }
        return data, []

    def delete_server(self, namespace, name):
        server_name = _calc_name(namespace, name)
        self._rds_client.delete_db_instance(
            DBInstanceIdentifier=server_name,
            SkipFinalSnapshot=True,
            DeleteAutomatedBackups=False # Keep backups around just in case
        )

    def create_or_update_database(self, namespace, server_name, database_name, spec, admin_credentials=None):
        pgclient = self._pgclient(admin_credentials)
        pgclient.create_database(database_name)
        pgclient.restrict_database_permissions(database_name)
        extensions = field_from_spec(spec, "database.extensions", default=[])
        if not extensions:
            return
        for extension in extensions:
            self._logger.info(f"Enabling extension {extension}")
            pgclient.create_extension(extension)
        return True


def _map_version(version: str):
    if not version:
        return "15.3"
    if "." in version:
        return version
    elif version.startswith("11"):
        return "11.20"
    elif version.startswith("12"):
        return "12.15"
    elif version.startswith("13"):
        return "13.11"
    elif version.startswith("14"):
        return "14.8"
    else:
        return "15.3"


def _determine_instance_class(size_spec):
    warnings = []
    size_class = size_spec.get("class")
    default_class = config_get("backends.awsrds.default_class", default="operator_default")
    if not size_class:
        warnings.append(f"Directly specifying CPU and memory is currently not supported. Falling back to default class {default_class}")
    classes = config_get("backends.awsrds.classes", default=[])
    if not default_class in classes:
        classes[default_class] = {"instance_type": "db.m5.large"}
    if not size_class in classes:
        warnings.append(f"selected class {size_class} is not allowed. Falling back to default {default_class}")
        size_class = default_class
    selected_class = classes[size_class]
    return selected_class["instance_type"], selected_class.get("storage_type", "gp2"), selected_class.get("iops") 
