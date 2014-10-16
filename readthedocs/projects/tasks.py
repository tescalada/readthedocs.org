"""Tasks related to projects

This includes fetching repository code, cleaning ``conf.py`` files, and
rebuilding documentation.
"""

import os
import shutil
import json
import logging
import socket
import requests
import hashlib
from collections import defaultdict

from celery import task, Task
from djcelery import celery as celery_app
from django.conf import settings
from django.core.urlresolvers import reverse
from django.utils.translation import ugettext_lazy as _

from readthedocs.builds.constants import (LATEST,
                                          BUILD_STATE_CLONING,
                                          BUILD_STATE_INSTALLING,
                                          BUILD_STATE_BUILDING,
                                          BUILD_STATE_FINISHED)
from readthedocs.builds.models import Build, Version
from readthedocs.core.utils import send_email, run_on_app_servers, broadcast
from readthedocs.core.symlink import PublicSymlink, PrivateSymlink
from readthedocs.cdn.purge import purge
from readthedocs.doc_builder.loader import get_builder_class
from readthedocs.doc_builder.config import load_yaml_config
from readthedocs.doc_builder.environments import (LocalEnvironment,
                                                  DockerEnvironment)
from readthedocs.doc_builder.exceptions import BuildEnvironmentError
from readthedocs.doc_builder.python_environments import Virtualenv, Conda
from readthedocs.projects.exceptions import ProjectImportError
from readthedocs.projects.models import ImportedFile, Project, Domain
from readthedocs.projects.utils import make_api_version, make_api_project
from readthedocs.projects.constants import LOG_TEMPLATE
from readthedocs.privacy.loader import Syncer
from readthedocs.search.parse_json import process_all_json_files
from readthedocs.search.utils import process_mkdocs_json
from readthedocs.restapi.utils import index_search_request
from readthedocs.vcs_support import utils as vcs_support_utils
from readthedocs.api.client import api as api_v1
from readthedocs.restapi.client import api as api_v2
from readthedocs.projects.signals import before_vcs, after_vcs, before_build, after_build
from readthedocs.core.resolver import resolve_path


log = logging.getLogger(__name__)

HTML_ONLY = getattr(settings, 'HTML_ONLY_PROJECTS', ())


class UpdateDocsTask(Task):

    """
    The main entry point for updating documentation.

    It handles all of the logic around whether a project is imported or we
    created it.  Then it will build the html docs and other requested parts.

    `pk`
        Primary key of the project to update

    `record`
        Whether or not to keep a record of the update in the database. Useful
        for preventing changes visible to the end-user when running commands
        from the shell, for example.

    """

    max_retries = 5
    default_retry_delay = (7 * 60)
    name = 'update_docs'

    def __init__(self, build_env=None, python_env=None, config=None,
                 force=False, search=True, localmedia=True,
                 build=None, project=None, version=None):
        self.build_env = build_env
        self.python_env = python_env
        self.build_force = force
        self.build_search = search
        self.build_localmedia = localmedia
        self.build = {}
        if build is not None:
            self.build = build
        self.version = {}
        if version is not None:
            self.version = version
        self.project = {}
        if project is not None:
            self.project = project
        if config is not None:
            self.config = config

    def _log(self, msg):
        log.info(LOG_TEMPLATE
                 .format(project=self.project.slug,
                         version=self.version.slug,
                         msg=msg))

    def run(self, pk, version_pk=None, build_pk=None, record=True, docker=False,
            search=True, force=False, localmedia=True, **kwargs):

        self.project = self.get_project(pk)
        self.version = self.get_version(self.project, version_pk)
        self.build = self.get_build(build_pk)
        self.build_search = search
        self.build_localmedia = localmedia
        self.build_force = force
        self.config = None

        env_cls = LocalEnvironment
        self.setup_env = env_cls(project=self.project, version=self.version,
                                 build=self.build, record=record,
                                 report_build_success=False)

        # Environment used for code checkout & initial configuration reading
        with self.setup_env:
            if self.project.skip:
                raise BuildEnvironmentError(
                    _('Builds for this project are temporarily disabled'))
            try:
                self.setup_vcs()
            except vcs_support_utils.LockTimeout as e:
                self.retry(exc=e, throw=False)
                raise BuildEnvironmentError(
                    'Version locked, retrying in 5 minutes.',
                    status_code=423
                )

            self.config = load_yaml_config(version=self.version)

        if self.setup_env.failed or self.config is None:
            self.send_notifications()
            self.setup_env.update_build(state=BUILD_STATE_FINISHED)
            return None

        if self.setup_env.successful and not self.project.has_valid_clone:
            self.set_valid_clone()

        env_vars = self.get_env_vars()
        if docker or settings.DOCKER_ENABLE:
            env_cls = DockerEnvironment
        self.build_env = env_cls(project=self.project, version=self.version,
                                 build=self.build, record=record, environment=env_vars)

        # Environment used for building code, usually with Docker
        with self.build_env:

            if self.project.documentation_type == 'auto':
                self.update_documentation_type()

            python_env_cls = Virtualenv
            if self.config.use_conda:
                self._log('Using conda')
                python_env_cls = Conda
            self.python_env = python_env_cls(version=self.version,
                                             build_env=self.build_env,
                                             config=self.config)

            self.setup_environment()

            # TODO the build object should have an idea of these states, extend
            # the model to include an idea of these outcomes
            outcomes = self.build_docs()
            build_id = self.build.get('id')

            # Web Server Tasks
            if build_id:
                finish_build.delay(
                    version_pk=self.version.pk,
                    build_pk=build_id,
                    hostname=socket.gethostname(),
                    html=outcomes['html'],
                    search=outcomes['search'],
                    localmedia=outcomes['localmedia'],
                    pdf=outcomes['pdf'],
                    epub=outcomes['epub'],
                )

        if self.build_env.failed:
            self.send_notifications()

    @staticmethod
    def get_project(project_pk):
        """Get project from API"""
        project_data = api_v1.project(project_pk).get()
        project = make_api_project(project_data)
        return project

    @staticmethod
    def get_version(project, version_pk):
        """Ensure we're using a sane version"""
        if version_pk:
            version_data = api_v1.version(version_pk).get()
        else:
            version_data = (api_v1
                            .version(project.slug)
                            .get(slug=LATEST)['objects'][0])
        return make_api_version(version_data)

    @staticmethod
    def get_build(build_pk):
        """
        Retrieve build object from API

        :param build_pk: Build primary key
        """
        build = {}
        if build_pk:
            build = api_v2.build(build_pk).get()
        return dict((key, val) for (key, val) in build.items()
                    if key not in ['project', 'version', 'resource_uri',
                                   'absolute_uri'])

    def setup_vcs(self):
        """
        Update the checkout of the repo to make sure it's the latest.

        This also syncs versions in the DB.

        :param build_env: Build environment
        """
        self.setup_env.update_build(state=BUILD_STATE_CLONING)

        self._log(msg='Updating docs from VCS')
        try:
            update_imported_docs(self.version.pk)
            commit = self.project.vcs_repo(self.version.slug).commit
            if commit:
                self.build['commit'] = commit
        except ProjectImportError as e:
            log.error(
                LOG_TEMPLATE.format(project=self.project.slug,
                                    version=self.version.slug,
                                    msg=str(e)),
                exc_info=True,
            )
            raise BuildEnvironmentError('Failed to import project',
                                        status_code=404)

    def get_env_vars(self):
        """
        Get bash environment variables used for all builder commands.
        """
        env = {
            'READTHEDOCS': True,
            'READTHEDOCS_VERSION': self.version.slug,
            'READTHEDOCS_PROJECT': self.project.slug
        }

        if self.config.use_conda:
            env.update({
                'CONDA_ENVS_PATH': os.path.join(self.project.doc_path, 'conda'),
                'CONDA_DEFAULT_ENV': self.version.slug,
                'BIN_PATH': os.path.join(self.project.doc_path, 'conda', self.version.slug, 'bin')
            })
        else:
            env.update({
                'BIN_PATH': os.path.join(self.project.doc_path, 'envs', self.version.slug, 'bin')
            })

        return env

    def set_valid_clone(self):
        """Mark on the project that it has been cloned properly."""
        project_data = api_v2.project(self.project.pk).get()
        project_data['has_valid_clone'] = True
        api_v2.project(self.project.pk).put(project_data)
        self.project.has_valid_clone = True

    def update_documentation_type(self):
        """
        Force Sphinx for 'auto' documentation type

        This used to determine the type and automatically set the documentation
        type to Sphinx for rST and Mkdocs for markdown. It now just forces
        Sphinx, due to markdown support.
        """
        ret = 'sphinx'
        project_data = api_v2.project(self.project.pk).get()
        project_data['documentation_type'] = ret
        api_v2.project(self.project.pk).put(project_data)
        self.project.documentation_type = ret

    def setup_environment(self):
        """
        Build the virtualenv and install the project into it.

        Always build projects with a virtualenv.

        :param build_env: Build environment to pass commands and execution through.
        """
        self.build_env.update_build(state=BUILD_STATE_INSTALLING)

        self.python_env.delete_existing_build_dir()
        self.python_env.setup_base()
        self.python_env.install_core_requirements()
        self.python_env.install_user_requirements()
        self.python_env.install_package()

    def build_docs(self):
        """Wrapper to all build functions

        Executes the necessary builds for this task and returns whether the
        build was successful or not.

        :returns: Build outcomes with keys for html, search, localmedia, pdf,
                  and epub
        :rtype: dict
        """
        self.build_env.update_build(state=BUILD_STATE_BUILDING)
        before_build.send(sender=self.version)

        outcomes = defaultdict(lambda: False)
        with self.project.repo_nonblockinglock(
                version=self.version,
                max_lock_age=getattr(settings, 'REPO_LOCK_SECONDS', 30)):
            outcomes['html'] = self.build_docs_html()
            outcomes['search'] = self.build_docs_search()
            outcomes['localmedia'] = self.build_docs_localmedia()
            outcomes['pdf'] = self.build_docs_pdf()
            outcomes['epub'] = self.build_docs_epub()

        after_build.send(sender=self.version)
        return outcomes

    def build_docs_html(self):
        """Build HTML docs"""
        html_builder = get_builder_class(self.project.documentation_type)(
            build_env=self.build_env,
            python_env=self.python_env,
        )
        if self.build_force:
            html_builder.force()
        html_builder.append_conf()
        success = html_builder.build()
        if success:
            html_builder.move()

        # Gracefully attempt to move files via task on web workers.
        try:
            move_files.delay(
                version_pk=self.version.pk,
                html=True,
                hostname=socket.gethostname(),
            )
        except socket.error:
            # TODO do something here
            pass

        return success

    def build_docs_search(self):
        """Build search data with separate build"""
        if self.build_search:
            if self.project.is_type_mkdocs:
                return self.build_docs_class('mkdocs_json')
            if self.project.is_type_sphinx:
                return self.build_docs_class('sphinx_search')
        return False

    def build_docs_localmedia(self):
        """Get local media files with separate build"""
        if 'htmlzip' not in self.config.formats:
            return False

        if self.build_localmedia:
            if self.project.is_type_sphinx:
                return self.build_docs_class('sphinx_singlehtmllocalmedia')
        return False

    def build_docs_pdf(self):
        """Build PDF docs"""
        if ('pdf' not in self.config.formats or
            self.project.slug in HTML_ONLY or
                not self.project.is_type_sphinx):
            return False
        return self.build_docs_class('sphinx_pdf')

    def build_docs_epub(self):
        """Build ePub docs"""
        if ('epub' not in self.config.formats or
            self.project.slug in HTML_ONLY or
                not self.project.is_type_sphinx):
            return False
        return self.build_docs_class('sphinx_epub')

    def build_docs_class(self, builder_class):
        """Build docs with additional doc backends

        These steps are not necessarily required for the build to halt, so we
        only raise a warning exception here. A hard error will halt the build
        process.
        """
        builder = get_builder_class(builder_class)(self.build_env, python_env=self.python_env)
        success = builder.build()
        builder.move()
        return success

    def send_notifications(self):
        """Send notifications on build failure"""
        send_notifications.delay(self.version.pk, build_pk=self.build['id'])


update_docs = celery_app.tasks[UpdateDocsTask.name]


@task()
def update_imported_docs(version_pk):
    """
    Check out or update the given project's repository

    :param version_pk: Version id to update
    """
    version_data = api_v1.version(version_pk).get()
    version = make_api_version(version_data)
    project = version.project
    ret_dict = {}

    # Make Dirs
    if not os.path.exists(project.doc_path):
        os.makedirs(project.doc_path)

    if not project.vcs_repo():
        raise ProjectImportError(("Repo type '{0}' unknown".format(project.repo_type)))

    with project.repo_nonblockinglock(
            version=version,
            max_lock_age=getattr(settings, 'REPO_LOCK_SECONDS', 30)):

        # Get the actual code on disk
        try:
            before_vcs.send(sender=version)
            if version:
                log.info(
                    LOG_TEMPLATE.format(
                        project=project.slug,
                        version=version.slug,
                        msg='Checking out version {slug}: {identifier}'.format(
                            slug=version.slug,
                            identifier=version.identifier
                        )
                    )
                )
                version_slug = version.slug
                version_repo = project.vcs_repo(version_slug)

                ret_dict['checkout'] = version_repo.checkout(version.identifier)
            else:
                # Does this ever get called?
                log.info(LOG_TEMPLATE.format(
                    project=project.slug, version=version.slug, msg='Updating to latest revision'))
                version_slug = LATEST
                version_repo = project.vcs_repo(version_slug)
                ret_dict['checkout'] = version_repo.update()
        except Exception:
            raise
        finally:
            after_vcs.send(sender=version)

        # Update tags/version

        version_post_data = {'repo': version_repo.repo_url}

        if version_repo.supports_tags:
            version_post_data['tags'] = [
                {'identifier': v.identifier,
                 'verbose_name': v.verbose_name,
                 } for v in version_repo.tags
            ]

        if version_repo.supports_branches:
            version_post_data['branches'] = [
                {'identifier': v.identifier,
                 'verbose_name': v.verbose_name,
                 } for v in version_repo.branches
            ]

        try:
            api_v2.project(project.pk).sync_versions.post(version_post_data)
        except Exception as e:
            print "Sync Versions Exception: %s" % e.message
    return ret_dict


def setup_environment(version):
    """
    Build the virtualenv and install the project into it.

    Always build projects with a virtualenv.
    """
    ret_dict = {}
    project = version.project
    build_dir = os.path.join(project.venv_path(version=version.slug), 'build')
    if os.path.exists(build_dir):
        log.info(LOG_TEMPLATE.format(project=project.slug, version=version.slug, msg='Removing existing build dir'))
        shutil.rmtree(build_dir)
    if project.use_system_packages:
        site_packages = '--system-site-packages'
    else:
        site_packages = '--no-site-packages'

    # TODO: why is this virtualenv-2.7?
    # Here the command has been modified to support different
    # interpreters.
    ret_dict['venv'] = run(
        '{cmd} {site_packages} {path}'.format(
            cmd='virtualenv-2.7 -p {interpreter}'.format(
                interpreter=project.python_interpreter),
            site_packages=site_packages,
            path=project.venv_path(version=version.slug)
        )
    )
    # Other code expects sphinx-build to be installed inside the
    # virtualenv.  Using the -I option makes sure it gets installed
    # even if it is already installed system-wide (and
    # --system-site-packages is used)
    if project.use_system_packages:
        ignore_option = '-I'
    else:
        ignore_option = ''

    wheeldir = os.path.join(settings.SITE_ROOT, 'deploy', 'wheels')
    ret_dict['doc_builder'] = run(
        (
            '{cmd} install --use-wheel --find-links={wheeldir} -U {ignore_option} '
            'sphinx==1.2.2 virtualenv==1.10.1 setuptools==1.1 docutils==0.11 readthedocs-sphinx-ext==0.4.4 mkdocs==0.11.1 mock==1.0.1 pillow==2.6.1'
        ).format(
            cmd=project.venv_bin(version=version.slug, bin='pip'),
            ignore_option=ignore_option,
            wheeldir=wheeldir,
        )
    )

    # Handle requirements

    requirements_file_path = project.requirements_file
    checkout_path = project.checkout_path(version.slug)
    if not requirements_file_path:
        docs_dir = builder_loading.get(project.documentation_type)(version).docs_dir()
        for path in [docs_dir, '']:
            for req_file in ['pip_requirements.txt', 'requirements.txt']:
                test_path = os.path.join(checkout_path, path, req_file)
                print('Testing %s' % test_path)
                if os.path.exists(test_path):
                    requirements_file_path = test_path
                    break

    if requirements_file_path:
        os.chdir(checkout_path)
        ret_dict['requirements'] = run(
            '{cmd} install --exists-action=w -r {requirements}'.format(
                cmd=project.venv_bin(version=version.slug, bin='pip'),
                requirements=requirements_file_path))

    # Handle setup.py

    os.chdir(project.checkout_path(version.slug))
    if os.path.isfile("setup.py"):
        if getattr(settings, 'USE_PIP_INSTALL', False):
            ret_dict['install'] = run(
                '{cmd} install --ignore-installed .'.format(
                    cmd=project.venv_bin(version=version.slug, bin='pip')))
        else:
            ret_dict['install'] = run(
                '{cmd} setup.py install --force'.format(
                    cmd=project.venv_bin(version=version.slug,
                                         bin='python')))
    else:
        ret_dict['install'] = (999, "", "No setup.py, skipping install")
    return ret_dict


@task()
def build_docs(version, force, pdf, man, epub, dash, search, localmedia):
    """
    This handles the actual building of the documentation
    """

    project = version.project
    results = {}

    before_build.send(sender=version)

    with project.repo_nonblockinglock(version=version,
                                      max_lock_age=getattr(settings, 'REPO_LOCK_SECONDS', 30)):
        html_builder = builder_loading.get(project.documentation_type)(version)
        if force:
            html_builder.force()
        html_builder.append_conf()
        results['html'] = html_builder.build()
        if results['html'][0] == 0:
            html_builder.move()

        # Gracefully attempt to move files via task on web workers.
        try:
            move_files.delay(
                version_pk=version.pk,
                html=True,
                hostname=socket.gethostname(),
            )
        except socket.error:
            pass

        fake_results = (999, "Project Skipped, Didn't build",
                        "Project Skipped, Didn't build")
        if 'mkdocs' in project.documentation_type:
            if search:
                try:
                    search_builder = builder_loading.get('mkdocs_json')(version)
                    results['search'] = search_builder.build()
                    if results['search'][0] == 0:
                        search_builder.move()
                except:
                    log.error(LOG_TEMPLATE.format(
                        project=project.slug, version=version.slug, msg="JSON Build Error"), exc_info=True)

        if 'sphinx' in project.documentation_type:
            # Search builder. Creates JSON from docs and sends it to the
            # server.
            if search:
                try:
                    search_builder = builder_loading.get(
                        'sphinx_search')(version)
                    results['search'] = search_builder.build()
                    if results['search'][0] == 0:
                        # Copy json for safe keeping
                        search_builder.move()
                except:
                    log.error(LOG_TEMPLATE.format(
                        project=project.slug, version=version.slug, msg="JSON Build Error"), exc_info=True)
            # Local media builder for singlepage HTML download archive
            if localmedia:
                try:
                    localmedia_builder = builder_loading.get(
                        'sphinx_singlehtmllocalmedia')(version)
                    results['localmedia'] = localmedia_builder.build()
                    if results['localmedia'][0] == 0:
                        localmedia_builder.move()
                except:
                    log.error(LOG_TEMPLATE.format(
                        project=project.slug, version=version.slug, msg="Local Media HTML Build Error"), exc_info=True)

            # Optional build steps
            if version.project.slug not in HTML_ONLY and not project.skip:
                if pdf:
                    pdf_builder = builder_loading.get('sphinx_pdf')(version)
                    results['pdf'] = pdf_builder.build()
                    # Always move pdf results even when there's an error.
                    # if pdf_results[0] == 0:
                    pdf_builder.move()
                else:
                    results['pdf'] = fake_results
                if epub:
                    epub_builder = builder_loading.get('sphinx_epub')(version)
                    results['epub'] = epub_builder.build()
                    if results['epub'][0] == 0:
                        epub_builder.move()
                else:
                    results['epub'] = fake_results

    after_build.send(sender=version)

    return results


def create_build(build_pk):
    """
    Old placeholder for build creation. Now it just gets it from the database.
    """
    if build_pk:
        build = api.build(build_pk).get()
        for key in ['project', 'version', 'resource_uri', 'absolute_uri']:
            if key in build:
                del build[key]
    else:
        build = {}
    return build


def record_build(api, record, build, results, state):
    """
    Record a build by hitting the API.

    Returns nothing
    """

    if not record:
        return None

    setup_steps = ['checkout', 'venv', 'doc_builder', 'requirements', 'install']
    output_steps = ['html']
    all_steps = setup_steps + output_steps

    build['state'] = state
    if 'html' in results:
        build['success'] = results['html'][0] == 0
    else:
        build['success'] = False

    # Set global state
    # for step in all_steps:
    #     if results.get(step, False):
    #         if results.get(step)[0] != 0:
    #             results['success'] = False

    build['exit_code'] = max([results.get(step, [0])[0] for step in all_steps])

    build['setup'] = build['setup_error'] = ""
    build['output'] = build['error'] = ""

    for step in setup_steps:
        if step in results:
            build['setup'] += "\n\n%s\n-----\n\n" % step
            build['setup'] += results.get(step)[1]
            build['setup_error'] += "\n\n%s\n-----\n\n" % step
            build['setup_error'] += results.get(step)[2]

    for step in output_steps:
        if step in results:
            build['output'] += "\n\n%s\n-----\n\n" % step
            build['output'] += results.get(step)[1]
            build['error'] += "\n\n%s\n-----\n\n" % step
            build['error'] += results.get(step)[2]

    # Attempt to stop unicode errors on build reporting
    for key, val in build.items():
        if isinstance(val, basestring):
            build[key] = val.decode('utf-8', 'ignore')

    try:
        api.build(build['id']).put(build)
    except Exception:
        log.error("Unable to post a new build", exc_info=True)


def record_pdf(api, record, results, state, version):
    if not record or 'sphinx' not in version.project.documentation_type:
        return None
    try:
        if 'pdf' in results:
            pdf_exit = results['pdf'][0]
            pdf_success = pdf_exit == 0
            pdf_output = results['pdf'][1]
            pdf_error = results['pdf'][2]
        else:
            pdf_exit = 999
            pdf_success = False
            pdf_output = pdf_error = "PDF Failed"

        pdf_output = pdf_output.decode('utf-8', 'ignore')
        pdf_error = pdf_error.decode('utf-8', 'ignore')

        api.build.post(dict(
            state=state,
            project='/api/v1/project/%s/' % version.project.pk,
            version='/api/v1/version/%s/' % version.pk,
            success=pdf_success,
            type='pdf',
            output=pdf_output,
            error=pdf_error,
            exit_code=pdf_exit,
        ))
    except Exception:
        log.error(LOG_TEMPLATE.format(project=version.project.slug,
                                      version=version.slug, msg="Unable to post a new build"), exc_info=True)


###########
# Web tasks
@task(queue='web')
def finish_build(version_pk, build_pk, hostname=None, html=False,
                 localmedia=False, search=False, pdf=False, epub=False):
    """Build Finished, do house keeping bits"""
    version = Version.objects.get(pk=version_pk)
    build = Build.objects.get(pk=build_pk)

    if html:
        version.active = True
        version.built = True
        version.save()

    if not pdf:
        clear_pdf_artifacts(version)
    if not epub:
        clear_epub_artifacts(version)

    move_files(
        version_pk=version_pk,
        hostname=hostname,
        html=html,
        localmedia=localmedia,
        search=search,
        pdf=pdf,
        epub=epub,
    )

    # Symlink project on every web
    broadcast(type='app', task=symlink_project, args=[version.project.pk])

    # Delayed tasks
    update_static_metadata.delay(version.project.pk)
    fileify.delay(version.pk, commit=build.commit)
    update_search.delay(version.pk, commit=build.commit)


@task(queue='web')
def move_files(version_pk, hostname, html=False, localmedia=False, search=False,
               pdf=False, epub=False):
    """Task to move built documentation to web servers

    :param version_pk: Version id to sync files for
    :param hostname: Hostname to sync to
    :param html: Sync HTML
    :type html: bool
    :param localmedia: Sync local media files
    :type localmedia: bool
    :param search: Sync search files
    :type search: bool
    :param pdf: Sync PDF files
    :type pdf: bool
    :param epub: Sync ePub files
    :type epub: bool
    """
    version = Version.objects.get(pk=version_pk)

    if html:
        from_path = version.project.artifact_path(
            version=version.slug, type_=version.project.documentation_type)
        target = version.project.rtd_build_path(version.slug)
        Syncer.copy(from_path, target, host=hostname)

    if 'sphinx' in version.project.documentation_type:
        if localmedia:
            from_path = version.project.artifact_path(
                version=version.slug, type_='sphinx_localmedia')
            to_path = version.project.get_production_media_path(
                type_='htmlzip', version_slug=version.slug, include_file=False)
            Syncer.copy(from_path, to_path, host=hostname)

        if search:
            from_path = version.project.artifact_path(
                version=version.slug, type_='sphinx_search')
            to_path = version.project.get_production_media_path(
                type_='json', version_slug=version.slug, include_file=False)
            Syncer.copy(from_path, to_path, host=hostname)

        # Always move PDF's because the return code lies.
        if pdf:
            from_path = version.project.artifact_path(version=version.slug,
                                                      type_='sphinx_pdf')
            to_path = version.project.get_production_media_path(
                type_='pdf', version_slug=version.slug, include_file=False)
            Syncer.copy(from_path, to_path, host=hostname)
        if epub:
            from_path = version.project.artifact_path(version=version.slug,
                                                      type_='sphinx_epub')
            to_path = version.project.get_production_media_path(
                type_='epub', version_slug=version.slug, include_file=False)
            Syncer.copy(from_path, to_path, host=hostname)

    if 'mkdocs' in version.project.documentation_type:
        if search:
            from_path = version.project.artifact_path(version=version.slug,
                                                      type_='mkdocs_json')
            to_path = version.project.get_production_media_path(
                type_='json', version_slug=version.slug, include_file=False)
            Syncer.copy(from_path, to_path, host=hostname)


@task(queue='web')
def update_search(version_pk, commit, delete_non_commit_files=True):
    """Task to update search indexes

    :param version_pk: Version id to update
    :param commit: Commit that updated index
    :param delete_non_commit_files: Delete files not in commit from index
    """
    version = Version.objects.get(pk=version_pk)

    if version.project.is_type_sphinx:
        page_list = process_all_json_files(version, build_dir=False)
    elif version.project.is_type_mkdocs:
        page_list = process_mkdocs_json(version, build_dir=False)
    else:
        log.error('Unknown documentation type: %s',
                  version.project.documentation_type)
        return

    log_msg = ' '.join([page['path'] for page in page_list])
    log.info("(Search Index) Sending Data: %s [%s]", version.project.slug,
             log_msg)
    index_search_request(
        version=version,
        page_list=page_list,
        commit=commit,
        project_scale=0,
        page_scale=0,
        # Don't index sections to speed up indexing.
        # They aren't currently exposed anywhere.
        section=False,
        delete=delete_non_commit_files,
    )


@task(queue='web')
def symlink_project(project_pk):
    project = Project.objects.get(pk=project_pk)
    for symlink in [PublicSymlink, PrivateSymlink]:
        sym = symlink(project=project)
        sym.run()


@task(queue='web')
def symlink_domain(project_pk, domain_pk, delete=False):
    project = Project.objects.get(pk=project_pk)
    domain = Domain.objects.get(pk=domain_pk)
    for symlink in [PublicSymlink, PrivateSymlink]:
        sym = symlink(project=project)
        if delete:
            sym.remove_symlink_cname(domain)
        else:
            sym.symlink_cnames(domain)


@task(queue='web')
def symlink_subproject(project_pk):
    project = Project.objects.get(pk=project_pk)
    for symlink in [PublicSymlink, PrivateSymlink]:
        sym = symlink(project=project)
        sym.symlink_subprojects()


@task(queue='web')
def fileify(version_pk, commit):
    """
    Create ImportedFile objects for all of a version's files.

    This is a prereq for indexing the docs for search.
    It also causes celery-haystack to kick off an index of the file.
    """
    version = Version.objects.get(pk=version_pk)
    project = version.project

    if not project.cdn_enabled:
        return

    if not commit:
        log.info(LOG_TEMPLATE
                 .format(project=project.slug, version=version.slug,
                         msg=('Imported File not being built because no commit '
                              'information')))

    path = project.rtd_build_path(version.slug)
    if path:
        log.info(LOG_TEMPLATE
                 .format(project=version.project.slug, version=version.slug,
                         msg='Creating ImportedFiles'))
        _manage_imported_files(version, path, commit)
    else:
        log.info(LOG_TEMPLATE
                 .format(project=project.slug, version=version.slug,
                         msg='No ImportedFile files'))


def _manage_imported_files(version, path, commit):
    """Update imported files for version

    :param version: Version instance
    :param path: Path to search
    :param commit: Commit that updated path
    """
    changed_files = set()
    for root, __, filenames in os.walk(path):
        for filename in filenames:
            dirpath = os.path.join(root.replace(path, '').lstrip('/'),
                                   filename.lstrip('/'))
            full_path = os.path.join(root, filename)
            md5 = hashlib.md5(open(full_path, 'rb').read()).hexdigest()
            try:
                obj, __ = ImportedFile.objects.get_or_create(
                    project=version.project,
                    version=version,
                    path=dirpath,
                    name=filename,
                )
            except ImportedFile.MultipleObjectsReturned:
                log.exception('Error creating ImportedFile')
                continue
            if obj.md5 != md5:
                obj.md5 = md5
                changed_files.add(dirpath)
            if obj.commit != commit:
                obj.commit = commit
            obj.save()
    # Delete ImportedFiles from previous versions
    ImportedFile.objects.filter(project=version.project,
                                version=version
                                ).exclude(commit=commit).delete()
    # Purge Cache
    changed_files = [resolve_path(
        version.project, filename=file, version_slug=version.slug,
    ) for file in changed_files]
    cdn_ids = getattr(settings, 'CDN_IDS', None)
    if cdn_ids:
        if version.project.slug in cdn_ids:
            purge(cdn_ids[version.project.slug], changed_files)


@task(queue='web')
def send_notifications(version_pk, build_pk):
    version = Version.objects.get(pk=version_pk)
    build = Build.objects.get(pk=build_pk)

    for hook in version.project.webhook_notifications.all():
        webhook_notification(version, build, hook.url)
    for email in version.project.emailhook_notifications.all().values_list('email', flat=True):
        email_notification(version, build, email)


def email_notification(version, build, email):
    """Send email notifications for build failure

    :param version: :py:cls:`Version` instance that failed
    :param build: :py:cls:`Build` instance that failed
    :param email: Email recipient address
    """
    log.debug(LOG_TEMPLATE.format(project=version.project.slug, version=version.slug,
                                  msg='sending email to: %s' % email))
    context = {'version': version,
               'project': version.project,
               'build': build,
               'build_url': 'https://{0}{1}'.format(
                   getattr(settings, 'PRODUCTION_DOMAIN', 'readthedocs.org'),
                   build.get_absolute_url()),
               'unsub_url': 'https://{0}{1}'.format(
                   getattr(settings, 'PRODUCTION_DOMAIN', 'readthedocs.org'),
                   reverse('projects_notifications', args=[version.project.slug])),
               }

    if build.commit:
        title = _('Failed: {project.name} ({commit})').format(commit=build.commit[:8], **context)
    else:
        title = _('Failed: {project.name} ({version.verbose_name})').format(**context)

    send_email(
        email,
        title,
        template='projects/email/build_failed.txt',
        template_html='projects/email/build_failed.html',
        context=context
    )


def webhook_notification(version, build, hook_url):
    """Send webhook notification for project webhook

    :param version: Version instance to send hook for
    :param build: Build instance that failed
    :param hook_url: Hook URL to send to
    """
    project = version.project

    data = json.dumps({
        'name': project.name,
        'slug': project.slug,
        'build': {
            'id': build.id,
            'success': build.success,
            'date': build.date.strftime('%Y-%m-%d %H:%M:%S'),
        }
    })
    log.debug(LOG_TEMPLATE
              .format(project=project.slug, version='',
                      msg='sending notification to: %s' % hook_url))
    requests.post(hook_url, data=data)


@task(queue='web')
def update_static_metadata(project_pk, path=None):
    """Update static metadata JSON file

    Metadata settings include the following project settings:

    version
      The default version for the project, default: `latest`

    language
      The default language for the project, default: `en`

    languages
      List of languages built by linked translation projects.
    """
    project = Project.objects.get(pk=project_pk)
    if not path:
        path = project.static_metadata_path()

    log.info(LOG_TEMPLATE.format(
        project=project.slug,
        version='',
        msg='Updating static metadata',
    ))
    translations = [trans.language for trans in project.translations.all()]
    languages = set(translations)
    # Convert to JSON safe types
    metadata = {
        'version': project.default_version,
        'language': project.language,
        'languages': list(languages),
        'single_version': project.single_version,
    }
    try:
        fh = open(path, 'w+')
        json.dump(metadata, fh)
        fh.close()
        Syncer.copy(path, path, host=socket.gethostname(), file=True)
    except (AttributeError, IOError) as e:
        log.debug(LOG_TEMPLATE.format(
            project=project.slug,
            version='',
            msg='Cannot write to metadata.json: {0}'.format(e)
        ))


# Random Tasks
@task()
def remove_dir(path):
    """
    Remove a directory on the build/celery server.

    This is mainly a wrapper around shutil.rmtree so that app servers
    can kill things on the build server.
    """
    log.info("Removing %s", path)
    shutil.rmtree(path)


@task(queue='web')
def clear_artifacts(version_pk):
    """Remove artifacts from the web servers"""
    version = Version.objects.get(pk=version_pk)
    clear_pdf_artifacts(version)
    clear_epub_artifacts(version)
    clear_htmlzip_artifacts(version)
    clear_html_artifacts(version)


def clear_pdf_artifacts(version):
    run_on_app_servers('rm -rf %s'
                       % version.project.get_production_media_path(
                           type_='pdf', version_slug=version.slug))


def clear_epub_artifacts(version):
    run_on_app_servers('rm -rf %s'
                       % version.project.get_production_media_path(
                           type_='epub', version_slug=version.slug))


def clear_htmlzip_artifacts(version):
    run_on_app_servers('rm -rf %s'
                       % version.project.get_production_media_path(
                           type_='htmlzip', version_slug=version.slug))


def clear_html_artifacts(version):
    run_on_app_servers('rm -rf %s' % version.project.rtd_build_path(version=version.slug))


@task(queue='web')
def remove_path_from_web(path):
    """
    Remove the given path from the web servers file system.
    """
    # Santity check  for spaces in the path since spaces would result in
    # deleting unpredictable paths with "rm -rf".
    assert ' ' not in path, "No spaces allowed in path"

    # TODO: We need some proper escaping here for the given path.
    run_on_app_servers('rm -rf {path}'.format(path=path))
