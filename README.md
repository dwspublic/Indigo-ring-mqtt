# Indigo-ring-mqtt

Plugin for the Indigo Home Automation system to communicate with ring devices via MQTT.

The plugin requires the ring-mqtt project (https://github.com/tsightler/ring-mqtt) to be running on your network.  The preferred method is in a docker container.  The ring-mqtt project is straightforward to install and configure to talk with the ring API using your ring credentials. It uses MQTT to publish messages for Indigo-ring-mqtt to subscribe to.  Testing has been done with latest version of ring-mqtt (v5.6.3).

The plugin also requires the Indigo MQTT Connector plugin (https://www.indigodomo.com/pluginstore/211/) and an MQTT Broker (such as https://www.indigodomo.com/pluginstore/260/).

Currently, the plugin supports Cameras, Doorbells, Chimes and Lighting.   It exposes them in Indigo as Cameras, Motion, Lights, Doorbells, Sirens and Chimes.  I will be adding ring alarm devices in the future.  Each ring device translates into one or more Indigo devices.  For example, a ring camera could have an Indigo Camera device, Indigo Motion device, Indigo Light device and Indigo Siren device.  Battery Level (single and dual) is also supported.

## Installation

1) Install and configure your MQTT Broker.  You can use the MQTT Broker in the Indigo Plugin Store, or any other Broker as well.

2) Install the MQTT Connector plugin and configure it to connect to your MQTT Broker.  Subscribe to the 
ring topic ("ring/#") as well as the homeassistant topic ("homeassistant/#). 

3) Install the ring-mqtt docker container and configure it to connect to your MQTT broker. Set the
MQTT topic prefix to "ring/".  Docker install instructions are here (https://github.com/tsightler/ring-mqtt/wiki/Installation-(Docker).

4) Install the indigo-ring-mqtt plugin as usual.  In the initial configuration dialog, you must select the MQTT Broker created in step #2.

5) Before creating any indigo devices, create the two necessary triggers using the associated plugin menu command.  This is only done once.  You may want to select "Hide trigger executions in Event Log" once you've confirmed messages are flowing (there are lots of messages published by ring-mqtt).

6) Wait for 10 to 20 seconds for the plugin to discover your ring devices, and then go ahead and create/map your ring devices in Indigo.  You can pick the associated ring device from the new device dropdown lists.
