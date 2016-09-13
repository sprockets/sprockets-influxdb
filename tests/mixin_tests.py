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
        result = self.fetch('/')
        self.assertEqual(result.code, 200)
        measurement = self.get_measurement()
        self.assertIsNotNone(measurement)
        self.assertEqual(measurement.db, 'database-name')
        self.assertEqual(measurement.name, 'my-service')
        self.assertEqual(measurement.tags['status_code'], '200')
        self.assertEqual(measurement.tags['method'], 'GET')
        self.assertEqual(
            measurement.tags['handler'], 'tests.base.RequestHandler')
        self.assertNotIn('endpoint', measurement.tags)
        self.assertEqual(measurement.tags['hostname'], socket.gethostname())

        self.assertGreater(float(measurement.fields['duration']), 0.001)
        self.assertLess(float(measurement.fields['duration']), 0.1)

        nanos_since_epoch = int(measurement.timestamp)
        then = nanos_since_epoch / 1000000000
        self.assertGreaterEqual(then, int(start_time))
        self.assertLessEqual(then, time.time())

    def test_measurement_without_endpoint(self):
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
            measurement.tags['correlation_id'],
            base.NamedRequestHandler.correlation_id)
        self.assertEqual(
            measurement.tags['handler'], 'tests.base.NamedRequestHandler')
        self.assertEqual(measurement.tags['hostname'], socket.gethostname())

        self.assertGreater(float(measurement.fields['duration']), 0.001)
        self.assertLess(float(measurement.fields['duration']), 0.1)

        nanos_since_epoch = int(measurement.timestamp)
        then = nanos_since_epoch / 1000000000
        self.assertGreaterEqual(then, int(start_time))
        self.assertLessEqual(then, time.time())
