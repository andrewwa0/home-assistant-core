"""Adds default on-values and circadian rhythm functionality to lights."""

from __future__ import annotations

import asyncio
from curses import meta
import json
import logging
from wsgiref import headers
import aiohttp
import re

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger("default_values")

from typing import Generic, Callable, TypeVar
_T = TypeVar("_T")

class MutableValue(Generic[_T]):
    def __init__(self, v:_T = None):
        self.value:_T = v
        self.dirty:bool = False
        self.major_update:bool = False
    def set(self,v:_T) -> bool:
        if self.value != v:
            self.major_update = isinstance(v,int) and (self.value is None or (abs(self.value-v)) > 10)
            self.value = v
            self.dirty = True
        return self.dirty
    def reset(self, v:_T = None):
        self.value = v
        self.dirty = True
        self.major_update = False
    def clean(self):
        self.dirty = False
        self.major_update = False
    def __repr__(self) -> str:
        return self.value.__repr__() + ('*' if self.dirty else '')

class StringSet(set[str]):
    pass

class LightList(MutableValue[StringSet]):
    pass

class HueGroup():
    def __init__(self):
        self.name:str = None
        self.number:str = None
        self.lights:LightList = LightList()
        self.brightness:MutableValue[int] = MutableValue()
        self.color_temp:MutableValue[int] = MutableValue()
    def __repr__(self) -> str:
        return 'Group ' + self.number + ' "' + self.name + '" ' + str(self.lights)

class HueLight():
    def __init__(self):
        self.name:str = None
        self.number:str = None
        self.unique_id:str = None
    def __repr__(self) -> str:
        return 'Light ' + self.number + ' "' + self.name + '" ' + self.unique_id

class HueAPI():

    def __init__(self, hass:HomeAssistant):
        self.hass = hass
        self.apikey = 'fzrRwobrK-cDGj3wJKiOZJ2fdiDCPrXGWKzXzGjl'
        self.session = aiohttp.ClientSession()
        self.hue_base_uri:str = 'http://192.168.1.2/api/' + self.apikey
        self.hue_base_uri2:str = 'https://192.168.1.2/clip/v2'
        self.hue_headers = { 'hue-application-key': self.apikey }

        self.hue_groups:list[HueGroup] = {}
        self.hue_lights:list[HueLight] = []

        self.auto_brightness_group:HueGroup = None
        self.auto_temperature_group:HueGroup = None
    
        self.reload_lights_requested = False
        self.reload_groups_requested = False
        self.all_off_requested = False

        self.looping = False
        self.ready_callback = None
    
    def set_ready_callback(self, callback:Callable):
        self.ready_callback = callback

    def set_automatic_brightness_lights(self, lights:set[str]):
        if self.auto_brightness_group:
            self.auto_brightness_group.lights.set(lights)
            self.run_loop()
    def set_automatic_temperature_lights(self, lights:set[str]):
        if self.auto_temperature_group:
            self.auto_temperature_group.lights.set(lights)
            self.run_loop()

    def add_automatic_brightness_light(self, light:str):
        if light and self.auto_brightness_group:
            if light not in self.auto_brightness_group.lights.value:
                self.auto_brightness_group.lights.value.add(light)
                self.auto_brightness_group.lights.dirty = True
                self.run_loop()
    def add_automatic_brightness_lights(self, lights:set[str]):
        if lights:
            for light in lights: self.add_automatic_brightness_light(light)

    def add_automatic_temperature_light(self, light:str):
        if light and self.auto_temperature_group:
            if light not in self.auto_temperature_group.lights.value:
                self.auto_temperature_group.lights.value.add(light)
                self.auto_temperature_group.lights.dirty = True
                self.run_loop()
    def add_automatic_temperature_lights(self, lights:set[str]):
        if lights:
            for light in lights: self.add_automatic_temperature_light(light)

    def remove_automatic_brightness_light(self, light:str):
        if light and self.auto_brightness_group:
            if light in self.auto_brightness_group.lights.value:
                self.auto_brightness_group.lights.value.remove(light)
                self.auto_brightness_group.lights.dirty = True
                self.run_loop()
    def remove_automatic_brightness_lights(self, lights:set[str]):
        if lights:
            for light in lights: self.remove_automatic_brightness_light(light)

    def remove_automatic_temperature_light(self, light:str):
        if light and self.auto_temperature_group:
            if light in self.auto_temperature_group.lights.value:
                self.auto_temperature_group.lights.value.remove(light)
                self.auto_temperature_group.lights.dirty = True
                self.run_loop()
    def remove_automatic_temperature_lights(self, lights:set[str]):
        if lights:
            for light in lights: self.remove_automatic_temperature_light(light)

    def set_automatic_brightness(self, brightness:int):
        if self.auto_brightness_group:
            # maximum hue value os 254
            if brightness > 254: brightness = 254
            if self.auto_brightness_group.brightness.set(brightness):
                self.run_loop()
    def set_automatic_temperature(self, color_temp:int):
        if self.auto_temperature_group:
            if self.auto_temperature_group.color_temp.set(color_temp):
                self.run_loop()

    def request_all_off(self, transition:float = None):
        self.all_off_requested = True
        self.run_loop()
    def request_reload(self):
        self.reload_lights_requested = True
        self.reload_groups_requested = True
        self.run_loop()
    def run_loop(self):
        if not self.looping:
            self.looping = True
            self.hass.async_create_task(self.async_loop())
    
    @property
    def ready(self) -> bool:
        return self.auto_brightness_group is not None and self.auto_temperature_group is not None
    
    def light_from_unique_id(self, unique_id:str) -> str|None:
        for light in self.hue_lights:
            if light.unique_id == unique_id:
                return light.number
        return None
    def lights_from_unique_id(self, unique_id:str) -> set[str]|None:
        for light in self.hue_lights:
            if light.unique_id == unique_id:
                return {light.number}
        _LOGGER.warning("Light with unique id '%s' not found", unique_id)
        return None
    def lights_from_name(self, name:str) -> set[str]|None:
        for light in self.hue_lights:
            if light.name == name:
                return {light.number}
        _LOGGER.warning("Light with name '%s' not found", name)
        return None
    def lights_from_group_name(self, name:str) -> set[str]|None:
        for group in self.hue_groups:
            if group.name == name:
                return group.lights.value
        return None

    async def async_loop(self) -> int:
        # _LOGGER.info("ENTER LOOP")
        tasks_completed:int = 0
        await asyncio.sleep(2.0)
        while await self.async_update():
            await asyncio.sleep(1.0)
            tasks_completed += 1
            if tasks_completed >= 10:
                break
        self.looping = False
        # _LOGGER.info("EXIT LOOP - %s TASKS COMPLETED", str(tasks_completed))
        return tasks_completed

    async def async_update(self) -> bool:
        if await self.async_process_commands():
            return True
        if not self.hue_lights or self.reload_lights_requested:
            await self.async_load_lights()
            return True
        if not self.hue_groups or self.reload_groups_requested:
            await self.async_load_groups()
            return True
        if await self.async_update_hue_groups():
            return True
        if await self.async_update_hue_values():
            return True
        return False # nothing to do

    async def async_load_lights(self):

        if 0:
            path:str = '/lights'
            async with self.session.get(self.uri(path)) as resp:
                json = await resp.json()
                if int(resp.status) == 200:
                    _LOGGER.info("GET %s %s",str(resp.status),path)
                else:
                    _LOGGER.warning("GET %s %s -> %s",str(resp.status),path,json)
                if int(resp.status) == 200:
                    self.hue_lights = []
                    for light_id in json:
                        light = json[light_id]
                        l = HueLight()
                        l.name = light["name"]
                        l.number = light_id
                        l.unique_id = light["uniqueid"]
                        self.hue_lights.append(l)
                        _LOGGER.info("Discovered Hue Light: %s", l)
                    _LOGGER.info("Loaded %s lights from Hue", len(self.hue_lights))
                    self.reload_lights_requested = False

        path:str = '/resource/light'
        async with self.session.get(self.uri2(path), headers=self.hue_headers, ssl=False) as resp:
            json = await resp.json()
            if int(resp.status) == 200:
                _LOGGER.info("GET %s %s",str(resp.status),path)
            else:
                _LOGGER.warning("GET %s %s -> %s",str(resp.status),path,json)
            if int(resp.status) == 200:
                light_data:list = json["data"]
                self.hue_lights = []
                for light in light_data:
                    unique_id:str = light["id"]
                    id_v1:str = light["id_v1"]
                    metadata:object = light["metadata"]
                    if metadata is not None:
                        m = re.search('/lights/(\\d+)',id_v1)
                        if m:
                            l = HueLight()
                            l.name = metadata["name"]
                            l.unique_id = unique_id
                            l.number = str(m.group(1))
                            self.hue_lights.append(l)
                            _LOGGER.info("Discovered Hue Light (v2): %s", l)
                _LOGGER.info("Loaded %s lights from Hue", len(self.hue_lights))
                self.reload_lights_requested = False

    async def async_load_groups(self):

        path:str = '/groups'
        async with self.session.get(self.uri(path)) as resp:

            json = await resp.json()

            if int(resp.status) == 200:
                _LOGGER.info("GET %s %s",str(resp.status),path)
            else:
                _LOGGER.warning("GET %s %s -> %s",str(resp.status),path,json)

            if int(resp.status) == 200:

                self.hue_groups = []
                self.auto_brightness_group = None
                self.auto_temperature_group = None

                for group_id in json:
                    group = json[group_id]   

                    g = HueGroup()
                    g.name = group["name"]
                    g.number = group_id
                    g.lights.set(set(group["lights"]))
                    self.hue_groups.append(g)

                    if g.name == 'Auto Temperature':
                        self.auto_temperature_group = g
                    elif g.name == 'Auto Brightness':
                        self.auto_brightness_group = g
                    _LOGGER.info("Discovered Hue Group: %s", g)

                _LOGGER.info("Loaded %s groups from Hue", len(self.hue_groups))
                self.reload_groups_requested = False

                if self.auto_brightness_group is not None:
                    _LOGGER.info("Hue Automatic Brightness group is %s", self.auto_brightness_group.number)
                if self.auto_temperature_group is not None:
                    _LOGGER.info("Hue Automatic Temperature group is %s", self.auto_temperature_group.number)

        # update the auto group lights from scratch
        self.auto_brightness_group.lights.reset(set())
        self.auto_temperature_group.lights.reset(set())
        self.auto_brightness_group.brightness.reset(0)
        self.auto_temperature_group.color_temp.reset(0)

        if self.ready_callback is not None:
            self.ready_callback()
    
    async def async_update_hue_groups(self) -> bool:

        if self.ready:
            if self.auto_brightness_group.lights.dirty and len(self.auto_brightness_group.lights.value) > 0:
                _LOGGER.info("UPDATING HUE BRIGHTNESS GROUP %s", self.auto_brightness_group)
                update:str = json.dumps({'lights':list(self.auto_brightness_group.lights.value)})
                path:str = '/groups/'+self.auto_brightness_group.number
                uri:str = self.uri(path)
                async with await self.session.put(uri, data=update, headers={aiohttp.hdrs.CONTENT_TYPE:'application/json'}) as res:
                    results = await res.json()
                    if int(res.status) == 200:
                        for result in results:
                            if 'success' in result:
                                success = result['success']
                                if path+'/lights' in success:
                                    lights = set(success[path+'/lights'])
                                    if lights == self.auto_brightness_group.lights.value:
                                        self.auto_brightness_group.lights.clean()
                                _LOGGER.info("PUT %s %s %s %s -> %s",str(res.status),('ACK' if not self.auto_brightness_group.lights.dirty else 'NAK'),path,update,result)
                            else:
                                _LOGGER.warning("PUT %s %s %s -> %s",str(res.status),path,update,result)
                    else:
                        _LOGGER.warning("PUT %s %s %s -> %s",str(res.status),path,update,results)
                return True
            if self.auto_temperature_group.lights.dirty and len(self.auto_temperature_group.lights.value) > 0:
                _LOGGER.info("UPDATING HUE TEMPERATURE GROUP %s", self.auto_temperature_group)
                update:str = json.dumps({'lights':list(self.auto_temperature_group.lights.value)})
                path:str = '/groups/'+self.auto_temperature_group.number
                uri:str = self.uri(path)
                async with await self.session.put(uri, data=update, headers={aiohttp.hdrs.CONTENT_TYPE:'application/json'}) as res:
                    results = await res.json()
                    if int(res.status) == 200:
                        for result in results:
                            if 'success' in result:
                                success = result['success']
                                if path+'/lights' in success:
                                    lights = set(success[path+'/lights'])
                                    if lights == self.auto_temperature_group.lights.value:
                                        self.auto_temperature_group.lights.clean()
                                _LOGGER.info("PUT %s %s %s %s -> %s",str(res.status),('ACK' if not self.auto_temperature_group.lights.dirty else 'NAK'),path,update,result)
                            else:
                                _LOGGER.warning("PUT %s %s %s -> %s",str(res.status),path,update,result)
                    else:
                        _LOGGER.warning("PUT %s %s %s -> %s",str(res.status),path,update,results)
                return True
        return False
    
    def uri(self, path:str) -> str:
        return self.hue_base_uri + path

    def uri2(self, path:str) -> str:
        return self.hue_base_uri2 + path
    
    def process_action_result(self, path, status, update, results):
        if int(status) == 200:
            _LOGGER.info("PUT %s %s %s -> %s",str(status),path,update,results)
            for result in results:
                if 'success' in result:
                    success = result['success']
                    if path+'/bri' in success:
                        if self.auto_brightness_group.brightness.value == success[path+'/bri']:
                            self.auto_brightness_group.brightness.clean()
                    if path+'/ct' in success:
                        if self.auto_temperature_group.color_temp.value == success[path+'/ct']:
                            self.auto_temperature_group.color_temp.clean()
        else:
            _LOGGER.warning("PUT %s %s %s -> %s",str(status),path,update,results)
    
    async def async_update_hue_values(self) -> bool:

        if self.ready:
            if self.auto_brightness_group.brightness.dirty or self.auto_temperature_group.color_temp.dirty:

                if (
                    self.auto_brightness_group.brightness.dirty 
                    and self.auto_temperature_group.color_temp.dirty
                    and self.auto_brightness_group.lights.value
                    and self.auto_temperature_group.lights.value
                    and self.auto_brightness_group.lights.value == self.auto_temperature_group.lights.value
                ):
                    # if the same lights are in both groups, issue a single command to one of the groups
                    if self.auto_brightness_group.brightness.major_update or self.auto_temperature_group.color_temp.major_update:
                        update:str = json.dumps({'bri':self.auto_brightness_group.brightness.value,'ct':self.auto_temperature_group.color_temp.value,'transitiontime':100})
                    else:
                        update:str = json.dumps({'bri':self.auto_brightness_group.brightness.value,'ct':self.auto_temperature_group.color_temp.value})
                    path:str = '/groups/'+self.auto_brightness_group.number+'/action'
                    uri:str = self.uri(path)
                    async with await self.session.put(uri, data=update, headers={aiohttp.hdrs.CONTENT_TYPE:'application/json'}) as res:
                        results = await res.json()
                        self.process_action_result(path,res.status,update,results)
                    return True
                elif self.auto_brightness_group.brightness.dirty and self.auto_brightness_group.lights.value:
                    if self.auto_brightness_group.brightness.major_update:
                        update:str = json.dumps({'bri':self.auto_brightness_group.brightness.value,'transitiontime':100})
                    else:
                        update:str = json.dumps({'bri':self.auto_brightness_group.brightness.value})
                    path:str = '/groups/'+self.auto_brightness_group.number+'/action'
                    uri:str = self.uri(path)
                    async with await self.session.put(uri, data=update, headers={aiohttp.hdrs.CONTENT_TYPE:'application/json'}) as res:
                        results = await res.json()
                        self.process_action_result(path,res.status,update,results)
                    return True
                elif self.auto_temperature_group.color_temp.dirty and self.auto_temperature_group.lights.value:
                    if self.auto_temperature_group.color_temp.major_update:
                        update:str = json.dumps({'ct':self.auto_temperature_group.color_temp.value,'transitiontime':100})
                    else:
                        update:str = json.dumps({'ct':self.auto_temperature_group.color_temp.value})
                    path:str = '/groups/'+self.auto_temperature_group.number+'/action'
                    uri:str = self.uri(path)
                    async with await self.session.put(uri, data=update, headers={aiohttp.hdrs.CONTENT_TYPE:'application/json'}) as res:
                        results = await res.json()
                        self.process_action_result(path,res.status,update,results)
                    return True
        return False
    
    async def async_process_commands(self) -> bool:
        if self.all_off_requested:
            update:str = json.dumps({'on':False,'transitiontime':25})
            path:str = '/groups/0/action'
            uri:str = self.uri(path)
            async with await self.session.put(uri, data=update, headers={aiohttp.hdrs.CONTENT_TYPE:'application/json'}) as res:
                _LOGGER.info("PUT %s %s -> %s",str(res.status),update,await res.json())
                self.auto_brightness_group.lights.reset(set())
                self.auto_brightness_group.lights.clean()
                self.auto_temperature_group.lights.reset(set())
                self.auto_temperature_group.lights.clean()
                # self.auto_brightness_group.brightness.reset(0)
                # self.auto_brightness_group.brightness.clean()
                # self.auto_temperature_group.color_temp.reset(0)
                # self.auto_temperature_group.color_temp.clean()
                self.all_off_requested = False

            return True

        return False


