# python3.6

import random

from paho.mqtt import client as mqtt_client


broker = '192.168.1.10'
port = 1883
topic = "ring/dpwhwp-3qqos-0/camera/0cae7dc23ab7/snapshot/image"
# Generate a Client ID with the subscribe prefix.
client_id = f'subscribe-{random.randint(0, 100)}'
# username = 'emqx'
# password = 'public'


def connect_mqtt() -> mqtt_client:
    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            print("Connected to MQTT Broker!")
        else:
            print("Failed to connect, return code %d\n", rc)

    client = mqtt_client.Client(client_id)
    # client.username_pw_set(username, password)
    client.on_connect = on_connect
    client.connect(broker, port)
    return client


def subscribe(client: mqtt_client):
    def on_message(client, userdata, msg):
        print(f"Received a message from `{msg.topic}` topic")
        topic_parts = msg.topic.split("/")
        indigowebpath = "/Library/Application Support/Perceptive Automation/Indigo 2022.2/Web Assets/public/images/"
        indigosnapshotfilename = topic_parts[1] + '-' + topic_parts[3] + '-snapshot.jpg'
        test_file = open(f'{indigowebpath}{indigosnapshotfilename}', 'ab')
        test_file.write(msg.payload)
        test_file.close()

    client.subscribe(topic)
    client.on_message = on_message


def run():
    client = connect_mqtt()
    subscribe(client)
    client.loop_forever()


if __name__ == '__main__':
    run()
