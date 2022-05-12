#
# ProvService.py - Main Dbus provisioning service interface
#
import dbus
import dbus.service
import dbus.exceptions
import time
import threading
import requests
import requests.exceptions
import json
import random
from syslog import syslog
from .ggconfig import GGConfig
from .prov_exceptions import *
import subprocess
from .edge_iq import EdgeIQConfig

import sys

PYTHON3 = sys.version_info >= (3, 0)
if PYTHON3:
    from gi.repository import GObject as gobject
else:
    import gobject

#
# Provisioning status/states
#
PROV_COMPLETE_SUCCESS = 0
PROV_UNPROVISIONED = 1
PROV_INPROGRESS_DOWNLOADING = 2
PROV_INPROGRESS_APPLYING = 3
PROV_FAILED_INVALID = -1
PROV_FAILED_CONNECT = -2
PROV_FAILED_AUTH = -3
PROV_FAILED_TIMEOUT = -4
PROV_FAILED_NOT_FOUND = -5
PROV_FAILED_BAD_CONFIG = -6
PROV_FAILED_UNKNOWN = -7

# Network Manager D-Bus interfaces
NM_IFACE = "org.freedesktop.NetworkManager"
NM_OBJ = "/org/freedesktop/NetworkManager"
NM_DEVICE_IFACE = "org.freedesktop.NetworkManager.Device"
NM_WIRED_DEVICE_IFACE = "org.freedesktop.NetworkManager.Device.Wired"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"

# Network Manager Connectivity States
NM_CONNECTIVITY_UNKNOWN = 0
NM_CONNECTIVITY_NONE = 1
NM_CONNECTIVITY_PORTAL = 2
NM_CONNECTIVITY_LIMITED = 3
NM_CONNECTIVITY_FULL = 4

# rsync command to sync GG logs to be readable from the container
GG_LOG_RSYNC = [
    "rsync",
    "-rltmog",
    "--delete",
    "--chmod=Dug+rx,Fug+r",
    "--chown=ggc_user:ggc_group",
    "/gg/greengrass/ggc/var/log",
    "/gg",
]

# Config file for escrow mode
ESCROW_CONFIG_FILE = "/etc/escrow.cfg"


class ProvThread(object):
    def __init__(self, gg_config, callback, download=True, apply=True):
        self.gg_config = gg_config
        self._callback = callback
        self.result = PROV_COMPLETE_SUCCESS
        self.download = download
        self.apply = apply
        self.thread = threading.Thread(target=self.run)
        self.thread.start()
        self.thread_id = str(self.thread.ident)

    def run(self):
        syslog("Starting provisioning thread routine...")
        try:
            if self.download:
                self.gg_config.start_core_download()
            if self.apply:
                self.gg_config.perform_core_update()
        except requests.exceptions.ConnectionError:
            syslog("Configuration failed to connect.")
            self.result = PROV_FAILED_CONNECT
        except (requests.Timeout, requests.exceptions.Timeout):
            syslog("Configuration failed with timeout.")
            self.result = PROV_FAILED_TIMEOUT
        except requests.exceptions.HTTPError as h:
            syslog("Configuration failed with HTTP error: %s" % str(h))
            if (
                h.response.status_code == requests.codes.UNAUTHORIZED
                or h.response.status_code == requests.codes.FORBIDDEN
            ):
                syslog("Configuration failed authentication.")
                self.result = PROV_FAILED_AUTH
            elif h.response.status_code == requests.codes.NOT_FOUND:
                self.result = PROV_FAILED_NOT_FOUND
            else:
                self.result = PROV_FAILED_UNKNOWN
        except ProvBadConfig:
            syslog("Configuration failed due to bad configuration.")
            self.result = PROV_FAILED_BAD_CONFIG
        except ProvInvalid:
            syslog("Configuration failed due to invalid request parameters.")
            self.result = PROV_FAILED_INVALID
        except Exception as e:
            syslog(
                "Configuration failed with exception: %s: %s"
                % (type(e).__name__, str(e))
            )
            self.result = PROV_FAILED_UNKNOWN
        self._callback(self.result)


class ProvService(dbus.service.Object):
    def __init__(self, bus_name):
        super(ProvService, self).__init__(bus_name, "/com/lairdtech/IG/ProvService")
        # Create Greengrass configuration handler
        self.gg_config = GGConfig(self.update_state)
        self.edge_iq_config = EdgeIQConfig(self.update_state)
        self.greengrass_provisioned = False
        self.edge_iq_provisioned = False
        self.bus = dbus.SystemBus()
        self.nm = dbus.Interface(self.bus.get_object(NM_IFACE, NM_OBJ), NM_IFACE)
        self.nm_props = dbus.Interface(
            self.bus.get_object(NM_IFACE, NM_OBJ), DBUS_PROP_IFACE
        )
        self.nm_props.connect_to_signal("PropertiesChanged", self.nm_props_changed)
        self.nm_connectivity = self.nm_props.GetAll(NM_IFACE)["Connectivity"]
        self.escrow_timer = None
        self.escrow_prefix = "esc_"
        self.auto_install_min_sec = 0
        self.auto_install_max_sec = 0

        # Check current Greengrass configuration status
        syslog("Checking Greengrass configuration.")
        try:
            if self.gg_config.check_config():
                syslog("Greengrass is provisioned.")
                self.greengrass_provisioned = True
                self.result = PROV_COMPLETE_SUCCESS
            else:
                syslog("Greengrass is not provisioned.")
                self.result = PROV_UNPROVISIONED
        except Exception as e:
            syslog("Greengrass configuration check failed with exception: %s" % str(e))
            self.result = PROV_FAILED_INVALID

        # Check current Edge IQ configuration status
        syslog("Checking Edge IQ configuration.")
        try:
            if self.edge_iq_config.check_config():
                syslog("Edge IQ is provisioned.")
                self.edge_iq_provisioned = True
            else:
                syslog("Edge IQ is not provisioned.")
                self.check_escrow_install()
        except Exception as e:
            syslog("Edge IQ configuration check failed with exception: %s" % str(e))
            self.result = PROV_FAILED_INVALID

    @dbus.service.method(
        "com.lairdtech.IG.ProvInterface", in_signature="sa{sv}", out_signature="i"
    )
    def StartProvisioning(self, endpoint_url, auth_params):
        syslog("Provisioning started.")
        syslog("Endpoint URL: %s" % endpoint_url)
        try:
            # Save parameters
            if self.edge_iq_config.has_edge_domain(endpoint_url):

                # Check if the device is already provisioned for Edge IQ
                if self.edge_iq_provisioned == True:
                    syslog("Edge IQ already provisioned")
                    self.result = PROV_FAILED_INVALID
                else:
                    self.edge_iq_config.set_company_id_from_url(endpoint_url)
                    self.StateChanged(PROV_INPROGRESS_DOWNLOADING)
                    thread = ProvThread(self.edge_iq_config, self.StateChanged)
            else:

                # Check if the device is already provisioned for Greengrass
                if self.greengrass_provisioned == True:
                    syslog("Greengrass already provisioned")
                    self.result = PROV_FAILED_INVALID
                else:
                    self.gg_config.endpoint_url = endpoint_url
                    self.gg_config.clientcert = auth_params.get("clientcert")
                    self.gg_config.username = auth_params.get("username")
                    self.gg_config.password = auth_params.get("password")
                    self.StateChanged(PROV_INPROGRESS_DOWNLOADING)
                    thread = ProvThread(self.gg_config, self.StateChanged)

        except Exception as e:
            syslog("Configuration failed, exception = %s" % str(e))
            self.StateChanged(PROV_FAILED_INVALID)

        return self.result

    @dbus.service.method(
        "com.lairdtech.IG.ProvInterface", in_signature="sa{sv}", out_signature="i"
    )
    def StartCoreDownload(self, endpoint_url, auth_params):
        syslog("Start core download.")
        syslog("Endpoint URL: %s" % endpoint_url)
        # Save parameters
        self.gg_config.endpoint_url = endpoint_url
        self.gg_config.clientcert = auth_params.get("clientcert")
        self.gg_config.username = auth_params.get("username")
        self.gg_config.password = auth_params.get("password")

        try:
            self.StateChanged(PROV_INPROGRESS_DOWNLOADING)
            thread = ProvThread(self.gg_config, self.StateChanged, apply=False)
        except Exception as e:
            syslog("Configuration failed, exception = %s" % str(e))
            self.StateChanged(PROV_FAILED_INVALID)

        return self.result

    @dbus.service.method("com.lairdtech.IG.ProvInterface", out_signature="i")
    def PerformCoreUpdate(self):
        syslog("Performing core update")

        try:
            self.StateChanged(PROV_INPROGRESS_DOWNLOADING)
            thread = ProvThread(self.gg_config, self.StateChanged, download=False)
        except Exception as e:
            syslog("Configuration failed, exception = %s" % str(e))
            self.StateChanged(PROV_FAILED_INVALID)

        return self.result

    @dbus.service.method("com.lairdtech.IG.ProvInterface", out_signature="i")
    def GGLogSync(self):
        syslog("Syncing GG logs")
        result = 0
        try:
            subprocess.run(GG_LOG_RSYNC)
        except subprocess.SubprocessError as e:
            syslog("GG log sync failed, exception = %s" % str(e))
            result = -1

        return result

    @dbus.service.signal("com.lairdtech.IG.ProvInterface", signature="i")
    def StateChanged(self, result):
        syslog("Provisioning State Changed: %d" % result)
        self.result = result

        # Check current Greengrass configuration status
        syslog("Checking Greengrass configuration.")
        try:
            if self.gg_config.check_config():
                syslog("Greengrass is provisioned.")
                self.greengrass_provisioned = True
            else:
                syslog("Greengrass is not provisioned.")
                self.greengrass_provisioned = False
        except Exception as e:
            syslog("Greengrass configuration check failed with exception: %s" % str(e))
            self.greengrass_provisioned = False

        # Check current Edge IQ configuration status
        syslog("Checking Edge IQ configuration.")
        try:
            if self.edge_iq_config.check_config():
                syslog("Edge IQ is provisioned.")
                self.edge_iq_provisioned = True
            else:
                syslog("Edge IQ is not provisioned.")
                self.edge_iq_provisioned = False
        except Exception as e:
            syslog("Edge IQ configuration check failed with exception: %s" % str(e))
            self.edge_iq_provisioned = False

        return result

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface_name, property_name):
        return self.GetAll(interface_name)[property_name]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):
        if interface_name == "com.lairdtech.IG.ProvInterface":
            return {
                "Status": self.result,
                "GreengrassProvisioned": self.greengrass_provisioned,
                "EdgeIQProvisioned": self.edge_iq_provisioned,
            }
        else:
            raise dbus.exceptions.DBusException(
                "com.lairdtech.UnknownInterface",
                "The ProvService does not implement the %s interface" % interface_name,
            )

    def update_state(self, new_state):
        # Don't call DBus method on thread (unsafe!), schedule it
        # on the main loop
        gobject.timeout_add(0, self.handle_update, new_state)

    def handle_update(self, new_state):
        self.StateChanged(new_state)

    def nm_props_changed(self, iface, props_changed, props_invalidated):
        if props_changed and "Connectivity" in props_changed:
            self.nm_connectivity = props_changed["Connectivity"]
            syslog("Connectivity changed: {}".format(self.nm_connectivity))
            if (
                self.nm_connectivity == NM_CONNECTIVITY_FULL
                and self.auto_install_min_sec > 0
            ):
                syslog("Connectivity established, scheduling escrow install.")
                self.schedule_escrow_install()
            elif (
                self.nm_connectivity != NM_CONNECTIVITY_FULL
                and self.escrow_timer is not None
            ):
                syslog("Connectivity dropped, cancelling escrow install.")
                gobject.source_remove(self.escrow_timer)

    def get_eth0_addr(self):
        eth0_dev_obj = self.bus.get_object(NM_IFACE, self.nm.GetDeviceByIpIface("eth0"))
        syslog("eth0_dev_obj={}".format(eth0_dev_obj))
        eth0_props = dbus.Interface(eth0_dev_obj, DBUS_PROP_IFACE)
        syslog("eth0_props={}".format(eth0_props))
        eth0_addr = eth0_props.GetAll(NM_WIRED_DEVICE_IFACE)["PermHwAddress"].lower()
        syslog("eth0_addr={}".format(eth0_addr))
        return eth0_addr

    def start_escrow_install(self):
        # Check again in case EdgeIQ was manually installed
        if self.edge_iq_config.check_config():
            syslog("EdgeIQ was manually installed, cancelling escrow install.")
            self.self.auto_install_min_sec = 0  # Prevent rescheduling
            return False
        syslog("Setting company id")
        self.edge_iq_config.company_id = self.company_id
        syslog("Getting eth0 address")
        eth0_addr = self.get_eth0_addr().replace(":", "")
        # Set escrow token from eth0, e.g., "esc_c0ee40dead01"
        syslog("Setting escrow token ID")
        self.edge_iq_config.escrow_token = f"{self.escrow_prefix}{eth0_addr}"
        syslog("Starting escrow install thread")
        thread = ProvThread(self.edge_iq_config, self.StateChanged)

    def schedule_escrow_install(self):
        delay_sec = random.randrange(
            self.auto_install_min_sec, self.auto_install_max_sec
        )
        syslog(
            "Scheduling escrow install for {} in {} sec".format(
                self.company_id, delay_sec
            )
        )
        self.escrow_timer = gobject.timeout_add(
            delay_sec * 1000, self.start_escrow_install
        )

    def check_escrow_install(self):
        try:
            with open(ESCROW_CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                self.auto_install_min_sec = cfg["auto_install_min_sec"]
                self.auto_install_max_sec = cfg["auto_install_max_sec"]
                self.escrow_prefix = cfg.get("escrow_prefix", "esc_")
                self.company_id = cfg["company_id"]
                # Schedule installation or await full connectivity
                if self.nm_connectivity == NM_CONNECTIVITY_FULL:
                    self.schedule_escrow_install()
                else:
                    syslog("Awaiting connectivity for escrow install.")
        except Exception as e:
            syslog(
                "Could not read escrow configuration: {}: {}".format(
                    type(e).__name__, e
                )
            )
