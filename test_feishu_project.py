"""No network/model/browser: verify personal edits with an in-memory MCP fixture."""
import copy
import json
import os
import unittest
import queue
from types import SimpleNamespace
from unittest.mock import Mock, patch
from feishu_project import FeishuProject, MCPClient, ScheduleError, PROJECT, day, timestamp, number


class FakeMCP:
    def __init__(self):
        self.calls = []
        self.user_key = 'me'
        self.update_error = False
        self.mismatch = False
        self.collateral = False
        self.node = dict(basic=dict(node_key='state_2', node_uuid='node_state_2_123', name='前端开发', status='doing'),
            assignees=dict(owners=[dict(user_key='me'), dict(user_key='other')], node_owner_mode=3),
            assignee_schedule_list=[dict(assignee=dict(user_key='me'), schedule_info=dict(
                estimate_start_time=timestamp('2026-10-19'), estimate_finish_time=timestamp('2026-10-20'),
                points=1, actual_work_time=0)), dict(assignee=dict(user_key='other'), schedule_info=dict(
                estimate_start_time=timestamp('2026-10-22'), estimate_finish_time=timestamp('2026-10-23'),
                points=2, actual_work_time=3))])
        self.node_pages = None
        self.pagination_overrides = {}

    def call(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == 'search_user_info':
            return [dict(user_key=self.user_key, name_cn='测试用户')]
        if name == 'get_workitem_brief':
            return dict(work_item_attribute=dict(owned_project=dict(key=PROJECT), work_item_type=dict(key='story'),
                                                 work_item_name='真实需求名称'))
        if name == 'get_node_detail':
            pages = self.node_pages or [[self.node]]
            page = arguments['page_num']
            pagination = dict(has_more=page < len(pages), page_num=page, page_size=20,
                              total=sum(len(nodes) for nodes in pages))
            pagination.update(self.pagination_overrides.get(page, {}))
            return dict(list=copy.deepcopy(pages[page - 1]), pagination=pagination)
        if name == 'list_schedule':
            return dict(user_workload_list=[dict(user_info=dict(userKey=self.user_key), total_score=0,
                total_unscheduled_task=2, tasks=[dict(work_item_info=dict(id=123, name='测试需求'),
                    state=dict(state_id='state_2', state_name='前端开发', passed=False),
                    time=dict(start='2026-10-19 00:00:00', end='2026-10-20 23:59:59', duration=0))])])
        if name == 'update_node':
            if self.update_error:
                raise ScheduleError('unknown write result')
            assert set(arguments) == {'project_key', 'work_item_id', 'node_id', 'schedules'}
            schedule = arguments['schedules'][0]
            assert schedule['owners'] == ['me']
            assert schedule['is_auto'] is False
            if not self.mismatch:
                record = self.node['assignee_schedule_list'][0]['schedule_info']
                mapping = dict(estimate_start_date='estimate_start_time', estimate_end_date='estimate_finish_time',
                               points='points', actual_work_time='actual_work_time')
                for source, target in mapping.items():
                    if source in schedule:
                        record[target] = schedule[source]
                    elif schedule['clear_schedule']:
                        record[target] = None
            if self.collateral:
                self.node['assignee_schedule_list'][1]['schedule_info']['points'] = 99
            return dict(success=True)
        raise AssertionError(name)

    def close(self):
        pass


class ConfigMCP:
    def __init__(self):
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == 'search_project_info':
            return dict(projects=[dict(project_key='700', name='第二空间', simple_name='space-b')],
                        pagination=dict(page_num=1, total=1, has_more=False))
        if name == 'list_workitem_types':
            return dict(list=[dict(type_key='feature', name='需求', api_name='ticket'),
                              dict(type_key='version', name='版本', api_name='release')])
        if name == 'list_workitem_field_config':
            fields = ([dict(field_key='planned_release', field_name='规划版本', field_type='workitem_related_multi_select',
                            related_work_item_info=[dict(project_key='700', work_item_type='version')])]
                      if arguments['work_item_type'] == 'feature' else
                      [dict(field_key='schedule_date', field_name='上线时间', field_type='date')])
            return dict(list=fields, pagination=dict(page_num=arguments['page_num'], total=len(fields)))
        if name == 'list_node_field_config':
            return dict(list=[dict(field_key='owner', field_name='负责人', field_type='multi-user')])
        if name == 'get_workitem_brief':
            return dict(work_item_attribute=dict(owned_project=dict(key='700'),
                                                work_item_type=dict(key='feature'), work_item_name='示例需求'))
        if name == 'get_node_detail':
            return dict(list=[dict(basic=dict(node_key='dev', node_uuid='node-dev', name='开发'),
                                   assignees=dict(owners=[dict(user_key='me')]))],
                        pagination=dict(has_more=False, page_num=1, total=1))
        if name == 'search_user_info':
            return [dict(user_key='me', name_cn='当前用户')]
        if name == 'search_by_mql':
            return dict(data={})
        raise AssertionError(name)

    def close(self):
        pass


class ConfigurationTests(unittest.TestCase):
    def test_disabled_project_discovers_then_validates_real_metadata(self):
        client = ConfigMCP()
        service = FeishuProject(client, config={})
        with self.assertRaisesRegex(ScheduleError, '未关联飞书'):
            service.handle('overview', dict(start='2026-10-26', end='2026-10-29'))
        self.assertEqual(client.calls, [])
        listing = service.metadata(dict(space_id=''))
        self.assertIsNone(listing['selected_space'])
        self.assertEqual(listing['spaces'][0]['project_url'], 'https://project.feishu.cn/space-b')
        metadata = service.metadata(dict(space_id='700', work_item_type='feature',
            version_field='planned_release', sample_work_item_id='22'))
        self.assertEqual(metadata['selected_space']['simple_name'], 'space-b')
        self.assertEqual(metadata['nodes'][0]['name'], '开发')
        config = service.validate_config(dict(enabled=True, space_id='700', work_item_type='feature',
            version_field='planned_release', release_date_field='schedule_date', owner_field='owner',
            node_name='开发', sample_work_item_id='22'))
        self.assertEqual(config['project_url'], 'https://project.feishu.cn/space-b')
        self.assertEqual(config['work_item_api'], 'ticket')
        self.assertEqual(config['version_api'], 'release')
        enabled = FeishuProject(client, config)
        self.assertEqual(enabled.requirement(dict(work_item_id='22'))['url'],
                         'https://project.feishu.cn/space-b/ticket/detail/22')
        self.assertEqual(enabled.release_overview(dict(start='2026-10-26', end='2026-10-29'))['rows'], [])
        query = next(args['mql'] for name, args in client.calls if name == 'search_by_mql')
        self.assertIn('FROM `space-b`.`ticket`', query)
        self.assertIn('`planned_release`', query)
        self.assertIn('`schedule_date<target:700::version>`', query)

    def test_unverified_space_and_query_values_are_rejected(self):
        client = ConfigMCP()
        service = FeishuProject(client, config={})
        valid = dict(enabled=True, space_id='700', work_item_type='feature', version_field='planned_release',
                     release_date_field='schedule_date', owner_field='owner', node_name='开发', sample_work_item_id='22')
        for changes in (dict(space_id='701'), dict(project_url='https://evil.example/project'),
                        dict(version_field='bad'), dict(node_name="开发' OR true")):
            with self.subTest(changes=changes), self.assertRaises(ScheduleError):
                service.validate_config(dict(valid, **changes))
        with self.assertRaises(ScheduleError):
            FeishuProject(client, dict(enabled=True, space_id='700', space_alias='x` OR true'))


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeMCP()
        self.service = FeishuProject(self.client)
        self.target = dict(work_item_id='123', node_id='node_state_2_123')

    def preview(self, **changes):
        detail = self.service.detail(self.target)
        values = {**detail['before'], **changes}
        return self.service.preview(dict(**self.target, fingerprint=detail['fingerprint'], values=values))

    def write_calls(self):
        return [args for name, args in self.client.calls if name == 'update_node']

    def test_update_only_me_and_changed_fields(self):
        other = copy.deepcopy(self.client.node['assignee_schedule_list'][1])
        preview = self.preview(points='0', actual='1.5')
        args = self.service.plans[preview['plan']]['schedule']
        self.assertEqual(args, dict(owners=['me'], clear_schedule=False, is_auto=False, points=0, actual_work_time=1.5))
        result = self.service.save({'plan': preview['plan']})
        self.assertTrue(result['saved'])
        self.assertEqual(self.client.node['assignee_schedule_list'][1], other)
        self.assertEqual(result['before']['points'], 0)
        self.assertEqual(len(self.write_calls()), 1)
        with self.assertRaises(ScheduleError):
            self.service.save({'plan': preview['plan']})

    def test_clear_dates_preserves_points_actual_zero(self):
        preview = self.preview(start='', end='')
        schedule = self.service.plans[preview['plan']]['schedule']
        self.assertEqual(schedule, dict(owners=['me'], clear_schedule=True, is_auto=False, points=1, actual_work_time=0))
        self.assertEqual(self.service.save({'plan': preview['plan']})['before'],
                         dict(start='', end='', points=1, actual=0))

    def test_clear_point_keeps_dates_and_actual(self):
        preview = self.preview(points='')
        schedule = self.service.plans[preview['plan']]['schedule']
        self.assertNotIn('points', schedule)
        self.assertEqual(schedule['estimate_start_date'], timestamp('2026-10-19'))
        self.assertEqual(schedule['actual_work_time'], 0)
        self.assertIsNone(self.service.save({'plan': preview['plan']})['before']['points'])

    def test_clear_all(self):
        preview = self.preview(start='', end='', points='', actual='')
        self.assertEqual(self.service.plans[preview['plan']]['schedule'],
                         dict(owners=['me'], clear_schedule=True, is_auto=False))
        self.service.save({'plan': preview['plan']})

    def test_preserve_planned_period_on_clear(self):
        self.client.node['assignee_schedule_list'][0]['schedule_info']['planned_construction_period'] = 2
        preview = self.preview(start='', end='')
        self.assertEqual(self.service.plans[preview['plan']]['schedule']['planned_construction_period'], 2)

    def test_stale_edit_form_rejected(self):
        detail = self.service.detail(self.target)
        self.client.node['assignee_schedule_list'][0]['schedule_info']['points'] = 4
        with self.assertRaisesRegex(ScheduleError, '表单后排期已变化'):
            self.service.preview(dict(**self.target, fingerprint=detail['fingerprint'], values={**detail['before'], 'points': 2}))
        self.assertFalse(self.write_calls())

    def test_stale_plan_rejected_without_write(self):
        preview = self.preview(points=2)
        self.client.node['assignee_schedule_list'][0]['schedule_info']['points'] = 4
        with self.assertRaisesRegex(ScheduleError, '发生变化'):
            self.service.save({'plan': preview['plan']})
        self.assertFalse(self.write_calls())

    def test_identity_changed_rejected(self):
        preview = self.preview(points=2)
        self.client.user_key = 'other'
        with self.assertRaises(ScheduleError):
            self.service.save({'plan': preview['plan']})
        self.assertFalse(self.write_calls())

    def test_non_owner_rejected(self):
        self.client.user_key = 'outsider'
        with self.assertRaisesRegex(ScheduleError, '本人负责'):
            self.service.detail(self.target)

    def test_shared_aggregate_rejected(self):
        self.client.node['assignee_schedule_list'] = []
        with self.assertRaisesRegex(ScheduleError, '多人共享'):
            self.service.detail(self.target)

    def test_bad_inputs_and_no_changes(self):
        for patch in [dict(start='2026-10-21', end='2026-10-20'), dict(start=''), dict(points=-1),
                      dict(points='nan'), dict(actual='inf'), dict(points=True), dict(start='2026-02-30'),
                      dict(start=1792425600000), dict(start='2026-10-19garbage'), {}]:
            with self.subTest(patch=patch), self.assertRaises(ScheduleError):
                self.preview(**patch)
        self.assertFalse(self.write_calls())

    def test_expired_plan_rejected(self):
        preview = self.preview(points=2)
        self.service.plans[preview['plan']]['expires'] = 0
        with self.assertRaises(ScheduleError):
            self.service.save({'plan': preview['plan']})
        self.assertFalse(self.write_calls())

    def test_no_automatic_retry_when_write_uncertain(self):
        preview = self.preview(points=2)
        self.client.update_error = True
        with self.assertRaisesRegex(ScheduleError, '结果未确认'):
            self.service.save({'plan': preview['plan']})
        with self.assertRaises(ScheduleError):
            self.service.save({'plan': preview['plan']})
        self.assertEqual(len(self.write_calls()), 1)

    def test_readback_mismatch_is_not_success(self):
        preview = self.preview(points=2)
        self.client.mismatch = True
        with self.assertRaisesRegex(ScheduleError, '回读与预期不一致'):
            self.service.save({'plan': preview['plan']})
        self.assertEqual(len(self.write_calls()), 1)

    def test_other_person_change_is_not_reported_as_success(self):
        preview = self.preview(points=2)
        self.client.collateral = True
        with self.assertRaisesRegex(ScheduleError, '其他人员排期发生变化'):
            self.service.save({'plan': preview['plan']})
        self.assertEqual(len(self.write_calls()), 1)

    def test_dates_and_range(self):
        self.assertEqual(day(timestamp('2026-10-19')), '2026-10-19')
        self.assertEqual(day('1970-01-01 08:00:00'), '')
        self.assertEqual(day('2026-10-18T16:00:00Z'), '2026-10-19')
        self.assertEqual(timestamp('2026-10-19'), 1792339200000)
        self.assertEqual(number(0), 0)
        for start, end in [('2026-10-20', '2026-10-19'), ('2026-01-01', '2026-04-02')]:
            with self.assertRaises(ScheduleError):
                self.service.overview(dict(start=start, end=end))

    def test_overview(self):
        result = self.service.overview(dict(start='2026-10-19', end='2026-10-25'))
        self.assertEqual(result['rows'][0]['points'], 0)
        self.assertEqual(result['user']['key'], 'me')
        self.assertEqual(set(result), {'user', 'project', 'project_url', 'space_id', 'work_item_type', 'node_name', 'rows', 'total_points'})
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual([name for name, _ in self.client.calls], ['search_user_info', 'list_schedule'])

    def test_detail_link_and_node_uuid_resolution(self):
        detail = self.service.detail(self.target)
        self.assertEqual(detail['node_id'], 'state_2')
        self.assertEqual(detail['url'], 'https://project.feishu.cn/platform/story/detail/123')
        self.assertTrue(detail['fingerprint'])

    def test_node_pages_are_read_before_editing(self):
        review = copy.deepcopy(self.client.node)
        review['basic'].update(node_key='review', node_uuid='uuid_review', name='代码评审')
        self.client.node_pages = [[review], [self.client.node]]
        detail = self.service.detail(self.target)
        self.assertEqual(detail['node_id'], 'state_2')
        calls = [args for name, args in self.client.calls if name == 'get_node_detail']
        self.assertEqual([args['page_num'] for args in calls], [1, 2])
        self.assertTrue(all(args['node_id_list'] == ['_all'] and args['need_sub_task'] is False for args in calls))

    def test_inconsistent_node_pagination_is_rejected(self):
        for metadata in [dict(total=21), dict(page_num=2)]:
            with self.subTest(metadata=metadata):
                self.client.pagination_overrides[1] = metadata
                with self.assertRaises(ScheduleError):
                    self.service.detail(self.target)
        self.assertFalse(self.write_calls())

    def test_missing_or_duplicate_node_identity_is_rejected(self):
        self.client.node_pages = [[self.client.node], [self.client.node]]
        with self.assertRaisesRegex(ScheduleError, '标识缺失或重复'):
            self.service.detail(self.target)
        self.client.node_pages = None
        del self.client.node['basic']['node_key']
        with self.assertRaisesRegex(ScheduleError, '标识缺失或重复'):
            self.service.detail(self.target)
        self.assertFalse(self.write_calls())

    def test_requirement_uses_verified_title_and_platform_story_only(self):
        item = self.service.requirement(dict(work_item_id='123', title='untrusted browser title'))
        self.assertEqual(item['title'], '真实需求名称')
        self.assertEqual(item['url'], 'https://project.feishu.cn/platform/story/detail/123')
        for attributes in [dict(owned_project=dict(key='other'),work_item_type=dict(key='story')),
                           dict(owned_project=dict(key=PROJECT),work_item_type=dict(key='issue')),
                           dict(owned_project=dict(key=PROJECT),work_item_type=dict(key='story'))]:
            with patch.object(self.client, 'call', return_value=dict(work_item_attribute=attributes)):
                with self.assertRaises(ScheduleError):
                    self.service.requirement({'work_item_id':'123'})

    def test_invalid_ids_and_unknown_routes(self):
        for identifier in ['../123', '123;echo', 123, '１２３', '']:
            with self.assertRaises(ScheduleError):
                self.service.detail(dict(work_item_id=identifier, node_id='state_2'))
        before = len(self.client.calls)
        for route in ('delete', 'todos', 'unscheduled'):
            with self.subTest(route=route), self.assertRaises(ScheduleError):
                self.service.handle(route, {})
        self.assertEqual(len(self.client.calls), before)


class ReleaseOverviewTests(unittest.TestCase):
    @staticmethod
    def page(rows, total=None, continuation=False):
        entries = []
        for row in rows:
            fields = []
            for key, value in row.items():
                kind = 'key_label_value_list' if isinstance(value, list) else 'long_value' if isinstance(value, int) else 'string_value'
                fields.append(dict(key=key, value_type=kind, value={kind: value} if value is not None else None))
            entries.append(dict(moql_field_list=fields))
        result = dict(data={'1': entries})
        if not continuation:
            result.update(list=[dict(group_infos=[dict(group_id='1')], count=len(rows) if total is None else total)], session_id='query-session')
        return result

    def service(self, responses):
        client = Mock()
        client.call.side_effect = [[dict(user_key='me', name_cn='测试用户')], *responses]
        return FeishuProject(client)

    def test_release_dates_are_independent_of_development_schedule(self):
        service = self.service([
            self.page([dict(work_item_id=123, name='往周完成的需求')]),
            self.page([dict(work_item_id=123, planning_version=[dict(key='10', label='本周版本'),
                dict(key='11', label='往期版本'), dict(key='12', label='未排版本')])]),
            self.page([dict(work_item_id=10, name='本周版本', schedule_publish_date='2026-10-26T16:00:00Z'),
                dict(work_item_id=11, name='往期版本', schedule_publish_date='2026-10-20'),
                dict(work_item_id=12, name='未排版本', schedule_publish_date=None)])])
        result = service.handle('release-overview', dict(start='2026-10-26', end='2026-11-01'))
        self.assertEqual(result, dict(user=dict(key='me', name='测试用户'), project='平台事业部',
            project_url='https://project.feishu.cn/platform', space_id=PROJECT,
            work_item_type='story', node_name='前端开发', rows=[
            dict(work_item_id='123', name='往周完成的需求', url='https://project.feishu.cn/platform/story/detail/123',
                 versions=[dict(id='10', name='本周版本', date='2026-10-27', status='ready')])]))
        query = service.client.call.call_args_list[1].args[1]['mql']
        self.assertIn("array_contains(get_node_attribute('前端开发','负责人'), current_login_user())", query)
        self.assertIn(f"any_relation_match(`planning_version`, x -> x.`schedule_publish_date<target:{PROJECT}::version>` >= '2026-10-26'", query)
        self.assertNotIn('list_schedule', [call.args[0] for call in service.client.call.call_args_list])
        self.assertTrue(all(call.args[0] in ('search_user_info', 'search_by_mql') for call in service.client.call.call_args_list))

    def test_reads_every_page_and_batches_version_details(self):
        stories = [dict(work_item_id=number, name=f'需求{number}') for number in range(1, 52)]
        links = [dict(work_item_id=number, planning_version=[dict(key='100', label='本周版本')]) for number in range(1, 52)]
        versions = self.page([dict(work_item_id=100, name='本周版本', schedule_publish_date='2026-10-29')])
        service = self.service([self.page(stories[:50], total=51), self.page(stories[50:], continuation=True),
                                self.page(links[:50]), versions, self.page(links[50:]), versions])
        result = service.release_overview(dict(start='2026-10-29', end='2026-10-29'))
        self.assertEqual([row['work_item_id'] for row in result['rows']], [str(number) for number in range(1, 52)])
        self.assertEqual(service.client.call.call_args_list[2].args, ('search_by_mql',
            dict(project_key=PROJECT, session_id='query-session', group_pagination_list=[dict(group_id='1', page_num=2)])))
        self.assertEqual(service.client.call.call_count, 7)

    def test_empty_result_is_complete(self):
        service = self.service([dict(list=None, session_id='empty', data={})])
        self.assertEqual(service.release_overview(dict(start='2026-10-29', end='2026-10-29'))['rows'], [])
        self.assertEqual(service.client.call.call_count, 2)

    def test_incomplete_or_duplicate_results_are_rejected(self):
        rows = [dict(work_item_id=123, name='需求')]
        missing_session = self.page(rows * 50, total=51)
        del missing_session['session_id']
        for raw in [missing_session, self.page(rows, total=2), self.page(rows * 2),
                    dict(data={}, list=[dict(count=1, group_infos=[])])]:
            with self.subTest(raw=raw):
                service = self.service([raw])
                with self.assertRaises(ScheduleError):
                    service.release_overview(dict(start='2026-10-26', end='2026-11-01'))
                self.assertEqual(service.client.call.call_count, 2)
        service = self.service([self.page([dict(work_item_id=i, name='需求') for i in range(50)], total=51),
                                self.page([], continuation=True)])
        with self.assertRaisesRegex(ScheduleError, '未完整读取'):
            service.release_overview(dict(start='2026-10-26', end='2026-11-01'))

    def test_unavailable_release_dates_do_not_become_an_empty_success(self):
        service = self.service([self.page([dict(work_item_id=123, name='需求')]),
            self.page([dict(work_item_id=123, planning_version=[dict(key='10', label='版本')])]),
            self.page([dict(work_item_id=10, name='版本')])])
        with self.assertRaisesRegex(ScheduleError, '部分上线日期读取失败'):
            service.release_overview(dict(start='2026-10-26', end='2026-11-01'))

    def test_invalid_range_never_reads_feishu(self):
        service = self.service([])
        for start, end in [('2026-10-29', '2026-10-27'), ('2026-01-01', '2026-04-02'), ('bad', '2026-10-29')]:
            with self.subTest(start=start, end=end), self.assertRaises(ScheduleError):
                service.release_overview(dict(start=start, end=end))
        service.client.call.assert_not_called()


class MCPChecks(unittest.TestCase):
    def client(self, result):
        client = MCPClient()
        client.process = SimpleNamespace(poll=lambda: None)
        client.allowed, client.disabled = None, []
        client.rpc = Mock(return_value=result)
        client.close = Mock()
        return client

    def test_npx_uses_cached_package_without_changing_other_launchers(self):
        for command, args, expected in [
            ('npx', ['-y', '@lark-project/mcp'], ['npx', '--offline', '-y', '@lark-project/mcp']),
            ('/runtime/bin/npx', ['-y', '@lark-project/mcp'], ['/runtime/bin/npx', '--offline', '-y', '@lark-project/mcp']),
            ('node', ['/installed/mcp.js'], ['node', '/installed/mcp.js']),
        ]:
            with self.subTest(command=command):
                transport = dict(type='stdio', command=command, args=args, env={'MCP_USER_TOKEN': 'fixture-token'}, cwd='/fixture')
                config = dict(enabled=True, transport=transport, enabled_tools=['list_schedule'], disabled_tools=['update_node'])
                client = MCPClient()
                client.rpc, client.send = Mock(), Mock()
                with patch('feishu_project.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(config))), \
                     patch('feishu_project.subprocess.Popen') as spawn, patch('feishu_project.threading.Thread'):
                    client.connect()
                self.assertEqual(spawn.call_args.args[0], expected)
                self.assertEqual(spawn.call_args.kwargs['env']['MCP_USER_TOKEN'], 'fixture-token')
                self.assertEqual(spawn.call_args.kwargs['cwd'], '/fixture')
                self.assertEqual(client.allowed, ['list_schedule'])
                self.assertEqual(client.disabled, ['update_node'])
                client.rpc.assert_called_once_with('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                    'clientInfo': {'name': 'worktree-merge', 'version': '1.0'}})
                client.send.assert_called_once_with({'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {}})

    def test_decode_json_without_log_text(self):
        client = self.client(dict(content=[dict(type='text', text='log_id: fixture'),
                                           dict(type='text', text='{"rows":[]}')]))
        self.assertEqual(client.call('list_schedule', {}), {'rows': []})

    def test_mcp_uses_system_https_proxy_without_overwriting_explicit_settings(self):
        system = {'https': 'http://127.0.0.1:7890'}
        for parent, configured, proxies, bypass, expected_proxy, expected_flag in [
            ({}, {}, system, False, 'http://127.0.0.1:7890', '1'),
            ({}, {}, {}, False, None, None),
            ({}, {}, system, True, None, None),
            ({'HTTPS_PROXY': 'http://parent:8080'}, {}, system, False, 'http://parent:8080', '1'),
            ({}, {'HTTP_PROXY': 'http://explicit:8080'}, system, False, None, '1'),
            ({}, {'ALL_PROXY': 'socks5://explicit:1080'}, system, False, None, None),
            ({}, {'https_proxy': 'http://explicit:8080', 'NO_PROXY': 'localhost', 'NODE_OPTIONS': '--no-warnings'}, system, True, None, '1'),
            ({}, {'HTTPS_PROXY': ''}, system, False, '', None),
            ({}, {'HTTPS_PROXY': 'http://explicit:8080', 'NODE_USE_ENV_PROXY': '0'}, system, False, 'http://explicit:8080', '0'),
        ]:
            with self.subTest(parent=parent, configured=configured, proxies=proxies, bypass=bypass):
                configured = {'MCP_USER_TOKEN': 'fixture-token', **configured}
                config = dict(enabled=True, transport=dict(type='stdio', command='npx', args=['-y', '@lark-project/mcp'], env=configured))
                client = MCPClient()
                client.rpc, client.send = Mock(), Mock()
                with patch.dict(os.environ, parent, clear=True), \
                     patch('feishu_project.getproxies', return_value=proxies), \
                     patch('feishu_project.proxy_bypass', return_value=bypass), \
                     patch('feishu_project.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(config))), \
                     patch('feishu_project.subprocess.Popen') as spawn, patch('feishu_project.threading.Thread'):
                    client.connect()
                    self.assertEqual(dict(os.environ), parent)
                env = spawn.call_args.kwargs['env']
                self.assertEqual(env.get('HTTPS_PROXY'), expected_proxy)
                self.assertEqual(env.get('NODE_USE_ENV_PROXY'), expected_flag)
                for key, value in configured.items():
                    self.assertEqual(env[key], value)

    def test_tool_error_is_rejected_and_connection_closed(self):
        for result in [dict(isError=True), dict(content=[dict(type='text', text='error=PermissionDenied')])]:
            client = self.client(result)
            with self.assertRaises(ScheduleError):
                client.call('update_node', {})
            client.close.assert_called_once()
            client.rpc.assert_called_once()

    def test_configured_tool_allowlist_is_respected(self):
        client = self.client({})
        for allowed, disabled in [([], []), (None, ['update_node'])]:
            client.allowed, client.disabled = allowed, disabled
            with self.assertRaises(ScheduleError):
                client.call('update_node', {})
        client.rpc.assert_not_called()

    def test_timeout_does_not_retry_write(self):
        client = self.client({})
        client.rpc.side_effect = queue.Empty
        with self.assertRaisesRegex(ScheduleError, '勿重复提交'):
            client.call('update_node', {})
        client.rpc.assert_called_once()
        client.close.assert_called_once()

    def test_rpc_deadline_applies_even_with_notifications(self):
        client = MCPClient()
        client.send = Mock()
        with patch('feishu_project.time.monotonic', side_effect=[10, 56]):
            with self.assertRaises(queue.Empty):
                client.rpc('tools/call', {})

    def test_config_failure_does_not_echo_credentials(self):
        client = MCPClient()
        import subprocess
        with patch('feishu_project.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'codex', stderr='secret-token')):
            with self.assertRaises(ScheduleError) as result:
                client.connect()
        self.assertNotIn('secret-token', str(result.exception))


if __name__ == '__main__':
    unittest.main()
