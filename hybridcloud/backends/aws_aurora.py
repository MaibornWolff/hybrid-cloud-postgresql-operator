import time
import kopf
from .aws_base import AwsBackendBase, calculate_maintenance_window
from ..config import get_one_of, config_get
from ..util.reconcile_helpers import field_from_spec


def _backend_config(key, default=None, fail_if_missing=False):
    return get_one_of(f"backends.awsaurora.{key}", f"backends.aws.{key}", default=default, fail_if_missing=fail_if_missing)


def _calc_name(namespace, name):
    # Allow admins to override names so that existing servers not following the schema can still be managed
    name_overrides = _backend_config("name_overrides", default=[])
    for override in name_overrides:
        if override["namespace"] == namespace and override["name"] == name:
            return override["aws_identifier"]
    return _backend_config("name_pattern", "{namespace}-{name}").format(namespace=namespace, name=name)


class AwsAuroraBackend(AwsBackendBase):

    def server_spec_valid(self, namespace, name, spec):
        server_name = _calc_name(namespace, name)
        if len(server_name) > 63:
            return (False, f"calculated server name '{server_name}' is longer than 63 characters")
        size = spec.get("size", dict())
        if size.get("storageGB", 20) < 20:
            return (False, f"size.storageGB must be at least 20 GB")
        return (True, "")

    def _get_cluster(self, namespace, name):
        server_name = _calc_name(namespace, name)
        try:
            result = self._rds_client.describe_db_clusters(DBClusterIdentifier=server_name)
            if not result or "DBClusters" not in result or len(result["DBClusters"]) == 0:
                return None
            return result["DBClusters"][0]
        except:
            return None

    def _get_server(self, namespace, name, subname):
        cluster_name = _calc_name(namespace, name)
        server_name = f"{cluster_name}-{subname}"
        try:
            result = self._rds_client.describe_db_instances(DBInstanceIdentifier=server_name)
            if not result or "DBInstances" not in result or len(result["DBInstances"]) == 0:
                return None
            return result["DBInstances"][0]
        except:
            return None

    def server_exists(self, namespace, name):
        return self._get_cluster(namespace, name) is not None

    def create_or_update_server(self, namespace, name, spec, password, admin_password_changed=False):
        cluster_name = _calc_name(namespace, name)
        instance_class, scaling_configuration, storage_type, iops, warnings = _determine_instance_class(spec.get("size", {}))
        admin_username = _backend_config("admin_username", default="postgres")
        version = _map_version(spec.get("version"))
        tags = [{"Key": "hybridcloud-postgresql-operator:namespace", "Value": namespace}, {"Key": "hybridcloud-postgresql-operator:name", "Value": name}]
        for k, v in _backend_config("tags", default={}).items():
            tags.append({"Key": k, "Value": v.format(namespace=namespace, name=name)})

        existing_cluster = self._get_cluster(namespace, name)
        existing_primary_instance = self._get_server(namespace, name, "primary")
        # Add optional fields based on configuration
        args = {}
        if iops:
            args["Iops"] = iops
        maintenance_window = calculate_maintenance_window(spec)
        if maintenance_window:
            args["PreferredMaintenanceWindow"] = maintenance_window
        if scaling_configuration:
            args["ServerlessV2ScalingConfiguration"] = scaling_configuration

        if not existing_cluster:
            response = self._rds_client.create_db_cluster(
                DBClusterIdentifier=cluster_name,
                AvailabilityZones=_backend_config("availability_zones", default=[]),
                BackupRetentionPeriod=field_from_spec(spec, "backup.retentionDays", default=1),
                DatabaseName="postgres",
                VpcSecurityGroupIds=_backend_config("vpc_security_group_ids", default=[]),
                DBSubnetGroupName=_backend_config("subnet_group", fail_if_missing=True),
                Engine="aurora-postgresql",
                EngineVersion=version,
                Port=5432,
                MasterUsername=admin_username,
                MasterUserPassword=password,
                Tags=tags,
                StorageEncrypted=True,
                EngineMode="provisioned",
                DeletionProtection=_backend_config("deletion_protection", default=False),
                CopyTagsToSnapshot=True,
                StorageType=storage_type,
                AutoMinorVersionUpgrade=True,
                **args
            )
        else:
            # Only modify if cluster is available, otherwise call would fail
            if existing_cluster.get("Status") != "available":
                self._logger.info("DB cluster is not available. Cannot perform update")
                raise kopf.TemporaryError("Waiting for cluster to be available", delay=20)

            if existing_cluster and existing_cluster.get("EngineVersion") != version:
                # Version updates can only be done while the primary instance is healthy
                if existing_primary_instance and existing_primary_instance.get("Status") != "available":
                    raise kopf.TemporaryError("Cannot update version while primary instance is not healthy")
                self._logger.info(f"Cluster version will be updated from {existing_cluster.get('EngineVersion')} to {version}")
                args["EngineVersion"] = version

            if admin_password_changed:
                args["MasterUserPassword"] = password

            response = self._rds_client.modify_db_cluster(
                DBClusterIdentifier=cluster_name,
                ApplyImmediately=True,
                BackupRetentionPeriod=field_from_spec(spec, "backup.retentionDays", default=1),
                VpcSecurityGroupIds=_backend_config("vpc_security_group_ids", default=[]),
                DeletionProtection=_backend_config("deletion_protection", default=False),
                StorageType=storage_type,
                AutoMinorVersionUpgrade=True,
                AllowMajorVersionUpgrade=True,
                **args
            )

        # Wait for endpoint to be configured
        self._logger.info("Waiting for cluster to be created")
        response = field_from_spec(response, "DBCluster")
        wait_time = 0
        while not field_from_spec(response, "Endpoint"):
            if wait_time > 10*60:
                raise kopf.TemporaryError("Timed out waiting for DB cluster to be created", delay=20)
            time.sleep(10)
            wait_time += 10
            response = self._get_cluster(namespace, name)
        host = response['Endpoint']

        self._logger.info("Waiting for cluster to become available")
        response = self._get_cluster(namespace, name)
        wait_time = 0
        while field_from_spec(response, "Status") != "available":
            if wait_time > 10*60:
                raise kopf.TemporaryError("Timed out waiting for DB cluster to be available", delay=20)
            time.sleep(10)
            wait_time += 10
            response = self._get_cluster(namespace, name)

        # Prepare credentials
        data = {
            "username": admin_username,
            "password": password,
            "dbname": "postgres",
            "host": host,
            "port": "5432",
            "sslmode": "require"
        }

        # Deploy primary (writer) instance
        instance_name = f"{cluster_name}-primary"
        public_access = _backend_config("network.public_access", default=False)
        if not existing_primary_instance:
            response = self._rds_client.create_db_instance(
                DBClusterIdentifier=cluster_name,
                DBInstanceIdentifier=instance_name,
                DBInstanceClass=instance_class,
                PubliclyAccessible=public_access,
                Engine='aurora-postgresql'
            )
        else:
            existing_primary_instance = self._get_server(namespace, name, "primary")
            if existing_primary_instance.get("DBInstanceClass") != instance_class or existing_primary_instance.get("PubliclyAccessible") != public_access:
                if existing_primary_instance.get("DBInstanceStatus") != "available":
                    self._logger.info("DB status is not available. Cannot perform update")
                    raise kopf.TemporaryError("Waiting for instance to be available", delay=20)
                self._logger.info("Updating primary instance")
                response = self._rds_client.modify_db_instance(
                    DBInstanceIdentifier=instance_name,
                    DBInstanceClass=instance_class,
                    PubliclyAccessible=public_access,
                )
            else:
                self._logger.info("Primary instance already up-to-date")

        self._logger.info("Waiting for writer instance to be available")
        response = field_from_spec(response, "DBInstance")
        wait_time = 0
        while field_from_spec(response, "DBInstanceStatus") != "available":
            if wait_time > 10*60:
                raise kopf.TemporaryError("Timed out waiting for DB writer instance to be available", delay=20)
            time.sleep(10)
            wait_time += 10
            response = self._get_server(namespace, name, "primary")

        return data, warnings

    def delete_server(self, namespace, name):
        cluster_name = _calc_name(namespace, name)
        # First delete primary instance
        self._rds_client.delete_db_instance(
            DBInstanceIdentifier=f"{cluster_name}-primary",
            SkipFinalSnapshot=True,
            DeleteAutomatedBackups=False # Keep backups around just in case
        )
        # Then delete cluster
        self._rds_client.delete_db_cluster(
            DBClusterIdentifier=cluster_name,
            SkipFinalSnapshot=True,
        )

    def create_or_update_database(self, namespace, server_name, database_name, spec, admin_credentials=None):
        primary_instance = self._get_server(namespace, server_name, "primary")
        if not primary_instance or primary_instance.get("DBInstanceStatus") != "available":
            raise kopf.TemporaryError("Database instance currently not available.", delay=20)
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
        return "15.2"
    if "." in version:
        # User supplied a major.minor combination so just use that
        return version
    # In any other case use the newest available minor of the provided major version
    elif version.startswith("11"):
        return "11.19"
    elif version.startswith("12"):
        return "12.14"
    elif version.startswith("13"):
        return "13.10"
    elif version.startswith("14"):
        return "14.7"
    else:
        return "15.2"


def _determine_instance_class(size_spec):
    warnings = []
    size_class = size_spec.get("class")
    default_class = config_get("backends.awsaurora.default_class", default="operator_default")
    if not size_class:
        warnings.append(f"Directly specifying CPU and memory is currently not supported. Falling back to default class {default_class}")
    classes = config_get("backends.awsaurora.classes", default=[])
    if not default_class in classes:
        classes[default_class] = {"instance_type": "db.m5.large"}
    if not size_class in classes:
        warnings.append(f"selected class {size_class} is not allowed. Falling back to default {default_class}")
        size_class = default_class
    selected_class = classes[size_class]
    scaling_configuration = selected_class.get("scaling_configuration")
    if scaling_configuration:
        scaling_configuration = {"MinCapacity": scaling_configuration.get("min_capacity", 0.5), "MaxCapacity": scaling_configuration.get("max_capacity", 1)}
    return selected_class["instance_type"], scaling_configuration, selected_class.get("storage_type", "aurora"), selected_class.get("iops"), warnings
