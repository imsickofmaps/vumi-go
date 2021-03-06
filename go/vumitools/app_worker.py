from zope.interface import implements
from twisted.internet.defer import inlineCallbacks, returnValue

from vumi import log
from vumi.application import ApplicationWorker
from vumi.blinkenlights.metrics import MetricManager, Metric, MAX
from vumi.message import TransportEvent
from vumi.config import IConfigData

from go.vumitools.api import VumiApiCommand, VumiApi, VumiApiEvent
from go.vumitools.api_worker import GoMessageMetadata


class OneShotMetricManager(MetricManager):
    # TODO: Replace this with appropriate functionality on MetricManager and
    # actions triggered by conversations ending.

    def _clear_metrics(self):
        self._metrics = []
        self._metrics_lookup = {}

    def _publish_metrics(self):
        super(OneShotMetricManager, self)._publish_metrics()
        self._clear_metrics()


class GoApplicationConfigData(object):
    implements(IConfigData)

    def __init__(self, config_dict, conversation):
        self.config_dict = config_dict
        self.conv = conversation

    def get(self, field_name, default):
        if self.conv.metadata and field_name in self.conv.metadata:
            return self.conv.metadata[field_name]
        return self.config_dict.get(field_name, default)

    def has_key(self, field_name):
        if self.conv.metadata and field_name in self.conv.metadata:
            return True
        return self.config_dict.has_key(field_name)


class GoApplicationConfigMixin(object):
    def get_conversation(self):
        return self._config_data.conv


class GoApplicationMixin(object):

    def _go_validate_config(self):
        self.worker_name = self.config.get('worker_name', self.worker_name)
        self.api_routing_config = VumiApiCommand.default_routing_config()
        self.api_routing_config.update(self.config.get('api_routing', {}))
        self.app_event_routing_config = VumiApiEvent.default_routing_config()
        self.app_event_routing_config.update(
            self.config.get('app_event_routing', {}))
        self.control_consumer = None
        # This is now mandatory, so you need to provide it even if the app
        # doesn't do metrics.
        self.metrics_prefix = self.config['metrics_prefix']

    @inlineCallbacks
    def _go_setup_application(self):
        self.vumi_api = yield VumiApi.from_config_async(
            self.config, self._amqp_client)
        self._metrics_conversations = set()
        self._cache_recon_conversations = set()

        # In case we need these.
        self.redis = self.vumi_api.redis
        self.manager = self.vumi_api.manager

        self.app_event_publisher = yield self.publish_to(
            self.app_event_routing_config['routing_key'])

        self.metrics = yield self.start_publisher(
            OneShotMetricManager, self.metrics_prefix)

        self.control_consumer = yield self.consume(
            '%s.control' % (self.worker_name,),
            self.consume_control_command,
            exchange_name=self.api_routing_config['exchange'],
            exchange_type=self.api_routing_config['exchange_type'],
            message_class=VumiApiCommand)

    @inlineCallbacks
    def _go_teardown_application(self):
        # Sometimes something else closes our Redis connection.
        if self.redis is not None:
            yield self.redis.close_manager()
        if self.control_consumer is not None:
            yield self.control_consumer.stop()
            self.control_consumer = None
        self.metrics.stop()

    def get_user_api(self, user_account_key):
        return self.vumi_api.get_user_api(user_account_key)

    def get_config_for_conversation(self, conversation):
        config_data = GoApplicationConfigData(self.config, conversation)
        return self.CONFIG_CLASS(config_data)

    @inlineCallbacks
    def get_message_config(self, msg):
        if isinstance(msg, TransportEvent):
            msg = yield self.find_message_for_event(msg)

        metadata = self.get_go_metadata(msg)
        conversation = yield metadata.get_conversation()

        returnValue(self.get_config_for_conversation(conversation))

    def consume_control_command(self, command_message):
        """
        Handle a VumiApiCommand message that has arrived.

        :type command_message: VumiApiCommand
        :param command_message:
            The command message received for this application.
        """
        cmd_method_name = 'process_command_%(command)s' % command_message
        args = command_message['args']
        kwargs = command_message['kwargs']
        cmd_method = getattr(self, cmd_method_name, None)
        if cmd_method:
            return cmd_method(*args, **kwargs)
        else:
            return self.process_unknown_cmd(cmd_method_name, *args, **kwargs)

    @inlineCallbacks
    def process_command_collect_metrics(self, conversation_key,
                                        user_account_key):
        key_tuple = (conversation_key, user_account_key)
        if key_tuple in self._metrics_conversations:
            log.info("Ignoring conversation %s for user %s because the "
                     "previous collection run is still going." % (
                        conversation_key, user_account_key))
            return
        self._metrics_conversations.add(key_tuple)
        user_api = self.get_user_api(user_account_key)
        yield self.collect_metrics(user_api, conversation_key)
        self._metrics_conversations.remove(key_tuple)

    @inlineCallbacks
    def process_command_reconcile_cache(self, conversation_key,
                                        user_account_key):
        key_tuple = (conversation_key, user_account_key)
        if key_tuple in self._cache_recon_conversations:
            log.info("Ignoring conversation %s for user %s because the "
                     "previous cache recon run is still going." % (
                        conversation_key, user_account_key))
            return
        self._cache_recon_conversations.add(key_tuple)
        user_api = self.get_user_api(user_account_key)
        yield self.reconcile_cache(user_api, conversation_key)
        self._cache_recon_conversations.remove(key_tuple)

    def process_unknown_cmd(self, method_name, *args, **kwargs):
        log.error("Unknown vumi API command: %s(%s, %s)" % (
            method_name, args, kwargs))

    def collect_metrics(self, user_api, conversation_key):
        # By default, we don't collect metrics.
        pass

    @inlineCallbacks
    def reconcile_cache(self, user_api, conversation_key, delta=0.01):
        """Reconcile the cached values for the conversation.

        Checks whether caches for a conversation are off by a given
        delta and if so, initiates a full cache reconciliation.

        :param VumiUserApi user_api:
            The Api for this user
        :param str conversation_key:
            The key of the conversation to reconcile
        :param float delta:
            If the key count difference between the message_store and
            the cache is bigger than the delta a reconciliation is initiated.
        """
        conv = yield user_api.get_wrapped_conversation(conversation_key)
        if conv is None:
            log.error('Conversation does not exist: %s' % (conversation_key,))
            return

        log.msg('Reconciling cache for %s' % (conversation_key,))
        message_store = user_api.api.mdb
        for batch_key in conv.get_batch_keys():
            if (yield message_store.needs_reconciliation(batch_key, delta)):
                yield message_store.reconcile_cache(batch_key)
        log.msg('Cache reconciled for %s' % (conversation_key,))

    @inlineCallbacks
    def get_contact_for_message(self, message):
        helper_metadata = message.get('helper_metadata', {})

        go_metadata = helper_metadata.get('go', {})
        account_key = go_metadata.get('user_account', None)
        conversation_key = go_metadata.get('conversation_key', None)

        if account_key and conversation_key:
            user_api = self.get_user_api(account_key)
            conv = yield user_api.get_wrapped_conversation(conversation_key)
            contact = yield user_api.contact_store.contact_for_addr(
                conv.delivery_class, message.user())

            returnValue(contact)

    @inlineCallbacks
    def get_conversation(self, batch_id, conversation_key):
        batch = yield self.vumi_api.mdb.get_batch(batch_id)
        if batch is None:
            log.error('Cannot find batch for batch_id %s' % (batch_id,))
            return

        user_account_key = batch.metadata["user_account"]
        if user_account_key is None:
            log.error("No account key in batch metadata: %r" % (batch,))
            return

        user_api = self.get_user_api(user_account_key)
        conv = yield user_api.get_wrapped_conversation(conversation_key)
        returnValue(conv)

    def get_go_metadata(self, msg):
        return GoMessageMetadata(self.vumi_api, msg)

    @inlineCallbacks
    def find_outboundmessage_for_event(self, event):
        user_message_id = event.get('user_message_id')
        if user_message_id is None:
            log.error('Received event without user_message_id: %s' % (event,))
            return

        msg = yield self.vumi_api.mdb.outbound_messages.load(user_message_id)
        if msg is None:
            log.error('Unable to find message for event: %s' % (event,))

        returnValue(msg)

    @inlineCallbacks
    def find_message_for_event(self, event):
        outbound_message = yield self.find_outboundmessage_for_event(event)
        if outbound_message:
            returnValue(outbound_message.msg)

    @inlineCallbacks
    def event_for_message(self, message, event_type, content):
        gmt = self.get_go_metadata(message)
        account_key = yield gmt.get_account_key()
        conversation_key, conversation_type = yield gmt.get_conversation_info()
        event = VumiApiEvent.event(account_key, conversation_key,
                                    event_type, content)
        returnValue(event)

    @inlineCallbacks
    def trigger_event(self, message, event_type, content):
        event = yield self.event_for_message(message, event_type, content)
        yield self.publish_app_event(event)

    def publish_app_event(self, event):
        self.app_event_publisher.publish_message(event)

    def publish_metric(self, name, value, agg=None):
        if agg is None:
            agg = MAX
        if name not in self.metrics:
            metric = Metric(name, [agg])
            self.metrics.register(metric)
        else:
            metric = self.metrics[name]
        metric.set(value)

    def publish_conversation_metric(self, conversation, name, value, agg=None):
        name = "%s.%s.%s" % (
            conversation.user_account.key, conversation.key, name)
        self.publish_metric(name, value, agg)

    def publish_account_metric(self, user_account_key, store, name, value,
                               agg=None):
        name = "%s.%s.%s" % (user_account_key, store, name)
        self.publish_metric(name, value, agg)

    @inlineCallbacks
    def collect_message_metrics(self, conversation):
        """Collect message count metrics.

        This is a utility method for collecting common metrics. It has to be
        called explicitly from :meth:`collect_metrics`
        """
        sent = 0
        received = 0
        for batch_id in conversation.batches.keys():
            sent += yield self.vumi_api.mdb.batch_outbound_count(batch_id)
            received += yield self.vumi_api.mdb.batch_inbound_count(batch_id)

        self.publish_conversation_metric(
            conversation, 'messages_sent', sent)
        self.publish_conversation_metric(
            conversation, 'messages_received', received)


class GoApplicationWorker(GoApplicationMixin, ApplicationWorker):
    """
    A base class for Vumi Go application worker.

    Configuration parameters:

    :type worker_name: str
    :param worker_name:
        The name of this worker, used for receiving control messages.

    """

    worker_name = None

    def validate_config(self):
        return self._go_validate_config()

    def setup_application(self):
        return self._go_setup_application()

    def teardown_application(self):
        return self._go_teardown_application()
