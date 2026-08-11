import os
import socket
import traceback
import unittest
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from unittest import mock

from bitbucket_pr_workflow.api import ApiError, ApiTransportError, BitbucketClient
from test_support import FakeBitbucketServer


class ExplodingMapping(dict):
  def items(self):
    raise RuntimeError('mapping serialization failed')


class CloseFailureBody:
  def close(self):
    raise OSError('close-secret')


class HttpErrorOpener:
  def open(self, _request, timeout):
    raise urllib.error.HTTPError(
      'https://example.invalid/2.0/user',
      403,
      'Forbidden',
      {},
      CloseFailureBody(),
    )


class SuccessCloseFailureResponse:
  def __enter__(self):
    return self

  def __exit__(self, _type, _value, _traceback):
    self.close()

  def read(self):
    return b'{"uuid":"{actor}"}'

  def close(self):
    raise RuntimeError('success-close-secret')


class SuccessCloseFailureOpener:
  def open(self, _request, timeout):
    return SuccessCloseFailureResponse()


class ReadFailureResponse:
  def __init__(self):
    self.reads = 0
    self.closes = 0

  def read(self):
    self.reads += 1
    raise RuntimeError('read-secret')

  def close(self):
    self.closes += 1


class ReadFailureOpener:
  def __init__(self, response):
    self.response = response

  def open(self, _request, timeout):
    return self.response


def captured_traceback_details(
  captured: traceback.TracebackException,
) -> tuple[int, list[str]]:
  frame_count = len(captured.stack)
  local_values = list(captured.format_exception_only())
  local_values.extend(
    value
    for frame in captured.stack
    for value in (frame.locals or {}).values()
  )
  for chained in (captured.__cause__, captured.__context__):
    if chained is not None:
      chained_count, chained_values = captured_traceback_details(chained)
      frame_count += chained_count
      local_values.extend(chained_values)
  return frame_count, local_values


def capture_failure(call: Callable[[], Any]) -> tuple[Exception, int, str]:
  try:
    call()
  except Exception as error:
    captured = traceback.TracebackException.from_exception(error, capture_locals=True)
    frame_count, local_values = captured_traceback_details(captured)
    return error, frame_count, '\n'.join(local_values)
  raise AssertionError('expected call to fail')


class ApiTests(unittest.TestCase):
  def test_capture_failure_includes_chained_traceback_locals(self):
    def inner():
      chained_marker = 'chained-secret'
      raise ValueError('inner failure')

    def outer():
      try:
        inner()
      except ValueError as error:
        raise RuntimeError('outer failure') from error

    _error, frame_count, local_values = capture_failure(outer)
    self.assertGreaterEqual(frame_count, 3)
    self.assertIn('chained-secret', local_values)
    self.assertIn('inner failure', local_values)

  def test_get_user_and_pr(self):
    with FakeBitbucketServer() as server:
      server.route('GET', '/2.0/user', 200, {'uuid': '{actor}'})
      server.route('GET', '/2.0/repositories/ws/repo/pullrequests/7', 200, {
        'id': 7,
        'author': {'uuid': '{actor}'},
        'source': {
          'commit': {'hash': 'a' * 40},
          'branch': {'name': 'feat/a'},
          'repository': {'uuid': '{repo}'},
        },
        'destination': {
          'commit': {'hash': 'b' * 40},
          'branch': {'name': 'master'},
          'repository': {'uuid': '{repo}'},
        },
        'description': '',
        'state': 'OPEN',
      })
      client = BitbucketClient(server.base_url, 'user@example.com', 'token')
      self.assertEqual(client.get_user()['uuid'], '{actor}')
      self.assertEqual(client.get_pr('ws', 'repo', 7)['id'], 7)

  def test_create_comment_sends_exact_body(self):
    with FakeBitbucketServer() as server:
      server.route('POST', '/2.0/repositories/ws/repo/pullrequests/7/comments', 201, {'id': 9})
      client = BitbucketClient(server.base_url, 'user@example.com', 'token')
      body = {'content': {'raw': 'hello'}}
      client.create_comment('ws', 'repo', 7, body)
      self.assertEqual(server.requests[-1].json, body)

  def test_client_repr_hides_credentials(self):
    client = BitbucketClient(
      'https://example.invalid/2.0',
      'user@example.com',
      'secret-token',
      10 ** 10000,
    )
    rendered = repr(client)
    self.assertNotIn('user@example.com', rendered)
    self.assertNotIn('secret-token', rendered)

  def test_client_equality_includes_credentials(self):
    first = BitbucketClient('https://example.invalid/2.0', 'first@example.com', 'token-a')
    same = BitbucketClient('https://example.invalid/2.0', 'first@example.com', 'token-a')
    different = BitbucketClient('https://example.invalid/2.0', 'second@example.com', 'token-b')
    self.assertEqual(first, same)
    self.assertNotEqual(first, different)

  def test_methods_use_expected_verbs_and_encoded_paths(self):
    with FakeBitbucketServer() as server:
      server.route('GET', '/2.0/repositories/work%20space/repo%2Fname', 200, {'uuid': '{repo}'})
      server.route(
        'GET',
        '/2.0/repositories/work%20space/repo%2Fname/refs/branches/feat%2Fa',
        200,
        {'name': 'feat/a'},
      )
      server.route(
        'GET',
        '/2.0/repositories/work%20space/repo%2Fname/commit/abc%2F123',
        200,
        {'hash': 'a' * 40},
      )
      server.route('POST', '/2.0/repositories/work%20space/repo%2Fname/pullrequests', 201, {'id': 7})
      server.route('PUT', '/2.0/repositories/work%20space/repo%2Fname/pullrequests/7', 200, {'id': 7})
      server.route(
        'GET',
        '/2.0/repositories/work%20space/repo%2Fname/pullrequests/7/comments/9',
        200,
        {'id': 9},
      )
      client = BitbucketClient(server.base_url, 'user@example.com', 'token')
      self.assertEqual(client.get_repository('work space', 'repo/name')['uuid'], '{repo}')
      self.assertEqual(client.get_branch('work space', 'repo/name', 'feat/a')['name'], 'feat/a')
      self.assertEqual(
        client.get_commit('work space', 'repo/name', 'abc/123')['hash'],
        'a' * 40,
      )
      self.assertEqual(client.create_pr('work space', 'repo/name', {'title': 'PR'})['id'], 7)
      self.assertEqual(client.update_pr('work space', 'repo/name', 7, {'title': 'PR 2'})['id'], 7)
      self.assertEqual(client.get_comment('work space', 'repo/name', 7, 9)['id'], 9)

  def test_write_path_encoding_failure_hides_request_body(self):
    with FakeBitbucketServer() as server:
      client = BitbucketClient(server.base_url, 'user@example.com', 'path-secret-token')
      calls = (
        lambda: client.create_pr('\ud800', 'repo', {'raw': 'create-body-secret'}),
        lambda: client.update_pr('\ud800', 'repo', 7, {'raw': 'update-body-secret'}),
        lambda: client.create_comment('\ud800', 'repo', 7, {'raw': 'comment-body-secret'}),
      )
      for call in calls:
        with self.subTest(call=call.__name__):
          error, frame_count, local_values = capture_failure(call)
          self.assertIsInstance(error, ApiTransportError)
          self.assertEqual(str(error), 'Bitbucket transport failed')
          self.assertGreater(frame_count, 0)
          for secret in (
            'path-secret-token',
            'create-body-secret',
            'update-body-secret',
            'comment-body-secret',
          ):
            self.assertNotIn(secret, local_values)
    self.assertEqual(server.requests, [])

  def test_redirect_is_not_followed_for_write(self):
    with FakeBitbucketServer() as sink:
      with FakeBitbucketServer() as source:
        path = '/2.0/repositories/ws/repo/pullrequests/7/comments'
        source.route('POST', path, 302, {}, {'Location': f'{sink.base_url}/capture'})
        client = BitbucketClient(source.base_url, 'user@example.com', 'secret-token')
        with self.assertRaises(ApiError) as caught:
          client.create_comment('ws', 'repo', 7, {'content': {'raw': 'hello'}})
        self.assertEqual(caught.exception.status, 302)
      self.assertEqual(sink.requests, [])

  def test_read_failure_closes_response_once(self):
    prepared = urllib.request.Request('https://example.invalid/2.0/user')
    response = ReadFailureResponse()
    with mock.patch(
      'bitbucket_pr_workflow.api._prepare_request',
      return_value=(prepared, ReadFailureOpener(response)),
    ):
      client = BitbucketClient('https://example.invalid/2.0', 'user@example.com', 'read-secret-token')
      error, frame_count, local_values = capture_failure(client.get_user)
    self.assertIsInstance(error, ApiTransportError)
    self.assertEqual(str(error), 'Bitbucket transport failed')
    self.assertIsNone(error.__cause__)
    self.assertIsNone(error.__context__)
    self.assertGreater(frame_count, 0)
    self.assertEqual(response.reads, 1)
    self.assertEqual(response.closes, 1)
    for secret in ('read-secret-token', 'read-secret'):
      self.assertNotIn(secret, local_values)

  def test_success_response_close_failure_does_not_override_result(self):
    prepared = urllib.request.Request('https://example.invalid/2.0/user')
    with mock.patch(
      'bitbucket_pr_workflow.api._prepare_request',
      return_value=(prepared, SuccessCloseFailureOpener()),
    ):
      client = BitbucketClient('https://example.invalid/2.0', 'user@example.com', 'close-secret-token')
      self.assertEqual(client.get_user()['uuid'], '{actor}')

  def test_http_error_close_failure_preserves_sanitized_status(self):
    prepared = urllib.request.Request('https://example.invalid/2.0/user')
    with mock.patch(
      'bitbucket_pr_workflow.api._prepare_request',
      return_value=(prepared, HttpErrorOpener()),
    ):
      client = BitbucketClient('https://example.invalid/2.0', 'user@example.com', 'close-secret-token')
      error, frame_count, local_values = capture_failure(client.get_user)
    self.assertIsInstance(error, ApiError)
    self.assertEqual(error.status, 403)
    self.assertIsNone(error.__cause__)
    self.assertIsNone(error.__context__)
    self.assertGreater(frame_count, 0)
    for secret in ('close-secret-token', 'close-secret'):
      self.assertNotIn(secret, local_values)

  def test_http_error_hides_request_body_from_traceback_locals(self):
    with FakeBitbucketServer() as server:
      path = '/2.0/repositories/ws/repo/pullrequests/7/comments'
      server.route('POST', path, 403, {'error': {'message': 'denied'}})
      client = BitbucketClient(server.base_url, 'user@example.com', 'secret-token')
      error, frame_count, local_values = capture_failure(
        lambda: client.create_comment(
          'ws',
          'repo',
          7,
          {'content': {'raw': 'request-body-secret'}},
        ),
      )
    self.assertIsInstance(error, ApiError)
    self.assertGreater(frame_count, 0)
    self.assertNotIn('secret-token', local_values)
    self.assertNotIn('request-body-secret', local_values)

  def test_http_error_exposes_only_status(self):
    with FakeBitbucketServer() as server:
      server.route(
        'GET',
        '/2.0/user',
        403,
        b'{"error":{"message":"body-secret"}}',
        {'X-Response-Secret': 'header-secret'},
      )
      client = BitbucketClient(server.base_url, 'user@example.com', 'secret-token')
      error, frame_count, local_values = capture_failure(client.get_user)
      self.assertIsInstance(error, ApiError)
      self.assertEqual(error.status, 403)
      self.assertEqual(str(error), 'Bitbucket HTTP 403')
      self.assertIsNone(error.__cause__)
      self.assertIsNone(error.__context__)
      self.assertGreater(frame_count, 0)
      for secret in ('secret-token', 'body-secret', 'header-secret'):
        self.assertNotIn(secret, repr(error))
        self.assertNotIn(secret, local_values)

  def test_request_body_uses_canonical_utf8_json(self):
    with FakeBitbucketServer() as server:
      server.route('POST', '/2.0/repositories/ws/repo/pullrequests/7/comments', 201, {'id': 9})
      client = BitbucketClient(server.base_url, 'user@example.com', 'token')
      client.create_comment('ws', 'repo', 7, {'z': '🧪', 'a': 1})
      self.assertEqual(server.requests[-1].raw, '{"a":1,"z":"🧪"}'.encode('utf-8'))

  def test_serialization_failure_hides_token_from_traceback_locals(self):
    client = BitbucketClient('https://example.invalid/2.0', 'user@example.com', 'serialization-secret-token')
    error, frame_count, local_values = capture_failure(
      lambda: client.create_comment('ws', 'repo', 7, {'unsupported': object()}),
    )
    self.assertIsInstance(error, ApiTransportError)
    self.assertEqual(str(error), 'Bitbucket transport failed')
    self.assertGreater(frame_count, 0)
    self.assertNotIn('serialization-secret-token', local_values)

  def test_unexpected_serialization_failure_hides_request_data(self):
    client = BitbucketClient('https://example.invalid/2.0', 'user@example.com', 'runtime-secret-token')
    error, frame_count, local_values = capture_failure(
      lambda: client.create_comment(
        'ws',
        'repo',
        7,
        ExplodingMapping({'raw': 'runtime-request-body-secret'}),
      ),
    )
    self.assertIsInstance(error, ApiTransportError)
    self.assertEqual(str(error), 'Bitbucket transport failed')
    self.assertIsNone(error.__cause__)
    self.assertIsNone(error.__context__)
    self.assertGreater(frame_count, 0)
    for secret in ('runtime-secret-token', 'runtime-request-body-secret', 'mapping serialization failed'):
      self.assertNotIn(secret, local_values)

  def test_invalid_url_hides_token_from_traceback_locals(self):
    client = BitbucketClient('http://[invalid', 'user@example.com', 'url-secret-token')
    error, frame_count, local_values = capture_failure(client.get_user)
    self.assertIsInstance(error, ApiTransportError)
    self.assertEqual(str(error), 'Bitbucket transport failed')
    self.assertGreater(frame_count, 0)
    self.assertNotIn('url-secret-token', local_values)

  def test_non_loopback_http_is_rejected_before_transport(self):
    client = BitbucketClient('http://example.invalid/2.0', 'user@example.com', 'http-secret-token')
    with mock.patch(
      'bitbucket_pr_workflow.api._prepare_request',
      side_effect=AssertionError('transport must not be prepared'),
    ) as prepare:
      error, frame_count, local_values = capture_failure(client.get_user)
    self.assertIsInstance(error, ApiTransportError)
    self.assertEqual(str(error), 'Bitbucket transport failed')
    self.assertGreater(frame_count, 0)
    self.assertNotIn('http-secret-token', local_values)
    prepare.assert_not_called()

  def test_loopback_http_ignores_environment_proxy(self):
    with FakeBitbucketServer() as proxy:
      with FakeBitbucketServer() as target:
        target.route('GET', '/2.0/user', 200, {'uuid': '{actor}'})
        proxy_url = proxy.base_url.removesuffix('/2.0')
        environment = {
          'http_proxy': proxy_url,
          'HTTP_PROXY': proxy_url,
          'no_proxy': '',
          'NO_PROXY': '',
        }
        with mock.patch.dict(os.environ, environment, clear=False):
          client = BitbucketClient(target.base_url, 'user@example.com', 'proxy-secret-token')
          self.assertEqual(client.get_user()['uuid'], '{actor}')
      self.assertEqual(proxy.requests, [])

  def test_invalid_timeout_hides_token_from_traceback_locals(self):
    with FakeBitbucketServer() as server:
      client = BitbucketClient(server.base_url, 'user@example.com', 'timeout-secret-token', 'invalid')
      error, frame_count, local_values = capture_failure(client.get_user)
    self.assertIsInstance(error, ApiTransportError)
    self.assertEqual(str(error), 'Bitbucket transport failed')
    self.assertGreater(frame_count, 0)
    self.assertNotIn('timeout-secret-token', local_values)

  def test_timeout_overflow_is_sanitized_transport_error(self):
    for timeout in (10 ** 10000, 1e20):
      with self.subTest(timeout_type=type(timeout).__name__):
        with FakeBitbucketServer() as server:
          client = BitbucketClient(server.base_url, 'user@example.com', 'overflow-secret-token', timeout)
          error, frame_count, local_values = capture_failure(client.get_user)
        self.assertIsInstance(error, ApiTransportError)
        self.assertEqual(str(error), 'Bitbucket transport failed')
        self.assertGreater(frame_count, 0)
        self.assertNotIn('overflow-secret-token', local_values)

  def test_json_value_error_hides_response_data(self):
    raw = (
      b'{"marker":"oversized-response-secret","n":'
      + b'9' * 5000
      + b'}'
    )
    with FakeBitbucketServer() as server:
      server.route('GET', '/2.0/user', 200, raw)
      client = BitbucketClient(server.base_url, 'user@example.com', 'json-secret-token')
      error, frame_count, local_values = capture_failure(client.get_user)
    self.assertIsInstance(error, ApiTransportError)
    self.assertEqual(str(error), 'Bitbucket returned invalid JSON')
    self.assertGreater(frame_count, 0)
    self.assertNotIn('json-secret-token', local_values)
    self.assertNotIn('oversized-response-secret', local_values)

  def test_invalid_json_is_sanitized_transport_error(self):
    with FakeBitbucketServer() as server:
      server.route('GET', '/2.0/user', 200, b'invalid-body-secret')
      client = BitbucketClient(server.base_url, 'user@example.com', 'secret-token')
      error, frame_count, local_values = capture_failure(client.get_user)
      self.assertIsInstance(error, ApiTransportError)
      self.assertEqual(str(error), 'Bitbucket returned invalid JSON')
      self.assertIsNone(error.__cause__)
      self.assertIsNone(error.__context__)
      self.assertGreater(frame_count, 0)
      for secret in ('secret-token', 'invalid-body-secret'):
        self.assertNotIn(secret, repr(error))
        self.assertNotIn(secret, local_values)

  def test_non_mapping_json_is_transport_error(self):
    with FakeBitbucketServer() as server:
      server.route('GET', '/2.0/user', 200, ['unexpected'])
      client = BitbucketClient(server.base_url, 'user@example.com', 'secret-token')
      with self.assertRaises(ApiTransportError) as caught:
        client.get_user()
      self.assertEqual(str(caught.exception), 'Bitbucket returned invalid JSON')
      self.assertIsNone(caught.exception.__context__)

  def test_connection_failure_is_sanitized_transport_error(self):
    with socket.socket() as listener:
      listener.bind(('127.0.0.1', 0))
      host, port = listener.getsockname()
      client = BitbucketClient(f'http://{host}:{port}/2.0', 'user@example.com', 'secret-token', 0.1)
      error, frame_count, local_values = capture_failure(client.get_user)
    self.assertIsInstance(error, ApiTransportError)
    self.assertEqual(str(error), 'Bitbucket transport failed')
    self.assertIsNone(error.__cause__)
    self.assertIsNone(error.__context__)
    self.assertGreater(frame_count, 0)
    self.assertNotIn('secret-token', repr(error))
    self.assertNotIn('secret-token', local_values)

  def test_empty_response_returns_empty_mapping(self):
    with FakeBitbucketServer() as server:
      server.route('PUT', '/2.0/repositories/ws/repo/pullrequests/7', 200, b'')
      client = BitbucketClient(server.base_url, 'user@example.com', 'token')
      self.assertEqual(client.update_pr('ws', 'repo', 7, {'title': 'PR'}), {})

  def test_fake_server_uses_joinable_request_threads(self):
    with FakeBitbucketServer() as server:
      self.assertFalse(server.server.daemon_threads)

  def test_fake_server_preserves_body_exception_over_background_error(self):
    def trigger():
      with FakeBitbucketServer() as server:
        server.route('GET', '/2.0/user', 200, object())
        client = BitbucketClient(server.base_url, 'user@example.com', 'token')
        client.get_user()

    error, frame_count, _local_values = capture_failure(trigger)
    self.assertIsInstance(error, ApiError)
    self.assertEqual(error.status, 500)
    self.assertIsNone(error.__cause__)
    self.assertIsNone(error.__context__)
    self.assertGreater(frame_count, 0)

  def test_fake_server_propagates_route_mismatch(self):
    with self.assertRaisesRegex(RuntimeError, 'Fake server background error: AssertionError'):
      with FakeBitbucketServer() as server:
        server.route('GET', '/2.0/wrong', 200, {})
        client = BitbucketClient(server.base_url, 'user@example.com', 'token')
        with self.assertRaises(ApiError):
          client.get_user()

  def test_fake_server_propagates_response_serialization_error(self):
    with self.assertRaisesRegex(RuntimeError, 'Fake server background error: TypeError'):
      with FakeBitbucketServer() as server:
        server.route('GET', '/2.0/user', 200, object())
        client = BitbucketClient(server.base_url, 'user@example.com', 'token')
        with self.assertRaises(ApiError):
          client.get_user()

  def test_fake_server_background_error_hides_sensitive_locals(self):
    def trigger():
      with FakeBitbucketServer() as server:
        path = '/2.0/repositories/ws/repo/pullrequests/7/comments'
        server.route(
          'POST',
          path,
          200,
          object(),
          {'X-Response-Secret': 'response-header-secret'},
        )
        client = BitbucketClient(server.base_url, 'user@example.com', 'background-secret-token')
        with self.assertRaises(ApiError):
          client.create_comment(
            'ws',
            'repo',
            7,
            {'content': {'raw': 'request-body-secret'}},
          )

    error, frame_count, local_values = capture_failure(trigger)
    self.assertIsInstance(error, RuntimeError)
    self.assertEqual(str(error), 'Fake server background error: TypeError')
    self.assertIsNone(error.__cause__)
    self.assertIsNone(error.__context__)
    self.assertGreater(frame_count, 0)
    for secret in (
      'background-secret-token',
      'request-body-secret',
      'response-header-secret',
    ):
      self.assertNotIn(secret, repr(error))
      self.assertNotIn(secret, local_values)


if __name__ == '__main__':
  unittest.main()
