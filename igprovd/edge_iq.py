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
from .prov_status import *
import tempfile
import stat

REQUEST_TIMEOUT = 30

GG_DIR = "/gg"
PERM_DIR = "/perm"
OPT_DIR = "/opt"

EDGEIQ_CONFIG = "/usr/bin/edge_iq_config"
EDGE_DOMAIN = "http://api.edgeiq.io/"
EDGECTL_NAME="edgectl"
EDGECTL_URL="https://api.edgeiq.io/api/v1/platform/edgectl/latest/edgectl-linux-armhf-latest"
ESCROW_TOKEN_FILE = "edge/escrow_token"

class EdgeIQConfig:
    def __init__(self, update_state):
        self.update_state = update_state
        self.endpoint_url = None
        self.company_id = None
        self.escrow_token = None
        if os.path.isdir(GG_DIR):
            self.install_dir = GG_DIR
        elif os.path.isdir(PERM_DIR):
            self.install_dir = PERM_DIR
        else:
            self.install_dir = OPT_DIR
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
    # start_core_download() - Download the Edge IQ installation utility
    #
    def start_core_download(self):
        self.update_state(PROV_INPROGRESS_DOWNLOADING)
        self.popen_log(["systemctl", "stop", "edge"])
        self.tmpdir = tempfile.mkdtemp()
        self.edgectl = self.tmpdir + "/" + EDGECTL_NAME
        self.download(EDGECTL_URL, self.edgectl)
        os.chmod(self.edgectl, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)

    #
    # perform_core_update() - Call the edgectl utility to perform
    # the Edge IQ installation.
    #
    def perform_core_update(self):
        self.update_state(PROV_INPROGRESS_APPLYING)
        install_cmd=f"{self.edgectl} install -p laird -t -d {self.install_dir} -c {self.company_id}"
        result = self.popen_log(install_cmd.split())
        os.remove(self.edgectl)
        if result != 0:
            raise RuntimeError("Failed to install Edge IQ.")
        # Create escrow token (if specified)
        if self.escrow_token is not None:
            syslog(
                "Writing {} to escrow token file {}".format(
                    self.escrow_token, self.token_file
                )
            )
            with open(self.token_file, "w") as f:
                f.write(self.escrow_token)
        result = self.popen_log([EDGEIQ_CONFIG, "install"])
        if result != 0:
            raise RuntimeError("Failed to install EdgeIQ.")

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
