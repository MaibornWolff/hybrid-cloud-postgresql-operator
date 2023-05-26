# Hybrid Cloud Operator for PostgreSQL

The Hybrid Cloud Operator for PostgreSQL is a Kubernetes Operator that has been designed for hybrid cloud, multi-teams kubernetes platforms to allow teams to deploy and manage their own databases via kubernetes without cloud provider specific provisioning.

In classical cloud environments things like databases would typically be managed by a central platform team via infrastructure automation like terraform. But this means when different teams are active on such a platform there exists a bottleneck because that central platform team must handle all requests for databases. With this operator teams in kubernetes gain the potential to manage databases on their own. And because the operator integrates into the kubernetes API the teams have the same unified interface/API for all their deployments: Kubernetes YAMLs.

Additionally the operator also provides a consistent interface regardless of the environment (cloud provider, on-premise) the kubernetes cluster runs in. This means in usecases where teams have to deploy to clusters running in different environments they still get the same interface on all clusters and do not have to concern themselves with any differences.

Main features:

* Provides Kubernetes Custom resources for deploying and managing PostgreSQL servers and databases
* Abstracted, unified API regardless of target environment (cloud, on-premise)
* Currently supported backends:
  * Azure Database for PostgreSQL single server
  * Azure Database for PostgreSQL flexible server
  * [bitnami](https://charts.bitnami.com/bitnami) [helm chart](https://github.com/bitnami/charts/tree/master/bitnami/postgresql/) (prototype)
  * [Yugabyte](https://docs.yugabyte.com/preview/deploy/kubernetes/single-zone/oss/helm-chart/) helm chart deployment (prototype, due to limitations in the chart only one cluster per namespace is possible)

## Quickstart

To test out the operator you do not need Azure, you just need a kubernetes cluster (you can for example create a local one with [k3d](https://k3d.io/)) and cluster-admin rights on it.

1. Run `helm repo add maibornwolff https://maibornwolff.github.io/hybrid-cloud-postgresql-operator/` to prepare the helm repository.
2. Run `helm install hybrid-cloud-postgresql-operator-crds maibornwolff/hybrid-cloud-postgresql-operator-crds` and `helm install hybrid-cloud-postgresql-operator maibornwolff/hybrid-cloud-postgresql-operator` to install the operator.
3. Check if the pod of the operator is running and healthy: `kubectl get pods -l app.kubernetes.io/name=hybrid-cloud-postgresql-operator`.
4. Create your first server: `kubectl apply -f examples/simple.yaml`.
5. Check if the postgresql instance is deployed: `kubectl get pods -l app.kubernetes.io/instance=demoteam-postgresql`.
6. Retrieve the credentials for the database: `kubectl get secret demoservice-postgres-credentials -o jsonpath="{.data.password}" | base64 -d`
7. After you are finished, delete the server: `kubectl delete -f examples/simple.yaml`

Note: You have to manually clean up any remaining PVCs in kubernetes as these are not automatically deleted to avoid accidental data loss.

## Operations Guide

To achieve its hybrid-cloud feature the operator abstracts between the generic API (Custom resources `PostgreSQLServer`, `PostgreSQLDatabase`) and the concrete implementation for a specific cloud service. The concrete implementations are called backends. You can configure which backends should be active in the configuration. If you have several backends active the user can also select one (for examaple choose between Azure Database for Postgres single server and flexible server).

The operator can be configured using a yaml-based config file. This is the complete configuration file with all options. Please refer to the comments in each line for explanations:

```yaml
handler_on_resume: false  # If set to true the operator will reconcile every available resource on restart even if there were no changes
backend: helmbitnami  # Default backend to use, required
allowed_backends: []  # List of backends the users can select from. If list is empty the default backend is always used regardless of if the user selects a backend 
backends:  # Configuration for the different backends. Required fields are only required if the backend is used
  azure:  # General azure configuration. Every option from here can be repeated in the specific azure backends. The operator first tries to find the option in the specific backend config and falls back to the general config if not found
    subscription_id: 1-2-3-4-5  # Azure Subscription id to provision database in, required
    location: westeurope  # Location to provision database in, required
    name_pattern: "foobar-{namespace}-{name}"  # Pattern to use for naming databases in azure. Variables {namespace} and {name} can be used and will be replaced by metadata.namespace and metadata.name of the custom object
    resource_group: foobar-rg  # Resource group to provision database in, required
    virtual_network: null  # Name of the virtual network to connect the database to, optional, only needed if public_access is disabled
    subnet: null  # Name of the subnet in the virtual_network to connect the database to, optional, only needed if public_access is disabled
    cpu_limit: 64  # Upper limit for the number of CPUs the user can request, optional
    storage_limit_gb: 512  # Upper limit for storage the user can request, optional
    server_delete_fake: false  # If enabled on delete the server will not actually be deleted but only be tagged, optional
    database_delete_fake: false  # If enabled on delete the database will not actually be deleted, optional
    lock_from_deletion: false  # If enabled an azure lock will be set on the server object, requires owner permissions for the operator, optional
    admin_username: postgres  # Username to use as admin user, optional
    tags: {}  # Extra tags to add to the server object in azure, {namespace} and {name} can be used as variables, optional
    network:
      public_access: true  # If enabled database server will be reachable from outside the virtual_network, optional
      allow_azure_services: true  # If enabled a firewall rule will be added so that azure services can access the database server, optional
    parameters:  # Defaults to use when the user does not provide values of their own
      geo_redundant_backup: false  # If enabled geo redundant backups will be enabled, optional
      backup_retention_days: 7  # Number of days the backups should be retained, optional
      network:
        firewall_rules:  # List of firewall rules to add to each server, optional
          - name: foobar  # Name of the rule, required
            startIp: 1.2.3.4  # Start IP address, required
            endIp: 1.2.3.4  # End IP address, required
  azurepostgres:
    classes:  # List of instance classes the user can select from, optional
      dev:  # Name of the class
        name: GP_Gen5_2  # Name of the SKU in Azure, required
        tier: GeneralPurpose  # Tier of the SKU in Azure, required
        family: Gen5  # Family of the SKU in Azure, required
        capacity: 2  # Capacity (CPU Cores) of the SKU in Azure, required
    default_class: dev  # Name of the class to use as default if the user-provided one is invalid or not available, required if classes should be usable
    network:
      create_private_endpoint: false  # If enabled a private link + private endpoint will be created for the server, virtual_network and subnet must be supplied in this case, optional
      vnets:  # List of vnets the database should allow access from, optional
        - vnet: foobar-vnet  # Name of the virtual network, required
          subnet: default  # Name of the subnet, required
    parameters:  # Defaults to use when the user does not provide values of their own
      infrastructure_encryption: Disabled  # Should infrastructure encryption be enabled for the database, optional
  azurepostgresflexible:
    classes:  # List of instance classes the user can select from, optional
      dev:  # Name of the class, required
        name: Standard_B1ms  # Name of the SKU in Azure, required
        tier: Burstable  # Tier of the SKU in Azure, required
      small:
        name: Standard_D2ds_v4
        tier: GeneralPurpose
    default_class: dev  # Name of the class to use as default if the user-provided one is invalid or not available, required if classes should be usable
    availability_zone: "1"  # Availability zone to use for the database, required
    standby_availability_zone: "2"  # Standby availability zone to use for the database if the user enables high-avalability, optional
    dns_zone:  # Settings for the private dns zone to use for vnet integration. If the private dns zone is in the same resource group as the server, the fields "name" and resource_group can be omitted and the name can be placed here, optional
      name: privatelink.postgres.database.azure.com # Name of the private dns zone, optional
      resource_group: foobar-rg # Resource group the private dns zone is part of, if omitted it defaults to the resource group the server resource group, optional
  helmbitnami:
    default_class: small  # Name of the class to use as default if the user-provided one is invalid or not available, required if classes should be usable
    classes:  # List of instance classes the user can select from, optional
      small:  # Name of the class
        cpu: "1000m"   # CPU requests/limits for the pod, required
        memory: "256Mi"  # Memory requests/limits for the pod, required
    admin_username: postgres  # Username to use as admin user, optional
    storage_class: ""  # Storage class to use for the pods, optional
    pvc_cleanup: false  # If set to true the operator will when deleting a server also delete the persistent volumes, optional
  helmyugabyte:
    default_class: small  # Name of the class to use as default if the user-provided one is invalid or not available, required if classes should be usable
    classes:  # List of instance classes the user can select from, optional
      small:  # Name of the class
        master:
          cpu: "1000m"   # CPU requests/limits for the master pods, required
          memory: "256Mi"  # Memory requests/limits for the master pod, required
        tserver:
          cpu: "1000m"  # CPU requests/limits for the tserver pods, required
          memory: "256Mi"  # Memory requests/limits for the tserver pod, required
    replicas_master: 1  # Number of replicas for the master nodes, set to 3 to get a HA cluster, optional
    replicas_tserver: 1  # Number of replicas for the tserver nodes, set to 3 to get a HA cluster, optional
    partitions_master: 1  # Number of partitions on the master nodes, optional
    partitions_tserver: 1  # Number of partitions on the tserver nodes, optional
    storage_class: ""  # Storage class to use for the pods, optional
    pvc_cleanup: false  # If set to true the operator will when deleting a server also delete the persistent volumes, optional
security: # Security-related settings independent of any backends, optional
  password_length: 16  # Number of characters to use for passwords that are generated for servers and databases, optional
```

Single configuration options can also be provided via environment variables, the complete path is concatenated using underscores, written in uppercase and prefixed with `HYBRIDCLOUD_`. As an example: `backends.azure.subscription_id` becomes `HYBRIDCLOUD_BACKENDS_AZURE_SUBSCRIPTION_ID`.

The `azure` backend is a virtual backend that allows you to specify options that are the same for both `azurepostgres` and `azurepostgresflexible`. As such each option under `backends.azure` in the above configuration can be repeated in the `backends.azurepostgres` and `backends.azurepostgresflexible` sections. Note that currrently the operator cannot handle using different subscriptions for the backends.

To make it easier for the users to specify database sizes you can prepare a list of recommendations, called classes, the users can choose from. The fields of the classes are backend-dependent. Using this mechanism you can give the users classes like `small`, `production`, `production-ha` and size them appropriately for each backend. If the user specifies size using CPU and memory the backend will pick an appropriate match.

To protect database servers against accidential deletion you can enable `lock_from_deletion` in the azure backends. When enabled the operator will create a delete lock on the server resource in Azure. Note that the operator will not remove that lock when the server object in kubernetes is deleted, you have to do that yourself via either the Azure CLI or the Azure Portal so the operator can delete the server. If that is not done the kubernetes object cannot be deleted and any calls ala `kubectl delete` will hang until the lock is manually removed.
The azure backends also support a feature called `fake deletion` (via options `server_delete_fake` and `database_delete_fake`) where the database or server are not actually deleted when the kubernetes custom object is deleted. This can be used in situations where the operator is freshly introduced in an environment where the users have little experience with this type of declarative management and you want to reduce the risk of accidental data loss.

The azure backends support deploying the server in a way that it is only reachable from inside an azure virtual network. For the single server this is done using a private endpoint, for the flexible server via the vnet integration. To enable the feature set `network.public_access` to false for the backend in the config. For `azurepostgres` you also need to enable `network.create_private_endpoint`. For `azurepostgresflexible` you can't change the option after a server is created (see [Azure Docs](https://docs.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-networking)). Additionally you need to prepare your Azure resource group:

For the single server:

* You need an existing virtual network with a subnet
* Create a private dns zone with the name `postgres.database.azure.com` in your resource group
* Link the dns zone to your virtual network

For the flexible server (also see the [Azure Docs](https://docs.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-networking#private-access-vnet-integration)):

* You need an existing virtual network with a subnet
* For the subnet enable the delegation to `Microsoft.DBforPostgreSQL/flexibleServers`
* You need a private dns zone with a name that ends on `.postgres.database.azure.com`, which can also be part of an other resource group (e.g. `mydatabases.postgres.database.azure.com`)
* Link the dns zone to the virtual network the server is part of
* In the operator config for the backend fill out the fields `virtual_network`, `subnet` and `dns_zone`

For the operator to interact with Azure it needs credentials. For local testing it can pick up the token from the azure cli but for real deployments it needs a dedicated service principal. Supply the credentials for the service principal using the environment variables `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` (if you deploy via the helm chart use the use `envSecret` value). Depending on the backend the operator requires the following azure permissions within the scope of the resource group it deploys to:

* `Microsoft.DBforPostgreSQL/*`
* `Microsoft.Network/*`
* `Microsoft.Authorization/locks/*`, optional, if you want the operator to set delete locks

Unfortunately there is no built-in azure role for the Database for PostgreSQL service, if you do not want to create a custom role you can also assign the operator the Contributor or Owner (if lock handling is required) roles, but beware this is a potential attack surface as someone compromising the operator can access your entire Azure account.

### Deployment

The operator can be deployed via helm chart:

1. Run `helm repo add maibornwolff https://maibornwolff.github.io/hybrid-cloud-postgresql-operator/` to prepare the helm repository.
2. Run `helm install hybrid-cloud-postgresql-operator-crds maibornwolff/hybrid-cloud-postgresql-operator-crds` to install the CRDs for the operator.
3. Run `helm install hybrid-cloud-postgresql-operator maibornwolff/hybrid-cloud-postgresql-operator -f values.yaml` to install the operator.

Configuration of the operator is done via helm values. For a full list of the available values see the [values.yaml in the chart](./helm/hybrid-cloud-postgresql-operator/values.yaml). These are the important ones:

* `operatorConfig`: overwrite this with your specific operator config
* `envSecret`: Name of a secret with sensitive credentials (e.g. Azure service principal credentials)
* `serviceAccount.create`: Either set this to true or create the serviceaccount with appropriate permissions yourself and set `serviceAccount.name` to its name

## User Guide

The operator is completely controlled via Kubernetes custom resources (`PostgreSQLServer` and `PostgreSQLDatabase`). Once a server object is created the operator will utilize one of its backends to provision an actual database server. For each server one or more databases can be created by creating `PostgreSQLDatabase` objects that reference that server.

The `PostgreSQLServer` resource has the following options:

```yaml
apiVersion: hybridcloud.maibornwolff.de/v1alpha1
kind: PostgreSQLServer
metadata:
  name: teamfoo  # Name of the database server, based on this a name in the backend will be generated
  namespace: default  # Kubernetes namespace
spec:
  backend: azurepostgres  # Name of the backend to use, optional, should be left empty unless provided by the admin
  version: latest  # Version to use, can be a number like 11, 12, 13. If empty or `latest` the newest available version for that backend is used. If specified version is not available in backend default is used, optional
  size:
    class: dev  # Resource class to use, available classes are specified by the operator admin. if this is specified cpu and memoryMB are ignored. Use only if told to by admin.
    cpu: 1  # Number of CPU cores to use, optional
    memoryMB: 512  # Memory to use in MB, optional
    storageGB: 32  # Size of the storage for the database in GB, required
    storageAutoGrow: false  # If the backend supports it automatic growing of the storage can be enabled, optional
  backup:  # If the backend supports automatic backup it can be configured here, optional
    retentionDays: 7  # Number of days backups should be retained. Min and max are dependent on the backend (for azure 7-35 days), optional
    geoRedundant: false  # If the backend supports it the backups can be stored geo-redundant in more than one region, optional
  extensions: [] # List of postgres extensions to install in the database. List is dependent on the backend (e.g. azure supports timescaledb). Currently only supported with azure backends. optional. 
  network:  # Network related features, optional
    firewallRules:  # If the backend supports it a list of firewall rules to configure access from outside the cluster
      - name: foobar  # Name of the rule
        startIp: 1.2.3.4  # Start IP
        endIp: 1.2.3.4  # End IP
  serverParameters: {} # Map of server parameters, optional
  maintenance:
    window:  # If the backend supports configuring a maintenance window it can be done here, optional
      weekday: Wed  # Weekday of the maintenance window. Must be provided as 3-letter english weekday name (Mon, Tue, Wed, Thu, Fri, Sat, Sun), required
      starttime: 03:00  # Start time as hour:minute, required
  highavailability:
    enabled: false  # If the backend supports it high availability (via several instances) can be enabled here, optional
  credentialsSecret: teamfoo-postgres-credentials  # Name of a secret where the credentials for the database server should be stored
```

For each server one or more databases can be created with the `PostgreSQLDatabase` resource which has the following options:

```yaml
apiVersion: hybridcloud.maibornwolff.de/v1alpha1
kind: PostgreSQLDatabase
metadata:
  name: fooservice  # Name of the database, will be used as name of the database in postgres (with dashes replaced with underscores)
  namespace: default  # Kubernetes namespace, must be in the same namespace as the server object
spec:
  serverRef:
    name: teamfoo  # Name of the server object of type `PostgreSQLServer`. Must be in the same namespace. Required
  database:
    charset: UTF8  # charset to use for the database, default depends on the backend, optional
    collation: "de-DE"  # Collation to use for the database, default depends on the backend, optional
    extensions: [] # List of extensions to activate in the database (via CREATE EXTENSION), only extensions provisioned for the server (via spec.extensions) can be activated here
  credentialsSecret: fooservice-postgres-credentials   # Name of a secret where the credentials for the database should be stored
```

It is recommended not to use the system database (`postgres`) for anything but instead create a separate database for each service/application.

A service/application that wants to access the database should depend on the credentials secret and use its values for the connection. That way it is independent of the actual backend. Provided keys in the secret are: `hostname`, `port`, `dbname`, `username`, `password`, `sslmode` and should be directly usable with any postgresql-compatible client library.

## Development

The operator is implemented in Python using the [Kopf](https://github.com/nolar/kopf) ([docs](https://kopf.readthedocs.io/en/stable/)) framework.

To run it locally follow these steps:

1. Create and activate a local python virtualenv
2. Install dependencies: `pip install -r requirements.txt`
3. Setup a local kubernetes cluster, e.g. with k3d: `k3d cluster create`
4. Apply the CRDs in your local cluster: `kubectl apply -f helm/hybrid-cloud-postgresql-operator-crds/templates/`
5. If you want to deploy to azure: Either have the azure cli installed and configured with an active login or export the following environment variables: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`
6. Adapt the `config.yaml` to suit your needs
7. Run `kopf run main.py -A`
8. In another window apply some objects to the cluster to trigger the operator (see the `examples` folder)

The code is structured in the following packages:

* `handlers`: Implements the operator interface for the provided custom resources, reacts to create/update/delete events in handler functions
* `backends`: Backends for the different environments (currently Azure + on-premise with helm)
* `util`: Helper and utility functions

To locally test the helm backends the operator needs a way to communicate with pods running in the cluster. You can use [sshuttle](https://github.com/sshuttle/sshuttle) and [kuttle](https://github.com/kayrus/kuttle) for that. Run:

```bash
kubectl run kuttle --image=python:3.10-alpine --restart=Never -- sh -c 'exec tail -f /dev/null'
sshuttle --dns -r kuttle -e kuttle <internal-ip-range-of-your-cluster>
```

### Tips and tricks

* Kopf marks every object it manages with a finalizer, that means that if the operator is down or doesn't work a `kubectl delete` will hang. To work around that edit the object in question (`kubectl edit <type> <name>`) and remove the finalizer from the metadata. After that you can normally delete the object. Note that in this case the operator will not take care of cleaning up any azure resources.
* If the operator encounters an exception while processing an event in a handler, the handler will be retried after a short back-off time. During the development you can then stop the operator, make changes to the code and start the operator again. Kopf will pick up again and rerun the failed handler.
* When a handler was successfull but you still want to rerun it you need to fake a change in the object being handled. The easiest is adding or changing a label.
