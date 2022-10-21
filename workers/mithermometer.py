from const import DEFAULT_PER_DEVICE_TIMEOUT
from exceptions import DeviceTimeoutError
from mqtt import MqttMessage, MqttConfigMessage
from interruptingcow import timeout

from workers.base import BaseWorker, retry
import logger

REQUIREMENTS = ["mithermometer==0.1.4", "bluepy"]
monitoredAttrs = ["temperature", "humidity", "battery"]
_LOGGER = logger.get(__name__)
ERRORS_TO_OFFLINE = 5

class MithermometerWorker(BaseWorker):
    per_device_timeout = DEFAULT_PER_DEVICE_TIMEOUT  # type: int
    error_count = 0
    is_online = None

    def _setup(self):
        from mithermometer.mithermometer_poller import MiThermometerPoller
        from btlewrap.bluepy import BluepyBackend

        _LOGGER.info("Adding %d %s devices", len(self.devices), repr(self))
        for name, mac in self.devices.items():
            _LOGGER.debug("Adding %s device '%s' (%s)", repr(self), name, mac)
            self.devices[name] = {
                "mac": mac,
                "poller": MiThermometerPoller(mac, BluepyBackend),
            }

    def config(self, availbility_topic):
        ret = []
        for name, data in self.devices.items():
            ret += self.config_device(name, data["mac"])
        return ret

    def config_device(self, name, mac):
        ret = []
        device = {
            "identifiers": [mac, self.format_discovery_id(mac, name)],
            "manufacturer": "Xiaomi",
            "model": "LYWSD(CGQ/01ZM)",
            "name": self.format_discovery_name(name),
        }

        for attr in monitoredAttrs:
            payload = {
                "unique_id": self.format_discovery_id(mac, name, attr),
                "name": self.format_discovery_name(name, attr),
                "state_topic": self.format_prefixed_topic(name, attr),
                "device_class": attr,
                "device": device,
            }

            if attr == "temperature":
                payload["unit_of_measurement"] = "°C"
            elif attr == "humidity":
                payload["unit_of_measurement"] = "%"
            elif attr == "battery":
                payload["unit_of_measurement"] = "%"

            ret.append(
                MqttConfigMessage(
                    MqttConfigMessage.SENSOR,
                    self.format_discovery_topic(mac, name, attr),
                    payload=payload,
                )
            )

        return ret

    def avail_offline(self, name):
        self.error_count+= 1
        _LOGGER.info("  Error count for %s is %d", name, self.error_count)
        if (self.error_count >= ERRORS_TO_OFFLINE):
            self.is_online = False
            return [MqttMessage(topic=self.format_topic(name, "availability"), payload="offline")]
        else:
            return None

    def status_update(self):
        _LOGGER.info("Updating %d %s devices", len(self.devices), repr(self))

        for name, data in self.devices.items():
            _LOGGER.debug("Updating %s device '%s' (%s)", repr(self), name, data["mac"])
            from btlewrap import BluetoothBackendException

            try:
                with timeout(self.per_device_timeout, exception=DeviceTimeoutError):
                    yield retry(self.update_device_state, retries=self.update_retries, exception_type=BluetoothBackendException)(name, data["poller"])
            except BluetoothBackendException as e:
                logger.log_exception(
                    _LOGGER,
                    "Error during update of %s device '%s' (%s): %s",
                    repr(self),
                    name,
                    data["mac"],
                    type(e).__name__,
                    suppress=True,
                )
                yield self.avail_offline(name)
            except DeviceTimeoutError:
                logger.log_exception(
                    _LOGGER,
                    "Time out during update of %s device '%s' (%s)",
                    repr(self),
                    name,
                    data["mac"],
                    suppress=True,
                )
                yield self.avail_offline(name)

    def update_device_state(self, name, poller):
        poller.clear_cache()

        data = {
            "temperature": poller.parameter_value("temperature"),
            "humidity": poller.parameter_value("humidity"),
            "battery": poller.parameter_value("battery"),
        }
        ret = [MqttMessage(topic=self.format_topic(name), payload=json.dumps(data))]
        if (self.error_count >= ERRORS_TO_OFFLINE or self.is_online is not True):
            ret.append(MqttMessage(topic=self.format_topic(name, "availability"), payload="online"))
            self.is_online = True
        self.error_count = 0
        return ret
