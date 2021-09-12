"""Adds default on-values and circadian rhythm functionality to lights."""

from __future__ import annotations

import asyncio
from asyncio.tasks import  sleep
import json
import logging
import aiohttp

from .hueapi import HueAPI

from homeassistant.core import HomeAssistant, ServiceCall, State, Event, callback
from homeassistant.helpers.entity import ToggleEntity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers import config_validation, entity_registry, device_registry
from homeassistant.helpers.entity_registry import EntityRegistry, RegistryEntry
from homeassistant.helpers.device_registry import DeviceRegistry, DeviceEntry
from homeassistant.helpers.event import async_track_state_change_event, async_track_state_added_domain, async_track_state_removed_domain

from homeassistant.const import (
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
ATTR_OFF_TRANSITION = "off_transition"
ATTR_DIM_AFTER = "dim_after"
ATTR_DIM_TRANSITION = "dim_transition"


_LOGGER = logging.getLogger("default_values")

import voluptuous as vol

VALID_TRANSITION = vol.All(vol.Coerce(float), vol.Clamp(min=0, max=6553))

VALID_BRIGHTNESS = vol.All(vol.Coerce(int), vol.Clamp(min=0, max=255))
VALID_COLOR_TEMP = vol.All(vol.Coerce(int), vol.Clamp(min=153, max=454))

LIGHT_DIM_SCHEMA = {ATTR_OFF_AFTER:VALID_TRANSITION, ATTR_DIM_TRANSITION:VALID_TRANSITION, ATTR_OFF_TRANSITION:VALID_TRANSITION}
LIGHT_AUTO_SCHEMA = {ATTR_BRIGHTNESS:VALID_BRIGHTNESS, ATTR_COLOR_TEMP:VALID_COLOR_TEMP, ATTR_TRANSITION:VALID_TRANSITION}
LIGHT_MOTION_SCHEMA = {ATTR_TRANSITION:VALID_TRANSITION, ATTR_DIM_AFTER:VALID_TRANSITION, ATTR_DIM_TRANSITION:VALID_TRANSITION, ATTR_OFF_AFTER:VALID_TRANSITION, ATTR_OFF_TRANSITION:VALID_TRANSITION}
LIGHTS_OFF_SCHEMA = {ATTR_TRANSITION:VALID_TRANSITION}

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
    hue:HueAPI = None
    current_brightness:int = None
    current_temperature:int = None
    night_mode:bool = False

    def update_from_state(state:State) -> None:
        if state is not None:
            entry:RegistryEntry = default_values.entity_registry.async_get(state.entity_id)
            if entry is not None:
                if default_values.is_automatic_entity(state.entity_id):
                    if state.state is not None and state.state == STATE_ON:
                        if bool(state.attributes.get('is_hue_group',False)) == False:
                            light:str = default_values.hue.lights_from_unique_id(entry.unique_id)
                            brightness:int = state.attributes.get(ATTR_BRIGHTNESS,0)
                            color_temp:int = state.attributes.get(ATTR_COLOR_TEMP,0)
                            if default_values.close_enough(brightness,default_values.current_brightness):
                                default_values.hue.add_automatic_brightness_light(light)
                            if default_values.close_enough(color_temp,default_values.current_temperature):
                                default_values.hue.add_automatic_temperature_light(light)
                            # _LOGGER.info('Added light %s %s -> %s', state.entity_id, state.attributes, lights)

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
        component.async_register_entity_service("motion_on",vol.All(config_validation.make_entity_service_schema(LIGHT_MOTION_SCHEMA)),default_values.async_handle_light_motion_service)
        component.async_register_entity_service("dim",vol.All(config_validation.make_entity_service_schema(LIGHT_DIM_SCHEMA)),default_values.async_handle_light_dim_service)
        default_values.hass.services.async_register("light","hue_reload",default_values.async_handle_hue_reload_service)
        default_values.hass.services.async_register("light","all_lights_off",default_values.async_handle_all_lights_off_service)

        default_values.hue = HueAPI(hass)
        default_values.hue.set_ready_callback(default_values.hue_ready)
        default_values.hue.run_loop()
    
    def hue_ready():

        auto_brightness_lights:set[str] = set()
        auto_temperature_lights:set[str] = set()

        states:list[State] = default_values.hass.states.async_all("light")
        for state in states:
            if state is not None:
                entry:RegistryEntry = default_values.entity_registry.async_get(state.entity_id)
                if entry is not None:
                    if default_values.is_automatic_entity(state.entity_id):
                        if state.state is not None and state.state == STATE_ON:
                            if bool(state.attributes.get('is_hue_group',False)) == False:
                                light:str = default_values.hue.light_from_unique_id(entry.unique_id)
                                if light:
                                    brightness:int = state.attributes.get(ATTR_BRIGHTNESS,0)
                                    color_temp:int = state.attributes.get(ATTR_COLOR_TEMP,0)
                                    if default_values.close_enough(brightness,default_values.current_brightness):
                                        auto_brightness_lights.add(light)
                                    if default_values.close_enough(color_temp,default_values.current_temperature):
                                        auto_temperature_lights.add(light)


        default_values.hue.set_automatic_brightness_lights(auto_brightness_lights)
        default_values.hue.set_automatic_temperature_lights(auto_temperature_lights)
        default_values.hue.set_automatic_brightness(default_values.current_brightness)
        default_values.hue.set_automatic_temperature(default_values.current_temperature)
   
    def preprocess_data(data):
        """Preprocess the service data."""
        base = {
            entity_field: data.pop(entity_field)
            for entity_field in config_validation.ENTITY_SERVICE_FIELDS
            if entity_field in data
        }
        base["params"] = data
        # _LOGGER.info("preprocess_data: base:%s", str(base))
        return base

    async def async_handle_hue_reload_service(call:ServiceCall):
        default_values.hue.request_reload()
    
    async def async_handle_all_lights_off_service(call:ServiceCall):
        default_values.hue.request_all_off(call.data.get(ATTR_TRANSITION,None))

    async def async_handle_light_motion_service(light:ToggleEntity, call:ServiceCall):
        lights:set[str] = default_values.lights_from_entity(light)

        obj = {}
        obj['bri'] = int(call.data.get(ATTR_BRIGHTNESS,default_values.current_brightness))
        obj['ct'] = int(call.data.get(ATTR_COLOR_TEMP,default_values.current_temperature))
        if ATTR_TRANSITION in call.data:
            obj['transitiontime'] = int(call.data.get(ATTR_TRANSITION) / 10)

        for group in default_values.hue_groups.values():
            if group.lights == lights:
                update:str = json.dumps(obj)
                uri:str = default_values.hue_base_uri+'/group/'+group.number+'/action'
                _LOGGER.info("PUT %s %s",uri,update)
                async with await default_values.session.put(uri, data=update, headers={aiohttp.hdrs.CONTENT_TYPE:'application/json'}) as res:
                    default_values.auto_brightness_group.dirty = False
                    _LOGGER.info("RESULT %s %s",str(res.status),await res.json())
                return
        for light in lights:
            update:str = json.dumps(obj)
            uri:str = default_values.hue_base_uri+'/lights/'+light+'/state'
            _LOGGER.info("PUT %s %s",uri,update)
            async with await default_values.session.put(uri, data=update, headers={aiohttp.hdrs.CONTENT_TYPE:'application/json'}) as res:
                default_values.auto_brightness_group.dirty = False
                _LOGGER.info("RESULT %s %s",str(res.status),await res.json())
            await sleep(0.2)
        return

    async def async_handle_light_auto_service(light:ToggleEntity, call:ServiceCall):
        _LOGGER.info("async_handle_light_auto_service: light:%s call:%s", str(light), str(call))
        params = {}
        if ATTR_COLOR_TEMP in call.data: params[ATTR_COLOR_TEMP] = call.data.get(ATTR_COLOR_TEMP)
        if ATTR_BRIGHTNESS in call.data: params[ATTR_BRIGHTNESS] = call.data.get(ATTR_BRIGHTNESS)
        if ATTR_TRANSITION in call.data: params[ATTR_TRANSITION] = call.data.get(ATTR_TRANSITION)
        params[ATTR_ENTITY_ID] = light.entity_id
        # _LOGGER.info("  - light_turn_on: entity_id:%s params:%s", str(entity_id), str(params))
        await default_values.hass.services.async_call(domain="light",service=SERVICE_TURN_ON,service_data=params,blocking=True)

    async def async_handle_light_dim_service(light:ToggleEntity, call:ServiceCall):
        _LOGGER.info("async_handle_light_dim_service: light:%s call:%s", str(light), str(call))
        if light is not None and light.is_on:
            params = {}
            params[ATTR_ENTITY_ID] = light.entity_id
            if ATTR_COLOR_TEMP in call.data:
                params[ATTR_COLOR_TEMP] = call.data.get(ATTR_COLOR_TEMP)
            else:
                params[ATTR_COLOR_TEMP] = default_values.current_temperature
            if ATTR_DIM_TRANSITION in call.data:
                params[ATTR_TRANSITION] = call.data.get(ATTR_DIM_TRANSITION)
            params[ATTR_BRIGHTNESS] = int(default_values.clamp_int(default_values.current_brightness / 4,3,255))
            await default_values.hass.services.async_call(domain="light",service=SERVICE_TURN_ON,service_data=params,blocking=True)

            off_after = call.data.get(ATTR_OFF_AFTER,None)
            if off_after is not None:
                off_transition = call.data.get(ATTR_OFF_TRANSITION,None)
                OffTimer.start_timer(default_values.hass,light.entity_id,off_after,off_transition)

    def close_enough(v1:int, v2:int) -> bool:
        if v1 is not None:
            if v2 is not None:
                return abs(v1-v2) < 10
        return False

    @callback
    async def async_home_assistant_started(event:Event):
        pass
        # default_values.hass.async_create_task(default_values.process_hue(5.0))

    @callback
    async def async_state_added(event:Event) -> None:
        default_values.update_from_state(event.data.get("new_state",None))

    @callback
    async def async_state_removed(event:Event) -> None:
        return
        # entity_id:str = event.data.get(ATTR_ENTITY_ID,None)
        # _LOGGER.info("Remove light: %s", entity_id)

    @callback
    async def async_night_mode_changed(_event:Event) -> None:
        new_state:State = _event.data.get("new_state")
        # old_state:State = _event.data.get("old_state")
        mode:str = new_state.state
        # if old_state is not None:
            # _LOGGER.info("Night mode changed from %s to %s", old_state.state, mode)
        # else:
            # _LOGGER.info("Night mode initialised to %s", mode)
        default_values.night_mode = (mode == STATE_ON)
        if default_values.night_mode:
            OffTimer.cancel_all_timers()

    @callback
    async def async_default_brightness_changed(_event:Event) -> None:
        new_state:State = _event.data.get("new_state")
        default_values.current_brightness = default_values.clamp_int(int(float(new_state.state)),3,255)
        if not default_values.night_mode:
            default_values.hue.set_automatic_brightness(default_values.current_brightness)
    
    @callback
    async def async_default_temperature_changed(_event:Event) -> None:
        new_state:State = _event.data.get("new_state")
        default_values.current_temperature = default_values.clamp_int(int(float(new_state.state)),153,454)
        if not default_values.night_mode:
            default_values.hue.set_automatic_temperature(default_values.current_temperature)

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

    def is_automatic_entity(entity_id:str) -> bool:
        if 'light.veranda' in entity_id:
            return False
        if 'light.tv_' in entity_id:
            return False
        if 'light.bed_' in entity_id:
            return False
        return True

    def is_automatic_light(entity:ToggleEntity) -> bool:
        return default_values.is_automatic_entity(entity.entity_id)

    def lights_from_entity(entity:ToggleEntity) -> set[str]|None:
        if entity.extra_state_attributes and entity.extra_state_attributes.get('is_hue_group',False):
            return default_values.hue.lights_from_group_name(entity.name)
        else:
            return default_values.hue.lights_from_unique_id(entity.unique_id)

    def apply_default_on_values(light:ToggleEntity, params:dict):
        is_on:bool = light.is_on

        
        # 

        if default_values.is_automatic_light(light): # and not ATTR_AUTOMATIC_UPDATE in params:

            lights:set[str] = default_values.lights_from_entity(light)

            _LOGGER.info("ON COMMAND FOR %s %s -> %s", light.entity_id, light.unique_id, str(lights))

            if is_on:
                # the light is being updated

                if (ATTR_BRIGHTNESS not in params and ATTR_COLOR_TEMP not in params):
                    # nothing specified (re-turn on the light e.g. motion re-triggered)
                    params[ATTR_BRIGHTNESS] = default_values.current_brightness
                    params[ATTR_COLOR_TEMP] = default_values.current_temperature
                    default_values.hue.add_automatic_brightness_lights(lights)
                    default_values.hue.add_automatic_temperature_lights(lights)
                else:
                    if ATTR_BRIGHTNESS in params:
                        # stop tracking brightness
                        default_values.hue.remove_automatic_brightness_lights(lights)

                    if ATTR_COLOR_TEMP in params:
                        # stop tracking temperature
                        default_values.hue.remove_automatic_temperature_lights(lights)
            else:
                # the light is being turned on

                if ATTR_BRIGHTNESS not in params:
                    params[ATTR_BRIGHTNESS] = default_values.current_brightness
                    default_values.hue.add_automatic_brightness_lights(lights)
                else:
                    default_values.hue.remove_automatic_brightness_lights(lights)

                if ATTR_COLOR_TEMP not in params:
                    params[ATTR_COLOR_TEMP] = default_values.current_temperature
                    default_values.hue.add_automatic_temperature_lights(lights)
                else:
                    default_values.hue.remove_automatic_temperature_lights(lights)

                if ATTR_TRANSITION not in params:
                    transition:float = default_values.on_transition_time()
                    if transition is not None:
                        params[ATTR_TRANSITION] = transition
        
        if ATTR_BRIGHTNESS in params:
            # prevent the time from turning off the light
            OffTimer.cancel_timer(entity_id=light.entity_id)
        
        # _LOGGER.debug("%s %s %s",("UPDATE" if is_on else "ON"),light.entity_id,str(params))
    
    def apply_default_off_values(light:ToggleEntity, params:dict):
        lights:set[str] = default_values.lights_from_entity(light)
        default_values.hue.remove_automatic_brightness_lights(lights)
        default_values.hue.remove_automatic_temperature_lights(lights)
        if default_values.is_automatic_light(light):
            if ATTR_TRANSITION not in params:
                transition:float = default_values.off_transition_time()
                if transition is not None:
                    params[ATTR_TRANSITION] = transition
        # _LOGGER.debug("OFF %s %s", light.entity_id, str(params))

