"""
Sprockets InfluxDB
==================
`sprockets_influxdb` includes both a buffering InfluxDB client and a Tornado
RequestHandler mixin.

"""
import contextlib
import logging
import os
import socket
import time
import urllib

try:
    from tornado import concurrent, httpclient, ioloop
except ImportError:  # pragma: no cover
    logging.critical('Could not import Tornado')
    concurrent, httpclient, ioloop = None, None, None

version_info = (1, 0, 2)
__version__ = '.'.join(str(v) for v in version_info)
__all__ = ['__version__', 'version_info', 'add_measurement', 'flush',
           'install', 'shutdown', 'Measurement']

LOGGER = logging.getLogger(__name__)

REQUEST_DATABASE = 'sprockets_influxdb.database'
USER_AGENT = 'sprockets-influxdb/v{}'.format(__version__)

_base_tags = {}
_base_url = 'http://localhost:8086/write'
_credentials = None, None
_dirty = False
_http_client = None
_installed = False
_io_loop = None
_last_warning = None
_measurements = {}
_max_batch_size = 5000
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
        handler = '{}.{}'.format(self.__module__, self.__class__.__name__)
        self.influxdb.set_tags({'handler': handler, 'method': request.method})
        for _host, handlers in application.handlers:
            for handler in handlers:
                match = handler.regex.match(request.path)
                if match:
                    self.influxdb.set_tag('endpoint',
                                          handler.regex.pattern.rstrip('$'))
                    break

    def on_finish(self):
        super(InfluxDBMixin, self).on_finish()
        if hasattr(self, 'correlation_id'):
            self.influxdb.set_tag('correlation_id', self.correlation_id)
        self.influxdb.set_tag('status_code', self._status_code)
        self.influxdb.set_field('duration', self.request.request_time())
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
    if _stopping:
        LOGGER.warning('Discarding measurement for %s while stopping',
                       measurement.database)
        return

    if not measurement.fields:
        raise ValueError('Measurement does not contain a field')

    if measurement.database not in _measurements:
        _measurements[measurement.database] = []

    tags = ','.join(['{}={}'.format(k, v)
                     for k, v in measurement.tags.items()])
    fields = ' '.join(['{}={}'.format(k, v)
                       for k, v in measurement.fields.items()])

    LOGGER.debug('Appending measurement to %s', measurement.database)
    _measurements[measurement.database].append(
        '{},{} {} {:d}'.format(
            measurement.name, tags, fields, int(time.time() * 1000000000)))

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
            submission_interval=5000, max_batch_size=1000, max_clients=10,
            base_tags=None):
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
    :returns: :data:`True` if the client was installed by this call
        and :data:`False` otherwise.

    If ``INFLUXDB_PASSWORD`` is specified as an environment variable, it will
    be masked in the Python process.

    """
    global _base_tags, _base_url, _credentials, _installed, _io_loop, \
        _max_batch_size, _max_clients, _periodic_callback

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
    _max_batch_size = max_batch_size
    _max_clients = max_clients
    _periodic_callback = ioloop.PeriodicCallback(
        _on_periodic_callback, submission_interval, _io_loop)

    # Set the base tags
    _base_tags.setdefault('hostname', socket.gethostname())
    if os.environ.get('ENVIRONMENT'):
        _base_tags.setdefault('environment', os.environ['ENVIRONMENT'])
    if os.environ.get('SERVICE'):
        _base_tags.setdefault('service', os.environ['SERVICE'])
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


def _escape_str(value):
    """Escape the value with InfluxDB's wonderful escaping logic:

    "Measurement names, tag keys, and tag values must escape any spaces or
    commas using a backslash (\). For example: \ and \,. All tag values are
    stored as strings and should not be surrounded in quotes."

    :param str value: The value to be escaped
    :rtype: str

    """
    return str(value).replace(' ', '\ ').replace(',', '\,')


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
    for (future, database, measurements) in futures:

        # If the future hasn't completed, add it to the remaining stack
        if not future.done():
            remaining.append((future, database, measurements))
            continue

        # Get the result of the HTTP request, processing any errors
        try:
            result = future.result()
        except (httpclient.HTTPError, OSError, socket.error) as error:
            _on_request_error(error, database, measurements)
        else:
            if result.code >= 400:
                _on_request_error(result.code, database, measurements)

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
    global _last_warning

    if not _last_warning:
        _last_warning = time.time()

    count = _pending_measurements()
    if count > _warn_threshold and (time.time() - _last_warning) > 60:
        LOGGER.warning('InfluxDB measurement buffer has %i entries', count)


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


def _on_request_error(error, database, measurements):
    """Handle a batch submission error, logging the problem and adding the
    measurements back to the stack.

    :param mixed error: The error that was returned
    :param str database: The database the submission failed for
    :param list measurements: The measurements to add back to the stack

    """
    LOGGER.error('Error submitting batch to %s: %r', database, error)
    _measurements[database] = measurements + _measurements[database]


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
        LOGGER.debug('No pending measurements, skipping write')
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
        url = '{}?db={}'.format(_base_url, database)

        # Get the measurements to submit
        measurements = _measurements[database][:_max_batch_size]

        # Pop them off the stack of pending measurements
        _measurements[database] = _measurements[database][_max_batch_size:]

        # Create the request future
        request = _http_client.fetch(
            url, method='POST', body='\n'.join(measurements).encode('utf-8'))

        # Keep track of each request in our future stack
        futures.append((request, database, measurements))

    # Start the wait cycle for all the requests to complete
    _writing = True
    _futures_wait(future, futures)

    return future


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
        self.name = _escape_str(name)
        self.fields = {}
        self.tags = dict(_base_tags)

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

    def set_field(self, name, value):
        """Set the value of a field in the measurement.

        :param str name: The name of the field to set the value for
        :param int|float value: The value of the field
        :raises: ValueError

        """
        if not isinstance(value, int) and not isinstance(value, float):
            raise ValueError('Value must be an integer or float')
        self.fields[_escape_str(name)] = str(value)

    def set_tag(self, name, value):
        """Set a tag on the measurement.

        :param str name: name of the tag to set
        :param str value: value to assign

        This will overwrite the current value assigned to a tag
        if one exists.

        """
        self.tags[_escape_str(name)] = _escape_str(value)

    def set_tags(self, tags):
        """Set multiple tags for the measurement.

        :param dict tags: Tag key/value pairs to assign

        This will overwrite the current value assigned to a tag
        if one exists with the same name.

        """
        for key, value in tags.items():
            self.set_tag(key, value)
