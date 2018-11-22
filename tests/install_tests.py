import os
import random
import socket
import unittest
import uuid

from tornado import ioloop, testing
import sprockets_influxdb as influxdb

from . import base


class InstallDefaultsTestCase(base.TestCase):

    def setUp(self):
        super(InstallDefaultsTestCase, self).setUp()
        os.environ['ENVIRONMENT'] = str(uuid.uuid4())
        influxdb.install()

    def test_calling_install_again_returns_false(self):
        self.assertFalse(influxdb.install())

    def test_dirty_flag_is_false(self):
        self.assertFalse(influxdb._dirty)

    def test_default_credentials(self):
        self.assertEqual(influxdb._credentials, (None, None))

    def test_default_tags(self):
        expectation = {
            'environment': os.environ['ENVIRONMENT'],
            'hostname': socket.gethostname()
        }
        self.assertDictEqual(influxdb._base_tags, expectation)

    def test_default_url(self):
        self.assertEqual(influxdb._base_url, 'http://localhost:8086/write')

    def test_http_client_defaults(self):
        influxdb._create_http_client()
        self.assertEqual(influxdb._http_client.defaults['user_agent'],
                         influxdb.USER_AGENT)

    def test_set_submission_interval(self):
        self.assertEqual(influxdb._timeout_interval, 60000)


class InstallCredentialsTestCase(base.TestCase):

    def test_credentials_from_environment_variables(self):
        password = str(uuid.uuid4())
        os.environ['INFLUXDB_USER'] = str(uuid.uuid4())
        os.environ['INFLUXDB_PASSWORD'] = password
        influxdb.install()

        expectation = (os.environ['INFLUXDB_USER'], password)
        self.assertEqual(influxdb._credentials, expectation)

    def test_password_envvar_is_masked(self):
        password = str(uuid.uuid4())
        os.environ['INFLUXDB_USER'] = str(uuid.uuid4())
        os.environ['INFLUXDB_PASSWORD'] = password
        influxdb.install()

        expectation = 'X' * len(password)
        self.assertEqual(os.environ['INFLUXDB_PASSWORD'], expectation)

    def test_http_client_defaults(self):
        password = str(uuid.uuid4())
        os.environ['INFLUXDB_USER'] = str(uuid.uuid4())
        os.environ['INFLUXDB_PASSWORD'] = password
        influxdb.install()
        influxdb._create_http_client()
        expectation = {
            'auth_username': os.environ['INFLUXDB_USER'],
            'auth_password': password,
            'user_agent': influxdb.USER_AGENT
        }
        for key in expectation:
            self.assertEqual(influxdb._http_client.defaults.get(key),
                             expectation[key])


class SetConfigurationTestCase(base.AsyncTestCase):

    def test_set_auth_credentials(self):
        influxdb.install()
        username = str(uuid.uuid4())
        password = str(uuid.uuid4())
        influxdb.set_auth_credentials(username, password)
        expectation = username, password
        self.assertEqual(influxdb._credentials, expectation)
        self.assertTrue(influxdb._dirty)

    def test_set_base_url(self):
        influxdb.install()
        expectation = 'https://influxdb.com:8086/write'
        influxdb.set_base_url(expectation)
        self.assertEqual(influxdb._base_url, expectation)
        self.assertTrue(influxdb._dirty)

    def test_set_max_batch_size(self):
        influxdb.install()
        expectation = random.randint(1000, 100000)
        influxdb.set_max_batch_size(expectation)
        self.assertEqual(influxdb._max_batch_size, expectation)

    def test_set_max_clients(self):
        influxdb.install()
        expectation = random.randint(1, 100)
        influxdb.set_max_clients(expectation)
        self.assertEqual(influxdb._max_clients, expectation)
        self.assertTrue(influxdb._dirty)

    @testing.gen_test()
    def test_set_timeout(self):
        influxdb.install()
        expectation = random.randint(1000, 10000)
        influxdb.set_timeout(expectation)
        self.assertEqual(influxdb._timeout_interval, expectation)

    def test_set_sample_probability(self):
        influxdb.install()
        expectation = random.random()
        influxdb.set_sample_probability(expectation)
        self.assertEqual(influxdb._sample_probability, expectation)

    def test_set_invalid_sample_probability(self):
        influxdb.install()
        with self.assertRaises(ValueError):
            influxdb.set_sample_probability(2.0)
        with self.assertRaises(ValueError):
            influxdb.set_sample_probability(-1.0)


class MeasurementTests(unittest.TestCase):

    def test_initialization(self):
        database = str(uuid.uuid4())
        name = str(uuid.uuid4())
        measurement = influxdb.Measurement(database, name)
        self.assertEqual(measurement.database, database)
        self.assertEqual(measurement.name, name)
