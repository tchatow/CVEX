import re
import sys
import tempfile
import yaml

from invoke import UnexpectedExit

from cvex.consts import *
from cvex.vm import VM, VMTemplate

class LinuxVM(VM):
    def __init__(self,
                 vms: list,
                 template: VMTemplate,
                 cve: str,
                 destination: Path | None = None):
        super().__init__(vms, template, cve, destination)

    def init(self, router: VM | None = None):
        self.log.info("Initializing the Linux VM")
        if router:
            # Install the Certificate Authority (root) certificate
            local_cert = tempfile.NamedTemporaryFile()
            router.ssh.download_file(local_cert.name, f"/home/{router.vag.user()}/.mitmproxy/mitmproxy-ca-cert.cer")
            remote_tmp_cert = "/tmp/mitmproxy-ca-cert.crt"
            self.ssh.upload_file(local_cert.name, remote_tmp_cert)
            self.ssh.run_command(f"sudo mv {remote_tmp_cert} /usr/local/share/ca-certificates")
            self.ssh.run_command("sudo update-ca-certificates")

    def update_hosts(self, vms: list[VM]):
        remote_hosts = "/etc/hosts"
        local_hosts = tempfile.NamedTemporaryFile()
        self.ssh.download_file(local_hosts.name, remote_hosts)
        with open(local_hosts.name, "r") as f:
            hosts = f.read()
        ips = "\n"
        for vm in vms:
            if vm != self:
                line = f"{vm.ip} {vm.vm_name}\n"
                if line not in hosts:
                    ips += line
        if ips != "\n":
            self.log.debug("Setting ip hosts: %s", ips)
            hosts += ips
            with open(local_hosts.name, "w") as f:
                f.write(hosts)
            self.ssh.upload_file(local_hosts.name, "/tmp/hosts")
            self.ssh.run_command(f"sudo mv /tmp/hosts {remote_hosts}")

    def get_ansible_inventory(self) -> Path:
        inventory = Path(self.destination, "inventory.ini")
        with open(inventory, "w") as f:
            self.log.info("Retrieving SSH configuration of %s...", self.vm_name)
            data = (f"{self.vm_name} "
                    f"ansible_host={self.vag.hostname()} "
                    f"ansible_port={self.vag.port()} "
                    f"ansible_user={self.vag.user()} "
                    f"ansible_ssh_private_key_file={self.vag.keyfile()} "
                    f"ansible_ssh_common_args='-o StrictHostKeyChecking=no'")
            f.write(data)
        return inventory
    
    def _set_network_interface_ip(self, router_ip: str, netcfg: dict, netcfg_dest: str):
        netcfg_local = tempfile.NamedTemporaryFile()
        with open(netcfg_local.name, "w") as f:
            yaml.dump(netcfg, f)
        self.ssh.upload_file(netcfg_local.name, "/tmp/cvex.yaml")
        self.ssh.run_command(f"sudo mv /tmp/cvex.yaml {netcfg_dest}")
        self.ssh.run_command("sudo ip link set eth1 up")
        self.ssh.run_command("sudo netplan apply")
        try:
            self.ssh.run_command(f"sudo ip route change 192.168.56.0/24 via {router_ip} dev eth1")
        except:
            pass
        self.ssh.run_command("sudo systemctl restart ufw")

    def set_network_interface_ip(self, router_ip: str):
        yamls = self.ssh.run_command("ls /etc/netplan")
        for fil in re.findall(r"([\w\.\-]+\.yaml)", yamls):
            netcfg_dest = f"/etc/netplan/{fil}"
            netcfg_local = tempfile.NamedTemporaryFile()
            try:
                self.ssh.download_file(netcfg_local.name, netcfg_dest)
            except PermissionError:
                # Some configs are accessible only to root
                continue
            with open(netcfg_local.name, "r") as f:
                netcfg = yaml.safe_load(f)
            if 'network' not in netcfg or 'ethernets' not in netcfg['network'] or 'eth1' not in netcfg['network']['ethernets']:
                continue
            self.log.debug("Old %s: %r", netcfg_dest, netcfg)
            netcfg['network']['ethernets']['eth1'] = {
                "dhcp4" : "no",
                "addresses" : [f"{self.ip}/24"],
                "routes" : [{"to" : "default", "via" : router_ip}]
            }
            self._set_network_interface_ip(router_ip, netcfg, netcfg_dest)
            return
        netcfg = {
            "network" : {
                "ethernets" : {
                    "eth1" : {
                        "dhcp4" : "no",
                        "addresses" : [f"{self.ip}/24"],
                        "routes" : [{"to" : "default", "via" : router_ip}]
                    }
                },
                "version" : 2
            }
        }
        self.log.debug("cvex.yaml: %r", netcfg)
        self._set_network_interface_ip(router_ip, netcfg, "/etc/netplan/cvex.yaml")


    def start_api_tracing(self):
        self.strace = {}
        if not self.trace:
            return
        try:
            self.ssh.run_command("sudo pkill strace")
        except:
            pass
        try:
            self.ssh.run_command(f"rm -rf {CVEX_TEMP_FOLDER_LINUX}")
        except:
            pass
        self.ssh.run_command(f"mkdir {CVEX_TEMP_FOLDER_LINUX}")
        try:
            procs = self.ssh.run_command(f"ps -ax | egrep \"{self.trace}\" | grep -v grep")
        except UnexpectedExit:
            procs = None
        if not procs:
            self.log.critical("VM %s doesn't have processes that match '%s'", vm.vm_name, vm.trace)
            sys.exit(1)
        for pid, proc in re.findall(rf"(\d+).+? ({self.trace})", procs):
            log = f"{CVEX_TEMP_FOLDER_LINUX}/{self.vm_name}_strace_{proc}_{pid}.log"
            if log not in self.strace:
                runner = self.ssh.run_command(f"sudo strace -p {pid} -o {log} -v", is_async=True, until="attached")
                self.strace[log] = runner

    def stop_api_tracing(self, output_dir: str):
        for _, runner in self.strace.items():
            self.ssh.send_ctrl_c(runner)
        for log, _ in self.strace.items():
            out = f"{output_dir}/{log[len(CVEX_TEMP_FOLDER_LINUX) + 1:]}"
            self.ssh.download_file(out, log)
