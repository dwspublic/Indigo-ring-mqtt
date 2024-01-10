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

        self.ringmqtt_devices = []
        self.ring_cameras = {}
        self.ring_motion_devices = {}
        self.get_ring_cameras()
        self.mqttPlugin = indigo.server.getPlugin("com.flyingdiver.indigoplugin.mqtt")
        if not self.mqttPlugin.isEnabled():
            self.logger.warning("MQTT Connector plugin not enabled!")

        if old_version := self.pluginPrefs.get("version", "0.0.0") != self.pluginVersion:
            self.logger.debug(f"Upgrading plugin from version {old_version} to {self.pluginVersion}")
            self.pluginPrefs["version"] = self.pluginVersion

    def startup(self):
        self.logger.info("Starting ringmqtt")
        indigo.server.subscribeToBroadcast("com.flyingdiver.indigoplugin.mqtt", "com.flyingdiver.indigoplugin.mqtt-message_queued", "message_handler")

    def message_handler(self, notification):
        self.logger.debug(f"message_handler: MQTT message {notification['message_type']} from {indigo.devices[int(notification['brokerID'])].name}")
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

                    if topic_parts[3] != device.address:     # wrong device
                      continue

                    if topic_parts[2] == "camera":
                        if topic_parts[4] == "motion" and topic_parts[5] == "state":
                            #device.updateStateOnServer(key=ringmqtt_status, value=payload)
                            if payload == "ON":
                                device.updateStateOnServer(key="onOffState", value=True)
                            else:
                                device.updateStateOnServer(key="onOffState", value=False)

                        if topic_parts[4] == "battery" and topic_parts[5] == "attributes":
                            p = json.loads(payload)
                            device.updateStateOnServer(key="batteryLevel", value=p["batteryLife"])

            if topic_parts[0] == "homeassistant":
                continue

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
        for aID in self.ring_cameras:
            retList.append((aID, self.ring_cameras[aID][1]))
        retList.sort(key=lambda tup: tup[1])
        return retList
    def get_ring_cameras(self):
        self.ring_cameras = {}
        self.ring_cameras["3045115dc3f1"] = [0, "Front Door", "Ring", "Doorbell 2", "3045115dc3f1"]
        self.ring_cameras["60b6e1b5eb26"] = [1, "Side", "Ring", "Spotlight Cam", "60b6e1b5eb26"]
        self.ring_cameras["0cae7dc23ab7"] = [2, "Backyard", "Ring", "Floodlight Cam", "0cae7dc23ab7"]
        return

    def selectionChanged(self, valuesDict, typeId, devId):
        self.logger.debug("SelectionChanged")
        #if int(valuesDict["doorbell"]) in self.ring_cameras:
        self.logger.debug("Looking up deviceID %s in DeviceList Table" % valuesDict["doorbell"])
        #selectedData = self.ring_cameras[int(valuesDict["doorbell"])]
        valuesDict["doorbellId"] = self.ring_cameras[valuesDict["doorbell"]][4]
        valuesDict["address"] = self.ring_cameras[valuesDict["doorbell"]][4]
        valuesDict["name"] = self.ring_cameras[valuesDict["doorbell"]][1]
        valuesDict["manufacturer"] = self.ring_cameras[valuesDict["doorbell"]][2]
        valuesDict["model"] = self.ring_cameras[valuesDict["doorbell"]][3]

        # self.debugLog(u"\tSelectionChanged valuesDict to be returned:\n%s" % (str(valuesDict)))
        return valuesDict

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
                        "match_list": ["Match: ringmqtt", "Any: ", "Match: status"]
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

        if action.deviceAction == indigo.kDeviceAction.Unlock:
            self.logger.debug(f"actionControlDevice: Unlock {device.name}")
            self.publish_topic(device, f"ringmqtt/{device.address}/command/door", "open")

        elif action.deviceAction == indigo.kDeviceAction.Lock:
            self.logger.debug(f"actionControlDevice: Lock {device.name}")
            self.publish_topic(device, f"ringmqtt/{device.address}/command/door", "close")

        else:
            self.logger.error(f"{device.name}: actionControlDevice: Unsupported action requested: {action.deviceAction}")

    def publish_topic(self, device, topic, payload):

        mqttPlugin = indigo.server.getPlugin("com.flyingdiver.indigoplugin.mqtt")
        if not mqttPlugin.isEnabled():
            self.logger.error("MQTT Connector plugin not enabled, publish_topic aborting.")
            return

        brokerID = int(device.pluginProps['brokerID'])
        props = {
            'topic': topic,
            'payload': payload,
            'qos': 0,
            'retain': 0,
        }
        mqttPlugin.executeAction("publish", deviceId=brokerID, props=props, waitUntilDone=False)
        self.logger.debug(f"{device.name}: publish_topic: {topic} -> {payload}")

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

