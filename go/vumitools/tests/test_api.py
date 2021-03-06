# -*- coding: utf-8 -*-

"""Tests for go.vumitools.api."""

import json

from twisted.trial.unittest import TestCase
from twisted.internet.defer import inlineCallbacks, returnValue

from vumi.tests.utils import get_fake_amq_client

from go.vumitools.opt_out import OptOutStore
from go.vumitools.contact import ContactStore
from go.vumitools.api import (
    VumiApi, VumiUserApi, VumiApiCommand, VumiApiEvent)
from go.vumitools.tests.utils import (
    AppWorkerTestCase, CeleryTestMixIn, DummyConsumerFactory)


class TestTxVumiApi(AppWorkerTestCase, CeleryTestMixIn):
    @inlineCallbacks
    def setUp(self):
        yield super(TestTxVumiApi, self).setUp()
        if self.sync_persistence:
            self.set_up_celery()
            self.api = VumiApi.from_config_sync(self._persist_config)
        else:
            self.api = yield VumiApi.from_config_async(
                self._persist_config, get_fake_amq_client(self._amqp))
        self._persist_riak_managers.append(self.api.manager)
        self._persist_redis_managers.append(self.api.redis)

    def tearDown(self):
        if self.sync_persistence:
            self.restore_celery()
        return super(TestTxVumiApi, self).tearDown()

    def set_up_celery(self):
        # Set up the vumi exchange, in case we don't have one.
        self._amqp.exchange_declare('vumi', 'direct')

        def consumer_factory():
            dummy_consumer = DummyConsumerFactory()
            dummy_consumer.publish = self._publish_celery_command
            return dummy_consumer

        self.VUMI_COMMANDS_CONSUMER = consumer_factory
        self.setup_celery_for_tests()

    def _publish_celery_command(self, cmd_dict):
        data = json.dumps(cmd_dict)
        self._amqp.publish_raw('vumi', 'vumi.api', data)

    @inlineCallbacks
    def test_batch_start(self):
        tag = ("pool", "tag")
        batch_id = yield self.api.batch_start([tag])
        self.assertEqual(len(batch_id), 32)

    @inlineCallbacks
    def test_batch_status(self):
        tag = ("pool", "tag")
        batch_id = yield self.api.mdb.batch_start([tag])
        batch_status = yield self.api.batch_status(batch_id)
        self.assertEqual(batch_status['sent'], 0)

    @inlineCallbacks
    def test_batch_outbound_keys(self):
        batch_id = yield self.api.batch_start([("poolA", "default10001")])
        msgs = [self.mkmsg_out(content=msg, message_id=str(i)) for
                i, msg in enumerate(("msg1", "msg2"))]
        for msg in msgs:
            yield self.api.mdb.add_outbound_message(msg, batch_id=batch_id)
        api_msgs = yield self.api.batch_outbound_keys(batch_id)
        self.assertEqual(sorted(api_msgs), ['0', '1'])

    @inlineCallbacks
    def test_batch_inbound_keys(self):
        tag = ("ambient", "default10001")
        to_addr = "+12310001"
        batch_id = yield self.api.batch_start([tag])
        msgs = [self.mkmsg_in(content=msg, to_addr=to_addr, message_id=str(i),
                              transport_type="sms")
                for i, msg in enumerate(("msg1", "msg2"))]
        for msg in msgs:
            yield self.api.mdb.add_inbound_message(msg, batch_id=batch_id)
        api_msgs = yield self.api.batch_inbound_keys(batch_id)
        self.assertEqual(sorted(api_msgs), ['0', '1'])

    @inlineCallbacks
    def test_batch_tags(self):
        tag1, tag2 = ("poolA", "tag1"), ("poolA", "tag2")
        batch_id = yield self.api.batch_start([tag1])
        self.assertEqual((yield self.api.batch_tags(batch_id)), [tag1])
        batch_id = yield self.api.batch_start([tag1, tag2])
        self.assertEqual((yield self.api.batch_tags(batch_id)), [tag1, tag2])

    @inlineCallbacks
    def test_declare_tags_from_different_pools(self):
        tag1, tag2 = ("poolA", "tag1"), ("poolB", "tag2")
        yield self.api.tpm.declare_tags([tag1, tag2])
        self.assertEqual((yield self.api.tpm.acquire_tag("poolA")), tag1)
        self.assertEqual((yield self.api.tpm.acquire_tag("poolB")), tag2)

    @inlineCallbacks
    def test_start_batch_and_batch_done(self):
        tag = ("pool", "tag")
        yield self.api.tpm.declare_tags([tag])

        @inlineCallbacks
        def tag_batch(t):
            tb = yield self.api.mdb.get_tag_info(t)
            if tb is None:
                returnValue(None)
            returnValue(tb.current_batch.key)

        self.assertEqual((yield tag_batch(tag)), None)

        batch_id = yield self.api.batch_start([tag])
        self.assertEqual((yield tag_batch(tag)), batch_id)

        yield self.api.batch_done(batch_id)
        self.assertEqual((yield tag_batch(tag)), None)

    @inlineCallbacks
    def test_send_command(self):
        for addr in ["+12", "+34"]:
            yield self.api.send_command(
                    "dummy_worker", "send",
                    batch_id="b123", content="Hello!",
                    msg_options={'from_addr': '+56'}, to_addr=addr)

        [cmd1, cmd2] = self.get_dispatcher_commands()
        self.assertEqual(cmd1.payload['kwargs']['to_addr'], '+12')
        self.assertEqual(cmd2.payload['kwargs']['to_addr'], '+34')


class TestVumiApi(TestTxVumiApi):
    sync_persistence = True


class TestTxVumiUserApi(AppWorkerTestCase):
    @inlineCallbacks
    def setUp(self):
        yield super(TestTxVumiUserApi, self).setUp()
        if self.sync_persistence:
            self.api = VumiApi.from_config_sync(self._persist_config)
        else:
            self.api = yield VumiApi.from_config_async(self._persist_config)
        self.user_account = yield self.mk_user(self.api, u'Buster')
        self.user_api = VumiUserApi(self.api, self.user_account.key)

    @inlineCallbacks
    def test_optout_filtering(self):
        group = yield self.user_api.contact_store.new_group(u'test-group')
        optout_store = OptOutStore.from_user_account(self.user_account)
        contact_store = ContactStore.from_user_account(self.user_account)

        # Create two random contacts
        yield self.user_api.contact_store.new_contact(
            msisdn=u'+27761234567', groups=[group.key])
        yield self.user_api.contact_store.new_contact(
            msisdn=u'+27760000000', groups=[group.key])

        conv = yield self.user_api.new_conversation(
            u'bulk_message', u'subject', u'message', delivery_class=u'sms')
        conv = self.user_api.wrap_conversation(conv)
        conv.add_group(group)
        yield conv.save()

        # Opt out the first contact
        yield optout_store.new_opt_out(u'msisdn', u'+27761234567', {
            'message_id': u'the-message-id'
        })
        contact_keys = yield contact_store.get_contacts_for_conversation(conv)
        all_addrs = []
        for contacts in contact_store.contacts.load_all_bunches(contact_keys):
            for contact in (yield contacts):
                all_addrs.append(contact.addr_for(conv.delivery_class))
        self.assertEqual(set(all_addrs), set(['+27760000000', '+27761234567']))
        optedin_addrs = []
        for contacts in (yield conv.get_opted_in_contact_bunches()):
            for contact in (yield contacts):
                optedin_addrs.append(contact.addr_for(conv.delivery_class))
        self.assertEqual(optedin_addrs, ['+27760000000'])

    @inlineCallbacks
    def test_exists(self):
        self.assertTrue((yield self.api.user_exists(self.user_account.key)))
        self.assertTrue((yield self.user_api.exists()))

        self.assertFalse((yield self.api.user_exists('foo')))
        self.assertFalse((yield VumiUserApi(self.api, 'foo').exists()))

    @inlineCallbacks
    def test_list_conversation_endpoints(self):
        tag1 = (u'pool1', u'1234')
        tag2 = (u'pool1', u'5678')
        tag3 = (u'pool1', u'9012')
        yield self.api.tpm.declare_tags([tag1, tag2, tag3])
        yield self.user_api.acquire_specific_tag(tag2)
        yield self.user_api.new_conversation(
            u'bulk_message', u'subject', u'message', delivery_class=u'sms',
            delivery_tag_pool=tag1[0], delivery_tag=tag1[1])
        endpoints = yield self.user_api.list_conversation_endpoints()
        self.assertEqual(endpoints, set([tag1]))

    @inlineCallbacks
    def test_list_endpoints(self):
        tag1 = (u'pool1', u'1234')
        tag2 = (u'pool1', u'5678')
        yield self.api.tpm.declare_tags([tag1, tag2])
        yield self.add_tagpool_permission(u'pool1')
        yield self.user_api.acquire_specific_tag(tag1)
        endpoints = yield self.user_api.list_endpoints()
        self.assertEqual(endpoints, set([tag1]))

    @inlineCallbacks
    def test_msg_options(self):
        tag = ('pool1', '1234')
        yield self.api.tpm.declare_tags([tag])
        yield self.api.tpm.set_metadata(tag[0], {
            'transport_type': 'dummy_transport',
            'msg_options': {'opt1': 'bar'},
        })
        msg_options = yield self.user_api.msg_options(tag)
        self.assertEqual(msg_options, {
            'from_addr': '1234',
            'helper_metadata': {
                'go': {'user_account': 'test-0-user'},
                'tag': {'tag': ['pool1', '1234']},
            },
            'transport_type': 'dummy_transport',
            'opt1': 'bar',
        })

    @inlineCallbacks
    def test_msg_options_with_tagpool_metadata(self):
        tag = ('pool1', '1234')
        tagpool_metadata = {
            'transport_type': 'dummy_transport',
            'msg_options': {'opt1': 'bar'},
        }
        msg_options = yield self.user_api.msg_options(tag, tagpool_metadata)
        self.assertEqual(msg_options, {
            'from_addr': '1234',
            'helper_metadata': {
                'go': {'user_account': 'test-0-user'},
                'tag': {'tag': ['pool1', '1234']},
            },
            'transport_type': 'dummy_transport',
            'opt1': 'bar',
        })

    @inlineCallbacks
    def assert_account_tags(self, expected):
        user_account = yield self.user_api.get_user_account()
        self.assertEqual(expected, user_account.tags)

    @inlineCallbacks
    def test_declare_acquire_and_release_tags(self):
        tag1, tag2 = ("poolA", "tag1"), ("poolA", "tag2")
        yield self.api.tpm.declare_tags([tag1, tag2])
        yield self.add_tagpool_permission(u"poolA")
        yield self.add_tagpool_permission(u"poolB")

        yield self.assert_account_tags([])
        self.assertEqual((yield self.user_api.acquire_tag(u"poolA")), tag1)
        self.assertEqual((yield self.user_api.acquire_tag(u"poolA")), tag2)
        self.assertEqual((yield self.user_api.acquire_tag(u"poolA")), None)
        self.assertEqual((yield self.user_api.acquire_tag(u"poolB")), None)
        yield self.assert_account_tags([list(tag1), list(tag2)])

        yield self.user_api.release_tag(tag2)
        yield self.assert_account_tags([list(tag1)])
        self.assertEqual((yield self.user_api.acquire_tag(u"poolA")), tag2)
        self.assertEqual((yield self.user_api.acquire_tag(u"poolA")), None)
        yield self.assert_account_tags([list(tag1), list(tag2)])


class TestVumiUserApi(TestTxVumiUserApi):
    sync_persistence = True


class TestVumiApiCommand(TestCase):
    def test_default_routing_config(self):
        cfg = VumiApiCommand.default_routing_config()
        self.assertEqual(cfg, {
            'exchange': 'vumi',
            'exchange_type': 'direct',
            'routing_key': 'vumi.api',
            'durable': True,
            })


class TestVumiApiEvent(TestCase):
    def test_default_routing_config(self):
        cfg = VumiApiEvent.default_routing_config()
        self.assertEqual(cfg, {
            'exchange': 'vumi',
            'exchange_type': 'direct',
            'routing_key': 'vumi.event',
            'durable': True,
            })

    def test_event(self):
        event = VumiApiEvent.event(
            'me', 'my_conv', 'my_event', {"foo": "bar"})
        self.assertEqual(event['account_key'], 'me')
        self.assertEqual(event['conversation_key'], 'my_conv')
        self.assertEqual(event['event_type'], 'my_event')
        self.assertEqual(event['content'], {"foo": "bar"})
