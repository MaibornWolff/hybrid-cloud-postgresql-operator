import base64
import kopf
from . import k8s
from ..util import env
from ..util.password import generate_password
from ..config import config_get


def ignore_control_label_change(diff):
    if diff:
        only_action_labels_removed = False
        for d in diff:
            d = repr(d)
            if "remove" in d and "operator/action" in d:
                only_action_labels_removed = True
            else:
                only_action_labels_removed = False
        return only_action_labels_removed
    else:
        return False


def process_action_label(labels, commands, body, resource: k8s.Resource):
    found_control_labels = False
    for label, value in labels.items():
        if label == "operator/action":
            found_control_labels = True
            if value in commands:
                result = commands[value]()
                if result and isinstance(result, str):
                    kopf.event(body, type="operator", reason="action", message=result)
            else:
                kopf.event(body, type="operator", reason="failure", message=f"Unknown action: {value}")
    if found_control_labels:
        labels = dict(labels)
        labels["operator/action"] = None
        namespace = body["metadata"]["namespace"]
        name = body["metadata"]["name"]
        k8s.patch_custom_object(resource, namespace, name, {
            "metadata": {
                "labels": labels
            }
        })


def has_label(labels, key, value=None):
    if key in labels:
        return value is None or labels[key] == value
    else:
        return False


def field_from_spec(spec, path, default=None, fail_if_missing=False):
    ptr = spec
    for var in path.split('.'):
        if ptr and var in ptr:
            ptr = ptr[var]
        else:
            if fail_if_missing:
                raise kopf.PermanentError(f"Missing spec field: {path}")
            return default
    return ptr


def determine_resource_password(credentials_secret, tmp_secret_name):
    """Determine password to use for a resource by either
        * Reading the password from a credentials secret
        * Reading the password from a temporary secret while the resource is still being created (when the operator has to rerun the handler)
        * Generate a new password and store it in a temporary secret
    """
    tmp_secret = k8s.get_secret(env.OPERATOR_NAMESPACE, tmp_secret_name)
    if credentials_secret:
        password = base64.b64decode(credentials_secret.data["password"]).decode("utf-8")
    elif tmp_secret:
        password = base64.b64decode(tmp_secret.data["password"]).decode("utf-8")
    else:
        password = generate_password(int(config_get("security.password_length", default=16)), bool(config_get("security.special_characters", default=True)))
        k8s.create_secret(env.OPERATOR_NAMESPACE, tmp_secret_name, {"password": password})
    return password


def shorten(text):
    if len(text) > 63:
        return text[:63]
    return text
