import base64
from dataclasses import dataclass
import kopf
import kubernetes


API_GROUP = "hybridcloud.maibornwolff.de"

@dataclass
class Resource:
    group: str
    version: str
    plural: str
    kind: str

    def kopf_on(self):
        return [self.group, self.version, self.plural]


PostgreSQLServer = Resource(API_GROUP, "v1alpha1", "postgresqlservers", "PostgreSQLServer")
PostgreSQLDatabase = Resource(API_GROUP, "v1alpha1", "postgresqldatabases", "PostgreSQLDatabase")


def decode_secret_data(secret):
    result = dict()
    for key, data in secret.data.items():
        result[key] = base64.b64decode(data).decode("utf-8")
    return result


def create_secret(namespace, name, data, labels={}):
    api = kubernetes.client.CoreV1Api()
    metadata = {
        "name": name,
        "namespace": namespace,
        "labels": labels,
    }
    body = kubernetes.client.V1Secret(metadata=metadata, string_data=data)
    api.create_namespaced_secret(namespace, body)


def get_secret(namespace, name):
    api = kubernetes.client.CoreV1Api()
    try:
        return api.read_namespaced_secret(name, namespace)
    except:
        return None


def update_secret(namespace, name, data):
    api = kubernetes.client.CoreV1Api()
    metadata = {
        "name": name,
        "namespace": namespace
    }
    body = kubernetes.client.V1Secret(metadata=metadata, string_data=data)
    api.patch_namespaced_secret(name, namespace, body)


def create_or_update_secret(namespace, name, data, labels={}):
    if get_secret(namespace, name):
        update_secret(namespace, name, data)
    else:
        create_secret(namespace, name, data, labels=labels)


def delete_secret(namespace, name):
    api = kubernetes.client.CoreV1Api()
    try:
        api.delete_namespaced_secret(name, namespace)
    except:
        pass


def patch_custom_object(resource: Resource, namespace: str,  name: str, body):
    api = kubernetes.client.CustomObjectsApi()
    api.patch_namespaced_custom_object(resource.group, resource.version, namespace, resource.plural, name, body)


def get_custom_object(resource: Resource, namespace: str, name: str):
    api = kubernetes.client.CustomObjectsApi()
    try:
        return api.get_namespaced_custom_object(resource.group, resource.version, namespace, resource.plural, name)
    except:
        return None


def patch_custom_object_status(resource: Resource, namespace: str, name: str, status):
    body = {
        "metadata": {
            "name": name,
            "namespace": namespace
        },
        "status": status
    }
    patch_custom_object(resource, namespace, name, body)
