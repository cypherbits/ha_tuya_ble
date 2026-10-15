"""Tests for the socket support resolved from the device description.

A Tuya BLE socket with energy metering carries the switch plus the metering
data points in its cloud specification whichever product id it is sold under,
so the entities are built from that description instead of a product id table.
"""

import json
from unittest.mock import Mock

from bleak.backends.device import BLEDevice

from homeassistant.core import HomeAssistant

from custom_components.tuya_ble.devices import (
    TuyaBLEProductInfo,
    find_device_function,
    is_energy_plug,
)
from custom_components.tuya_ble.sensor import TuyaBLESensor
from custom_components.tuya_ble.sensor import (
    get_energy_plug_mapping as get_plug_sensor_mapping,
)
from custom_components.tuya_ble.switch import (
    get_energy_plug_mapping as get_plug_switch_mapping,
)
from custom_components.tuya_ble.tuya_ble import TuyaBLEDevice
from custom_components.tuya_ble.tuya_ble.const import TuyaBLEDataPointType

ADDRESS = "00:11:22:33:44:55"


def _function(code: str, dp_id: int, dtype: str = "Value", **values) -> dict:
    return {
        "code": code,
        "dp_id": dp_id,
        "type": dtype,
        "values": json.dumps(values) if values else "{}",
    }


def _device(*functions: dict) -> TuyaBLEDevice:
    device = TuyaBLEDevice(
        Mock(), BLEDevice(name="plug", address=ADDRESS, details="")
    )
    device.append_functions(list(functions), [])
    return device


def _plug_device() -> TuyaBLEDevice:
    """A typical socket with metering: switch, voltage, current, power, energy."""
    return _device(
        _function("switch_1", 1, "Boolean"),
        _function("cur_voltage", 20, unit="V", min=0, max=30000, scale=1, step=1),
        _function("cur_current", 18, unit="mA", min=0, max=30000, scale=0, step=1),
        _function("cur_power", 19, unit="W", min=0, max=50000, scale=1, step=1),
        _function("add_ele", 17, unit="kWh", min=0, max=50000, scale=3, step=1),
        _function("countdown_1", 9),
    )


def test_energy_plug_is_detected_from_the_data_points() -> None:
    """The switch plus a metering code is what makes a device a plug."""
    assert is_energy_plug(_plug_device())


def test_a_bare_switch_is_not_a_plug() -> None:
    """A switch without metering is not treated as a socket."""
    assert not is_energy_plug(_device(_function("switch", 1, "Boolean")))


def test_metering_without_a_switch_is_not_a_plug() -> None:
    """Metering data points alone are not enough."""
    assert not is_energy_plug(
        _device(_function("cur_power", 19, unit="W", scale=1))
    )


def test_other_products_are_not_treated_as_plugs() -> None:
    """A temperature/humidity sensor is left alone."""
    sensor = _device(
        _function("temp_current", 1, unit="℃", scale=1),
        _function("humidity_value", 2, unit="%", scale=0),
        _function("battery_percentage", 4, unit="%", scale=0),
        _function("switch", 9, "Boolean"),
    )

    assert not is_energy_plug(sensor)


def test_plug_switch_uses_the_device_data_point() -> None:
    """The switch entity is bound to the dp_id the device declares."""
    mappings = get_plug_switch_mapping(_plug_device())

    assert len(mappings) == 1
    mapping = mappings[0]
    assert mapping.dp_id == 1
    assert mapping.dp_type == TuyaBLEDataPointType.DT_BOOL
    assert mapping.description.key == "switch"


def test_plug_switch_is_absent_without_the_signature() -> None:
    """Nothing is exposed for a device that is not a socket."""
    assert get_plug_switch_mapping(_device(_function("switch", 1, "Boolean"))) == []


def test_plug_sensors_follow_the_declared_description() -> None:
    """Unit and scale come from the device, so the values are correctly scaled."""
    mappings = {m.description.key: m for m in get_plug_sensor_mapping(_plug_device())}

    assert set(mappings) == {"cur_voltage", "cur_current", "cur_power", "add_ele"}
    assert mappings["cur_voltage"].dp_id == 20
    assert mappings["cur_voltage"].coefficient == 10.0
    assert mappings["cur_voltage"].description.native_unit_of_measurement == "V"
    assert mappings["cur_current"].coefficient == 1.0
    assert mappings["cur_power"].coefficient == 10.0
    assert mappings["add_ele"].coefficient == 1000.0
    assert mappings["add_ele"].description.native_unit_of_measurement == "kWh"


async def test_sensor_reports_the_scaled_value(hass: HomeAssistant) -> None:
    """A raw 2301 with scale 1 is reported as 230.1 V."""
    device = _plug_device()
    mapping = next(
        m for m in get_plug_sensor_mapping(device) if m.description.key == "cur_voltage"
    )
    entity = TuyaBLESensor(
        hass,
        Mock(),
        device,
        TuyaBLEProductInfo(name="Smart plug"),
        mapping,
    )
    entity.async_write_ha_state = Mock()
    device.datapoints._update_from_device(
        mapping.dp_id, 0, 0, TuyaBLEDataPointType.DT_VALUE, 2301
    )

    entity._handle_coordinator_update()

    assert entity.native_value == 230.1


def test_documented_codes_are_reused_from_status_range() -> None:
    """A read only data point is still found in the status range."""
    device = TuyaBLEDevice(
        Mock(), BLEDevice(name="plug", address=ADDRESS, details="")
    )
    device.append_functions(
        [_function("switch_1", 1, "Boolean")],
        [_function("cur_power", 19, unit="W", scale=1)],
    )

    assert find_device_function(device, ("cur_power",)).dp_id == 19
    assert is_energy_plug(device)
    assert [m.dp_id for m in get_plug_sensor_mapping(device)] == [19]
