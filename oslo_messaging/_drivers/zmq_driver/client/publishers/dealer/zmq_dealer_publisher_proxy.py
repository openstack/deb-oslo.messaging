#    Copyright 2015 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import logging
import six
import time

from oslo_messaging._drivers.zmq_driver.client.publishers.dealer \
    import zmq_dealer_call_publisher
from oslo_messaging._drivers.zmq_driver.client.publishers.dealer \
    import zmq_reply_waiter
from oslo_messaging._drivers.zmq_driver.client.publishers \
    import zmq_publisher_base
from oslo_messaging._drivers.zmq_driver import zmq_address
from oslo_messaging._drivers.zmq_driver import zmq_async
from oslo_messaging._drivers.zmq_driver import zmq_names
from oslo_messaging._drivers.zmq_driver import zmq_updater

zmq = zmq_async.import_zmq()

LOG = logging.getLogger(__name__)


class DealerPublisherProxy(object):
    """Used when publishing to a proxy. """

    def __init__(self, conf, matchmaker, socket_to_proxy):
        self.conf = conf
        self.sockets_manager = zmq_publisher_base.SocketsManager(
            conf, matchmaker, zmq.ROUTER, zmq.DEALER)
        self.socket = socket_to_proxy
        self.routing_table = RoutingTable(conf, matchmaker)
        self.connection_updater = PublisherConnectionUpdater(
            conf, matchmaker, self.socket)

    def send_request(self, request):
        if request.msg_type == zmq_names.CALL_TYPE:
            raise zmq_publisher_base.UnsupportedSendPattern(
                request.msg_type)

        if self.conf.use_pub_sub:
            routing_key = self.routing_table.get_routable_host(request.target) \
                if request.msg_type in zmq_names.DIRECT_TYPES else \
                zmq_address.target_to_subscribe_filter(request.target)
            self._do_send_request(request, routing_key)
        else:
            routing_keys = self.routing_table.get_all_hosts(request.target)
            for routing_key in routing_keys:
                self._do_send_request(request, routing_key)

    def _do_send_request(self, request, routing_key):
        self.socket.send(b'', zmq.SNDMORE)
        self.socket.send(six.b(str(request.msg_type)), zmq.SNDMORE)
        self.socket.send(six.b(routing_key), zmq.SNDMORE)
        self.socket.send(six.b(request.message_id), zmq.SNDMORE)
        self.socket.send_pyobj(request.context, zmq.SNDMORE)
        self.socket.send_pyobj(request.message)

        LOG.debug("->[proxy:%(addr)s] Sending message_id %(message)s to "
                  "a target %(target)s",
                  {"message": request.message_id,
                   "target": request.target,
                   "addr": list(self.socket.connections)})

    def cleanup(self):
        self.socket.close()


class DealerCallPublisherProxy(zmq_dealer_call_publisher.DealerCallPublisher):

    def __init__(self, conf, matchmaker, sockets_manager):
        reply_waiter = ReplyWaiterProxy(conf)
        sender = CallSenderProxy(conf, matchmaker, sockets_manager,
                                 reply_waiter)
        super(DealerCallPublisherProxy, self).__init__(
            conf, matchmaker, sockets_manager, sender, reply_waiter)


class CallSenderProxy(zmq_dealer_call_publisher.CallSender):

    def __init__(self, conf, matchmaker, sockets_manager, reply_waiter):
        super(CallSenderProxy, self).__init__(
            sockets_manager, reply_waiter)
        self.socket = self.outbound_sockets.get_socket_to_publishers()
        self.reply_waiter.poll_socket(self.socket)
        self.routing_table = RoutingTable(conf, matchmaker)
        self.connection_updater = PublisherConnectionUpdater(
            conf, matchmaker, self.socket)

    def _connect_socket(self, target):
        return self.socket

    def _do_send_request(self, socket, request):
        routing_key = self.routing_table.get_routable_host(request.target)

        # DEALER socket specific envelope empty delimiter
        socket.send(b'', zmq.SNDMORE)
        socket.send(six.b(str(request.msg_type)), zmq.SNDMORE)
        socket.send(six.b(routing_key), zmq.SNDMORE)
        socket.send(six.b(request.message_id), zmq.SNDMORE)
        socket.send_pyobj(request.context, zmq.SNDMORE)
        socket.send_pyobj(request.message)

        LOG.debug("Sent message_id %(message)s to a target %(target)s",
                  {"message": request.message_id,
                   "target": request.target})


class ReplyWaiterProxy(zmq_reply_waiter.ReplyWaiter):

    def receive_method(self, socket):
        empty = socket.recv()
        assert empty == b'', "Empty expected!"
        reply_id = socket.recv()
        assert reply_id is not None, "Reply ID expected!"
        message_type = int(socket.recv())
        assert message_type == zmq_names.REPLY_TYPE, "Reply is expected!"
        message_id = socket.recv()
        reply = socket.recv_pyobj()
        LOG.debug("Received reply %s", message_id)
        return reply


class RoutingTable(object):
    """This class implements local routing-table cache
        taken from matchmaker. Its purpose is to give the next routable
        host id (remote DEALER's id) by request for specific target in
        round-robin fashion.
    """

    def __init__(self, conf, matchmaker):
        self.conf = conf
        self.matchmaker = matchmaker
        self.routing_table = {}
        self.routable_hosts = {}

    def get_all_hosts(self, target):
        self._update_routing_table(target)
        return list(self.routable_hosts.get(str(target)) or [])

    def get_routable_host(self, target):
        self._update_routing_table(target)
        hosts_for_target = self.routable_hosts[str(target)]
        host = hosts_for_target.pop(0)
        if not hosts_for_target:
            self._renew_routable_hosts(target)
        return host

    def _is_tm_expired(self, tm):
        return 0 <= self.conf.zmq_target_expire <= time.time() - tm

    def _update_routing_table(self, target):
        routing_record = self.routing_table.get(str(target))
        if routing_record is None:
            self._fetch_hosts(target)
            self._renew_routable_hosts(target)
        elif self._is_tm_expired(routing_record[1]):
            self._fetch_hosts(target)

    def _fetch_hosts(self, target):
        self.routing_table[str(target)] = (self.matchmaker.get_hosts(
            target, zmq_names.socket_type_str(zmq.DEALER)), time.time())

    def _renew_routable_hosts(self, target):
        hosts, _ = self.routing_table[str(target)]
        self.routable_hosts[str(target)] = list(hosts)


class PublisherConnectionUpdater(zmq_updater.ConnectionUpdater):

    def _update_connection(self):
        publishers = self.matchmaker.get_publishers()
        for pub_address, router_address in publishers:
            self.socket.connect_to_host(router_address)
