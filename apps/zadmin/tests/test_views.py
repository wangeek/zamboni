# -*- coding: utf-8 -*-
import csv
import json
from cStringIO import StringIO
from datetime import datetime

from django.conf import settings
from django.core import mail, management
from django.core.cache import cache

import mock
from nose.plugins.attrib import attr
from nose.tools import eq_
from pyquery import PyQuery as pq

import amo
import amo.tests
from amo.tests import (assert_no_validation_errors, assert_required, formset,
                       initial)
from access.models import Group, GroupUser
from addons.models import Addon
from amo.urlresolvers import reverse
from amo.utils import urlparams
from applications.models import AppVersion
from bandwagon.models import FeaturedCollection, MonthlyPick
from devhub.models import ActivityLog
from files.models import File
from users.models import UserProfile
from users.utils import get_task_user
from versions.models import ApplicationsVersions, Version
from zadmin import forms, tasks
from zadmin.forms import DevMailerForm
from zadmin.models import EmailPreviewTopic, ValidationJob, ValidationResult
from zadmin.views import updated_versions, find_files


no_op_validation = dict(errors=0, warnings=0, notices=0, messages=[],
                        compatibility_summary=dict(errors=0, warnings=0,
                                                   notices=0))


class TestSiteEvents(amo.tests.TestCase):
    fixtures = ['base/users', 'zadmin/tests/siteevents']

    def setUp(self):
        self.client.login(username='admin@mozilla.com', password='password')

    def test_get(self):
        url = reverse('zadmin.site_events')
        response = self.client.get(url)
        eq_(response.status_code, 200)
        events = response.context['events']
        eq_(len(events), 1)

    def test_add(self):
        url = reverse('zadmin.site_events')
        new_event = {
            'event_type': 2,
            'start': '2012-01-01',
            'description': 'foo',
        }
        response = self.client.post(url, new_event, follow=True)
        eq_(response.status_code, 200)
        events = response.context['events']
        eq_(len(events), 2)

    def test_edit(self):
        url = reverse('zadmin.site_events', args=[1])
        modified_event = {
            'event_type': 2,
            'start': '2012-01-01',
            'description': 'bar',
        }
        response = self.client.post(url, modified_event, follow=True)
        eq_(response.status_code, 200)
        events = response.context['events']
        eq_(events[0].description, 'bar')

    def test_delete(self):
        url = reverse('zadmin.site_events.delete', args=[1])
        response = self.client.get(url, follow=True)
        eq_(response.status_code, 200)
        events = response.context['events']
        eq_(len(events), 0)


class BulkValidationTest(amo.tests.TestCase):
    fixtures = ['base/apps', 'base/platforms', 'base/addon_3615',
                'base/appversion', 'base/users']

    def setUp(self):
        assert self.client.login(username='admin@mozilla.com',
                                 password='password')
        self.addon = Addon.objects.get(pk=3615)
        self.creator = UserProfile.objects.get(username='editor')
        self.version = self.addon.get_version()
        ApplicationsVersions.objects.filter(
            application=amo.FIREFOX.id, version=self.version).update(
            max=AppVersion.objects.get(application=1, version='3.7a1pre'))
        self.application_version = self.version.apps.all()[0]
        self.application = self.application_version.application
        self.min = self.application_version.min
        self.max = self.application_version.max
        self.curr_max = self.appversion('3.7a1pre')
        self.counter = 0

        self.old_task_user = settings.TASK_USER_ID
        settings.TASK_USER_ID = self.creator.id

    def tearDown(self):
        settings.TASK_USER_ID = self.old_task_user

    def appversion(self, version, application=amo.FIREFOX.id):
        return AppVersion.objects.get(application=application,
                                      version=version)

    def create_job(self, **kwargs):
        kw = dict(application_id=amo.FIREFOX.id,
                  curr_max_version=kwargs.pop('current', self.curr_max),
                  target_version=kwargs.pop('target',
                                            self.appversion('3.7a3')),
                  creator=self.creator)
        kw.update(kwargs)

        return ValidationJob.objects.create(**kw)

    def create_file(self, version=None, platform_id=amo.PLATFORM_ALL.id):
        if not version:
            version = self.version
        return File.objects.create(version=version,
                                   filename='file-%s' % self.counter,
                                   platform_id=platform_id,
                                   status=amo.STATUS_PUBLIC)

    def create_result(self, job, f, **kwargs):
        self.counter += 1
        kw = dict(file=f,
                  validation='{}',
                  errors=0,
                  warnings=0,
                  notices=0,
                  validation_job=job,
                  task_error=None,
                  valid=0,
                  completed=datetime.now())
        kw.update(kwargs)
        return ValidationResult.objects.create(**kw)

    def start_validation(self, new_max='3.7a3'):
        self.new_max = self.appversion(new_max)
        r = self.client.post(reverse('zadmin.start_validation'),
                             {'application': amo.FIREFOX.id,
                              'curr_max_version': self.curr_max.id,
                              'target_version': self.new_max.id,
                              'finish_email': 'fliggy@mozilla.com'},
                             follow=True)
        eq_(r.status_code, 200)


class TestBulkValidation(BulkValidationTest):

    @mock.patch('zadmin.tasks.bulk_validate_file')
    def test_start(self, bulk_validate_file):
        new_max = self.appversion('3.7a3')
        r = self.client.post(reverse('zadmin.start_validation'),
                             {'application': amo.FIREFOX.id,
                              'curr_max_version': self.curr_max.id,
                              'target_version': new_max.id,
                              'finish_email': 'fliggy@mozilla.com'},
                             follow=True)
        self.assertNoFormErrors(r)
        self.assertRedirects(r, reverse('zadmin.validation'))
        job = ValidationJob.objects.get()
        eq_(job.application_id, amo.FIREFOX.id)
        eq_(job.curr_max_version.version, self.curr_max.version)
        eq_(job.target_version.version, new_max.version)
        eq_(job.finish_email, 'fliggy@mozilla.com')
        eq_(job.completed, None)
        eq_(job.result_set.all().count(),
            len(self.version.all_files))
        assert bulk_validate_file.delay.called

    @mock.patch('zadmin.tasks.bulk_validate_file')
    def test_ignore_user_disabled_addons(self, bulk_validate_file):
        self.addon.update(disabled_by_user=True)
        r = self.client.post(reverse('zadmin.start_validation'),
                             {'application': amo.FIREFOX.id,
                              'curr_max_version': self.curr_max.id,
                              'target_version': self.appversion('3.7a3').id,
                              'finish_email': 'fliggy@mozilla.com'},
                             follow=True)
        self.assertNoFormErrors(r)
        self.assertRedirects(r, reverse('zadmin.validation'))
        assert not bulk_validate_file.delay.called

    @mock.patch('zadmin.tasks.bulk_validate_file')
    def test_ignore_non_public_addons(self, bulk_validate_file):
        target_ver = self.appversion('3.7a3').id
        for status in (amo.STATUS_DISABLED, amo.STATUS_NULL,
                       amo.STATUS_DELETED):
            self.addon.update(status=status)
            r = self.client.post(reverse('zadmin.start_validation'),
                                 {'application': amo.FIREFOX.id,
                                  'curr_max_version': self.curr_max.id,
                                  'target_version': target_ver,
                                  'finish_email': 'fliggy@mozilla.com'},
                                 follow=True)
            self.assertNoFormErrors(r)
            self.assertRedirects(r, reverse('zadmin.validation'))
            assert not bulk_validate_file.delay.called, (
                            'Addon with status %s should be ignored' % status)

    @mock.patch('zadmin.tasks.bulk_validate_file')
    def test_ignore_lang_packs(self, bulk_validate_file):
        target_ver = self.appversion('3.7a3').id
        self.addon.update(type=amo.ADDON_LPAPP)
        r = self.client.post(reverse('zadmin.start_validation'),
                             {'application': amo.FIREFOX.id,
                              'curr_max_version': self.curr_max.id,
                              'target_version': target_ver,
                              'finish_email': 'fliggy@mozilla.com'},
                             follow=True)
        self.assertNoFormErrors(r)
        self.assertRedirects(r, reverse('zadmin.validation'))
        assert not bulk_validate_file.delay.called, (
                        'Lang pack addons should be ignored')

    @mock.patch('zadmin.tasks.bulk_validate_file')
    def test_ignore_themes(self, bulk_validate_file):
        target_ver = self.appversion('3.7a3').id
        self.addon.update(type=amo.ADDON_THEME)
        self.client.post(reverse('zadmin.start_validation'),
                         {'application': amo.FIREFOX.id,
                          'curr_max_version': self.curr_max.id,
                          'target_version': target_ver,
                          'finish_email': 'fliggy@mozilla.com'})
        assert not bulk_validate_file.delay.called, (
                        'Theme addons should be ignored')

    @mock.patch('zadmin.tasks.bulk_validate_file')
    def test_validate_all_non_disabled_addons(self, bulk_validate_file):
        target_ver = self.appversion('3.7a3').id
        bulk_validate_file.delay.called = False
        self.addon.update(status=amo.STATUS_PUBLIC)
        r = self.client.post(reverse('zadmin.start_validation'),
                             {'application': amo.FIREFOX.id,
                              'curr_max_version': self.curr_max.id,
                              'target_version': target_ver,
                              'finish_email': 'fliggy@mozilla.com'},
                             follow=True)
        self.assertNoFormErrors(r)
        self.assertRedirects(r, reverse('zadmin.validation'))
        assert bulk_validate_file.delay.called, (
            'Addon with status %s should be validated' % self.addon.status)

    def test_grid(self):
        job = self.create_job()
        for res in (dict(errors=0), dict(errors=1)):
            self.create_result(job, self.create_file(), **res)

        r = self.client.get(reverse('zadmin.validation'))
        eq_(r.status_code, 200)
        doc = pq(r.content)
        eq_(doc('table tr td').eq(0).text(), str(job.pk))  # ID
        eq_(doc('table tr td').eq(3).text(), 'Firefox')  # Application
        eq_(doc('table tr td').eq(4).text(), self.curr_max.version)
        eq_(doc('table tr td').eq(5).text(), '3.7a3')
        eq_(doc('table tr td').eq(6).text(), '2')  # tested
        eq_(doc('table tr td').eq(7).text(), '1')  # failing
        eq_(doc('table tr td').eq(8).text()[0], '1')  # passing
        eq_(doc('table tr td').eq(9).text(), '0')  # exceptions

    def test_application_versions_json(self):
        r = self.client.post(reverse('zadmin.application_versions_json'),
                             {'application_id': amo.FIREFOX.id})
        eq_(r.status_code, 200)
        data = json.loads(r.content)
        empty = True
        for id, ver in data['choices']:
            empty = False
            eq_(AppVersion.objects.get(pk=id).version, ver)
        assert not empty, "Unexpected: %r" % data

    def test_job_status(self):
        job = self.create_job()

        def get_data():
            self.create_result(job, self.create_file(), **{})
            r = self.client.post(reverse('zadmin.job_status'),
                                 {'job_ids': json.dumps([job.pk])})
            eq_(r.status_code, 200)
            data = json.loads(r.content)[str(job.pk)]
            return data

        data = get_data()
        eq_(data['completed'], 1)
        eq_(data['total'], 1)
        eq_(data['percent_complete'], '100')
        eq_(data['job_id'], job.pk)
        eq_(data['completed_timestamp'], '')
        job.update(completed=datetime.now())
        data = get_data()
        assert data['completed_timestamp'] != '', (
                            'Unexpected: %s' % data['completed_timestamp'])


class TestBulkUpdate(BulkValidationTest):

    def setUp(self):
        super(TestBulkUpdate, self).setUp()

        self.job = self.create_job(completed=datetime.now())
        self.update_url = reverse('zadmin.notify', args=[self.job.pk])
        self.list_url = reverse('zadmin.validation')
        self.data = {'text': '{{ APPLICATION }} {{ VERSION }}',
                     'subject': '..'}

        self.version_one = Version.objects.create(addon=self.addon)
        self.version_two = Version.objects.create(addon=self.addon)

        appver = AppVersion.objects.get(application=1, version='3.7a1pre')
        for v in self.version_one, self.version_two:
            ApplicationsVersions.objects.create(
                application_id=amo.FIREFOX.id, version=v,
                min=appver, max=appver)

    def test_no_update_link(self):
        self.create_result(self.job, self.create_file(), **{})
        r = self.client.get(self.list_url)
        doc = pq(r.content)
        eq_(doc('table tr td a.set-max-version').text(),
            'Notify and set max versions')

    def test_update_link(self):
        self.create_result(self.job, self.create_file(), **{'valid': 1})
        r = self.client.get(self.list_url)
        doc = pq(r.content)
        eq_(doc('table tr td a.set-max-version').text(),
            'Notify and set max versions')

    def test_update_url(self):
        self.create_result(self.job, self.create_file(), **{'valid': 1})
        r = self.client.get(self.list_url)
        doc = pq(r.content)
        eq_(doc('table tr td a.set-max-version').attr('data-job-url'),
            self.update_url)

    def test_update_anonymous(self):
        self.client.logout()
        r = self.client.post(self.update_url)
        eq_(r.status_code, 302)

    def test_version_pks(self):
        for version in [self.version_one, self.version_two]:
            for x in range(0, 3):
                self.create_result(self.job, self.create_file(version))

        eq_(sorted(updated_versions(self.job)),
            [self.version_one.pk, self.version_two.pk])

    def test_update_passing_only(self):
        self.create_result(self.job, self.create_file(self.version_one))
        self.create_result(self.job, self.create_file(self.version_two),
                           errors=1)

        eq_(sorted(updated_versions(self.job)),
            [self.version_one.pk])

    def test_update_pks(self):
        self.create_result(self.job, self.create_file(self.version))
        r = self.client.post(self.update_url, self.data)
        eq_(r.status_code, 302)
        eq_(self.version.apps.all()[0].max, self.job.target_version)

    def test_update_unclean_pks(self):
        self.create_result(self.job, self.create_file(self.version))
        self.create_result(self.job, self.create_file(self.version),
                           errors=1)
        r = self.client.post(self.update_url, self.data)
        eq_(r.status_code, 302)
        eq_(self.version.apps.all()[0].max, self.job.curr_max_version)

    def test_update_pks_logs(self):
        self.create_result(self.job, self.create_file(self.version))
        eq_(ActivityLog.objects.for_addons(self.addon).count(), 0)
        self.client.post(self.update_url, self.data)
        upd = amo.LOG.MAX_APPVERSION_UPDATED.id
        logs = ActivityLog.objects.for_addons(self.addon).filter(action=upd)
        eq_(logs.count(), 1)
        eq_(logs[0].user, get_task_user())

    def test_update_wrong_version(self):
        self.create_result(self.job, self.create_file(self.version))
        av = self.version.apps.all()[0]
        av.max = self.appversion('3.6')
        av.save()
        self.client.post(self.update_url, self.data)
        eq_(self.version.apps.all()[0].max, self.appversion('3.6'))

    def test_update_all_within_range(self):
        self.create_result(self.job, self.create_file(self.version))
        # Create an appversion in between current and target.
        av = self.version.apps.all()[0]
        av.max = self.appversion('3.7a2')
        av.save()
        self.client.post(self.update_url, self.data)
        eq_(self.version.apps.all()[0].max, self.appversion('3.7a3'))

    def test_version_comparison(self):
        # regression test for bug 691984
        job = self.create_job(completed=datetime.now(),
                              current=self.appversion('3.0.9'),
                              target=self.appversion('3.5'))
        # .* was not sorting right
        self.version.apps.all().update(max=self.appversion('3.0.*'))
        self.create_result(job, self.create_file(self.version))
        self.client.post(reverse('zadmin.notify', args=[job.pk]),
                         self.data)
        eq_(self.version.apps.all()[0].max, self.appversion('3.5'))

    def test_update_different_app(self):
        self.create_result(self.job, self.create_file(self.version))
        target = self.version.apps.all()[0]
        target.application_id = amo.FIREFOX.id
        target.save()
        eq_(self.version.apps.all()[0].max, self.curr_max)

    def test_update_twice(self):
        self.create_result(self.job, self.create_file(self.version))
        self.client.post(self.update_url, self.data)
        eq_(self.version.apps.all()[0].max, self.job.target_version)
        now = self.version.modified
        self.client.post(self.update_url, self.data)
        eq_(self.version.modified, now)

    def test_update_notify(self):
        self.create_result(self.job, self.create_file(self.version))
        self.client.post(self.update_url, self.data)
        eq_(len(mail.outbox), 1)

    def test_update_subject(self):
        data = self.data.copy()
        data['subject'] = '{{ PASSING_ADDONS.0.name }}'
        f = self.create_file(self.version)
        self.create_result(self.job, f)
        self.client.post(self.update_url, data)
        eq_(mail.outbox[0].subject,
            '%s' % self.addon.name)

    @mock.patch('zadmin.tasks.log')
    def test_bulk_email_logs_stats(self, log):
        log.info = mock.Mock()
        self.create_result(self.job, self.create_file(self.version))
        self.client.post(self.update_url, self.data)
        eq_(log.info.call_args_list[-4][0][0],
            '[1@None] bulk update stats for job %s: '
            '{bumped: 1, is_dry_run: 0, processed: 1}'
            % self.job.pk)
        eq_(log.info.call_args_list[-1][0][0],
            '[1@None] bulk email stats for job %s: '
            '{author_emailed: 1, is_dry_run: 0, processed: 1}'
            % self.job.pk)

    def test_application_version(self):
        self.create_result(self.job, self.create_file(self.version))
        self.client.post(self.update_url, self.data)
        eq_(mail.outbox[0].body, 'Firefox 3.7a3')

    def test_multiple_result_links(self):
        # Creates validation results for two files of the same addon:
        results = [
            self.create_result(self.job, self.create_file(self.version)),
            self.create_result(self.job, self.create_file(self.version))]
        self.client.post(self.update_url,
                         {'text': '{{ PASSING_ADDONS.0.links }}',
                          'subject': '..'})
        links = mail.outbox[0].body.split(' ')
        for result in results:
            assert any(ln.endswith(reverse('devhub.bulk_compat_result',
                                           args=(self.addon.slug, result.pk)))
                       for ln in links), ('Expected links: %s' % links)

    def test_notify_mail_preview(self):
        self.create_result(self.job, self.create_file(self.version))
        self.client.post(self.update_url,
                         {'text': 'the message', 'subject': 'the subject',
                          'preview_only': 'on'})
        eq_(len(mail.outbox), 0)
        rs = self.job.get_notify_preview_emails()
        eq_([e.subject for e in rs], ['the subject'])
        # version should not be bumped since it's in preview mode:
        eq_(self.version.apps.all()[0].max, self.max)
        upd = amo.LOG.MAX_APPVERSION_UPDATED.id
        logs = ActivityLog.objects.for_addons(self.addon).filter(action=upd)
        eq_(logs.count(), 0)


class TestBulkNotify(BulkValidationTest):

    def setUp(self):
        super(TestBulkNotify, self).setUp()

        self.job = self.create_job(completed=datetime.now())
        self.update_url = reverse('zadmin.notify', args=[self.job.pk])
        self.syntax_url = reverse('zadmin.notify.syntax')
        self.list_url = reverse('zadmin.validation')

        self.version_one = Version.objects.create(addon=self.addon)
        self.version_two = Version.objects.create(addon=self.addon)

    def test_no_notify_link(self):
        self.create_result(self.job, self.create_file(), **{})
        r = self.client.get(self.list_url)
        doc = pq(r.content)
        eq_(len(doc('table tr td a.notify')), 0)

    def test_notify_link(self):
        self.create_result(self.job, self.create_file(), **{'errors': 1})
        r = self.client.get(self.list_url)
        doc = pq(r.content)
        eq_(doc('table tr td a.set-max-version').text(),
            'Notify and set max versions')

    def test_notify_url(self):
        self.create_result(self.job, self.create_file(), **{'errors': 1})
        r = self.client.get(self.list_url)
        doc = pq(r.content)
        eq_(doc('table tr td a.set-max-version').attr('data-job-url'),
            self.update_url)

    def test_notify_anonymous(self):
        self.client.logout()
        r = self.client.post(self.update_url)
        eq_(r.status_code, 302)

    def test_notify_log(self):
        self.create_result(self.job, self.create_file(self.version),
                           **{'errors': 1})
        eq_(ActivityLog.objects.for_addons(self.addon).count(), 0)
        self.client.post(self.update_url, {'text': '..', 'subject': '..'})
        upd = amo.LOG.BULK_VALIDATION_USER_EMAILED.id
        logs = (ActivityLog.objects.for_user(self.creator)
                           .filter(action=upd))
        eq_(logs.count(), 1)
        eq_(logs[0].user, self.creator)

    def test_compat_bump_log(self):
        self.create_result(self.job, self.create_file(self.version),
                           **{'errors': 0})
        eq_(ActivityLog.objects.for_addons(self.addon).count(), 0)
        self.client.post(self.update_url, {'text': '..', 'subject': '..'})
        upd = amo.LOG.MAX_APPVERSION_UPDATED.id
        logs = ActivityLog.objects.for_addons(self.addon).filter(action=upd)
        eq_(logs.count(), 1)
        eq_(logs[0].user, self.creator)

    def test_notify_mail(self):
        self.create_result(self.job, self.create_file(self.version),
                           **{'errors': 1})
        r = self.client.post(self.update_url,
                             {'text': '..',
                              'subject': '{{ FAILING_ADDONS.0.name }}'})
        eq_(r.status_code, 302)
        eq_(len(mail.outbox), 1)
        eq_(mail.outbox[0].body, '..')
        eq_(mail.outbox[0].subject, self.addon.name)
        eq_(mail.outbox[0].to, [u'del@icio.us'])

    def test_result_links(self):
        result = self.create_result(self.job, self.create_file(self.version),
                                    **{'errors': 1})
        r = self.client.post(self.update_url,
                             {'text': '{{ FAILING_ADDONS.0.links }}',
                              'subject': '...'})
        eq_(r.status_code, 302)
        eq_(len(mail.outbox), 1)
        res = reverse('devhub.bulk_compat_result',
                      args=(self.addon.slug, result.pk))
        email = mail.outbox[0].body
        assert res in email, ('Unexpected message: %s' % email)

    def test_notify_mail_partial(self):
        self.create_result(self.job, self.create_file(self.version),
                           **{'errors': 1})
        self.create_result(self.job, self.create_file(self.version))
        r = self.client.post(self.update_url, {'text': '..', 'subject': '..'})
        eq_(r.status_code, 302)
        eq_(len(mail.outbox), 1)

    def test_notify_mail_multiple(self):
        self.create_result(self.job, self.create_file(self.version),
                           **{'errors': 1})
        self.create_result(self.job, self.create_file(self.version),
                           **{'errors': 1})
        r = self.client.post(self.update_url, {'text': '..', 'subject': '..'})
        eq_(r.status_code, 302)
        eq_(len(mail.outbox), 1)

    def test_notify_mail_preview(self):
        for i in range(2):
            self.create_result(self.job, self.create_file(self.version),
                               **{'errors': 1})
        r = self.client.post(self.update_url,
                             {'text': 'the message', 'subject': 'the subject',
                              'preview_only': 'on'})
        eq_(r.status_code, 302)
        eq_(len(mail.outbox), 0)
        rs = self.job.get_notify_preview_emails()
        eq_([e.subject for e in rs], ['the subject'])

    def test_notify_rendering(self):
        self.create_result(self.job, self.create_file(self.version),
                           **{'errors': 1})
        r = self.client.post(self.update_url,
                             {'text': '{{ FAILING_ADDONS.0.name }}'
                                      '{{ FAILING_ADDONS.0.compat_link }}',
                              'subject': '{{ FAILING_ADDONS.0.name }} blah'})
        eq_(r.status_code, 302)
        eq_(len(mail.outbox), 1)
        url = reverse('devhub.versions.edit', args=[self.addon.pk,
                                                    self.version.pk])
        assert str(self.addon.name) in mail.outbox[0].body
        assert url in mail.outbox[0].body
        assert str(self.addon.name) in mail.outbox[0].subject

    def test_notify_unicode(self):
        self.addon.name = u'འབྲུག་ཡུལ།'
        self.addon.save()
        self.create_result(self.job, self.create_file(self.version),
                           **{'errors': 1})
        r = self.client.post(self.update_url,
                             {'text': '{{ FAILING_ADDONS.0.name }}',
                              'subject': '{{ FAILING_ADDONS.0.name }} blah'})
        eq_(r.status_code, 302)
        eq_(len(mail.outbox), 1)
        eq_(mail.outbox[0].body, self.addon.name)

    def test_notify_template(self):
        for text, res in (['some sample text', True],
                          ['{{ FAILING_ADDONS.0.name }}{% if %}', False]):
            eq_(forms.NotifyForm({'text': text, 'subject': '...'}).is_valid(),
                res)

    def test_notify_syntax(self):
        for text, res in (['some sample text', True],
                          ['{{ FAILING_ADDONS.0.name }}{% if %}', False]):
            r = self.client.post(self.syntax_url, {'text': text,
                                                   'subject': '..'})
            eq_(r.status_code, 200)
            eq_(json.loads(r.content)['valid'], res)

    def test_undeclared_variables(self):
        for text, res in (['{{NOT_DECLARED}}', False],
                          ['{{ NOT_DECLARED }}', False],
                          ["""
                                {{FAILING_ADDONS.0.name}}
                                {{NOT_DECLARED}}
                           """, False],
                          ['{{FAILING_ADDONS.0.name}} {{NOT_DECLARED}}',
                           False],
                          ['{{FAILING_ADDONS.0.name}}', True]):
            r = self.client.post(self.syntax_url, {'text': text,
                                                   'subject': '..'})
            eq_(r.status_code, 200)
            assert json.loads(r.content)['valid'] == res, (
                        'Text %r unexpectedly resulted in %r' % (text, res))

    def test_undeclared_variable_form_submit(self):
        f = forms.NotifyForm({'text': '{{ UNDECLARED }}', 'subject': '...'})
        eq_(f.is_valid(), False)


class TestBulkValidationTask(BulkValidationTest):

    @attr('validator')
    def test_validate(self):
        self.start_validation()
        res = ValidationResult.objects.get()
        self.assertCloseToNow(res.completed)
        assert_no_validation_errors(res)
        eq_(res.errors, 1)  # package could not be found
        eq_(res.valid, False)
        eq_(res.warnings, 0)
        eq_(res.notices, 0)
        v = json.loads(res.validation)
        eq_(v['errors'], 1)
        self.assertCloseToNow(res.validation_job.completed)
        eq_(res.validation_job.stats['total'], 1)
        eq_(res.validation_job.stats['completed'], 1)
        eq_(res.validation_job.stats['passing'], 0)
        eq_(res.validation_job.stats['failing'], 1)
        eq_(res.validation_job.stats['errors'], 0)
        eq_(len(mail.outbox), 1)
        eq_(mail.outbox[0].subject,
            'Behold! Validation results for Firefox %s->%s'
            % (self.curr_max.version, self.new_max.version))
        eq_(mail.outbox[0].to, ['fliggy@mozilla.com'])

    @mock.patch('validator.validate.validate')
    def test_validator_bulk_compat_flag(self, validate):
        try:
            self.start_validation()
        except Exception:
            # We only care about the call to `validate()`, not the result.
            pass
        assert validate.call_args[1].get('compat_test')

    @mock.patch('zadmin.tasks.run_validator')
    def test_task_error(self, run_validator):
        run_validator.side_effect = RuntimeError('validation error')
        try:
            self.start_validation()
        except:
            # the real test is how it's handled, below...
            pass
        res = ValidationResult.objects.get()
        err = res.task_error.strip()
        assert err.endswith('RuntimeError: validation error'), (
                                                    'Unexpected: %s' % err)
        self.assertCloseToNow(res.completed)
        eq_(res.validation_job.stats['total'], 1)
        eq_(res.validation_job.stats['errors'], 1)
        eq_(res.validation_job.stats['passing'], 0)
        eq_(res.validation_job.stats['failing'], 0)

    @mock.patch('zadmin.tasks.run_validator')
    def test_validate_for_appversions(self, run_validator):
        data = {
            "errors": 1,
            "warnings": 50,
            "notices": 1,
            "messages": [],
            "compatibility_summary": {
                "errors": 0,
                "warnings": 0,
                "notices": 0
            },
            "metadata": {}
        }
        run_validator.return_value = json.dumps(data)
        self.start_validation()
        assert run_validator.called
        eq_(run_validator.call_args[1]['for_appversions'],
            {amo.FIREFOX.guid: [self.new_max.version]})

    @mock.patch('zadmin.tasks.run_validator')
    def test_validate_all_tiers(self, run_validator):
        run_validator.return_value = json.dumps(no_op_validation)
        res = self.create_result(self.create_job(), self.create_file(), **{})
        tasks.bulk_validate_file(res.id)
        assert run_validator.called
        eq_(run_validator.call_args[1]['test_all_tiers'], True)

    @mock.patch('zadmin.tasks.run_validator')
    def test_merge_with_compat_summary(self, run_validator):
        data = {
            "errors": 1,
            "detected_type": "extension",
            "success": False,
            "warnings": 50,
            "notices": 1,
            "ending_tier": 5,
            "messages": [
            {
                "description": "A global function was called ...",
                "tier": 3,
                "message": "Global called in dangerous manner",
                "uid": "de93a48831454e0b9d965642f6d6bf8f",
                "id": [],
                "compatibility_type": None,
                "for_appversions": None,
                "type": "warning",
            },
            {
                "description": ("...no longer indicate the language "
                                "of Firefox's UI..."),
                "tier": 5,
                "message": "navigator.language may not behave as expected",
                "uid": "f44c1930887c4d9e8bd2403d4fe0253a",
                "id": [],
                "compatibility_type": "error",
                "for_appversions": {
                    "{ec8030f7-c20a-464f-9b0e-13a3a9e97384}": ["4.2a1pre",
                                                               "5.0a2",
                                                               "6.0a1"]
                },
                "type": "warning"
            }],
            "compatibility_summary": {
                "notices": 1,
                "errors": 6,
                "warnings": 0
            },
            "metadata": {
                "version": "1.0",
                "name": "FastestFox",
                "id": "<id>"
            }
        }
        run_validator.return_value = json.dumps(data)
        res = self.create_result(self.create_job(), self.create_file(), **{})
        tasks.bulk_validate_file(res.id)
        assert run_validator.called
        res = ValidationResult.objects.get(pk=res.pk)
        eq_(res.errors,
            data['errors'] + data['compatibility_summary']['errors'])
        eq_(res.warnings,
            data['warnings'] + data['compatibility_summary']['warnings'])
        eq_(res.notices,
            data['notices'] + data['compatibility_summary']['notices'])

    @mock.patch('validator.validate.validate')
    def test_app_version_overrides(self, validate):
        validate.return_value = json.dumps(no_op_validation)
        self.start_validation(new_max='3.7a4')
        assert validate.called
        overrides = validate.call_args[1]['overrides']
        eq_(overrides['targetapp_minVersion'], {amo.FIREFOX.guid: '3.7a4'})
        eq_(overrides['targetapp_maxVersion'], {amo.FIREFOX.guid: '3.7a4'})

    def create_version(self, addon, statuses, version_str=None):
        max = self.max
        if version_str:
            max = AppVersion.objects.filter(version=version_str)[0]
        version = Version.objects.create(addon=addon)

        ApplicationsVersions.objects.create(application=self.application,
                                            min=self.min, max=max,
                                            version=version)
        for status in statuses:
            File.objects.create(status=status, version=version)
        return version

    def find_files(self, job_kwargs=None):
        if not job_kwargs:
            job_kwargs = {}
        job = self.create_job(**job_kwargs)
        find_files(job)
        return list(job.result_set.values_list('file_id', flat=True))

    def test_getting_disabled(self):
        self.addon.update(status=amo.STATUS_DISABLED)
        eq_(len(self.find_files()), 0)

    def test_getting_deleted(self):
        self.addon.update(status=amo.STATUS_DELETED)
        eq_(len(self.find_files()), 0)

    def test_getting_status(self):
        self.create_version(self.addon, [amo.STATUS_PUBLIC,
                                         amo.STATUS_NOMINATED])
        ids = self.find_files()
        eq_(len(ids), 2)

    def test_getting_latest_public(self):
        old_version = self.create_version(self.addon, [amo.STATUS_PUBLIC])
        self.create_version(self.addon, [amo.STATUS_NULL])
        ids = self.find_files()
        eq_(len(ids), 1)
        eq_(old_version.files.all()[0].pk, ids[0])

    def test_getting_latest_public_order(self):
        self.create_version(self.addon, [amo.STATUS_PURGATORY])
        new_version = self.create_version(self.addon, [amo.STATUS_PUBLIC])
        ids = self.find_files()
        eq_(len(ids), 1)
        eq_(new_version.files.all()[0].pk, ids[0])

    def delete_orig_version(self, fixup=True):
        # Because deleting versions resets the status...
        self.version.delete()
        # Don't really care what status this is, as long
        # as it gets past the first SQL query.
        self.addon.update(status=amo.STATUS_PUBLIC)

    def test_no_versions(self):
        self.delete_orig_version()
        eq_(len(self.find_files()), 0)

    def test_no_files(self):
        self.version.files.all().delete()
        self.addon.update(status=amo.STATUS_PUBLIC)
        eq_(len(self.find_files()), 0)

    def test_not_public(self):
        version = self.create_version(self.addon, [amo.STATUS_LITE])
        self.delete_orig_version()
        ids = self.find_files()
        eq_(len(ids), 1)
        eq_(version.files.all()[0].pk, ids[0])

    def test_not_public_and_newer(self):
        self.create_version(self.addon, [amo.STATUS_LITE])
        new_version = self.create_version(self.addon, [amo.STATUS_LITE])
        self.delete_orig_version()
        ids = self.find_files()
        eq_(len(ids), 1)
        eq_(new_version.files.all()[0].pk, ids[0])

    def test_not_public_w_beta(self):
        self.create_version(self.addon, [amo.STATUS_LITE])
        self.create_version(self.addon, [amo.STATUS_BETA])
        self.delete_orig_version()
        ids = self.find_files()
        eq_(len(ids), 2)

    def test_not_public_w_multiple_files(self):
        self.create_version(self.addon, [amo.STATUS_BETA])
        new_version = self.create_version(self.addon, [amo.STATUS_LITE,
                                                       amo.STATUS_BETA])
        self.delete_orig_version()
        ids = self.find_files()
        eq_(len(ids), 2)
        eq_(sorted([v.id for v in new_version.files.all()]), sorted(ids))

    def test_not_prelim_w_multiple_files(self):
        self.create_version(self.addon, [amo.STATUS_BETA])
        self.create_version(self.addon, [amo.STATUS_BETA,
                                         amo.STATUS_NOMINATED])
        self.delete_orig_version()
        ids = self.find_files()
        eq_(len(ids), 3)

    def test_public_partial(self):
        self.create_version(self.addon, [amo.STATUS_PUBLIC])
        new_version = self.create_version(self.addon, [amo.STATUS_BETA,
                                                       amo.STATUS_DISABLED])
        ids = self.find_files()
        eq_(len(ids), 2)
        assert new_version.files.all()[1].pk not in ids

    def test_getting_w_unreviewed(self):
        old_version = self.create_version(self.addon, [amo.STATUS_PUBLIC])
        new_version = self.create_version(self.addon, [amo.STATUS_UNREVIEWED])
        ids = self.find_files()
        eq_(len(ids), 2)
        eq_(sorted([old_version.files.all()[0].pk,
                    new_version.files.all()[0].pk]),
            sorted(ids))

    def test_multiple_files(self):
        self.create_version(self.addon, [amo.STATUS_PUBLIC, amo.STATUS_PUBLIC,
                                         amo.STATUS_PUBLIC])
        ids = self.find_files()
        eq_(len(ids), 3)

    def test_multiple_public(self):
        self.create_version(self.addon, [amo.STATUS_PUBLIC])
        new_version = self.create_version(self.addon, [amo.STATUS_PUBLIC])
        ids = self.find_files()
        eq_(len(ids), 1)
        eq_(new_version.files.all()[0].pk, ids[0])

    def test_multiple_addons(self):
        addon = Addon.objects.create(type=amo.ADDON_EXTENSION)
        self.create_version(addon, [amo.STATUS_PURGATORY])
        ids = self.find_files()
        eq_(len(ids), 1)
        eq_(self.version.files.all()[0].pk, ids[0])

    def test_no_app(self):
        version = self.create_version(self.addon, [amo.STATUS_LITE])
        self.delete_orig_version()
        version.apps.all().delete()
        ids = self.find_files()
        eq_(len(ids), 0)

    def test_wrong_version(self):
        self.create_version(self.addon, [amo.STATUS_LITE],
                            version_str='4.0b2pre')
        self.delete_orig_version()
        ids = self.find_files()
        eq_(len(ids), 0)

    def test_version_slightly_newer_than_current(self):
        # addon matching current app/version but with a newer public version
        # that is within range of the target app/version.
        # See bug 658739.
        self.create_version(self.addon, [amo.STATUS_PUBLIC],
                            version_str='3.7a2')
        newer = self.create_version(self.addon, [amo.STATUS_PUBLIC],
                                    version_str='3.7a3')
        kw = dict(curr_max_version=self.appversion('3.7a2'),
                  target_version=self.appversion('3.7a4'))
        ids = self.find_files(job_kwargs=kw)
        eq_(newer.files.all()[0].pk, ids[0])

    def test_version_compatible_with_newer_app(self):
        # addon with a newer public version that is already compatible with
        # an app/version higher than the target.
        # See bug 658739.
        self.create_version(self.addon, [amo.STATUS_PUBLIC],
                            version_str='3.7a2')
        # A version that supports a newer Firefox than what we're targeting
        self.create_version(self.addon, [amo.STATUS_PUBLIC],
                            version_str='3.7a4')
        kw = dict(curr_max_version=self.appversion('3.7a2'),
                  target_version=self.appversion('3.7a3'))
        ids = self.find_files(job_kwargs=kw)
        eq_(len(ids), 0)

    def test_version_compatible_with_target_app(self):
        self.create_version(self.addon, [amo.STATUS_PUBLIC],
                            version_str='3.7a2')
        # Already has a version that supports target:
        self.create_version(self.addon, [amo.STATUS_PUBLIC],
                            version_str='3.7a3')
        kw = dict(curr_max_version=self.appversion('3.7a2'),
                  target_version=self.appversion('3.7a3'))
        ids = self.find_files(job_kwargs=kw)
        eq_(len(ids), 0)


class TestTallyValidationErrors(BulkValidationTest):

    def setUp(self):
        super(TestTallyValidationErrors, self).setUp()
        self.data = {
            "errors": 1,
            "warnings": 1,
            "notices": 0,
            "messages": [
            {
                "message": "message one",
                "description": ["message one long"],
                "id": ["path", "to", "test_one"],
                "uid": "de93a48831454e0b9d965642f6d6bf8f",
                "type": "error",
            },
            {
                "message": "message two",
                "description": "message two long",
                "id": ["path", "to", "test_two"],
                "uid": "f44c1930887c4d9e8bd2403d4fe0253a",
                "compatibility_type": "error",
                "type": "warning"
            }],
            "metadata": {},
            "compatibility_summary": {
                "errors": 1,
                "warnings": 1,
                "notices": 0
            }
        }

    def csv(self, job_id):
        r = self.client.get(reverse('zadmin.validation_tally_csv',
                            args=[job_id]))
        eq_(r.status_code, 200)
        rdr = csv.reader(StringIO(r.content))
        header = rdr.next()
        rows = sorted((r for r in rdr), key=lambda r: r[0])
        return header, rows

    @mock.patch('zadmin.tasks.run_validator')
    def test_csv(self, run_validator):
        run_validator.return_value = json.dumps(self.data)
        self.start_validation()
        res = ValidationResult.objects.get()
        eq_(res.task_error, None)
        header, rows = self.csv(res.validation_job.pk)
        eq_(header, ['message_id', 'message', 'long_message',
                     'type', 'addons_affected'])
        eq_(rows.pop(0), ['path.to.test_one',
                          'message one', 'message one long', 'error', '1'])
        eq_(rows.pop(0), ['path.to.test_two',
                          'message two', 'message two long', 'error', '1'])

    def test_count_per_addon(self):
        job = self.create_job()
        data_str = json.dumps(self.data)
        for i in range(3):
            tasks.tally_validation_results(job.pk, data_str)
        header, rows = self.csv(job.pk)
        eq_(rows.pop(0), ['path.to.test_one',
                          'message one', 'message one long', 'error', '3'])
        eq_(rows.pop(0), ['path.to.test_two',
                          'message two', 'message two long', 'error', '3'])

    def test_nested_list_messages(self):
        job = self.create_job()
        self.data['messages'] = [{
            "message": "message one",
            "description": ["message one long", ["something nested"]],
            "id": ["path", "to", "test_one"],
            "uid": "de93a48831454e0b9d965642f6d6bf8f",
            "type": "error",
        }]
        data_str = json.dumps(self.data)
        # This was raising an exception. bug 733845
        tasks.tally_validation_results(job.pk, data_str)


class TestEmailPreview(amo.tests.TestCase):
    fixtures = ['base/addon_3615', 'base/users']

    def setUp(self):
        assert self.client.login(username='admin@mozilla.com',
                                 password='password')
        addon = Addon.objects.get(pk=3615)
        self.topic = EmailPreviewTopic(addon)

    def test_csv(self):
        self.topic.send_mail('the subject', u'Hello Ivan Krsti\u0107',
                             from_email='admin@mozilla.org',
                             recipient_list=['funnyguy@mozilla.org'])
        r = self.client.get(reverse('zadmin.email_preview_csv',
                            args=[self.topic.topic]))
        eq_(r.status_code, 200)
        rdr = csv.reader(StringIO(r.content))
        eq_(rdr.next(), ['from_email', 'recipient_list', 'subject', 'body'])
        eq_(rdr.next(), ['admin@mozilla.org', 'funnyguy@mozilla.org',
                         'the subject', 'Hello Ivan Krsti\xc4\x87'])


class TestMonthlyPick(amo.tests.TestCase):
    fixtures = ['base/addon_3615', 'base/apps', 'base/users']

    def setUp(self):
        assert self.client.login(username='admin@mozilla.com',
                                 password='password')
        self.url = reverse('zadmin.monthly_pick')
        addon = Addon.objects.get(pk=3615)
        MonthlyPick.objects.create(addon=addon,
                                   locale='zh-CN',
                                   blurb="test data",
                                   image="http://www.google.com")
        self.f = self.client.get(self.url).context['form'].initial_forms[0]
        self.initial = self.f.initial

    def test_form_initial(self):
        eq_(self.initial['addon'], 3615)
        eq_(self.initial['locale'], 'zh-CN')
        eq_(self.initial['blurb'], 'test data')
        eq_(self.initial['image'], 'http://www.google.com')

    def test_success_insert(self):
        dupe = initial(self.f)
        del dupe['id']
        dupe.update(locale='fr')
        data = formset(initial(self.f), dupe, initial_count=1)
        self.client.post(self.url, data)
        eq_(MonthlyPick.objects.count(), 2)
        eq_(MonthlyPick.objects.all()[1].locale, 'fr')

    def test_insert_no_image(self):
        dupe = initial(self.f)
        dupe.update(id='', image='', locale='en-US')
        data = formset(initial(self.f), dupe, initial_count=1)
        self.client.post(self.url, data)
        eq_(MonthlyPick.objects.count(), 2)
        eq_(MonthlyPick.objects.all()[1].image, '')

    def test_success_insert_no_locale(self):
        dupe = initial(self.f)
        del dupe['id']
        del dupe['locale']
        data = formset(initial(self.f), dupe, initial_count=1)
        self.client.post(self.url, data)
        eq_(MonthlyPick.objects.count(), 2)
        eq_(MonthlyPick.objects.all()[1].locale, '')

    def test_insert_long_blurb(self):
        dupe = initial(self.f)
        dupe.update(id='', blurb='x' * 201, locale='en-US')
        data = formset(initial(self.f), dupe, initial_count=1)
        r = self.client.post(self.url, data)
        eq_(r.context['form'].errors[1]['blurb'][0],
            'Ensure this value has at most 200 characters (it has 201).')

    def test_success_update(self):
        d = initial(self.f)
        d.update(locale='fr')
        r = self.client.post(self.url, formset(d, initial_count=1))
        eq_(r.status_code, 302)
        eq_(MonthlyPick.objects.all()[0].locale, 'fr')

    def test_success_delete(self):
        d = initial(self.f)
        d.update(DELETE=True)
        self.client.post(self.url, formset(d, initial_count=1))
        eq_(MonthlyPick.objects.count(), 0)

    def test_require_login(self):
        self.client.logout()
        r = self.client.get(self.url)
        eq_(r.status_code, 302)


class TestFeatures(amo.tests.TestCase):
    fixtures = ['base/apps', 'base/users', 'base/collections',
                'base/addon_3615.json']

    def setUp(self):
        assert self.client.login(username='admin@mozilla.com',
                                 password='password')
        self.url = reverse('zadmin.features')
        FeaturedCollection.objects.create(application_id=amo.FIREFOX.id,
                                          locale='zh-CN', collection_id=80)
        self.f = self.client.get(self.url).context['form'].initial_forms[0]
        self.initial = self.f.initial

    def test_form_initial(self):
        eq_(self.initial['application'], amo.FIREFOX.id)
        eq_(self.initial['locale'], 'zh-CN')
        eq_(self.initial['collection'], 80)

    def test_form_attrs(self):
        r = self.client.get(self.url)
        eq_(r.status_code, 200)
        doc = pq(r.content)
        eq_(doc('#features tr').attr('data-app'), str(amo.FIREFOX.id))
        assert doc('#features td.app').hasClass(amo.FIREFOX.short)
        eq_(doc('#features td.collection.loading').attr('data-collection'),
            '80')
        assert doc('#features .collection-ac.js-hidden')
        assert not doc('#features .collection-ac[disabled]')

    def test_disabled_autocomplete_errors(self):
        """If any collection errors, autocomplete field should be enabled."""
        d = dict(application=amo.FIREFOX.id, collection=999)
        data = formset(self.initial, d, initial_count=1)
        r = self.client.post(self.url, data)
        doc = pq(r.content)
        assert not doc('#features .collection-ac[disabled]')

    def test_required_app(self):
        d = dict(locale='zh-CN', collection=80)
        data = formset(self.initial, d, initial_count=1)
        r = self.client.post(self.url, data)
        eq_(r.status_code, 200)
        eq_(r.context['form'].errors[0]['application'],
            ['This field is required.'])
        eq_(r.context['form'].errors[0]['collection'],
            ['Invalid collection for this application.'])

    def test_bad_app(self):
        d = dict(application=999, collection=80)
        data = formset(self.initial, d, initial_count=1)
        r = self.client.post(self.url, data)
        eq_(r.context['form'].errors[0]['application'],
            ['Select a valid choice. That choice is not one of the available '
             'choices.'])

    def test_bad_collection_for_app(self):
        d = dict(application=amo.THUNDERBIRD.id, collection=80)
        data = formset(self.initial, d, initial_count=1)
        r = self.client.post(self.url, data)
        eq_(r.context['form'].errors[0]['collection'],
            ['Invalid collection for this application.'])

    def test_optional_locale(self):
        d = dict(application=amo.FIREFOX.id, collection=80)
        data = formset(self.initial, d, initial_count=1)
        r = self.client.post(self.url, data)
        eq_(r.context['form'].errors, [{}])

    def test_bad_locale(self):
        d = dict(application=amo.FIREFOX.id, locale='klingon', collection=80)
        data = formset(self.initial, d, initial_count=1)
        r = self.client.post(self.url, data)
        eq_(r.context['form'].errors[0]['locale'],
            ['Select a valid choice. klingon is not one of the available '
             'choices.'])

    def test_required_collection(self):
        d = dict(application=amo.FIREFOX.id)
        data = formset(self.initial, d, initial_count=1)
        r = self.client.post(self.url, data)
        eq_(r.context['form'].errors[0]['collection'],
            ['This field is required.'])

    def test_bad_collection(self):
        d = dict(application=amo.FIREFOX.id, collection=999)
        data = formset(self.initial, d, initial_count=1)
        r = self.client.post(self.url, data)
        eq_(r.context['form'].errors[0]['collection'],
            ['Invalid collection for this application.'])

    def test_success_insert(self):
        dupe = initial(self.f)
        del dupe['id']
        dupe.update(locale='fr')
        data = formset(initial(self.f), dupe, initial_count=1)
        self.client.post(self.url, data)
        eq_(FeaturedCollection.objects.count(), 2)
        eq_(FeaturedCollection.objects.all()[1].locale, 'fr')

    def test_success_update(self):
        d = initial(self.f)
        d.update(locale='fr')
        r = self.client.post(self.url, formset(d, initial_count=1))
        eq_(r.status_code, 302)
        eq_(FeaturedCollection.objects.all()[0].locale, 'fr')

    def test_success_delete(self):
        d = initial(self.f)
        d.update(DELETE=True)
        self.client.post(self.url, formset(d, initial_count=1))
        eq_(FeaturedCollection.objects.count(), 0)


class TestLookup(amo.tests.TestCase):
    fixtures = ['base/users']

    def setUp(self):
        assert self.client.login(username='admin@mozilla.com',
                                 password='password')
        self.user = UserProfile.objects.get(pk=999)
        self.url = reverse('zadmin.search', args=['users', 'userprofile'])

    def test_logged_out(self):
        self.client.logout()
        eq_(self.client.get('%s?q=admin' % self.url).status_code, 403)

    def check_results(self, q, expected):
        res = self.client.get(urlparams(self.url, q=q))
        eq_(res.status_code, 200)
        content = json.loads(res.content)
        eq_(len(content), len(expected))
        ids = [int(c['value']) for c in content]
        emails = [u'%s' % c['label'] for c in content]
        for d in expected:
            id = d['value']
            email = u'%s' % d['label']
            assert id in ids, (
                'Expected user ID "%s" not found' % id)
            assert email in emails, (
                'Expected username "%s" not found' % email)

    def test_lookup_wrong_model(self):
        self.url = reverse('zadmin.search', args=['doesnt', 'exist'])
        res = self.client.get(urlparams(self.url, q=''))
        eq_(res.status_code, 404)

    def test_lookup_empty(self):
        users = UserProfile.objects.values('id', 'email')
        self.check_results('', [dict(
            value=u['id'], label=u['email']) for u in users])

    def test_lookup_by_id(self):
        self.check_results(self.user.id, [dict(value=self.user.id,
                                               label=self.user.email)])

    def test_lookup_by_email(self):
        self.check_results(self.user.email, [dict(value=self.user.id,
                                                  label=self.user.email)])

    def test_lookup_by_username(self):
        self.check_results(self.user.username, [dict(value=self.user.id,
                                                     label=self.user.email)])


class TestAddonSearch(amo.tests.ESTestCase):
    fixtures = ['base/users', 'base/addon_3615']

    def setUp(self):
        self.reindex(Addon)
        assert self.client.login(username='admin@mozilla.com',
                                 password='password')
        self.url = reverse('zadmin.addon-search')

    @mock.patch('mkt.webapps.tasks.index_webapps')
    def test_lookup_app(self, index_webapps_mock):
        # Load the Webapp fixture here, as loading it in the
        # TestAddonSearch.fixtures would trigger the reindex, and fail, as
        # this is an AMO test.
        management.call_command('loaddata', 'base/337141-steamcube')
        index_webapps_mock.assert_called()

        res = self.client.get(urlparams(self.url, q='steamcube'))
        eq_(res.status_code, 200)
        links = pq(res.content)('form + h3 + ul li a')
        eq_(len(links), 0)
        if any(li.text().contains('Steamcube') for li in links):
            raise AssertionError('Did not expect webapp in results.')

    def test_lookup_addon(self):
        res = self.client.get(urlparams(self.url, q='delicious'))
        # There's only one result, so it should just forward us to that page.
        eq_(res.status_code, 302)


class TestAddonAdmin(amo.tests.TestCase):
    fixtures = ['base/users', 'base/337141-steamcube', 'base/addon_3615']

    def setUp(self):
        assert self.client.login(username='admin@mozilla.com',
                                 password='password')
        self.url = reverse('admin:addons_addon_changelist')

    def test_no_webapps(self):
        res = self.client.get(self.url)
        doc = pq(res.content)
        rows = doc('#result_list tbody tr')
        eq_(rows.length, 1)
        eq_(rows.find('a').attr('href'),
            '/en-US/admin/models/addons/addon/3615/')


class TestAddonManagement(amo.tests.TestCase):
    fixtures = ['base/addon_3615', 'base/users']

    def setUp(self):
        self.addon = Addon.objects.get(pk=3615)
        self.url = reverse('zadmin.addon_manage', args=[self.addon.slug])
        self.client.login(username='admin@mozilla.com', password='password')

    def _form_data(self, data=None):
        initial_data = {
            'status': '4',
            'highest_status': '4',
            'outstanding': '0',
            'form-0-status': '4',
            'form-0-id': '67442',
            'form-TOTAL_FORMS': '1',
            'form-INITIAL_FORMS': '1',
        }
        if data:
            initial_data.update(data)
        return initial_data

    def test_addon_status_change(self):
        data = self._form_data({'status': '2'})
        r = self.client.post(self.url, data, follow=True)
        eq_(r.status_code, 200)
        addon = Addon.objects.get(pk=3615)
        eq_(addon.status, 2)

    def test_outstanding_change(self):
        data = self._form_data({'outstanding': '1'})
        r = self.client.post(self.url, data, follow=True)
        eq_(r.status_code, 200)
        addon = Addon.objects.get(pk=3615)
        eq_(addon.outstanding, 1)

    def test_addon_file_status_change(self):
        data = self._form_data({'form-0-status': '2'})
        r = self.client.post(self.url, data, follow=True)
        eq_(r.status_code, 200)
        file = File.objects.get(pk=67442)
        eq_(file.status, 2)

    @mock.patch.object(File, 'file_path',
                       amo.tests.AMOPaths().file_fixture_path(
                           'delicious_bookmarks-2.1.106-fx.xpi'))
    def test_regenerate_hash(self):
        version = Version.objects.create(addon_id=3615)
        file = File.objects.create(
            filename='delicious_bookmarks-2.1.106-fx.xpi', version=version)

        r = self.client.post(reverse('zadmin.recalc_hash', args=[file.id]))
        eq_(json.loads(r.content)[u'success'], 1)

        file = File.objects.get(pk=file.id)

        assert file.size, 'File size should not be zero'
        assert file.hash, 'File hash should not be empty'

    @mock.patch.object(File, 'file_path',
                       amo.tests.AMOPaths().file_fixture_path(
                           'delicious_bookmarks-2.1.106-fx.xpi'))
    def test_regenerate_hash_get(self):
        """ Don't allow GET """
        version = Version.objects.create(addon_id=3615)
        file = File.objects.create(
            filename='delicious_bookmarks-2.1.106-fx.xpi', version=version)

        r = self.client.get(reverse('zadmin.recalc_hash', args=[file.id]))
        eq_(r.status_code, 405)  # GET out of here


class TestJetpack(amo.tests.TestCase):
    fixtures = ['base/users']

    def setUp(self):
        self.url = reverse('zadmin.jetpack')
        self.client.login(username='admin@mozilla.com', password='password')

        self.versions = '["1.0", "1.1", "1.2", "1.2.1"]'
        self.patcher = mock.patch('devhub.tasks.urllib2.urlopen')
        self.urlopen_mock = self.patcher.start()
        self.urlopen_mock.return_value = self.urlopener(self.versions)
        self.addCleanup(self.patcher.stop)

    def urlopener(self, content):
        m = mock.Mock()
        m.read.return_value = content
        return m

    def test_no_builder_versions(self):
        self.urlopen_mock.return_value = self.urlopener('xxx')
        r = self.client.get(self.url)
        eq_(r.status_code, 200)
        doc = pq(r.content)
        for field in ('minver', 'maxver'):
            eq_(doc('input[name=%s]' % field).length, 1)

    def test_get_builder_versions(self):
        r = self.client.get(self.url)
        eq_(r.status_code, 200)
        doc = pq(r.content)
        for field in ('minver', 'maxver'):
            eq_(doc('select[name=%s]' % field).length, 1)
            options = doc('select[name=%s] option' % field)
            versions = [''] + json.loads(self.versions)
            for option, version in zip(options, versions):
                eq_(pq(option).attr('value'), version)

    def test_change_range_optional(self):
        r = self.client.post(self.url)
        self.assertRedirects(r, self.url)

    def test_change_range_max_required(self):
        r = self.client.post(self.url, {'minver': '1.0'})
        eq_(r.status_code, 200)
        assert_required(r.context['form'].errors['maxver'][0])

    def test_change_range_min_required(self):
        r = self.client.post(self.url, {'maxver': '1.1'})
        eq_(r.status_code, 200)
        assert_required(r.context['form'].errors['minver'][0])

    def test_change_range_bad(self):
        r = self.client.post(self.url, {'minver': '1.1', 'maxver': '1.0'})
        eq_(r.status_code, 200)
        eq_(r.context['form'].non_field_errors(), ['Invalid version range.'])

    def test_change_range_unknown(self):
        r = self.client.post(self.url, {'minver': '9.0', 'maxver': '99.0'})
        eq_(r.status_code, 200)
        self.assertFormError(r, 'form', 'minver',
            'Select a valid choice. 9.0 is not one of the available choices.')
        self.assertFormError(r, 'form', 'maxver',
            'Select a valid choice. 99.0 is not one of the available choices.')

    def set_range(self, min_, max_):
        r = self.client.post(self.url, {'minver': min_, 'maxver': max_})
        self.assertRedirects(r, self.url)

        r = self.client.get(self.url)
        eq_(r.status_code, 200)
        minver, maxver = r.context['upgrader'].jetpack_versions()
        eq_(minver, min_)
        eq_(maxver, max_)
        eq_(r.context['upgrader'].version(), None)
        eq_(pq(r.content)('input[name=upgrade]').length, 1)

    def test_change_range_success(self):
        self.set_range('1.0', '1.1')

    def submit_upgrade(self):
        r = self.client.post(self.url, {'upgrade': True})
        self.assertRedirects(r, self.url)

    def test_upgrade(self):
        self.set_range('1.2', '1.2.1')
        self.submit_upgrade()

        r = self.client.get(self.url)
        eq_(r.status_code, 200)
        eq_(r.context['upgrader'].version(), '1.2.1')
        eq_(pq(r.content)('input[name=cancel]').length, 1)

    def test_cancel(self):
        self.set_range('1.2', '1.2.1')
        self.submit_upgrade()

        r = self.client.post(self.url, {'cancel': True})
        self.assertRedirects(r, self.url)

        r = self.client.get(self.url)
        eq_(r.status_code, 200)
        eq_(r.context['upgrader'].version(), None)

    @mock.patch('zadmin.views.start_upgrade_task.delay')
    def test_resend(self, start_upgrade):
        self.set_range('1.2', '1.2.1')
        self.submit_upgrade()

        file_id = str(5)
        self.client.get(reverse('zadmin.jetpack.resend', args=[file_id]))
        start_upgrade.assert_called_with([file_id], sdk_version='1.2.1')


class TestMemcache(amo.tests.TestCase):
    fixtures = ['base/addon_3615', 'base/users']

    def setUp(self):
        self.url = reverse('zadmin.memcache')
        cache.set('foo', 'bar')
        self.client.login(username='admin@mozilla.com', password='password')

    def test_login(self):
        self.client.logout()
        eq_(self.client.get(self.url).status_code, 302)

    def test_can_clear(self):
        self.client.post(self.url, {'yes': 'True'})
        eq_(cache.get('foo'), None)

    def test_cant_clear(self):
        self.client.post(self.url, {'yes': 'False'})
        eq_(cache.get('foo'), 'bar')


class TestElastic(amo.tests.ESTestCase):
    fixtures = ['base/addon_3615', 'base/users']

    def setUp(self):
        self.url = reverse('zadmin.elastic')
        self.client.login(username='admin@mozilla.com', password='password')

    def test_login(self):
        self.client.logout()
        self.assertRedirects(self.client.get(self.url),
            reverse('users.login') + '?to=/en-US/admin/elastic')


class TestEmailDevs(amo.tests.TestCase):
    fixtures = ['base/addon_3615', 'base/users']

    def setUp(self):
        self.login('admin')
        self.addon = Addon.objects.get(pk=3615)

    def post(self, recipients='eula', subject='subject', message='msg',
             preview_only=False):
        return self.client.post(reverse('zadmin.email_devs'),
                                dict(recipients=recipients, subject=subject,
                                     message=message,
                                     preview_only=preview_only))

    def test_preview(self):
        res = self.post(preview_only=True)
        self.assertNoFormErrors(res)
        preview = EmailPreviewTopic(topic='email-devs')
        eq_([e.recipient_list for e in preview.filter()], ['del@icio.us'])
        eq_(len(mail.outbox), 0)

    def test_actual(self):
        subject = 'about eulas'
        message = 'message about eulas'
        res = self.post(subject=subject, message=message)
        self.assertNoFormErrors(res)
        self.assertRedirects(res, reverse('zadmin.email_devs'))
        eq_(len(mail.outbox), 1)
        eq_(mail.outbox[0].subject, subject)
        eq_(mail.outbox[0].body, message)
        eq_(mail.outbox[0].to, ['del@icio.us'])
        eq_(mail.outbox[0].from_email, settings.DEFAULT_FROM_EMAIL)

    def test_only_eulas(self):
        self.addon.update(eula=None)
        res = self.post()
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 0)

    def test_sdk_devs(self):
        (File.objects.filter(version__addon=self.addon)
                     .update(jetpack_version='1.5'))
        res = self.post(recipients='sdk')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 1)
        eq_(mail.outbox[0].to, ['del@icio.us'])

    def test_only_sdk_devs(self):
        res = self.post(recipients='sdk')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 0)

    def test_only_apps_with_payments(self):
        self.addon.update(type=amo.ADDON_WEBAPP,
                          premium_type=amo.ADDON_PREMIUM)
        res = self.post(recipients='payments')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 1)

        mail.outbox = []
        self.addon.update(status=amo.STATUS_PENDING)
        res = self.post(recipients='payments')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 1)

        mail.outbox = []
        self.addon.update(status=amo.STATUS_DELETED)
        res = self.post(recipients='payments')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 0)

    def test_only_free_apps_with_new_regions(self):
        self.addon.update(type=amo.ADDON_WEBAPP)
        res = self.post(recipients='free_apps_region_enabled')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 0)
        mail.outbox = []
        res = self.post(recipients='free_apps_region_disabled')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 1)

        mail.outbox = []
        self.addon.update(enable_new_regions=True)
        res = self.post(recipients='free_apps_region_enabled')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 1)
        mail.outbox = []
        res = self.post(recipients='free_apps_region_disabled')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 0)

    def test_only_apps_with_payments_and_new_regions(self):
        self.addon.update(type=amo.ADDON_WEBAPP,
                          premium_type=amo.ADDON_PREMIUM)
        res = self.post(recipients='payments_region_enabled')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 0)
        mail.outbox = []
        res = self.post(recipients='payments_region_disabled')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 1)

        mail.outbox = []
        self.addon.update(enable_new_regions=True)
        res = self.post(recipients='payments_region_enabled')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 1)
        mail.outbox = []
        res = self.post(recipients='payments_region_disabled')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 0)

    def test_only_desktop_apps(self):
        from addons.models import AddonDeviceType
        self.addon.update(type=amo.ADDON_WEBAPP)
        AddonDeviceType.objects.create(addon=self.addon,
            device_type=amo.DEVICE_MOBILE.id)
        res = self.post(recipients='desktop_apps')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 0)

        mail.outbox = []
        AddonDeviceType.objects.create(addon=self.addon,
            device_type=amo.DEVICE_DESKTOP.id)
        res = self.post(recipients='desktop_apps')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 1)

        mail.outbox = []
        self.addon.update(status=amo.STATUS_PENDING)
        res = self.post(recipients='desktop_apps')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 1)

        mail.outbox = []
        self.addon.update(status=amo.STATUS_DELETED)
        res = self.post(recipients='desktop_apps')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 0)

    def test_only_apps(self):
        self.addon.update(type=amo.ADDON_WEBAPP)
        res = self.post(recipients='apps')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 1)

    def test_only_extensions(self):
        self.addon.update(type=amo.ADDON_EXTENSION)
        res = self.post(recipients='all_extensions')
        self.assertNoFormErrors(res)
        eq_(len(mail.outbox), 1)

    def test_ignore_deleted_always(self):
        self.addon.update(status=amo.STATUS_DELETED)
        for name, label in DevMailerForm._choices:
            res = self.post(recipients=name)
            self.assertNoFormErrors(res)
            eq_(len(mail.outbox), 0)

    def test_exclude_pending_for_addons(self):
        self.addon.update(status=amo.STATUS_PENDING)
        for name, label in DevMailerForm._choices:
            if name in ('payments', 'desktop_apps'):
                continue
            res = self.post(recipients=name)
            self.assertNoFormErrors(res)
            eq_(len(mail.outbox), 0)


class TestPerms(amo.tests.TestCase):
    fixtures = ['base/users', 'base/apps', 'zadmin/tests/flagged']

    def test_admin_user(self):
        # Admin should see views with Django's perm decorator and our own.
        assert self.client.login(username='admin@mozilla.com',
                                 password='password')
        eq_(self.client.get(reverse('zadmin.index')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.settings')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.flagged')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.langpacks')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.addon-search')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.monthly_pick')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.features')).status_code, 200)
        eq_(self.client.get(
            reverse('discovery.module_admin')).status_code, 200)

    def test_staff_user(self):
        # Staff users have some privileges.
        user = UserProfile.objects.get(email='regular@mozilla.com')
        group = Group.objects.create(name='Staff', rules='AdminTools:View')
        GroupUser.objects.create(group=group, user=user)
        assert self.client.login(username='regular@mozilla.com',
                                 password='password')
        eq_(self.client.get(reverse('zadmin.index')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.settings')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.flagged')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.langpacks')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.addon-search')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.monthly_pick')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.features')).status_code, 200)
        eq_(self.client.get(
            reverse('discovery.module_admin')).status_code, 200)

    def test_sr_reviewers_user(self):
        # Sr Reviewers users have only a few privileges.
        user = UserProfile.objects.get(email='regular@mozilla.com')
        group = Group.objects.create(name='Sr Reviewer',
                                     rules='ReviewerAdminTools:View')
        GroupUser.objects.create(group=group, user=user)
        assert self.client.login(username='regular@mozilla.com',
                                 password='password')
        eq_(self.client.get(reverse('zadmin.index')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.flagged')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.langpacks')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.addon-search')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.settings')).status_code, 403)

    def test_bulk_compat_user(self):
        # Bulk Compatibility Updaters only have access to /admin/validation/*.
        user = UserProfile.objects.get(email='regular@mozilla.com')
        group = Group.objects.create(name='Bulk Compatibility Updaters',
                                     rules='BulkValidationAdminTools:View')
        GroupUser.objects.create(group=group, user=user)
        assert self.client.login(username='regular@mozilla.com',
                                 password='password')
        eq_(self.client.get(reverse('zadmin.index')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.validation')).status_code, 200)
        eq_(self.client.get(reverse('zadmin.flagged')).status_code, 403)
        eq_(self.client.get(reverse('zadmin.langpacks')).status_code, 403)
        eq_(self.client.get(reverse('zadmin.addon-search')).status_code, 403)
        eq_(self.client.get(reverse('zadmin.settings')).status_code, 403)

    def test_unprivileged_user(self):
        # Unprivileged user.
        assert self.client.login(username='regular@mozilla.com',
                                 password='password')
        eq_(self.client.get(reverse('zadmin.index')).status_code, 403)
        eq_(self.client.get(reverse('zadmin.settings')).status_code, 403)
        eq_(self.client.get(reverse('zadmin.flagged')).status_code, 403)
        eq_(self.client.get(reverse('zadmin.langpacks')).status_code, 403)
        eq_(self.client.get(reverse('zadmin.addon-search')).status_code, 403)
        eq_(self.client.get(reverse('zadmin.monthly_pick')).status_code, 403)
        eq_(self.client.get(reverse('zadmin.features')).status_code, 403)
        eq_(self.client.get(
            reverse('discovery.module_admin')).status_code, 403)
        # Anonymous users should also get a 403.
        self.client.logout()
        self.assertRedirects(self.client.get(reverse('zadmin.index')),
                             reverse('users.login') + '?to=/en-US/admin/')
