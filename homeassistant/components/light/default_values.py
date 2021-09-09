"""Adds default on-values and circadian rhythm functionality to lights."""

from __future__ import annotations

import asyncio
from asyncio.tasks import Task, sleep
import json
import logging
import aiohttp

from homeassistant.core import HomeAssistant, ServiceCall, State, Event, callback
from homeassistant.helpers.entity import ToggleEntity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers import config_validation, entity_registry, device_registry
from homeassistant.helpers.entity_registry import EntityRegistry, RegistryEntry
from homeassistant.helpers.device_registry import DeviceRegistry, DeviceEntry
from homeassistant.helpers.event import async_track_state_change_event, async_track_state_added_domain, async_track_state_removed_domain

from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    EVENT_HOMEASSISTANT_START,
    ATTR_ENTITY_ID,
    SERVICE_TURN_ON,
    SERVICE_TURN_OFF,
    STATE_ON
)

ATTR_TRANSITION = "transition"
ATTR_BRIGHTNESS = "brightness"
ATTR_COLOR_TEMP = "color_temp"
ATTR_AUTOMATIC = "automatic"
ATTR_AUTOMATIC_UPDATE = "auto"
ATTR_OFF_AFTER = "off_after"
ATTR_DIM_TRANSITION = "dim_transition"
ATTR_OFF_TRANSITION = "off_transition"

_LOGGER = logging.getLogger("default_values")

import voluptuous as vol

VALID_TRANSITION = vol.All(vol.Coerce(float), vol.Clamp(min=0, max=6553))

VALID_BRIGHTNESS = vol.All(vol.Coerce(int), vol.Clamp(min=0, max=255))
VALID_COLOR_TEMP = vol.All(vol.Coerce(int), vol.Clamp(min=153, max=454))

LIGHT_DIM_SCHEMA = {ATTR_OFF_AFTER:VALID_TRANSITION, ATTR_DIM_TRANSITION:VALID_TRANSITION, ATTR_OFF_TRANSITION:VALID_TRANSITION}
LIGHT_AUTO_SCHEMA = {ATTR_BRIGHTNESS:VALID_BRIGHTNESS, ATTR_COLOR_TEMP:VALID_COLOR_TEMP, ATTR_TRANSITION:VALID_TRANSITION}

class OffTimer:
    def __init__(self, hass:HomeAssistant, entity_id:str, timeout:float, transition:float|None = None):
        self._hass = hass
        self._timeout = timeout
        self._entity_id = entity_id
        self._transition = transition
        self._task = default_values.hass.async_create_task(self._job())
        _LOGGER.info("Start timer: %s (%s seconds)", self._entity_id, self._timeout)
    
    all_timers = {}

    def start_timer(hass:HomeAssistant, entity_id:str, timeout:float, transition:float|None = None):
        OffTimer.cancel_timer(entity_id)
        OffTimer.all_timers[entity_id] = OffTimer(hass=hass,entity_id=entity_id,timeout=timeout,transition=transition)

    def cancel_timer(entity_id:str):
        timer:OffTimer = OffTimer.all_timers.pop(entity_id,None)
        if timer is not None:
            timer.cancel()
    
    def cancel_all_timers():
        for entity_id in OffTimer.all_timers:
            timer:OffTimer = OffTimer.all_timers[entity_id]
            if timer is not None:
                timer.cancel()
        OffTimer.all_timers.clear()

    async def _job(self):
        await asyncio.sleep(self._timeout)
        _LOGGER.info("Fire timer: %s", self._entity_id)
        if self._transition is not None:
            await self._hass.services.async_call(domain="light",service=SERVICE_TURN_OFF,service_data={ATTR_ENTITY_ID:self._entity_id,ATTR_TRANSITION:self._transition})
        else:
            await self._hass.services.async_call(domain="light",service=SERVICE_TURN_OFF,service_data={ATTR_ENTITY_ID:self._entity_id})
        OffTimer.all_timers.pop(self._entity_id)

    def cancel(self):
        if not self._task.done():
            _LOGGER.info("Cancel timer: %s", self._entity_id)
            self._task.cancel()

class default_values():

    hass:HomeAssistant = None
    entity_registry:EntityRegistry = None
    device_registry:DeviceRegistry = None
    hue_auto_brightness_group:str = None
    hue_auto_temperature_group:str = None
    session = aiohttp.ClientSession()

    hue_uniqueid = {} # unique_id -> light_number
    hass_uniqueid = {} # entity_id -> unique_id

    update_hue_groups_task:Task = None
    hue_base_uri:str = 'http://192.168.1.2/api/fzrRwobrK-cDGj3wJKiOZJ2fdiDCPrXGWKzXzGjl'

    async def process_hue():
        _LOGGER.info("process_hue")
        async with default_values.session.get(default_values.hue_base_uri+'/lights') as resp:
            _LOGGER.info(resp.status)
            json = await resp.json()
            for light_id in json:
                light = json[light_id]
                name:str = light["name"]
                unique_id:str = light['uniqueid']
                # self.Lights[id] = name
                _LOGGER.info("Discovered '%s' UniqueID: '%s'",name,unique_id)
                default_values.hue_uniqueid[unique_id] = light_id
        async with default_values.session.get(default_values.hue_base_uri+'/groups') as resp:
            _LOGGER.info(resp.status)
            json = await resp.json()
            for group_id in json:
                group = json[group_id]
                name:str = group["name"]
                if name == 'Auto Temperature':
                    default_values.hue_auto_temperature_group = group_id
                    _LOGGER.info("Hue Automatic Temperature groupid is %s", group_id)
                if name == 'Auto Brightness':
                    default_values.hue_auto_brightness_group = group_id
                    _LOGGER.info("Hue Automatic Brightness groupid is %s", group_id)
        await default_values.update_hue_groups()

    async def update_hue_groups():
        await sleep(2)
        auto_brightness_lights = []
        auto_temperature_lights = []
        for entity_id in default_values.tracking_brightness:
            unique_id = default_values.hass_uniqueid.get(entity_id,None)
            if unique_id is not None:
                light_id = default_values.hue_uniqueid.get(unique_id,None)
                if light_id is not None:
                    auto_brightness_lights.append(light_id)
                else:
                    _LOGGER.warning("Hue light_id not found for '%s' -> '%s'", entity_id, unique_id)
            else:
                _LOGGER.warning("Hue unique_id not found for '%s'", entity_id)
        for entity_id in default_values.tracking_temperature:
            unique_id = default_values.hass_uniqueid.get(entity_id,None)
            if unique_id is not None:
                light_id = default_values.hue_uniqueid.get(unique_id,None)
                if light_id is not None:
                    auto_temperature_lights.append(light_id)
                else:
                    _LOGGER.warning("Hue light_id not found for '%s' -> '%s'", entity_id, unique_id)
            else:
                _LOGGER.warning("Hue unique_id not found for '%s'", entity_id)
        if default_values.hue_auto_brightness_group is not None:
            _LOGGER.info("SET HUE GROUP %s (BRIGHTNESS) %s", default_values.hue_auto_brightness_group, str(auto_brightness_lights))
            update:str = json.dumps({'lights':auto_brightness_lights})
            uri:str = default_values.hue_base_uri+'/groups/'+default_values.hue_auto_brightness_group
            async with await default_values.session.put(uri, data=update, headers={aiohttp.hdrs.CONTENT_TYPE:'application/json'}) as res:
                _LOGGER.info("HTTP %s",str(res.status))
                _LOGGER.info("DATA %s",await res.json())
        if default_values.hue_auto_temperature_group is not None:
            _LOGGER.info("SET HUE GROUP %s (TEMPERATURE) %s", default_values.hue_auto_temperature_group, str(auto_temperature_lights))
            update:str = json.dumps({'lights':auto_temperature_lights})
            uri:str = default_values.hue_base_uri+'/groups/'+default_values.hue_auto_temperature_group
            async with await default_values.session.put(uri, data=update, headers={aiohttp.hdrs.CONTENT_TYPE:'application/json'}) as res:
                _LOGGER.info("HTTP %s",str(res.status))
                _LOGGER.info("DATA %s",await res.json())

        default_values.update_hue_groups_task = None

    async def update_hue_brightness():
        if default_values.hue_auto_brightness_group is not None:
            update:str = json.dumps({'bri':default_values.current_brightness})
            uri:str = default_values.hue_base_uri+'/groups/'+default_values.hue_auto_brightness_group+'/action'
            async with await default_values.session.put(uri, data=update, headers={aiohttp.hdrs.CONTENT_TYPE:'application/json'}) as res:
                _LOGGER.info("HTTP %s",str(res.status))
                _LOGGER.info("DATA %s",await res.json())
    
    async def update_hue_temperature():
        if default_values.hue_auto_temperature_group is not None:
            update:str = json.dumps({'ct':default_values.current_temperature})
            uri:str = default_values.hue_base_uri+'/groups/'+default_values.hue_auto_temperature_group+'/action'
            async with await default_values.session.put(uri, data=update, headers={aiohttp.hdrs.CONTENT_TYPE:'application/json'}) as res:
                _LOGGER.info("HTTP %s",str(res.status))
                _LOGGER.info("DATA %s",await res.json())

    def update_hue_groups_later():
        if default_values.update_hue_groups_task is not None:
            default_values.update_hue_groups_task.cancel()
        if default_values.hue_auto_brightness_group is not None:
            if default_values.hue_auto_temperature_group is not None:
                default_values.update_hue_groups_task = default_values.hass.async_create_task(default_values.update_hue_groups())

    def setup(hass:HomeAssistant, component:EntityComponent):
        default_values.hass = hass
        default_values.entity_registry = entity_registry.async_get(hass)
        default_values.device_registry = device_registry.async_get(hass)
        default_values.night_mode = default_values.hass.states.is_state("input_boolean.night_mode",STATE_ON)
        default_values.current_brightness = default_values.calculate_current_brightness()
        default_values.current_temperature = default_values.calculate_current_temperature()
        async_track_state_change_event(default_values.hass,"input_number.default_brightness",default_values.async_default_brightness_changed)
        async_track_state_change_event(default_values.hass,"input_number.default_temperature",default_values.async_default_temperature_changed)
        async_track_state_change_event(default_values.hass,"input_boolean.night_mode",default_values.async_night_mode_changed)
        async_track_state_added_domain(default_values.hass,"light",default_values.async_state_added)
        async_track_state_removed_domain(default_values.hass,"light",default_values.async_state_removed)
        default_values.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START,default_values.async_home_assistant_started)

        component.async_register_entity_service("auto_on",vol.All(config_validation.make_entity_service_schema(LIGHT_AUTO_SCHEMA)),default_values.async_handle_light_auto_service)
        component.async_register_entity_service("dim",vol.All(config_validation.make_entity_service_schema(LIGHT_DIM_SCHEMA)),default_values.async_handle_light_dim_service)
   
    def preprocess_data(data):
        """Preprocess the service data."""
        base = {
            entity_field: data.pop(entity_field)
            for entity_field in config_validation.ENTITY_SERVICE_FIELDS
            if entity_field in data
        }
        base["params"] = data
        _LOGGER.info("preprocess_data: base:%s", str(base))
        return base

    async def async_handle_light_auto_service(light:ToggleEntity, call:ServiceCall):
        _LOGGER.info("async_handle_light_auto_service: light:%s call:%s", str(light), str(call))
        params = {}
        # params.update(call.data)
        # await default_values.hass.services.async_call(domain="light",service=SERVICE_TURN_ON,service_data=params)
        if ATTR_COLOR_TEMP in call.data: params[ATTR_COLOR_TEMP] = call.data.get(ATTR_COLOR_TEMP)
        if ATTR_BRIGHTNESS in call.data: params[ATTR_BRIGHTNESS] = call.data.get(ATTR_BRIGHTNESS)
        if ATTR_TRANSITION in call.data: params[ATTR_TRANSITION] = call.data.get(ATTR_TRANSITION)
        # entity_id_list:set[str] = await default_values.get_entities_from_service_call(call)
        # for entity_id in entity_id_list:
        params[ATTR_ENTITY_ID] = light.entity_id
        # _LOGGER.info("  - light_turn_on: entity_id:%s params:%s", str(entity_id), str(params))
        await default_values.hass.services.async_call(domain="light",service=SERVICE_TURN_ON,service_data=params,blocking=True)
    
    async def get_entities_from_area_id(area_id) -> set[str]:
        entityid_list:set = set()
        if isinstance(area_id,list):
            for area_id_element in area_id:
                entityid_list.update(await default_values.get_entities_from_area_id(area_id_element))
        elif isinstance(area_id,str):
            entries:list[RegistryEntry] = entity_registry.async_entries_for_area(default_values.entity_registry,area_id)
            for entry in entries:
                if entry.domain == "light": entityid_list.add(entry.entity_id)
            entries:list[DeviceRegistry] = device_registry.async_entries_for_area(default_values.device_registry,area_id)
            for entry in entries:
                entityid_list.update(await default_values.get_entities_from_device_id(entry.id))

        return entityid_list
    
    async def get_entities_from_device_id(device_id) -> set[str]:
        entityid_list:set = set()
        if isinstance(device_id,list):
            for area_id_element in device_id:
                entityid_list.unite(await default_values.get_entities_from_device_id(area_id_element))
        elif isinstance(device_id,str):
            entries:list[RegistryEntry] = entity_registry.async_entries_for_device(default_values.entity_registry,device_id)
            for entry in entries:
                if entry.domain == "light": entityid_list.add(entry.entity_id)
        return entityid_list

    async def get_entities_from_service_call(call:ServiceCall) -> set[str]:
        entityid_list:set = set()
        if ATTR_AREA_ID in call.data:
            entityid_list.update(await default_values.get_entities_from_area_id(call.data.get(ATTR_AREA_ID,None)))
            _LOGGER.debug("get_entity_list_from_service_call area_id:%s -> %s",str(call.data.get(ATTR_AREA_ID)),str(entityid_list))
        if ATTR_DEVICE_ID in call.data:
            entityid_list.update(await default_values.get_entities_from_device_id(call.data.get(ATTR_DEVICE_ID,None)))
            _LOGGER.debug("get_entity_list_from_service_call device_id:%s -> %s",str(call.data.get(ATTR_DEVICE_ID)),str(entityid_list))
        if ATTR_ENTITY_ID in call.data:
            var = call.data.get(ATTR_ENTITY_ID)
            if isinstance(var,list):
                entityid_list.update(var)
            if isinstance(var,str):
                entityid_list.add(var)
        return entityid_list

    async def async_handle_light_dim_service(light:ToggleEntity, call:ServiceCall):
        _LOGGER.info("async_handle_light_dim_service: light:%s call:%s", str(light), str(call))
        if light is not None and light.is_on:
            params = {}
            # if ATTR_ENTITY_ID in call.data: params[ATTR_ENTITY_ID] = call.data.get(ATTR_ENTITY_ID)
            # if ATTR_AREA_ID in call.data: params[ATTR_AREA_ID] = call.data.get(ATTR_AREA_ID)
            # if ATTR_DEVICE_ID in call.data: params[ATTR_DEVICE_ID] = call.data.get(ATTR_DEVICE_ID)
            params[ATTR_ENTITY_ID] = light.entity_id
            if ATTR_COLOR_TEMP in call.data: params[ATTR_COLOR_TEMP] = call.data.get(ATTR_COLOR_TEMP)
            if ATTR_DIM_TRANSITION in call.data: params[ATTR_TRANSITION] = call.data.get(ATTR_DIM_TRANSITION)
            params[ATTR_BRIGHTNESS] = int(default_values.clamp_int(default_values.current_brightness / 4,3,255))
            await default_values.hass.services.async_call(domain="light",service=SERVICE_TURN_ON,service_data=params,blocking=True)

            off_after = call.data.get(ATTR_OFF_AFTER,None)
            if off_after is not None:
                off_transition = call.data.get(ATTR_OFF_TRANSITION,None)
                # entity_id_list:set[str] = await default_values.get_entities_from_service_call(call)
                # for entity_id in entity_id_list:
                OffTimer.start_timer(default_values.hass,light.entity_id,off_after,off_transition)

    automatic_lights = []
    tracking_brightness = []
    tracking_temperature = []
    current_brightness:int = None
    current_temperature:int = None
    night_mode:bool = False

    def close_enough(v1:int, v2:int) -> bool:
        if v1 is not None:
            if v2 is not None:
                return abs(v1-v2) < 10
        return False

    @callback
    async def async_home_assistant_started(event:Event):
        _LOGGER.info("Automatic lights:")
        for entity_id in default_values.automatic_lights:

            entry:RegistryEntry = default_values.entity_registry.async_get(entity_id)
            unique_id:str = "???"
            if entry is not None:
                default_values.hass_uniqueid[entity_id] = entry.unique_id
                unique_id = entry.unique_id

            _LOGGER.info("  - %s (B:%s T:%s) %s", entity_id,
                str(entity_id in default_values.tracking_brightness),
                str(entity_id in default_values.tracking_temperature),
                unique_id
            )
        default_values.hass.async_create_task(default_values.process_hue())

    @callback
    async def async_state_added(event:Event) -> None:
        entity_id:str = event.data.get(ATTR_ENTITY_ID,None)
        if entity_id is not None:
            state:State = event.data.get("new_state",None)
            if state is not None:
                if ATTR_AUTOMATIC in state.attributes:
                    automatic:bool = bool(state.attributes.get(ATTR_AUTOMATIC,False))
                    if automatic:
                        if entity_id not in default_values.automatic_lights:
                            default_values.automatic_lights.append(entity_id)
                        if state.state is not None and state.state == STATE_ON:
                            brightness:int = state.attributes.get(ATTR_BRIGHTNESS,0)
                            if default_values.close_enough(brightness,default_values.current_brightness):
                                default_values.set_tracking_brightness(entity_id,True)
                            color_temp:int = state.attributes.get(ATTR_COLOR_TEMP,0)
                            if default_values.close_enough(color_temp,default_values.current_temperature):
                                default_values.set_tracking_temperature(entity_id,True)
        _LOGGER.info("Add light %s (Auto:%s On:%s B:%s T:%s)", 
            entity_id, str(state is not None and state.state is not None and state.state == STATE_ON),
            str(entity_id is not None and entity_id in default_values.tracking_brightness),
            str(entity_id is not None and entity_id in default_values.tracking_temperature))

    @callback
    async def async_state_removed(event:Event) -> None:
        entity_id:str = event.data.get(ATTR_ENTITY_ID,None)
        if entity_id is not None and entity_id in default_values.automatic_lights:
            _LOGGER.info("Remove light: %s", entity_id)
            default_values.automatic_lights.remove(entity_id)
        return

    @callback
    async def async_night_mode_changed(_event:Event) -> None:
        new_state:State = _event.data.get("new_state")
        old_state:State = _event.data.get("old_state")
        mode:str = new_state.state
        if old_state is not None:
            _LOGGER.info("Night mode changed from %s to %s", old_state.state, mode)
        else:
            _LOGGER.info("Night mode initialised to %s", mode)
        default_values.night_mode = (mode == STATE_ON)
        if default_values.night_mode:
            OffTimer.cancel_all_timers()

    @callback
    async def async_default_brightness_changed(_event:Event) -> None:
        new_state:State = _event.data.get("new_state")
        old_state:State = _event.data.get("old_state")
        default_values.current_brightness = default_values.clamp_int(int(float(new_state.state)),3,255)
        if old_state is None:
            _LOGGER.info("Default brightness initialised to %s", str(default_values.current_brightness))
        else:
            _LOGGER.info("Default brightness changed from %s to %s", str(int(float(old_state.state))), str(default_values.current_brightness), default_values.tracking_brightness)
            if not default_values.night_mode and len(default_values.tracking_brightness) > 0:
                await default_values.update_hue_brightness()
                # await default_values.hass.services.async_call(
                    # domain="light",
                    # service=SERVICE_TURN_ON,
                    # service_data={ATTR_ENTITY_ID:default_values.tracking_brightness,ATTR_BRIGHTNESS:default_values.current_brightness,ATTR_AUTOMATIC_UPDATE:True}
                # )
    
    @callback
    async def async_default_temperature_changed(_event:Event) -> None:
        new_state:State = _event.data.get("new_state")
        old_state:State = _event.data.get("old_state")
        default_values.current_temperature = default_values.clamp_int(int(float(new_state.state)),153,454)
        if old_state is None:
            _LOGGER.info("Default temperature initialised to %s", str(default_values.current_temperature))
        else:
            _LOGGER.info("Default temperature changed from %s to %s -> %s", str(int(float(old_state.state))), str(default_values.current_temperature), default_values.tracking_temperature)
            if not default_values.night_mode and len(default_values.tracking_temperature) > 0:
                await default_values.update_hue_temperature()
                # await default_values.hass.services.async_call(
                    # domain="light",
                    # service=SERVICE_TURN_ON,
                    # service_data={ATTR_ENTITY_ID:default_values.tracking_temperature,ATTR_COLOR_TEMP:default_values.current_temperature,ATTR_AUTOMATIC_UPDATE:True}
                # )

    def calculate_current_brightness() -> int :
        if default_values.hass is not None:
            br = default_values.hass.states.get("input_number.default_brightness")
            if br is not None:
                return default_values.clamp_int(int(float(br.state)),3,255)
        return 255

    def calculate_current_temperature() -> int :
        if default_values.hass is not None:
            br = default_values.hass.states.get("input_number.default_temperature")
            if br is not None:
                return default_values.clamp_int(int(float(br.state)),153,454)
        return 200
    
    def on_transition_time() -> float | None:
        return None

    def off_transition_time() -> float | None:
        return 1.5
    
    def clamp_int(value, min, max) -> int :
        if value < min:
            return min
        if value > max:
            return max
        return value

    def set_tracking_brightness(entity_id:str, tracking:bool):
        if tracking:
            if entity_id not in default_values.tracking_brightness:
                default_values.tracking_brightness.append(entity_id)
                default_values.update_hue_groups_later()
        elif entity_id in default_values.tracking_brightness:
            default_values.tracking_brightness.remove(entity_id)
            default_values.update_hue_groups_later()

    def set_tracking_temperature(entity_id:str, tracking:bool):
        if tracking:
            if entity_id not in default_values.tracking_temperature:
                default_values.tracking_temperature.append(entity_id)
                default_values.update_hue_groups_later()
        elif entity_id in default_values.tracking_temperature:
            default_values.tracking_temperature.remove(entity_id)
            default_values.update_hue_groups_later()
    
    def is_light_automatic(entity_id:str) -> bool:
        # TODO: cache results
        return entity_id in default_values.automatic_lights

    def apply_default_on_values(light:ToggleEntity, params:dict):

        automatic_light:bool = default_values.is_light_automatic(light.entity_id)
        is_on:bool = light.is_on
        if automatic_light and not ATTR_AUTOMATIC_UPDATE in params:

            if is_on:
                # the light is being updated

                if (ATTR_BRIGHTNESS not in params and ATTR_COLOR_TEMP not in params):
                    # nothing specified (re-turn on the light e.g. motion re-triggered)
                    params[ATTR_BRIGHTNESS] = default_values.current_brightness
                    params[ATTR_COLOR_TEMP] = default_values.current_temperature
                    default_values.set_tracking_brightness(entity_id=light.entity_id, tracking=True)
                    default_values.set_tracking_temperature(entity_id=light.entity_id, tracking=True)
                else:
                    if ATTR_BRIGHTNESS in params:
                        # stop tracking brightness
                        default_values.set_tracking_brightness(entity_id=light.entity_id, tracking=False)

                    if ATTR_COLOR_TEMP in params:
                        # stop tracking temperature
                        default_values.set_tracking_temperature(entity_id=light.entity_id, tracking=False)
            else:
                # the light is being turned on

                brightness:bool = False
                temperature:bool = False

                if ATTR_BRIGHTNESS not in params:
                    brightness = True
                if ATTR_COLOR_TEMP not in params:
                    temperature = True

                default_values.set_tracking_brightness(entity_id=light.entity_id, tracking=brightness)
                default_values.set_tracking_temperature(entity_id=light.entity_id, tracking=temperature)
                if brightness:
                    params[ATTR_BRIGHTNESS] = default_values.current_brightness
                if temperature:
                    params[ATTR_COLOR_TEMP] = default_values.current_temperature
                
                if ATTR_TRANSITION not in params:
                    transition:float = default_values.on_transition_time()
                    if transition is not None:
                        params[ATTR_TRANSITION] = transition
        
        if ATTR_BRIGHTNESS in params:
            # prevent the time from turning off the light
            OffTimer.cancel_timer(entity_id=light.entity_id)
        
        _LOGGER.debug("%s %s (A:%s B:%s T:%s) %s", 
            ("UPDATE" if is_on else "ON"),
            light.entity_id,
            str(automatic_light),
            str(light.entity_id in default_values.tracking_brightness),
            str(light.entity_id in default_values.tracking_temperature),
            str(params))
    
    def apply_default_off_values(light:ToggleEntity, params:dict):
        automatic_light:bool = default_values.is_light_automatic(light.entity_id)
        if automatic_light:
            default_values.set_tracking_brightness(entity_id=light.entity_id, tracking=False)
            default_values.set_tracking_temperature(entity_id=light.entity_id, tracking=False)
            if ATTR_TRANSITION not in params:
                transition:float = default_values.off_transition_time()
                if transition is not None:
                    params[ATTR_TRANSITION] = transition

        _LOGGER.debug("OFF %s (A:%s B:%s T:%s) %s", 
            light.entity_id, str(automatic_light),
            str(light.entity_id in default_values.tracking_brightness),
            str(light.entity_id in default_values.tracking_temperature),
            str(params))
