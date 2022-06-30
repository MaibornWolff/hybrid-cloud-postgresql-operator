import json
import subprocess


def run(cmd, fail=False, **kwargs):
    res = subprocess.run(cmd, shell=True, capture_output=True, **kwargs)
    if fail:
        if res.returncode != 0:
            print(res.stdout)
            print(res.stderr)
            res.check_returncode()
    return res


def run_helm(cmd, **kwargs):
    return run(f"helm " + cmd, **kwargs)


def install_upgrade(namespace, name, chart, options, values=None):
    if values:
        return run_helm(f"upgrade --install -n {namespace} {name} {chart} -f - {options}", fail=True, input=values, text=True)
    else:
        return run_helm(f"upgrade --install -n {namespace} {name} {chart} {options}", fail=True)


def check_installed(namespace, name):
    res = run_helm(f"list -n {namespace} -o json")
    if res.returncode != 0:
        return False
    data = json.loads(res.stdout)
    for deployment in data:
        if deployment["name"] == name:
            return True
    return False


def uninstall(namespace, name):
    return run_helm(f"uninstall -n {namespace} {name}", fail=True)
