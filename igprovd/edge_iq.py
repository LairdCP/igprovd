#
# edge_iq.py - Configuration handler for Edge IQ
#
import json
import requests
import subprocess
from requests.exceptions import RequestException
from syslog import syslog
import os
from .prov_exceptions import *

REQUEST_TIMEOUT = 30

GG_DIR = "/gg"
OPT_DIR = "/opt"

EDGEIQ_CONFIG = "/usr/bin/edge_iq_config"
EDGE_DOMAIN = "http://api.edgeiq.io/"
EDGE_FILE = "edge/edge"
ASSETS_FILE = "edge-assets-latest.tar.gz"
REMOTE_ASSETS_FILE = (
    "http://api.edgeiq.io/api/v1/platform/downloads/latest/edge-assets-latest.tar.gz"
)
REMOTE_EDGE_FILE = (
    "http://api.edgeiq.io/api/v1/platform/downloads/latest/edge-linux-arm7-latest"
)
BOOTSTRAP_CONFIG_FILE = "edge/conf/bootstrap.json"
CONF_CONFIG_FILE = "edge/conf/conf.json"
ESCROW_TOKEN_FILE = "edge/escrow_token"


def get_bootstrap_config(company_id):
    return {
        "company_id": company_id,
        "platform": "laird",
        "init_system": "systemd",
        "network_configurer": "nmcli",
        "local": False,
    }


def get_config(company_id, install_dir):
    return {
        "edge": {
            "manufacturer": "generic",
            "model": "linux",
            "company": company_id,
            "env": "prod",
            "local": False,
            "bypass_and_relay": False,
            "init_system": "systemd",
            "identifier_path": f"{install_dir}/edgeiq_bootstrap.json",
            "ui_port": 9001,
            "api_port": 9000,
        },
        "mqtt": {
            "broker": {
                "protocol": "ssl",
                "host": "mqtt.ms-io.com",
                "port": "443",
                "username": "edge",
                "password": "Dmn2LKZNcYSBd1PAbRMcmEKBG8EDpRjxc0BB5A==",
                "escrow_token_path": f"{install_dir}/{ESCROW_TOKEN_FILE}",
            },
            "topics": {
                "upstream": {
                    "report": "reports",
                    "heartbeat": "reports/hb",
                    "config": "config",
                    "action": "action",
                    "new_version": "new_version",
                    "lwt": "lwt",
                    "status": "status",
                    "log": "logs",
                    "gateway_command_status": "gateway_command_status",
                    "deployment_status": "deployment_status",
                    "error": "error",
                    "escrow_request": "escrow_request",
                },
                "downstream": {
                    "config": "config",
                    "command": "commands",
                    "new_version": "new_version",
                    "gateway_command": "gateway_commands",
                    "escrow": "escrow",
                },
            },
        },
        "platform": {"url": "https://api.edgeiq.io/api/v1/platform/"},
        "aws": {"greengrass": {"heartbeat_port": 9002}},
    }


class EdgeIQConfig:
    def __init__(self, update_state):
        self.update_state = update_state
        self.endpoint_url = None
        self.company_id = None
        self.escrow_token = None
        self.install_dir = GG_DIR if os.path.isdir(GG_DIR) else OPT_DIR
        self.bootstrap_config_file = f"{self.install_dir}/{BOOTSTRAP_CONFIG_FILE}"
        self.config_file = f"{self.install_dir}/{CONF_CONFIG_FILE}"
        self.assets_file = f"{self.install_dir}/{ASSETS_FILE}"
        self.edge_file = f"{self.install_dir}/{EDGE_FILE}"
        self.token_file = f"{self.install_dir}/{ESCROW_TOKEN_FILE}"

    #
    # popen_log() - Execute a process & log output; returns process resturn code
    #
    def popen_log(self, args):
        # Exec process, redirect stderr -> stdout, and buffer by line
        p = subprocess.Popen(
            args, bufsize=1, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        # Read stdout lines, until EOF
        l = p.stdout.readline()
        while l:
            out = l.rstrip()
            if out:
                syslog(out.decode("utf-8"))
            l = p.stdout.readline()
        return p.wait()

    #
    # download() - Download the file specified by url to the destination_file
    #
    def download(self, url, destination_file):
        r = requests.get(url, stream=True, timeout=REQUEST_TIMEOUT)
        # Raise any HTTP error
        r.raise_for_status()
        with open(destination_file, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:  # Skip keep-alive chunks
                    f.write(chunk)
        return

    #
    # start_core_download() - Download the Edge IQ assets and binary
    #
    def start_core_download(self):
        self.popen_log(["systemctl", "stop", "edge"])
        self.download(REMOTE_ASSETS_FILE, self.assets_file)
        self.popen_log(["tar", "xzvf", self.assets_file, "-C", self.install_dir])
        self.popen_log(["rm", self.assets_file])
        self.download(REMOTE_EDGE_FILE, self.edge_file)
        self.popen_log(
            [
                "rm",
                self.bootstrap_config_file,
                self.config_file,
            ]
        )

        # Create the bootstrap.json config
        bootstrap = json.loads(json.dumps(get_bootstrap_config(self.company_id)))
        with open(self.bootstrap_config_file, "w") as f:
            json.dump(bootstrap, f, indent=4)

        # Create the conf.json config
        conf = json.loads(json.dumps(get_config(self.company_id, self.install_dir)))
        with open(self.config_file, "w") as f:
            json.dump(conf, f, indent=4)

        # Create escrow token (if specified)
        if self.escrow_token is not None:
            syslog(
                "Writing {} to escrow token file {}".format(
                    self.escrow_token, self.token_file
                )
            )
            with open(self.token_file, "w") as f:
                f.write(self.escrow_token)

    #
    # perform_core_update() - Call the edge_iq_config script to finish
    # the Edge IQ installation.
    #
    def perform_core_update(self):
        result = self.popen_log([EDGEIQ_CONFIG, "install"])
        if result != 0:
            raise RuntimeError("Failed to install Edge IQ.")

    #
    # check_config() - Check the status of the Edge IQ configuration.
    # Returns True if Edge IQ is properly installed and configured,
    # otherwise False.  Raises exceptions to indicate unexpected failure.
    #
    def check_config(self):
        # Run config utility & log output
        result = self.popen_log([EDGEIQ_CONFIG, "check"])
        if result == 0:
            return True
        elif result == 1:
            return False
        else:
            raise RuntimeError("Unexpected failure checking configuration.")

    #
    # set_endpoint_url() - URL and company id should be seperated by a space.
    #
    def set_company_id_from_url(self, url):
        url_config = url.split("/")
        if len(url_config) < 4:
            raise ProvBadConfig("Missing url or company id")

        self.company_id = url_config[-1]

    #
    # has_edge_domain() - Check a url to see if it has the edge iq domain
    #
    def has_edge_domain(self, url):
        if EDGE_DOMAIN in url:
            return True
        else:
            return False
