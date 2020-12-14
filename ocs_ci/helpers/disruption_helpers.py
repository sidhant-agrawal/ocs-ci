import logging
import random
import re
import time

from ocs_ci.ocs.resources import pod
from ocs_ci.ocs import constants, ocp, utils
from ocs_ci.framework import config
from ocs_ci.utility.utils import TimeoutSampler, run_async, run_cmd
from ocs_ci.utility.retry import retry
from ocs_ci.ocs.exceptions import TimeoutExpiredError

log = logging.getLogger(__name__)

POD = ocp.OCP(kind=constants.POD, namespace=config.ENV_DATA["cluster_namespace"])


class Disruptions:
    """
    This class contains methods of disrupt operations
    """

    resource = None
    resource_obj = None
    resource_count = 0
    selector = None
    daemon_pid = None

    def set_resource(self, resource, leader_type="provisioner"):
        self.resource = resource
        resource_count = 0
        if self.resource == "mgr":
            self.resource_obj = pod.get_mgr_pods()
            self.selector = constants.MGR_APP_LABEL
        if self.resource == "mon":
            self.resource_obj = pod.get_mon_pods()
            self.selector = constants.MON_APP_LABEL
        if self.resource == "osd":
            self.resource_obj = pod.get_osd_pods()
            self.selector = constants.OSD_APP_LABEL
        if self.resource == "mds":
            self.resource_obj = pod.get_mds_pods()
            self.selector = constants.MDS_APP_LABEL
        if self.resource == "cephfsplugin":
            self.resource_obj = pod.get_plugin_pods(interface=constants.CEPHFILESYSTEM)
            self.selector = constants.CSI_CEPHFSPLUGIN_LABEL
        if self.resource == "rbdplugin":
            self.resource_obj = pod.get_plugin_pods(interface=constants.CEPHBLOCKPOOL)
            self.selector = constants.CSI_RBDPLUGIN_LABEL
        if self.resource == "cephfsplugin_provisioner":
            self.resource_obj = [
                pod.get_plugin_provisioner_leader(interface=constants.CEPHFILESYSTEM)
            ]
            self.selector = constants.CSI_CEPHFSPLUGIN_PROVISIONER_LABEL
            resource_count = len(pod.get_cephfsplugin_provisioner_pods())
        if self.resource == "rbdplugin_provisioner":
            self.resource_obj = [
                pod.get_plugin_provisioner_leader(interface=constants.CEPHBLOCKPOOL)
            ]
            self.selector = constants.CSI_RBDPLUGIN_PROVISIONER_LABEL
            resource_count = len(pod.get_rbdfsplugin_provisioner_pods())
        if self.resource == "operator":
            self.resource_obj = pod.get_operator_pods()
            self.selector = constants.OPERATOR_LABEL

        self.resource_count = resource_count or len(self.resource_obj)

    def delete_resource(self, resource_id=0):
        self.resource_obj[resource_id].delete(force=True)
        assert POD.wait_for_resource(
            condition="Running",
            selector=self.selector,
            resource_count=self.resource_count,
            timeout=300,
        )

    @retry(AssertionError, tries=5, delay=3, backoff=1)
    def select_daemon(self, node_name=None):
        """
        Select pid of self.resource daemon

        Args:
            node_name (str): Name of node in which the resource daemon has
                to be selected.
        """
        node_name = node_name or self.resource_obj[0].pod_data.get("spec").get(
            "nodeName"
        )
        awk_print = "'{print $1}'"
        pid_cmd = (
            f"oc debug node/{node_name} -- chroot /host ps ax | grep"
            f" ' ceph-{self.resource} --' | grep -v grep | awk {awk_print}"
        )
        pid_proc = run_async(pid_cmd)
        ret, pid, err = pid_proc.async_communicate()
        pid = pid.strip()

        # Consider scenario where more than one self.resource pod is running
        # on one node. eg: More than one osd on same node.
        pids = pid.split()
        self.pids = [pid.strip() for pid in pids]
        assert self.pids, "Obtained pid value is empty."
        pid = self.pids[0]

        # ret will be 0 and err will be None if command is success
        assert not any([ret, err, not pid.isdigit()]), (
            f"Failed to fetch pid of ceph-{self.resource} "
            f"from {node_name}. ret:{ret}, pid:{pid}, err:{err}"
        )

        self.daemon_pid = pid

    def kill_daemon(self, node_name=None, check_new_pid=True):
        """
        Kill self.resource daemon

        Args:
            node_name (str): Name of node in which the resource daemon has
                to be killed
            check_new_pid (bool): True to check for new pid after killing the
                daemon. False to skip the check.
        """
        node_name = node_name or self.resource_obj[0].pod_data.get("spec").get(
            "nodeName"
        )
        if not self.daemon_pid:
            self.select_daemon(node_name=node_name)

        # Command to kill the daemon
        kill_cmd = (
            f"oc debug node/{node_name} -- chroot /host  " f"kill -9 {self.daemon_pid}"
        )
        daemon_kill = run_cmd(kill_cmd)

        # 'daemon_kill' will be an empty string if command is success
        assert isinstance(daemon_kill, str) and (not daemon_kill), (
            f"Failed to kill ceph-{self.resource} daemon in {node_name}. "
            f"Daemon kill command output - {daemon_kill}"
        )
        log.info(f"Killed ceph-{self.resource} daemon on node {node_name}")

        if check_new_pid:
            awk_print = "'{print $1}'"
            pid_cmd = (
                f"oc debug node/{node_name} -- chroot /host ps ax | grep"
                f" ' ceph-{self.resource} --' | grep -v grep | awk {awk_print}"
            )
            try:
                for pid_proc in TimeoutSampler(60, 2, run_async, command=pid_cmd):
                    ret, pid, err = pid_proc.async_communicate()

                    # Consider scenario where more than one self.resource pod
                    # is running on one node. eg:More than one osd on same node
                    pids = pid.strip().split()
                    pids = [pid.strip() for pid in pids]
                    if len(pids) != len(self.pids):
                        continue
                    new_pid = [pid for pid in pids if pid not in self.pids]
                    assert len(new_pid) == 1, "Found more than one new pid."
                    new_pid = new_pid[0]
                    if new_pid.isdigit() and (new_pid != self.daemon_pid):
                        log.info(f"New pid of ceph-{self.resource} is {new_pid}")
                        break
            except TimeoutExpiredError:
                raise TimeoutExpiredError(
                    f"Waiting for pid of ceph-{self.resource} in {node_name}"
                )


class DisruptionsExternalCluster:
    """
    This class contains methods of disrupt operations for External mode
    """

    def __init__(self):
        self.resource = None
        self.resource_id = None
        self.resource_count = 0
        self.ceph_cluster = utils.get_external_mode_rhcs()

    def set_resource(self, resource):
        self.resource = resource
        self.resource_count = len(self.ceph_cluster.get_metadata_list(resource))

    def select_daemon(self):
        """
        Select id of leader/active self.resource daemon

        """
        ct_pod = pod.get_ceph_tools_pod()
        if self.resource == "mgr":
            self.resource_id = ct_pod.exec_ceph_cmd("ceph mgr dump").get("active_name")
        elif self.resource == "mon":
            self.resource_id = ct_pod.exec_ceph_cmd("ceph quorum_status").get(
                "quorum_leader_name"
            )
        elif self.resource == "mds":
            mds_stat = ct_pod.exec_ceph_cmd("ceph mds stat", format="plain")
            self.resource_id = re.search(r"(?<=0=).*?(?==up:active)", mds_stat).group(0)
        else:
            self.resource_id = random.randint(0, self.resource_count)

        log.info(f"Selected daemon: ceph-{self.resource}@{self.resource_id}")

    def delete_resource(self, wait_time=60):
        """
        Simulate pod deletion by stop and start ceph daemon

        Args:
            wait_time (int): Seconds to wait before starting the Ceph daemon

        """
        if not self.resource_id:
            self.select_daemon()

        utils.change_daemon_status_external(
            self.ceph_cluster, self.resource, self.resource_id, "stop"
        )
        log.info(f"Waiting for {wait_time} seconds before starting the Ceph daemon")
        time.sleep(wait_time)
        utils.change_daemon_status_external(
            self.ceph_cluster, self.resource, self.resource_id, "start"
        )

    def kill_daemon(self):
        """
        Kill self.resource daemon

        """
        if not self.resource_id:
            self.select_daemon()

        utils.kill_daemon_external(self.ceph_cluster, self.resource, self.resource_id)
        log.info(f"Killed ceph-{self.resource}@{self.resource_id} daemon")
