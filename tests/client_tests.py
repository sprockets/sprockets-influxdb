import base64
import random
import mock
import time
import uuid

from tornado import concurrent, gen, httpclient

from sprockets.clients import influxdb

from . import base


class AuthTestCase(base.AsyncServerTestCase):
    def setUp(self):
        super(AuthTestCase, self).setUp()
        self.username, self.password = str(uuid.uuid4()), str(uuid.uuid4())
        influxdb.set_auth_credentials(self.username, self.password)

    def test_that_authentication_header_was_sent(self):
        database = str(uuid.uuid4())
        name = str(uuid.uuid4())
        test_value = random.randint(1000, 2000)

        measurement = influxdb.Measurement(database, name)
        measurement.set_field('test', test_value)

        influxdb.add_measurement(measurement)
        self.flush()

        result = self.get_measurement()
        self.assertIn('Authorization', result.headers)
        scheme, value = result.headers['Authorization'].split(' ')
        self.assertEqual(scheme, 'Basic')
        temp = base64.b64decode(value.encode('utf-8'))
        values = temp.decode('utf-8').split(':')
        self.assertEqual(values[0], self.username)
        self.assertEqual(values[1], self.password)


class MeasurementTestCase(base.AsyncServerTestCase):

    def test_measurement_was_sent(self):
        database = str(uuid.uuid4())
        name = str(uuid.uuid4())
        tag_value = str(uuid.uuid4())
        test_value = random.randint(1000, 2000)

        measurement = influxdb.Measurement(database, name)
        measurement.set_field('test', test_value)
        measurement.set_tag('test_tag', tag_value)

        influxdb.add_measurement(measurement)
        self.flush()
        result = self.get_measurement()
        self.assertEqual(result.db, database)
        self.assertEqual(result.name, name)
        self.assertEqual(result.fields['test'], test_value)
        self.assertEqual(result.tags['test_tag'], tag_value)

    def test_measurement_tags(self):
        database = str(uuid.uuid4())
        name = str(uuid.uuid4())
        tags = {
            'tag1': str(uuid.uuid4()),
            'tag2': str(uuid.uuid4())
        }
        test_value = random.randint(1000, 2000)

        measurement = influxdb.Measurement(database, name)
        measurement.set_field('test', test_value)
        measurement.set_tags(tags)

        influxdb.add_measurement(measurement)
        self.flush()
        result = self.get_measurement()
        self.assertEqual(result.db, database)
        self.assertEqual(result.name, name)
        self.assertEqual(result.fields['test'], test_value)
        for tag, value in tags.items():
            self.assertEqual(result.tags[tag], value)

    def test_invalid_value_raises_value_error(self):
        with self.assertRaises(ValueError):
            database = str(uuid.uuid4())
            name = str(uuid.uuid4())
            measurement = influxdb.Measurement(database, name)
            measurement.set_field('foo', 'bar')

    def test_missing_value_raises_value_error(self):
        with self.assertRaises(ValueError):
            database = str(uuid.uuid4())
            name = str(uuid.uuid4())
            measurement = influxdb.Measurement(database, name)
            influxdb.add_measurement(measurement)

    def test_write_with_no_measurements(self):
        self.assertEqual(len(base.measurements), 0)
        self.flush()
        self.assertEqual(len(base.measurements), 0)

    def test_with_http_error(self):
        database = str(uuid.uuid4())
        name = str(uuid.uuid4())
        test_value = random.randint(1000, 2000)
        measurement = influxdb.Measurement(database, name)
        measurement.set_field('test', test_value)
        influxdb.add_measurement(measurement)
        self.assertEqual(influxdb._pending_measurements(), 1)
        with mock.patch('tornado.httpclient.AsyncHTTPClient.fetch') as fetch:
            future = concurrent.Future()
            fetch.return_value = future
            request = httpclient.HTTPRequest('http://localhost/write?db')
            future.set_result(
                httpclient.HTTPResponse(
                    request, 599, error=OSError(),
                    request_time=time.time() - request.start_time))
            self.flush()
        self.assertEqual(influxdb._pending_measurements(), 1)
        self.flush()
        result = self.get_measurement()
        self.assertEqual(result.db, database)
        self.assertEqual(result.name, name)
        self.assertEqual(result.fields['test'], test_value)

    def test_with_oserror(self):
        database = str(uuid.uuid4())
        name = str(uuid.uuid4())
        test_value = random.randint(1000, 2000)
        measurement = influxdb.Measurement(database, name)
        measurement.set_field('test', test_value)
        influxdb.add_measurement(measurement)
        influxdb._create_http_client()
        self.assertEqual(influxdb._pending_measurements(), 1)
        with mock.patch.object(influxdb._http_client, 'fetch') as fetch:
            future = concurrent.Future()
            future.set_exception(OSError())
            fetch.return_value = future
            influxdb._on_periodic_callback()
        self.assertEqual(influxdb._pending_measurements(), 1)
        self.flush()
        result = self.get_measurement()
        self.assertEqual(result.db, database)
        self.assertEqual(result.name, name)
        self.assertEqual(result.fields['test'], test_value)

    def test_periodic_callback_while_already_processing(self):
        database = str(uuid.uuid4())
        name = str(uuid.uuid4())
        test_value = random.randint(1000, 2000)
        measurement = influxdb.Measurement(database, name)
        measurement.set_field('test', test_value)
        influxdb.add_measurement(measurement)
        self.assertEqual(influxdb._pending_measurements(), 1)
        future = influxdb._on_periodic_callback()
        self.assertIsNone(influxdb._on_periodic_callback())
        self.assertEqual(influxdb._pending_measurements(), 0)
        self.io_loop.add_future(future, self.stop)
        self.wait()
        result = self.get_measurement()
        self.assertEqual(result.db, database)
        self.assertEqual(result.name, name)
        self.assertEqual(result.fields['test'], test_value)

    def test_write_measurements_while_already_processing(self):
        database = str(uuid.uuid4())
        name = str(uuid.uuid4())
        test_value = random.randint(1000, 2000)
        measurement = influxdb.Measurement(database, name)
        measurement.set_field('test', test_value)
        influxdb.add_measurement(measurement)
        self.assertEqual(influxdb._pending_measurements(), 1)
        future = influxdb._on_periodic_callback()
        second_write = influxdb._write_measurements()
        self.assertTrue(concurrent.is_future(second_write))
        self.assertTrue(second_write.done())
        self.assertFalse(second_write.result())
        self.assertEqual(influxdb._pending_measurements(), 0)
        self.io_loop.add_future(future, self.stop)
        self.wait()
        result = self.get_measurement()
        self.assertEqual(result.db, database)
        self.assertEqual(result.name, name)
        self.assertEqual(result.fields['test'], test_value)

    def test_timer(self):
        database = str(uuid.uuid4())
        name = str(uuid.uuid4())
        test_value = random.randint(1000, 2000)
        measurement = influxdb.Measurement(database, name)
        measurement.set_field('test', test_value)
        with measurement.duration('duration-test'):
            future = gen.sleep(0.100)
            self.io_loop.add_future(future, self.stop)
            self.wait()
        influxdb.add_measurement(measurement)
        self.assertEqual(influxdb._pending_measurements(), 1)
        self.flush()
        value = self.get_measurement()
        self.assertAlmostEqual(float(value.fields['duration-test']), 0.1, 2)
