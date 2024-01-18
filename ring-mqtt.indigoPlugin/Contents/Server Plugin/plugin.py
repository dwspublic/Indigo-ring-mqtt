#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################

import logging
import json
try:
    import indigo
except ImportError:
    pass

RINGMQTT_MESSAGE_TYPE = "##ring##"


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
        self.brokerID = self.pluginPrefs["brokerID"]

        self.ringmqtt_devices = []
        self.ring_devices = {}
        self.ring_battery_devices = {}

        if "ring_devices" not in self.pluginPrefs:
            self.pluginPrefs["ring_devices"] = str(self.ring_devices)
        if "ring_battery_devices" not in self.pluginPrefs:
            self.pluginPrefs["ring_battery_devices"] = str(self.ring_battery_devices)

        self.ring_devices = eval(self.pluginPrefs["ring_devices"])
        self.ring_battery_devices = eval(self.pluginPrefs["ring_battery_devices"])

        self.mqttPlugin = indigo.server.getPlugin("com.flyingdiver.indigoplugin.mqtt")
        if not self.mqttPlugin.isEnabled():
            self.logger.warning("MQTT Connector plugin not enabled!")

        if old_version := self.pluginPrefs.get("version", "0.0.0") != self.pluginVersion:
            self.logger.debug(f"Upgrading plugin from version {old_version} to {self.pluginVersion}")
            self.pluginPrefs["version"] = self.pluginVersion

    def startup(self):
        self.logger.info("Starting ringmqtt")
        indigo.server.subscribeToBroadcast("com.flyingdiver.indigoplugin.mqtt", "com.flyingdiver.indigoplugin.mqtt-message_queued", "message_handler")

    def shutdown(self):
        self.logger.info("Stopping ringmqtt")
        self.pluginPrefs["ring_devices"] = str(self.ring_devices)
        self.pluginPrefs["ring_battery_devices"] = str(self.ring_battery_devices)
    def message_handler(self, notification):
        self.logger.debug(f"message_handler: MQTT message {notification['message_type']} from {indigo.devices[int(self.brokerID)].name}")
        self.processMessage(notification)

    def deviceStartComm(self, device):
        self.logger.info(f"{device.name}: Starting Device")
        if device.id not in self.ringmqtt_devices:
            self.ringmqtt_devices.append(device.id)

    def deviceStopComm(self, device):
        self.logger.info(f"{device.name}: Stopping Device")
        if device.id in self.ringmqtt_devices:
            self.ringmqtt_devices.remove(device.id)

    def processMessage(self, notification):

        if notification["message_type"] != RINGMQTT_MESSAGE_TYPE:
            return

        props = {'message_type': RINGMQTT_MESSAGE_TYPE}
        brokerID = int(notification['brokerID'])
        while True:
            message_data = self.mqttPlugin.executeAction("fetchQueuedMessage", deviceId=brokerID, props=props, waitUntilDone=True)
            if message_data is None:
                break
            self.logger.debug(f"processMessage: {message_data}")

            topic_parts = message_data["topic_parts"]
            payload = message_data["payload"]

            if topic_parts[0] == "ring":
                for device_id in self.ringmqtt_devices:
                    device = indigo.devices[device_id]

                    ringdevice_id = topic_parts[1] + "-" + device.deviceTypeId[4:5] + "-" + topic_parts[3]

                    if ringdevice_id != device.address:     # wrong device
                        continue

                    if topic_parts[2] == "camera":
                        self.processCMessage(device, topic_parts, payload)
                        self.processBMessage(device, topic_parts, payload)

                    if topic_parts[2] == "lighting":
                        self.processLMessage(device, topic_parts, payload)
                        self.processBMessage(device, topic_parts, payload)

                    if topic_parts[2] == "chime":
                        self.processZMessage(device, topic_parts, payload)

            elif topic_parts[0] == "homeassistant":
                self.processHAMessage(topic_parts, payload)

    def processHAMessage(self, topic_parts, payload):

        if topic_parts[1] == "binary_sensor":
            if "_motion" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_devices[topic_parts[2] + "-M-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0], topic_parts[2], "RingMotion"]
                return
            if "_ding" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_devices[topic_parts[2] + "-D-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0], topic_parts[2], "RingDoorbell"]
                return
        if topic_parts[1] == "sensor":
            if "_battery" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_battery_devices[topic_parts[2] + "-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0], topic_parts[2], "Battery"]
                return
        if topic_parts[1] == "camera":
            if "_snapshot" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_devices[topic_parts[2] + "-C-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0],topic_parts[2], "RingCamera"]
                return
        if topic_parts[1] == "switch":
            if "_siren" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_devices[topic_parts[2] + "-S-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0], topic_parts[2], "RingSiren"]
                return
            if "_snooze" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_devices[topic_parts[2] + "-Z-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0], topic_parts[2], "RingZChime"]
                return
        if topic_parts[1] == "light":
            if "_light" in topic_parts[3]:
                p = json.loads(payload)
                q = p["device"]
                self.ring_devices[topic_parts[2] + "-L-" + q["ids"][0]] = [q["name"], q["mf"], q["mdl"], q["ids"][0], topic_parts[2], "RingLight"]
                return
    def processCMessage(self, device, topic_parts, payload):

        if topic_parts[4] == "status":
            device.updateStateOnServer(key="status", value=payload)
        if topic_parts[4] == "info":
            p = json.loads(payload)
            device.updateStateOnServer(key="firmwareStatus", value=p["firmwareStatus"])
            if device.deviceTypeId == "RingCamera":
                device.updateStateOnServer(key="stream_Source", value=p["stream_Source"])
                device.updateStateOnServer(key="still_Image_URL", value=p["still_Image_URL"])
        if topic_parts[4] == "motion" and topic_parts[5] == "state" and device.deviceTypeId == "RingMotion":
            if payload == "ON":
                device.updateStateOnServer(key="onOffState", value=True)
            else:
                device.updateStateOnServer(key="onOffState", value=False)

        if topic_parts[4] == "motion" and topic_parts[5] == "attributes" and device.deviceTypeId == "RingMotion":
            p = json.loads(payload)
            device.updateStateOnServer(key="lastMotionTime", value=p["lastMotionTime"])
            device.updateStateOnServer(key="personDetected", value=p["personDetected"])
            device.updateStateOnServer(key="motionDetectionEnabled", value=p["motionDetectionEnabled"])

        if topic_parts[4] == "ding" and topic_parts[5] == "state" and device.deviceTypeId == "RingDoorbell":
            if payload == "ON":
                device.updateStateOnServer(key="onOffState", value=True)
            else:
                device.updateStateOnServer(key="onOffState", value=False)

        if topic_parts[4] == "ding" and topic_parts[5] == "attributes" and device.deviceTypeId == "RingDoorbell":
            p = json.loads(payload)
            device.updateStateOnServer(key="lastDingTime", value=p["lastDingTime"])

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

        if topic_parts[4] == "battery" and topic_parts[5] == "attributes":
            p = json.loads(payload)
            if "batteryLife2" in p:
                if int(p["batteryLife2"]) > int(p["batteryLife"]) and self.pluginPrefs["batterystateUI"]:
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

        if topic_parts[4] == "status":
            device.updateStateOnServer(key="status", value=payload)
        if topic_parts[4] == "info":
            p = json.loads(payload)
            device.updateStateOnServer(key="firmwareStatus", value=p["firmwareStatus"])
        if topic_parts[4] == "light" and topic_parts[5] == "state" and device.deviceTypeId == "RingLight":
            if payload == "ON":
                device.updateStateOnServer(key="onOffState", value=True)
            else:
                device.updateStateOnServer(key="onOffState", value=False)
        if topic_parts[4] == "light" and topic_parts[5] == "brightness_state" and device.deviceTypeId == "RingLight":
            device.updateStateOnServer(key="brightness_state", value=payload)
        if topic_parts[4] == "beam_duration" and topic_parts[5] == "state" and device.deviceTypeId == "RingLight":
            device.updateStateOnServer(key="beam_duration", value=payload)

    def processZMessage(self, device, topic_parts, payload):

        if topic_parts[4] == "status":
            device.updateStateOnServer(key="status", value=payload)
        if topic_parts[4] == "info":
            p = json.loads(payload)
            device.updateStateOnServer(key="firmwareStatus", value=p["firmwareStatus"])
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
                retList.append((aID, self.ring_devices[aID][0] + " (" + self.ring_devices[aID][1] + " - " + self.ring_devices[aID][2] + ")"))
        retList.sort(key=lambda tup: tup[1])
        return retList

    def selectionChanged(self, valuesDict, typeId, devId):
        self.logger.debug("SelectionChanged")
        self.logger.debug("Looking up deviceID %s in DeviceList Table" % valuesDict["doorbell"])
        #selectedData = self.ring_cameras[int(valuesDict["doorbell"])]
        if valuesDict["doorbell"] in self.ring_devices:
            valuesDict["doorbellId"] = valuesDict["doorbell"]
            valuesDict["address"] = valuesDict["doorbell"]
            valuesDict["name"] = self.ring_devices[valuesDict["doorbell"]][0]
            valuesDict["manufacturer"] = self.ring_devices[valuesDict["doorbell"]][1]
            valuesDict["model"] = self.ring_devices[valuesDict["doorbell"]][2]
            p = self.ring_devices[valuesDict["doorbellId"]][4] + "-" + self.ring_devices[valuesDict["doorbellId"]][3]
            if p in self.ring_battery_devices:
                valuesDict["SupportsBatteryLevel"] = True
            else:
                valuesDict["SupportsBatteryLevel"] = False

        #self.logger.debug(u"\tSelectionChanged valuesDict to be returned:\n%s" % (str(valuesDict)))
        return valuesDict

    def force_ha_messages(self, valuesDict, typeId):

        self.ring_devices = {}
        self.ring_battery_devices = {}
        brokerID = int(self.brokerID)
        self.publish_topic(brokerID, "HA_Message_Refresh", f"hass/status", "online")
        self.logger.info(f"Sent Haas/Status - online - devices should be updated shortly")

        return True

    def create_trigger(self, valuesDict, typeId):

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
                self.logger.info(f"Created trigger '{name} for message type '{RINGMQTT_MESSAGE_TYPE}'")

        return True

    ########################################
    # Relay / Dimmer Action callback
    ########################################

    def actionControlDevice(self, action, device):

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

    def publish_topic(self, brokerID, devicename, topic, payload):

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
        self.logger.debug(f"{devicename}: publish_topic: {topic} -> {payload}")

    ########################################
    ########################################
    # PluginConfig methods
    ########################################

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if not userCancelled:
            self.logLevel = int(valuesDict.get("logLevel", logging.INFO))
            self.indigo_log_handler.setLevel(self.logLevel)

    ########################################
    # Custom Plugin Action callbacks (defined in Actions.xml)
    ########################################

    def pickDevice(self, filter=None, valuesDict=None, typeId=0, targetId=0):
        retList = []
        for devID in self.shimDevices:
            device = indigo.devices[int(devID)]
            retList.append((device.id, device.name))
        retList.sort(key=lambda tup: tup[1])
        return retList
