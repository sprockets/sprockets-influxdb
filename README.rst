Sprockets InfluxDB
==================
Buffering InfluxDB client and mixin for Tornado applications

|Version| |Status| |Coverage| |License|

Installation
------------
``sprockets-influxdb`` is available on the Python package index and is installable via pip:

.. code:: bash

    pip install sprockets-influxdb

Documentation
-------------
Documentation is available at `sprockets-influxdb.readthedocs.io <https://sprockets-influxdb.readthedocs.io>`_.

Configuration
-------------
Configuration can be managed by specifying arguments when invoking
``sprockets_influxdb.install`` or by using environment variables.

For programmatic configuration, see the
`sprockets_influxdb.install <https://sprockets-influxdb.readthedocs.io/en/latest/api.html#sprockets_influxdb.install>`_
documentation.

The following table details the environment variable configuration options.

+---------------------------------+--------------------------------------------------+---------------+
| Variable                        | Definition                                       | Default       |
+=================================+==================================================+===============+
| ``INFLUXDB_SCHEME``             | The URL request scheme for making HTTP requests  | ``https``     |
+---------------------------------+--------------------------------------------------+---------------+
| ``INFLUXDB_HOST``               | The InfluxDB server hostname                     | ``localhost`` |
+---------------------------------+--------------------------------------------------+---------------+
| ``INFLUXDB_PORT``               | The InfluxDB server port                         | ``8086``      |
+---------------------------------+--------------------------------------------------+---------------+
| ``INFLUXDB_USER``               | The InfluxDB server username                     |               |
+---------------------------------+--------------------------------------------------+---------------+
| ``INFLUXDB_PASSWORD``           | The InfluxDB server password                     |               |
+---------------------------------+--------------------------------------------------+---------------+
| ``INFLUXDB_ENABLED``            | Set to ``false`` to disable InfluxDB support     | ``true``      |
+---------------------------------+--------------------------------------------------+---------------+
| ``INFLUXDB_INTERVAL``           | How many milliseconds to wait before submitting  | ``60000``     |
|                                 | measurements when the buffer has fewer than      |               |
|                                 | ``INFLUXDB_TRIGGER_SIZE`` measurements.          |               |
+---------------------------------+--------------------------------------------------+---------------+
| ``INFLUXDB_MAX_BATCH_SIZE``     | Max # of measurements to submit in a batch       | ``10000``     |
+---------------------------------+--------------------------------------------------+---------------+
| ``INFLUXDB_MAX_BUFFER_SIZE``    | Limit of measurements in a buffer before new     | ``25000``     |
|                                 | measurements are discarded.                      |               |
+---------------------------------+--------------------------------------------------+---------------+
| ``INFLUXDB_SAMPLE_PROBABILITY`` | A value that is >= 0 and <= 1.0 that specifies   | ``1.0``       |
|                                 | the probability that a batch will be submitted   |               |
|                                 | to InfluxDB or dropped.                          |               |
+---------------------------------+--------------------------------------------------+---------------+
| ``INFLUXDB_TRIGGER_SIZE``       | The number of metrics in the buffer to trigger   | ``60000``     |
|                                 | the submission of a batch.                       |               |
+---------------------------------+--------------------------------------------------+---------------+
| ``INFLUXDB_TAG_HOSTNAME``       | Include the hostname as a tag in the measurement | ``true``      |
+---------------------------------+--------------------------------------------------+---------------+

Mixin Configuration
^^^^^^^^^^^^^^^^^^^
The ``sprockets_influxdb.InfluxDBMixin`` class will automatically tag the measurement if the
``ENVIRONMENT`` environment variable is set with the environment that the application is running
in. Finally, if you are using the
`Sprockets Correlation Mixin <https://github.com/sprockets/sprockets.mixins.correlation>`_,
measurements will automatically be tagged with the correlation ID for a request.

Example
-------
In the following example, a measurement is added to the ``example`` InfluxDB database
with the measurement name of ``measurement-name``. When the ``~tornado.ioloop.IOLoop``
is started, the ``stop`` method is invoked, calling ``~sprockets_influxdb.shutdown``.
``~sprockets_influxdb.shutdown`` ensures that all of the buffered metrics are
written before the IOLoop is stopped.

.. code:: python

    import logging

    import sprockets_influxdb as influxdb
    from tornado import ioloop

    logging.basicConfig(level=logging.INFO)

    io_loop = ioloop.IOLoop.current()
    influxdb.install(io_loop=io_loop)

    measurement = influxdb.Measurement('example', 'measurement-name')
    measurement.set_tag('foo', 'bar')
    measurement.set_field('baz', 1.05)

    influxdb.add_measurement(measurement)

    def stop():
        influxdb.shutdown()
        io_loop.stop()

    io_loop.add_callback(stop)
    io_loop.start()

Requirements
------------
-  `Tornado <https://tornadoweb.org>`_

Version History
---------------
Available at https://sprockets-influxdb.readthedocs.org/en/latest/history.html

.. |Version| image:: https://img.shields.io/pypi/v/sprockets-influxdb.svg?
   :target: http://badge.fury.io/py/sprockets-influxdb

.. |Status| image:: https://img.shields.io/travis/sprockets/sprockets-influxdb.svg?
   :target: https://travis-ci.org/sprockets/sprockets-influxdb

.. |Coverage| image:: https://img.shields.io/codecov/c/github/sprockets/sprockets-influxdb.svg?
   :target: https://codecov.io/github/sprockets/sprockets-influxdb?branch=master

.. |License| image:: https://img.shields.io/pypi/l/sprockets-influxdb.svg?
   :target: https://sprockets-influxdb.readthedocs.org
