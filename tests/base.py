import collections
import logging
import os
import re
import unittest
import uuid

from tornado import gen, testing, web

import sprockets_influxdb as influxdb

LOGGER = logging.getLogger(__name__)

LINE_PATTERN = re.compile(r'^([\w\-\\, ]+)(?<!\\),([\\\w=\-_.,/"\(\)<>?\+]+)(?'
                          r'<!\\) ([\w=\-/\\_.,;" ]+)(?<!\\) (\d+)$')
TAG_PATTERN = re.compile(r'(?P<name>(?:(?:\\ )|(?:\\,)|(?:[\w\-_.]+))+)=(?P<va'
                         r'lue>(?:(?:\\ )|(?:\\,)|(?:[\\\w\-_./\(\)<>?\+]+))+)')
FIELD_PATTERN = re.compile(r'(?P<key>(?:(?:\\ )|(?:\\,)|(?:\\")|(?:[\w\-_./]+)'
                           r')+)=(?P<value>(?:(?:(?:(?:\\ )|(?:\\,)|(?:\\")|(?'
                           r':[\w\-_./\;]+))+)|(?:"(?:(?:\\ )|(?:\\,)|(?:\\")|'
                           r'(?:[\w\-_./\;=]+))+")))')


Measurement = collections.namedtuple(
    'measurement', ['db', 'timestamp', 'name', 'tags', 'fields', 'headers'])

measurements = collections.deque()


def clear_influxdb_module():
    for variable in {'INFLUXDB_SCHEME', 'INFLUXDB_HOST', 'INFLUXDB_PORT',
                     'INFLUXDB_USER', 'INFLUXDB_PASSWORD'}:
        if variable in os.environ:
            del os.environ[variable]
    influxdb._base_tags = {}
    influxdb._base_url = 'http://localhost:8086/write'
    influxdb._credentials = None, None
    influxdb._dirty = False
    influxdb._http_client = None
    influxdb._installed = False
    influxdb._io_loop = None
    influxdb._last_warning = None
    influxdb._measurements = {}
    influxdb._max_batch_size = 5000
    influxdb._max_clients = 10
    influxdb._periodic_callback = None
    influxdb._periodic_future = None
    influxdb._stopping = False
    influxdb._warn_threshold = 5000
    influxdb._writing = False


def _strip_backslashes(line):
    for sequence in {'\\ ', '\\,', '\\"'}:
        line = line.replace(sequence, sequence[-1])
    return line


class TestCase(unittest.TestCase):

    def setUp(self):
        clear_influxdb_module()


class AsyncTestCase(testing.AsyncTestCase):

    def setUp(self):
        clear_influxdb_module()
        return super(AsyncTestCase, self).setUp()


class AsyncServerTestCase(testing.AsyncHTTPTestCase):

    def setUp(self):
        clear_influxdb_module()
        self.application = None
        super(AsyncServerTestCase, self).setUp()
        logging.getLogger(
            AsyncServerTestCase.__module__).setLevel(logging.DEBUG)
        influxdb.install(url=self.get_url('/write'))

    def fetch(self, path, **kwargs):
        result = super(AsyncServerTestCase, self).fetch(path, **kwargs)
        self.flush()
        return result

    def flush(self):
        """Ensure that all metrics that are buffered in
        sprockets.clients.influxdb are flushed to the server.

        """
        future = influxdb._on_periodic_callback()
        if future:
            self.io_loop.add_future(future, self.stop)
            self.wait()

    def get_app(self):
        if not self.application:
            settings = {influxdb.REQUEST_DATABASE: 'database-name',
                        'service': 'my-service'}
            self.application = web.Application(
                [
                    web.url('/', RequestHandler),
                    web.url('/named', NamedRequestHandler,
                            name='tests.base.NamedRequestHandler'),
                    web.url('/param/(?P<id>\d+)', ParamRequestHandler,
                            name='tests.base.ParamRequestHandler'),
                    web.url('/write', FakeInfluxDBHandler)
                ],
                **settings)
        return self.application

    @staticmethod
    def get_measurement():
        """Return metrics for a request as the Measurement namedtuple.

        :rtype: Measurement

        """
        try:
            return measurements.pop()
        except IndexError:
            return


class RequestHandler(influxdb.InfluxDBMixin,
                     web.RequestHandler):

    @gen.coroutine
    def get(self, *args, **kwargs):
        yield gen.sleep(0.01)
        self.write({'result': 'ok'})
        self.finish()


class NamedRequestHandler(RequestHandler):

    correlation_id = str(uuid.uuid4())


class ParamRequestHandler(RequestHandler):

    correlation_id = str(uuid.uuid4())

    @gen.coroutine
    def get(self, *args, **kwargs):
        yield gen.sleep(0.01)
        self.write({'id': kwargs['id']})
        self.finish()


class FakeInfluxDBHandler(web.RequestHandler):

    def post(self, *args, **kwargs):
        db = self.get_query_argument('db')
        payload = self.request.body.decode('utf-8')
        for line in payload.splitlines():
            LOGGER.debug('Line: %r', line)
            parts = LINE_PATTERN.match(line)
            name, tags_str, fields_str, timestamp = parts.groups()

            matches = TAG_PATTERN.findall(tags_str)
            tags = dict([(_strip_backslashes(k), _strip_backslashes(v))
                         for k, v in matches])

            matches = FIELD_PATTERN.findall(fields_str)
            fields = dict([(_strip_backslashes(k), _strip_backslashes(v))
                           for k, v in matches])
            for key, value in fields.items():
                if value[-1] == 'i' and value[:-1].isdigit():
                    fields[key] = int(value[:-1])
                elif value[0] == '"' and value[-1] == '"':
                    fields[key] = value[1:-1]
                elif value.lower() in {'t', 'true', 'f', 'false'}:
                    fields[key] = value.lower() in {'t', 'true'}
                else:
                    fields[key] = float(value)

            measurements.append(
                Measurement(db, int(timestamp), name, tags, fields,
                            self.request.headers))
        self.set_status(204)
