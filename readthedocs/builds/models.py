import logging
import re
import os.path
from shutil import rmtree

from django.core.urlresolvers import reverse
from django.conf import settings
from django.db import models
from django.utils.translation import ugettext_lazy as _, ugettext

from guardian.shortcuts import assign
from taggit.managers import TaggableManager

from readthedocs.privacy.loader import (VersionManager, RelatedProjectManager,
                                        RelatedBuildManager)
from readthedocs.projects.models import Project
from readthedocs.projects.constants import (PRIVACY_CHOICES, REPO_TYPE_GIT,
                                            REPO_TYPE_HG)

from .constants import (BUILD_STATE, BUILD_TYPES, VERSION_TYPES,
                        LATEST, NON_REPOSITORY_VERSIONS, STABLE,
                        BUILD_STATE_FINISHED)
from .version_slug import VersionSlugField


DEFAULT_VERSION_PRIVACY_LEVEL = getattr(settings, 'DEFAULT_VERSION_PRIVACY_LEVEL', 'public')


log = logging.getLogger(__name__)


class Version(models.Model):

    """
    Attributes
    ----------

    ``identifier``
        The identifier is the ID for the revision this is version is for. This
        might be the revision number (e.g. in SVN), or the commit hash (e.g. in
        Git). If the this version is pointing to a branch, then ``identifier``
        will contain the branch name.

    ``verbose_name``
        This is the actual name that we got for the commit stored in
        ``identifier``. This might be the tag or branch name like ``"v1.0.4"``.
        However this might also hold special version names like ``"latest"``
        and ``"stable"``.

    ``slug``
        The slug is the slugified version of ``verbose_name`` that can be used
        in the URL to identify this version in a project. It's also used in the
        filesystem to determine how the paths for this version are called. It
        must not be used for any other identifying purposes.
    """
    project = models.ForeignKey(Project, verbose_name=_('Project'),
                                related_name='versions')
    type = models.CharField(
        _('Type'), max_length=20,
        choices=VERSION_TYPES, default='unknown',
    )
    # used by the vcs backend
    identifier = models.CharField(_('Identifier'), max_length=255)

    verbose_name = models.CharField(_('Verbose Name'), max_length=255)

    slug = VersionSlugField(_('Slug'), max_length=255,
                            populate_from='verbose_name')

    supported = models.BooleanField(_('Supported'), default=True)
    active = models.BooleanField(_('Active'), default=False)
    built = models.BooleanField(_('Built'), default=False)
    uploaded = models.BooleanField(_('Uploaded'), default=False)
    privacy_level = models.CharField(
        _('Privacy Level'), max_length=20, choices=PRIVACY_CHOICES,
        default=DEFAULT_VERSION_PRIVACY_LEVEL, help_text=_("Level of privacy for this Version.")
    )
    tags = TaggableManager(blank=True)
    machine = models.BooleanField(_('Machine Created'), default=False)
    objects = VersionManager()

    class Meta:
        unique_together = [('project', 'slug')]
        ordering = ['-verbose_name']
        permissions = (
            # Translators: Permission around whether a user can view the
            #              version
            ('view_version', _('View Version')),
        )

    def __unicode__(self):
        return ugettext(u"Version %(version)s of %(project)s (%(pk)s)" % {
            'version': self.verbose_name,
            'project': self.project,
            'pk': self.pk
        })

    @property
    def commit_name(self):
        """Return the branch name, the tag name or the revision identifier."""
        if self.type == 'branch':
            return self.identifier
        if self.verbose_name in NON_REPOSITORY_VERSIONS:
            return self.identifier
        return self.verbose_name

    def get_absolute_url(self):
        if not self.built and not self.uploaded:
            return reverse('project_version_detail', kwargs={
                'project_slug': self.project.slug,
                'version_slug': self.slug,
            })
        return self.project.get_docs_url(version_slug=self.slug)

    def save(self, *args, **kwargs):
        """
        Add permissions to the Version for all owners on save.
        """
        obj = super(Version, self).save(*args, **kwargs)
        for owner in self.project.users.all():
            assign('view_version', owner, self)
        self.project.sync_supported_versions()
        return obj

    @property
    def remote_slug(self):
        if self.slug == LATEST:
            if self.project.default_branch:
                return self.project.default_branch
            else:
                return self.project.vcs_repo().fallback_branch
        else:
            return self.slug

    @property
    def identifier_friendly(self):
        '''Return display friendly identifier'''
        re_sha = re.compile(r'^[0-9a-f]{40}$', re.I)
        if re_sha.match(str(self.identifier)):
            return self.identifier[:8]
        return self.identifier

    def get_subdomain_url(self):
        use_subdomain = getattr(settings, 'USE_SUBDOMAIN', False)
        if use_subdomain:
            return "/%s/%s/" % (
                self.project.language,
                self.slug,
            )
        else:
            return reverse('docs_detail', kwargs={
                'project_slug': self.project.slug,
                'lang_slug': self.project.language,
                'version_slug': self.slug,
                'filename': ''
            })

    def get_subproject_url(self):
        return "/projects/%s/%s/%s/" % (
            self.project.slug,
            self.project.language,
            self.slug,
        )

    def get_downloads(self, pretty=False):
        project = self.project
        data = {}
        if pretty:
            if project.has_pdf(self.slug):
                data['PDF'] = project.get_production_media_url('pdf', self.slug)
            if project.has_htmlzip(self.slug):
                data['HTML'] = project.get_production_media_url('htmlzip', self.slug)
            if project.has_epub(self.slug):
                data['Epub'] = project.get_production_media_url('epub', self.slug)
        else:
            if project.has_pdf(self.slug):
                data['pdf'] = project.get_production_media_url('pdf', self.slug)
            if project.has_htmlzip(self.slug):
                data['htmlzip'] = project.get_production_media_url('htmlzip', self.slug)
            if project.has_epub(self.slug):
                data['epub'] = project.get_production_media_url('epub', self.slug)
        return data

    def get_conf_py_path(self):
        conf_py_path = self.project.conf_file(self.slug)
        conf_py_path = conf_py_path.replace(
            self.project.checkout_path(self.slug), '')
        return conf_py_path.replace('conf.py', '')

    def get_build_path(self):
        '''Return version build path if path exists, otherwise `None`'''
        path = self.project.checkout_path(version=self.slug)
        if os.path.exists(path):
            return path
        return None

    def clean_build_path(self):
        '''Clean build path for project version

        Ensure build path is clean for project version. Used to ensure stale
        build checkouts for each project version are removed.
        '''
        try:
            path = self.get_build_path()
            if path is not None:
                log.debug('Removing build path {0} for {1}'.format(
                    path, self))
                rmtree(path)
        except OSError:
            log.error('Build path cleanup failed', exc_info=True)

    def get_vcs_slug(self):
        slug = None
        if self.slug == LATEST:
            if self.project.default_branch:
                slug = self.project.default_branch
            else:
                slug = self.project.vcs_repo().fallback_branch
        elif self.slug == STABLE:
            return self.identifier
        else:
            slug = self.slug
        # https://github.com/rtfd/readthedocs.org/issues/561
        # version identifiers with / characters in branch name need to un-slugify
        # the branch name for remote links to work
        if slug.replace('-', '/') in self.identifier:
            slug = slug.replace('-', '/')
        return slug

    def get_github_url(self, docroot, filename, source_suffix='.rst', action='view'):
        GITHUB_REGEXS = [
            re.compile('github.com/(.+)/(.+)(?:\.git){1}'),
            re.compile('github.com/(.+)/(.+)'),
            re.compile('github.com:(.+)/(.+).git'),
        ]
        GITHUB_URL = ('https://github.com/{user}/{repo}/'
                      '{action}/{version}{docroot}{path}{source_suffix}')

        repo_url = self.project.repo
        if 'github' not in repo_url:
            return ''

        if not docroot:
            return ''
        else:
            if docroot[0] != '/':
                docroot = "/%s" % docroot
            if docroot[-1] != '/':
                docroot = "%s/" % docroot

        if action == 'view':
            action_string = 'blob'
        elif action == 'edit':
            action_string = 'edit'

        for regex in GITHUB_REGEXS:
            match = regex.search(repo_url)
            if match:
                user, repo = match.groups()
                break
        else:
            return ''
        repo = repo.rstrip('/')

        return GITHUB_URL.format(
            user=user,
            repo=repo,
            version=self.remote_slug,
            docroot=docroot,
            path=filename,
            source_suffix=source_suffix,
            action=action_string,
        )

    def get_bitbucket_url(self, docroot, filename, source_suffix='.rst'):
        BB_REGEXS = [
            re.compile('bitbucket.org/(.+)/(.+).git'),
            re.compile('bitbucket.org/(.+)/(.+)/'),
            re.compile('bitbucket.org/(.+)/(.+)'),
        ]
        BB_URL = 'https://bitbucket.org/{user}/{repo}/src/{version}{docroot}{path}{source_suffix}'

        repo_url = self.project.repo
        if 'bitbucket' not in repo_url:
            return ''
        if not docroot:
            return ''

        for regex in BB_REGEXS:
            match = regex.search(repo_url)
            if match:
                user, repo = match.groups()
                break
        else:
            return ''
        repo = repo.rstrip('/')

        return BB_URL.format(
            user=user,
            repo=repo,
            version=self.remote_slug,
            docroot=docroot,
            path=filename,
            source_suffix=source_suffix,
        )


class VersionAlias(models.Model):
    project = models.ForeignKey(Project, verbose_name=_('Project'),
                                related_name='aliases')
    from_slug = models.CharField(_('From slug'), max_length=255, default='')
    to_slug = models.CharField(_('To slug'), max_length=255, default='',
                               blank=True)
    largest = models.BooleanField(_('Largest'), default=False)

    def __unicode__(self):
        return ugettext(u"Alias for %(project)s: %(from)s -> %(to)s" % {
            'project': self.project,
            'from': self.from_slug,
            'to': self.to_slug,
        })


class Build(models.Model):
    project = models.ForeignKey(Project, verbose_name=_('Project'),
                                related_name='builds')
    version = models.ForeignKey(Version, verbose_name=_('Version'), null=True,
                                related_name='builds')
    type = models.CharField(_('Type'), max_length=55, choices=BUILD_TYPES,
                            default='html')
    state = models.CharField(_('State'), max_length=55, choices=BUILD_STATE,
                             default='finished')
    date = models.DateTimeField(_('Date'), auto_now_add=True)
    success = models.BooleanField(_('Success'), default=True)

    setup = models.TextField(_('Setup'), null=True, blank=True)
    setup_error = models.TextField(_('Setup error'), null=True, blank=True)
    output = models.TextField(_('Output'), default='', blank=True)
    error = models.TextField(_('Error'), default='', blank=True)
    exit_code = models.IntegerField(_('Exit code'), null=True, blank=True)
    commit = models.CharField(_('Commit'), max_length=255, null=True, blank=True)

    length = models.IntegerField(_('Build Length'), null=True, blank=True)

    builder = models.CharField(_('Builder'), max_length=255, null=True, blank=True)

    # Manager

    objects = RelatedProjectManager()

    class Meta:
        ordering = ['-date']
        get_latest_by = 'date'
        index_together = [
            ['version', 'state', 'type']
        ]

    def __unicode__(self):
        return ugettext(u"Build %(project)s for %(usernames)s (%(pk)s)" % {
            'project': self.project,
            'usernames': ' '.join(self.project.users.all()
                                  .values_list('username', flat=True)),
            'pk': self.pk,
        })

    @models.permalink
    def get_absolute_url(self):
        return ('builds_detail', [self.project.slug, self.pk])

    @property
    def finished(self):
        '''Return if build has a finished state'''
        return self.state == BUILD_STATE_FINISHED


class BuildCommandResultMixin(object):
    '''Mixin for common command result methods/properties

    Shared methods between the database model :py:cls:`BuildCommandResult` and
    non-model respresentations of build command results from the API
    '''

    @property
    def successful(self):
        '''Did the command exit with a successful exit code'''
        return self.exit_code == 0

    @property
    def failed(self):
        '''Did the command exit with a failing exit code

        Helper for inverse of :py:meth:`successful`'''
        return not self.successful


class BuildCommandResult(BuildCommandResultMixin, models.Model):
    build = models.ForeignKey(Build, verbose_name=_('Build'),
                              related_name='commands')

    command = models.TextField(_('Command'))
    description = models.TextField(_('Description'), null=True, blank=True)
    output = models.TextField(_('Command output'), null=True, blank=True)
    exit_code = models.IntegerField(_('Command exit code'), default=0)

    start_time = models.DateTimeField(_('Start time'))
    end_time = models.DateTimeField(_('End time'))

    class Meta:
        ordering = ['start_time']
        get_latest_by = 'start_time'

    objects = RelatedBuildManager()

    def __unicode__(self):
        return (ugettext(u'Build command {pk} for build {build}')
                .format(pk=self.pk, build=self.build))

    @property
    def run_time(self):
        """Total command runtime in seconds"""
        if self.start_time is not None and self.end_time is not None:
            diff = self.end_time - self.start_time
            return diff.seconds
