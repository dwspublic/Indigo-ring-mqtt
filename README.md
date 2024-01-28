# Indigo-ring-mqtt
Plugin for the Indigo Home Automation system to communicate with ring devices via MQTT.

The plugin requires the ring-mqtt project (https://github.com/tsightler/ring-mqtt) to be running on your network.  The preferred method is in a docker container.  The ring-mqtt project is straightforward to install and configure to talk with the ring API. It uses MQTT to send status updates and receive commands.

The plugin also requires the Indigo MQTT Connector plugin (https://www.indigodomo.com/pluginstore/211/) and an MQTT Broker (such as https://www.indigodomo.com/pluginstore/260/).

## Installation

Install and configure your MQTT Broker.

Install the MQTT Connector plugin and configure it to connect to your MQTT Broker.  Subscribe to the 
ring topic ("ring/#") as well as the homeassistant topic ("homeassistant/#). 

Install the ring-mqtt docker container and configure it to connect to your MQTT broker. Set the
MQTT topic prefix to "ring/"

Install the plugin as usual.  And then go ahead and create your ring devices in Indigo.  You can pick the associated ring device from the dropdown list.

Create a trigger using the plugin Menu command.  This is only done once.

Currently the plugin supports Cameras, Doorbells, Chimes and Lighting.   It exposes them in Indigo as Cameras, Motion, Lights, Doorbells, Sirens and Chimes.  I will be adding ring alarm devices in the future.  Each ring device translates into one or more Indigo devices.  For example, a ring camera could have an Indigo Camera device, Indigo Motion device, Indigo Light device and Indigo Siren device.  Battery Level (single and dual) is also supported.

