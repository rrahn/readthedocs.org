import json
import base64
import datetime
import unittest

from django.test import TestCase
from django.contrib.auth.models import User
from django_dynamic_fixture import get
from rest_framework import status
from rest_framework.test import APIClient

from readthedocs.builds.models import Build


super_auth = base64.b64encode('super:test')
eric_auth = base64.b64encode('eric:test')


class APIBuildTests(TestCase):
    fixtures = ['eric.json', 'test_data.json']

    def test_make_build(self):
        """
        Test that a superuser can use the API
        """
        client = APIClient()
        client.login(username='super', password='test')
        resp = client.post(
            '/api/v2/build/',
            {
                'project': 1,
                'version': 1,
                'success': True,
                'output': 'Test Output',
                'error': 'Test Error',
                'state': 'cloning',
            },
            format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        build = resp.data
        self.assertEqual(build['id'], 1)
        self.assertEqual(build['state_display'], 'Cloning')

        resp = client.get('/api/v2/build/1/')
        self.assertEqual(resp.status_code, 200)
        build = resp.data
        self.assertEqual(build['output'], 'Test Output')
        self.assertEqual(build['state_display'], 'Cloning')

    def test_make_build_without_permission(self):
        """Ensure anonymous/non-staff users cannot write the build endpoint"""
        client = APIClient()

        def _try_post():
            resp = client.post(
                '/api/v2/build/',
                {
                    'project': 1,
                    'version': 1,
                    'success': True,
                    'output': 'Test Output',
                    'error': 'Test Error',
                },
                format='json')
            self.assertEqual(resp.status_code, 403)

        _try_post()

        api_user = get(User, staff=False, password='test')
        assert api_user.is_staff == False
        client.force_authenticate(user=api_user)
        _try_post()

    def test_update_build_without_permission(self):
        """Ensure anonymous/non-staff users cannot update build endpoints"""
        client = APIClient()
        api_user = get(User, staff=False, password='test')
        client.force_authenticate(user=api_user)
        build = get(Build, project_id=1, version_id=1, state='cloning')
        resp = client.put(
            '/api/v2/build/{0}/'.format(build.pk),
            {
                'project': 1,
                'version': 1,
                'state': 'finished'
            },
            format='json')
        self.assertEqual(resp.status_code, 403)

    def test_make_build_protected_fields(self):
        """Ensure build api view delegates correct serializer

        Super users should be able to read/write the `builder` property, but we
        don't expose this to end users via the API
        """
        build = get(Build, project_id=1, version_id=1, builder='foo')
        client = APIClient()

        api_user = get(User, staff=False, password='test')
        client.force_authenticate(user=api_user)
        resp = client.get('/api/v2/build/{0}/'.format(build.pk), format='json')
        self.assertEqual(resp.status_code, 403)

        client.force_authenticate(user=User.objects.get(username='super'))
        resp = client.get('/api/v2/build/{0}/'.format(build.pk), format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('builder', resp.data)

    def test_make_build_commands(self):
        """Create build and build commands"""
        client = APIClient()
        client.login(username='super', password='test')
        resp = client.post(
            '/api/v2/build/',
            {
                'project': 1,
                'version': 1,
                'success': True,
            },
            format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        build = resp.data
        now = datetime.datetime.utcnow()
        resp = client.post(
            '/api/v2/command/',
            {
                'build': build['id'],
                'command': 'echo test',
                'description': 'foo',
                'start_time': str(now - datetime.timedelta(seconds=5)),
                'end_time': str(now),
            },
            format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        resp = client.get('/api/v2/build/1/')
        self.assertEqual(resp.status_code, 200)
        build = resp.data
        self.assertEqual(len(build['commands']), 1)
        self.assertEqual(build['commands'][0]['run_time'], 5)
        self.assertEqual(build['commands'][0]['description'], 'foo')


class APITests(TestCase):
    fixtures = ['eric.json', 'test_data.json']

    def test_make_project(self):
        """
        Test that a superuser can use the API
        """
        post_data = {"name": "awesome-project",
                     "repo": "https://github.com/ericholscher/django-kong.git"}
        resp = self.client.post('/api/v1/project/',
                                data=json.dumps(post_data),
                                content_type='application/json',
                                HTTP_AUTHORIZATION='Basic %s' % super_auth)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp['location'],
                         'http://testserver/api/v1/project/24/')
        resp = self.client.get('/api/v1/project/24/', data={'format': 'json'},
                               HTTP_AUTHORIZATION='Basic %s' % eric_auth)
        self.assertEqual(resp.status_code, 200)
        obj = json.loads(resp.content)
        self.assertEqual(obj['slug'], 'awesome-project')

    def test_invalid_make_project(self):
        """
        Test that the authentication is turned on.
        """
        post_data = {"user": "/api/v1/user/2/",
                     "name": "awesome-project-2",
                     "repo": "https://github.com/ericholscher/django-bob.git"
                     }
        resp = self.client.post(
            '/api/v1/project/', data=json.dumps(post_data),
            content_type='application/json',
            HTTP_AUTHORIZATION='Basic %s' % base64.b64encode('tester:notapass')
        )
        self.assertEqual(resp.status_code, 401)

    def test_make_project_dishonest_user(self):
        """
        Test that you can't create a project for another user
        """
        # represents dishonest data input, authentication happens for user 2
        post_data = {
            "users": ["/api/v1/user/1/"],
            "name": "awesome-project-2",
            "repo": "https://github.com/ericholscher/django-bob.git"
        }
        resp = self.client.post(
            '/api/v1/project/',
            data=json.dumps(post_data),
            content_type='application/json',
            HTTP_AUTHORIZATION='Basic %s' % base64.b64encode('tester:test')
        )
        self.assertEqual(resp.status_code, 401)

    def test_ensure_get_unauth(self):
        """
        Test that GET requests work without authenticating.
        """

        resp = self.client.get("/api/v1/project/", data={"format": "json"})
        self.assertEqual(resp.status_code, 200)

    def test_not_highest(self):
        resp = self.client.get(
            "http://testserver/api/v1/version/read-the-docs/highest/0.2.1/",
            data={"format": "json"}
        )
        self.assertEqual(resp.status_code, 200)
        obj = json.loads(resp.content)
        self.assertEqual(obj['is_highest'], False)

    def test_latest_version_highest(self):
        resp = self.client.get(
            "http://testserver/api/v1/version/read-the-docs/highest/latest/",
            data={"format": "json"}
        )
        self.assertEqual(resp.status_code, 200)
        obj = json.loads(resp.content)
        self.assertEqual(obj['is_highest'], True)

    def test_real_highest(self):
        resp = self.client.get(
            "http://testserver/api/v1/version/read-the-docs/highest/0.2.2/",
            data={"format": "json"}
        )
        self.assertEqual(resp.status_code, 200)
        obj = json.loads(resp.content)
        self.assertEqual(obj['is_highest'], True)
