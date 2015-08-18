#!/usr/bin/env python3

import argparse
import logging
import asyncio
import atexit
import fnmatch
from functools import partial

import aiohttp

from artiq.tools import verbosity_args, init_logger
from artiq.tools import TaskObject
from artiq.protocols.sync_struct import Subscriber
from artiq.protocols.pc_rpc import Server
from artiq.protocols import pyon


logger = logging.getLogger(__name__)


def get_argparser():
    parser = argparse.ArgumentParser(
        description="ARTIQ data to InfluxDB bridge")
    group = parser.add_argument_group("master")
    group.add_argument(
        "--server-master", default="::1",
        help="hostname or IP of the master to connect to")
    group.add_argument(
        "--port-master", default=3250, type=int,
        help="TCP port to use to connect to the master")
    group.add_argument(
        "--retry-master", default=5.0, type=float,
        help="retry timer for reconnecting to master")
    group = parser.add_argument_group("database")
    group.add_argument(
        "--baseurl-db", default="http://localhost:8086",
        help="base URL to access InfluxDB (default: %(default)s)")
    group.add_argument(
        "--user-db", default="", help="InfluxDB username")
    group.add_argument(
        "--password-db", default="", help="InfluxDB password")
    group.add_argument(
        "--database", default="db", help="database name to use")
    group.add_argument(
        "--table", default="lab", help="table name to use")
    group = parser.add_argument_group("filter")
    group.add_argument(
        "--bind", default="::1",
        help="hostname or IP address to bind to")
    group.add_argument(
        "--bind-port", default=3248, type=int,
        help="TCP port to listen to for control (default: %(default)d)")
    group.add_argument(
        "--pattern-file", default="influxdb_patterns.pyon",
        help="file to save the patterns in (default: %(default)s)")
    verbosity_args(parser)
    return parser


class DBWriter(TaskObject):
    def __init__(self, base_url, user, password, database, table):
        self.base_url = base_url
        self.user = user
        self.password = password
        self.database = database
        self.table = table

        self._queue = asyncio.Queue(100)

    def update(self, k, v):
        try:
            self._queue.put_nowait((k, v))
        except asyncio.QueueFull:
            logger.warning("failed to update parameter '%s': "
                           "too many pending updates", k)

    @asyncio.coroutine
    def _do(self):
        while True:
            k, v = yield from self._queue.get()
            url = self.base_url + "/write"
            params = {"u": self.user, "p": self.password, "db": self.database,
                      "consistency": "any", "precision": "n"}
            data = "{} {}={}".format(self.table, k, v)
            try:
                response = yield from aiohttp.request(
                    "POST", url, params=params, data=data)
            except:
                logger.warning("got exception trying to update '%s'",
                               k, exc_info=True)
            else:
                if response.status not in (200, 204):
                    logger.warning("got HTTP status %d trying to update '%s'",
                                   response.status, k)
                response.close()


class Parameters:
    def __init__(self, filter_function, writer, init):
        self.filter_function = filter_function
        self.writer = writer

    def __setitem__(self, k, v):
        try:
            v = float(v)
        except:
            pass
        else:
            if self.filter_function(k):
                self.writer.update(k, v)


class MasterReader(TaskObject):
    def __init__(self, server, port, retry, filter_function, writer):
        self.server = server
        self.port = port
        self.retry = retry

        self.filter_function = filter_function
        self.writer = writer

    @asyncio.coroutine
    def _do(self):
        subscriber = Subscriber(
            "parameters",
            partial(Parameters, self.filter_function, self.writer))
        while True:
            try:
                yield from subscriber.connect(self.server, self.port)
                try:
                    yield from asyncio.wait_for(subscriber.receive_task, None)
                finally:
                    yield from subscriber.close()
            except (ConnectionAbortedError, ConnectionError,
                    ConnectionRefusedError, ConnectionResetError) as e:
                logger.warning("Connection to master failed (%s: %s)",
                    e.__class__.__name__, str(e))
            else:
                logger.warning("Connection to master lost")
            logger.warning("Retrying in %.1f seconds", self.retry)
            yield from asyncio.sleep(self.retry)


class Filter:
    def __init__(self, pattern_file):
        self.pattern_file = pattern_file
        self.patterns = []
        try:
            self.patterns = pyon.load_file(self.pattern_file)
        except FileNotFoundError:
            logger.info("no pattern file found, logging everything")

    def _save(self):
        pyon.store_file(self.pattern_file, self.patterns)

    # Privatize so that it is not shown in artiq_rpctool list-methods.
    def _filter(self, k):
        take = "+"
        for pattern in self.patterns:
            sign = "-"
            if pattern[0] in "+-":
                sign, pattern = pattern[0], pattern[1:]
            if fnmatch.fnmatchcase(k, pattern):
                take = sign
        return take == "+"

    def add_pattern(self, pattern, index=None):
        """Add a pattern.

        Optional + and - pattern prefixes specify whether to ignore or log
        keys matching the rest of the pattern.
        Default (in the absence of prefix) is to ignore. Keys that match no
        pattern are logged. Last matched pattern takes precedence.

        The optional index parameter specifies where to insert the pattern.
        By default, patterns are added at the end. If index is an integer, it
        specifies the index where the pattern is inserted. If it is a string,
        that string must match an existing pattern and the new pattern is
        inserted immediately after it."""
        if pattern not in self.patterns:
            if index is None:
                index = len(self.patterns)
            if isinstance(index, str):
                index = self.patterns.index(index) + 1
            self.patterns.insert(index, pattern)
        self._save()

    def remove_pattern(self, pattern):
        """Remove a pattern."""
        self.patterns.remove(pattern)
        self._save()

    def get_patterns(self):
        """Show existing patterns."""
        return self.patterns


def main():
    args = get_argparser().parse_args()
    init_logger(args)

    loop = asyncio.get_event_loop()
    atexit.register(lambda: loop.close())

    writer = DBWriter(args.baseurl_db,
                      args.user_db, args.password_db,
                      args.database, args.table)
    writer.start()
    atexit.register(lambda: loop.run_until_complete(writer.stop()))

    filter = Filter(args.pattern_file)
    rpc_server = Server({"influxdb_filter": filter}, builtin_terminate=True)
    loop.run_until_complete(rpc_server.start(args.bind, args.bind_port))
    atexit.register(lambda: loop.run_until_complete(rpc_server.stop()))

    reader = MasterReader(args.server_master, args.port_master,
                          args.retry_master, filter._filter, writer)
    reader.start()
    atexit.register(lambda: loop.run_until_complete(reader.stop()))

    loop.run_until_complete(rpc_server.wait_terminate())


if __name__ == "__main__":
    main()
