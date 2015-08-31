# -*- coding: utf-8 -*-
import os
import json

from fabric.api import cd, env, require, run, task
from fabric.colors import green, white
from fabric.context_managers import contextmanager, shell_env, prefix
from fabric.utils import puts

from fabutils import arguments, join, options
from fabutils.env import set_env_from_json_file
from fabutils.context import cmd_msg
from fabutils.tasks import ulocal, urun, ursync_project
from fabutils.text import SUCCESS_ART
from fabutils.utils import boolean


project_conf = {
    "name": "luke",
    "tools": {
        "stylus": True,
        "bower": False,
    }
}


@contextmanager
def virtualenv():
    """
    Activates the virtualenv in which the commands shall be run.
    """
    require('site_dir', 'django_settings')

    with cd(env.site_dir):
        with shell_env(DJANGO_SETTINGS_MODULE=env.django_settings):
            yield


@task
def environment(env_name):
    """
    Creates a dynamic environment based on the contents of the given
    environments_file.
    """
    if env_name == 'vagrant':
        result = ulocal('vagrant ssh-config | grep IdentityFile', capture=True)
        env.key_filename = result.split()[1].replace('"', '')

    set_env_from_json_file('environments.json', env_name)


def get_property_from_env(env, property):
    with open('environments.json', 'r') as environments_data:
        json_envs_data = json.load(environments_data)

        return json_envs_data[env][property]


def checkstylus():
    if project_conf['tools']['stylus']:
        styluscompile()


@task
def styluscompile(watch=False):
    """
    Compiles custom.styl file to css.
    """
    watch_config = ''
    if watch:
        watch_config = '-w'

    with virtualenv(), prefix('nvm use stable'), shell_env(CI='true'):
        run('stylus -c {0} assets/css/custom.styl'.format(watch_config))


def bower_install():
    """
    Installs frontend dependencies with bower.
    """
    with virtualenv(), prefix('nvm use stable'), shell_env(CI='true'):
        run('bower install')


@task
def bower_install_package(package):
    """
    Installs frontend individual package dependencies with bower.
    """
    with virtualenv(), prefix('nvm use stable'), shell_env(CI='true'):
        run('bower install {0} --save'.format(package))


@task
def startapp(app_name):
    """
    Starts a new app
    """
    if app_name:
        with virtualenv():
            run('python manage.py startapp {0}'.format(app_name))


@task
def createsuperuser():
    """
    Starts a new app
    """
    with virtualenv():
        run('python manage.py createsuperuser')


@task
def runtests(app=""):
    """
    Runs django tests
    """
    with virtualenv():
        run("coverage run --source='.' manage.py test {0}".format(app))
        run("coverage html --omit=luke/settings/*,luke/wsgi.py")


@task
def createdb():
    """
    Creates a new database instance with utf-8 encoding for the project.
    """
    urun(
        'createdb {0} -l en_US.UTF-8 -E UTF8 -T template0'.format(
            project_conf['name']
        )
    )


@task
def dropdb():
    """
    Drops the project's database
    """
    urun('dropdb {0}'.format(project_conf['name']))


@task
def resetdb():
    """
    Reset the project's database by dropping an creating it again.
    """
    dropdb()
    createdb()
    migrate()


@task
def bootstrap():
    """
    Builds the environment to start the project.
    """
    # Build the DB schema and collect the static files.
    createdb()
    migrate()
    if project_conf['tools']['bower']:
        bower_install()
    collectstatic()


@task
def loaddata(*args):
    """
    Loads the given data fixtures into the project's database.
    """
    with virtualenv():
        run(join('python manage.py loaddata', arguments(*args)))


@task
def makemigrations(*args, **kwargs):
    """
    Creates the new migrations based on the project's models changes.
    """
    with virtualenv():
        run(join('python manage.py makemigrations',
                 options(**kwargs), arguments(*args)))


@task
def migrate(*args, **kwargs):
    """
    Syncs the DB and applies the available migrations.
    """
    with virtualenv():
        run(join('python manage.py migrate',
                 options(**kwargs), arguments(*args)))


@task
def collectstatic():
    """
    Collects the static files.
    """
    with virtualenv():
        run('python manage.py collectstatic --noinput')


@task
def install_requirements(upgrade=False):
    """
    Installs the python dependencies specified in the given requirements file.
    """
    require('env_name', 'site_dir')

    requirements = env.env_name if env.env_name != 'vagrant' else 'devel'
    path = os.path.realpath(os.path.join(
        env.site_dir, 'requirements', '{0}.txt'.format(requirements)))

    with virtualenv():
        run('pip install -{0}r {1}'.format('U' if upgrade else '', path))


@task
def runserver():
    """
    Starts the development server inside the Vagrant VM.
    """

    # Checks if stylus compilation is needed
    checkstylus()

    with virtualenv():
        run('python manage.py runserver_plus 0.0.0.0:8000')


@task
def deploy(git_ref, upgrade=False):
    """
    Deploy the code of the given git reference to the previously selected
    environment.
    Pass ``upgrade=True`` to upgrade the versions of the already installed
    project requirements (with pip).
    """
    require('hosts', 'user', 'group', 'site_dir', 'django_settings')

    # Retrives git reference metadata and creates a temp directory with the
    # contents resulting of applying a ``git archive`` command.
    message = white('Creating git archive from {0}'.format(git_ref), bold=True)
    with cmd_msg(message):
        repo = ulocal(
            'basename `git rev-parse --show-toplevel`', capture=True)
        commit = ulocal(
            'git rev-parse --short {0}'.format(git_ref), capture=True)
        branch = ulocal(
            'git rev-parse --abbrev-ref HEAD', capture=True)

        tmp_dir = '/tmp/blob-{0}-{1}/'.format(repo, commit)

        ulocal('rm -fr {0}'.format(tmp_dir))
        ulocal('mkdir {0}'.format(tmp_dir))
        ulocal('git archive {0} ./src | tar -xC {1} --strip 1'.format(
            commit, tmp_dir))

    # Puts the site into maintenance mode.
    with cmd_msg(white('Enabling maintenance mode', bold=True)):
        maintenance('on')

    # Uploads the code of the temp directory to the host with rsync telling
    # that it must delete old files in the server, upload deltas by checking
    # file checksums recursivelly in a zipped way; changing the file
    # permissions to allow read, write and execution to the owner, read and
    # execution to the group and no permissions for any other user.
    with cmd_msg(white('Uploading code to server', bold=True)):
        ursync_project(
            local_dir=tmp_dir,
            remote_dir=env.site_dir,
            delete=True,
            default_opts='-chrtvzP',
            extra_opts='--chmod=750',
            exclude=[
                "*.pyc", "env/",
                "cover/", "*.style",
                "bower_components",
            ]
        )

    # Performs the deployment task, i.e. Install/upgrade project
    # requirements, syncronize and migrate the database changes, collect
    # static files, reload the webserver, etc.
    message = white('Running deployment tasks', bold=True)
    with cmd_msg(message, grouped=True):
        with virtualenv():

            message = 'Installing Python requirements with pip'
            with cmd_msg(message, spaces=2):
                install_requirements(upgrade=upgrade)

            message = 'Migrating database'
            with cmd_msg(message, spaces=2):
                migrate(noinput=True)

            message = 'Installing bower components'
            with cmd_msg(message, spaces=2):
                bower_install()

            message = 'Collecting static files'
            with cmd_msg(message, spaces=2):
                collectstatic()

            message = 'Setting file permissions'
            with cmd_msg(message, spaces=2):
                run('chgrp -R {0} .'.format(env.group))
                run('chgrp -R {0} ../media'.format(env.group))

            message = 'Restarting webserver'
            with cmd_msg(message, spaces=2):
                run('touch ../reload')

            message = 'Restarting celery workers'
            with cmd_msg(message, spaces=2):
                run('sudo /usr/bin/supervisorctl restart {0}-celeryd'.format(
                    env.user))

            message = 'Registering deployment'
            with cmd_msg(message, spaces=2):
                register_deployment(commit, branch)

    # Disable maintenance mode.
    with cmd_msg(white('Disabling maintenance mode', bold=True)):
        maintenance('off')

    # Clean the temporary snapshot files that was just deployed to the host
    message = white('Cleaning up...', bold=True)
    with cmd_msg(message):
        ulocal('rm -fr {0}'.format(tmp_dir))

    puts(green(SUCCESS_ART), show_prefix=False)
    puts(white('Code from {0} was succesfully deployed to host {1}'.format(
        git_ref, ', '.join(env.hosts)), bold=True), show_prefix=False)


@task
def register_deployment(commit, branch):
    """
    Register the current deployment at Opbeat with given commit and branch.
    """
    with virtualenv():
        run(
            'opbeat -o $OPBEAT_ORGANIZATION_ID '
            '-a $OPBEAT_APP_ID '
            '-t $OPBEAT_SECRET_TOKEN deployment '
            '--component path:. vcs:git rev:%s branch:%s '
            % (commit, branch)
        )


@task
def maintenance(state):
    """
    Sets maintenance mode 'on' or 'off' on the server.
    """
    require('maintenance_dir')

    if boolean(state):
        ursync_project(
            local_dir='./maintenance/',
            remote_dir=env.maintenance_dir,
            delete=True,
            default_opts='-chrtvzP',
            extra_opts='--chmod=750',
        )

        with cd(env.maintenance_dir):
            run('chgrp -R {0} .'.format(env.group))

    else:
        with cd(env.maintenance_dir):
            run('rm -rf ./*')
