from twisted.internet.defer import inlineCallbacks

from vumi_wikipedia.tests import test_wikipedia

from go.vumitools.tests.utils import GoPersistenceMixin
from go.apps.wikipedia.vumi_app import WikipediaApplication


class WikipediaApplicationTestCase(GoPersistenceMixin,
                                   test_wikipedia.WikipediaWorkerTestCase):
    application_class = WikipediaApplication
    use_riak = True

    def setUp(self):
        self.patch(WikipediaApplication, 'get_conversation_metadata',
            lambda s, msg: {'send_from_tagpool': 'devnull',
                            'send_from_tag': '100@devnull',
                            })
        self.patch(WikipediaApplication, 'get_tagpool_metadata',
            lambda s, tp, key, default=None: {
                'transport_name': 'devnull_transport',
            })
        return super(WikipediaApplicationTestCase, self).setUp()

    @inlineCallbacks
    def tearDown(self):
        # Ensure that for every test, SMS is going out via the tagpool
        # as specified in the conversation metadata
        for message in self._amqp.get_messages('vumi', 'sphex_sms.outbound'):
            self.assertEqual(message['transport_name'], 'devnull_transport')
            self.assertEqual(message['from_addr'], '100@devnull')
        # All other 'normal' USSD traffic should continue over the existing
        # tagpool / transport for this conversation.
        for message in self.get_dispatched_messages():
            self.assertEqual(message['transport_name'], self.transport_name)

        yield super(WikipediaApplicationTestCase, self).tearDown()

    def assert_metrics(self, expected_metrics):
        # We aren't collecting these.
        pass

    def test_no_metrics_prefix(self):
        # This isn't supported.
        pass
