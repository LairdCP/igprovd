#
# ggconfig.py - Configuration handler for Greengrass
#
import requests
import dbus, dbus.exceptions
from requests.exceptions import RequestException
from syslog import syslog
import tempfile, shutil, os, json
import subprocess
from base64 import b64decode
from .prov_exceptions import *

import sys

from urllib.parse import urljoin

IGUPD_SVC = "com.lairdtech.security.UpdateService"
IGUPD_IFACE = "com.lairdtech.security.UpdateInterface"
IGUPD_OBJ = "/com/lairdtech/security/UpdateService"

DEVICE_SVC = "com.lairdtech.device.DeviceService"
DEVICE_PUB_IFACE = "com.lairdtech.device.public.DeviceInterface"
DEVICE_OBJ = "/com/lairdtech/device/DeviceService"

# List of identifiers from the Device Service that have Bluetooth hardware
DEVICE_IDS_BT_HW = [1]

# Valid MQTT, HTTP port values
VALID_MQTT_PORTS = [8883, 443]
VALID_HTTP_PORTS = [8443, 443]
VALID_LOCAL_PORT_MIN = 1024
VALID_LOCAL_PORT_MAX = 65535

IGCONFD_SVC = "com.lairdtech.security.ConfigService"
IGCONFD_IFACE = "com.lairdtech.security.ConfigInterface"
IGCONFD_OBJ = "/com/lairdtech/security/ConfigService"


def set_schedule(t_config):
    bus = dbus.SystemBus()
    igupd = dbus.Interface(bus.get_object(IGUPD_SVC, IGUPD_OBJ), IGUPD_IFACE)
    syslog("igprovd: set_schedule: Connected to IGUPD Dbus Object")
    return igupd.SetConfiguration(t_config)


def set_wireless_config(config):
    bus = dbus.SystemBus()
    igconfd = dbus.Interface(bus.get_object(IGCONFD_SVC, IGCONFD_OBJ), IGCONFD_IFACE)
    syslog("igconfd: set_schedule: Connected to IGCONFD Dbus Object")
    return igconfd.SetWifiConfigurations(config)


RESFILE_NAME = "setup.tar.gz"
CFGFILE_PATH = "config/config.json"
ROOTCA_PATH = "certs/root.ca.pem"
FW_NAME = "fw.uwf"
REQUEST_TIMEOUT = 30
CONFIG_MAX_SIZE = 1024 * 1024  # 1MB


class GGConfig:
    DST_CORE_NAME = "ggcore.tar.gz"
    CLIENT_CERT_NAME = "client.crt"
    GGCONF = "/usr/bin/ggconf"
    DEVICE_CERTFILE = "/etc/ssl/misc/dev.crt"

    def __init__(self, update_state):
        self.update_state = update_state
        self.endpoint_url = None
        self.clientcert = None
        self.certfile = None
        self.username = None
        self.password = None
        self.tmpdir = None
        self.core_thing = None
        self.gg = None

    #
    # get_tmpfile() - Return temp filename
    #
    def tmpfilename(self, basename):
        return self.tmpdir + "/" + basename

    #
    # http_download() - Download a file into a destination on the
    # filesystem, using streaming.  Returns nothing if successful,
    # failures are indicated by raising exceptions.
    #
    def http_download(self, url, destfile):
        if self.certfile:
            r = requests.get(
                url, stream=True, timeout=REQUEST_TIMEOUT, cert=self.certfile
            )
        elif self.username and self.password:
            r = requests.get(
                url,
                stream=True,
                timeout=REQUEST_TIMEOUT,
                auth=(self.username, self.password),
            )
        else:
            raise ProvInvalid(
                "Invalid authentication (username and passord or certificates)"
            )
        # Raise any HTTP error
        r.raise_for_status()
        with open(destfile, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:  # Skip keep-alive chunks
                    f.write(chunk)
        return

    #
    # http_read_config() - Read a configuration document (JSON) via HTTP
    # Returns a dictionary representing the JSON document if successful;
    # failures are indicated by raising exceptions.
    #
    def http_read_config(self, url):
        if self.certfile:
            r = requests.get(
                url, stream=True, timeout=REQUEST_TIMEOUT, cert=self.certfile
            )
        elif self.username and self.password:
            r = requests.get(
                url,
                stream=True,
                timeout=REQUEST_TIMEOUT,
                auth=(self.username, self.password),
            )
        else:
            raise ProvInvalid(
                "Invalid authentication (username and passord or certificates)"
            )
        # Raise any HTTP error
        r.raise_for_status()
        # Download content up to maximum size
        content = r.raw.read(CONFIG_MAX_SIZE, decode_content=True)
        # Return JSON as dictionary object (raises ValueError if not JSON)
        return json.loads(content.decode("utf-8"))

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
    # popen_write() - Execute a process & write output; returns process resturn code
    #
    def popen_write(self, args, fname):
        with open(fname, "w") as f:
            # Exec process, redirect stderr -> stdout, and buffer by line
            p = subprocess.Popen(
                args, bufsize=1, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            # Read stdout lines, until EOF
            l = p.stdout.readline()
            while l:
                f.write(l.decode("utf-8"))
                l = p.stdout.readline()
            return p.wait()

    #
    # check_config() - Check the status of the Greengrass configuration.
    # Returns True if Greengrass is properly installed and configured,
    # otherwise False.  Raises exceptions to indicate unexpected failure.
    #
    def check_config(self):
        # Run ggconf utility & log output
        result = self.popen_log([self.GGCONF, "check"])
        if result == 0:
            return True
        elif result == 1:
            return False
        else:
            raise RuntimeError("Unexpected failure checking configuration.")

    #
    # download_gg_core() - Download and verify the Greengrass core tarball.
    # Returns nothing if successful, failures are indicated via exceptions.
    #
    def download_gg_core(self, corefilename, signature, certfile):
        SIGFILE_NAME = "sig.bin"
        PUBKEY_NAME = "pub.key"
        syslog(
            "Downloading core tarball to {}".format(
                self.tmpfilename(self.DST_CORE_NAME)
            )
        )
        self.http_download(
            urljoin(self.endpoint_url, corefilename),
            self.tmpfilename(self.DST_CORE_NAME),
        )
        # Decode signature from Base64 string into binary file
        with open(self.tmpfilename(SIGFILE_NAME), "wb") as f:
            f.write(b64decode(signature))
        syslog("Verifying core signature.")
        # Extract public key from certificate with OpenSSL
        self.popen_write(
            ["openssl", "x509", "-in", certfile, "-pubkey", "-noout"],
            self.tmpfilename(PUBKEY_NAME),
        )
        # Verify tarball signature with OpenSSL
        ret = self.popen_log(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-verify",
                self.tmpfilename(PUBKEY_NAME),
                "-signature",
                self.tmpfilename(SIGFILE_NAME),
                self.tmpfilename(self.DST_CORE_NAME),
            ]
        )
        if ret != 0:
            raise ProvBadConfig("Core signature verification failed.")

    #
    # prune_config() - Prune the configuration JSON for security
    # and correct configuration.
    #
    def prune_config(self, cfg_path):
        with open(cfg_path, "r") as f:
            cfg = json.load(f)
        # Create new configuration, copying only the 'coreThing' element.
        # This removes any other top-level elements that we don't want.
        cfg_new = {"coreThing": cfg["coreThing"]}
        # Create new 'runtime' element; enable systemd.
        cfg_new["runtime"] = {"cgroup": {"useSystemd": "yes"}}
        # Write the modified configuration

        # Validate and transfer port information to the sanitized config
        if self.core_thing is not None:
            # Note: ValueError is caught and reported as bad config
            if "iotMqttPort" in self.core_thing["coreThing"]:
                if (
                    not int(self.core_thing["coreThing"]["iotMqttPort"])
                    in VALID_MQTT_PORTS
                ):
                    raise ProvBadConfig("Invalid iotMqttPort.")
                cfg_new["coreThing"]["iotMqttPort"] = self.core_thing["coreThing"][
                    "iotMqttPort"
                ]
            if "iotHttpPort" in self.core_thing["coreThing"]:
                if (
                    not int(self.core_thing["coreThing"]["iotHttpPort"])
                    in VALID_HTTP_PORTS
                ):
                    raise ProvBadConfig("Invalid iotHttpPort.")
                cfg_new["coreThing"]["iotHttpPort"] = self.core_thing["coreThing"][
                    "iotHttpPort"
                ]
            if "ggMqttPort" in self.core_thing["coreThing"]:
                ggMqttPort = int(self.core_thing["coreThing"]["ggMqttPort"])
                if (
                    ggMqttPort < VALID_LOCAL_PORT_MIN
                    or ggMqttPort > VALID_LOCAL_PORT_MAX
                ):
                    raise ProvBadConfig("Invalid ggMqttPort.")
                cfg_new["coreThing"]["ggMqttPort"] = self.core_thing["coreThing"][
                    "ggMqttPort"
                ]
            if "ggHttpPort" in self.core_thing["coreThing"]:
                if (
                    not int(self.core_thing["coreThing"]["ggHttpPort"])
                    in VALID_HTTP_PORTS
                ):
                    raise ProvBadConfig("Invalid ggHttpPort.")
                cfg_new["coreThing"]["ggHttpPort"] = self.core_thing["coreThing"][
                    "ggHttpPort"
                ]
            if "keepAlive" in self.core_thing["coreThing"]:
                cfg_new["coreThing"]["keepAlive"] = self.core_thing["coreThing"][
                    "keepAlive"
                ]

        syslog("Config after pruning: " + str(cfg_new))
        with open(cfg_path, "w") as f:
            json.dump(cfg_new, f, sort_keys=True, indent=2, separators=(",", ": "))

    def bt_hw_present(self):
        devicesvc = dbus.Interface(
            dbus.SystemBus().get_object(DEVICE_SVC, DEVICE_OBJ), DEVICE_PUB_IFACE
        )
        return devicesvc.Identify() in DEVICE_IDS_BT_HW

    def start_core_download(self):
        fw_url = None
        cur_dir = os.getcwd()
        try:
            self.tmpdir = tempfile.mkdtemp()

            os.chdir(self.tmpdir)

            if self.clientcert:
                self.certfile = self.tmpfilename(self.CLIENT_CERT_NAME)
                syslog("Storing client cert in %s" % self.certfile)
                with open(self.certfile, "w") as f:
                    f.write(self.clientcert)
            syslog("Reading configuration from %s" % self.endpoint_url)
            # Get top-level configuration document
            cfg = self.http_read_config(self.endpoint_url)

            # check if configuration consist information about update
            if "update" in cfg:
                if (
                    "update_schedule" in cfg["update"]
                    or "download_schedule" in cfg["update"]
                ):
                    for k, v in cfg["update"].items():
                        syslog("key : {} value: {}".format(k, v))
                    if set_schedule(json.dumps(cfg["update"])) == -1:
                        raise ProvBadConfig("Failed to set up update schedule")
                    else:
                        syslog("igprovd: Update schedule successfully modified")

            # check if config should apply BL654 firmware
            if self.bt_hw_present() and "bl654" in cfg:
                fw_name = cfg["bl654"].get("firmware")
                if fw_name:
                    fw_url = urljoin(self.endpoint_url, fw_name)

            if "coreThing" in cfg:
                self.core_thing = {"coreThing": cfg["coreThing"]}

            # check if configuration consists of information about wireless
            if "updateAPS" in cfg:
                if set_wireless_config(json.dumps(cfg["updateAPS"])) == -1:
                    raise RuntimeError("Failed to set up wireless configs")
                else:
                    syslog("igprovd: Wireless configs successfully modified")

            # Extract Greengrass-specific configuration
            self.gg = cfg["greengrass"]
            # TODO: Verify core version
            syslog("Downloading resources.")
            self.update_state(provsvc.PROV_INPROGRESS_DOWNLOADING)

            # User could update the core without a new resource file
            if "resourceFile" in self.gg:
                self.http_download(
                    urljoin(self.endpoint_url, self.gg["resourceFile"]),
                    self.tmpfilename(RESFILE_NAME),
                )

            self.download_gg_core(
                self.gg["coreFile"], self.gg["coreSignature"], self.DEVICE_CERTFILE
            )
            if fw_url:
                syslog("Downloading BT firmware image.")
                self.http_download(fw_url, self.tmpfilename(FW_NAME))

            # Download root CA cert
            syslog("Downloading root CA certificate from %s" % self.gg["rootCACert"])
            os.mkdir(self.tmpfilename("certs"))
            self.http_download(self.gg["rootCACert"], self.tmpfilename(ROOTCA_PATH))
        except (Exception, IOError) as e:
            syslog("Unexpected exception while downloading: %s" % str(e))
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            raise e
        finally:
            syslog("Cleaning up.")
            os.chdir(cur_dir)

    def perform_core_update(self):
        try:
            cur_dir = os.getcwd()
            os.chdir(self.tmpdir)
            # If this is a core only update we don't may not need the resource files
            if "resourceFile" in self.gg:
                syslog("Extracting configuration files.")
                self.update_state(provsvc.PROV_INPROGRESS_APPLYING)
                self.popen_log(["tar", "xzvf", self.tmpfilename(RESFILE_NAME)])
                # Whitelist the Greengrass configuration JSON
                self.prune_config(self.tmpfilename(CFGFILE_PATH))
                result = self.popen_log([self.GGCONF, "install", self.tmpdir])
            else:
                result = self.popen_log(
                    [self.GGCONF, "install", self.tmpdir, "core-only"]
                )
            # Call external install script
            syslog("Installing Greengrass files.")
            if result != 0:
                raise RuntimeError("Failed to install Greengrass.")
            # TODO: Whitelist configuration file from resources and
            # merge in changes from cloud configuration
        except ValueError as i:
            syslog("Unexpected Value Error")
            raise ProvBadConfig("Value Error")
        except (Exception, IOError) as e:
            syslog("Unexpected exception while installing: %s" % str(e))
            result = self.popen_log([self.GGCONF, "restore", self.tmpdir])
            raise e
        finally:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            os.chdir(cur_dir)
