# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:

# Copyright (c) 2016, Battelle Memorial Institute
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

# }}}

import datetime
import logging
import os
import sys

from volttron.platform.vip.agent import Agent, Core, PubSub
from volttron.platform.messaging import topics
from volttron.platform.agent import utils
from volttron.platform.messaging.utils import normtopic


from dateutil.parser import parse


utils.setup_logging()
_log = logging.getLogger(__name__)

__version__ = "0.1"


class AlertMonitorAgent(Agent):
    """
    The `AlertMonitoringAgent` is a simple example class for demonstrating
    the functionality of the `volttron.platform.vip.agent.subsystems.health`
    subsystem.

    The `AlertMonitoringAgent` will listen to the `ALERTS_BASE` (alerts) topic
    and then write any alert to a text file.  The text file location can
    be customized from the agent configuration file using the "outfile"
    parameter.
    """

    def __init__(self, config_path, **kwargs):
        """ Configures the `AlertMonitorAgent`

        Validates that the outfile parameter in the config file is specified
        and sets up the agent.

        @param config_path: path to the configuration file for this agent.
        @param kwargs:
        @return:
        """
        config = utils.load_config(config_path)
        self._outfile = config.pop('outfile')
        if not self._outfile:
            raise ValueError('Invalid outfile parameter in config file.')

        # pop off the identity arge because we are goint to explicitly
        # set it to our identity.  If we didn't do this it would cause
        # an error.  The default identity is the uuid of the agent.
        kwargs.pop('identity')

        _log.debug('outfile is {}'.format(os.path.abspath(self._outfile)))
        super(AlertMonitorAgent, self).__init__(**kwargs)

    @PubSub.subscribe("pubsub", topics.ALERTS.format(agent_class='',
                      agent_uuid=''))
    def onmessage(self, peer, sender, bus, topic, headers, message):
        with open(self._outfile, 'a') as f:
            f.write("headers: {} message: {}\n".format(headers, message))


def main(argv=sys.argv):
    '''Main method called to start the agent.'''
    utils.vip_main(AlertMonitorAgent, identity='alert.monitor')


if __name__ == '__main__':
    # Entry point for script
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
