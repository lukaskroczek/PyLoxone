"""
Component to create an interface to the Loxone Miniserver.

For more details about this component, please refer to the documentation at
https://github.com/JoDehli/PyLoxone
"""
import asyncio
import logging
import re
import sys
import traceback

import homeassistant.components.group as group
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (CONF_HOST, CONF_PASSWORD, CONF_PORT,
                                 CONF_USERNAME, EVENT_COMPONENT_LOADED,
                                 EVENT_HOMEASSISTANT_START,
                                 EVENT_HOMEASSISTANT_STOP)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.entity import Entity

from .api import LoxApp, LoxWs
from .const import (AES_KEY_SIZE, ATTR_AREA_CREATE, ATTR_CODE, ATTR_COMMAND,
                    ATTR_UUID, ATTR_VALUE, CMD_AUTH_WITH_TOKEN,
                    CMD_ENABLE_UPDATES, CMD_ENCRYPT_CMD, CMD_GET_KEY,
                    CMD_GET_KEY_AND_SALT, CMD_GET_PUBLIC_KEY,
                    CMD_GET_VISUAL_PASSWD, CMD_KEY_EXCHANGE, CMD_REFRESH_TOKEN,
                    CMD_REFRESH_TOKEN_JSON_WEB, CMD_REQUEST_TOKEN,
                    CMD_REQUEST_TOKEN_JSON_WEB,
                    CONF_LIGHTCONTROLLER_SUBCONTROLS_GEN, CONF_SCENE_GEN,
                    CONF_SCENE_GEN_DELAY, DEFAULT, DEFAULT_DELAY_SCENE,
                    DEFAULT_PORT, DEFAULT_TOKEN_PERSIST_NAME, DOMAIN,
                    DOMAIN_DEVICES, ERROR_VALUE, EVENT, IV_BYTES,
                    KEEP_ALIVE_PERIOD, LOXAPPPATH, LOXONE_PLATFORMS,
                    SALT_BYTES, SALT_MAX_AGE_SECONDS, SALT_MAX_USE_COUNT,
                    SECUREDSENDDOMAIN, SENDDOMAIN, TIMEOUT, TOKEN_PERMISSION,
                    TOKEN_REFRESH_DEFAULT_SECONDS, TOKEN_REFRESH_RETRY_COUNT,
                    TOKEN_REFRESH_SECONDS_BEFORE_EXPIRY, cfmt)
from .helpers import get_miniserver_type
from .miniserver import (MiniServer, get_miniserver_from_config,
                         get_miniserver_from_hass)

REQUIREMENTS = ["websockets", "pycryptodome", "numpy"]

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Required(CONF_HOST): cv.string,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
                vol.Optional(CONF_SCENE_GEN, default=True): cv.boolean,
                vol.Optional(
                    CONF_SCENE_GEN_DELAY, default=DEFAULT_DELAY_SCENE
                ): cv.positive_int,
                vol.Required(CONF_LIGHTCONTROLLER_SUBCONTROLS_GEN, default=False): bool,
            }
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

_UNDEF: dict = {}


# TODO: Implement a complete restart of the loxone component without restart HomeAssistant
# TODO: Unload device
# TODO: get version and check for updates https://update.loxone.com/updatecheck.xml?serial=xxxxxxxxx


async def async_unload_entry(hass, config_entry):
    """Restart of Home Assistant needed."""
    return False


async def async_setup(hass, config):
    """setup loxone"""
    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "import"}, data=config[DOMAIN]
            )
        )
    return True


async def async_migrate_entry(hass, config_entry):
    # _LOGGER.debug("Migrating from version %s", config_entry.version)
    if config_entry.version == 1:
        new = {**config_entry.options, CONF_LIGHTCONTROLLER_SUBCONTROLS_GEN: True}
        config_entry.options = {**new}
        config_entry.version = 2
        _LOGGER.info("Migration to version %s successful", 2)

    if config_entry.version == 2:
        new = {**config_entry.options, CONF_SCENE_GEN_DELAY: DEFAULT_DELAY_SCENE}
        config_entry.options = {**new}
        config_entry.version = 3
        _LOGGER.info("Migration to version %s successful", 3)
    return True


async def async_set_options(hass, config_entry):
    data = {**config_entry.data}
    options = {
        CONF_HOST: data.pop(CONF_HOST, ""),
        CONF_PORT: data.pop(CONF_PORT, DEFAULT_PORT),
        CONF_USERNAME: data.pop(CONF_USERNAME, ""),
        CONF_PASSWORD: data.pop(CONF_PASSWORD, ""),
        CONF_SCENE_GEN: data.pop(CONF_SCENE_GEN, ""),
        CONF_SCENE_GEN_DELAY: data.pop(CONF_SCENE_GEN_DELAY, DEFAULT_DELAY_SCENE),
        CONF_LIGHTCONTROLLER_SUBCONTROLS_GEN: data.pop(
            CONF_LIGHTCONTROLLER_SUBCONTROLS_GEN, ""
        ),
    }
    hass.config_entries.async_update_entry(config_entry, data=data, options=options)


async def async_config_entry_updated(hass, entry) -> None:
    """Handle signals of config entry being updated.

    This is a static method because a class method (bound method), can not be used with weak references.
    Causes for this is either discovery updating host address or config entry options changing.
    """
    pass


async def create_group_for_loxone_enties(hass, entites, name, object_id):
    try:
        await group.Group.async_create_group(
            hass,
            name,
            object_id=object_id,
            entity_ids=entites,
        )
    except HomeAssistantError as err:
        _LOGGER.error("Can't create group '%s' with error: %s", name, err)
    except Exception as err:
        _LOGGER.error("Can't create group '%s' with error: %s", name, err)


async def async_setup_entry(hass, config_entry):
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    if not config_entry.options:
        await async_set_options(hass, config_entry)

    miniserver = MiniServer(hass, config_entry)

    if not await miniserver.async_setup():
        return False

    hass.data[DOMAIN][miniserver.serial] = miniserver

    setup_tasks = []

    for platform in LOXONE_PLATFORMS:
        _LOGGER.debug("starting loxone {}...".format(platform))

        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(config_entry, platform)
        )
        setup_tasks.append(
            hass.async_create_task(
                async_load_platform(hass, platform, DOMAIN, {}, config_entry)
            )
        )

    if setup_tasks:
        await asyncio.wait(setup_tasks)

    config_entry.add_update_listener(async_config_entry_updated)

    new_data = _UNDEF

    if config_entry.unique_id is None:
        hass.config_entries.async_update_entry(
            config_entry, unique_id=miniserver.serial, data=new_data
        )
        # Workaround
        await asyncio.sleep(5)

    await miniserver.async_update_device_registry()

    async def message_callback(message):
        """Fire message on HomeAssistant Bus."""
        hass.bus.async_fire(EVENT, message)

    async def handle_websocket_command(call):
        """Handle websocket command services."""
        value = call.data.get(ATTR_VALUE, DEFAULT)
        device_uuid = call.data.get(ATTR_UUID, DEFAULT)
        await miniserver.api.send_websocket_command(device_uuid, value)

    async def sync_areas_with_loxone(data={}):
        create_areas = data.get(ATTR_AREA_CREATE, DEFAULT)
        if create_areas not in [True, False]:
            create_areas = False
        lox_items = []
        er_registry = er.async_get(hass)
        ar_registry = ar.async_get(hass)
        for id, entry in er_registry.entities.items():
            if entry.platform == DOMAIN:
                state = hass.states.get(entry.entity_id)
                if hasattr(state, "attributes") and "room" in state.attributes:
                    area = ar_registry.async_get_area_by_name(state.attributes["room"])
                    if area is None and create_areas:
                        area = ar_registry.async_get_or_create(state.attributes["room"])
                    if area and entry.area_id is None:
                        lox_items.append((entry.entity_id, area.id))

        for _ in lox_items:
            er_registry.async_update_entity(_[0], area_id=_[1])

    async def handle_sync_areas_with_loxone(call):
        await sync_areas_with_loxone(call.data)

    async def loxone_discovered(event):
        miniserver = get_miniserver_from_hass(hass)
        if miniserver.miniserver_type < 2 and "component" in event.data:
            if event.data["component"] == DOMAIN:
                try:
                    _LOGGER.info("loxone discovered")
                    await asyncio.sleep(0.1)
                    # await sync_areas_with_loxone()
                    entity_ids = hass.states.async_all()
                    sensors_analog = []
                    sensors_digital = []
                    switches = []
                    covers = []
                    lights = []
                    dimmers = []
                    climates = []
                    fans = []
                    accontrols = []

                    for s in entity_ids:
                        s_dict = s.as_dict()
                        attr = s_dict["attributes"]
                        if "platform" in attr and attr["platform"] == DOMAIN:
                            device_typ = attr.get("device_typ", "")
                            if device_typ == "analog_sensor":
                                sensors_analog.append(s_dict["entity_id"])
                            elif device_typ == "digital_sensor":
                                sensors_digital.append(s_dict["entity_id"])
                            elif device_typ in ["Jalousie", "Gate", "Window"]:
                                covers.append(s_dict["entity_id"])
                            elif device_typ in ["Switch", "Pushbutton", "TimedSwitch"]:
                                switches.append(s_dict["entity_id"])
                            elif device_typ in ["LightControllerV2"]:
                                lights.append(s_dict["entity_id"])
                            elif device_typ == "Dimmer":
                                dimmers.append(s_dict["entity_id"])
                            elif device_typ == "IRoomControllerV2":
                                climates.append(s_dict["entity_id"])
                            elif device_typ == "Ventilation":
                                fans.append(s_dict["entity_id"])
                            elif device_typ == "AcControl":
                                accontrols.append(s_dict["entity_id"])

                    sensors_analog.sort()
                    sensors_digital.sort()
                    covers.sort()
                    switches.sort()
                    lights.sort()
                    climates.sort()
                    dimmers.sort()
                    fans.sort()
                    accontrols.sort()

                    await create_group_for_loxone_enties(
                        hass, sensors_analog, "Loxone Analog Sensors", "loxone_analog"
                    )
                    await create_group_for_loxone_enties(
                        hass,
                        sensors_digital,
                        "Loxone Digital Sensors",
                        "loxone_digital",
                    )
                    await create_group_for_loxone_enties(
                        hass, switches, "Loxone Switches", "loxone_switches"
                    )
                    await create_group_for_loxone_enties(
                        hass, covers, "Loxone Covers", "loxone_covers"
                    )
                    await create_group_for_loxone_enties(
                        hass, lights, "Loxone LightControllers", "loxone_lights"
                    )
                    await create_group_for_loxone_enties(
                        hass, lights, "Loxone Dimmer", "loxone_dimmers"
                    )
                    await create_group_for_loxone_enties(
                        hass, climates, "Loxone Room Controllers", "loxone_climates"
                    )
                    await create_group_for_loxone_enties(
                        hass,
                        fans,
                        "Loxone Ventilation Controllers",
                        "loxone_ventilations",
                    )
                    await create_group_for_loxone_enties(
                        hass,
                        accontrols,
                        "Loxone AC Controllers",
                        "loxone_accontrollers",
                    )
                    await hass.async_block_till_done()
                    await create_group_for_loxone_enties(
                        hass,
                        [
                            "group.loxone_analog",
                            "group.loxone_digital",
                            "group.loxone_switches",
                            "group.loxone_covers",
                            "group.loxone_lights",
                            "group.loxone_ventilations",
                        ],
                        "Loxone Group",
                        "loxone_group",
                    )
                except Exception as err:
                    _LOGGER.error("Error Group generation: %s", err)

    await miniserver.async_set_callback(message_callback)

    res = await miniserver.start_ws()
    if not res:
        return False

    for platform in ["scene"]:
        _LOGGER.debug("starting loxone {}...".format(platform))
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(config_entry, platform)
        )

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, miniserver.start_loxone)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, miniserver.stop_loxone)
    hass.bus.async_listen_once(EVENT_COMPONENT_LOADED, loxone_discovered)

    hass.bus.async_listen(SENDDOMAIN, miniserver.listen_loxone_send)
    hass.bus.async_listen(SECUREDSENDDOMAIN, miniserver.listen_loxone_send)

    hass.services.async_register(
        DOMAIN, "event_websocket_command", handle_websocket_command
    )

    hass.services.async_register(DOMAIN, "sync_areas", handle_sync_areas_with_loxone)

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    return True


class LoxoneEntity(Entity):
    """
    @DynamicAttrs
    """

    def __init__(self, **kwargs):
        self._name = ""
        for key in kwargs:
            if not hasattr(self, key):
                setattr(self, key, kwargs[key])
            else:
                try:
                    setattr(self, key, kwargs[key])
                except AttributeError:
                    _LOGGER.error(f"Could set {key} for {self._name}")
                except (Exception,):
                    traceback.print_exc()
                    sys.exit(-1)

        self.listener = None

    async def async_added_to_hass(self):
        """Subscribe to device events."""
        self.listener = self.hass.bus.async_listen(EVENT, self.event_handler)

    async def async_will_remove_from_hass(self):
        """Disconnect callbacks."""
        self.listener = None

    async def event_handler(self, e):
        pass

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, n):
        self._name = n

    @staticmethod
    def _clean_unit(lox_format):
        search = re.search(cfmt, lox_format, flags=re.X)
        if search:
            unit = lox_format.replace(search.group(0).strip(), "").strip()
            if unit == "%%":
                unit = unit.replace("%%", "%")
            return unit
        else:
            return lox_format

    @staticmethod
    def _get_format(lox_format):
        search = re.search(cfmt, lox_format, flags=re.X)
        if search:
            return search.group(0).strip()
        return None

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self.uuidAction
