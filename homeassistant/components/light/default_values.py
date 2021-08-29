"""Adds default on-values and circadian rhythm functionality to lights."""

from __future__ import annotations
import asyncio
from typing import Coroutine
from homeassistant.core import HomeAssistant, ServiceCall, State, Event, callback
import logging
from homeassistant.helpers import entity

from homeassistant.helpers.entity import ToggleEntity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers import config_validation
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_added_domain,
    async_track_state_removed_domain
)

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
        self._task = asyncio.create_task(self._job())

    async def _job(self):
        await asyncio.sleep(self._timeout)
        if self._transition is not None:
            await self._hass.services.async_call(domain="light",service=SERVICE_TURN_OFF,service_data={ATTR_ENTITY_ID:self._entity_id,ATTR_TRANSITION:self._transition})
        else:
            await self._hass.services.async_call(domain="light",service=SERVICE_TURN_OFF,service_data={ATTR_ENTITY_ID:self._entity_id})

    def cancel(self):
        self._task.cancel()

class default_values():

    hass:HomeAssistant = None

    def setup(hass:HomeAssistant, component:EntityComponent):
        default_values.hass = hass
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

    async def async_handle_light_auto_service(light:ToggleEntity, call:ServiceCall):
        service_data = {}
        entity_id:str = call.data.get(ATTR_ENTITY_ID)
        if entity_id is not None:
            service_data[ATTR_ENTITY_ID] = entity_id
            brightness = call.data.get(ATTR_BRIGHTNESS,None)
            if brightness is not None:
                service_data[ATTR_BRIGHTNESS] = brightness
            color_temp = call.data.get(ATTR_COLOR_TEMP,None)
            if color_temp is not None:
                service_data[ATTR_COLOR_TEMP] = color_temp
            transition = call.data.get(ATTR_TRANSITION,None)
            if transition is not None:
                service_data[ATTR_TRANSITION] = transition
            await default_values.hass.services.async_call(domain="light",service=SERVICE_TURN_ON,service_data=service_data)

    async def async_handle_light_dim_service(light:ToggleEntity, call:ServiceCall):
        if light is not None and light.is_on:
            service_data = {}
            entity_id:str = call.data.get(ATTR_ENTITY_ID)
            if entity_id is not None:
                service_data[ATTR_ENTITY_ID] = entity_id
                service_data[ATTR_BRIGHTNESS] = int(default_values.clamp_int(default_values.current_brightness / 4,3,255))
                transition = call.data.get(ATTR_DIM_TRANSITION,None)
                if transition is not None:
                    service_data[ATTR_TRANSITION] = transition
                await default_values.hass.services.async_call(domain="light",service=SERVICE_TURN_ON,service_data=service_data,blocking=True)

                off_after = call.data.get(ATTR_OFF_AFTER,None)
                if off_after is not None:
                    off_transition = call.data.get(ATTR_OFF_TRANSITION,None)
                    if isinstance(entity_id,str):
                        default_values.start_off_timer(entity_id,off_after,off_transition)
                    elif isinstance(entity_id,list):
                        for entity_id_element in entity_id:
                            default_values.start_off_timer(entity_id_element,off_after,off_transition)

    automatic_lights = []
    tracking_brightness = []
    tracking_temperature = []
    current_brightness:int = None
    current_temperature:int = None
    night_mode:bool = False
    off_timers = {}

    def start_off_timer(entity_id:str, timeout:float, transition:float|None = None):
        default_values.cancel_off_timer(entity_id)
        _LOGGER.info("Start timer: %s (%s seconds)", entity_id, timeout)
        default_values.off_timers[entity_id] = OffTimer(hass=default_values.hass,entity_id=entity_id,timeout=timeout,transition=transition)

    def cancel_off_timer(entity_id:str):
        timer:OffTimer = default_values.off_timers.pop(entity_id,None)
        if timer is not None:
            _LOGGER.info("Cancel timer: %s", entity_id)
            timer.cancel()

    def close_enough(v1:int, v2:int) -> bool:
        if v1 is not None:
            if v2 is not None:
                return abs(v1-v2) < 10
        return False

    @callback
    async def async_home_assistant_started(event:Event):
        _LOGGER.info("Automatic lights:")
        for entity_id in default_values.automatic_lights:
            _LOGGER.info("  - %s (B:%s T:%s)", entity_id,
                str(entity_id in default_values.tracking_brightness),
                str(entity_id in default_values.tracking_temperature)
            )

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
                await default_values.hass.services.async_call(
                    domain="light",
                    service=SERVICE_TURN_ON,
                    service_data={ATTR_ENTITY_ID:default_values.tracking_brightness,ATTR_BRIGHTNESS:default_values.current_brightness,ATTR_AUTOMATIC_UPDATE:True}
                )
    
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
                await default_values.hass.services.async_call(
                    domain="light",
                    service=SERVICE_TURN_ON,
                    service_data={ATTR_ENTITY_ID:default_values.tracking_temperature,ATTR_COLOR_TEMP:default_values.current_temperature,ATTR_AUTOMATIC_UPDATE:True}
                )

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

    def set_tracking_brightness(entityid:str, tracking:bool):
        if tracking:
            if entityid not in default_values.tracking_brightness:
                default_values.tracking_brightness.append(entityid)
        elif entityid in default_values.tracking_brightness:
            default_values.tracking_brightness.remove(entityid)

    def set_tracking_temperature(entityid:str, tracking:bool):
        if tracking:
            if entityid not in default_values.tracking_temperature:
                default_values.tracking_temperature.append(entityid)
        elif entityid in default_values.tracking_temperature:
            default_values.tracking_temperature.remove(entityid)
    
    def is_light_automatic(entity_id:str) -> bool:
        # TODO: cache results
        return entity_id in default_values.automatic_lights

    def apply_default_on_values(light:ToggleEntity, params:dict):

        default_values.cancel_off_timer(entity_id=light.entity_id)

        automatic_light:bool = default_values.is_light_automatic(light.entity_id)
        is_on:bool = light.is_on
        if automatic_light and not ATTR_AUTOMATIC_UPDATE in params:

            if is_on:
                # the light is being updated

                if (ATTR_BRIGHTNESS not in params and ATTR_COLOR_TEMP not in params):
                    # nothing specified (re-turn on the light e.g. motion re-triggered)
                    params[ATTR_BRIGHTNESS] = default_values.current_brightness
                    params[ATTR_COLOR_TEMP] = default_values.current_temperature
                    default_values.set_tracking_brightness(entityid=light.entity_id, tracking=True)
                    default_values.set_tracking_temperature(entityid=light.entity_id, tracking=True)
                else:
                    if ATTR_BRIGHTNESS in params:
                        # stop tracking brightness
                        default_values.set_tracking_brightness(entityid=light.entity_id, tracking=False)

                    if ATTR_COLOR_TEMP in params:
                        # stop tracking temperature
                        default_values.set_tracking_temperature(entityid=light.entity_id, tracking=False)
            else:
                # the light is being turned on

                brightness:bool = False
                temperature:bool = False

                if ATTR_BRIGHTNESS not in params:
                    brightness = True
                if ATTR_COLOR_TEMP not in params:
                    temperature = True

                default_values.set_tracking_brightness(entityid=light.entity_id, tracking=brightness)
                default_values.set_tracking_temperature(entityid=light.entity_id, tracking=temperature)
                if brightness:
                    params[ATTR_BRIGHTNESS] = default_values.current_brightness
                if temperature:
                    params[ATTR_COLOR_TEMP] = default_values.current_temperature
                
                if ATTR_TRANSITION not in params:
                    transition:float = default_values.on_transition_time()
                    if transition is not None:
                        params[ATTR_TRANSITION] = transition
        
        _LOGGER.info("%s %s (A:%s B:%s T:%s) %s", 
            ("UPDATE" if is_on else "ON"),
            light.entity_id,
            str(automatic_light),
            str(light.entity_id in default_values.tracking_brightness),
            str(light.entity_id in default_values.tracking_temperature),
            str(params))
    
    def apply_default_off_values(light:ToggleEntity, params:dict):
        automatic_light:bool = default_values.is_light_automatic(light.entity_id)
        if automatic_light:
            default_values.set_tracking_brightness(entityid=light.entity_id, tracking=False)
            default_values.set_tracking_temperature(entityid=light.entity_id, tracking=False)
            if ATTR_TRANSITION not in params:
                transition:float = default_values.off_transition_time()
                if transition is not None:
                    params[ATTR_TRANSITION] = transition

        _LOGGER.info("OFF %s (A:%s B:%s T:%s) %s", 
            light.entity_id, str(automatic_light),
            str(light.entity_id in default_values.tracking_brightness),
            str(light.entity_id in default_values.tracking_temperature),
            str(params))