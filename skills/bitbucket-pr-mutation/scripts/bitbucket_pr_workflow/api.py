from __future__ import annotations

import base64
import http.client
import json
import math
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, IO, Mapping

from bitbucket_pr_workflow.core import canonical_json_bytes


class ApiError(RuntimeError):
  def __init__(self, status: int) -> None:
    self.status = status
    super().__init__(f'Bitbucket HTTP {status}')


class ApiTransportError(RuntimeError):
  pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
  def redirect_request(
    self,
    req: urllib.request.Request,
    fp: IO[bytes],
    code: int,
    msg: str,
    headers: http.client.HTTPMessage,
    newurl: str,
  ) -> None:
    return None


def segment(value: str) -> str:
  return urllib.parse.quote(value, safe='')


def _valid_base_url(value: Any) -> bool:
  try:
    parsed = urllib.parse.urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
      return False
    if parsed.scheme == 'https':
      return parsed.hostname is not None
    return parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', '::1')
  except Exception:
    return False


def _valid_timeout(value: Any) -> bool:
  if type(value) not in (int, float):
    return False
  try:
    return math.isfinite(value) and 0 <= value <= threading.TIMEOUT_MAX
  except (OverflowError, TypeError, ValueError):
    return False


def _serialize_body(
  body: Mapping[str, Any] | None,
) -> tuple[bytes | None, bool]:
  if body is None:
    return None, False
  try:
    return canonical_json_bytes(body), False
  except Exception:
    return None, True


def _prepare_request(
  base_url: str,
  username: str,
  token: str,
  method: str,
  path: str,
  payload: bytes | None,
) -> tuple[urllib.request.Request | None, urllib.request.OpenerDirector | None]:
  try:
    credentials = f'{username}:{token}'.encode('utf-8')
    authorization = base64.b64encode(credentials).decode('ascii')
    request = urllib.request.Request(
      f'{base_url.rstrip("/")}{path}',
      data=payload,
      method=method,
      headers={
        'Authorization': f'Basic {authorization}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
      },
    )
    redirect_handler = _NoRedirectHandler()
    if urllib.parse.urlsplit(base_url).scheme == 'http':
      opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        redirect_handler,
      )
    else:
      opener = urllib.request.build_opener(redirect_handler)
  except Exception:
    return None, None
  return request, opener


def _perform_request(
  request: urllib.request.Request,
  opener: urllib.request.OpenerDirector,
  timeout_seconds: float,
) -> tuple[Mapping[str, Any] | None, int | None, str | None]:
  response = None
  try:
    response = opener.open(request, timeout=timeout_seconds)
    raw = response.read()
  except urllib.error.HTTPError as error:
    status = error.code
    try:
      error.close()
    except Exception:
      pass
    return None, status, None
  except Exception:
    return None, None, 'Bitbucket transport failed'
  finally:
    if response is not None:
      try:
        response.close()
      except Exception:
        pass
  if not raw:
    return {}, None, None
  try:
    decoded = json.loads(raw.decode('utf-8'))
  except Exception:
    return None, None, 'Bitbucket returned invalid JSON'
  if not isinstance(decoded, dict):
    return None, None, 'Bitbucket returned invalid JSON'
  return decoded, None, None


@dataclass(frozen=True)
class BitbucketClient:
  base_url: str = field(repr=False)
  username: str = field(repr=False)
  token: str = field(repr=False)
  timeout_seconds: float = field(default=30, repr=False)

  def request(
    self,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
  ) -> Mapping[str, Any]:
    if not _valid_timeout(self.timeout_seconds):
      body = None
      raise ApiTransportError('Bitbucket transport failed')
    if not _valid_base_url(self.base_url):
      body = None
      raise ApiTransportError('Bitbucket transport failed')
    payload, serialization_failed = _serialize_body(body)
    if serialization_failed:
      body = None
      payload = None
      raise ApiTransportError('Bitbucket transport failed')
    request, opener = _prepare_request(
      self.base_url,
      self.username,
      self.token,
      method,
      path,
      payload,
    )
    if request is None or opener is None:
      body = None
      payload = None
      request = None
      opener = None
      raise ApiTransportError('Bitbucket transport failed')
    result, status, transport_error = _perform_request(
      request,
      opener,
      self.timeout_seconds,
    )
    body = None
    payload = None
    request = None
    opener = None
    if status is not None:
      raise ApiError(status)
    if transport_error is not None:
      raise ApiTransportError(transport_error)
    if result is None:
      raise ApiTransportError('Bitbucket transport failed')
    return result

  def get_user(self) -> Mapping[str, Any]:
    return self.request('GET', '/user')

  def get_repository(self, workspace: str, repo: str) -> Mapping[str, Any]:
    return self.request('GET', f'/repositories/{segment(workspace)}/{segment(repo)}')

  def get_branch(self, workspace: str, repo: str, branch: str) -> Mapping[str, Any]:
    path = f'/repositories/{segment(workspace)}/{segment(repo)}/refs/branches/{segment(branch)}'
    return self.request('GET', path)

  def get_commit(self, workspace: str, repo: str, commit: str) -> Mapping[str, Any]:
    path = f'/repositories/{segment(workspace)}/{segment(repo)}/commit/{segment(commit)}'
    return self.request('GET', path)

  def get_pr(self, workspace: str, repo: str, pr_id: int) -> Mapping[str, Any]:
    path = f'/repositories/{segment(workspace)}/{segment(repo)}/pullrequests/{pr_id}'
    return self.request('GET', path)

  def create_pr(
    self,
    workspace: str,
    repo: str,
    body: Mapping[str, Any],
  ) -> Mapping[str, Any]:
    path = None
    try:
      path = f'/repositories/{segment(workspace)}/{segment(repo)}/pullrequests'
    except Exception:
      pass
    if path is None:
      body = {}
      raise ApiTransportError('Bitbucket transport failed')
    try:
      return self.request('POST', path, body)
    except (ApiError, ApiTransportError) as error:
      sanitized_error = error.with_traceback(None)
    body = {}
    raise sanitized_error

  def update_pr(
    self,
    workspace: str,
    repo: str,
    pr_id: int,
    body: Mapping[str, Any],
  ) -> Mapping[str, Any]:
    path = None
    try:
      path = f'/repositories/{segment(workspace)}/{segment(repo)}/pullrequests/{pr_id}'
    except Exception:
      pass
    if path is None:
      body = {}
      raise ApiTransportError('Bitbucket transport failed')
    try:
      return self.request('PUT', path, body)
    except (ApiError, ApiTransportError) as error:
      sanitized_error = error.with_traceback(None)
    body = {}
    raise sanitized_error

  def create_comment(
    self,
    workspace: str,
    repo: str,
    pr_id: int,
    body: Mapping[str, Any],
  ) -> Mapping[str, Any]:
    path = None
    try:
      path = f'/repositories/{segment(workspace)}/{segment(repo)}/pullrequests/{pr_id}/comments'
    except Exception:
      pass
    if path is None:
      body = {}
      raise ApiTransportError('Bitbucket transport failed')
    try:
      return self.request('POST', path, body)
    except (ApiError, ApiTransportError) as error:
      sanitized_error = error.with_traceback(None)
    body = {}
    raise sanitized_error

  def get_comment(
    self,
    workspace: str,
    repo: str,
    pr_id: int,
    comment_id: int,
  ) -> Mapping[str, Any]:
    path = f'/repositories/{segment(workspace)}/{segment(repo)}/pullrequests/{pr_id}/comments/{comment_id}'
    return self.request('GET', path)
