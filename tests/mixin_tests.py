import socket
import time

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

        self.assertGreaterEqual(measurement.timestamp, int(start_time))
        self.assertLessEqual(measurement.timestamp, time.time())

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
        self.assertEqual(
            measurement.fields['correlation_id'],
            base.NamedRequestHandler.correlation_id)

        self.assertGreater(float(measurement.fields['duration']), 0.001)
        self.assertLess(float(measurement.fields['duration']), 0.1)
        self.assertGreaterEqual(measurement.timestamp, int(start_time))
        self.assertLessEqual(measurement.timestamp, time.time())

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
