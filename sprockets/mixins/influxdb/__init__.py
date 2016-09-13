"""
A simple RequestHandler mixin for adding per-request measurements to InfluxDB

"""
from sprockets.clients import influxdb


class InfluxDBMixin(object):
    """Mixin that automatically submits per-request measurements to InfluxDB
    with the request duration.

    The measurements will automatically add the following tags:

     - Request `handler`
     - Request `endpoint` (if enabled via a named URL)
     - Request `method`
     - Request `correlation_id` (if set)
     - Response `status_code`

    To add additional tags and fields, use the
    :meth:`~sprockets.clients.influxdb.Measurement.set_field`,
    :meth:`~sprockets.clients.influxdb.Measurement.set_tag`,
    :meth:`~sprockets.clients.influxdb.Measurement.set_tags`, and
    :meth:`~sprockets.clients.influxdb.Measurement.timer` methods of the
    `influxdb` attribute of the `RequestHandler`.

    """
    def __init__(self, application, request, **kwargs):
        self.application = application  # Set this here for reverse_url
        self.__metrics = []
        handler = '{}.{}'.format(self.__module__, self.__class__.__name__)

        self.influxdb = influxdb.Measurement(
            application.settings[influxdb.REQUEST_DATABASE],
            application.settings.get('service', 'request'))
        self.influxdb.set_tags({'handler': handler, 'method': request.method})
        try:
            self.influxdb.set_tag('endpoint', self.reverse_url(handler))
        except KeyError:
            pass

        # Call to super().__init__() needs to be *AFTER* we create our
        # properties since it calls initialize() which may want to call
        # methods like ``set_metric_tag``
        super(InfluxDBMixin, self).__init__(application, request, **kwargs)

    def on_finish(self):
        super(InfluxDBMixin, self).on_finish()
        if hasattr(self, 'correlation_id'):
            self.influxdb.set_tag('correlation_id', self.correlation_id)
        self.influxdb.set_tag('status_code', self._status_code)
        self.influxdb.set_field('duration', self.request.request_time())
        influxdb.add_measurement(self.influxdb)
