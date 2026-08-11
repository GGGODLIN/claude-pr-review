from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock
from types import TracebackType
from typing import Any, Mapping

from bitbucket_pr_workflow.api import ApiTransportError


class JoinableThreadingHTTPServer(ThreadingHTTPServer):
  daemon_threads = False


@dataclass(frozen=True)
class CapturedRequest:
  method: str
  path: str
  raw: bytes
  json: Any


class FakeBitbucketServer:
  def __init__(self) -> None:
    self.routes: list[tuple[str, str, int, Any, dict[str, str]]] = []
    self.requests: list[CapturedRequest] = []
    self.errors: list[str] = []
    owner = self

    class Handler(BaseHTTPRequestHandler):
      def dispatch_request(self) -> None:
        try:
          owner.dispatch(self)
        except Exception as error:
          owner.errors.append(type(error).__name__)
          try:
            self.send_response(500)
            self.send_header('Content-Length', '0')
            self.end_headers()
          except OSError:
            return

      def do_GET(self) -> None:
        self.dispatch_request()

      def do_POST(self) -> None:
        self.dispatch_request()

      def do_PUT(self) -> None:
        self.dispatch_request()

      def log_message(self, _format: str, *_args: object) -> None:
        return

    self.server = JoinableThreadingHTTPServer(('127.0.0.1', 0), Handler)
    self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

  @property
  def base_url(self) -> str:
    host, port = self.server.server_address
    return f'http://{host}:{port}/2.0'

  def route(
    self,
    method: str,
    path: str,
    status: int,
    body: Any,
    headers: Mapping[str, str] | None = None,
  ) -> None:
    self.routes.append((method, path, status, body, dict(headers or {})))

  def dispatch(self, handler: BaseHTTPRequestHandler) -> None:
    length = int(handler.headers.get('Content-Length', '0'))
    payload = handler.rfile.read(length) if length else b''
    parsed = json.loads(payload.decode('utf-8')) if payload else None
    self.requests.append(CapturedRequest(handler.command, handler.path, payload, parsed))
    if not self.routes:
      raise AssertionError(f'unexpected request {handler.command} {handler.path}')
    method, path, status, body, headers = self.routes.pop(0)
    if (handler.command, handler.path) != (method, path):
      raise AssertionError(f'expected {(method, path)}, got {(handler.command, handler.path)}')
    encoded = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    for name, value in headers.items():
      handler.send_header(name, value)
    handler.send_header('Content-Length', str(len(encoded)))
    handler.end_headers()
    if encoded:
      handler.wfile.write(encoded)

  def __enter__(self) -> FakeBitbucketServer:
    self.thread.start()
    return self

  def __exit__(
    self,
    exception_type: type[BaseException] | None,
    _value: BaseException | None,
    _traceback: TracebackType | None,
  ) -> None:
    self.server.shutdown()
    self.server.server_close()
    self.thread.join()
    if self.errors and exception_type is None:
      raise RuntimeError(f'Fake server background error: {self.errors[0]}') from None
    if exception_type is None and self.routes:
      raise AssertionError(f'unconsumed routes: {len(self.routes)}')


class FakeClient:
  def __init__(
    self,
    actor_uuid='{actor}',
    author_uuid='{actor}',
    description='',
    source_pr_sha='a' * 40,
    destination_pr_sha='b' * 40,
    source_commit_sha=None,
    destination_commit_sha=None,
    create_source_pr_sha='a' * 40,
    create_destination_pr_sha='b' * 40,
    create_source_commit_sha=None,
    create_destination_commit_sha=None,
    state='OPEN',
    title='Example',
  ):
    self.actor_uuid = actor_uuid
    self.repo_uuid = '{repo}'
    self.pr = {
      'id': 7,
      'author': {'uuid': author_uuid},
      'source': {
        'branch': {'name': 'feat/a'},
        'commit': {'hash': source_pr_sha},
        'repository': {'uuid': self.repo_uuid},
      },
      'destination': {
        'branch': {'name': 'master'},
        'commit': {'hash': destination_pr_sha},
        'repository': {'uuid': self.repo_uuid},
      },
      'description': description,
      'state': state,
      'title': title,
      'links': {'html': {'href': 'https://bitbucket.example/pr/7'}},
    }
    self.comments = {}
    self.create_source_pr_sha = create_source_pr_sha
    self.create_destination_pr_sha = create_destination_pr_sha
    self.commit_hashes = {
      source_pr_sha: source_commit_sha if source_commit_sha is not None else source_pr_sha,
      destination_pr_sha: (
        destination_commit_sha
        if destination_commit_sha is not None
        else destination_pr_sha
      ),
      create_source_pr_sha: (
        create_source_commit_sha
        if create_source_commit_sha is not None
        else create_source_pr_sha
      ),
      create_destination_pr_sha: (
        create_destination_commit_sha
        if create_destination_commit_sha is not None
        else create_destination_pr_sha
      ),
    }
    self.commit_requests = []
    self.commit_readback_override = {}
    self.write_count = 0
    self.transport_after_write = False
    self.get_failure_after_write = False
    self.drift_after_write = False
    self.block_first_write = False
    self.pr_readback_override = {}
    self.comment_readback_override = {}
    self.create_author_override = None
    self.write_started = Event()
    self.release_write = Event()
    self.lock = Lock()

  def get_user(self):
    return {'uuid': self.actor_uuid}

  def get_repository(self, workspace, repo):
    return {'uuid': self.repo_uuid, 'slug': repo, 'workspace': {'slug': workspace}}

  def get_branch(self, _workspace, _repo, branch):
    sha = self.pr['source']['commit']['hash'] if branch == 'feat/a' else self.pr['destination']['commit']['hash']
    return {'name': branch, 'target': {'hash': sha, 'repository': {'uuid': self.repo_uuid}}}

  def get_commit(self, workspace, repo, commit):
    self.commit_requests.append((workspace, repo, commit))
    if commit in self.commit_readback_override:
      return deepcopy(self.commit_readback_override[commit])
    return {'hash': self.commit_hashes.get(commit)}

  def get_pr(self, _workspace, _repo, _pr_id):
    if self.get_failure_after_write and self.write_count:
      raise ApiTransportError('read-back failed')
    value = deepcopy(self.pr)
    if self.write_count:
      value.update(deepcopy(self.pr_readback_override))
    return value

  def update_pr(self, _workspace, _repo, _pr_id, body):
    with self.lock:
      self.write_count += 1
      if self.block_first_write and self.write_count == 1:
        self.write_started.set()
        self.release_write.wait(timeout=5)
      if 'description' in body:
        self.pr['description'] = body['description']
      if 'title' in body:
        self.pr['title'] = body['title']
      if self.drift_after_write:
        self.pr['source']['commit']['hash'] = 'd' * 40
      if self.transport_after_write:
        raise ApiTransportError('response lost')
      return deepcopy(self.pr)

  def create_comment(self, _workspace, _repo, _pr_id, body):
    with self.lock:
      self.write_count += 1
      comment_id = len(self.comments) + 1
      stored = deepcopy(body)
      if isinstance(stored.get('inline'), dict):
        # Bitbucket echoes the full inline shape: both anchor sides are always present
        # and the unused one is null. Mirroring that here keeps read-back honest.
        stored['inline'] = {
          'from': None,
          'to': None,
          'start_from': None,
          'start_to': None,
          'outdated': False,
          'base_rev': None,
          **stored['inline'],
        }
      self.comments[comment_id] = {
        'id': comment_id,
        **stored,
        'links': {
          'html': {
            'href': f'https://bitbucket.example/comment/{comment_id}',
          },
        },
      }
      if self.drift_after_write:
        self.pr['source']['commit']['hash'] = 'd' * 40
      if self.transport_after_write:
        raise ApiTransportError('response lost')
      return deepcopy(self.comments[comment_id])

  def create_pr(self, _workspace, _repo, body):
    with self.lock:
      self.write_count += 1
      self.pr = {
        'id': 8,
        'author': {'uuid': self.create_author_override or self.actor_uuid},
        'source': {
          'branch': {'name': body['source']['branch']['name']},
          'commit': {'hash': self.create_source_pr_sha},
          'repository': {'uuid': self.repo_uuid},
        },
        'destination': {
          'branch': {'name': body['destination']['branch']['name']},
          'commit': {'hash': self.create_destination_pr_sha},
          'repository': {'uuid': self.repo_uuid},
        },
        'description': body.get('description', ''),
        'state': 'OPEN',
        'title': body['title'],
        'links': {'html': {'href': 'https://bitbucket.example/pr/8'}},
      }
      if self.drift_after_write:
        self.pr['source']['commit']['hash'] = 'd' * 40
      if self.transport_after_write:
        raise ApiTransportError('response lost')
      return deepcopy(self.pr)

  def get_comment(self, _workspace, _repo, _pr_id, comment_id):
    if self.get_failure_after_write:
      raise ApiTransportError('read-back failed')
    value = deepcopy(self.comments[comment_id])
    value.update(deepcopy(self.comment_readback_override))
    return value
