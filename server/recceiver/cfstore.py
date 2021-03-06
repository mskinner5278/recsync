# -*- coding: utf-8 -*-

import logging
_log = logging.getLogger(__name__)
from requests import RequestException
from zope.interface import implements
from twisted.application import service
from twisted.internet.threads import deferToThread
from twisted.internet.defer import DeferredLock
from twisted.internet import defer
from operator import itemgetter
from collections import defaultdict
import time
import interfaces
import datetime
import os
import json

# ITRANSACTION FORMAT:
#
# src = source address
# addrec = records ein added ( recname, rectype, {key:val})
# delrec = a set() of records which are being removed
# infos = dictionary of client infos
# recinfos = additional infos being added to existing records 
# "recid: {key:value}"
#


__all__ = ['CFProcessor']

class CFProcessor(service.Service):
    implements(interfaces.IProcessor)

    def __init__(self, name, conf):
        _log.info("CF_INIT %s", name)
        self.name, self.conf = name, conf
        self.channel_dict = defaultdict(list)
        self.iocs = dict()
        self.client = None
        self.currentTime = getCurrentTime
        self.lock = DeferredLock()

    def startService(self):
        service.Service.startService(self)
        self.running = 1
        _log.info("CF_START")
        from channelfinder import ChannelFinderClient
        # Using the default python cf-client.
        # The usr, username, and password are provided by the channelfinder._conf module.
        if self.client is None:  # For setting up mock test client
            self.client = ChannelFinderClient()
        self.clean_service()

    def stopService(self):
        service.Service.stopService(self)
        #Set channels to inactive and close connection to client
        self.running = 0
        self.clean_service()
        _log.info("CF_STOP")

    @defer.inlineCallbacks
    def commit(self, transaction_record):
        yield self.lock.acquire()
        try:
            yield deferToThread(self.__commit__, transaction_record)
        finally:
            self.lock.release()

    def __commit__(self, TR):
        _log.debug("CF_COMMIT %s", TR.infos.items())
        pvNames = [unicode(rname, "utf-8") for rid, (rname, rtype) in TR.addrec.iteritems()]
        delrec = list(TR.delrec)
        iocName = TR.src.port
        hostName = TR.src.host
        iocid = hostName + ":" + str(iocName)
        owner = TR.infos.get('CF_USERNAME') or TR.infos.get('ENGINEER') or self.conf.get('username', 'cfstore')
        time = self.currentTime()
        if TR.initial:
            self.iocs[iocid] = {"iocname": iocName, "hostname": hostName, "owner": owner, "channelcount": 0}  # add IOC to source list
        if not TR.connected:
            delrec.extend(self.channel_dict.keys())
        for pv in pvNames:
            self.channel_dict[pv].append(iocid)  # add iocname to pvName in dict
            self.iocs[iocid]["channelcount"] += 1
        for pv in delrec:
            if iocid in self.channel_dict[pv]:
                self.channel_dict[pv].remove(iocid)
                self.iocs[iocid]["channelcount"] -= 1
                if self.iocs[iocid]['channelcount'] == 0:
                    self.iocs.pop(iocid, None)
                elif self.iocs[iocid]['channelcount'] < 0:
                    _log.error("channel count negative!")
                if len(self.channel_dict[pv]) <= 0:  # case: channel has no more iocs
                    del self.channel_dict[pv]
        poll(__updateCF__, self.client, pvNames, delrec, self.channel_dict, self.iocs, hostName, iocName, time, owner)
        dict_to_file(self.channel_dict, self.iocs, self.conf)

    def clean_service(self):
        sleep = 1
        retry_limit = 5
        owner = self.conf.get('username', 'cfstore')
        while 1:
            try:
                _log.debug("Cleaning service...")
                channels = self.client.findByArgs([('pvStatus', 'Active')])
                if channels is not None:
                    new_channels = []
                    for ch in channels or []:
                        new_channels.append(ch[u'name'])
                    if len(new_channels) > 0:
                        self.client.update(property={u'name': 'pvStatus', u'owner': owner, u'value': "Inactive"},
                                           channelNames=new_channels)
                    _log.debug("Service clean.")
                    return
            except RequestException:
                _log.exception("cleaning failed, retrying: ")

            time.sleep(min(60, sleep))
            sleep *= 1.5
            if self.running == 0 and sleep >= retry_limit:
                _log.debug("Abandoning clean.")
                return


def dict_to_file(dict, iocs, conf):
    filename = conf.get('debug_file_loc', None)
    if filename:
        if os.path.isfile(filename):
            os.remove(filename)
        list = []
        for key in dict:
            list.append([key, iocs[dict[key][-1]]['hostname'], iocs[dict[key][-1]]['iocname']])

        list.sort(key=itemgetter(0))

        with open(filename, 'wrx') as f:
            json.dump(list, f)


def __updateCF__(client, new, delrec, channels_dict, iocs, hostName, iocName, time, owner):
    if hostName is None or iocName is None:
        raise Exception('missing hostName or iocName')
    channels = []
    checkPropertiesExist(client, owner)
    old = client.findByArgs([('hostName', hostName), ('iocName', iocName)])
    if old is not None:
        for ch in old:
            if new == [] or ch[u'name'] in delrec:  # case: empty commit/del, remove all reference to ioc
                if ch[u'name'] in channels_dict:
                    channels.append(updateChannel(ch,
                                                  owner=iocs[channels_dict[ch[u'name']][-1]]["owner"],
                                                  hostName=iocs[channels_dict[ch[u'name']][-1]]["hostname"],
                                                  iocName=iocs[channels_dict[ch[u'name']][-1]]["iocname"],
                                                  pvStatus='Active',
                                                  time=time))
                else:
                    '''Orphan the channel : mark as inactive, keep the old hostName and iocName'''
                    oldHostName = hostName
                    oldIocName = iocName
                    oldTime = time
                    for prop in ch[u'properties']:
                        if prop[u'name'] == u'hostName':
                            oldHostName = prop[u'value']
                        if prop[u'name'] == u'iocName':
                            oldIocName = prop[u'value']
                        if prop[u'name'] == u'time':
                            oldTime = prop[u'value']
                    channels.append(updateChannel(ch,
                                                  owner=owner,
                                                  hostName=oldHostName,
                                                  iocName=oldIocName,
                                                  pvStatus='Inactive',
                                                  time=oldTime))
            else:
                if ch in new:  # case: channel in old and new
                    channels.append(updateChannel(ch,
                                                  owner=iocs[channels_dict[ch[u'name']][-1]]["owner"],
                                                  hostName=iocs[channels_dict[ch[u'name']][-1]]["hostname"],
                                                  iocName=iocs[channels_dict[ch[u'name']][-1]]["iocname"],
                                                  pvStatus='Active',
                                                  time=time))
                    new.remove(ch[u'name'])

    # now pvNames contains a list of pv's new on this host/ioc
    for pv in new:
        ch = client.findByArgs([('~name', pv)])
        if not ch:
            '''New channel'''
            channels.append(createChannel(pv,
                                          chOwner=owner,
                                          hostName=hostName,
                                          iocName=iocName,
                                          pvStatus='Active',
                                          time=time))
        else:
            '''update existing channel: exists but with a different hostName and/or iocName'''
            channels.append(updateChannel(ch[0],
                                          owner=owner,
                                          hostName=hostName,
                                          iocName=iocName,
                                          pvStatus='Active',
                                          time=time))
    if len(channels) != 0:  # Fixes a potential server error which occurs when a client.set results in no changes
        client.set(channels=channels)
    else:
        if old and len(old) != 0:
            client.set(channels=channels)

def updateChannel(channel, owner, hostName=None, iocName=None, pvStatus='Inactive', time=None):
    '''
    Helper to update a channel object so as to not affect the existing properties
    '''
    # properties list devoid of hostName and iocName properties
    if channel[u'properties']:
        channel[u'properties'] = [property for property in channel[u'properties']
                         if property[u'name'] != 'hostName'
                         and property[u'name'] != 'iocName'
                         and property[u'name'] != 'pvStatus'
                         and property[u'name'] != 'time']
    else:
        channel[u'properties'] = []
    if hostName is not None:
        channel[u'properties'].append({u'name': 'hostName', u'owner': owner, u'value': hostName})
    if iocName is not None:
        channel[u'properties'].append({u'name': 'iocName', u'owner': owner, u'value': iocName})
    if pvStatus:
        channel[u'properties'].append({u'name': 'pvStatus', u'owner': owner, u'value': pvStatus})
    if time:
        channel[u'properties'].append({u'name': 'time', u'owner': owner, u'value': time})
    return channel

def clean_channel(channel):
    # properties list devoid of hostName and iocName properties
    if channel[u'properties']:
        channel[u'properties'] = [property for property in channel[u'properties']
                                  if property[u'name'] != 'pvStatus']
    else:
        channel[u'properties'] = []

    channel[u'properties'].append({u'name': 'pvStatus', u'owner': channel['owner'], u'value': 'Inactive'})
    return channel

def createChannel(chName, chOwner, hostName=None, iocName=None, pvStatus='Inactive', time=None):
    '''
    Helper to create a channel object with the required properties
    '''
    ch = {u'name': chName, u'owner': chOwner, u'properties': []}
    if hostName is not None:
        ch[u'properties'].append({u'name': 'hostName', u'owner': chOwner, u'value': hostName})
    if iocName is not None:
        ch[u'properties'].append({u'name': 'iocName', u'owner': chOwner, u'value': iocName})
    if pvStatus:
        ch[u'properties'].append({u'name': 'pvStatus', u'owner': chOwner, u'value': pvStatus})
    if time:
        ch[u'properties'].append({u'name': 'time', u'owner': chOwner, u'value': time})
    return ch

def checkPropertiesExist(client, propOwner):
    '''
    Checks if the properties used by dbUpdate are present if not it creates them
    '''
    requiredProperties = ['hostName', 'iocName', 'pvStatus', 'time']
    for propName in requiredProperties:
        if client.findProperty(propName) is None:
            try:
                client.set(property={u'name': propName, u'owner': propOwner})
            except Exception:
                _log.exception('Failed to create the property %s', propName)
                raise


def getCurrentTime():
    return str(datetime.datetime.now())


def poll(update, client, new, delrec, channels_dict, iocs, hostName, iocName, times, owner):
    _log.debug("Polling begin: ")
    sleep = 1
    success = False
    while not success:
        try:
            update(client, new, delrec, channels_dict, iocs, hostName, iocName, times, owner)
            success = True
            return success
        except RequestException as e:
            _log.debug("error: " + str(e.message))
            _log.debug("SLEEP: " + str(min(60, sleep)))
            _log.debug(str(channels_dict))
            time.sleep(min(60, sleep))
            sleep *= 1.5

