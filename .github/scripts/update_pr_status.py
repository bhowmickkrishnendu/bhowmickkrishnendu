"""Refresh only the tracked-PR section of the profile README."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
START = '<!-- PR-STATUS:START -->'
END = '<!-- PR-STATUS:END -->'
IST = timezone(timedelta(hours=5, minutes=30), 'IST')
QUERY = '''
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      title url state isDraft reviewDecision updatedAt
      labels(first: 100) { nodes { name } }
      commits(last: 1) {
        nodes { commit { statusCheckRollup { state } } }
      }
    }
  }
}
'''


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, path, method='GET', data=None):
        request = Request(
            'https://api.github.com/' + path,
            data=json.dumps(data).encode() if data is not None else None,
            method=method,
            headers={
                'Authorization': f'Bearer {self.token}',
                'Accept': 'application/vnd.github+json',
                'Content-Type': 'application/json',
                'User-Agent': 'profile-pr-status',
                'X-GitHub-Api-Version': '2022-11-28',
            },
        )
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    def pull_request(self, tracked):
        result = self.request('graphql', 'POST', {
            'query': QUERY,
            'variables': {key: tracked[key] for key in ('owner', 'repo', 'number')},
        })
        if result.get('errors'):
            raise ValueError('GitHub could not read the tracked PR; README was not updated.')
        repository = result.get('data', {}).get('repository')
        pr = repository.get('pullRequest') if repository else None
        if not pr:
            raise ValueError('A tracked PR was not found; README was not updated.')
        return {**pr, 'project': tracked['project'], 'number': tracked['number']}


def cell(value):
    value = html.escape(' '.join(str(value).split()), quote=False).replace('|', '&#124;')
    return re.sub(r'([\\`*_[\]])', r'\\\1', value)


def ci_status(pr):
    commits = pr['commits']['nodes']
    rollup = commits[-1]['commit']['statusCheckRollup'] if commits else None
    state = rollup['state'] if rollup else None
    text = {
        'SUCCESS': '✅ Passing', 'FAILURE': '❌ Failing', 'ERROR': '❌ Error',
        'PENDING': '⏳ Pending', 'EXPECTED': '⏳ Expected', None: '— No checks',
    }.get(state, '❔ Unknown')
    labels = {label['name'].lower().replace('-', '_') for label in pr['labels']['nodes']}
    if 'stale_ci' in labels:
        text = '⚠️ Stale · passing' if state == 'SUCCESS' else text + ' · stale CI'
    return text


def render_table(prs):
    rows = [
        '| Project | Pull request | Status | Review | CI | Last activity (IST) |',
        '| --- | --- | --- | --- | --- | --- |',
    ]
    for pr in prs:
        state = pr['state']
        status = {'OPEN': '🟢 Open', 'MERGED': '🟣 Merged', 'CLOSED': '⚪ Closed'}[state]
        if state == 'OPEN' and pr['isDraft']:
            status = '📝 Draft'
        review = {
            'APPROVED': '✅ Approved', 'CHANGES_REQUESTED': '🛠 Changes requested',
            'REVIEW_REQUIRED': '⏳ Review required', None: '⏳ Awaiting decision',
        }.get(pr['reviewDecision'], 'No decision')
        if state != 'OPEN':
            review = 'Complete' if state == 'MERGED' else '—'
        updated = datetime.fromisoformat(pr['updatedAt'].replace('Z', '+00:00')).astimezone(IST)
        rows.append(
            f"| {cell(pr['project'])} | [#{pr['number']} · {cell(pr['title'])}]({pr['url']}) "
            f"| {status} | {review} | [{ci_status(pr)}]({pr['url']}/checks) "
            f"| {updated:%d %b %Y, %H:%M} |"
        )
    return '\n'.join(rows)


def replace_section(readme, table, now=None):
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ValueError('README must contain exactly one pair of PR-STATUS markers.')
    start = readme.index(START) + len(START)
    end = readme.index(END)
    if end < start:
        raise ValueError('README PR-STATUS markers are out of order.')
    existing = readme[start:end].split('\n\n_Last table update:', 1)[0].strip()
    if existing == table.strip():
        return readme
    now = (now or datetime.now(IST)).astimezone(IST)
    body = f'\n{table}\n\n_Last table update: {now:%d %b %Y, %H:%M IST}._\n'
    return readme[:start] + body + readme[end:]


def publish(api, repository, branch, table):
    # Re-read immediately before writing and use the content SHA so a concurrent
    # blog-post refresh cannot be silently overwritten.
    endpoint = f'repos/{repository}/contents/README.md'
    for attempt in range(3):
        file = api.request(endpoint + '?' + urlencode({'ref': branch}))
        original = base64.b64decode(file['content']).decode('utf-8')
        updated = replace_section(original, table)
        if updated == original:
            print('Tracked PRs unchanged; no README commit needed.')
            return False
        try:
            api.request(endpoint, 'PUT', {
                'message': 'docs: refresh tracked pull request status',
                'content': base64.b64encode(updated.encode('utf-8')).decode(),
                'sha': file['sha'], 'branch': branch,
            })
            print('Updated the tracked-PR table in README.md.')
            return True
        except HTTPError as error:
            if error.code != 409 or attempt == 2:
                raise
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Print the table without writing GitHub or local files.')
    args = parser.parse_args()
    token = os.environ.get('GH_TOKEN')
    if not token:
        raise ValueError('GH_TOKEN is required.')
    api = GitHub(token)
    tracked = json.loads((ROOT / '.github/tracked-prs.json').read_text(encoding='utf-8'))
    if not tracked:
        raise ValueError('At least one tracked PR is required.')
    # Fetch everything before any write. API failures preserve the last good table.
    table = render_table([api.pull_request(pr) for pr in tracked])
    if args.dry_run:
        print(table)
        return
    repository = os.environ['PROFILE_REPOSITORY']
    branch = os.environ.get('PROFILE_BRANCH', 'main')
    publish(api, repository, branch, table)


if __name__ == '__main__':
    try:
        main()
    except HTTPError as error:
        print(f'GitHub API returned HTTP {error.code}; no successful update was recorded.', file=sys.stderr)
        sys.exit(1)
    except (ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
