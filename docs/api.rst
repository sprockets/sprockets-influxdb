Sprockets InfluxDB API
======================
To use the InfluxDB client, you need to install it into the runtime environment.
To do so, you use the :meth:`~sprockets_influxdb.install` method. Then metrics
can be added by creating instances of the :class:`~sprockets_influxdb.Measurement`
class and then adding them to the write buffer by calling
:meth:`~sprockets_influxdb.add_measurement`. In the following example, a measurement
is added to the ``example`` InfluxDB database with the measurement name of
``measurement-name``. When the IOLoop is started, the ``stop`` method is invoked
which calls :meth:`~sprockets_influxdb.shutdown`. :meth:`~sprockets_influxdb.shutdown`
ensures that all of the buffered metrics are written before the IOLoop is stopped.

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

If you are writing a Tornado web application, you can automatically instrument
all of your Request Handlers by adding the :class:`~sprockets_influxdb.InfluxDBMixin`:

.. code:: python

   import logging
   import os

   import sprockets_influxdb as influxdb
   from tornado import ioloop, web


   class RequestHandler(influxdb.InfluxDBMixin,
                        web.RequestHandler):

       def get(self, *args, **kwargs):
           self.write({'hello': 'world'})


   if __name__ == '__main__':
       logging.basicConfig(level=logging.INFO)

       os.environ['ENVIRONMENT'] = 'development'
       os.environ['SERVICE'] = 'example'

       io_loop = ioloop.IOLoop.current()

       application = web.Application([
           (r"/", RequestHandler),
       ], **{influxdb.REQUEST_DATABASE: 'example'})
       application.listen(8888)
       influxdb.install(io_loop=io_loop)
       try:
           io_loop.start()
       except KeyboardInterrupt:
           logging.info('Stopping')
           influxdb.shutdown()
           io_loop.stop()
           logging.info('Stopped')


Core Methods
------------

.. autofunction:: sprockets_influxdb.install
.. autofunction:: sprockets_influxdb.add_measurement
.. autofunction:: sprockets_influxdb.shutdown

Measurement Class
-----------------
.. autoclass:: sprockets_influxdb.Measurement
   :members:

Configuration Methods
---------------------

.. autofunction:: sprockets_influxdb.set_auth_credentials
.. autofunction:: sprockets_influxdb.set_base_url
.. autofunction:: sprockets_influxdb.set_io_loop
.. autofunction:: sprockets_influxdb.set_max_batch_size
.. autofunction:: sprockets_influxdb.set_max_buffer_size
.. autofunction:: sprockets_influxdb.set_clients
.. autofunction:: sprockets_influxdb.set_submission_interval

Request Handler Mixin
---------------------

.. autoclass:: sprockets_influxdb.InfluxDBMixin
   :members:

Other
-----

.. autofunction:: sprockets_influxdb.flush
