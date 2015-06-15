# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:

# Copyright (c) 2013, Battelle Memorial Institute
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in
#    the documentation and/or other materials provided with the
#    distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation
# are those of the authors and should not be interpreted as representing
# official policies, either expressed or implied, of the FreeBSD
# Project.
#
# This material was prepared as an account of work sponsored by an
# agency of the United States Government.  Neither the United States
# Government nor the United States Department of Energy, nor Battelle,
# nor any of their employees, nor any jurisdiction or organization that
# has cooperated in the development of these materials, makes any
# warranty, express or implied, or assumes any legal liability or
# responsibility for the accuracy, completeness, or usefulness or any
# information, apparatus, product, software, or process disclosed, or
# represents that its use would not infringe privately owned rights.
#
# Reference herein to any specific commercial product, process, or
# service by trade name, trademark, manufacturer, or otherwise does not
# necessarily constitute or imply its endorsement, recommendation, or
# favoring by the United States Government or any agency thereof, or
# Battelle Memorial Institute. The views and opinions of authors
# expressed herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# PACIFIC NORTHWEST NATIONAL LABORATORY
# operated by BATTELLE for the UNITED STATES DEPARTMENT OF ENERGY
# under Contract DE-AC05-76RL01830
#}}}

from __future__ import absolute_import, print_function
from abc import abstractmethod
from collections import defaultdict
from dateutil.parser import parse
from datetime import datetime, timedelta

import logging
from pprint import pprint
import pytz
#from Queue import Queue, Empty
import re
import sqlite3
from threading import Thread

from zmq.utils import jsonapi



import gevent
from gevent.queue import Queue, Empty

from volttron.platform.vip.agent import *

from volttron.platform.agent import utils, matching
from volttron.platform.agent.vipagent import (BaseAgent, RPCAgent, export,
                                              onevent, spawn)
from volttron.platform.messaging import topics, headers as headers_mod


utils.setup_logging()
_log = logging.getLogger(__name__)

ACTUATOR_TOPIC_PREFIX_PARTS = len(topics.ACTUATOR_VALUE.split('/'))

class BaseHistorianAgent(Agent):
    '''This is the base agent for historian Agents.
    It automatically subscribes to all device publish topics.

    Event processing in publish_to_historian and setup in historian_setup
    both happen in the same thread separate from the main thread. This is
    to allow blocking while processing events.
    '''

    def __init__(self,
                 retry_period=300.0,
                 submit_size_limit=1000,
                 max_time_publishing=30,
                 **kwargs):
        super(BaseHistorianAgent, self).__init__(**kwargs)
        self._retry_period = retry_period
        self._submit_size_limit = submit_size_limit
        self._max_time_publishing = timedelta(seconds=max_time_publishing)
        self._successful_published = set()
        self._meta_data = defaultdict(dict)
        self._topic_map = {}
        self._event_queue = Queue()
        self._processing = False
#         self._process_thread = Thread(target = self._process_loop)
#         self._process_thread.daemon = True  # Don't wait on thread to exit.
#         self._process_thread.start()

    @Core.receiver("onsetup")
    def settingup(self, sender, **kwargs):
        backup_setup = gevent.spawn(self._setup_backup_db)
        historian_setup = gevent.spawn(self.historian_setup)

        gevent.joinall([backup_setup, historian_setup])
        _log.debug("All setup")


    @Core.receiver("onstart")
    def starting_base(self, sender, **kwargs):
        _log.debug("Starting base historian")

#         self.pubsub_subscribe(peer='pubsub',
#                               prefix=topics.DRIVER_TOPIC_BASE+'/'+topics.DRIVER_TOPIC_ALL,
#                               callback=self.capture_device_data)

        self.vip.pubsub.subscribe(peer='pubsub',
                               prefix=topics.LOGGER_LOG,
                               callback=self.capture_log_data)

    @Core.receiver("onstop")
    def stopping(self, sender, **kwargs):
        self.vip.pubsub.unsubscribe(peer=self.identity,
                                    topic=topics.LOGGER_LOG,
                                    callback=self.capture_log_data)

    def capture_log_data(self, peer, sender, bus, topic, headers, message):
        '''Capture log data and submit it to be published by a historian.'''

        parts = topic.split('/')
        location = '/'.join(reversed(parts[2:]))

        try:
            data = jsonapi.loads(message[0])
        except ValueError as e:
            _log.error("message for {topic} bad message string: {message_string}".format(topic=topic,
                                                                                     message_string=message[0]))
            return
        except IndexError as e:
            _log.error("message for {topic} missing message string".format(topic=topic))
            return

        source = 'log'
        _log.debug("Queuing {topic} from {source} for publish".format(topic=topic,
                                                                      source=source))
        for point, item in data.iteritems():
            ts_path = location + '/' + point
            if 'Readings' not in item or 'Units' not in item:
                _log.error("logging request for {path} missing Readings or Units".format(path=ts_path))
                continue
            units = item['Units']
            dtype = item.get('data_type', 'float')
            if dtype == 'double':
                dtype = 'float'

            meta = {'units': units, 'type': dtype}

            readings = item['Readings']
            if not isinstance(readings, list):
                readings = [(datetime.utcnow(), readings)]

            self._event_queue.put({'source': source,
                                   'topic': topic+'/'+point,
                                   'readings': readings,
                                   'meta':meta})
        if not self._processing:
            gevent.spawn(self._process_loop)

    # @matching.match_start(topics.DRIVER_TOPIC_BASE+'/'+topics.DRIVER_TOPIC_ALL)
    def capture_device_data(self, peer, bus, topic, headers, message):
        '''Capture device data and submit it to be published by a historian.'''
        timestamp_string = headers.get(headers_mod.DATE)
        if timestamp_string is None:
            _log.error("message for {topic} missing timetamp".format(topic=topic))
            return
        try:
            timestamp = parse(timestamp_string)
        except (ValueError, TypeError) as e:
            _log.error("message for {topic} bad timetamp string: {ts_string}".format(topic=topic,
                                                                                     ts_string=timestamp_string))
            return

        if timestamp.tzinfo is None:
            timestamp.replace(tzinfo=pytz.UTC)
        else:
            timestamp = timestamp.astimezone(pytz.UTC)

        parts = topic.split('/')
        device = '/'.join(reversed(parts[2:]))

        try:
            values = utils.jsonapi.loads(message[0])
        except ValueError as e:
            _log.error("message for {topic} bad message string: {message_string}".format(topic=topic,
                                                                                     message_string=message[0]))
            return
        except IndexError as e:
            _log.error("message for {topic} missing message string".format(topic=topic))
            return

        meta = {}
        try:
            meta = utils.jsonapi.loads(message[1])
        except ValueError as e:
            _log.warning("meta data for {topic} bad message string: {message_string}".format(topic=topic,
                                                                                     message_string=message[0]))
        except IndexError as e:
            _log.warning("meta data for {topic} missing message string".format(topic=topic))


        source = 'scrape'
        _log.debug("Queuing {topic} from {source} for publish".format(topic=topic,
                                                                      source=source))

        for key, value in values.iteritems():
            point_topic = device + '/' + key
            self._event_queue.put({'source': source,
                                   'topic': topic,
                                   'readings': [(timestamp,value)],
                                   'meta': meta.get(key,{})})

    @matching.match_start(topics.ACTUATOR_VALUE)
    def capture_actuator_data(self, topic, headers, message, match):
        '''Capture device data and submit it to be published by a historian.'''
        timestamp_string = headers.get('time')
        if timestamp_string is None:
            _log.error("message for {topic} missing timetamp".format(topic=topic))
            return
        try:
            timestamp = parse(timestamp_string)
        except (ValueError, TypeError) as e:
            _log.error("message for {topic} bad timetamp string: {ts_string}".format(topic=topic,
                                                                                     ts_string=timestamp_string))
            return

        parts = topic.split('/')
        topic = '/'.join(parts[ACTUATOR_TOPIC_PREFIX_PARTS:])

        try:
            value = utils.jsonapi.loads(message[0])
        except ValueError as e:
            _log.error("message for {topic} bad message string: {message_string}".format(topic=topic,
                                                                                     message_string=message[0]))
            return
        except IndexError as e:
            _log.error("message for {topic} missing message string".format(topic=topic))
            return

        source = 'actuator'
        _log.debug("Queuing {topic} from {source} for publish".format(topic=topic,
                                                                      source=source))


        self._event_queue.put({'source': source,
                               'topic': topic,
                               'readings': [timestamp,value]})


    def _process_loop(self):
        _log.debug("Starting process loop.")
        self._processing = True
        backup_setup = gevent.spawn(self._setup_backup_db)
        historian_setup = gevent.spawn(self.historian_setup)

        gevent.joinall([backup_setup, historian_setup])
        #Based on the state of the back log and whether or not sucessful
        #publishing is currently happening (and how long it's taking)
        #we may or may not want to wait on the event queue for more input
        #before proceeding with the rest of the loop.
        wait_for_input = not bool(self._get_outstanding_to_publish())

        while True:
            try:
                _log.debug("Reading from/waiting for queue.")
                new_to_publish = [self._event_queue.get(wait_for_input, self._retry_period)]
            except Empty:
                _log.debug("Queue wait timed out. Falling out.")
                new_to_publish = []

            if new_to_publish:
                while True:
                    try:
                        _log.debug("Checking for queue build up.")
                        new_to_publish.append(self._event_queue.get_nowait())
                    except Empty:
                        break

            self._backup_new_to_publish(new_to_publish)

            wait_for_input = True
            start_time = datetime.utcnow()

            while True:
                to_publish_list = self._get_outstanding_to_publish()
                if not to_publish_list:
                    break
                _log.debug("Calling publish_to_historian.")
                self.publish_to_historian(to_publish_list)
                if not self._any_sucessfull_publishes():
                    break
                self._cleanup_successful_publishes()

                now = datetime.utcnow()
                if now - start_time > self._max_time_publishing:
                    wait_for_input = False
                    break
        self._processing = False
        _log.debug("Finished processing")

    def _setup_backup_db(self):
        _log.debug("Setting up backup DB.")
        self._connection = sqlite3.connect('backup.sqlite',
                                           detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES)

        c = self._connection.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outstanding';")

        if c.fetchone() is None:
            _log.debug("Configuring backup BD for the first time.")
            self._connection.execute('''PRAGMA auto_vacuum = FULL''')
            self._connection.execute('''CREATE TABLE outstanding
                                        (id INTEGER PRIMARY KEY,
                                         ts timestamp NOT NULL,
                                         source TEXT NOT NULL,
                                         topic_id INTEGER NOT NULL,
                                         value_string TEXT NOT NULL,
                                         UNIQUE(ts, topic_id, source))''')

        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='metadata';")

        if c.fetchone() is None:
            self._connection.execute('''CREATE TABLE metadata
                                        (source TEXT NOT NULL,
                                         topic_id INTEGER NOT NULL,
                                         name TEXT NOT NULL,
                                         value TEXT NOT NULL,
                                         UNIQUE(topic_id, source, name))''')
        else:
            c.execute("SELECT * FROM metadata")
            for row in c:
                self._meta_data[(row[0], row[1])][row[2]] = row[3]

        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='topics';")

        if c.fetchone() is None:
            self._connection.execute('''create table topics
                                        (topic_id INTEGER PRIMARY KEY,
                                         topic_name TEXT NOT NULL,
                                         UNIQUE(topic_name))''')
        else:
            c.execute("SELECT * FROM topics")
            for row in c:
                self._topic_map[row[0]] = row[1]
                self._topic_map[row[1]] = row[0]

        c.close()

        self._connection.commit()

    def _get_outstanding_to_publish(self):
        _log.debug("Getting oldest outstanding to publish.")
        c = self._connection.cursor()
        c.execute('select * from outstanding order by ts limit ?', (self._submit_size_limit,))

        results = []
        for row in c:
            _id = row[0]
            timestamp = row[1]
            source = row[2]
            topic_id = row[3]
            value = jsonapi.loads(row[4])
            meta = self._meta_data[(source, topic_id)].copy()
            results.append({'_id':_id,
                            'timestamp': timestamp.replace(tzinfo=pytz.UTC),
                            'source': source,
                            'topic': self._topic_map[topic_id],
                            'value': value,
                            'meta': meta})

        c.close()

        return results

    def _cleanup_successful_publishes(self):
        _log.debug("Cleaning up successfully published values.")
        c = self._connection.cursor()

        if None in self._successful_published:
            c.execute('''DELETE FROM outstanding
                        WHERE ROWID IN
                        (SELECT ROWID FROM outstanding
                          ORDER BY ts LIMIT ?)''', (self._submit_size_limit,))
        else:
            temp = list(self._successful_published)
            temp.sort()
            pprint(temp)
            c.executemany('''DELETE FROM outstanding
                            WHERE id = ?''',
                            ((_id,) for _id in
                             self._successful_published))

        self._connection.commit()

        self._successful_published = set()

    def _any_sucessfull_publishes(self):
        return bool(self._successful_published)

    def _backup_new_to_publish(self, new_publish_list):
        _log.debug("Backing up unpublished values.")
        c = self._connection.cursor()

        for item in new_publish_list:
            source = item['source']
            topic = item['topic']
            meta = item.get('meta', {})
            values = item['readings']

            topic_id = self._topic_map.get(topic)

            if topic_id is None:
                    c.execute('''INSERT INTO topics values (?,?)''', (None, topic))
                    c.execute('''SELECT last_insert_rowid()''')
                    row = c.fetchone()
                    topic_id = row[0]
                    self._topic_map[topic_id] = topic
                    self._topic_map[topic] = topic_id

            #update meta data
            for name, value in meta.iteritems():
                c.execute('''INSERT OR REPLACE INTO metadata values(?, ?, ?, ?)''',
                            (source,topic_id,name,value))
                self._meta_data[(source,topic_id)][name] = value

            for timestamp, value in values:
                c.execute('''INSERT OR REPLACE INTO outstanding values(NULL, ?, ?, ?, ?)''',
                          (timestamp,source,topic_id,jsonapi.dumps(value)))

        self._connection.commit()

    def report_published(self, record):
        self._successful_published.add(record['_id'])

    def report_all_published(self):
        self._successful_published.add(None)

    @abstractmethod
    def publish_to_historian(self, to_publish_list):
        '''Main publishing method for historian Agents.'''

    def historian_setup(self):
        '''Optional setup routine, run in the processing thread before
           main processing loop starts.'''


class BaseQueryHistorianAgent(RPCAgent):
    '''This is the base agent for query historian Agents.
    It defines functions that must be defined to impliment the

    Event processing in publish_to_historian and setup in historian_setup
    both happen in the same thread separate from the main thread. This is
    to allow blocking while processing events.
    '''

    @export()
    def query(self, topic=None, start=None, end=None, skip=0, count=None):
        """Actual RPC handler"""

        if topic is None:
            raise TypeError('"Topic" required')

        if start is not None:
            try:
                start = parse(start)
            except TypeError:
                start = time_parser.parse(start)

        if end is not None:
            try:
                end = parse(end)
            except TypeError:
                end = time_parser.parse(end)

        _log.debug("In base query")

        if start:
            _log.debug("start={}".format(start))

        results = self.query_historian(topic, start, end, skip, count)
        metadata = results.get("metadata")
        if metadata is None:
            results['metadata'] = {}
        return results

    @abstractmethod
    def query_historian(self, topic, start=None, end=None, skip=0, count=None):
        """This function should return the results of a query in the form:
        {"values": [(timestamp1: value1), (timestamp2: value2), ...],
         "metadata": {"key1": value1, "key2": value2, ...}}

         metadata is not required (The caller will normalize this to {} for you)
        """

#The following code is
#Copyright (c) 2011, 2012, Regents of the University of California
#and is under the same licence as the remainder of the code in this file.
#Modification were made to remove unneeded pieces and to fit with the
#intended use.
import ply
import ply.lex as lex
import ply.yacc as yacc
from dateutil.tz import gettz, tzlocal
local = tzlocal()

def now(tzstr = 'UTC'):
    '''Returns an aware datetime object with the current time in tzstr timezone'''
    if tzstr == 'Local':
        tz = local
    else:
        tz = gettz(tzstr)
    return datetime.datetime.now(tz)

def strptime_tz(str, format='%x %X', tzstr='Local'):
    '''Returns an aware datetime object. tzstr is a timezone string such as
       'US/Pacific' or 'Local' by default which uses the local timezone.
    '''
    dt = datetime.datetime.strptime(str, format)
    if tzstr == 'Local':
        tz = local
    else:
        tz = gettz(tzstr)
    return dt.replace(tzinfo = tz)

tokens = ('NOW',"QSTRING", 'LVALUE', 'NUMBER')

reserved = {
    'now' : 'NOW'}

literals = '()[]*^.,<>=+-/'

time_units = re.compile('^(d|days?|h|hours?|m|minutes?|s|seconds?)$')

def get_timeunit(t):
    if not time_units.match(t):
        raise ValueError("Invalid timeunit: %s" % t)
    if t.startswith('d'): return 'days'
    elif t.startswith('h'): return 'hours'
    elif t.startswith('m'): return 'minutes'
    elif t.startswith('s'): return 'seconds'

def t_QSTRING(t):
    r'("[^"\\]*?(\\.[^"\\]*?)*?")|(\'[^\'\\]*?(\\.[^\'\\]*?)*?\')'
    if t.value[0] == '"':
        t.value = t.value[1:-1].replace('\\"', '"')
    elif t.value[0] == "'":
        t.value = t.value[1:-1].replace("\\'", "'")
    return t

def t_LVALUE(t):
    r'[a-zA-Z\~\$\_][a-zA-Z0-9\/\%_\-]*'
    t.type = reserved.get(t.value, 'LVALUE')
    return t

def t_NUMBER(t):
    r'([+-]?([0-9]*\.)?[0-9]+)'
    if '.' in t.value:
        try:
            t.value = float(t.value)
        except ValueError:
            print("Invalid floating point number", t.value)
            t.value = 0
    else:
        try:
            t.value = int(t.value)
        except ValueError:
            print("Integer value too large %d", t.value)
            t.value = 0

    return t
is_number = lambda x: isinstance(x, int) or isinstance(x, float)

t_ignore = " \t"
def t_newline(t):
    r'[\n\r]+'
    t.lexer.lineno += t.value.count("\n")

def t_error(t):
    raise ValueError("Syntax Error in Query")
    #print("Illegal character '%s'" % t.value[0])
    #t.lexer.skip(1)

smapql_lex = lex.lex()

TIMEZONE_PATTERNS = [
    "%m/%d/%Y",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    ]
def parse_time(ts):
    for pat in TIMEZONE_PATTERNS:
        try:
            return strptime_tz(ts, pat)
        except ValueError:
            continue
    raise ValueError("Invalid time string:" + ts)

def p_query_pair(t):
    """query : '(' timeref ',' timeref ')' """
    t[0] = (t[2], t[4])

def p_query_single(t):
    """query : timeref """
    t[0] = t[1]

# an absolute time reference.  can be a unix timestamp, a date string,
# or "now"
def p_timeref(t):
    """timeref : abstime
               | abstime reltime"""
    t[0] = t[1]
    if len(t) == 2:
        ref = t[1]
    else:
        ref = t[1] + t[2]
    t[0] = ref

def p_abstime(t):
    """abstime : NUMBER
               | QSTRING
               | NOW"""
    if t[1] == 'now':
        t[0] = now()
    elif type(t[1]) == type(''):
        t[0] = parse_time(t[1])
    else:
        t[0] = datetime.datetime.utcfromtimestamp(t[1] / 1000)

def p_reltime(t):
    """reltime : NUMBER LVALUE
               | NUMBER LVALUE reltime"""
    timeunit = get_timeunit(t[2])
    delta = datetime.timedelta(**{timeunit: t[1]})
    if len(t) == 3:
        t[0] = delta
    else:
        t[0] = t[3] + delta

# Error rule for syntax errors
def p_error(p):
    raise ValueError("Syntax Error in Query")

# Build the parser
time_parser = yacc.yacc(write_tables=0)