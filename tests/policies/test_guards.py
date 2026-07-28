"""Tests for the shipped secret / destructive-op / path / network policies."""

import pytest

from tollgate.core.interceptor import TollgateInterceptor
from tollgate.decisions import GuardBlocked
from tollgate.policies import (
    domain_allowlist,
    find_secrets,
    host_allowed,
    is_within,
    no_destructive_shell,
    no_destructive_sql,
    no_secrets_in_args,
    path_within,
)


def _noop(**kwargs):
    return kwargs


# --- secrets ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "AKIAIOSFODNN7EXAMPLE",
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "xoxb-1234567890-abcdefghij",
        "AIzaSyA1234567890abcdefghijklmnopqrstuv",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE...",
    ],
)
def test_known_credential_shapes_are_detected(value):
    assert find_secrets(value)


@pytest.mark.parametrize(
    "value",
    ["hello world", "sk-", "a normal sentence about an AKIA product", "", "SELECT * FROM users"],
)
def test_ordinary_text_is_not_flagged(value):
    assert find_secrets(value) == []


def test_secrets_nested_in_a_payload_are_found():
    payload = {"body": {"headers": [{"Authorization": "Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789"}]}}
    assert find_secrets(payload)


def test_no_secrets_in_args_blocks_a_leaking_call():
    interceptor = TollgateInterceptor(policies=[no_secrets_in_args()])
    interceptor.call("post", _noop, body="nothing sensitive")

    with pytest.raises(GuardBlocked, match="credential"):
        interceptor.call("post", _noop, body="key is AKIAIOSFODNN7EXAMPLE")


# --- destructive SQL -------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "DROP TABLE users",
        "drop database prod",
        "TRUNCATE TABLE events",
        "DELETE FROM users",
        "UPDATE users SET admin = true",
    ],
)
def test_destructive_sql_is_blocked(query):
    interceptor = TollgateInterceptor(policies=[no_destructive_sql(tool_names=("run_sql",))])
    with pytest.raises(GuardBlocked):
        interceptor.call("run_sql", _noop, query=query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM users",
        "DELETE FROM users WHERE id = 1",
        "UPDATE users SET name = 'x' WHERE id = 1",
        "INSERT INTO users (name) VALUES ('x')",
    ],
)
def test_ordinary_sql_passes(query):
    interceptor = TollgateInterceptor(policies=[no_destructive_sql(tool_names=("run_sql",))])
    interceptor.call("run_sql", _noop, query=query)


def test_unbounded_writes_can_be_allowed_while_drops_stay_blocked():
    policy = no_destructive_sql(tool_names=("run_sql",), allow_unbounded_writes=True)
    interceptor = TollgateInterceptor(policies=[policy])
    interceptor.call("run_sql", _noop, query="DELETE FROM users")
    with pytest.raises(GuardBlocked):
        interceptor.call("run_sql", _noop, query="DROP TABLE users")


def test_sql_policy_scoped_by_tool_ignores_other_tools():
    interceptor = TollgateInterceptor(policies=[no_destructive_sql(tool_names=("run_sql",))])
    interceptor.call("send_email", _noop, query="DROP TABLE users")


# --- destructive shell -----------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /var/data",
        "rm -fr ./build",
        "sudo mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "shutdown -h now",
        "history -c",
    ],
)
def test_destructive_shell_is_blocked(command):
    interceptor = TollgateInterceptor(policies=[no_destructive_shell(tool_names=("run_shell",))])
    with pytest.raises(GuardBlocked):
        interceptor.call("run_shell", _noop, command=command)


@pytest.mark.parametrize(
    "command",
    ["ls -la", "rm ./tmp.txt", "grep -r pattern .", "python script.py", "git status"],
)
def test_ordinary_shell_passes(command):
    interceptor = TollgateInterceptor(policies=[no_destructive_shell(tool_names=("run_shell",))])
    interceptor.call("run_shell", _noop, command=command)


# --- paths -----------------------------------------------------------------


def test_path_within_allows_inside_and_blocks_traversal(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    interceptor = TollgateInterceptor(policies=[path_within([root], tool_names=("write_file",))])

    interceptor.call("write_file", _noop, path=str(root / "notes.txt"))
    interceptor.call("write_file", _noop, path=str(root / "sub" / "deep.txt"))

    with pytest.raises(GuardBlocked, match="outside the allowed roots"):
        interceptor.call("write_file", _noop, path=str(root / ".." / "escaped.txt"))


def test_path_within_follows_symlinks_out_of_the_root(tmp_path):
    """Resolving is the point — a string check would pass this."""
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "secrets"
    outside.mkdir()
    (root / "link").symlink_to(outside)

    interceptor = TollgateInterceptor(policies=[path_within([root], tool_names=("write_file",))])
    with pytest.raises(GuardBlocked):
        interceptor.call("write_file", _noop, path=str(root / "link" / "key.pem"))


def test_path_within_fails_closed_on_a_missing_argument(tmp_path):
    interceptor = TollgateInterceptor(policies=[path_within([tmp_path], tool_names=("write_file",))])
    with pytest.raises(GuardBlocked):
        interceptor.call("write_file", _noop, filename="notes.txt")  # wrong arg name


def test_is_within_handles_multiple_roots(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert is_within(b / "x", [a, b])
    assert not is_within(tmp_path / "c", [a, b])


# --- network ---------------------------------------------------------------


def test_host_allowlist_matches_exactly_not_by_substring():
    """The attack this defends against: example.com.evil.com."""
    assert host_allowed("https://example.com/x", ["example.com"])
    assert not host_allowed("https://example.com.evil.com/x", ["example.com"])
    assert not host_allowed("https://notexample.com/x", ["example.com"])


def test_leading_dot_allows_subdomains():
    assert host_allowed("https://api.internal.corp/x", [".internal.corp"])
    assert host_allowed("https://internal.corp/x", [".internal.corp"])
    assert not host_allowed("https://internal.corp.evil.com/x", [".internal.corp"])


def test_domain_allowlist_blocks_an_unlisted_host():
    policy = domain_allowlist(["api.stripe.com"], tool_names=("http_get",))
    interceptor = TollgateInterceptor(policies=[policy])

    interceptor.call("http_get", _noop, url="https://api.stripe.com/v1/charges")
    with pytest.raises(GuardBlocked, match="outside the allowlist"):
        interceptor.call("http_get", _noop, url="https://attacker.example/collect")


def test_domain_allowlist_rejects_non_https_by_default():
    policy = domain_allowlist(["api.stripe.com"], tool_names=("http_get",))
    interceptor = TollgateInterceptor(policies=[policy])
    with pytest.raises(GuardBlocked, match="schemes"):
        interceptor.call("http_get", _noop, url="http://api.stripe.com/v1")
    with pytest.raises(GuardBlocked, match="schemes"):
        interceptor.call("http_get", _noop, url="file:///etc/passwd")


def test_domain_allowlist_can_widen_allowed_schemes():
    policy = domain_allowlist(["localhost"], tool_names=("http_get",), allowed_schemes=("http",))
    interceptor = TollgateInterceptor(policies=[policy])
    interceptor.call("http_get", _noop, url="http://localhost:8080/health")


# --- composition -----------------------------------------------------------


def test_shipped_policies_compose_with_the_operators():
    """They are ordinary PolicySets, so & / | / ~ work as usual."""
    combined = no_secrets_in_args() & no_destructive_shell(tool_names=("run_shell",))
    interceptor = TollgateInterceptor(policies=[combined])

    interceptor.call("run_shell", _noop, command="ls -la")
    with pytest.raises(GuardBlocked):
        interceptor.call("run_shell", _noop, command="rm -rf /")
    with pytest.raises(GuardBlocked):
        interceptor.call("run_shell", _noop, command="echo AKIAIOSFODNN7EXAMPLE")
