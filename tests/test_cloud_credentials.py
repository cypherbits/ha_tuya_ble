"""Tests for matching Tuya cloud credentials to a scanned BLE device.

"Device is not registered in Tuya cloud" is shown whenever the credential
lookup comes back empty, so the cache builder must (a) index and look up by a
normalized address and (b) keep the real reason a lookup failed.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

import custom_components.tuya_ble as integration
from custom_components.tuya_ble import cloud as cloud_module
from custom_components.tuya_ble.cloud import (
    CLOUD_ERROR_NO_DEVICES,
    CLOUD_ERROR_NO_FACTORY_MAC,
    CLOUD_ERROR_PERMISSION_DENIED,
    HASSTuyaBLEDeviceManager,
    TuyaCloudCacheItem,
)
from custom_components.tuya_ble.config_flow import _cloud_lookup_error_key
from custom_components.tuya_ble.const import (
    CONF_CATEGORY,
    CONF_DEVICE_NAME,
    CONF_LOCAL_KEY,
    TUYA_API_PERMISSION_DENIED_CODE,
    TUYA_API_SUBSCRIPTION_EXPIRED_CODE,
)


DEVICE = {
    "id": "device-1",
    "uuid": "uuid-1",
    "local_key": "0123456789abcdef",
    "category": "cz",
    "product_id": "product-1",
    "name": "Energy plug",
    "model": "Model 1",
    "product_name": "Energy plug",
}

PERMISSION_ERROR = {
    "success": False,
    "code": TUYA_API_PERMISSION_DENIED_CODE,
    "msg": "API permission package error",
}

# What every Smart Home API answers once the Cloud Development plan expires.
EXPIRED_SUBSCRIPTION = {
    "success": False,
    "code": TUYA_API_SUBSCRIPTION_EXPIRED_CODE,
    "msg": "No permissions. Your subscription to cloud development plan has expired.",
}


class _FakeApi:
    """Minimal stand-in for ``TuyaOpenAPI`` that answers the cache URLs."""

    def __init__(
        self,
        *,
        devices: dict | None = None,
        factory_info: dict | None = None,
        specification: dict | None = None,
    ) -> None:
        self.token_info = SimpleNamespace(uid="fixture-uid")
        self.calls: list[str] = []
        self._responses = {
            "/users/": (
                devices
                if devices is not None
                else {"success": True, "result": [DEVICE]}
            ),
            "factory-infos": (
                factory_info
                if factory_info is not None
                else {
                    "success": True,
                    "result": [{"id": "device-1", "mac": "DC:23:50:DE:BD:04"}],
                }
            ),
            "/specifications": (
                specification
                if specification is not None
                else {"success": True, "result": {"functions": [], "status": []}}
            ),
        }

    def get(self, url: str) -> dict:
        self.calls.append(url)
        for fragment, response in self._responses.items():
            if fragment in url:
                return response
        raise AssertionError(f"unexpected URL: {url}")


async def _fill(
    hass: HomeAssistant, api: _FakeApi
) -> tuple[HASSTuyaBLEDeviceManager, TuyaCloudCacheItem]:
    manager = HASSTuyaBLEDeviceManager(hass, {})
    item = TuyaCloudCacheItem(api=api, login={}, credentials={})
    await manager._fill_cache_item(item)
    return manager, item


@pytest.mark.parametrize(
    "raw_mac",
    (
        "DC:23:50:DE:BD:04",
        "dc:23:50:de:bd:04",
        "dc2350debd04",
        "DC-23-50-DE-BD-04",
    ),
)
async def test_mac_is_normalized_to_one_cache_key(
    hass: HomeAssistant, raw_mac: str
) -> None:
    """Every format the cloud returns for a MAC lands on the same key."""
    api = _FakeApi(
        factory_info={"success": True, "result": [{"id": "device-1", "mac": raw_mac}]}
    )

    _, item = await _fill(hass, api)

    assert "DC:23:50:DE:BD:04" in item.credentials
    assert item.credentials["DC:23:50:DE:BD:04"][CONF_CATEGORY] == "cz"
    assert item.credentials["DC:23:50:DE:BD:04"][CONF_DEVICE_NAME] == "Energy plug"
    assert item.credentials["DC:23:50:DE:BD:04"][CONF_LOCAL_KEY] == "0123456789abcdef"


async def test_credentials_are_found_for_a_lowercase_ble_address(
    hass: HomeAssistant,
) -> None:
    """A lower-case address from the Bluetooth stack still resolves."""
    api = _FakeApi()
    manager, item = await _fill(hass, api)

    with (
        patch.object(manager, "_has_login", return_value=True),
        patch.object(manager, "_get_cache_key", return_value="fixture-cache"),
        patch.dict(cloud_module._cache, {"fixture-cache": item}, clear=True),
        patch.object(manager, "login", new=AsyncMock(return_value={"success": True})),
        patch.object(manager, "_fill_cache_item", new=AsyncMock()),
    ):
        credentials = await manager.get_device_credentials("dc:23:50:de:bd:04")

    assert credentials is not None
    assert credentials.device_id == "device-1"
    assert credentials.uuid == "uuid-1"
    assert credentials.product_id == "product-1"


async def test_specification_is_fetched_once_per_device(hass: HomeAssistant) -> None:
    """The duplicated specification request of the cache builder is gone."""
    api = _FakeApi()

    await _fill(hass, api)

    specification_calls = [url for url in api.calls if "/specifications" in url]
    assert len(specification_calls) == 1


async def test_successful_fill_reports_no_error(hass: HomeAssistant) -> None:
    """A working lookup leaves no failure reason behind."""
    manager, item = await _fill(hass, _FakeApi())

    assert item.credentials
    assert manager.last_cloud_error is None
    assert _cloud_lookup_error_key(manager) == "device_not_registered"


async def test_permission_error_is_recorded_instead_of_an_empty_result(
    hass: HomeAssistant,
) -> None:
    """An unauthorized factory-info call is not reported as 'not registered'."""
    api = _FakeApi(factory_info=PERMISSION_ERROR)

    manager, item = await _fill(hass, api)

    assert item.credentials == {}
    assert manager.last_cloud_error == CLOUD_ERROR_PERMISSION_DENIED
    assert _cloud_lookup_error_key(manager) == "cloud_permission_denied"


async def test_denied_device_list_is_reported_as_a_permission_error(
    hass: HomeAssistant,
) -> None:
    """The device list endpoint can be denied too."""
    manager, item = await _fill(hass, _FakeApi(devices=PERMISSION_ERROR))

    assert item.credentials == {}
    assert manager.last_cloud_error == CLOUD_ERROR_PERMISSION_DENIED


async def test_expired_subscription_is_reported_as_a_permission_error(
    hass: HomeAssistant,
) -> None:
    """An expired Cloud Development plan must not read as 'not registered'."""
    manager, item = await _fill(hass, _FakeApi(devices=EXPIRED_SUBSCRIPTION))

    assert item.credentials == {}
    assert manager.last_cloud_error == CLOUD_ERROR_PERMISSION_DENIED
    assert _cloud_lookup_error_key(manager) == "cloud_permission_denied"


async def test_empty_account_is_reported_as_no_devices(hass: HomeAssistant) -> None:
    """An unlinked or wrongly scoped account returns an empty device list."""
    manager, _ = await _fill(hass, _FakeApi(devices={"success": True, "result": []}))

    assert manager.last_cloud_error == CLOUD_ERROR_NO_DEVICES
    assert _cloud_lookup_error_key(manager) == "cloud_no_devices"


async def test_device_without_a_mac_is_reported(hass: HomeAssistant) -> None:
    """Devices the cloud lists without a MAC can never be matched."""
    manager, item = await _fill(
        hass,
        _FakeApi(factory_info={"success": True, "result": [{"id": "device-1"}]}),
    )

    assert item.credentials == {}
    assert manager.last_cloud_error == CLOUD_ERROR_NO_FACTORY_MAC
    assert _cloud_lookup_error_key(manager) == "cloud_no_factory_mac"


def test_unknown_reason_falls_back_to_not_registered() -> None:
    """An unset or unknown reason keeps the previous message."""
    assert _cloud_lookup_error_key(None) == "device_not_registered"
    assert (
        _cloud_lookup_error_key(SimpleNamespace(last_cloud_error="sync_failed"))
        == "device_not_registered"
    )


def test_new_errors_are_translated_in_every_language() -> None:
    """New config-flow errors are translated everywhere."""
    component_dir = Path(integration.__file__).parent
    paths = [component_dir / "strings.json", *component_dir.glob("translations/*.json")]

    assert len(paths) > 1
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for section in ("config", "options"):
            errors = data[section]["error"]
            for key in (
                "cloud_permission_denied",
                "cloud_no_devices",
                "cloud_no_factory_mac",
            ):
                assert key in errors, f"{key} missing from {path.name}"
