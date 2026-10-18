"""Isolated regression tests for the real auth/discovery functions, without DB or network.
Run: python3 scenarios/test_discovery_auth.py
"""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]


def load_function(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    node.returns = None
    node.args.defaults = []
    for arg in node.args.args:
        arg.annotation = None
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, str(path), 'exec'), namespace)
    return namespace[name]


class DiscoveryAuthTests(unittest.TestCase):
    def setUp(self):
        self.gateway = SimpleNamespace(id=1, auth_method='api_key', api_key='test-key', username='filled-user', password='filled-password', url='https://example.com')
        self.requests = Mock()
        self.model = SimpleNamespace(AUTH_METHOD_API_KEY='api_key', objects=Mock())
        self.user = SimpleNamespace(is_authenticated=True, is_superuser=True)
        self.request = SimpleNamespace(user=self.user)
        self.namespace = {
            'GatewayIOT': self.model, 'get_object_or_404': lambda *a, **k: self.gateway,
            'requests': self.requests, 'json': json,
        }
        self.auth = load_function(Path('core/api.py'), 'get_gateway_auth_headers', self.namespace)

    def test_key_ignores_filled_login_fields(self):
        result, status = self.auth(self.request, 1)
        self.assertEqual(status, 200)
        self.assertEqual(result['headers']['X-Authorization'], 'ApiKey test-key')
        self.requests.post.assert_not_called()

    def test_key_needs_no_login_fields(self):
        self.gateway.username = self.gateway.password = None
        self.test_key_ignores_filled_login_fields()

    def test_missing_key_does_not_fall_back_to_login(self):
        self.gateway.api_key = None
        _, status = self.auth(self.request, 1)
        self.assertEqual(status, 400)
        self.requests.post.assert_not_called()

    def test_password_mode_performs_login(self):
        self.gateway.auth_method = 'user_password'
        self.requests.post.return_value.status_code = 200
        self.requests.post.return_value.json.return_value = {'token': 'test-jwt'}
        result, status = self.auth(self.request, 1)
        self.assertEqual(status, 200)
        self.assertEqual(result['headers']['X-Authorization'], 'Bearer test-jwt')
        self.requests.post.assert_called_once()

    def test_password_login_strips_trailing_slash(self):
        self.gateway.url = 'https://example.com/'
        self.test_password_mode_performs_login()
        self.assertEqual(self.requests.post.call_args.args[0], 'https://example.com/api/auth/login')

    def test_discovery_strips_trailing_slash(self):
        self.gateway.url = 'https://example.com/'
        self.test_discovery_explains_upstream_401_without_login()

    def test_discovery_explains_upstream_401_without_login(self):
        response = self.requests.get.return_value
        response.status_code = 401
        response.json.return_value = {'status': 401, 'errorCode': 10, 'message': 'Invalid username or password'}
        self.namespace.update(
            get_user_model=lambda: Mock(), get_gateway_auth_headers=self.auth,
            api=SimpleNamespace(create_response=lambda request, payload, status: (payload, status)),
        )
        discover = load_function(Path('facade/api.py'), 'discover_devices', self.namespace)
        payload, status = discover(self.request, 1, SimpleNamespace(dict=lambda: {'pageSize': 1, 'page': 0}))
        self.assertEqual(status, 401)
        self.assertEqual(payload['message'], 'Invalid username or password')
        self.assertEqual(payload['gateway_auth_method'], 'api_key')
        self.assertIn('No username/password login', payload['detail'])
        self.requests.post.assert_not_called()
        self.requests.get.assert_called_once_with(
            'https://example.com/api/tenant/devices',
            headers={'Content-Type': 'application/json', 'X-Authorization': 'ApiKey test-key'},
            params={'pageSize': 1, 'page': 0},
        )
        self.assertNotIn('test-key', json.dumps(payload))
        self.assertNotIn('filled-password', json.dumps(payload))


if __name__ == '__main__':
    unittest.main()
