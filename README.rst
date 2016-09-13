Sprockets InfluxDB
==================
Buffering InfluxDB client and mixin for Tornado applications

|Version| |Downloads| |Status| |Coverage| |License|

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

+-----------------------+-------------------------------------------------+---------------+
| Variable              | Definition                                      | Default       |
+=======================+=================================================+===============+
| ``INFLUXDB_SCHEME``   | The URL request scheme for making HTTP requests | ``https``     |
+-----------------------+-------------------------------------------------+---------------+
| ``INFLUXDB_HOST``     | The InfluxDB server hostname                    | ``localhost`` |
+-----------------------+-------------------------------------------------+---------------+
| ``INFLUXDB_PORT``     | The InfluxDB server port                        | ``8086``      |
+-----------------------+-------------------------------------------------+---------------+
| ``INFLUXDB_USER``     | The InfluxDB server username                    |               |
+-----------------------+-------------------------------------------------+---------------+
| ``INFLUXDB_PASSWORD`` | The InfluxDB server password                    |               |
+-----------------------+-------------------------------------------------+---------------+

Mixin Configuration
^^^^^^^^^^^^^^^^^^^
The ``sprockets_influxdb.InfluxDBMixin`` class will automatically tag measurements
with the application/service name if the ``SERVICE`` environment variable is set. It will
also tag the measurement if the ``ENVIRONMENT`` environment variable is set with the environment
that the application is running in. Finally, if you are using the
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

.. |Downloads| image:: https://img.shields.io/pypi/dm/sprockets-influxdb.svg?
   :target: https://pypi.python.org/pypi/sprockets-influxdb

.. |License| image:: https://img.shields.io/pypi/l/sprockets-influxdb.svg?
   :target: https://sprockets-influxdb.readthedocs.org
