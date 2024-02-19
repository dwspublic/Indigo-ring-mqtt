#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################

import logging
import json
import datetime
import base64
import os
try:
    import indigo
except ImportError:
    pass

import sys
import paho.mqtt.client as mqtt

from pathlib import Path

import asyncio

import threading

import time

from ring_doorbell import Auth, AuthenticationError, Requires2FAError, Ring, RingEvent
from ring_doorbell.const import CLI_TOKEN_FILE, PACKAGE_NAME, USER_AGENT
from ring_doorbell.listen import can_listen
SNAPSHOT_ENDPOINT = "/clients_api/snapshots/image/{0}"
SNAPSHOT_TIMESTAMP_ENDPOINT = "/clients_api/snapshots/timestamps"

RINGMQTT_MESSAGE_TYPE = "##ring##"
kCurDevVersCount = 0  # current version of plugin devices


################################################################################
class Plugin(indigo.PluginBase):

    ########################################
    # Main Plugin methods
    ########################################
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)

        pfmt = logging.Formatter('%(asctime)s.%(msecs)03d\t[%(levelname)8s] %(name)20s.%(funcName)-25s%(msg)s', datefmt='%Y-%m-%d %H:%M:%S')
        self.plugin_file_handler.setFormatter(pfmt)
        self.logLevel = int(self.pluginPrefs.get("logLevel", logging.INFO))
        self.indigo_log_handler.setLevel(self.logLevel)
        self.logger.debug(f"logLevel = {self.logLevel}")
        self.brokerID = pluginPrefs.get("brokerID", "0")

        self._event_loop = None
        self._async_thread = None
        self.credentials = pluginPrefs.get("credentials", "")

        self.ringmqtt_devices = []
        self.ringpyapi_devices = []
        self.ring_devices = eval(pluginPrefs.get("ring_devices", "{}"))
        self.ring_battery_devices = eval(pluginPrefs.get("ring_battery_devices", "{}"))

        self.mqttPlugin = indigo.server.getPlugin("com.flyingdiver.indigoplugin.mqtt")

        self.old_version = self.pluginPrefs.get("version", "0.0.0")
        if self.old_version != self.pluginVersion:
            self.logger.debug(f"Upgrading plugin from version {self.old_version} to {self.pluginVersion}")
            self.pluginPrefs["version"] = self.pluginVersion

        self.MQTT_SERVER = ''
        self.MQTT_PORT = 0
        self.protocol = 4
        self.transport = "tcp"
        self.topicList = {}
        self.client = mqtt.Client(client_id=f"indigo-ring-mqtt", clean_session=True, userdata=None,
                                  protocol=self.protocol, transport=self.transport)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message
        self.connected = False

        self.sentMessages = []
        self.LastConnectionWarning = datetime.datetime.now() - datetime.timedelta(days=1)

        self.MQTTConnectorPlugin = False
        self.MQTTConnectorDeviceId = 0
        self.PyAPIConnectorDeviceId = 0


    def startup(self):
        self.logger.info("Starting ringmqtt")
        self._event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._event_loop)
        self._async_thread = threading.Thread(target=self._run_async_thread)
        self._async_thread.start()


    def shutdown(self):
        self.logger.info("Stopping ringmqtt")
        self.pluginPrefs["ring_devices"] = str(self.ring_devices)
        self.pluginPrefs["ring_battery_devices"] = str(self.ring_battery_devices)
        self.pluginPrefs["credentials"] = self.credentials

    def runConcurrentThread(self):
        try:
            while True:
                if self.PyAPIConnectorDeviceId != 0:
                    self.pyapiUpdateDevices()
                if not self.MQTTConnectorPlugin:
                    if self.MQTTConnectorDeviceId != 0 and not self.connected:
                        self.connectToMQTTBroker()

                self.sleep(60)  # in seconds
        except self.StopThread:
            # do any cleanup here
            pass
    def message_handler(self, notification):
        self.logger.debug(f"message_handler: MQTT message {notification['message_type']} from brokerId: {self.brokerID}")
        self.processMessageNotification(notification)

    def deviceStartComm(self, device):
        self.logger.info(f"{device.name}: Starting Device")
        if device.deviceTypeId == "APIConnector":
            device.updateStateOnServer(key="status", value="Not Connected")
            self.PyAPIConnectorDeviceId = 0
            self.ringtoken = device.pluginProps["ringtoken"]
            if self.ringtoken != "":
                auth = Auth("YourProject/1.0", json.loads(self.ringtoken), self.token_updated)
                self.ring = Ring(auth)
                self.PyAPIConnectorDeviceId = device.id
                try:
                    self.pyapiDeviceCache()
                except:
                    self.logger.error(f"PyAPI Connector Failure")
                    self.PyAPIConnectorDeviceId = 0
                else:
                    self.pyapilisten = True
                    self._event_loop.create_task(self._async_start())
                    device.updateStateOnServer(key="status", value="Connected")
            else:
                self.pyapilisten = False
            return
        if device.deviceTypeId == "MQTTConnector":
            if self.MQTTConnectorPlugin:
                if not self.mqttPlugin.isEnabled():
                    self.logger.error(f"startup - MQTT Connector plugin not enabled!")
                    return
                if (self.brokerID == "0" or self.brokerID == ""):
                    self.logger.error(f'startup - The MQTT Broker (brokerID) must be set for MQTT Connector to be functional! {self.brokerID} : {self.pluginPrefs.get("brokerID", "0")}')
                    return
                indigo.server.subscribeToBroadcast("com.flyingdiver.indigoplugin.mqtt", "com.flyingdiver.indigoplugin.mqtt-message_queued", "message_handler")
                self.connected = True
            else:
                self.MQTT_SERVER = device.address
                self.MQTT_PORT = int(device.pluginProps["port"])
                self.username = device.pluginProps["username"]
                self.password = device.pluginProps["password"]
                self.topicList = device.pluginProps["subscriptions"]
                self.MQTTConnectorDeviceId = device.id
                try:
                    self.connectToMQTTBroker()
                    self.sleep(2)
                except:
                    self.logger.error(f"MQTT Connector Failure")
                    device.updateStateOnServer(key="status", value="Not Connected")
                    self.MQTTConnectorDeviceId = 0
            if self.connected:
                self.MQTTConnectorDeviceId = device.id
                device.updateStateOnServer(key="status", value="Connected")
                if self.pluginPrefs.get("startupHADiscovery", False):
                    brokerID = int(self.brokerID)
                    self.publish_topic(brokerID, "HA_Discovery", f"hass/status", "online")
            else:
                device.updateStateOnServer(key="status", value="Not Connected")
                self.MQTTConnectorDeviceId = 0
            return
        if device.id not in self.ringmqtt_devices and device.pluginProps["apitype"] == "mqtt":
            self.ringmqtt_devices.append(device.id)
        if device.id not in self.ringpyapi_devices and device.pluginProps["apitype"] == "pyapi":
            self.ringpyapi_devices.append(device.id)
        if not device.errorState:
            if device.deviceTypeId == "RingLight":
                device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOff)
            elif device.deviceTypeId == "RingCamera":
                device.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
                device.updateStateOnServer(key="state", value="Not Connected")
            else:
                device.updateStateImageOnServer(indigo.kStateImageSel.Auto)
            if device.states["batteryLevel2"] == "":
                device.updateStateOnServer(key="batteryLevel2", value="N/A")
            device.stateListOrDisplayStateIdChanged()

        instanceVers = int(device.pluginProps.get('devVersCount', 0))
        if instanceVers == kCurDevVersCount:
            self.logger.debug(f"{device.name}: Device is current version: {instanceVers}")
        elif instanceVers < kCurDevVersCount:
            newProps = device.pluginProps
            newProps["devVersCount"] = kCurDevVersCount
            device.replacePluginPropsOnServer(newProps)
            self.logger.debug(f"{device.name}: Updated device version: {instanceVers} -> {kCurDevVersCount}")
        else:
            self.logger.warning(f"{device.name}: Invalid device version: {instanceVers}")

    def deviceStopComm(self, device):
        self.logger.info(f"{device.name}: Stopping Device")
        if device.deviceTypeId == "APIConnector":
            if self.ringtoken != device.pluginProps["ringtoken"]:
                self.logger.info("Ring Token Updated")
                newProps = device.pluginProps
                device.pluginProps["ringtoken"] = self.ringtoken
                device.replacePluginPropsOnServer(newProps)
            self.PyAPIConnectorDeviceId = 0
            return
        if device.deviceTypeId == "MQTTConnector":
            self.MQTTConnectorDeviceId = 0
            return
        if device.id in self.ringmqtt_devices:
            self.ringmqtt_devices.remove(device.id)
            self.deviceRingCacheCheck(device.id)

    def token_updated(self, token):
        self.ringtoken = json.dumps(token)

    def pyapiDeviceCache(self):
        self.logger.debug(f"pyapiDeviceCache: Retrieve Ring devices through api and update cache")

        if self.PyAPIConnectorDeviceId != 0:
            self.ring.update_data()
            devices = self.ring.devices()

            for dev in list(devices['stickup_cams'] + devices['chimes'] + devices['doorbots']):
                dev.update_health_data()

                if hasattr(dev, 'motion_detection'):
                    self.ring_devices[dev._attrs["location_id"] + "-MA-" + dev.device_id] = [dev.name, "Ring", dev.model, dev.device_id, dev._attrs["location_id"], "RingMotion", "pyapi"]
                if hasattr(dev, 'siren'):
                    self.ring_devices[dev._attrs["location_id"] + "-SA-" + dev.device_id] = [dev.name, "Ring", dev.model, dev.device_id, dev._attrs["location_id"], "RingSiren", "pyapi"]
                if hasattr(dev, 'lights'):
                    self.ring_devices[dev._attrs["location_id"] + "-LA-" + dev.device_id] = [dev.name, "Ring", dev.model, dev.device_id, dev._attrs["location_id"], "RingLight", "pyapi"]
                if dev.family == "chimes":
                    self.ring_devices[dev._attrs["location_id"] + "-ZA-" + dev.device_id] = [dev.name, "Ring", dev.model, dev.device_id, dev._attrs["location_id"], "RingZChime", "pyapi"]
                if dev.family == "stickup_cams" or dev.family == "doorbots":
                    self.ring_devices[dev._attrs["location_id"] + "-CA-" + dev.device_id] = [dev.name, "Ring", dev.model, dev.device_id, dev._attrs["location_id"], "RingCamera", "pyapi"]
                if dev.family == "doorbots":
                    self.ring_devices[dev._attrs["location_id"] + "-DA-" + dev.device_id] = [dev.name, "Ring", dev.model, dev.device_id, dev._attrs["location_id"], "RingDoorbell", "pyapi"]
                if dev.has_capability("battery"):
                    self.ring_battery_devices[dev._attrs["location_id"] + "-" + dev.device_id] = [dev.name, "Ring", dev.model, dev.device_id, dev._attrs["location_id"], "Battery", "pyapi"]

            groups = self.ring.groups()

            for group in groups:
                dev = groups[group]

                if hasattr(dev, 'motion_detection'):
                    self.ring_devices[dev.location_id + "-MA-" + dev.device_id] = [dev.name, "Ring", dev.model, dev.device_id, dev.location_id, "RingMotion", "pyapi"]
                if hasattr(dev, 'lights'):
                    self.ring_devices[dev.location_id + "-LA-" + dev.device_id] = [dev.name, "Ring", dev.model, dev.device_id, dev.location_id, "RingLight", "pyapi"]

    def pyapiUpdateDevices(self):
        self.logger.debug(f"pyapiUpdateDevices: Retrieve Ring devices through api and update indigo device states")

        if self.PyAPIConnectorDeviceId != 0:
            self.ring.update_data()
            devices = self.ring.devices()

            for dev in list(devices['stickup_cams'] + devices['chimes'] + devices['doorbots']):
                dev.update_health_data()

                for deviceid in self.ringpyapi_devices:
                    device = indigo.devices[deviceid]

                    if self.ring_devices[device.address][4] == dev._attrs["location_id"] and self.ring_devices[device.address][3] == dev.device_id:
                        #device.updateStateOnServer(key="lastUpdate", value=str(datetime.datetime.now()))
                        health = dev._attrs["health"]
                        settings = dev._attrs["settings"]
                        if "last_update_time" in health:
                            device.updateStateOnServer(key="lastUpdate", value=str(datetime.datetime.fromtimestamp(health["last_update_time"])))
                        if device.deviceTypeId == "RingMotion":
                            if hasattr(dev, "motion_detection"):
                                if dev.motion_detection:
                                    device.updateStateOnServer(key="motionDetectionEnabled", value="ON")
                                else:
                                    device.updateStateOnServer(key="motionDetectionEnabled", value="OFF")
                                #if dev.motion_snooze == "true":
                                #    device.updateStateOnServer(key="motionSnooze", value="ON")
                                #else:
                                #    device.updateStateOnServer(key="motionSnooze", value="OFF")
                        if device.deviceTypeId == "RingMotion" or device.deviceTypeId == "RingDoorbell" or device.deviceTypeId == "RingSiren":
                            device.updateStateOnServer(key="onOffState", value=False)
                        if device.deviceTypeId == "RingCamera":
                            device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)
                            device.updateStateOnServer(key="state", value="Connected")
                            if self.pluginPrefs.get("storeSnapShots", False):
                                if self.pluginPrefs.get("snapshotImagePath", "") == "":
                                    tpath = indigo.server.getInstallFolderPath() + '/Web Assets/public/ring/'
                                    device.updateStateOnServer(key="snapshot_image",
                                                               value="http://localhost:8176/public/ring/" + device.address + ".jpg")
                                else:
                                    tpath = self.pluginPrefs.get("snapshotImagePath", "")
                                    device.updateStateOnServer(key="snapshot_image",
                                                               value=tpath + device.address + ".jpg")
                                if os.path.isdir(tpath):
                                    payloadimage = self.get_snapshot(dev)
                                    if type(payloadimage) == bytes:
                                        test_file = open(tpath + device.address + '.jpg', 'wb')
                                        test_file.write(payloadimage)
                                        test_file.close()
                                        device.updateStateOnServer(key="snapshot_timestamp", value=str(datetime.datetime.now()))
                                        device.updateStateOnServer(key="snapshot_type", value="Unknown")
                            if dev.last_recording_id != device.states["event_eventId1"]:
                                device.updateStateOnServer(key="event_recordingUrl3", value=device.states["event_recordingUrl2"])
                                device.updateStateOnServer(key="event_eventId3", value=device.states["event_eventId2"])
                                device.updateStateOnServer(key="event_recordingUrl2", value=device.states["event_recordingUrl1"])
                                device.updateStateOnServer(key="event_eventId2", value=device.states["event_eventId1"])
                                device.updateStateOnServer(key="event_recordingUrl1", value=dev.recording_url(dev.last_recording_id))
                                device.updateStateOnServer(key="event_eventId1", value=dev.last_recording_id)
                        if device.deviceTypeId == "RingLight":
                            if dev.lights == "on":
                                device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOn)
                                device.updateStateOnServer(key="onOffState", value=True)
                            else:
                                device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOff)
                                device.updateStateOnServer(key="onOffState", value=False)
                            if "floodlight_settings" in settings:
                                p = settings["floodlight_settings"]
                                device.updateStateOnServer(key="brightness_state", value=p["brightness"])
                        if device.deviceTypeId == "RingZChime":
                            if "volume" in settings:
                                device.updateStateOnServer(key="volume", value=str(settings["volume"]))
                            if "connected" in health and health["connected"]:
                                device.updateStateOnServer(key="status", value="online")
                            device.updateStateOnServer(key="play_ding_sound", value="Turn On")
                            device.updateStateOnServer(key="play_motion_sound", value="Turn Off")
                            do_not_disturb = dev._attrs["do_not_disturb"]
                            if "seconds_left" in do_not_disturb and do_not_disturb["seconds_left"] > 0:
                                device.updateStateOnServer(key="snooze_minutes_remaining", value=str(int(do_not_disturb["seconds_left"]/60)))
                                device.updateStateOnServer(key="snooze", value="ON")
                                device.updateStateOnServer(key="snooze_minutes", value="Unknown")
                            else:
                                device.updateStateOnServer(key="snooze", value="OFF")
                                device.updateStateOnServer(key="snooze_minutes", value="0")
                                device.updateStateOnServer(key="snooze_minutes_remaining", value="0")
                        if hasattr(dev, "connection_status"):
                            device.updateStateOnServer(key="status", value=dev.connection_status)
                        if hasattr(dev, "firmware"):
                            device.updateStateOnServer(key="firmwareStatus", value=dev.firmware)
                        if "battery_life_2" in dev._attrs or "battery_life" in dev._attrs:
                            if "battery_life_2" in dev._attrs:
                                if int(dev._attrs["battery_life_2"]) > int(dev._attrs["battery_life"]) and self.pluginPrefs.get("batterystateUI", False):
                                    b1 = dev._attrs["battery_life_2"]
                                    b2 = dev._attrs["battery_life"]
                                else:
                                    b1 = dev._attrs["battery_life"]
                                    b2 = dev._attrs["battery_life_2"]
                            else:
                                b1 = dev._attrs["battery_life"]
                                b2 = "N/A"
                            device.updateStateOnServer(key="batteryLevel", value=b1)
                            device.updateStateOnServer(key="batteryLevel2", value=b2)

            groups = self.ring.groups()

            for group in groups:
                dev = groups[group]

                for deviceid in self.ringpyapi_devices:
                    device = indigo.devices[deviceid]

                    if self.ring_devices[device.address][4] == dev.location_id and self.ring_devices[device.address][3] == dev.device_id:
                        self.logger.debug(f"Group: {dev.device_id}")
                        device.updateStateOnServer(key="lastUpdate", value=str(datetime.datetime.now()))
                        if device.deviceTypeId == "RingLight":
                            if dev.lights:
                                device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOn)
                                device.updateStateOnServer(key="onOffState", value=True)
                            else:
                                device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOff)
                                device.updateStateOnServer(key="onOffState", value=False)

    def processMessageNotification(self, notification):

        if notification["message_type"] != RINGMQTT_MESSAGE_TYPE:
            return

        props = {'message_type': RINGMQTT_MESSAGE_TYPE, 'message_encode': True}
        brokerID = int(notification['brokerID'])
        while True:
            #self.logger.warning("About to call fetchQueuedMessage")
            message_data = self.mqttPlugin.executeAction("fetchQueuedMessage", deviceId=brokerID, props=props, waitUntilDone=True)
            if message_data is None:
                break
            self.logger.threaddebug(f"processMessage: {message_data}")

            topic_parts = message_data["topic_parts"]
            payload = base64.b64decode(message_data['payload'])

            self.processMessage(topic_parts, payload)

    def processMessage(self, topic_parts, payload):

            if topic_parts[0] == "ring":

                payloadimage = ""
                if len(topic_parts) > 5:
                    if topic_parts[5] != "image":
                        payload = payload.decode("ascii")
                    else:
                        payloadimage = payload
                        payload = "Binary Image Hidden"
                else:
                    payload = payload.decode("ascii")

                for device_id in self.ringmqtt_devices:
                    device = indigo.devices[device_id]

                    ringdevice_id = topic_parts[1] + "-" + device.deviceTypeId[4:5] + "-" + topic_parts[3]

                    if ringdevice_id != device.address:     # wrong device
                        continue

                    if topic_parts[2] == "camera":
                        self.processCMessage(device, topic_parts, payload, payloadimage)
                        self.processBMessage(device, topic_parts, payload)

                    if topic_parts[2] == "lighting":
                        self.processLMessage(device, topic_parts, payload)
                        self.processBMessage(device, topic_parts, payload)

                    if topic_parts[2] == "chime":
                        self.processZMessage(device, topic_parts, payload)

            elif topic_parts[0] == "homeassistant":
                payload = payload.decode("ascii")
                self.processHADMessage(topic_parts, payload)

    def deviceRingCacheCheck(self, devId):
        try:
            device = indigo.devices[devId]
        except:
            self.logger.debug(f"Device {devId} not found - Occurs after Deleting Indigo Device")
        else:
            device.setErrorStateOnServer(u"")
            if device.address not in self.ring_devices:
                self.logger.error(f"deviceRingCacheCheck - Device Name: {device.name} - Device Address: {device.address}")
                device.setErrorStateOnServer(u"no ack")
            else:
                newProps = device.pluginProps
                newProps["name"] = self.ring_devices[device.address][0]
                newProps["manufacturer"] = self.ring_devices[device.address][1]
                newProps["model"] = self.ring_devices[device.address][2]
                device.replacePluginPropsOnServer(newProps)

    def processHADMessage(self, topic_parts, payload):
        self.logger.debug(f"processHADMessage: {'/'.join(topic_parts)}:{payload}")

        if topic_parts[1] == "binary_sensor":
            if "_motion" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_devices[topic_parts[2] + "-M-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0], topic_parts[2], "RingMotion", "mqtt"]
                return
            if "_ding" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_devices[topic_parts[2] + "-D-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0], topic_parts[2], "RingDoorbell", "mqtt"]
                return
        if topic_parts[1] == "sensor":
            if "_battery" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_battery_devices[topic_parts[2] + "-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0], topic_parts[2], "Battery", "mqtt"]
                return
        if topic_parts[1] == "camera":
            if "_snapshot" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_devices[topic_parts[2] + "-C-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0],topic_parts[2], "RingCamera", "mqtt"]
                return
        if topic_parts[1] == "switch":
            if "_siren" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_devices[topic_parts[2] + "-S-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0], topic_parts[2], "RingSiren", "mqtt"]
                return
            if "_snooze" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_devices[topic_parts[2] + "-Z-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0], topic_parts[2], "RingZChime", "mqtt"]
                return
        if topic_parts[1] == "light":
            if "_light" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_devices[topic_parts[2] + "-L-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0], topic_parts[2], "RingLight", "mqtt"]
                return
    def processCMessage(self, device, topic_parts, payload, payloadimage):
        self.logger.debug(f"processCMessage: {'/'.join(topic_parts)}:{payload} - Device:{device.name}")

        if topic_parts[4] == "status":
            device.updateStateOnServer(key="status", value=payload)
        if topic_parts[4] == "info":
            p = json.loads(payload)
            device.updateStateOnServer(key="firmwareStatus", value=p["firmwareStatus"])
            q = self.convertZeroDate(p["lastUpdate"])
            device.updateStateOnServer(key="lastUpdate", value=str(q))
            r = self.getDuration(q, datetime.datetime.now())
            if r > int(self.pluginPrefs.get("ringCommAlertHours","06")):
                self.logger.warning(f"Device {device.name} hasn't had communication from Ring in {r} hours")
                if device.deviceTypeId == "RingCamera":
                    device.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
                    device.updateStateOnServer(key="state", value="Not Connected")
            else:
                if device.deviceTypeId == "RingCamera":
                    device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)
                    device.updateStateOnServer(key="state", value="Connected")
            if device.deviceTypeId == "RingCamera":
                device.updateStateOnServer(key="stream_Source", value=p["stream_Source"])
        if topic_parts[4] == "motion" and topic_parts[5] == "state" and device.deviceTypeId == "RingMotion":
            if payload == "ON":
                device.updateStateOnServer(key="onOffState", value=True)
            else:
                device.updateStateOnServer(key="onOffState", value=False)
        if topic_parts[4] == "motion_detection" and topic_parts[5] == "state" and device.deviceTypeId == "RingMotion":
            device.updateStateOnServer(key="motionDetectionEnabled", value=payload)

        if topic_parts[4] == "motion" and topic_parts[5] == "attributes" and device.deviceTypeId == "RingMotion":
            p = json.loads(payload)
            device.updateStateOnServer(key="lastMotionTime", value=str(self.convertZeroDate(p["lastMotionTime"])))
            #device.updateStateOnServer(key="personDetected", value=p["personDetected"])

        if topic_parts[4] == "motion_duration" and topic_parts[5] == "state" and device.deviceTypeId == "RingMotion":
            device.updateStateOnServer(key="motion_duration", value=payload)

        if topic_parts[4] == "snapshot" and topic_parts[5] == "image" and device.deviceTypeId == "RingCamera":
            self.logger.debug(f"processCMessage: Image Binary {'/'.join(topic_parts)}:{payload} - Device:{device.name}")
            if self.pluginPrefs.get("storeSnapShots", False):
                if self.pluginPrefs.get("snapshotImagePath", "") == "":
                    tpath = indigo.server.getInstallFolderPath() + '/Web Assets/public/ring/'
                    device.updateStateOnServer(key="snapshot_image", value="http://localhost:8176/public/ring/" + device.address + ".jpg")
                else:
                    tpath = self.pluginPrefs.get("snapshotImagePath", "")
                    device.updateStateOnServer(key="snapshot_image", value=tpath + device.address + ".jpg")
                if os.path.isdir(tpath):
                    test_file = open(tpath + device.address + '.jpg','wb')
                    test_file.write(payloadimage)
                    test_file.close()

        if topic_parts[4] == "snapshot" and topic_parts[5] == "attributes" and device.deviceTypeId == "RingCamera":
            self.logger.debug(f"processCMessage: Image Attributes {'/'.join(topic_parts)}:{payload} - Device:{device.name}")
            p = json.loads(payload)
            device.updateStateOnServer(key="snapshot_timestamp", value=str(datetime.datetime.fromtimestamp(p["timestamp"])))
            device.updateStateOnServer(key="snapshot_type", value=p["type"])

        if topic_parts[4] == "snapshot_mode" and topic_parts[5] == "state" and device.deviceTypeId == "RingCamera":
            if device.pluginProps["snapshot_mode"] != payload:
                self.logger.debug(f'processCMessage: Snapshot Mode different for device {device.name} : PluginProps: {device.pluginProps["snapshot_mode"]}  Payload: {payload}')
                newProps = device.pluginProps
                newProps["snapshot_mode"] = payload
                device.replacePluginPropsOnServer(newProps)

        if topic_parts[4] == "snapshot_interval" and topic_parts[5] == "state" and device.deviceTypeId == "RingCamera":
            if device.pluginProps["snapshot_interval"] != payload:
                self.logger.debug(f'processCMessage: Snapshot Interval different for device {device.name} : PluginProps: {device.pluginProps["snapshot_interval"]}  Payload: {payload}')
                newProps = device.pluginProps
                newProps["snapshot_interval"] = payload
                device.replacePluginPropsOnServer(newProps)

        if topic_parts[4] == "event_select" and topic_parts[5] == "attributes" and device.deviceTypeId == "RingCamera":
            p = json.loads(payload)
            if p["eventId"] != device.states["event_eventId1"]:
                device.updateStateOnServer(key="event_recordingUrl3", value=device.states["event_recordingUrl2"])
                device.updateStateOnServer(key="event_eventId3", value=device.states["event_eventId2"])
                device.updateStateOnServer(key="event_recordingUrl2", value=device.states["event_recordingUrl1"])
                device.updateStateOnServer(key="event_eventId2", value=device.states["event_eventId1"])
                device.updateStateOnServer(key="event_recordingUrl1", value=p["recordingUrl"])
                device.updateStateOnServer(key="event_eventId1", value=p["eventId"])

        if topic_parts[4] == "ding" and topic_parts[5] == "state" and device.deviceTypeId == "RingDoorbell":
            if payload == "ON":
                device.updateStateOnServer(key="onOffState", value=True)
            else:
                device.updateStateOnServer(key="onOffState", value=False)

        if topic_parts[4] == "ding" and topic_parts[5] == "attributes" and device.deviceTypeId == "RingDoorbell":
            p = json.loads(payload)
            device.updateStateOnServer(key="lastDingTime", value=str(self.convertZeroDate(p["lastDingTime"])))

        if topic_parts[4] == "light" and topic_parts[5] == "state" and device.deviceTypeId == "RingLight":
            if payload == "ON":
                device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOn)
                device.updateStateOnServer(key="onOffState", value=True)
            else:
                device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOff)
                device.updateStateOnServer(key="onOffState", value=False)

        if topic_parts[4] == "siren" and topic_parts[5] == "state" and device.deviceTypeId == "RingSiren":
            if payload == "ON":
                device.updateStateOnServer(key="onOffState", value=True)
            else:
                device.updateStateOnServer(key="onOffState", value=False)

    def processBMessage(self, device, topic_parts, payload):
        self.logger.debug(f"processBMessage: {'/'.join(topic_parts)}:{payload}")

        if topic_parts[4] == "battery" and topic_parts[5] == "attributes":
            p = json.loads(payload)
            if "batteryLife2" in p:
                if int(p["batteryLife2"]) > int(p["batteryLife"]) and self.pluginPrefs.get("batterystateUI", False):
                    b1 = p["batteryLife2"]
                    b2 = p["batteryLife"]
                else:
                    b1 = p["batteryLife"]
                    b2 = p["batteryLife2"]
            else:
                b1 = p["batteryLife"]
                b2 = "N/A"
            device.updateStateOnServer(key="batteryLevel", value=b1)
            device.updateStateOnServer(key="batteryLevel2", value=b2)

    def processLMessage(self, device, topic_parts, payload):
        self.logger.debug(f"processLMessage: {'/'.join(topic_parts)}:{payload} - Device:{device.name}")

        if topic_parts[4] == "status":
            device.updateStateOnServer(key="status", value=payload)
        if topic_parts[4] == "info":
            p = json.loads(payload)
            device.updateStateOnServer(key="firmwareStatus", value=p["firmwareStatus"])
            device.updateStateOnServer(key="lastUpdate", value=str(self.convertZeroDate(p["lastUpdate"])))
        if topic_parts[4] == "light" and topic_parts[5] == "state" and device.deviceTypeId == "RingLight":
            if payload == "ON":
                device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOn)
                device.updateStateOnServer(key="onOffState", value=True)
            else:
                device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOff)
                device.updateStateOnServer(key="onOffState", value=False)
        if topic_parts[4] == "light" and topic_parts[5] == "brightness_state" and device.deviceTypeId == "RingLight":
            device.updateStateOnServer(key="brightness_state", value=payload)
        if topic_parts[4] == "beam_duration" and topic_parts[5] == "state" and device.deviceTypeId == "RingLight":
            device.updateStateOnServer(key="beam_duration", value=payload)
        if topic_parts[4] == "motion" and topic_parts[5] == "state" and device.deviceTypeId == "RingMotion":
            device.updateStateOnServer(key="motionDetectionEnabled", value="ON")
            if payload == "ON":
                device.updateStateOnServer(key="onOffState", value=True)
                device.updateStateOnServer(key="lastMotionTime", value=str(datetime.datetime.fromtimestamp(time.time())))
            else:
                device.updateStateOnServer(key="onOffState", value=False)

    def processZMessage(self, device, topic_parts, payload):
        self.logger.debug(f"processZMessage: {'/'.join(topic_parts)}:{payload} - Device:{device.name}")

        if topic_parts[4] == "status":
            device.updateStateOnServer(key="status", value=payload)
        if topic_parts[4] == "info":
            p = json.loads(payload)
            device.updateStateOnServer(key="firmwareStatus", value=p["firmwareStatus"])
            device.updateStateOnServer(key="lastUpdate", value=str(self.convertZeroDate(p["lastUpdate"])))
        if topic_parts[4] == "play_ding_sound" and topic_parts[5] == "state":
            device.updateStateOnServer(key="play_ding_sound", value=payload)
            if payload == "ON":
                device.updateStateOnServer(key="onOffState", value=True)
            else:
                device.updateStateOnServer(key="onOffState", value=False)
        if topic_parts[4] == "play_motion_sound" and topic_parts[5] == "state":
            device.updateStateOnServer(key="play_motion_sound", value=payload)
        if topic_parts[4] == "volume" and topic_parts[5] == "state":
            device.updateStateOnServer(key="volume", value=payload)
        if topic_parts[4] == "snooze" and topic_parts[5] == "state":
            device.updateStateOnServer(key="snooze", value=payload)
        if topic_parts[4] == "snooze" and topic_parts[5] == "attributes":
            p = json.loads(payload)
            device.updateStateOnServer(key="snooze_minutes_remaining", value=p["minutes_remaining"])
        if topic_parts[4] == "snooze_minutes" and topic_parts[5] == "state":
            device.updateStateOnServer(key="snooze_minutes", value=payload)
    @staticmethod
    def get_mqtt_connectors(filter="", valuesDict=None, typeId="", targetId=0):
        retList = []
        devicePlugin = valuesDict.get("devicePlugin", None)
        for dev in indigo.devices.iter():
            if dev.protocol == indigo.kProtocol.Plugin and dev.pluginId == "com.flyingdiver.indigoplugin.mqtt" and dev.deviceTypeId == 'mqttBroker':
                retList.append((dev.id, dev.name))
        retList.sort(key=lambda tup: tup[1])
        return retList

    def get_ring_devices(self, filter="", valuesDict=None, typeId="", targetId=0):
        retList = []
        for aID in self.ring_devices:
            if self.ring_devices[aID][5] == typeId:
                retList.append((aID, self.ring_devices[aID][0] + " (" + self.ring_devices[aID][1] + " - " + self.ring_devices[aID][2] + " - " + self.ring_devices[aID][6] + ")"))
        retList.sort(key=lambda tup: tup[1])
        return retList

    def convertZeroDate(self, zeroDate=""):
        d1 = datetime.datetime.fromisoformat(zeroDate.replace('Z', '+00:00'))
        gmtoffset = int(datetime.datetime.now().astimezone().strftime("%z")[0:3])
        sgtTimeDelta = datetime.timedelta(hours=gmtoffset)
        sgtTZObject = datetime.timezone(sgtTimeDelta, name="SGT")
        d2 = d1.astimezone(sgtTZObject)
        #d3 = str(d2).replace('-05:00','')
        #d4 = datetime.datetime.strptime(d3, '%Y-%m-%d %H:%M:%S')
        d4 = datetime.datetime.strftime(d2, '%Y-%m-%d %H:%M:%S')
        d5 = datetime.datetime.strptime(d4, '%Y-%m-%d %H:%M:%S')
        self.logger.debug(f"convertZeroDate - Input: {zeroDate} - Output {d5}")
        return d5

    def selectionChanged(self, valuesDict, typeId, devId):
        self.logger.debug("SelectionChanged")
        self.logger.debug("Looking up deviceID %s in Device Cache" % valuesDict["doorbell"])
        if valuesDict["doorbell"] in self.ring_devices:
            valuesDict["doorbellId"] = valuesDict["doorbell"]
            valuesDict["address"] = valuesDict["doorbell"]
            valuesDict["name"] = self.ring_devices[valuesDict["doorbell"]][0]
            valuesDict["manufacturer"] = self.ring_devices[valuesDict["doorbell"]][1]
            valuesDict["model"] = self.ring_devices[valuesDict["doorbell"]][2]
            valuesDict["apitype"] = self.ring_devices[valuesDict["doorbell"]][6]
            p = self.ring_devices[valuesDict["doorbellId"]][4] + "-" + self.ring_devices[valuesDict["doorbellId"]][3]
            if p in self.ring_battery_devices:
                valuesDict["SupportsBatteryLevel"] = True
            else:
                valuesDict["SupportsBatteryLevel"] = False

        self.logger.debug(u"\tSelectionChanged valuesDict to be returned:\n%s" % (str(valuesDict)))
        return valuesDict

    def didDeviceCommPropertyChange(self, origDev, newDev):
        if origDev.address != newDev.address:
            return True
        return False

    def closedDeviceConfigUi(self, valuesDict, userCancelled, typeId, devId):
        if not userCancelled:
            device = indigo.devices[devId]
            if device.deviceTypeId == "RingCamera":
                self.publishSnapshotProps(device.pluginProps.get("snapshot_mode",""), device.pluginProps.get("snapshot_interval",""), valuesDict["snapshot_mode"], valuesDict["snapshot_interval"], device.name, valuesDict["address"])
        return
    def publishSnapshotProps(self, origsnapshotMode, origsnapshotInterval, newsnapshotMode, newsnapshotInterval, devName, devAddress):
        brokerID = int(self.brokerID)
        topicType = "camera"
        self.logger.debug(f"{devName}: publishSnapshotProps")
        if newsnapshotMode != "" and origsnapshotMode != newsnapshotMode:
            payload = newsnapshotMode
            self.logger.debug(f'publishSnapshotProps: Snapshot Mode Publish for device {devName} : Payload: {payload}')
            self.publish_topic(brokerID, devName,f"ring/{self.ring_devices[devAddress][4]}/{topicType}/{self.ring_devices[devAddress][3]}/snapshot_mode/command", payload)
        elif newsnapshotInterval != "" and origsnapshotInterval != newsnapshotInterval:
            payload = newsnapshotInterval
            self.logger.debug(f'publishSnapshotProps: Snapshot Interval Publish for device {devName} : Payload: {payload}')
            self.publish_topic(brokerID, devName,f"ring/{self.ring_devices[devAddress][4]}/{topicType}/{self.ring_devices[devAddress][3]}/snapshot_interval/command", payload)

    def generate_HAD_messages(self):
        self.logger.debug(f"generate_HAD_messages: Clear Device Cache, start HA Discovery and rebuild cache")

        self.ring_devices = {}
        self.ring_battery_devices = {}
        brokerID = int(self.brokerID)
        self.publish_topic(brokerID, "HA_Discovery", f"hass/status", "online")
        self.logger.info(f"Sent topic: haas/status, payload: online - devices should be updated shortly")
        self.pyapiDeviceCache()

        return True

    def create_trigger(self, valuesDict, typeId):

        if not self.MQTTConnectorPlugin:
            self.logger.info(f"No need to create triggers unless using the MQTT Connector Plugin")
            return True

        found = False

        # look to see if there's an existing trigger for this message-type
        for trigger in indigo.triggers:
            try:
                if trigger.pluginId != 'com.flyingdiver.indigoplugin.mqtt':
                    continue
                if trigger.pluginTypeId != 'topicMatch':
                    continue
                self.logger.debug(f"startup: Checking existing trigger: {trigger}")
                if trigger.globalProps['com.flyingdiver.indigoplugin.mqtt']['message_type'] == RINGMQTT_MESSAGE_TYPE and \
                        trigger.globalProps['com.flyingdiver.indigoplugin.mqtt']['brokerID'] == valuesDict['brokerID']:
                    self.logger.info(f"Skipping trigger creation, trigger already exists: {trigger.name}")
                    found = True
                    return True
            except Exception as e:
                self.logger.debug(f"startup: Error reading trigger: {trigger}\n{e}")
                continue

        if not found:
            broker = indigo.devices[int(valuesDict['brokerID'])]
            name = f"ringmqtt Trigger ({broker.name})"
            try:
                indigo.pluginEvent.create(name=name, pluginId="com.flyingdiver.indigoplugin.mqtt", pluginTypeId="topicMatch",
                    props={
                        "brokerID": valuesDict['brokerID'],
                        "message_type": RINGMQTT_MESSAGE_TYPE,
                        "queueMessage": "True",
                        "match_list": ["Match: ring", "Any: "]
                    })
            except Exception as e:
                self.logger.error(f"Error calling indigo.pluginEvent.create(): {e}")
            else:
                self.logger.info(f"Created trigger '{name} for message type '{RINGMQTT_MESSAGE_TYPE}'")

            name = f"ringmqtt-HA Trigger ({broker.name})"
            try:
                indigo.pluginEvent.create(name=name, pluginId="com.flyingdiver.indigoplugin.mqtt", pluginTypeId="topicMatch",
                    props={
                        "brokerID": valuesDict['brokerID'],
                        "message_type": RINGMQTT_MESSAGE_TYPE,
                        "queueMessage": "True",
                        "match_list": ["Match: homeassistant", "Any: "]
                    })
            except Exception as e:
                self.logger.error(f"Error calling indigo.pluginEvent.create(): {e}")
            else:
                self.logger.info(f"Created HA trigger '{name} for message type '{RINGMQTT_MESSAGE_TYPE}'")
                brokerID = int(self.brokerID)
                if brokerID != 0:
                    self.publish_topic(brokerID, "HA_Discovery", f"hass/status", "online")
                    self.logger.info(f"Generated initial HA Discovery")

        return True

    ########################################
    # Relay / Dimmer Action callback
    ########################################

    def actionControlDevice(self, action, device):

        if device.pluginProps["apitype"] == "pyapi":
            self.actionControlDevicePyAPI(action, device)
            return

        self.logger.debug(f"actionControlDevice: Action: {action.deviceAction} Device: {device.name}")

        brokerID = int(self.brokerID)

        if action.deviceAction == indigo.kDeviceAction.TurnOff:
            self.logger.debug(f"actionControlDevice: Turn Off {device.name}")
            if device.deviceTypeId == "RingZChime":
                topicType = "chime"
                topicSubtype = "play_ding_sound"
            else:
                topicType = "lighting"
                topicSubtype = "light"
                tCamCheck = self.ring_devices[device.address][4] + "-C-" + self.ring_devices[device.address][3]
                if tCamCheck in self.ring_devices:
                    topicType = "camera"
                    if device.deviceTypeId == "RingSiren":
                        topicSubtype = "siren"
            self.publish_topic(brokerID, device.name, f"ring/{self.ring_devices[device.address][4]}/{topicType}/{self.ring_devices[device.address][3]}/{topicSubtype}/command", "OFF")

        elif action.deviceAction == indigo.kDeviceAction.TurnOn:
            self.logger.debug(f"actionControlDevice: Turn On {device.name}")
            if device.deviceTypeId == "RingZChime":
                topicType = "chime"
                topicSubtype = "play_ding_sound"
            else:
                topicType = "lighting"
                topicSubtype = "light"
                tCamCheck = self.ring_devices[device.address][4] + "-C-" + self.ring_devices[device.address][3]
                if tCamCheck in self.ring_devices:
                    topicType = "camera"
                    if device.deviceTypeId == "RingSiren":
                        topicSubtype = "siren"
            self.publish_topic(brokerID, device.name, f"ring/{self.ring_devices[device.address][4]}/{topicType}/{self.ring_devices[device.address][3]}/{topicSubtype}/command", "ON")

        else:
            self.logger.error(f"{device.name}: actionControlDevice: Unsupported action requested: {action.deviceAction}")

    def actionControlDevicePyAPI(self, action, device):

        if self.PyAPIConnectorDeviceId == 0:
            return

        self.logger.debug(f"actionControlDevicePyAPI: Action: {action.deviceAction} Device: {device.name}")

        if device.pluginProps["model"] == "Light Group":
            groups = self.ring.groups()
            dev = groups[self.ring_devices[device.address][3]]
            isGroup = True
        else:
            dev = self.ring.get_device_by_name(device.pluginProps["name"])
            isGroup = False

        if action.deviceAction == indigo.kDeviceAction.TurnOff:
            self.logger.debug(f"actionControlDevicePyAPI: Turn Off {device.name}")
            if device.deviceTypeId == "RingZChime":
                dev.test_sound(kind = 'motion')
            elif device.deviceTypeId == "RingSiren":
                if dev.siren:
                    dev.siren = "off"
            elif device.deviceTypeId == "RingLight":
                if isGroup:
                    dev.lights = False
                    device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOff)
                    device.updateStateOnServer(key="onOffState", value=False)
                elif dev.family == "stickup_cams" and dev.lights:
                    dev.lights = "off"
                    device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOff)
                    device.updateStateOnServer(key="onOffState", value=False)
        elif action.deviceAction == indigo.kDeviceAction.TurnOn:
            self.logger.debug(f"actionControlDevicePyAPI: Turn On {device.name}")
            if device.deviceTypeId == "RingZChime":
                dev.test_sound(kind = 'ding')
            elif device.deviceTypeId == "RingSiren":
                if dev.siren:
                    dev.siren = "on"
            elif device.deviceTypeId == "RingLight":
                if isGroup:
                    dev.lights = True
                    device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOn)
                    device.updateStateOnServer(key="onOffState", value=True)
                elif dev.family == "stickup_cams" and dev.lights:
                    dev.lights = "on"
                    device.updateStateImageOnServer(indigo.kStateImageSel.DimmerOn)
                    device.updateStateOnServer(key="onOffState", value=True)

        else:
            self.logger.error(f"{device.name}: actionControlDevicePyAPI: Unsupported action requested: {action.deviceAction}")

    def publish_topic(self, brokerID, devicename, topic, payload):

        if self.MQTTConnectorDeviceId == 0:
            return

        if self.MQTTConnectorPlugin:
            mqttPlugin = indigo.server.getPlugin("com.flyingdiver.indigoplugin.mqtt")
            if not mqttPlugin.isEnabled():
                self.logger.error("MQTT Connector plugin not enabled, publish_topic aborting.")
                return

            props = {
                'topic': topic,
                'payload': payload,
                'qos': 0,
                'retain': 0,
            }
            mqttPlugin.executeAction("publish", deviceId=brokerID, props=props, waitUntilDone=False)
        else:
            qos = 0
            retain = 0
            self.client.publish(topic, payload, qos, retain)

        self.logger.debug(f"{devicename}: publish_topic: {topic} -> {payload}")

    ########################################
    # Methods for the subscriptions config panel
    ########################################

    def addTopic(self, valuesDict, typeId, deviceId):
        topic = valuesDict["subscriptions_newTopic"]
        qos = valuesDict["subscriptions_qos"]
        self.logger.debug(f"addTopic: {typeId}, {topic} ({qos})")

        topicList = list()
        if 'subscriptions' in valuesDict:
            for t in valuesDict['subscriptions']:
                topicList.append(t)

        if typeId == 'MQTTConnector':
            s = "{}:{}".format(qos, topic)
            if s not in topicList:
                topicList.append(s)

        else:
            self.logger.warning(f"addTopic: Invalid device type: {typeId} for device {deviceId}")

        valuesDict['subscriptions'] = topicList
        return valuesDict

    @staticmethod
    def deleteSubscriptions(valuesDict, typeId, deviceId):
        topicList = list()
        if 'subscriptions' in valuesDict:
            for t in valuesDict['subscriptions']:
                topicList.append(t)
        for t in valuesDict['subscriptions_items']:
            if t in topicList:
                topicList.remove(t)
        valuesDict['subscriptions'] = topicList
        return valuesDict

    @staticmethod
    def subscribedList(ifilter, valuesDict, typeId, deviceId):
        returnList = list()
        if 'subscriptions' in valuesDict:
            for topic in valuesDict['subscriptions']:
                returnList.append(topic)
        return returnList

    ########################################
    ########################################
    # PluginConfig methods
    ########################################

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if not userCancelled:
            self.logLevel = int(valuesDict.get("logLevel", logging.INFO))
            self.indigo_log_handler.setLevel(self.logLevel)

    def getDuration(self, then, now=datetime.datetime.now(), interval="hours"):

        # Returns a duration as specified by variable interval
        # Functions, except totalDuration, returns [quotient, remainder]

        duration = now - then  # For build-in functions
        duration_in_s = duration.total_seconds()

        def years():
            return divmod(duration_in_s, 31536000)  # Seconds in a year=31536000.

        def days(seconds=None):
            return divmod(seconds if seconds != None else duration_in_s, 86400)  # Seconds in a day = 86400

        def hours(seconds=None):
            return divmod(seconds if seconds != None else duration_in_s, 3600)  # Seconds in an hour = 3600

        def minutes(seconds=None):
            return divmod(seconds if seconds != None else duration_in_s, 60)  # Seconds in a minute = 60

        def seconds(seconds = None):
            if seconds != None:
                return divmod(seconds, 1)
            return duration_in_s

        def totalDuration():
            y = years()
            d = days(y[1]) # Use remainder to calculate next variable
            h = hours(d[1])
            m = minutes(h[1])
            s = seconds(m[1])

            return "Time between dates: {} years, {} days, {} hours, {} minutes and {} seconds".format(int(y[0]), int(d[0]), int(h[0]), int(m[0]), int(s[0]))

        return {
            'years': int(years()[0]),
            'days': int(days()[0]),
            'hours': int(hours()[0]),
            'minutes': int(minutes()[0]),
            'seconds': int(seconds()),
            'default': totalDuration()
        }[interval]

    ########################################
    # Plugin Actions object callbacks (pluginAction is an Indigo plugin action instance)
    ########################################

    def publishChimeAction(self, pluginAction, chimeDevice, callerWaitingForResult):

        brokerID = int(self.brokerID)
        topicType = "chime"
        self.logger.debug(f"{chimeDevice.name}: publishChimeAction")
        if pluginAction.props["volume"] != "":
            payload = indigo.activePlugin.substitute(pluginAction.props["volume"])
            if chimeDevice.pluginProps["apitype"] == "pyapi":
                dev = self.ring.get_device_by_name(chimeDevice.pluginProps["name"])
                dev.volume = int(payload)
            else:
                self.publish_topic(brokerID, chimeDevice.name,
                           f"ring/{self.ring_devices[chimeDevice.address][4]}/{topicType}/{self.ring_devices[chimeDevice.address][3]}/volume/command",
                           payload)
        if pluginAction.props["play_motion_sound"] is True:
            payload = "ON"
            if chimeDevice.pluginProps["apitype"] == "pyapi":
                dev = self.ring.get_device_by_name(chimeDevice.pluginProps["name"])
                dev.test_sound(kind = 'motion')
            else:
                self.publish_topic(brokerID, chimeDevice.name,
                               f"ring/{self.ring_devices[chimeDevice.address][4]}/{topicType}/{self.ring_devices[chimeDevice.address][3]}/play_motion_sound/command",
                               payload)

        if chimeDevice.pluginProps["apitype"] == "pyapi":
            if pluginAction.props["snooze_minutes"] != "" or pluginAction.props["snooze"] != "":
                self.logger.info(f"publishChimeAction - Chime snooze property changes are not supported through API Connector")
            return

        if pluginAction.props["snooze_minutes"] != "":
            payload = indigo.activePlugin.substitute(pluginAction.props["snooze_minutes"])
            self.publish_topic(brokerID, chimeDevice.name,
                           f"ring/{self.ring_devices[chimeDevice.address][4]}/{topicType}/{self.ring_devices[chimeDevice.address][3]}/snooze_minutes/command",
                           payload)
        if pluginAction.props["snooze"] != "":
            payload = indigo.activePlugin.substitute(pluginAction.props["snooze"])
            self.publish_topic(brokerID, chimeDevice.name,
                           f"ring/{self.ring_devices[chimeDevice.address][4]}/{topicType}/{self.ring_devices[chimeDevice.address][3]}/snooze/command",
                           payload)

    def publishMotionAction(self, pluginAction, motionDevice, callerWaitingForResult):

        if motionDevice.pluginProps["apitype"] == "pyapi":
            self.logger.info(f"publishMotionAction - Actions not supported with API Connector")
            return

        brokerID = int(self.brokerID)
        topicType = "camera"
        self.logger.debug(f"{motionDevice.name}: publishMotionAction")
        if pluginAction.props["motion_detection"] != "":
            payload = indigo.activePlugin.substitute(pluginAction.props["motion_detection"])
            self.publish_topic(brokerID, motionDevice.name,
                           f"ring/{self.ring_devices[motionDevice.address][4]}/{topicType}/{self.ring_devices[motionDevice.address][3]}/motion_detection/command",
                           payload)
        if pluginAction.props["motion_warning"] != "":
            payload = indigo.activePlugin.substitute(pluginAction.props["motion_warning"])
            self.publish_topic(brokerID, motionDevice.name,
                           f"ring/{self.ring_devices[motionDevice.address][4]}/{topicType}/{self.ring_devices[motionDevice.address][3]}/motion_warning/command",
                           payload)

    def publishLightAction(self, pluginAction, lightDevice, callerWaitingForResult):

        if lightDevice.pluginProps["apitype"] == "pyapi":
            self.logger.info(f"publishLightAction - Actions not supported with API Connector")
            return

        brokerID = int(self.brokerID)
        topicType = "camera"
        self.logger.debug(f"{lightDevice.name}: publishLightAction")
        if pluginAction.props["brightness"] != "":
            payload = indigo.activePlugin.substitute(pluginAction.props["brightness"])
            self.publish_topic(brokerID, lightDevice.name,
                           f"ring/{self.ring_devices[lightDevice.address][4]}/{topicType}/{self.ring_devices[lightDevice.address][3]}/brightness/command",
                           payload)
        if pluginAction.props["duration"] != "":
            payload = indigo.activePlugin.substitute(pluginAction.props["duration"])
            self.publish_topic(brokerID, lightDevice.name,
                           f"ring/{self.ring_devices[lightDevice.address][4]}/{topicType}/{self.ring_devices[lightDevice.address][3]}/duration/command",
                           payload)

    def publishCameraAction(self, pluginAction, cameraDevice, callerWaitingForResult):

        if cameraDevice.pluginProps["apitype"] == "pyapi":
            self.logger.info(f"publishCameraAction - Actions not supported with API Connector")
            return

        brokerID = int(self.brokerID)
        topicType = "camera"
        self.logger.debug(f"{cameraDevice.name}: publishCameraAction")
        if pluginAction.props["snapshot_interval"] != "":
            payload = indigo.activePlugin.substitute(pluginAction.props["snapshot_interval"])
            self.publish_topic(brokerID, cameraDevice.name,
                           f"ring/{self.ring_devices[cameraDevice.address][4]}/{topicType}/{self.ring_devices[cameraDevice.address][3]}/snapshot_interval/command",
                           payload)
        if pluginAction.props["live_stream"] != "":
            payload = indigo.activePlugin.substitute(pluginAction.props["live_stream"])
            self.publish_topic(brokerID, cameraDevice.name,
                           f"ring/{self.ring_devices[cameraDevice.address][4]}/{topicType}/{self.ring_devices[cameraDevice.address][3]}/stream/command",
                           payload)

    def _run_async_thread(self):
        self.logger.debug("_run_async_thread starting")
        # the create_task needs to be called when the APIConnector device starts
        #self._event_loop.create_task(self._async_start())
        self._event_loop.run_until_complete(self._async_stop())
        self._event_loop.close()

    async def _async_start(self):
        if self.pyapilisten:
            self.logger.debug("_async_start")
            await self._pyapi_listen()
        # add things you need to do at the start of the plugin here

    async def _async_stop(self):
        while True:
            await asyncio.sleep(1.0)
            if self.stopThread:
                break

    class _event_handler:  # pylint:disable=invalid-name
        def __init__(self, ring: Ring):
            self.ring = ring

        def on_event(self, event: RingEvent):
            dev = self.ring.get_device_by_name(event.device_name)
            taddress = "Event not caught"
            t = str(datetime.datetime.fromtimestamp(event.now))
            if event.kind == "motion":
                taddress = dev._attrs["location_id"] + "-MA-" + dev.device_id
                eventid = event.id
                for device in indigo.devices.iter(u"self"):
                    if device.address == taddress:
                        device.updateStateOnServer(key="onOffState", value=True)
                        device.updateStateOnServer(key="lastMotionTime", value=str(t))
            elif event.kind == "com.ring.push.HANDLE_NEW_DING":
                taddress = dev._attrs["location_id"] + "-DA-" + dev.device_id
                eventid = event.id
                for device in indigo.devices.iter(u"self"):
                    if device.address == taddress:
                        device.updateStateOnServer(key="onOffState", value=True)
                        device.updateStateOnServer(key="lastDingTime", value=str(t))

            tVar = "{" + f'"address": "{taddress}", "event.kind": "{event.kind}", "timestamp": "{t}"' + "}"
            indigo.server.log(tVar)
            indigo.server.log(event.kind)
            #msg = (
            #        str(datetime.datetime.utcnow())
            #        + ": "
            #        + str(event)
            #        + " : Currently active count = "
            #        + str(len(self.ring.push_dings_data))
            #)
            #self.logger.info(msg)
            #indigo.server.log(msg)

    async def _pyapi_listen(self):

            ring = self.ring
            ring.create_session()
            store_credentials = True

            """Listen to push notification like the ones sent to your phone."""
            if not can_listen:
                self.logger.error("Ring is not configured for listening to notifications!")
                self.logger.error("pip install 'ring_doorbell[listen]'")
                return

            from listen import (  # pylint:disable=import-outside-toplevel
                RingEventListener,
            )

            def credentials_updated_callback(credentials):
                if store_credentials:
                    self.credentials = json.dumps(credentials)
                else:
                    self.logger.debug("_pyapi_listen: New push credentials: " + str(credentials))

            credentials = None
            if store_credentials and self.credentials != "":
                credentials = json.loads(self.credentials)
                self.logger.debug("_pyapi_listen: New push credentials: " + str(credentials))

            self.logger.debug("_pyapi_listen - RingEventListener")
            event_listener = RingEventListener(ring, credentials, credentials_updated_callback)
            self.logger.debug("_pyapi_listen - start event_listener")
            event_listener.start()
            self.logger.debug("_pyapi_listen - add_notification_callback to event_listener")
            event_listener.add_notification_callback(self._event_handler(ring).on_event)

            self.logger.debug("_pyapi_listen - before while wait loop")
            self.logger.info("Listening for Push Events from Ring - PyAPI")

            while True:
                await asyncio.sleep(1.0)
                if self.stopThread:
                    self.logger.info("_pyapi_listen - while loop hits stop thread condition")
                    event_listener.stop()
                    break

            self.logger.info("_pyapi_listen - after while wait loop")

            event_listener.stop()

    def connectToMQTTBroker(self):
        try:
            self.logger.info("Connecting to the MQTT Server...")
            self.client.disconnect()
            self.client.loop_stop()
            self.client.username_pw_set(username=self.username, password=self.password)
            self.client.connect(self.MQTT_SERVER, self.MQTT_PORT, 59)
            self.logger.info("Connected!")
            if self.MQTTConnectorDeviceId != 0:
                device = indigo.devices[self.MQTTConnectorDeviceId]
                device.updateStateOnServer(key="status", value="Connected")

            self.client.loop_start()
        except Exception:
            t, v, tb = sys.exc_info()
            self.logger.info(f"t={t}, v={v}, traceback={tb}")
            if v == 61:
                self.logger.critical(u"Connection Refused when connecting to broker.")
            elif v == 60:
                self.logger.error(u"Timeout when connecting to broker.")
            else:
                self.handle_exception(t, v, tb)
                raise

    def on_connect(self, client, userdata, flags, rc):
        try:
            self.logger.debug(u"Connected with result code " + str(rc))
            if rc == 0:
                # self.client.subscribe(u"$SYS/#")

                self.connected = True
                for s in self.topicList:
                    qos = int(s[0:1])
                    topic = s[2:]
                    t = topic
                    self.logger.debug(u"Subscribing to " + t)
                    try:
                        self.client.subscribe(t)
                    except UnicodeDecodeError:
                        self.logger.warn(u'Failed to subscribe to ' + t + u' as it contains non-ascii characters.')

            if rc == 1:
                indigo.server.log(u"Error: Invalid Protocol Version.")
            if rc == 2:
                indigo.server.log(u"Error: Invalid Client Identifier.")

            if rc == 3:
                indigo.server.log(u"Error: Server Unavailable.")
            if rc == 4:
                indigo.server.log(u"Error: Bad Username or Password.")
            if rc == 5:
                indigo.server.log(u"Error: Not Authorised.")
        except Exception:
            t, v, tb = sys.exc_info()
            self.logger.debug({t, v, tb})
            self.handle_exception(t, v, tb)

    def on_disconnect(self):
        self.logger.warn(u"Disconnected from Broker. ")
        self.connected = False

    # The callback for when a PUBLISH message is received from the server.
    def on_message(self, client, userdata, msg):
        try:
            self.logger.threaddebug(u"Message recd: " + msg.topic + " | " + str(msg.payload))
            topic_parts = msg.topic.split("/")
            payload = msg.payload
            self.processMessage(topic_parts, payload)

            #_thread.start_new_thread(self.processMessage, (client, userdata, msg))
        except Exception:
            t, v, tb = sys.exc_info()
            self.handle_exception(t, v, tb)

    def handle_exception(self, exc_type, exc_value, exc_traceback):
        self.logger.error(u'Exception trapped:' + str(exc_value))

    def get_snapshot(self, doorbell, retries=1, delay=1, filename=None):
        """Take a snapshot and download it"""
        url = SNAPSHOT_TIMESTAMP_ENDPOINT
        snapshot = ""
        payload = {"doorbot_ids": [doorbell._attrs.get("id")]}
        if retries > 1:
            doorbell._ring.query(url, method="POST", json=payload)
        request_time = time.time()
        for _ in range(retries):
            time.sleep(delay)
            if retries > 1:
                response = doorbell._ring.query(url, method="POST", json=payload).json()
                #if response["timestamps"][0]["timestamp"] / 1000 > request_time:
            try:
                snapshot = doorbell._ring.query(
                    SNAPSHOT_ENDPOINT.format(doorbell._attrs.get("id"), raw=True)
                ).content
            except:
                self.logger.debug(f"get_snapshot - ring returned 404 for device: {doorbell.name}")
            if filename:
                with open(filename, "wb") as jpg:
                   jpg.write(snapshot)
                return True
            return snapshot
        return False

