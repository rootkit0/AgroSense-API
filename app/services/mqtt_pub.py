import os
import paho.mqtt.client as mqtt

def publish_retained(topic: str, payload: str, qos: int = 1, timeout_s: int = 5) -> None:
    host = os.getenv("MQTT_HOST", "188.26.214.67")
    port = int(os.getenv("MQTT_PORT", "1883"))
    client_id = os.getenv("MQTT_CLIENT_ID_PREFIX", "sense-api")
    c = mqtt.Client(client_id=client_id)
    c.connect(host, port, keepalive=20)
    c.loop_start()
    info = c.publish(topic, payload=payload, qos=qos, retain=True)
    info.wait_for_publish(timeout=timeout_s)
    c.loop_stop()
    c.disconnect()
