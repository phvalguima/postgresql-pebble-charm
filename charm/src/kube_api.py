"""

Methods to interact with Kubernetes.

Uses a kubeconfig passed as argument and allows to control k8s apis such
as execute commands on the pod.

Based on: https://github.com/kubernetes-client/python/blob/master/examples/pod_exec.py

TODO: Replace the kubeconfig logic for a Service Account

""" # noqa

import yaml
import logging

from kubernetes import config
from kubernetes.client import Configuration
from kubernetes.client.api import core_v1_api
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

logger = logging.getLogger(__name__)


class PsqlK8sAPIPsqlK8sAPIError(Exception):
    def __init__(self, msg):
        super().__init__(msg)


class PsqlK8sAPI(object):

    def __init__(self, container, kubeconfig, ops_model):
        """Creates the PsqlK8sAPI class.

        Pebble must be ready to receive commands otherwise this class
        will throw an Exception.
        """
        self._container = container
        self._hostname = container.pull("/etc/hostname")
        self._namespace = ops_model.name

        self._kubeconfig = yaml.safe_load(kubeconfig)
        config.load_kube_config_from_dict(config_dict=self._kubeconfig)
        try:
            c = Configuration().get_default_copy()
        except AttributeError:
            c = Configuration()
            c.assert_hostname = False
        Configuration.set_default(c)
        self._core_v1 = core_v1_api.CoreV1Api()

    @property
    def container(self):
        return self._container

    @property
    def kubeconfig(self):
        return self._kubeconfig

    @property
    def hostname(self):
        return self._hostname

    @property
    def namespace(self):
        return self._namespace

    def exec_command(self, cmd):
        resp = None
        try:
            resp = self._core_v1.read_namespaced_pod(
                name=self.hostname, namespace=self.namespace)
        except ApiException as e:
            if e.status != 404:
                raise PsqlK8sAPIPsqlK8sAPIError("Unknown error: %s" % e)

        resp = stream(
            self._core_v1.connect_get_namespaced_pod_exec,
            self.hostname,
            self.namespace,
            command=cmd,
            stderr=True, stdin=False,
            stdout=True, tty=False)

        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                logger.info("exec_command returned: %s" % resp.read_stdout())
            if resp.peek_stderr():
                resp.close()
                raise PsqlK8sAPIPsqlK8sAPIError(
                    "exec_command stderr returned: %s" % resp.read_stderr())
        resp.close()
