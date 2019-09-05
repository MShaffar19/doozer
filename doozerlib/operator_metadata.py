import glob
import re
import shutil
import threading
import yaml

from functools import wraps
from dockerfile_parse import DockerfileParser
from doozerlib import brew, exectools, logutil, pushd, util

logger = logutil.getLogger(__name__)


def log(func):
    """Logging decorator, log the call and return value of a decorated function

    :param function func: Function to be decorated
    :return: Return wrapper function
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        logger.info('running: {}, with args {} {}'.format(func.__name__, args, kwargs))
        return_val = func(*args, **kwargs)
        logger.info('{} returned {}'.format(func.__name__, return_val))
        return return_val
    return wrapper


def unpack(func):
    """Unpacking decorator, unpacks a tuple into arguments for a function call
    Needed because Python 2.7 doesn't have "starmap" for Pool / ThreadPool

    :param function func: Function to be decorated
    :return: Return wrapper function
    """
    @wraps(func)
    def wrapper(arg_tuple):
        return func(*arg_tuple)
    return wrapper


@unpack
def update_and_build(nvr, stream, runtime, merge_branch='rhaos-4-rhel-7', force_build=False):
    """Module entrypoint, orchestrate update and build steps of metadata repos

    :param string nvr: Operator name-version-release
    :param string stream: Which metadata repo should be updated (dev, stage, prod)
    :param Runtime runtime: a runtime instance
    :param string merge_branch: Which branch should be updated in the metadata repo
    :return bool True if operations succeeded, False if something went wrong
    """
    op_md = OperatorMetadataBuilder(nvr, stream, runtime=runtime)

    if not op_md.update_metadata_repo(merge_branch) and not force_build:
        util.green_print('No changes in metadata repo, skipping build')
        return True

    if not op_md.build_metadata_container():
        util.red_print('Build of {} failed, see debug.log'.format(op_md.metadata_repo))
        return False

    return True


class OperatorMetadataBuilder:
    def __init__(self, nvr, stream, runtime, **kwargs):
        self.nvr = nvr
        self.stream = stream
        self.runtime = runtime
        self._cached_attrs = kwargs

    @log
    def update_metadata_repo(self, metadata_branch='dev'):
        """Update the corresponding metadata repository of an operator

        :param string metadata_branch: Which branch of the metadata repository should be updated
        :return: bool True if metadata repo was updated, False if there was nothing to update
        """
        exectools.cmd_assert('mkdir -p {}'.format(self.working_dir))

        self.clone_repo(self.operator_name, self.operator_branch)
        self.clone_repo(self.metadata_repo, metadata_branch)
        self.checkout_repo(self.operator_name, self.commit_hash)

        self.update_metadata_manifests_dir()
        self.merge_streams_on_top_level_package_yaml()
        self.create_metadata_dockerfile()
        return self.commit_and_push_metadata_repo()

    @log
    def build_metadata_container(self):
        """Build the metadata container using rhpkg

        :return: bool True if build succeeded, False otherwise
        :raise: Exception if command failed (rc != 0)
        """
        with pushd.Dir('{}/{}'.format(self.working_dir, self.metadata_repo)):
            cmd = 'timeout 600 rhpkg {}container-build --nowait --target {}'.format(
                ('--user {} '.format(self.rhpkg_user) if self.rhpkg_user else ''),
                self.target
            )
            rc, stdout, stderr = exectools.cmd_gather(cmd)

            if rc != 0:
                raise Exception('{} failed! rc={} stdout={} stderr={}'.format(
                    cmd, rc, stdout, stderr
                ))

            return self.watch_brew_task(self.extract_brew_task_id(stdout)) is None

    @log
    def clone_repo(self, repo, branch):
        """Clone a repository using rhpkg

        :param string repo: Name of the repository to be cloned
        :param string branch: Which branch of the repository should be cloned
        """
        def delete_and_clone():
            self.delete_repo(repo)

            cmd = 'timeout 600 rhpkg '
            cmd += '--user {} '.format(self.rhpkg_user) if self.rhpkg_user else ''
            cmd += 'clone containers/{} --branch {}'.format(repo, branch)
            return exectools.cmd_assert(cmd)

        with pushd.Dir(self.working_dir):
            exectools.retry(retries=3, task_f=delete_and_clone)

    @log
    def delete_repo(self, repo):
        """Delete repository from working_dir. Ignore errors if repo is already absent
        """
        try:
            shutil.rmtree('{}/{}'.format(self.working_dir, repo))
        except OSError:
            pass

    @log
    def checkout_repo(self, repo, commit_hash):
        """Checkout a repository to a particular commit hash

        :param string repo: The repository in which the checkout operation will be performed
        :param string commit_hash: The desired point to checkout the repository
        """
        with pushd.Dir('{}/{}'.format(self.working_dir, repo)):
            exectools.cmd_assert('git checkout {}'.format(commit_hash))

    @log
    def update_metadata_manifests_dir(self):
        """Update channel-specific manifests in the metadata repository with the latest
        manifests found in the operator repository

        If the metadata repository is empty, bring the top-level package YAML file also
        """
        self.remove_metadata_channel_dir()
        self.ensure_metadata_manifests_dir_exists()
        self.copy_channel_manifests_from_operator_to_metadata()

        if not self.metadata_package_yaml_exists():
            self.copy_operator_package_yaml_to_metadata()

    @log
    def merge_streams_on_top_level_package_yaml(self):
        """Update (or create) a channel entry on the top-level package YAML file,
        pointing to the current CSV
        """
        package_yaml = yaml.safe_load(open(self.metadata_package_yaml_filename))

        def get_default_channel(package_yaml):
            channel_names = [str(channel['name']) for channel in package_yaml['channels']]
            return '4.2' if '4.2' in channel_names else self.channel_name

        package_yaml['defaultChannel'] = get_default_channel(package_yaml)

        def find_channel_index(package_yaml):
            for index, channel in enumerate(package_yaml['channels']):
                if str(channel['name']) == str(self.channel_name):
                    return index
            return None

        index = find_channel_index(package_yaml)

        if index is not None:
            package_yaml['channels'][index]['currentCSV'] = str(self.csv)
        else:
            package_yaml['channels'].append({
                'name': str(self.channel_name),
                'currentCSV': str(self.csv)
            })

        with open(self.metadata_package_yaml_filename, 'w') as file:
            file.write(yaml.dump(package_yaml))

    @log
    def create_metadata_dockerfile(self):
        """Create a minimal Dockerfile on the metadata repository, copying all manifests
        inside the image and having nearly the same labels as its corresponding operator Dockerfile

        But some modifications on the labels are neeeded:

        - 'com.redhat.component' label should contain the metadata component name,
           otherwise it conflicts with the operator.


        - 'com.redhat.delivery.appregistry' should always be "true", regardless of
          the value coming from the operator Dockerfile

        - 'release' label should be removed, because we can't build the same NVR
        multiple times
        """
        operator_dockerfile = DockerfileParser('{}/{}/Dockerfile'.format(self.working_dir, self.operator_name))
        metadata_dockerfile = DockerfileParser('{}/{}/Dockerfile'.format(self.working_dir, self.metadata_repo))
        metadata_dockerfile.content = 'FROM scratch\nCOPY ./manifests /manifests'
        metadata_dockerfile.labels = operator_dockerfile.labels
        metadata_dockerfile.labels['com.redhat.component'] = (
            operator_dockerfile.labels['com.redhat.component']
            .replace(self.operator_name, self.metadata_name)
        )
        metadata_dockerfile.labels['com.redhat.delivery.appregistry'] = 'true'
        metadata_dockerfile.labels['name'] = 'openshift/ose-{}'.format(self.metadata_name)
        try:
            del(metadata_dockerfile.labels['release'])
        except KeyError:
            pass

    @log
    def commit_and_push_metadata_repo(self):
        """Commit and push changes made on the metadata repository, using rhpkg
        """
        with pushd.Dir('{}/{}'.format(self.working_dir, self.metadata_repo)):
            try:
                exectools.cmd_assert('git add .')
                user_option = '--user {} '.format(self.rhpkg_user) if self.rhpkg_user else ''
                exectools.cmd_assert('rhpkg {}commit -m "Update operator metadata"'.format(user_option))
                exectools.retry(retries=3, task_f=lambda: exectools.cmd_assert('timeout 600 rhpkg {}push'.format(user_option)))
                return True
            except Exception:
                # The metadata repo might be already up to date, so we don't have anything new to commit
                return False

    @log
    def remove_metadata_channel_dir(self):
        exectools.cmd_assert('rm -rf {}/{}/{}/{}'.format(
            self.working_dir,
            self.metadata_repo,
            self.metadata_manifests_dir,
            self.channel
        ))

    @log
    def ensure_metadata_manifests_dir_exists(self):
        exectools.cmd_assert('mkdir -p {}/{}/{}'.format(
            self.working_dir,
            self.metadata_repo,
            self.metadata_manifests_dir
        ))

    @log
    def copy_channel_manifests_from_operator_to_metadata(self):
        exectools.cmd_assert('cp -r {}/{}/{}/{} {}/{}/{}'.format(
            self.working_dir,
            self.operator_name,
            self.operator_manifests_dir,
            self.channel,
            self.working_dir,
            self.metadata_repo,
            self.metadata_manifests_dir
        ))

    @log
    def copy_operator_package_yaml_to_metadata(self):
        exectools.cmd_assert('cp {} {}/{}/{}'.format(
            self.operator_package_yaml_filename,
            self.working_dir,
            self.metadata_repo,
            self.metadata_manifests_dir
        ))

    @log
    def metadata_package_yaml_exists(self):
        return len(glob.glob('{}/{}/{}/*package.yaml'.format(
            self.working_dir,
            self.metadata_repo,
            self.metadata_manifests_dir
        ))) > 0

    @log
    def extract_brew_task_id(self, container_build_output):
        """Extract the Task ID from the output of a `rhpkg container-build` command

        :param string container_build_output: stdout from `rhpkg container-build`
        :return: string of captured task ID
        :raise: AttributeError if task ID can't be found in provided output
        """
        return re.search(r'Created task:\ (\d+)', container_build_output).group(1)

    @log
    def watch_brew_task(self, task_id):
        """Keep watching progress of brew task

        :param string task_id: The Task ID to be watched
        :return: string with an error if an error happens, None otherwise
        """
        return brew.watch_task(
            self.runtime.group_config.urls.brewhub, logger.info, task_id, threading.Event()
        )

    @property
    def working_dir(self):
        return self._cache_attr('working_dir')

    @property
    def rhpkg_user(self):
        return self._cache_attr('rhpkg_user')

    @property
    def operator_branch(self):
        return self._cache_attr('operator_branch')

    @property
    def target(self):
        return '{}-candidate'.format(self.operator_branch)

    @property
    def operator_name(self):
        return self._cache_attr('operator_name')

    @property
    def commit_hash(self):
        return self._cache_attr('commit_hash')

    @property
    def operator(self):
        return self._cache_attr('operator')

    @property
    def metadata_name(self):
        return '{}-metadata'.format(self.operator_name)

    @property
    def metadata_repo(self):
        return self.operator_name.replace(
            '-operator', '-{}-operator-metadata'.format(self.stream)
        )

    @property
    def channel(self):
        return re.search(r'^v?(\d+\.\d+)\.*', self.nvr.split('-')[-2]).group(1)

    @property
    def brew_buildinfo(self):
        return self._cache_attr('brew_buildinfo')

    @property
    def operator_manifests_dir(self):
        return self.operator.config['update-csv']['manifests-dir'].rstrip('/')

    @property
    def metadata_manifests_dir(self):
        return 'manifests'

    @property
    def operator_package_yaml_filename(self):
        return glob.glob('{}/{}/{}/*package.yaml'.format(
            self.working_dir,
            self.operator_name,
            self.operator_manifests_dir
        ))[0]

    @property
    def metadata_package_yaml_filename(self):
        return glob.glob('{}/{}/{}/*package.yaml'.format(
            self.working_dir,
            self.metadata_repo,
            self.metadata_manifests_dir
        ))[0]

    @property
    def metadata_csv_yaml_filename(self):
        return glob.glob('{}/{}/{}/{}/*.clusterserviceversion.yaml'.format(
            self.working_dir,
            self.metadata_repo,
            self.metadata_manifests_dir,
            self.channel
        ))[0]

    @property
    def csv(self):
        return self._cache_attr('csv')

    @property
    def channel_name(self):
        """Use a custom name for a channel on package YAML if specified,
        fallback to default channel (4.1, 4.2, etc) otherwise

        This is valid only for 4.1, custom names should be ignored on 4.2
        """
        if str(self.channel) == '4.1' and 'channel' in self.operator.config['update-csv']:
            return self.operator.config['update-csv']['channel']
        return self.channel

    def get_working_dir(self):
        return '{}/{}/{}'.format(self.runtime.working_dir, 'distgits', 'containers')

    def get_rhpkg_user(self):
        return self.runtime.user if hasattr(self.runtime, 'user') else ''

    def get_operator_branch(self):
        return self.runtime.group_config.branch

    def get_operator_name(self):
        _rc, stdout, _stderr = self.brew_buildinfo
        return re.search('Source:([^#]+)', stdout).group(1).split('/')[-1]

    def get_commit_hash(self):
        _rc, stdout, _stderr = self.brew_buildinfo
        return re.search('Source:[^#]+#(.+)', stdout).group(1)

    def get_operator(self):
        return self.runtime.image_map[self.operator_name]

    @log
    def get_brew_buildinfo(self):
        """Output of this command is used to extract the operator name and its commit hash
        """
        cmd = 'brew buildinfo {}'.format(self.nvr)
        return exectools.retry(retries=3, task_f=lambda *_: exectools.cmd_gather(cmd))

    def get_csv(self):
        return yaml.safe_load(open(self.metadata_csv_yaml_filename))['metadata']['name']

    def _cache_attr(self, attr):
        """Some attribute values are time-consuming to retrieve, as they might
        come from running an external command, etc. So, after obtaining the value
        it gets saved in "_cached_attrs" for future uses

        Also makes automated testing easier, as values can be simply injected
        at "_cached_attrs", without the need of mocking the sources from which
        the values come
        """
        if attr not in self._cached_attrs:
            self._cached_attrs[attr] = getattr(self, 'get_{}'.format(attr))()
        return self._cached_attrs[attr]


class OperatorMetadataLatestBuildReporter:
    def __init__(self, operator_name, runtime):
        self.operator_name = operator_name
        self.runtime = runtime

    @log
    def get_latest_build(self):
        cmd = 'brew latest-build {} {} --quiet'.format(self.target, self.metadata_component_name)
        _rc, stdout, _stderr = exectools.retry(retries=3, task_f=lambda *_: exectools.cmd_gather(cmd))
        return stdout.split(' ')[0]

    @property
    def target(self):
        return '{}-candidate'.format(self.operator_branch)

    @property
    def operator_branch(self):
        return self.runtime.group_config.branch

    @property
    def metadata_component_name(self):
        return self.operator_component_name.replace('-container', '-metadata-container')

    @property
    def operator_component_name(self):
        if 'distgit' in self.operator.config and 'component' in self.operator.config['distgit']:
            return self.operator.config['distgit']['component']

        return '{}-container'.format(self.operator_name)

    @property
    def operator(self):
        return self.runtime.image_map[self.operator_name]
