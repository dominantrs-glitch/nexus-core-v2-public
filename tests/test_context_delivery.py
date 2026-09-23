import copy
import json
import unittest
import httpx2

from nexus.artifacts import InsufficientContext
from nexus.context_delivery import DeliveryGrant, build_context_server, project_delivery
from nexus.context_connector import ReviewedTask
from nexus.connector import local_response


class TaskFixture:
    project = 'synthetic-delivery'
    def __init__(self):
        self.view = dict(project={'state':'active', 'owner':'PRIVATE OWNER'},
            contract=dict(revision=1, goal='Synthetic task', acceptance={'x':['machine']},
                constraints=['synthetic only'], authority='confirmed', source='PRIVATE SOURCE'),
            context=[], personal={'items':[
                dict(id='selected', revision=1, text='Short reports.', authority='confirmed', binding=False),
                dict(id='hidden', revision=1, text='PRIVATE BODY', authority='candidate', binding=False)]},
            pending_correction={'body':'PRIVATE CORRECTION'})
    def read(self, operation): return copy.deepcopy(self.view)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.task = TaskFixture()
        self.grant = DeliveryGrant(self.task.project, 1, (('personal','selected',1),))

    async def test_only_reviewed_projection_and_zero_argument_tool(self):
        server = build_context_server(self.task, self.grant)
        listing = await server.list_tools()
        self.assertEqual([t.name for t in listing], ['read_project_context'])
        self.assertEqual(listing[0].input_schema.get('properties', {}), {})
        result = await server.call_tool('read_project_context', {})
        self.assertNotIn('PRIVATE', str(result))
        self.assertIn('Short reports.', str(result))
        self.assertFalse(project_delivery(self.task,self.grant)['records'][0]['binding'])

    async def test_revision_change_or_missing_permission_fails_closed(self):
        self.task.view['personal']['items'][0]['revision'] = 2
        with self.assertRaises(InsufficientContext): project_delivery(self.task, self.grant)
        result = await build_context_server(self.task,self.grant).call_tool('read_project_context', {})
        self.assertIn('insufficient_context', str(result))
        self.assertNotIn('PRIVATE', str(result))
        self.task.view['personal']['items'] = []
        with self.assertRaises(InsufficientContext): project_delivery(self.task,self.grant)

    async def test_project_and_contract_cannot_be_substituted(self):
        with self.assertRaises(InsufficientContext):
            project_delivery(self.task,DeliveryGrant('other-project',1))
        self.task.view['contract']['revision']=2
        with self.assertRaises(InsufficientContext): project_delivery(self.task,self.grant)

    async def test_preview_change_is_refused_without_new_approval(self):
        reviewed = ReviewedTask(self.task,self.grant,project_delivery(self.task,self.grant))
        self.assertEqual(project_delivery(reviewed,self.grant),project_delivery(self.task,self.grant))
        self.task.view['contract']['goal'] = 'Unreviewed goal'
        with self.assertRaises(InsufficientContext): project_delivery(reviewed,self.grant)

    async def test_real_sdk_http_reply_contains_only_projection(self):
        app=build_context_server(self.task,self.grant).streamable_http_app(json_response=True,stateless_http=True)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
                status,body=await local_response(client,{'protocol':'2025-11-25','body':json.dumps({
                    'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'read_project_context','arguments':{}}})})
        self.assertEqual(status,200)
        self.assertNotIn(b'PRIVATE',body)
        self.assertIn(b'Short reports.',body)


if __name__ == '__main__': unittest.main()
