"""
Zeromq transport classes
"""

import asyncio
import asyncio.exceptions
import errno
import hashlib
import logging
import multiprocessing
import os
import signal
import sys
import threading
from random import randint

import tornado
import tornado.concurrent
import tornado.gen
import tornado.ioloop
import tornado.locks
import zmq.asyncio
import zmq.error
import zmq.eventloop.future
import zmq.eventloop.zmqstream

import salt.payload
import salt.transport.base
import salt.utils.files
import salt.utils.process
import salt.utils.stringutils
import salt.utils.zeromq
from salt._compat import ipaddress
from salt.exceptions import SaltException, SaltReqTimeoutError
from salt.utils.zeromq import LIBZMQ_VERSION_INFO, ZMQ_VERSION_INFO, zmq

try:
    import zmq.utils.monitor

    HAS_ZMQ_MONITOR = True
except ImportError:
    HAS_ZMQ_MONITOR = False


log = logging.getLogger(__name__)


def _get_master_uri(master_ip, master_port, source_ip=None, source_port=None):
    """
    Return the ZeroMQ URI to connect the Minion to the Master.
    It supports different source IP / port, given the ZeroMQ syntax:
    // Connecting using a IP address and bind to an IP address
    rc = zmq_connect(socket, "tcp://192.168.1.17:5555;192.168.1.1:5555"); assert (rc == 0);
    Source: http://api.zeromq.org/4-1:zmq-tcp
    """
    from salt.utils.network import ip_bracket

    master_uri = "tcp://{master_ip}:{master_port}".format(
        master_ip=ip_bracket(master_ip), master_port=master_port
    )

    if source_ip or source_port:
        if LIBZMQ_VERSION_INFO >= (4, 1, 6) and ZMQ_VERSION_INFO >= (16, 0, 1):
            # The source:port syntax for ZeroMQ has been added in libzmq 4.1.6
            # which is included in the pyzmq wheels starting with 16.0.1.
            if source_ip and source_port:
                master_uri = (
                    "tcp://{source_ip}:{source_port};{master_ip}:{master_port}".format(
                        source_ip=ip_bracket(source_ip),
                        source_port=source_port,
                        master_ip=ip_bracket(master_ip),
                        master_port=master_port,
                    )
                )
            elif source_ip and not source_port:
                master_uri = "tcp://{source_ip}:0;{master_ip}:{master_port}".format(
                    source_ip=ip_bracket(source_ip),
                    master_ip=ip_bracket(master_ip),
                    master_port=master_port,
                )
            elif source_port and not source_ip:
                ip_any = (
                    "0.0.0.0"
                    if ipaddress.ip_address(master_ip).version == 4
                    else ip_bracket("::")
                )
                master_uri = (
                    "tcp://{ip_any}:{source_port};{master_ip}:{master_port}".format(
                        ip_any=ip_any,
                        source_port=source_port,
                        master_ip=ip_bracket(master_ip),
                        master_port=master_port,
                    )
                )
        else:
            log.warning(
                "Unable to connect to the Master using a specific source IP / port"
            )
            log.warning("Consider upgrading to pyzmq >= 16.0.1 and libzmq >= 4.1.6")
            log.warning(
                "Specific source IP / port for connecting to master returner port:"
                " configuraion ignored"
            )

    return master_uri


class PublishClient(salt.transport.base.PublishClient):
    """
    A transport channel backed by ZeroMQ for a Salt Publisher to use to
    publish commands to connected minions
    """

    ttype = "zeromq"

    async_methods = [
        "connect",
        "connect_uri",
        "recv",
        # "close",
    ]
    close_methods = [
        "close",
    ]

    def _legacy_setup(
        self,
        _id,
        role,
        zmq_filtering=False,
        tcp_keepalive=True,
        tcp_keepalive_idle=300,
        tcp_keepalive_cnt=-1,
        tcp_keepalive_intvl=-1,
        recon_default=1000,
        recon_max=10000,
        recon_randomize=True,
        ipv6=None,
        master_ip="127.0.0.1",
        zmq_monitor=False,
        **extras,
    ):
        self.hexid = hashlib.sha1(salt.utils.stringutils.to_bytes(_id)).hexdigest()
        self._closing = False
        self.context = zmq.asyncio.Context()
        self._socket = self.context.socket(zmq.SUB)
        self._socket.setsockopt(zmq.LINGER, -1)
        if zmq_filtering:
            # TODO: constants file for "broadcast"
            self._socket.setsockopt(zmq.SUBSCRIBE, b"broadcast")
            if role == "syndic":
                self._socket.setsockopt(zmq.SUBSCRIBE, b"syndic")
            else:
                self._socket.setsockopt(
                    zmq.SUBSCRIBE, salt.utils.stringutils.to_bytes(self.hexid)
                )
        else:
            self._socket.setsockopt(zmq.SUBSCRIBE, b"")

        if _id:
            self._socket.setsockopt(zmq.IDENTITY, salt.utils.stringutils.to_bytes(_id))

        # TODO: cleanup all the socket opts stuff
        if hasattr(zmq, "TCP_KEEPALIVE"):
            self._socket.setsockopt(zmq.TCP_KEEPALIVE, tcp_keepalive)
            self._socket.setsockopt(zmq.TCP_KEEPALIVE_IDLE, tcp_keepalive_idle)
            self._socket.setsockopt(zmq.TCP_KEEPALIVE_CNT, tcp_keepalive_cnt)
            self._socket.setsockopt(zmq.TCP_KEEPALIVE_INTVL, tcp_keepalive_intvl)

        if recon_randomize:
            recon_delay = randint(
                recon_default,
                recon_default + recon_max,
            )

            log.debug(
                "Generated random reconnect delay between '%sms' and '%sms' (%s)",
                recon_delay,
                recon_delay + recon_max,
                recon_delay,
            )

            log.debug("Setting zmq_reconnect_ivl to '%sms'", recon_delay)
            self._socket.setsockopt(zmq.RECONNECT_IVL, recon_delay)

            if hasattr(zmq, "RECONNECT_IVL_MAX"):
                log.debug(
                    "Setting zmq_reconnect_ivl_max to '%sms'",
                    recon_delay + recon_max,
                )

                self._socket.setsockopt(zmq.RECONNECT_IVL_MAX, recon_max)

        if (ipv6 is True or ":" in master_ip) and hasattr(zmq, "IPV4ONLY"):
            # IPv6 sockets work for both IPv6 and IPv4 addresses
            self._socket.setsockopt(zmq.IPV4ONLY, 0)

        self.poller = zmq.Poller()
        self.poller.register(self._socket, zmq.POLLIN)

        if HAS_ZMQ_MONITOR and zmq_monitor:
            self._monitor = ZeroMQSocketMonitor(self._socket)
            self._monitor.start_io_loop(self.io_loop)

    def __init__(self, opts, io_loop, **kwargs):
        super().__init__(opts, io_loop, **kwargs)
        self.opts = opts
        self.io_loop = io_loop
        self._legacy_setup(
            _id=opts.get("id", ""),
            role=opts.get("__role", ""),
            **opts,
        )
        self.connect_called = False
        self.callbacks = {}

        self.host = kwargs.get("host", None)
        self.port = kwargs.get("port", None)
        self.path = kwargs.get("path", None)
        self.source_ip = self.opts.get("source_ip")
        self.source_port = self.opts.get("source_publish_port")
        if self.host is None and self.port is None:
            if self.path is None:
                raise Exception("A host and port or a path must be provided")
        elif self.host and self.port:
            if self.path:
                raise Exception("A host and port or a path must be provided, not both")
        self.on_recv_task = None

    def close(self):
        if self._closing is True:
            return
        self._closing = True
        if hasattr(self, "_monitor") and self._monitor is not None:
            self._monitor.stop()
            self._monitor = None
        if hasattr(self, "_stream"):
            self._stream.close(0)
        elif hasattr(self, "_socket"):
            self._socket.close(0)
        if hasattr(self, "context") and self.context.closed is False:
            self.context.term()
        callbacks = self.callbacks
        self.callbacks = {}
        for callback, (running, task) in callbacks.items():
            running.clear()
        return

    # pylint: enable=W1701
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # TODO: this is the time to see if we are connected, maybe use the req channel to guess?
    async def connect(
        self, port=None, connect_callback=None, disconnect_callback=None, timeout=None
    ):
        self._connect_called = True
        if port is not None:
            self.port = port
        if self.path:
            pub_uri = f"ipc://{self.path}"
            log.debug("Connecting the publisher client to: %s", pub_uri)
            self._socket.connect(pub_uri)
        else:
            # host = self.opts["master_ip"],
            if port is not None:
                self.port = port
            master_pub_uri = _get_master_uri(
                self.host, self.port, self.source_ip, self.source_port
            )
            log.debug(
                "Connecting the Minion to the Master publish port, using the URI: %s",
                master_pub_uri,
            )
            self._socket.connect(master_pub_uri)
        if connect_callback:
            await connect_callback(True)

    async def connect_uri(self, uri, connect_callback=None, disconnect_callback=None):
        self._connect_called = True
        log.debug("Connecting the publisher client to: %s", uri)
        # log.debug("%r connecting to %s", self, self.master_pub)
        self.uri = uri
        self._socket.connect(uri)
        if connect_callback:
            await connect_callback(True)

    def _decode_messages(self, messages):
        """
        Take the zmq messages, decrypt/decode them into a payload

        :param list messages: A list of messages to be decoded
        """
        if isinstance(messages, list):
            messages_len = len(messages)
            # if it was one message, then its old style
            if messages_len == 1:
                payload = salt.payload.loads(messages[0])
            # 2 includes a header which says who should do it
            elif messages_len == 2:
                message_target = salt.utils.stringutils.to_str(messages[0])
                if (
                    self.opts.get("__role") != "syndic"
                    and message_target not in ("broadcast", self.hexid)
                ) or (
                    self.opts.get("__role") == "syndic"
                    and message_target not in ("broadcast", "syndic")
                ):
                    log.debug(
                        "Publish received for not this minion: %s", message_target
                    )
                    return None
                payload = salt.payload.loads(messages[1])
            else:
                raise Exception(
                    "Invalid number of messages ({}) in zeromq pubmessage from master".format(
                        len(messages_len)
                    )
                )
        else:
            payload = salt.payload.loads(messages)
        # Yield control back to the caller. When the payload has been decoded, assign
        # the decoded payload to 'ret' and resume operation
        return payload

    async def recv(self, timeout=None):
        if timeout == 0:
            events = self.poller.poll(timeout=timeout)
            if events:
                return await self._socket.recv()
        elif timeout:
            try:
                return await asyncio.wait_for(self._socket.recv(), timeout=timeout)
            except asyncio.exceptions.TimeoutError:
                log.trace("PublishClient recieve timedout: %d", timeout)
        else:
            return await self._socket.recv()

    async def send(self, msg):
        return
        # raise Exception("Send not supported")
        # await self._socket.send(msg)

    # async def on_recv_handler(self, callback):
    #    while not self._socket:
    #        # Retry quickly, we may want to increase this if it's hogging cpu.
    #        await asyncio.sleep(0.003)
    #    while True:
    #        msg = await self.recv()
    #        if msg:
    #            await callback(msg)

    # def on_recv(self, callback):
    #    """
    #    Register a callback for received messages (that we didn't initiate)
    #    """
    #    if self.on_recv_task:
    #        # XXX: We are not awaiting this canceled task. This still needs to
    #        # be addressed.
    #        self.on_recv_task.cancel()
    #    if callback is None:
    #        self.on_recv_task = None
    #    else:
    #        self.on_recv_task = asyncio.create_task(self.on_recv_handler(callback))

    def on_recv(self, callback):
        """
        Register a callback for received messages (that we didn't initiate)

        :param func callback: A function which should be called when data is received
        """
        if callback is None:
            callbacks = self.callbacks
            self.callbacks = {}
            for callback, (running, task) in callbacks.items():
                running.clear()
            return

        running = asyncio.Event()
        running.set()

        async def consume(running):
            try:
                while running.is_set():
                    try:
                        msg = await self.recv(timeout=None)
                    except zmq.error.ZMQError as exc:
                        # We've disconnected just die
                        break
                    if msg:
                        try:
                            await callback(msg)
                        except Exception:  # pylint: disable=broad-except
                            log.error("Exception while running callback", exc_info=True)
                    # log.debug("Callback done %r", callback)
            except Exception as exc:  # pylint: disable=broad-except
                log.error(
                    "Exception while consuming%s %s", self.uri, exc, exc_info=True
                )

        task = self.io_loop.spawn_callback(consume, running)
        self.callbacks[callback] = running, task


class RequestServer(salt.transport.base.DaemonizedRequestServer):
    def __init__(self, opts):  # pylint: disable=W0231
        self.opts = opts
        self._closing = False
        self._monitor = None
        self._w_monitor = None
        self.tasks = set()
        self._event = asyncio.Event()

    def zmq_device(self):
        """
        Multiprocessing target for the zmq queue device
        """
        self.__setup_signals()
        context = zmq.Context(self.opts["worker_threads"])
        # Prepare the zeromq sockets
        self.uri = "tcp://{interface}:{ret_port}".format(**self.opts)
        self.clients = context.socket(zmq.ROUTER)
        self.clients.setsockopt(zmq.LINGER, -1)
        if self.opts["ipv6"] is True and hasattr(zmq, "IPV4ONLY"):
            # IPv6 sockets work for both IPv6 and IPv4 addresses
            self.clients.setsockopt(zmq.IPV4ONLY, 0)
        self.clients.setsockopt(zmq.BACKLOG, self.opts.get("zmq_backlog", 1000))
        self._start_zmq_monitor()
        self.workers = context.socket(zmq.DEALER)
        self.workers.setsockopt(zmq.LINGER, -1)

        if self.opts["mworker_queue_niceness"] and not salt.utils.platform.is_windows():
            log.info(
                "setting mworker_queue niceness to %d",
                self.opts["mworker_queue_niceness"],
            )
            os.nice(self.opts["mworker_queue_niceness"])

        if self.opts.get("ipc_mode", "") == "tcp":
            self.w_uri = "tcp://127.0.0.1:{}".format(
                self.opts.get("tcp_master_workers", 4515)
            )
        else:
            self.w_uri = "ipc://{}".format(
                os.path.join(self.opts["sock_dir"], "workers.ipc")
            )

        log.info("Setting up the master communication server")
        log.info("ReqServer clients %s", self.uri)
        self.clients.bind(self.uri)
        log.info("ReqServer workers %s", self.w_uri)
        self.workers.bind(self.w_uri)
        if self.opts.get("ipc_mode", "") != "tcp":
            os.chmod(os.path.join(self.opts["sock_dir"], "workers.ipc"), 0o600)

        while True:
            if self.clients.closed or self.workers.closed:
                break
            try:
                zmq.device(zmq.QUEUE, self.clients, self.workers)
            except zmq.ZMQError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise
            except (KeyboardInterrupt, SystemExit):
                break
        context.term()

    def close(self):
        """
        Cleanly shutdown the router socket
        """
        if self._closing:
            return
        log.info("MWorkerQueue under PID %s is closing", os.getpid())
        self._closing = True
        self._event.set()
        if getattr(self, "_monitor", None) is not None:
            self._monitor.stop()
            self._monitor = None
        if getattr(self, "_w_monitor", None) is not None:
            self._w_monitor.stop()
            self._w_monitor = None
        if hasattr(self, "clients") and self.clients.closed is False:
            self.clients.close()
        if hasattr(self, "workers") and self.workers.closed is False:
            self.workers.close()
        if hasattr(self, "stream"):
            self.stream.close()
        if hasattr(self, "_socket") and self._socket.closed is False:
            self._socket.close()
        if hasattr(self, "context") and self.context.closed is False:
            self.context.term()
        for task in list(self.tasks):
            try:
                task.cancel()
            except RuntimeError:
                log.error("IOLoop closed when trying to cancel task")

    def pre_fork(self, process_manager):
        """
        Pre-fork we need to create the zmq router device

        :param func process_manager: An instance of salt.utils.process.ProcessManager
        """
        process_manager.add_process(self.zmq_device, name="MWorkerQueue")

    def _start_zmq_monitor(self):
        """
        Starts ZMQ monitor for debugging purposes.
        :return:
        """
        # Socket monitor shall be used the only for debug
        # purposes so using threading doesn't look too bad here

        if HAS_ZMQ_MONITOR and self.opts["zmq_monitor"]:
            log.debug("Starting ZMQ monitor")
            self._w_monitor = ZeroMQSocketMonitor(self._socket)
            threading.Thread(target=self._w_monitor.start_poll).start()
            log.debug("ZMQ monitor has been started started")

    def post_fork(self, message_handler, io_loop):
        """
        After forking we need to create all of the local sockets to listen to the
        router

        :param func message_handler: A function to called to handle incoming payloads as
                                     they are picked up off the wire
        :param IOLoop io_loop: An instance of a Tornado IOLoop, to handle event scheduling
        """
        # context = zmq.Context(1)
        self.context = zmq.asyncio.Context(1)
        self._socket = self.context.socket(zmq.REP)
        # Linger -1 means we'll never discard messages.
        self._socket.setsockopt(zmq.LINGER, -1)
        self._start_zmq_monitor()

        if self.opts.get("ipc_mode", "") == "tcp":
            self.w_uri = "tcp://127.0.0.1:{}".format(
                self.opts.get("tcp_master_workers", 4515)
            )
        else:
            self.w_uri = "ipc://{}".format(
                os.path.join(self.opts["sock_dir"], "workers.ipc")
            )
        log.info("Worker binding to socket %s", self.w_uri)
        self._socket.connect(self.w_uri)
        if self.opts.get("ipc_mode", "") != "tcp" and os.path.isfile(
            os.path.join(self.opts["sock_dir"], "workers.ipc")
        ):
            os.chmod(os.path.join(self.opts["sock_dir"], "workers.ipc"), 0o600)
        self.message_handler = message_handler

        async def callback():
            task = asyncio.create_task(self.request_handler())
            task.add_done_callback(self.tasks.discard)
            self.tasks.add(task)

        io_loop.add_callback(callback)

    async def request_handler(self):
        while not self._event.is_set():
            try:
                request = await asyncio.wait_for(self._socket.recv(), 0.3)
                reply = await self.handle_message(None, request)
                await self._socket.send(self.encode_payload(reply))
            except zmq.error.Again:
                continue
            except asyncio.exceptions.TimeoutError:
                continue
            except Exception as exc:  # pylint: disable=broad-except
                log.error(
                    "Exception in request handler",
                    exc_info_on_loglevel=logging.DEBUG,
                )
                continue

    async def handle_message(self, stream, payload):
        try:
            payload = self.decode_payload(payload)
        except salt.exceptions.SaltDeserializationError:
            return {"msg": "bad load"}
        return await self.message_handler(payload)

    def encode_payload(self, payload):
        return salt.payload.dumps(payload)

    def __setup_signals(self):
        signal.signal(signal.SIGINT, self._handle_signals)
        signal.signal(signal.SIGTERM, self._handle_signals)

    def _handle_signals(self, signum, sigframe):
        msg = f"{self.__class__.__name__} received a "
        if signum == signal.SIGINT:
            msg += "SIGINT"
        elif signum == signal.SIGTERM:
            msg += "SIGTERM"
        msg += ". Exiting"
        log.debug(msg)
        self.close()
        sys.exit(salt.defaults.exitcodes.EX_OK)

    def decode_payload(self, payload):
        payload = salt.payload.loads(payload)
        return payload


def _set_tcp_keepalive(zmq_socket, opts):
    """
    Ensure that TCP keepalives are set as specified in "opts".

    Warning: Failure to set TCP keepalives on the salt-master can result in
    not detecting the loss of a minion when the connection is lost or when
    its host has been terminated without first closing the socket.
    Salt's Presence System depends on this connection status to know if a minion
    is "present".

    Warning: Failure to set TCP keepalives on minions can result in frequent or
    unexpected disconnects!
    """
    if hasattr(zmq, "TCP_KEEPALIVE") and opts:
        if "tcp_keepalive" in opts:
            zmq_socket.setsockopt(zmq.TCP_KEEPALIVE, opts["tcp_keepalive"])
        if "tcp_keepalive_idle" in opts:
            zmq_socket.setsockopt(zmq.TCP_KEEPALIVE_IDLE, opts["tcp_keepalive_idle"])
        if "tcp_keepalive_cnt" in opts:
            zmq_socket.setsockopt(zmq.TCP_KEEPALIVE_CNT, opts["tcp_keepalive_cnt"])
        if "tcp_keepalive_intvl" in opts:
            zmq_socket.setsockopt(zmq.TCP_KEEPALIVE_INTVL, opts["tcp_keepalive_intvl"])


class AsyncReqMessageClient:
    """
    This class wraps the underlying zeromq REQ socket and gives a future-based
    interface to sending and recieving messages. This works around the primary
    limitation of serialized send/recv on the underlying socket by queueing the
    message sends in this class. In the future if we decide to attempt to multiplex
    we can manage a pool of REQ/REP sockets-- but for now we'll just do them in serial
    """

    def __init__(self, opts, addr, linger=0, io_loop=None):
        """
        Create an asynchronous message client

        :param dict opts: The salt opts dictionary
        :param str addr: The interface IP address to bind to
        :param int linger: The number of seconds to linger on a ZMQ socket. See
                           http://api.zeromq.org/2-1:zmq-setsockopt [ZMQ_LINGER]
        :param IOLoop io_loop: A Tornado IOLoop event scheduler [tornado.ioloop.IOLoop]
        """
        salt.utils.versions.warn_until(
            3009,
            "AsyncReqMessageClient has been deprecated and will be removed.",
        )
        self.opts = opts
        self.addr = addr
        self.linger = linger
        if io_loop is None:
            self.io_loop = tornado.ioloop.IOLoop.current()
        else:
            self.io_loop = io_loop

        self.context = zmq.eventloop.future.Context()

        self.send_queue = []

        self._closing = False
        self.lock = tornado.locks.Lock()

    def connect(self):
        if hasattr(self, "socket") and self.socket:
            return
        # wire up sockets
        self._init_socket()

    def close(self):
        if self._closing:
            return
        else:
            self._closing = True
            if hasattr(self, "socket") and self.socket is not None:
                self.socket.close(0)
                self.socket = None
            if self.context.closed is False:
                self.context.term()

    def _init_socket(self):
        self.socket = self.context.socket(zmq.REQ)

        # socket options
        if hasattr(zmq, "RECONNECT_IVL_MAX"):
            self.socket.setsockopt(zmq.RECONNECT_IVL_MAX, 5000)

        _set_tcp_keepalive(self.socket, self.opts)
        if self.addr.startswith("tcp://["):
            # Hint PF type if bracket enclosed IPv6 address
            if hasattr(zmq, "IPV6"):
                self.socket.setsockopt(zmq.IPV6, 1)
            elif hasattr(zmq, "IPV4ONLY"):
                self.socket.setsockopt(zmq.IPV4ONLY, 0)
        self.socket.setsockopt(zmq.LINGER, self.linger)
        self.socket.connect(self.addr)

    @tornado.gen.coroutine
    def send(self, message, timeout=None, callback=None):
        """
        Return a future which will be completed when the message has a response
        """
        future = tornado.concurrent.Future()

        message = salt.payload.dumps(message)

        if callback is not None:

            def handle_future(future):
                response = future.result()
                self.io_loop.add_callback(callback, response)

            future.add_done_callback(handle_future)

        if self.opts.get("detect_mode") is True:
            timeout = 1

        if timeout is not None:
            send_timeout = self.io_loop.call_later(
                timeout, self._timeout_message, future
            )

        self.io_loop.spawn_callback(self._send_recv, message, future)

        recv = yield future

        raise tornado.gen.Return(recv)

    def _timeout_message(self, future):
        if not future.done():
            future.set_exception(SaltReqTimeoutError("Message timed out"))

    @tornado.gen.coroutine
    def _send_recv(self, message, future):
        try:
            with (yield self.lock.acquire()):
                yield self.socket.send(message)
                try:
                    recv = yield self.socket.recv()
                except zmq.eventloop.future.CancelledError as exc:
                    if not future.done():
                        future.set_exception(exc)
                    return

            if not future.done():
                data = salt.payload.loads(recv)
                future.set_result(data)
        except Exception as exc:  # pylint: disable=broad-except
            if not future.done():
                future.set_exception(exc)


class ZeroMQSocketMonitor:
    __EVENT_MAP = None

    def __init__(self, socket):
        """
        Create ZMQ monitor sockets

        More information:
            http://api.zeromq.org/4-0:zmq-socket-monitor
        """
        self._socket = socket
        self._monitor_socket = self._socket.get_monitor_socket()
        self._monitor_task = None
        self._running = asyncio.Event()

    def start_io_loop(self, io_loop):
        log.trace("Event monitor start!")
        self._running.set()
        io_loop.spawn_callback(self.consume)

    async def consume(self):
        while self._running.is_set():
            try:
                if await self._monitor_socket.poll():
                    msg = await self._monitor_socket.recv_multipart()
                    self.monitor_callback(msg)
                else:
                    await asyncio.sleep(0.3)
            except zmq.error.ZMQError as exc:
                log.error("ZmqMonitor, %s", exc)
                # We've disconnected just die
                break
            except Exception as exc:  # pylint: disable=broad-except
                log.error("ZmqMonitor, %s", exc)
                break

    def start_poll(self):
        log.trace("Event monitor start!")
        try:
            while self._monitor_socket is not None and self._monitor_socket.poll():
                msg = self._monitor_socket.recv_multipart()
                self.monitor_callback(msg)
        except (AttributeError, zmq.error.ContextTerminated):
            # We cannot log here because we'll get an interrupted system call in trying
            # to flush the logging buffer as we terminate
            pass

    @property
    def event_map(self):
        if ZeroMQSocketMonitor.__EVENT_MAP is None:
            event_map = {}
            for name in dir(zmq):
                if name.startswith("EVENT_"):
                    value = getattr(zmq, name)
                    event_map[value] = name
            ZeroMQSocketMonitor.__EVENT_MAP = event_map
        return ZeroMQSocketMonitor.__EVENT_MAP

    def monitor_callback(self, msg):
        evt = zmq.utils.monitor.parse_monitor_message(msg)
        evt["description"] = self.event_map[evt["event"]]
        log.debug("ZeroMQ event: %s", evt)
        if evt["event"] == zmq.EVENT_MONITOR_STOPPED:
            self.stop()

    def stop(self):
        if self._socket is None:
            return
        try:
            self._socket.disable_monitor()
        except zmq.Error:
            pass
        self._socket = None
        self._running.clear()
        self._monitor_socket = None
        log.trace("Event monitor done!")


class PublishServer(salt.transport.base.DaemonizedPublishServer):
    """
    Encapsulate synchronous operations for a publisher channel
    """

    async_methods = [
        "publish",
    ]
    close_methods = [
        "close",
    ]

    def __init__(
        self,
        opts,
        pub_host=None,
        pub_port=None,
        pub_path=None,
        pull_host=None,
        pull_port=None,
        pull_path=None,
        pull_path_perms=0o600,
        pub_path_perms=0o600,
        started=None,
    ):
        self.opts = opts
        self.pub_host = pub_host
        self.pub_port = pub_port
        self.pub_path = pub_path
        if pub_path:
            self.pub_uri = f"ipc://{pub_path}"
        else:
            self.pub_uri = f"tcp://{pub_host}:{pub_port}"
        self.pull_host = pull_host
        self.pull_port = pull_port
        self.pull_path = pull_path
        self.pub_path_perms = pub_path_perms
        self.pull_path_perms = pull_path_perms
        if pull_path:
            self.pull_uri = f"ipc://{pull_path}"
        else:
            self.pull_uri = f"tcp://{pull_host}:{pull_port}"
        self.ctx = None
        self.sock = None
        self.daemon_context = None
        self.daemon_pub_sock = None
        self.daemon_pull_sock = None
        self.daemon_monitor = None
        if started is None:
            self.started = multiprocessing.Event()
        else:
            self.started = started

    @classmethod
    def support_ssl(cls):
        # Required from DaemonizedPublishServer
        return False

    def topic_support(self):
        # Required from DaemonizedPublishServer
        return self.opts.get("zmq_filtering", False)

    def __repr__(self):
        return f"<PublishServer pub_uri={self.pub_uri} pull_uri={self.pull_uri} at {hex(id(self))}>"

    def __setstate__(self, state):
        self.__init__(**state)

    def __getstate__(self):
        return {
            "opts": self.opts,
            "pub_host": self.pub_host,
            "pub_port": self.pub_port,
            "pub_path": self.pub_path,
            "pull_host": self.pull_host,
            "pull_port": self.pull_port,
            "pull_path": self.pull_path,
            "pub_path_perms": self.pub_path_perms,
            "pull_path_perms": self.pull_path_perms,
            "started": self.started,
        }

    def publish_daemon(
        self,
        publish_payload,
        presence_callback=None,
        remove_presence_callback=None,
    ):
        """
        This method represents the Publish Daemon process. It is intended to be
        run in a thread or process as it creates and runs its own ioloop.
        """
        io_loop = tornado.ioloop.IOLoop()
        io_loop.add_callback(self.publisher, publish_payload, io_loop=io_loop)
        try:
            io_loop.start()
        finally:
            self.close()

    def _get_sockets(self, context, io_loop):
        pub_sock = context.socket(zmq.PUB)
        monitor = ZeroMQSocketMonitor(pub_sock)
        monitor.start_io_loop(io_loop)
        _set_tcp_keepalive(pub_sock, self.opts)
        self.dpub_sock = pub_sock  # = zmq.eventloop.zmqstream.ZMQStream(pub_sock)
        # if 2.1 >= zmq < 3.0, we only have one HWM setting
        try:
            pub_sock.setsockopt(zmq.HWM, self.opts.get("pub_hwm", 1000))
        # in zmq >= 3.0, there are separate send and receive HWM settings
        except (AttributeError, zmq.error.ZMQError):
            # Set the High Water Marks. For more information on HWM, see:
            # http://api.zeromq.org/4-1:zmq-setsockopt
            pub_sock.setsockopt(zmq.SNDHWM, self.opts.get("pub_hwm", 1000))
            pub_sock.setsockopt(zmq.RCVHWM, self.opts.get("pub_hwm", 1000))
        if self.opts["ipv6"] is True and hasattr(zmq, "IPV4ONLY"):
            # IPv6 sockets work for both IPv6 and IPv4 addresses
            pub_sock.setsockopt(zmq.IPV4ONLY, 0)

        pub_sock.setsockopt(zmq.BACKLOG, self.opts.get("zmq_backlog", 1000))
        pub_sock.setsockopt(zmq.LINGER, -1)
        # Prepare minion pull socket
        pull_sock = context.socket(zmq.PULL)
        pull_sock.setsockopt(zmq.LINGER, -1)
        # pull_sock = zmq.eventloop.zmqstream.ZMQStream(pull_sock)
        pull_sock.setsockopt(zmq.LINGER, -1)
        salt.utils.zeromq.check_ipc_path_max_len(self.pull_uri)
        # Start the minion command publisher
        # Securely create socket
        with salt.utils.files.set_umask(0o177):
            log.info("Starting the Salt Publisher on %s", self.pub_uri)
            pub_sock.bind(self.pub_uri)
            if self.pub_path:
                os.chmod(  # nosec
                    self.pub_path,
                    self.pub_path_perms,
                )
            log.info("Starting the Salt Puller on %s", self.pull_uri)
            pull_sock.bind(self.pull_uri)
            if self.pull_path:
                os.chmod(  # nosec
                    self.pull_path,
                    self.pull_path_perms,
                )
        return pull_sock, pub_sock, monitor

    async def publisher(
        self,
        publish_payload,
        presence_callback=None,
        remove_presence_callback=None,
        io_loop=None,
    ):
        if io_loop is None:
            io_loop = tornado.ioloop.IOLoop.current()
        self.daemon_context = zmq.asyncio.Context()
        (
            self.daemon_pull_sock,
            self.daemon_pub_sock,
            self.daemon_monitor,
        ) = self._get_sockets(self.daemon_context, io_loop)
        self.started.set()
        while True:
            try:
                package = await self.daemon_pull_sock.recv()
                await publish_payload(package)
            except Exception as exc:  # pylint: disable=broad-except
                log.error(
                    "Exception in publisher %s %s",
                    self.pull_uri,
                    exc,
                    exc_info_on_loglevel=logging.DEBUG,
                )

    async def publish_payload(self, payload, topic_list=None):
        log.trace("Publish payload %r", payload)
        if self.opts["zmq_filtering"]:
            if topic_list:
                for topic in topic_list:
                    log.trace("Sending filtered data over publisher %s", self.pub_uri)
                    # zmq filters are substring match, hash the topic
                    # to avoid collisions
                    htopic = salt.utils.stringutils.to_bytes(
                        hashlib.sha1(salt.utils.stringutils.to_bytes(topic)).hexdigest()
                    )
                    await self.dpub_sock.send_multipart([htopic, payload])
                    log.trace("Filtered data has been sent")
                # Syndic broadcast
                if self.opts.get("order_masters"):
                    log.trace("Sending filtered data to syndic")
                    await self.dpub_sock.send_multipart([b"syndic", payload])
                    log.trace("Filtered data has been sent to syndic")
            # otherwise its a broadcast
            else:
                # TODO: constants file for "broadcast"
                log.trace("Sending broadcasted data over publisher %s", self.pub_uri)
                await self.dpub_sock.send_multipart([b"broadcast", payload])
                log.trace("Broadcasted data has been sent")
        else:
            log.trace("Sending ZMQ-unfiltered data over publisher %s", self.pub_uri)
            await self.dpub_sock.send(payload)
            log.trace("Unfiltered data has been sent")

    def pre_fork(self, process_manager):
        """
        Do anything necessary pre-fork. Since this is on the master side this will
        primarily be used to create IPC channels and create our daemon process to
        do the actual publishing

        :param func process_manager: A ProcessManager, from salt.utils.process.ProcessManager
        """
        process_manager.add_process(
            self.publish_daemon,
            args=(self.publish_payload,),
        )

    def connect(self, timeout=None):
        """
        Create and connect this thread's zmq socket. If a publisher socket
        already exists "pub_close" is called before creating and connecting a
        new socket.
        """
        log.debug("Connecting to pub server: %s", self.pull_uri)
        self.ctx = zmq.asyncio.Context()
        self.sock = self.ctx.socket(zmq.PUSH)
        self.sock.setsockopt(zmq.LINGER, -1)
        self.sock.connect(self.pull_uri)
        return self.sock

    def close(self):
        """
        Disconnect an existing publisher socket and remove it from the local
        thread's cache.
        """
        if self.sock is not None:
            sock = self.sock
            self.sock = None
            sock.close()
        if self.ctx and self.ctx.closed is False:
            ctx = self.ctx
            self.ctx = None
            ctx.term()
        if self.daemon_monitor:
            self.daemon_monitor.stop()
        if self.daemon_pub_sock:
            self.daemon_pub_sock.close()
        if self.daemon_pull_sock:
            self.daemon_pull_sock.close()
        if self.daemon_context:
            self.daemon_context.destroy(1)
            self.daemon_context.term()

    async def publish(
        self, payload, **kwargs
    ):  # pylint: disable=invalid-overridden-method
        """
        Publish "load" to minions. This send the load to the publisher daemon
        process with does the actual sending to minions.

        :param dict load: A load to be sent across the wire to minions
        """
        if not self.sock:
            self.connect()
        await self.sock.send(payload)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class RequestClient(salt.transport.base.RequestClient):

    ttype = "zeromq"

    def __init__(self, opts, io_loop, linger=0):  # pylint: disable=W0231
        super().__init__(opts, io_loop)
        self.opts = opts
        # XXX Support host, port, path, instead of using get_master_uri
        self.master_uri = self.get_master_uri(opts)
        self.linger = linger
        if io_loop is None:
            self.io_loop = tornado.ioloop.IOLoop.current()
        else:
            self.io_loop = io_loop
        self.context = None
        self.send_queue = []
        # mapping of message -> future
        self.send_future_map = {}
        self._closing = False
        self.socket = None
        self.sending = asyncio.Lock()

    async def connect(self):  # pylint: disable=invalid-overridden-method
        if self.socket is None:
            self._connect_called = True
            self._closing = False
            # wire up sockets
            self._init_socket()

    def _init_socket(self):
        if self.socket is not None:
            self.context = zmq.asyncio.Context()
            self.socket.close()  # pylint: disable=E0203
            del self.socket
        self.context = zmq.asyncio.Context()
        self.socket = self.context.socket(zmq.REQ)
        self.socket.setsockopt(zmq.LINGER, -1)

        # socket options
        if hasattr(zmq, "RECONNECT_IVL_MAX"):
            self.socket.setsockopt(zmq.RECONNECT_IVL_MAX, 5000)

        _set_tcp_keepalive(self.socket, self.opts)
        if self.master_uri.startswith("tcp://["):
            # Hint PF type if bracket enclosed IPv6 address
            if hasattr(zmq, "IPV6"):
                self.socket.setsockopt(zmq.IPV6, 1)
            elif hasattr(zmq, "IPV4ONLY"):
                self.socket.setsockopt(zmq.IPV4ONLY, 0)
        self.socket.linger = self.linger
        self.socket.connect(self.master_uri)

    # TODO: timeout all in-flight sessions, or error
    def close(self):
        if self._closing:
            return
        self._closing = True
        if self.socket:
            self.socket.close()
            self.socket = None
        if self.context and self.context.closed is False:
            # This hangs if closing the stream causes an import error
            self.context.term()
            self.context = None

    async def _send_recv(self, message):
        message = salt.payload.dumps(message)
        async with self.sending:
            try:
                await self.socket.send(message)
                ret = await self.socket.recv()
            except zmq.error.ZMQError:
                self.close()
                await self.connect()
                await self.socket.send(message)
                ret = await self.socket.recv()
        return salt.payload.loads(ret)

    async def send(self, load, timeout=60):
        """
        Return a future which will be completed when the message has a response
        """
        if not self.socket:
            await self.connect()
        try:
            return await asyncio.wait_for(self._send_recv(load), timeout=timeout)
        except (asyncio.exceptions.TimeoutError, TimeoutError):
            self.close()
            raise SaltReqTimeoutError("Request client send timedout")
        except Exception:
            self.close()
            raise

    @staticmethod
    def get_master_uri(opts):
        if "master_uri" in opts:
            return opts["master_uri"]
        if "master_ip" in opts:
            return _get_master_uri(
                opts["master_ip"],
                opts["master_port"],
                source_ip=opts.get("source_ip"),
                source_port=opts.get("source_ret_port"),
            )
        # if we've reached here something is very abnormal
        raise SaltException("ReqChannel: missing master_uri/master_ip in self.opts")
