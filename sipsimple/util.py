# Copyright (C) 2008-2009 AG Projects. See LICENSE for details.
#

"""
Implements utilities commonly used in various parts of the library.
"""

from __future__ import absolute_import, with_statement

__all__ = ["classproperty", "run_in_green_thread", "run_in_waitable_green_thread", "run_in_twisted_thread",
           "Command", "PersistentTones", "Route", "SilenceableWaveFile", "Timestamp", "TimestampedNotificationData",
           "call_in_green_thread", "call_in_twisted_thread", "limit", "makedirs"]

import errno
import os
import re
import socket
import sys
from datetime import datetime, timedelta
from threading import Lock, Timer

from zope.interface import implements
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python.decorator import decorator, preserve_signature
from eventlet import coros
from eventlet.twistedutil import callInGreenThread
from twisted.python import threadable


# Descriptors and decorators
#

def classproperty(function):
    class Descriptor(object):
        def __get__(self, instance, owner):
            return function(owner)
        def __set__(self, instance, value):
            raise AttributeError("read-only attribute cannot be set")
        def __delete__(self, instance):
            raise AttributeError("read-only attribute cannot be deleted")
    return Descriptor()


@decorator
def run_in_green_thread(func):
    @preserve_signature(func)
    def wrapper(*args, **kwargs):
        from twisted.internet import reactor
        if threadable.isInIOThread():
            callInGreenThread(func, *args, **kwargs)
        else:
            reactor.callFromThread(callInGreenThread, func, *args, **kwargs)
    return wrapper


@decorator
def run_in_waitable_green_thread(func):
    @preserve_signature(func)
    def wrapper(*args, **kwargs):
        from twisted.internet import reactor
        event = coros.event()
        def wrapped_func():
            try:
                result = func(*args, **kwargs)
            except:
                event.send_exception(*sys.exc_info())
            else:
                event.send(result)
        if threadable.isInIOThread():
            callInGreenThread(wrapped_func)
        else:
            reactor.callFromThread(callInGreenThread, wrapped_func)
        return event
    return wrapper


@decorator
def run_in_twisted_thread(func):
    @preserve_signature(func)
    def wrapper(*args, **kwargs):
        from twisted.internet import reactor
        if threadable.isInIOThread():
            func(*args, **kwargs)
        else:
            reactor.callFromThread(func, *args, **kwargs)
    return wrapper


# Utility classes
#

class Command(object):
    def __init__(self, name, event=None):
        self.name = name
        self.event = event or coros.event()

    def signal(self):
        self.event.send()

    def wait(self):
        return self.event.wait()


class PersistentTones(object):

    def __init__(self, conference_bridge, tones, interval, volume=100, initial_play=True):
        from sipsimple.core import ToneGenerator
        self.tones = tones
        self.interval = interval
        self._initial_play = initial_play
        self._lock = Lock()
        self._timer = None
        self._tone_generator = ToneGenerator(conference_bridge)
        self._tone_generator.volume = volume

    @property
    def is_active(self):
        with self._lock:
            return self._timer is not None

    def _play_tones(self):
        from sipsimple.core import SIPCoreError
        with self._lock:
            try:
                self._tone_generator.play_tones(self.tones)
            except SIPCoreError:
                pass
            self._timer = Timer(self.interval, self._play_tones)
            self._timer.setDaemon(True)
            self._timer.start()

    def start(self, *args, **kwargs):
        if self._timer is None:
            if not self._tone_generator.is_active:
                self._tone_generator.start()
                self._tone_generator.conference_bridge.connect_slots(self._tone_generator.slot, 0)
            if self._initial_play:
                self._play_tones()
            else:
                self._timer = Timer(self.interval, self._play_tones)
                self._timer.setDaemon(True)
                self._timer.start()

    def stop(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
                if self._tone_generator.is_active:
                    self._tone_generator.stop()


class Route(object):
    def __init__(self, address, port=None, transport='udp'):
        self.address = address
        self.port = port
        self.transport = transport

    def _get_address(self):
        return self._address
    def _set_address(self, address):
        try:
            socket.inet_aton(address)
        except:
            raise ValueError('illegal address: %s' % address)
        self._address = address
    address = property(_get_address, _set_address)
    del _get_address, _set_address

    def _get_port(self):
        if self._port is None:
            return 5060 if self.transport in ('udp', 'tcp') else 5061
        else:
            return self._port
    def _set_port(self, port):
        port = int(port) if port is not None else None
        if port is not None and not (0 < port < 65536):
            raise ValueError('illegal port value: %d' % port)
        self._port = port
    port = property(_get_port, _set_port)
    del _get_port, _set_port

    def _get_transport(self):
        return self._transport
    def _set_transport(self, transport):
        if transport not in ('udp', 'tcp', 'tls'):
            raise ValueError('illegal transport value: %s' % transport)
        self._transport = transport
    transport = property(_get_transport, _set_transport)
    del _get_transport, _set_transport

    def get_uri(self):
        from sipsimple.core import SIPURI
        if self.transport in ('udp', 'tcp') and self.port == 5060:
            port = None
        elif self.transport == 'tls' and self.port == 5061:
            port = None
        else:
            port = self.port
        parameters = {'transport': self.transport} if self.transport != 'udp' else {}
        return SIPURI(host=self.address, port=port, parameters=parameters)

    def __repr__(self):
        return '%s(%r, %r, %r)' % (self.__class__.__name__, self.address, self.port, self.transport)
    
    def __str__(self):
        return 'sip:%s:%d;transport=%s' % (self.address, self.port, self.transport)


class SilenceableWaveFile(object):
    implements(IObserver)

    def __init__(self, conference_bridge, file_name, volume=100, loop_count=1, pause_time=0, initial_play=True):
        self.conference_bridge = conference_bridge
        self.file_name = file_name
        self.volume = volume
        self.loop_count = loop_count
        self.pause_time = pause_time
        self.initial_play = initial_play
        self._channel = None
        self._current_loop = 0
        self._state = 'stopped'
        self._wave_file = None
        if not os.path.exists(file_name):
            raise ValueError("File not found: %s" % file_name)

    @run_in_twisted_thread
    def start(self):
        if self._state != 'stopped':
            return
        self._state = 'started'
        self._channel = coros.queue()
        self._current_loop = 0
        if self.initial_play:
            self._channel.send(Command('play'))
        else:
            from twisted.internet import reactor
            reactor.callLater(self.pause_time, self._channel.send, Command('play'))
        self._run()

    @run_in_twisted_thread
    def stop(self):
        if self._state != 'started':
            return
        self._channel.send(Command('stop'))

    @property
    def is_active(self):
        return self._state == "started"

    @run_in_green_thread
    def _run(self):
        from sipsimple.core import SIPCoreError, WaveFile
        notification_center = NotificationCenter()
        try:
            while True:
                command = self._channel.wait()
                if command.name == 'play':
                    self._wave_file = WaveFile(self.conference_bridge, self.file_name)
                    notification_center.add_observer(self, sender=self._wave_file, name='WaveFileDidFinishPlaying')
                    self._wave_file.volume = self.volume
                    try:
                        self._wave_file.start()
                    except SIPCoreError:
                        self._channel.send(Command('reschedule'))
                    else:
                        self.conference_bridge.connect_slots(self._wave_file.slot, 0)
                elif command.name == 'reschedule':
                    self._current_loop += 1
                    notification_center.remove_observer(self, sender=self._wave_file, name='WaveFileDidFinishPlaying')
                    self._wave_file = None
                    if self.loop_count == 0 or self._current_loop < self.loop_count:
                        from twisted.internet import reactor
                        reactor.callLater(self.pause_time, self._channel.send, Command('play'))
                    else:
                        break
                elif command.name == 'stop':
                    if self._wave_file is not None:
                        notification_center.remove_observer(self, sender=self._wave_file, name='WaveFileDidFinishPlaying')
                        self._wave_file.stop()
                        self._wave_file = None
                    break
        finally:
            self._channel = None
            self._state = 'stopped'

    @run_in_twisted_thread
    def handle_notification(self, notification):
        self._channel.send(Command('reschedule'))


class Timestamp(datetime):
    _timestamp_re = re.compile(r'(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})T(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(\.(?P<secfrac>\d{1,}))?((?P<UTC>Z)|((?P<tzsign>\+|-)(?P<tzhour>\d{2}):(?P<tzminute>\d{2})))')

    def __init__(self, *args, **kwargs):
        if kwargs:
            datetime.__init__(self, *args, **kwargs)

    @classmethod
    def utc_offset(cls):
        timediff = datetime.now() - datetime.utcnow()
        return int(round((timediff.days*86400 + timediff.seconds + timediff.microseconds/1000000.0)/60))

    @classmethod
    def parse(cls, stamp):
        if stamp is None:
            return None
        match = cls._timestamp_re.match(stamp)
        if match is None:
            raise ValueError("Timestamp %s is not in RFC3339 format" % stamp)
        dct = match.groupdict()
        if dct['UTC'] is not None:
            secoffset = 0
        else:
            secoffset = int(dct['tzminute'])*60 + int(dct['tzhour'])*3600
            if dct['tzsign'] == '-':
                secoffset *= -1
        if dct['secfrac'] is not None:
            secfrac = dct['secfrac'][:6]
            secfrac += '0'*(6-len(secfrac))
            secfrac = int(secfrac)
        else:
            secfrac = 0
        dt = datetime(int(dct['year']), month=int(dct['month']), day=int(dct['day']),
                      hour=int(dct['hour']), minute=int(dct['minute']), second=int(dct['second']),
                      microsecond=secfrac)
        return cls(dt - timedelta(seconds=secoffset) + timedelta(seconds=cls.utc_offset()*60))

    @classmethod
    def format(cls, dt):
        if dt is None:
            return None
        minutes = cls.utc_offset()
        if minutes == 0:
            tzspec = 'Z'
        else:
            if minutes < 0:
                sign = '-'
                minutes *= -1
            else:
                sign = '+'
            hours = minutes / 60
            minutes = minutes % 60
            tzspec = '%s%02d:%02d' % (sign, hours, minutes)
        return dt.replace(microsecond=0).isoformat()+tzspec

    def __new__(cls, value, *args, **kwargs):
        if isinstance(value, cls):
            return value
        elif isinstance(value, datetime):
            return cls(value.year, month=value.month, day=value.day,
                       hour=value.hour, minute=value.minute, second=value.second,
                       microsecond=value.microsecond)
        elif isinstance(value, basestring):
            return cls.parse(value)
        else:
            return datetime.__new__(cls, value, *args, **kwargs)

    def __str__(self):
        return self.format(self)


class TimestampedNotificationData(NotificationData):

    def __init__(self, **kwargs):
        self.timestamp = datetime.now()
        NotificationData.__init__(self, **kwargs)


# Utility functions
#

def call_in_green_thread(func, *args, **kwargs):
    from twisted.internet import reactor
    if threadable.isInIOThread():
        callInGreenThread(*args, **kwargs)
    else:
        reactor.callFromThread(callInGreenThread, func, *args, **kwargs)


def call_in_twisted_thread(func, *args, **kwargs):
    from twisted.internet import reactor
    if threadable.isInIOThread():
        func(*args, **kwargs)
    else:
        reactor.callFromThread(func, *args, **kwargs)


def limit(value, min=float("-infinity"), max=float("+infinity")):
    from __builtin__ import min as minimum, max as maximum
    return maximum(min, minimum(value, max))


def makedirs(path):
    try:
        os.makedirs(path)
    except OSError, e:
        if e.errno == errno.EEXIST and os.path.isdir(path): # directory exists
            return
        raise


