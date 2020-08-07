"""
    tests.pytests.conftest
    ~~~~~~~~~~~~~~~~~~~~~~
"""
import logging
import os
import pathlib
import shutil
import stat
import textwrap

import pytest
import salt.utils.files
import salt.utils.platform
from salt.serializers import yaml
from tests.conftest import _get_virtualenv_binary_path
from tests.support.runtests import RUNTIME_VARS
from tests.support.unit import TestCase

PYTESTS_SUITE_PATH = pathlib.Path(__file__).parent

log = logging.getLogger(__name__)


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_collection_modifyitems(config, items):
    """
    called after collection has been performed, may filter or re-order
    the items in-place.

    :param _pytest.main.Session session: the pytest session object
    :param _pytest.config.Config config: pytest config object
    :param List[_pytest.nodes.Item] items: list of item objects
    """
    # Let PyTest or other plugins handle the initial collection
    yield

    # Check each collected item that's under this package to ensure that none is using TestCase as the base class
    for item in items:
        if not str(item.fspath).startswith(str(PYTESTS_SUITE_PATH)):
            continue
        if not item.cls:
            # The test item is not part of a class
            continue

        if issubclass(item.cls, TestCase):
            raise RuntimeError(
                "The tests under {} MUST NOT use unittest's TestCase class or a subclass of it.".format(
                    pathlib.Path(str(item.fspath)).relative_to(RUNTIME_VARS.CODE_DIR)
                )
            )


@pytest.fixture(scope="package")
def salt_syndic_master_factory(request, salt_factories, salt_ssh_sshd_port):
    root_dir = salt_factories._get_root_dir_for_daemon("syndic_master")
    conf_dir = root_dir / "conf"
    conf_dir.mkdir(exist_ok=True)

    with salt.utils.files.fopen(
        os.path.join(RUNTIME_VARS.CONF_DIR, "syndic_master")
    ) as rfh:
        config_defaults = yaml.deserialize(rfh.read())

        tests_known_hosts_file = str(root_dir / "salt_ssh_known_hosts")
        with salt.utils.files.fopen(tests_known_hosts_file, "w") as known_hosts:
            known_hosts.write("")

    config_defaults["root_dir"] = str(root_dir)
    config_defaults["known_hosts_file"] = tests_known_hosts_file
    config_defaults["syndic_master"] = "localhost"
    config_defaults["transport"] = request.config.getoption("--transport")

    config_overrides = {}
    ext_pillar = []
    if salt.utils.platform.is_windows():
        ext_pillar.append(
            {"cmd_yaml": "type {}".format(os.path.join(RUNTIME_VARS.FILES, "ext.yaml"))}
        )
    else:
        ext_pillar.append(
            {"cmd_yaml": "cat {}".format(os.path.join(RUNTIME_VARS.FILES, "ext.yaml"))}
        )

    # We need to copy the extension modules into the new master root_dir or
    # it will be prefixed by it
    extension_modules_path = str(root_dir / "extension_modules")
    if not os.path.exists(extension_modules_path):
        shutil.copytree(
            os.path.join(RUNTIME_VARS.FILES, "extension_modules"),
            extension_modules_path,
        )

    # Copy the autosign_file to the new  master root_dir
    autosign_file_path = str(root_dir / "autosign_file")
    shutil.copyfile(
        os.path.join(RUNTIME_VARS.FILES, "autosign_file"), autosign_file_path
    )
    # all read, only owner write
    autosign_file_permissions = (
        stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH | stat.S_IWUSR
    )
    os.chmod(autosign_file_path, autosign_file_permissions)

    config_overrides.update(
        {
            "ext_pillar": ext_pillar,
            "extension_modules": extension_modules_path,
            "file_roots": {
                "base": [
                    RUNTIME_VARS.TMP_STATE_TREE,
                    os.path.join(RUNTIME_VARS.FILES, "file", "base"),
                ],
                # Alternate root to test __env__ choices
                "prod": [
                    RUNTIME_VARS.TMP_PRODENV_STATE_TREE,
                    os.path.join(RUNTIME_VARS.FILES, "file", "prod"),
                ],
            },
            "pillar_roots": {
                "base": [
                    RUNTIME_VARS.TMP_PILLAR_TREE,
                    os.path.join(RUNTIME_VARS.FILES, "pillar", "base"),
                ],
                "prod": [RUNTIME_VARS.TMP_PRODENV_PILLAR_TREE],
            },
        }
    )

    # We also need a salt-ssh roster config file
    roster_path = str(conf_dir / "roster")
    roster_contents = textwrap.dedent(
        """\
        localhost:
          host: 127.0.0.1
          port: {}
          user: {}
          mine_functions:
            test.arg: ['itworked']
        """.format(
            salt_ssh_sshd_port, RUNTIME_VARS.RUNNING_TESTS_USER
        )
    )
    log.debug(
        "Writing to configuration file %s. Configuration:\n%s",
        roster_path,
        roster_contents,
    )
    with salt.utils.files.fopen(roster_path, "w") as wfh:
        wfh.write(roster_contents)

    factory = salt_factories.get_salt_master_daemon(
        "syndic_master",
        order_masters=True,
        config_defaults=config_defaults,
        config_overrides=config_overrides,
    )
    return factory


@pytest.fixture(scope="package")
def salt_syndic_factory(request, salt_factories, salt_syndic_master_factory):
    config_defaults = {"master": None, "minion": None, "syndic": None}
    with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, "syndic")) as rfh:
        opts = yaml.deserialize(rfh.read())

        opts["hosts.file"] = os.path.join(RUNTIME_VARS.TMP, "hosts")
        opts["aliases.file"] = os.path.join(RUNTIME_VARS.TMP, "aliases")
        opts["transport"] = request.config.getoption("--transport")
        config_defaults["syndic"] = opts
    factory = salt_syndic_master_factory.get_salt_syndic_daemon(
        "syndic", config_defaults=config_defaults
    )
    return factory


@pytest.fixture(scope="package")
def salt_master_id():
    return "master-pytest"


@pytest.fixture(scope="package")
def salt_master_factory(
    request,
    salt_factories,
    salt_syndic_master_factory,
    salt_ssh_sshd_port,
    salt_master_id,
):
    root_dir = salt_factories._get_root_dir_for_daemon("master")
    conf_dir = root_dir / "conf"
    conf_dir.mkdir(exist_ok=True)

    with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, "master")) as rfh:
        config_defaults = yaml.deserialize(rfh.read())

        tests_known_hosts_file = str(root_dir / "salt_ssh_known_hosts")
        with salt.utils.files.fopen(tests_known_hosts_file, "w") as known_hosts:
            known_hosts.write("")

    config_defaults["id"] = salt_master_id
    config_defaults["root_dir"] = str(root_dir)
    config_defaults["known_hosts_file"] = tests_known_hosts_file
    config_defaults["syndic_master"] = "localhost"
    config_defaults["transport"] = request.config.getoption("--transport")
    config_defaults["reactor"] = [
        {"salt/test/reactor": [os.path.join(RUNTIME_VARS.FILES, "reactor-test.sls")]}
    ]

    config_overrides = {}
    ext_pillar = []
    if salt.utils.platform.is_windows():
        ext_pillar.append(
            {"cmd_yaml": "type {}".format(os.path.join(RUNTIME_VARS.FILES, "ext.yaml"))}
        )
    else:
        ext_pillar.append(
            {"cmd_yaml": "cat {}".format(os.path.join(RUNTIME_VARS.FILES, "ext.yaml"))}
        )
    ext_pillar.append(
        {
            "file_tree": {
                "root_dir": os.path.join(RUNTIME_VARS.PILLAR_DIR, "base", "file_tree"),
                "follow_dir_links": False,
                "keep_newline": True,
            }
        }
    )
    config_overrides["pillar_opts"] = True

    # We need to copy the extension modules into the new master root_dir or
    # it will be prefixed by it
    extension_modules_path = str(root_dir / "extension_modules")
    if not os.path.exists(extension_modules_path):
        shutil.copytree(
            os.path.join(RUNTIME_VARS.FILES, "extension_modules"),
            extension_modules_path,
        )

    # Copy the autosign_file to the new  master root_dir
    autosign_file_path = str(root_dir / "autosign_file")
    shutil.copyfile(
        os.path.join(RUNTIME_VARS.FILES, "autosign_file"), autosign_file_path
    )
    # all read, only owner write
    autosign_file_permissions = (
        stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH | stat.S_IWUSR
    )
    os.chmod(autosign_file_path, autosign_file_permissions)

    config_overrides.update(
        {
            "ext_pillar": ext_pillar,
            "extension_modules": extension_modules_path,
            "file_roots": {
                "base": [
                    RUNTIME_VARS.TMP_STATE_TREE,
                    os.path.join(RUNTIME_VARS.FILES, "file", "base"),
                ],
                # Alternate root to test __env__ choices
                "prod": [
                    RUNTIME_VARS.TMP_PRODENV_STATE_TREE,
                    os.path.join(RUNTIME_VARS.FILES, "file", "prod"),
                ],
            },
            "pillar_roots": {
                "base": [
                    RUNTIME_VARS.TMP_PILLAR_TREE,
                    os.path.join(RUNTIME_VARS.FILES, "pillar", "base"),
                ],
                "prod": [RUNTIME_VARS.TMP_PRODENV_PILLAR_TREE],
            },
        }
    )

    # Let's copy over the test cloud config files and directories into the running master config directory
    for entry in os.listdir(RUNTIME_VARS.CONF_DIR):
        if not entry.startswith("cloud"):
            continue
        source = os.path.join(RUNTIME_VARS.CONF_DIR, entry)
        dest = str(conf_dir / entry)
        if os.path.isdir(source):
            shutil.copytree(source, dest)
        else:
            shutil.copyfile(source, dest)

    # We also need a salt-ssh roster config file
    roster_path = str(conf_dir / "roster")
    roster_contents = textwrap.dedent(
        """\
        localhost:
          host: 127.0.0.1
          port: {}
          user: {}
          mine_functions:
            test.arg: ['itworked']
        """.format(
            salt_ssh_sshd_port, RUNTIME_VARS.RUNNING_TESTS_USER
        )
    )
    log.debug(
        "Writing to configuration file %s. Configuration:\n%s",
        roster_path,
        roster_contents,
    )
    with salt.utils.files.fopen(roster_path, "w") as wfh:
        wfh.write(roster_contents)

    factory = salt_syndic_master_factory.get_salt_master_daemon(
        salt_master_id,
        config_defaults=config_defaults,
        config_overrides=config_overrides,
    )
    return factory


@pytest.fixture(scope="package")
def salt_minion_id():
    return "minion-pytest"


@pytest.fixture(scope="package")
def salt_minion_factory(request, salt_master_factory, salt_minion_id):
    with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, "minion")) as rfh:
        config_defaults = yaml.deserialize(rfh.read())
    config_defaults["id"] = salt_minion_id
    config_defaults["hosts.file"] = os.path.join(RUNTIME_VARS.TMP, "hosts")
    config_defaults["aliases.file"] = os.path.join(RUNTIME_VARS.TMP, "aliases")
    config_defaults["transport"] = request.config.getoption("--transport")

    config_overrides = {
        "file_roots": {
            "base": [
                RUNTIME_VARS.TMP_STATE_TREE,
                os.path.join(RUNTIME_VARS.FILES, "file", "base"),
            ],
            # Alternate root to test __env__ choices
            "prod": [
                RUNTIME_VARS.TMP_PRODENV_STATE_TREE,
                os.path.join(RUNTIME_VARS.FILES, "file", "prod"),
            ],
        },
        "pillar_roots": {
            "base": [
                RUNTIME_VARS.TMP_PILLAR_TREE,
                os.path.join(RUNTIME_VARS.FILES, "pillar", "base"),
            ],
            "prod": [RUNTIME_VARS.TMP_PRODENV_PILLAR_TREE],
        },
    }
    virtualenv_binary = _get_virtualenv_binary_path()
    if virtualenv_binary:
        config_overrides["venv_bin"] = virtualenv_binary
    factory = salt_master_factory.get_salt_minion_daemon(
        salt_minion_id,
        config_defaults=config_defaults,
        config_overrides=config_overrides,
    )
    return factory


@pytest.fixture(scope="package")
def salt_sub_minion_id():
    return "sub-minion-pytest"


@pytest.fixture(scope="package")
def salt_sub_minion_factory(request, salt_master_factory, salt_sub_minion_id):
    with salt.utils.files.fopen(
        os.path.join(RUNTIME_VARS.CONF_DIR, "sub_minion")
    ) as rfh:
        config_defaults = yaml.deserialize(rfh.read())
    config_defaults["id"] = salt_sub_minion_id
    config_defaults["hosts.file"] = os.path.join(RUNTIME_VARS.TMP, "hosts")
    config_defaults["aliases.file"] = os.path.join(RUNTIME_VARS.TMP, "aliases")
    config_defaults["transport"] = request.config.getoption("--transport")

    config_overrides = {
        "file_roots": {
            "base": [
                RUNTIME_VARS.TMP_STATE_TREE,
                os.path.join(RUNTIME_VARS.FILES, "file", "base"),
            ],
            # Alternate root to test __env__ choices
            "prod": [
                RUNTIME_VARS.TMP_PRODENV_STATE_TREE,
                os.path.join(RUNTIME_VARS.FILES, "file", "prod"),
            ],
        },
        "pillar_roots": {
            "base": [
                RUNTIME_VARS.TMP_PILLAR_TREE,
                os.path.join(RUNTIME_VARS.FILES, "pillar", "base"),
            ],
            "prod": [RUNTIME_VARS.TMP_PRODENV_PILLAR_TREE],
        },
    }
    virtualenv_binary = _get_virtualenv_binary_path()
    if virtualenv_binary:
        config_overrides["venv_bin"] = virtualenv_binary
    factory = salt_master_factory.get_salt_minion_daemon(
        salt_sub_minion_id,
        config_defaults=config_defaults,
        config_overrides=config_overrides,
    )
    return factory


@pytest.fixture(scope="package")
def salt_proxy_id():
    return "proxy-pytest"


@pytest.fixture(scope="package")
def salt_proxy_factory(request, salt_factories, salt_master_factory, salt_proxy_id):
    root_dir = salt_factories._get_root_dir_for_daemon(salt_proxy_id)
    conf_dir = root_dir / "conf"
    conf_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME_VARS.TMP_PROXY_CONF_DIR = str(conf_dir)

    with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, "proxy")) as rfh:
        config_defaults = yaml.deserialize(rfh.read())

    config_defaults["id"] = salt_proxy_id
    config_defaults["hosts.file"] = os.path.join(RUNTIME_VARS.TMP, "hosts")
    config_defaults["aliases.file"] = os.path.join(RUNTIME_VARS.TMP, "aliases")
    config_defaults["transport"] = request.config.getoption("--transport")
    config_defaults["root_dir"] = str(root_dir)

    def remove_stale_key(proxy_key_file):
        log.debug("Proxy minion %r KEY FILE: %s", salt_proxy_id, proxy_key_file)
        if os.path.exists(proxy_key_file):
            os.unlink(proxy_key_file)
        else:
            log.warning("The proxy minion key was not found at %s", proxy_key_file)

    factory = salt_master_factory.get_salt_proxy_minion_daemon(
        salt_proxy_id, config_defaults=config_defaults
    )
    proxy_key_file = os.path.join(
        salt_master_factory.config["pki_dir"], "minions", salt_proxy_id
    )
    factory.register_after_terminate_callback(remove_stale_key, proxy_key_file)
    return factory


@pytest.fixture(scope="package")
def salt_cli(salt_master_factory):
    return salt_master_factory.get_salt_cli()


@pytest.fixture(scope="package")
def salt_cp_cli(salt_master_factory):
    return salt_master_factory.get_salt_cp_cli()


@pytest.fixture(scope="package")
def salt_key_cli(salt_master_factory):
    return salt_master_factory.get_salt_key_cli()


@pytest.fixture(scope="package")
def salt_run_cli(salt_master_factory):
    return salt_master_factory.get_salt_run_cli()


@pytest.fixture(scope="package")
def salt_call_cli(salt_minion_factory):
    return salt_minion_factory.get_salt_call_cli()


@pytest.fixture(scope="package")
def bridge_pytest_and_runtests():
    yield
