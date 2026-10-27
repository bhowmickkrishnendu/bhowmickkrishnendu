import base64
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from urllib.error import HTTPError

from update_pr_status import START, END, ci_status, publish, render_table, replace_section


def example():
    return {
        'project': 'Example', 'number': 1, 'title': 'Fix values | defaults [test]',
        'url': 'https://github.com/example/project/pull/1', 'state': 'OPEN',
        'isDraft': False, 'reviewDecision': 'REVIEW_REQUIRED',
        'updatedAt': '2026-10-27T00:00:00Z', 'labels': {'nodes': []},
        'commits': {'nodes': [{'commit': {'statusCheckRollup': {'state': 'SUCCESS'}}}]},
    }


class StatusTests(unittest.TestCase):
    def test_stale_passing_is_distinct_from_failure(self):
        pr = example()
        pr['labels']['nodes'] = [{'name': 'stale_ci'}]
        self.assertEqual(ci_status(pr), '⚠️ Stale · passing')
        pr['commits']['nodes'][0]['commit']['statusCheckRollup']['state'] = 'FAILURE'
        self.assertEqual(ci_status(pr), '❌ Failing · stale CI')

    def test_no_checks_is_not_success(self):
        pr = example()
        pr['commits']['nodes'][0]['commit']['statusCheckRollup'] = None
        self.assertEqual(ci_status(pr), '— No checks')

    def test_pending_checks(self):
        pr = example()
        pr['commits']['nodes'][0]['commit']['statusCheckRollup']['state'] = 'PENDING'
        self.assertEqual(ci_status(pr), '⏳ Pending')

    def test_terminal_prs_do_not_look_like_pending_reviews(self):
        for state, expected in [('MERGED', '🟣 Merged'), ('CLOSED', '⚪ Closed')]:
            pr = example()
            pr.update(state=state, isDraft=True)
            table = render_table([pr])
            self.assertIn(expected, table)
            self.assertNotIn('Review required', table)
            self.assertNotIn('📝 Draft', table)

    def test_title_cannot_add_table_columns(self):
        table = render_table([example()])
        row = table.splitlines()[-1]
        self.assertEqual(row.count('|'), 7)
        self.assertIn('&#124;', row)
        self.assertIn(r'\[test\]', row)
        self.assertIn('27 Oct 2026, 05:30', row)


class UpdateTests(unittest.TestCase):
    original = f'About me\nBlog posts\n{START}\n{END}\nSocial links\n'
    table = '| Project | Status |\n| --- | --- |\n| Example | Open |'

    def test_other_readme_sections_are_preserved(self):
        updated = replace_section(self.original, self.table)
        self.assertTrue(updated.startswith('About me\nBlog posts\n' + START))
        self.assertTrue(updated.endswith(END + '\nSocial links\n'))

    def test_no_timestamp_only_commits(self):
        first = replace_section(self.original, self.table, datetime(2026, 1, 1, tzinfo=timezone.utc))
        again = replace_section(first, self.table, datetime(2026, 10, 27, tzinfo=timezone.utc))
        self.assertEqual(first, again)

    def test_unsafe_marker_layouts_are_rejected(self):
        for text in ['README with no markers', f'{END}\n{START}', self.original + START]:
            with self.assertRaises(ValueError):
                replace_section(text, self.table)

    def test_concurrent_blog_update_is_preserved_on_retry(self):
        original = self.original
        table = self.table

        class API:
            writes = []
            reads = 0

            def request(self, path, method='GET', data=None):
                if method == 'GET':
                    self.reads += 1
                    text = original if self.reads == 1 else original.replace('Blog posts', 'New blog post')
                    return {'content': base64.b64encode(text.encode()).decode(), 'sha': str(self.reads)}
                if not self.writes:
                    self.writes.append(deepcopy(data))
                    raise HTTPError(path, 409, 'conflict', {}, None)
                self.writes.append(deepcopy(data))
                return {}

        api = API()
        self.assertTrue(publish(api, 'example/profile', 'main', table))
        self.assertEqual(api.writes[-1]['sha'], '2')
        self.assertIn('New blog post', base64.b64decode(api.writes[-1]['content']).decode())

    def test_unchanged_status_does_not_write(self):
        updated = replace_section(self.original, self.table)

        class API:
            def request(self, path, method='GET', data=None):
                if method != 'GET':
                    raise AssertionError('Unchanged table must not be committed.')
                return {'content': base64.b64encode(updated.encode()).decode(), 'sha': 'unchanged'}

        self.assertFalse(publish(API(), 'example/profile', 'main', self.table))


if __name__ == '__main__':
    unittest.main()
