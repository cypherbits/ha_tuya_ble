"""The Tuya BLE integration."""

from __future__ import annotations

import logging

from dataclasses import dataclass, replace
import json
from typing import Any, Iterable

from homeassistant.const import (
    CONF_ADDRESS,
    CONF_DEVICE_ID,
    CONF_COUNTRY_CODE,
    CONF_PASSWORD,
    CONF_USERNAME,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from tuya_mobile import TuyaMobileApiError, TuyaPasswordClient

from tuya_iot import (
    TuyaOpenAPI,
    AuthType,
)

from .tuya_ble import (
    AbstaractTuyaBLEDeviceManager,
    TuyaBLEDeviceCredentials,
)

from .const import (
    TUYA_DOMAIN,
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_APP_TYPE,
    CONF_AUTH_TYPE,
    CONF_ENDPOINT,
    CONF_PRODUCT_MODEL,
    CONF_UUID,
    CONF_LOCAL_KEY,
    CONF_MOBILE_APP,
    CONF_SEC_KEY,
    CONF_CATEGORY,
    CONF_PRODUCT_ID,
    CONF_DEVICE_NAME,
    CONF_PRODUCT_NAME,
    CONF_FUNCTIONS,
    CONF_STATUS_RANGE,
    DOMAIN,
    TUYA_API_DEVICES_URL,
    TUYA_API_FACTORY_INFO_URL,
    TUYA_API_DEVICE_SPECIFICATION,
    TUYA_API_PERMISSION_DENIED_CODE,
    TUYA_API_SUBSCRIPTION_EXPIRED_CODE,
    TUYA_FACTORY_INFO_MAC,
    TUYA_RESPONSE_CODE,
    TUYA_RESPONSE_MSG,
    TUYA_RESPONSE_RESULT,
    TUYA_RESPONSE_SUCCESS,
)
from .mobile import get_mobile_endpoint

_LOGGER = logging.getLogger(__name__)


@dataclass
class TuyaCloudCacheItem:
    """A cache model for API keys/credentials"""

    api: TuyaOpenAPI | None
    login: dict[str, Any]
    credentials: dict[str, dict[str, Any]]


CONF_TUYA_LOGIN_KEYS = [
    CONF_ENDPOINT,
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_AUTH_TYPE,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_COUNTRY_CODE,
    CONF_APP_TYPE,
]

CONF_TUYA_DEVICE_KEYS = [
    CONF_UUID,
    CONF_LOCAL_KEY,
    CONF_DEVICE_ID,
    CONF_CATEGORY,
    CONF_PRODUCT_ID,
    CONF_DEVICE_NAME,
    CONF_PRODUCT_NAME,
    CONF_PRODUCT_MODEL,
]

_cache: dict[str, TuyaCloudCacheItem] = {}


# Reasons a credential lookup can fail, exposed as `last_cloud_error` so the
# config flow can tell the user what actually went wrong instead of the generic
# "device not registered" message.
CLOUD_ERROR_PERMISSION_DENIED = "permission_denied"
CLOUD_ERROR_NO_DEVICES = "no_devices"
CLOUD_ERROR_NO_FACTORY_MAC = "no_factory_mac"
CLOUD_ERROR_SYNC_FAILED = "sync_failed"


def _normalize_address(address: str) -> str:
    """Return the uppercase, colon-separated key used by the credential cache.

    The factory-info endpoint returns the MAC either colon-separated or as a
    bare hex string, while the Bluetooth stack can hand back a lower-case
    address. Both sides of the lookup are normalized here, otherwise a device
    that is registered in the cloud can never be matched to the device seen
    over Bluetooth and setup fails with "Device is not registered in Tuya
    cloud".
    """
    clean = address.replace("-", ":").replace(".", ":").strip().upper()
    if ":" in clean:
        return clean
    return ":".join(clean[i : i + 2] for i in range(0, 12, 2))


class TuyaMobileIdentityMismatch(TuyaMobileApiError):
    """Mobile credentials do not belong to the OpenAPI device."""


class HASSTuyaBLEDeviceManager(AbstaractTuyaBLEDeviceManager):
    """Cloud connected manager of the Tuya BLE devices credentials."""

    def __init__(self, hass: HomeAssistant, data: dict[str, Any]) -> None:
        assert hass is not None
        self._hass = hass
        self._data = data
        # Populated by the credential cache builder; `None` means the last
        # lookup itself worked and simply had no entry for the given address.
        self.last_cloud_error: str | None = None

    @staticmethod
    def _is_login_success(response: dict[Any, Any]) -> bool:
        return bool(response.get(TUYA_RESPONSE_SUCCESS, False))

    @staticmethod
    def _get_cache_key(data: dict[str, Any]) -> str:
        key_dict = {key: data.get(key) for key in CONF_TUYA_LOGIN_KEYS}
        return json.dumps(key_dict)

    @staticmethod
    def _has_login(data: dict[Any, Any]) -> bool:
        for key in CONF_TUYA_LOGIN_KEYS:
            if data.get(key) is None:
                return False
        return True

    @staticmethod
    def _has_credentials(data: dict[Any, Any]) -> bool:
        for key in CONF_TUYA_DEVICE_KEYS:
            if data.get(key) is None:
                return False
        return True

    async def _login(self, data: dict[str, Any], add_to_cache: bool) -> dict[Any, Any]:
        """Login into Tuya cloud using credentials from data dictionary."""
        global _cache

        if len(data) == 0 or not self._has_login(data):
            return {}

        api = TuyaOpenAPI(
            endpoint=data.get(CONF_ENDPOINT, ""),
            access_id=data.get(CONF_ACCESS_ID, ""),
            access_secret=data.get(CONF_ACCESS_SECRET, ""),
            auth_type=data.get(CONF_AUTH_TYPE, ""),
        )
        api.set_dev_channel("hass")

        response = await self._hass.async_add_executor_job(
            api.connect,
            data.get(CONF_USERNAME, ""),
            data.get(CONF_PASSWORD, ""),
            data.get(CONF_COUNTRY_CODE, ""),
            data.get(CONF_APP_TYPE, ""),
        )

        if self._is_login_success(response):
            _LOGGER.debug("Successful login for %s", data[CONF_USERNAME])
            if add_to_cache:
                auth_type = data[CONF_AUTH_TYPE]
                if isinstance(auth_type, AuthType):
                    data[CONF_AUTH_TYPE] = auth_type.value
                cache_key = self._get_cache_key(data)
                cache_item = _cache.get(cache_key)
                if cache_item:
                    cache_item.api = api
                    cache_item.login = data
                else:
                    _cache[cache_key] = TuyaCloudCacheItem(api, data, {})

        return response

    def _check_login(self) -> bool:
        cache_key = self._get_cache_key(self._data)
        return _cache.get(cache_key) is not None

    async def login(self, add_to_cache: bool = False) -> dict[Any, Any]:
        return await self._login(self._data, add_to_cache)

    def _record_cloud_failure(self, what: str, response: dict[Any, Any]) -> None:
        """Keep why a cloud lookup failed so it can be reported to the user.

        Without this the caller only ever sees an empty credential set and
        shows "Device is not registered in Tuya cloud", which is wrong and
        misleading whenever the failure is actually an API permission or an
        account problem.
        """
        code = response.get(TUYA_RESPONSE_CODE)
        msg = response.get(TUYA_RESPONSE_MSG)
        if code in (
            TUYA_API_PERMISSION_DENIED_CODE,
            TUYA_API_SUBSCRIPTION_EXPIRED_CODE,
        ) or (isinstance(msg, str) and "permission" in msg.lower()):
            self.last_cloud_error = CLOUD_ERROR_PERMISSION_DENIED
        else:
            self.last_cloud_error = CLOUD_ERROR_SYNC_FAILED
        _LOGGER.warning(
            "Tuya cloud %s request failed (code=%s, msg=%s); device credentials "
            "cannot be read until it succeeds",
            what,
            code,
            msg,
        )

    async def _fill_cache_device(
        self, item: TuyaCloudCacheItem, device: dict[str, Any]
    ) -> None:
        """Add one cloud device to the credential cache, keyed by its BLE MAC."""
        device_id = device.get("id")
        fi_response = await self._hass.async_add_executor_job(
            item.api.get,
            TUYA_API_FACTORY_INFO_URL % (device_id),
        )
        if not self._is_login_success(fi_response):
            self._record_cloud_failure("factory info", fi_response)
            return

        fi_response_result = fi_response.get(TUYA_RESPONSE_RESULT)
        factory_info = fi_response_result[0] if fi_response_result else None
        raw_mac = factory_info.get(TUYA_FACTORY_INFO_MAC) if factory_info else None
        if not raw_mac:
            _LOGGER.warning(
                "Tuya cloud exposes no BLE MAC for device %s (%s), so it cannot "
                "be matched to the device seen over Bluetooth",
                device.get("name"),
                device_id,
            )
            return

        mac = _normalize_address(raw_mac)
        item.credentials[mac] = {
            CONF_ADDRESS: mac,
            CONF_UUID: device.get("uuid"),
            CONF_LOCAL_KEY: device.get("local_key"),
            CONF_DEVICE_ID: device_id,
            CONF_CATEGORY: device.get("category"),
            CONF_PRODUCT_ID: device.get("product_id"),
            CONF_DEVICE_NAME: device.get("name"),
            CONF_PRODUCT_MODEL: device.get("model"),
            CONF_PRODUCT_NAME: device.get("product_name"),
        }
        sec_key = device.get("sec_key") or device.get("secKey")
        if sec_key:
            item.credentials[mac][CONF_SEC_KEY] = sec_key

        # Some devices (e.g. jtmspro knob locks) report the MAC byte-reversed
        # in the cloud factory info compared to what they advertise over BLE,
        # so the credentials can never be matched to the scanned device. Index
        # the same entry under the reversed address too. Existing keys win, so
        # a device that genuinely owns that address is never clobbered.
        reversed_mac = _normalize_address(":".join(reversed(mac.split(":"))))
        item.credentials.setdefault(reversed_mac, item.credentials[mac])

        await self._fill_cache_specification(item, mac, device_id)

    async def _fill_cache_specification(
        self, item: TuyaCloudCacheItem, mac: str, device_id: str
    ) -> None:
        """Attach the device functions and status range to a cached credential."""
        spec_response = await self._hass.async_add_executor_job(
            item.api.get,
            TUYA_API_DEVICE_SPECIFICATION % device_id,
        )
        spec_response_result = spec_response.get(TUYA_RESPONSE_RESULT)
        if spec_response_result:
            functions = spec_response_result.get("functions")
            if functions:
                item.credentials[mac][CONF_FUNCTIONS] = functions
            status = spec_response_result.get("status")
            if status:
                item.credentials[mac][CONF_STATUS_RANGE] = status

    async def _fill_cache_item(self, item: TuyaCloudCacheItem) -> None:
        self.last_cloud_error = None
        devices_response = await self._hass.async_add_executor_job(
            item.api.get,
            TUYA_API_DEVICES_URL % (item.api.token_info.uid),
        )
        if not self._is_login_success(devices_response):
            self._record_cloud_failure("device list", devices_response)
            return

        devices = devices_response.get(TUYA_RESPONSE_RESULT)
        device_count = 0
        if isinstance(devices, Iterable):
            for device in devices:
                device_count += 1
                await self._fill_cache_device(item, device)

        if not item.credentials:
            if device_count == 0:
                # Nothing came back at all: the app account that owns the
                # devices is not linked to this project, or it lives in a
                # different data center.
                self.last_cloud_error = CLOUD_ERROR_NO_DEVICES
                _LOGGER.warning(
                    "Tuya cloud returned no devices for this account; no BLE "
                    "address can be resolved"
                )
            elif self.last_cloud_error is None:
                # Devices came back but none exposes a usable BLE MAC.
                self.last_cloud_error = CLOUD_ERROR_NO_FACTORY_MAC

    async def build_cache(self) -> None:
        global _cache
        data = {}
        tuya_config_entries = self._hass.config_entries.async_entries(TUYA_DOMAIN)
        for config_entry in tuya_config_entries:
            data.clear()
            data.update(config_entry.data)
            if not self._has_login(data):
                continue
            key = self._get_cache_key(data)
            item = _cache.get(key)
            if item is None or len(item.credentials) == 0:
                if self._is_login_success(await self._login(data, True)):
                    item = _cache.get(key)
                    if item and len(item.credentials) == 0:
                        await self._fill_cache_item(item)

        ble_config_entries = self._hass.config_entries.async_entries(DOMAIN)
        for config_entry in ble_config_entries:
            data.clear()
            data.update(config_entry.options)
            if not self._has_login(data):
                continue
            key = self._get_cache_key(data)
            item = _cache.get(key)
            if item is None or len(item.credentials) == 0:
                if self._is_login_success(await self._login(data, True)):
                    item = _cache.get(key)
                    if item and len(item.credentials) == 0:
                        await self._fill_cache_item(item)

    def get_login_from_cache(self) -> None:
        global _cache
        for cache_item in _cache.values():
            self._data.update(cache_item.login)
            break

    async def get_device_credentials(
        self,
        address: str,
        force_update: bool = False,
        save_data: bool = False,
    ) -> TuyaBLEDeviceCredentials | None:
        """Get credentials of the Tuya BLE device."""
        global _cache
        item: TuyaCloudCacheItem | None = None
        credentials: dict[str, any] | None = None
        result: TuyaBLEDeviceCredentials | None = None
        normalized_address = _normalize_address(address)

        if not force_update and self._has_credentials(self._data):
            credentials = self._data.copy()
        else:
            cache_key: str | None = None
            if self._has_login(self._data):
                cache_key = self._get_cache_key(self._data)
            else:
                for key in _cache.keys():
                    if _cache[key].credentials.get(normalized_address) is not None:
                        cache_key = key
                        break
            if cache_key:
                item = _cache.get(cache_key)

            if item is None or force_update:
                if self._is_login_success(await self.login(True)):
                    item = _cache.get(cache_key)
                    if item:
                        if force_update:
                            refreshed_item = TuyaCloudCacheItem(
                                item.api,
                                item.login,
                                {},
                            )
                            await self._fill_cache_item(refreshed_item)
                            item.credentials = refreshed_item.credentials
                        else:
                            await self._fill_cache_item(item)

            if item:
                credentials = item.credentials.get(normalized_address)

        if credentials:
            sec_key = credentials.get(CONF_SEC_KEY) or self._data.get(CONF_SEC_KEY)
            result = TuyaBLEDeviceCredentials(
                credentials.get(CONF_UUID, ""),
                credentials.get(CONF_LOCAL_KEY, ""),
                credentials.get(CONF_DEVICE_ID, ""),
                credentials.get(CONF_CATEGORY, ""),
                credentials.get(CONF_PRODUCT_ID, ""),
                credentials.get(CONF_DEVICE_NAME, ""),
                credentials.get(CONF_PRODUCT_MODEL, ""),
                credentials.get(CONF_PRODUCT_NAME, ""),
                credentials.get(CONF_FUNCTIONS, []),
                credentials.get(CONF_STATUS_RANGE, []),
                sec_key=sec_key,
            )
            _LOGGER.debug("Retrieved: %s", result)
            if save_data:
                if item:
                    self._data.update(item.login)
                self._data.update(credentials)
                if sec_key:
                    self._data[CONF_SEC_KEY] = sec_key

        return result

    async def get_mobile_device_credentials(
        self,
        cloud_credentials: TuyaBLEDeviceCredentials,
        save_data: bool = False,
    ) -> TuyaBLEDeviceCredentials:
        """Retrieve and validate an atomic mobile localKey/SecKey pair."""
        mobile_app = self._data.get(CONF_MOBILE_APP)
        if not mobile_app:
            raise TuyaMobileApiError("No Tuya mobile application was selected")

        client = TuyaPasswordClient.for_application(
            mobile_app,
            async_get_clientsession(self._hass),
            username=self._data.get(CONF_USERNAME, ""),
            endpoint=get_mobile_endpoint(self._data.get(CONF_ENDPOINT, "")),
        )
        await client.login_with_password(
            self._data.get(CONF_PASSWORD, ""),
            self._data.get(CONF_COUNTRY_CODE, ""),
        )
        mobile_credentials = await client.get_device_credentials(
            cloud_credentials.device_id
        )

        if mobile_credentials.device_id != cloud_credentials.device_id:
            raise TuyaMobileIdentityMismatch(
                "Tuya mobile device ID does not match the OpenAPI device"
            )
        if (
            mobile_credentials.uuid
            and cloud_credentials.uuid
            and mobile_credentials.uuid != cloud_credentials.uuid
        ):
            raise TuyaMobileIdentityMismatch(
                "Tuya mobile UUID does not match the OpenAPI device"
            )
        if (
            mobile_credentials.product_id
            and cloud_credentials.product_id
            and mobile_credentials.product_id != cloud_credentials.product_id
        ):
            raise TuyaMobileIdentityMismatch(
                "Tuya mobile product does not match the OpenAPI device"
            )

        result = replace(
            cloud_credentials,
            local_key=mobile_credentials.local_key,
            sec_key=mobile_credentials.sec_key,
        )
        if save_data:
            self._data.update(
                {
                    CONF_LOCAL_KEY: mobile_credentials.local_key,
                    CONF_SEC_KEY: mobile_credentials.sec_key,
                }
            )
        return result

    @property
    def data(self) -> dict[str, Any]:
        return self._data
