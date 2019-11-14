"""
Sprockets InfluxDB
==================
`sprockets_influxdb` includes both a buffering InfluxDB client and a Tornado
RequestHandler mixin.

Measurements will be sent in batches to InfluxDB when there are
``INFLUXDB_TRIGGER_SIZE`` measurements in the buffer or after
``INFLUXDB_INTERVAL`` milliseconds have passed since the last measurement was
added, which ever occurs first.

The timeout timer for submitting a buffer of < ``INFLUXDB_TRIGGER_SIZE``
measurements is only started when there isn't an active timer, there is not a
batch currently being written, and a measurement is added to the buffer.

"""
import contextlib
import logging
import os
import random
import select
import socket
import ssl
import time
import uuid

try:
    from tornado import concurrent, httpclient, ioloop
except ImportError:  # pragma: no cover
    logging.critical('Could not import Tornado')
    concurrent, httpclient, ioloop = None, None, None

try:
    from tornado import routing
except ImportError:  # Not needed for Tornado<4.5
    pass


version_info = (2, 2, 1)
__version__ = '.'.join(str(v) for v in version_info)
__all__ = ['__version__', 'version_info', 'add_measurement', 'flush',
           'install', 'shutdown', 'Measurement']

LOGGER = logging.getLogger(__name__)

REQUEST_DATABASE = 'sprockets_influxdb.database'
USER_AGENT = 'sprockets-influxdb/v{}'.format(__version__)

try:
    TimeoutError
except NameError:  # Python 2.7 compatibility
    class TimeoutError(Exception):
        pass


_base_tags = {}
_base_url = 'http://localhost:8086/write'
_batch_future = None
_buffer_size = 0
_credentials = None, None
_dirty = False
_enabled = True
_http_client = None
_installed = False
_last_warning = None
_measurements = {}
_max_batch_size = 10000
_max_buffer_size = 25000
_max_clients = 10
_sample_probability = 1.0
_stopping = False
_timeout_interval = 60000
_timeout = None
_trigger_size = 5000
_warn_threshold = 15000
_writing = False


class InfluxDBMixin(object):
    """Mixin that automatically submits per-request measurements to InfluxDB
    with the request duration.

    The measurements will automatically add the following tags:

     - Request :data:`handler`
     - Request :data:`endpoint` (if enabled via a named URL)
     - Request :data:`method`
     - Request :data:`correlation_id` (if set)
     - Response :data:`status_code`

    To add additional tags and fields, use the
    :meth:`~sprockets_influxdb.Measurement.set_field`,
    :meth:`~sprockets_influxdb.Measurement.set_tag`,
    :meth:`~sprockets_influxdb.Measurement.set_tags`, and
    :meth:`~sprockets_influxdb.Measurement.timer` methods of the
    ``influxdb`` attribute of the :class:`~tornado.web.RequestHandler`.

    """
    def __init__(self, application, request, **kwargs):
        self.influxdb = Measurement(
            application.settings[REQUEST_DATABASE],
            application.settings.get('service', 'request'))
        super(InfluxDBMixin, self).__init__(application, request, **kwargs)
        if _enabled:
            handler = '{}.{}'.format(self.__module__, self.__class__.__name__)
            self.influxdb.set_tags({'handler': handler,
                                    'method': request.method})

            pattern = None
            if hasattr(application, 'handlers'):
                pattern = self._get_path_pattern_tornado4()
            else:
                pattern = self._get_path_pattern_tornado45()
            if pattern:
                endpoint = pattern.rstrip('$')
            else:
                LOGGER.warning('Unable to determine routing pattern')
                endpoint = request.path
            self.influxdb.set_tags({'endpoint': endpoint})

    def _get_path_pattern_tornado4(self):
        """Return the path pattern used when routing a request. (Tornado<4.5)

        :rtype: str
        """
        for host, handlers in self.application.handlers:
            if host.match(self.request.host):
                for handler in handlers:
                    if handler.regex.match(self.request.path):
                        return handler.regex.pattern

    def _get_path_pattern_tornado45(self, router=None):
        """Return the path pattern used when routing a request. (Tornado>=4.5)

        :param tornado.routing.Router router: (Optional) The router to scan.
            Defaults to the application's router.

        :rtype: str
        """
        if router is None:
            router = self.application.default_router
        for rule in router.rules:
            if rule.matcher.match(self.request) is not None:
                if isinstance(rule.matcher, routing.PathMatches):
                    return rule.matcher.regex.pattern
                elif isinstance(rule.target, routing.Router):
                    return self._get_path_pattern_tornado45(rule.target)

    def on_finish(self):
        if _enabled:
            self.influxdb.set_field(
                'content_length', int(self._headers.get('Content-Length', 0)))
            self.influxdb.set_field('duration', self.request.request_time())
            self.influxdb.set_tag('status_code', self._status_code)
            self.influxdb.set_tag('remote_ip', self.request.remote_ip)
            add_measurement(self.influxdb)


def add_measurement(measurement):
    """Add measurement data to the submission buffer for eventual writing to
    InfluxDB.

    Example:

    .. code:: python

        import sprockets_influxdb as influxdb

        measurement = influxdb.Measurement('example', 'measurement-name')
        measurement.set_tag('foo', 'bar')
        measurement.set_field('baz', 1.05)

        influxdb.add_measurement(measurement)

    :param :class:`~sprockets_influxdb.Measurement` measurement: The
        measurement to add to the buffer for submission to InfluxDB.

    """
    global _buffer_size

    if not _enabled:
        LOGGER.debug('Discarding measurement for %s while not enabled',
                     measurement.database)
        return

    if _stopping:
        LOGGER.warning('Discarding measurement for %s while stopping',
                       measurement.database)
        return

    if _buffer_size > _max_buffer_size:
        LOGGER.warning('Discarding measurement due to buffer size limit')
        return

    if not measurement.fields:
        raise ValueError('Measurement does not contain a field')

    if measurement.database not in _measurements:
        _measurements[measurement.database] = []

    value = measurement.marshall()
    _measurements[measurement.database].append(value)

    # Ensure that len(measurements) < _trigger_size are written
    if not _timeout:
        if (_batch_future and _batch_future.done()) or not _batch_future:
            _start_timeout()

    # Check to see if the batch should be triggered
    _buffer_size = _pending_measurements()
    if _buffer_size >= _trigger_size:
        _trigger_batch_write()


def flush():
    """Flush all pending measurements to InfluxDB. This will ensure that all
    measurements that are in the buffer for any database are written. If the
    requests fail, it will continue to try and submit the metrics until they
    are successfully written.

    :rtype: :class:`~tornado.concurrent.Future`

    """
    flush_future = concurrent.Future()
    if _batch_future and not _batch_future.done():
        LOGGER.debug('Flush waiting on incomplete _batch_future')
        _flush_wait(flush_future, _batch_future)
    else:
        LOGGER.info('Flushing buffer with %i measurements to InfluxDB',
                    _pending_measurements())
        _flush_wait(flush_future, _write_measurements())
    return flush_future


def install(url=None, auth_username=None, auth_password=None,
            submission_interval=None, max_batch_size=None, max_clients=10,
            base_tags=None, max_buffer_size=None, trigger_size=None,
            sample_probability=1.0):
    """Call this to install/setup the InfluxDB client collector. All arguments
    are optional.

    :param str url: The InfluxDB API URL. If URL is not specified, the
        ``INFLUXDB_SCHEME``, ``INFLUXDB_HOST`` and ``INFLUXDB_PORT``
        environment variables will be used to construct the base URL. Default:
        ``http://localhost:8086/write``
    :param str auth_username: A username to use for InfluxDB authentication. If
        not specified, the ``INFLUXDB_USER`` environment variable will
        be used. Default: ``None``
    :param str auth_password: A password to use for InfluxDB authentication. If
        not specified, the ``INFLUXDB_PASSWORD`` environment variable will
        be used. Default: ``None``
    :param int submission_interval: The maximum number of milliseconds to wait
        after the last batch submission before submitting a batch that is
        smaller than ``trigger_size``. Default: ``60000``
    :param int max_batch_size: The number of measurements to be submitted in a
        single HTTP request. Default: ``10000``
    :param int max_clients: The number of simultaneous batch submissions that
        may be made at any given time. Default: ``10``
    :param dict base_tags: Default tags that are to be submitted with each
        measurement.  Default: ``None``
    :param int max_buffer_size: The maximum number of pending measurements
        in the buffer before new measurements are discarded.
        Default: ``25000``
    :param int trigger_size: The minimum number of measurements that
        are in the buffer before a batch can be submitted. Default: ``5000``
    :param float sample_probability: Value between 0 and 1.0 specifying the
        probability that a batch will be submitted (0.25 == 25%)
    :returns: :data:`True` if the client was installed by this call
        and :data:`False` otherwise.

    If ``INFLUXDB_PASSWORD`` is specified as an environment variable, it will
    be masked in the Python process.

    """
    global _base_tags, _base_url, _credentials, _enabled, _installed, \
        _max_batch_size, _max_buffer_size, _max_clients, \
        _sample_probability, _timeout, _timeout_interval, _trigger_size

    _enabled = os.environ.get('INFLUXDB_ENABLED', 'true') == 'true'
    if not _enabled:
        LOGGER.warning('Disabling InfluxDB support')
        return

    if _installed:
        LOGGER.warning('InfluxDB client already installed')
        return False

    _base_url = url or '{}://{}:{}/write'.format(
        os.environ.get('INFLUXDB_SCHEME', 'http'),
        os.environ.get('INFLUXDB_HOST', 'localhost'),
        os.environ.get('INFLUXDB_PORT', 8086))

    _credentials = (auth_username or os.environ.get('INFLUXDB_USER', None),
                    auth_password or os.environ.get('INFLUXDB_PASSWORD', None))

    # Don't leave the environment variable out there with the password
    if os.environ.get('INFLUXDB_PASSWORD'):
        os.environ['INFLUXDB_PASSWORD'] = \
            'X' * len(os.environ['INFLUXDB_PASSWORD'])

    # Submission related values
    _timeout_interval = submission_interval or \
        int(os.environ.get('INFLUXDB_INTERVAL', _timeout_interval))
    _max_batch_size = max_batch_size or \
        int(os.environ.get('INFLUXDB_MAX_BATCH_SIZE', _max_batch_size))
    _max_clients = max_clients
    _max_buffer_size = max_buffer_size or \
        int(os.environ.get('INFLUXDB_MAX_BUFFER_SIZE', _max_buffer_size))
    _sample_probability = sample_probability or \
        float(os.environ.get('INFLUXDB_SAMPLE_PROBABILITY',
                             _sample_probability))
    _trigger_size = trigger_size or \
        int(os.environ.get('INFLUXDB_TRIGGER_SIZE', _trigger_size))

    # Set the base tags
    if os.environ.get('INFLUXDB_TAG_HOSTNAME', 'true') == 'true':
        _base_tags.setdefault('hostname', socket.gethostname())
    if os.environ.get('ENVIRONMENT'):
        _base_tags.setdefault('environment', os.environ['ENVIRONMENT'])
    _base_tags.update(base_tags or {})

    # Seed the random number generator for batch sampling
    random.seed()

    # Don't let this run multiple times
    _installed = True

    LOGGER.info('sprockets_influxdb v%s installed; %i measurements or %.2f '
                'seconds will trigger batch submission', __version__,
                _trigger_size, _timeout_interval / 1000.0)
    return True


def set_auth_credentials(username, password):
    """Override the default authentication credentials obtained from the
    environment variable configuration.

    :param str username: The username to use
    :param str password: The password to use

    """
    global _credentials, _dirty

    LOGGER.debug('Setting authentication credentials')
    _credentials = username, password
    _dirty = True


def set_base_url(url):
    """Override the default base URL value created from the environment
    variable configuration.

    :param str url: The base URL to use when submitting measurements

    """
    global _base_url, _dirty

    LOGGER.debug('Setting base URL to %s', url)
    _base_url = url
    _dirty = True


def set_max_batch_size(limit):
    """Set a limit to the number of measurements that are submitted in
    a single batch that is submitted per databases.

    :param int limit: The maximum number of measurements per batch


    """
    global _max_batch_size

    LOGGER.debug('Setting maximum batch size to %i', limit)
    _max_batch_size = limit


def set_max_buffer_size(limit):
    """Set the maximum number of pending measurements allowed in the buffer
    before new measurements are discarded.

    :param int limit: The maximum number of measurements per batch

    """
    global _max_buffer_size

    LOGGER.debug('Setting maximum buffer size to %i', limit)
    _max_buffer_size = limit


def set_max_clients(limit):
    """Set the maximum number of simultaneous batch submission that can execute
    in parallel.

    :param int limit: The maximum number of simultaneous batch submissions

    """
    global _dirty, _max_clients

    LOGGER.debug('Setting maximum client limit to %i', limit)
    _dirty = True
    _max_clients = limit


def set_sample_probability(probability):
    """Set the probability that a batch will be submitted to the InfluxDB
    server. This should be a value that is greater than or equal to ``0`` and
    less than or equal to ``1.0``. A value of ``0.25`` would represent a
    probability of 25% that a batch would be written to InfluxDB.

    :param float probability: The value between 0 and 1.0 that represents the
        probability that a batch will be submitted to the InfluxDB server.

    """
    global _sample_probability

    if not 0.0 <= probability <= 1.0:
        raise ValueError('Invalid probability value')

    LOGGER.debug('Setting sample probability to %.2f', probability)
    _sample_probability = float(probability)


def set_timeout(milliseconds):
    """Override the maximum duration to wait for submitting measurements to
    InfluxDB.

    :param int milliseconds: Maximum wait in milliseconds

    """
    global _timeout, _timeout_interval

    LOGGER.debug('Setting batch wait timeout to %i ms', milliseconds)
    _timeout_interval = milliseconds
    _maybe_stop_timeout()
    _timeout = ioloop.IOLoop.current().add_timeout(milliseconds, _on_timeout)


def set_trigger_size(limit):
    """Set the number of pending measurements that trigger the writing of data
    to InfluxDB

    :param int limit: The minimum number of measurements to trigger a batch

    """
    global _trigger_size

    LOGGER.debug('Setting trigger buffer size to %i', limit)
    _trigger_size = limit


def shutdown():
    """Invoke on shutdown of your application to stop the periodic
    callbacks and flush any remaining metrics.

    Returns a future that is complete when all pending metrics have been
    submitted.

    :rtype: :class:`~tornado.concurrent.Future`

    """
    global _stopping

    if _stopping:
        LOGGER.warning('Already shutting down')
        return

    _stopping = True
    _maybe_stop_timeout()
    return flush()


def _create_http_client():
    """Create the HTTP client with authentication credentials if required."""
    global _http_client

    defaults = {'user_agent': USER_AGENT}
    auth_username, auth_password = _credentials
    if auth_username and auth_password:
        defaults['auth_username'] = auth_username
        defaults['auth_password'] = auth_password

    _http_client = httpclient.AsyncHTTPClient(
        force_instance=True, defaults=defaults,
        max_clients=_max_clients)


def _flush_wait(flush_future, write_future):
    """Pause briefly allowing any pending metric writes to complete before
    shutting down.

    :param tornado.concurrent.Future flush_future: The future to resolve
        when the shutdown is complete.
    :param tornado.concurrent.Future write_future: The future that is for the
        current batch write operation.

    """
    if write_future.done():
        if not _pending_measurements():
            flush_future.set_result(True)
            return
        else:
            write_future = _write_measurements()
    ioloop.IOLoop.current().add_timeout(
        ioloop.IOLoop.current().time() + 0.25,
        _flush_wait, flush_future, write_future)


def _futures_wait(wait_future, futures):
    """Waits for all futures to be completed. If the futures are not done,
    wait 100ms and then invoke itself via the ioloop and check again. If
    they are done, set a result on `wait_future` indicating the list of
    futures are done.

    :param wait_future: The future to complete when all `futures` are done
    :type wait_future: tornado.concurrent.Future
    :param list futures: The list of futures to watch for completion

    """
    global _buffer_size, _writing

    remaining = []
    for (future, batch, database, measurements) in futures:

        # If the future hasn't completed, add it to the remaining stack
        if not future.done():
            remaining.append((future, batch, database, measurements))
            continue

        # Get the result of the HTTP request, processing any errors
        error = future.exception()
        if isinstance(error, httpclient.HTTPError):
            if error.code == 400:
                _write_error_batch(batch, database, measurements)
            elif error.code >= 500:
                _on_5xx_error(batch, error, database, measurements)
            else:
                LOGGER.error('Error submitting %s batch %s to InfluxDB (%s): '
                             '%s', database, batch, error.code,
                             error.response.body)
        elif isinstance(error, (TimeoutError, OSError, socket.error,
                                select.error, ssl.socket_error)):
            _on_5xx_error(batch, error, database, measurements)

    # If there are futures that remain, try again in 100ms.
    if remaining:
        return ioloop.IOLoop.current().add_timeout(
            ioloop.IOLoop.current().time() + 0.1,
            _futures_wait, wait_future, remaining)
    else:  # Start the next timeout or trigger the next batch
        _buffer_size = _pending_measurements()
        LOGGER.debug('Batch submitted, %i measurements remain', _buffer_size)
        if _buffer_size >= _trigger_size:
            ioloop.IOLoop.current().add_callback(_trigger_batch_write)
        elif _buffer_size:
            _start_timeout()

    _writing = False
    wait_future.set_result(True)


def _maybe_stop_timeout():
    """If there is a pending timeout, remove it from the IOLoop and set the
    ``_timeout`` global to None.

    """
    global _timeout

    if _timeout is not None:
        LOGGER.debug('Removing the pending timeout (%r)', _timeout)
        ioloop.IOLoop.current().remove_timeout(_timeout)
        _timeout = None


def _maybe_warn_about_buffer_size():
    """Check the buffer size and issue a warning if it's too large and
    a warning has not been issued for more than 60 seconds.

    """
    global _last_warning

    if not _last_warning:
        _last_warning = time.time()

    if _buffer_size > _warn_threshold and (time.time() - _last_warning) > 120:
        LOGGER.warning('InfluxDB measurement buffer has %i entries',
                       _buffer_size)


def _on_5xx_error(batch, error, database, measurements):
    """Handle a batch submission error, logging the problem and adding the
    measurements back to the stack.

    :param str batch: The batch ID
    :param mixed error: The error that was returned
    :param str database: The database the submission failed for
    :param list measurements: The measurements to add back to the stack

    """
    LOGGER.info('Appending %s measurements to stack due to batch %s %r',
                database, batch, error)
    _measurements[database] = _measurements[database] + measurements


def _on_timeout():
    """Invoked periodically to ensure that metrics that have been collected
    are submitted to InfluxDB.

    :rtype: tornado.concurrent.Future or None

    """
    global _buffer_size

    LOGGER.debug('No metrics submitted in the last %.2f seconds',
                 _timeout_interval / 1000.0)
    _buffer_size = _pending_measurements()
    if _buffer_size:
        return _trigger_batch_write()
    _start_timeout()


def _pending_measurements():
    """Return the number of measurements that have not been submitted to
    InfluxDB.

    :rtype: int

    """
    return sum([len(_measurements[dbname]) for dbname in _measurements])


def _sample_batch():
    """Determine if a batch should be processed and if not, pop off all of
    the pending metrics for that batch.

    :rtype: bool

    """
    if _sample_probability == 1.0 or random.random() < _sample_probability:
        return True

    # Pop off all the metrics for the batch
    for database in _measurements:
        _measurements[database] = _measurements[database][_max_batch_size:]
    return False


def _start_timeout():
    """Stop a running timeout if it's there, then create a new one."""
    global _timeout

    LOGGER.debug('Adding a new timeout in %i ms', _timeout_interval)
    _maybe_stop_timeout()
    _timeout = ioloop.IOLoop.current().add_timeout(
        ioloop.IOLoop.current().time() + _timeout_interval / 1000.0,
        _on_timeout)


def _trigger_batch_write():
    """Stop a timeout if it's running, and then write the measurements."""
    global _batch_future

    LOGGER.debug('Batch write triggered (%r/%r)',
                 _buffer_size, _trigger_size)
    _maybe_stop_timeout()
    _maybe_warn_about_buffer_size()
    _batch_future = _write_measurements()
    return _batch_future


def _write_measurements():
    """Write out all of the metrics in each of the databases,
    returning a future that will indicate all metrics have been written
    when that future is done.

    :rtype: tornado.concurrent.Future

    """
    global _timeout, _writing

    future = concurrent.Future()

    if _writing:
        LOGGER.warning('Currently writing measurements, skipping write')
        future.set_result(False)
    elif not _pending_measurements():
        future.set_result(True)
    elif not _sample_batch():
        LOGGER.debug('Skipping batch submission due to sampling')
        future.set_result(True)

    # Exit early if there's an error condition
    if future.done():
        return future

    if not _http_client or _dirty:
        _create_http_client()

    # Keep track of the futures for each batch submission
    futures = []

    # Submit a batch for each database
    for database in _measurements:
        url = '{}?db={}&precision=ms'.format(_base_url, database)

        # Get the measurements to submit
        measurements = _measurements[database][:_max_batch_size]

        # Pop them off the stack of pending measurements
        _measurements[database] = _measurements[database][_max_batch_size:]

        # Create the request future
        LOGGER.debug('Submitting %r measurements to %r',
                     len(measurements), url)
        request = _http_client.fetch(
            url, method='POST', body='\n'.join(measurements).encode('utf-8'))

        # Keep track of each request in our future stack
        futures.append((request, str(uuid.uuid4()), database, measurements))

    # Start the wait cycle for all the requests to complete
    _writing = True
    _futures_wait(future, futures)

    return future


def _write_error_batch(batch, database, measurements):
    """Invoked when a batch submission fails, this method will submit one
    measurement to InfluxDB. It then adds a timeout to the IOLoop which will
    invoke :meth:`_write_error_batch_wait` which will evaluate the result and
    then determine what to do next.

    :param str batch: The batch ID for correlation purposes
    :param str database: The database name for the measurements
    :param list measurements: The measurements that failed to write as a batch

    """
    if not measurements:
        LOGGER.info('All %s measurements from batch %s processed',
                    database, batch)
        return

    LOGGER.debug('Processing batch %s for %s by measurement, %i left',
                 batch, database, len(measurements))

    url = '{}?db={}&precision=ms'.format(_base_url, database)

    measurement = measurements.pop(0)

    # Create the request future
    future = _http_client.fetch(
        url, method='POST', body=measurement.encode('utf-8'))

    # Check in 25ms to see if it's done
    ioloop.IOLoop.current().add_timeout(
        ioloop.IOLoop.current().time() + 0.025,
        _write_error_batch_wait, future, batch, database, measurement,
        measurements)


def _write_error_batch_wait(future, batch, database, measurement,
                            measurements):
    """Invoked by the IOLoop, this method checks if the HTTP request future
    created by :meth:`_write_error_batch` is done. If it's done it will
    evaluate the result, logging any error and moving on to the next
    measurement. If there are no measurements left in the `measurements`
    argument, it will consider the batch complete.


    :param tornado.concurrent.Future future: The AsyncHTTPClient request future
    :param str batch: The batch ID
    :param str database: The database name for the measurements
    :param str measurement: The measurement the future is for
    :param list measurements: The measurements that failed to write as a batch

    """
    if not future.done():
        ioloop.IOLoop.current().add_timeout(
            ioloop.IOLoop.current().time() + 0.025,
            _write_error_batch_wait, future, batch, database, measurement,
            measurements)
        return

    error = future.exception()
    if isinstance(error, httpclient.HTTPError):
        if error.code == 400:
            LOGGER.error('Error writing %s measurement from batch %s to '
                         'InfluxDB (%s): %s', database, batch, error.code,
                         error.response.body)
            LOGGER.info('Bad %s measurement from batch %s: %s',
                        database, batch, measurement)
        else:
            LOGGER.error('Error submitting individual metric for %s from '
                         'batch %s to InfluxDB (%s): %s',
                         database, batch, error.code)
            measurements = measurements + [measurement]
    elif isinstance(error, (TimeoutError, OSError, socket.error,
                            select.error, ssl.socket_error)):
        LOGGER.error('Error submitting individual metric for %s from batch '
                     '%s to InfluxDB (%s)', database, batch, error)
        _write_error_batch(batch, database, measurements + [measurement])
        measurements = measurements + [measurement]

    if not measurements:
        LOGGER.info('All %s measurements from batch %s processed',
                    database, batch)
        return

    # Continue writing measurements
    _write_error_batch(batch, database, measurements)


class Measurement(object):
    """The :class:`Measurement` class represents what will become a single row
    in an InfluxDB database. Measurements are added to InfluxDB via the
    :meth:`~sprockets_influxdb.add_measurement` method.

    Example:

    .. code:: python

        import sprockets_influxdb as influxdb

        measurement = Measurement('database-name', 'measurement-name')
        measurement.set_tag('foo', 'bar')
        measurement.set_field('baz', 1.05)

        influxdb.add_measurement(measurement)

    :param str database: The database name to use when submitting
    :param str name: The measurement name

    """
    def __init__(self, database, name):
        self.database = database
        self.name = name
        self.fields = {}
        self.tags = dict(_base_tags)
        self.timestamp = time.time()

    @contextlib.contextmanager
    def duration(self, name):
        """Record the time it takes to run an arbitrary code block.

        :param str name: The field name to record the timing in

        This method returns a context manager that records the amount
        of time spent inside of the context, adding the timing to the
        measurement.

        """
        start = time.time()
        try:
            yield
        finally:
            self.set_field(name, max(time.time(), start) - start)

    def marshall(self):
        """Return the measurement in the line protocol format.

        :rtype: str

        """
        return '{},{} {} {}'.format(
            self._escape(self.name),
            ','.join(['{}={}'.format(self._escape(k), self._escape(v))
                      for k, v in self.tags.items()]),
            self._marshall_fields(),
            int(self.timestamp * 1000))

    def set_field(self, name, value):
        """Set the value of a field in the measurement.

        :param str name: The name of the field to set the value for
        :param int|float|bool|str value: The value of the field
        :raises: ValueError

        """
        if not any([isinstance(value, t) for t in {int, float, bool, str}]):
            LOGGER.debug('Invalid field value: %r', value)
            raise ValueError('Value must be a str, bool, integer, or float')
        self.fields[name] = value

    def set_tag(self, name, value):
        """Set a tag on the measurement.

        :param str name: name of the tag to set
        :param str value: value to assign

        This will overwrite the current value assigned to a tag
        if one exists.

        """
        self.tags[name] = value

    def set_tags(self, tags):
        """Set multiple tags for the measurement.

        :param dict tags: Tag key/value pairs to assign

        This will overwrite the current value assigned to a tag
        if one exists with the same name.

        """
        for key, value in tags.items():
            self.set_tag(key, value)

    def set_timestamp(self, value):
        """Override the timestamp of a measurement.

        :param float value: The timestamp to assign to the measurement

        """
        self.timestamp = value

    @staticmethod
    def _escape(value):
        """Escape a string (key or value) for InfluxDB's line protocol.

        :param str|int|float|bool value: The value to be escaped
        :rtype: str

        """
        value = str(value)
        for char, escaped in {' ': '\ ', ',': '\,', '"': '\"'}.items():
            value = value.replace(char, escaped)
        return value

    def _marshall_fields(self):
        """Convert the field dict into the string segment of field key/value
        pairs.

        :rtype: str

        """
        values = {}
        for key, value in self.fields.items():
            if (isinstance(value, int) or
                    (isinstance(value, str) and value.isdigit() and
                     '.' not in value)):
                values[key] = '{}i'.format(value)
            elif isinstance(value, bool):
                values[key] = self._escape(value)
            elif isinstance(value, float):
                values[key] = '{}'.format(value)
            elif isinstance(value, str):
                values[key] = '"{}"'.format(self._escape(value))
        return ','.join(['{}={}'.format(self._escape(k), v)
                         for k, v in values.items()])
