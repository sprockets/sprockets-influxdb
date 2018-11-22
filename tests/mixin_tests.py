import mock
import socket
import time
import unittest

import tornado

from . import base


def assert_between(low, value, high):
    if not (low <= value < high):
        raise AssertionError('Expected {} to be between {} and {}'.format(
            value, low, high))


class MeasurementTestCase(base.AsyncServerTestCase):

    def test_measurement_was_sent(self):
        start_time = time.time()
        result = self.fetch('/', headers={'Accept': 'application/json'})
        self.assertEqual(result.code, 200)
        measurement = self.get_measurement()
        self.assertIsNotNone(measurement)
        self.assertEqual(measurement.db, 'database-name')
        self.assertEqual(measurement.name, 'my-service')
        self.assertEqual(measurement.tags['status_code'], '200')
        self.assertEqual(measurement.tags['method'], 'GET')
        self.assertEqual(measurement.tags['handler'],
                         'tests.base.RequestHandler')
        self.assertEqual(measurement.tags['endpoint'], '/')
        self.assertEqual(measurement.tags['hostname'], socket.gethostname())

        self.assertEqual(measurement.fields['content_length'], 16)
        self.assertGreater(float(measurement.fields['duration']), 0.001)
        self.assertLess(float(measurement.fields['duration']), 0.1)

        self.assertGreaterEqual(measurement.timestamp/1000, int(start_time))
        self.assertLessEqual(measurement.timestamp/1000, time.time())

    def test_measurement_with_named_endpoint(self):
        start_time = time.time()
        result = self.fetch('/named')
        self.assertEqual(result.code, 200)
        measurement = self.get_measurement()
        self.assertIsNotNone(measurement)
        self.assertEqual(measurement.db, 'database-name')
        self.assertEqual(measurement.name, 'my-service')
        self.assertEqual(measurement.tags['status_code'], '200')
        self.assertEqual(measurement.tags['method'], 'GET')
        self.assertEqual(measurement.tags['endpoint'], '/named')
        self.assertEqual(
            measurement.tags['handler'], 'tests.base.NamedRequestHandler')
        self.assertEqual(measurement.tags['hostname'], socket.gethostname())
        self.assertEqual(measurement.fields['content_length'], 16)

        self.assertGreater(float(measurement.fields['duration']), 0.001)
        self.assertLess(float(measurement.fields['duration']), 0.1)
        self.assertGreaterEqual(measurement.timestamp/1000, int(start_time))
        self.assertLessEqual(measurement.timestamp/1000, time.time())

    def test_measurement_with_param_endpoint(self):
        result = self.fetch('/param/100')
        self.assertEqual(result.code, 200)
        measurement = self.get_measurement()
        self.assertIsNotNone(measurement)
        self.assertEqual(measurement.db, 'database-name')
        self.assertEqual(measurement.name, 'my-service')
        self.assertEqual(measurement.tags['status_code'], '200')
        self.assertEqual(measurement.tags['method'], 'GET')
        self.assertEqual(measurement.tags['endpoint'], '/param/(?P<id>\d+)')
        self.assertEqual(measurement.fields['content_length'], 13)

    def test_measurement_with_specific_host(self):
        self.application.add_handlers(
            'some_host', [('/host/(?P<id>\d+)', base.ParamRequestHandler)])
        result = self.fetch('/host/100', headers={'Host': 'some_host'})
        self.assertEqual(result.code, 200)
        measurement = self.get_measurement()
        self.assertIsNotNone(measurement)
        self.assertEqual(measurement.db, 'database-name')
        self.assertEqual(measurement.name, 'my-service')
        self.assertEqual(measurement.tags['status_code'], '200')
        self.assertEqual(measurement.tags['method'], 'GET')
        self.assertEqual(measurement.tags['endpoint'], '/host/(?P<id>\d+)')
        self.assertEqual(measurement.fields['content_length'], 13)

    @unittest.skipIf(tornado.version_info >= (4, 5),
                     'legacy routing removed in 4.5')
    @mock.patch(
        'sprockets_influxdb.InfluxDBMixin._get_path_pattern_tornado45')
    @mock.patch(
        'sprockets_influxdb.InfluxDBMixin._get_path_pattern_tornado4')
    def test_mesurement_with_ambiguous_route_4(self, mock_4, mock_45):
        mock_4.return_value = None
        mock_45.return_value = None

        result = self.fetch('/param/100')
        self.assertEqual(result.code, 200)
        measurement = self.get_measurement()
        self.assertIsNotNone(measurement)
        self.assertEqual(measurement.db, 'database-name')
        self.assertEqual(measurement.name, 'my-service')
        self.assertEqual(measurement.tags['status_code'], '200')
        self.assertEqual(measurement.tags['method'], 'GET')
        self.assertEqual(measurement.tags['endpoint'], '/param/100')
        self.assertEqual(measurement.fields['content_length'], 13)

        self.assertEqual(1, mock_4.call_count)
        self.assertEqual(0, mock_45.call_count)

    @unittest.skipIf(tornado.version_info < (4, 5),
                     'routing module introduced in tornado 4.5')
    @mock.patch(
        'sprockets_influxdb.InfluxDBMixin._get_path_pattern_tornado45')
    @mock.patch(
        'sprockets_influxdb.InfluxDBMixin._get_path_pattern_tornado4')
    def test_mesurement_with_ambiguous_route_45(self, mock_4, mock_45):
        mock_4.return_value = None
        mock_45.return_value = None

        result = self.fetch('/param/100')
        self.assertEqual(result.code, 200)
        measurement = self.get_measurement()
        self.assertIsNotNone(measurement)
        self.assertEqual(measurement.db, 'database-name')
        self.assertEqual(measurement.name, 'my-service')
        self.assertEqual(measurement.tags['status_code'], '200')
        self.assertEqual(measurement.tags['method'], 'GET')
        self.assertEqual(measurement.tags['endpoint'], '/param/100')
        self.assertEqual(measurement.fields['content_length'], 13)

        self.assertEqual(0, mock_4.call_count)
        self.assertEqual(1, mock_45.call_count)
