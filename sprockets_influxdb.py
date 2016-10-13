"""
Sprockets InfluxDB
==================
`sprockets_influxdb` includes both a buffering InfluxDB client and a Tornado
RequestHandler mixin.

"""
import contextlib
import logging
import os
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

version_info = (1, 3, 1)
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
_buffer_size = 0
_credentials = None, None
_dirty = False
_enabled = True
_http_client = None
_installed = False
_io_loop = None
_last_warning = None
_measurements = {}
_max_batch_size = 5000
_max_buffer_size = 20000
_max_clients = 10
_periodic_callback = None
_periodic_future = None
_stopping = False
_warn_threshold = 5000
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
            for host, handlers in application.handlers:
                if not host.match(request.host):
                    continue
                for handler in handlers:
                    match = handler.regex.match(request.path)
                    if match:
                        self.influxdb.set_tag('endpoint',
                                              handler.regex.pattern.rstrip('$'))
                        break

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
    if not _enabled:
        LOGGER.debug('Discarding measurement for %s while not enabled',
                     measurement.database)
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
    _maybe_warn_about_buffer_size()


def flush():
    """Flush all pending measurements to InfluxDB. This will ensure that all
    measurements that are in the buffer for any database are written. If the
    requests fail, it will continue to try and submit the metrics until they
    are successfully written.

    :rtype: :class:`~tornado.concurrent.Future`

    """
    LOGGER.debug('Flushing')
    flush_future = concurrent.TracebackFuture()
    if _periodic_future and not _periodic_future.done():
        LOGGER.debug('Waiting on _periodic_future instead')
        write_future = _periodic_future
    else:
        write_future = _write_measurements()
    _flush_wait(flush_future, write_future)
    return flush_future


def install(url=None, auth_username=None, auth_password=None, io_loop=None,
            submission_interval=None, max_batch_size=None, max_clients=10,
            base_tags=None, max_buffer_size=None):
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
    :param io_loop: A :class:`~tornado.ioloop.IOLoop` to use instead of the
        version returned by :meth:`~tornado.ioloop.IOLoop.current`
    :type io_loop: :class:`tornado.ioloop.IOLoop`
    :param int submission_interval: How often to submit metric batches in
        milliseconds. Default: ``5000``
    :param int max_batch_size: The number of measurements to be submitted in a
        single HTTP request. Default: ``1000``
    :param int max_clients: The number of simultaneous batch submissions that
        may be made at any given time. Default: ``10``
    :param dict base_tags: Default tags that are to be submitted with each
        measurement.  Default: ``None``
    :param int max_buffer_size: The maximum number of pending measurements
        in the buffer before new measurements are discarded.
    :returns: :data:`True` if the client was installed by this call
        and :data:`False` otherwise.

    If ``INFLUXDB_PASSWORD`` is specified as an environment variable, it will
    be masked in the Python process.

    """
    global _base_tags, _base_url, _credentials, _enabled, _installed, \
        _io_loop, _max_batch_size, _max_buffer_size, _max_clients, \
        _periodic_callback

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
    _io_loop = io_loop or ioloop.IOLoop.current()
    interval = submission_interval or \
        int(os.environ.get('INFLUXDB_INTERVAL', 5000))
    _max_batch_size = max_batch_size or \
        int(os.environ.get('INFLUXDB_MAX_BATCH_SIZE', 5000))
    _max_clients = max_clients
    _max_buffer_size = max_buffer_size or \
        int(os.environ.get('INFLUXDB_MAX_BUFFER_SIZE', 20000))

    _periodic_callback = ioloop.PeriodicCallback(
        _on_periodic_callback, interval, _io_loop)

    # Set the base tags
    _base_tags.setdefault('hostname', socket.gethostname())
    if os.environ.get('ENVIRONMENT'):
        _base_tags.setdefault('environment', os.environ['ENVIRONMENT'])
    _base_tags.update(base_tags or {})

    # Start the periodic callback on IOLoop start
    _io_loop.add_callback(_periodic_callback.start)

    # Don't let this run multiple times
    _installed = True

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


def set_io_loop(io_loop):
    """Override the use of the default IOLoop.

    :param tornado.ioloop.IOLoop io_loop: The IOLoop to use
    :raises: ValueError

    """
    global _dirty, _io_loop

    if not isinstance(io_loop, ioloop.IOLoop):
        raise ValueError('Invalid io_loop value')

    LOGGER.debug('Overriding the default IOLoop, using %r', io_loop)
    _dirty = True
    _io_loop = io_loop


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


def set_submission_interval(seconds):
    """Override how often to submit measurements to InfluxDB.

    :param int seconds: How often to wait in seconds

    """
    global _periodic_callback

    LOGGER.debug('Setting submission interval to %s seconds', seconds)
    if _periodic_callback.is_running():
        _periodic_callback.stop()
    _periodic_callback = ioloop.PeriodicCallback(_on_periodic_callback,
                                                 seconds, _io_loop)
    # Start the periodic callback on IOLoop start if it's not already started
    _io_loop.add_callback(_periodic_callback.start)


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
    if _periodic_callback.is_running():
        _periodic_callback.stop()
    LOGGER.info('Stopped periodic measurement submission and writing current '
                'buffer to InfluxDB')
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
        force_instance=True, defaults=defaults, io_loop=_io_loop,
        max_clients=_max_clients)


def _flush_wait(flush_future, write_future):
    """Pause briefly allowing any pending metric writes to complete before
    shutting down.

    :param future tornado.concurrent.TracebackFuture: The future to resolve
        when the shutdown is complete.

    """
    if write_future.done():
        if not _pending_measurements():
            flush_future.set_result(True)
            return
        else:
            write_future = _write_measurements()
    _io_loop.add_timeout(
        _io_loop.time() + 0.25, _flush_wait, flush_future, write_future)


def _futures_wait(wait_future, futures):
    """Waits for all futures to be completed. If the futures are not done,
    wait 100ms and then invoke itself via the ioloop and check again. If
    they are done, set a result on `wait_future` indicating the list of
    futures are done.

    :param wait_future: The future to complete when all `futures` are done
    :type wait_future: tornado.concurrent.Future
    :param list futures: The list of futures to watch for completion

    """
    global _writing

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
        return _io_loop.add_timeout(
            _io_loop.time() + 0.1, _futures_wait, wait_future, remaining)

    _writing = False
    wait_future.set_result(True)


def _maybe_warn_about_buffer_size():
    """Check the buffer size and issue a warning if it's too large and
    a warning has not been issued for more than 60 seconds.

    """
    global _buffer_size, _last_warning

    if not _last_warning:
        _last_warning = time.time()

    _buffer_size = _pending_measurements()
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


def _on_periodic_callback():
    """Invoked periodically to ensure that metrics that have been collected
    are submitted to InfluxDB. If metrics are still being written when it
    is invoked, pass until the next time.

    :rtype: tornado.concurrent.Future

    """
    global _periodic_future

    if isinstance(_periodic_future, concurrent.Future) \
            and not _periodic_future.done():
        LOGGER.warning('Metrics are currently being written, '
                       'skipping write interval')
        return
    _periodic_future = _write_measurements()
    return _periodic_future


def _pending_measurements():
    """Return the number of measurements that have not been submitted to
    InfluxDB.

    :rtype: int

    """
    return sum([len(_measurements[dbname]) for dbname in _measurements])


def _write_measurements():
    """Write out all of the metrics in each of the databases,
    returning a future that will indicate all metrics have been written
    when that future is done.

    :rtype: tornado.concurrent.Future

    """
    global _writing

    future = concurrent.TracebackFuture()

    if _writing:
        LOGGER.warning('Currently writing measurements, skipping write')
        future.set_result(False)
    elif not _pending_measurements():
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
    _io_loop.add_timeout(_io_loop.time() + 0.025, _write_error_batch_wait,
                         future, batch, database, measurement, measurements)


def _write_error_batch_wait(future, batch, database, measurement, measurements):
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
        _io_loop.add_timeout(_io_loop.time() + 0.025, _write_error_batch_wait,
                             future, batch, database, measurement,
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
            LOGGER.error('Error submitting individual metric for %s from batch '
                         '%s to InfluxDB (%s): %s', database, batch, error.code)
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
