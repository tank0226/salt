"""
    :codeauthor: Pedro Algarvio (pedro@algarvio.me)


    tests.unit.utils.cloud_test
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Test the salt-cloud utilities module
"""
import os
import shutil
import tempfile

import salt.utils.cloud as cloud
import salt.utils.platform

from tests.support.mixins import LoaderModuleMockMixin
from tests.support.mock import MagicMock, patch
from tests.support.runtests import RUNTIME_VARS
from tests.support.unit import SkipTest, TestCase, skipIf


class CloudUtilsTestCase(TestCase, LoaderModuleMockMixin):
    def setup_loader_modules(self):
        return {
            cloud: {"__opts__": {"sock_dir": "master"}},
        }

    @classmethod
    def setUpClass(cls):
        old_cwd = os.getcwd()
        cls.gpg_keydir = gpg_keydir = os.path.join(RUNTIME_VARS.TMP, "gpg-keydir")
        try:
            # The keyring library uses `getcwd()`, let's make sure we in a good directory
            # before importing keyring
            if not os.path.isdir(gpg_keydir):
                os.makedirs(gpg_keydir)
            os.chdir(gpg_keydir)

            # Late import because of the above reason
            import keyring
            import keyring.backend

            class CustomKeyring(keyring.backend.KeyringBackend):
                """
                A test keyring which always outputs same password
                """

                def __init__(self):
                    self.__storage = {}

                def supported(self):
                    return 0

                def set_password(
                    self, servicename, username, password
                ):  # pylint: disable=arguments-differ
                    self.__storage.setdefault(servicename, {}).update(
                        {username: password}
                    )
                    return 0

                def get_password(
                    self, servicename, username
                ):  # pylint: disable=arguments-differ
                    return self.__storage.setdefault(servicename, {}).get(
                        username, None
                    )

                def delete_password(
                    self, servicename, username
                ):  # pylint: disable=arguments-differ
                    self.__storage.setdefault(servicename, {}).pop(username, None)
                    return 0

            # set the keyring for keyring lib
            keyring.set_keyring(CustomKeyring())
        except ImportError:
            raise SkipTest('The "keyring" python module is not installed')
        finally:
            os.chdir(old_cwd)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.gpg_keydir):
            shutil.rmtree(cls.gpg_keydir)
        del cls.gpg_keydir

    def test_ssh_password_regex(self):
        """Test matching ssh password patterns"""
        for pattern in (
            "Password for root@127.0.0.1:",
            "root@127.0.0.1 Password:",
            " Password:",
        ):
            self.assertNotEqual(cloud.SSH_PASSWORD_PROMP_RE.match(pattern), None)
            self.assertNotEqual(
                cloud.SSH_PASSWORD_PROMP_RE.match(pattern.lower()), None
            )
            self.assertNotEqual(
                cloud.SSH_PASSWORD_PROMP_RE.match(pattern.strip()), None
            )
            self.assertNotEqual(
                cloud.SSH_PASSWORD_PROMP_RE.match(pattern.lower().strip()), None
            )

    def test__save_password_in_keyring(self):
        """
        Test storing password in the keyring
        """
        # Late import
        import keyring

        cloud._save_password_in_keyring(
            "salt.cloud.provider.test_case_provider",
            "fake_username",
            "fake_password_c8231",
        )
        stored_pw = keyring.get_password(
            "salt.cloud.provider.test_case_provider", "fake_username",
        )
        keyring.delete_password(
            "salt.cloud.provider.test_case_provider", "fake_username",
        )
        self.assertEqual(stored_pw, "fake_password_c8231")

    def test_retrieve_password_from_keyring(self):
        # Late import
        import keyring

        keyring.set_password(
            "salt.cloud.provider.test_case_provider",
            "fake_username",
            "fake_password_c8231",
        )
        pw_in_keyring = cloud.retrieve_password_from_keyring(
            "salt.cloud.provider.test_case_provider", "fake_username"
        )
        self.assertEqual(pw_in_keyring, "fake_password_c8231")

    def test_sftp_file_with_content_under_python3(self):
        with self.assertRaises(Exception) as context:
            cloud.sftp_file("/tmp/test", "ТЕСТ test content")
        # we successful pass the place with os.write(tmpfd, ...
        self.assertNotEqual(
            "a bytes-like object is required, not 'str'", str(context.exception),
        )

    @skipIf(salt.utils.platform.is_windows(), "Not applicable to Windows")
    def test_check_key_path_and_mode(self):
        with tempfile.NamedTemporaryFile() as f:
            key_file = f.name

            os.chmod(key_file, 0o644)
            self.assertFalse(cloud.check_key_path_and_mode("foo", key_file))
            os.chmod(key_file, 0o600)
            self.assertTrue(cloud.check_key_path_and_mode("foo", key_file))
            os.chmod(key_file, 0o400)
            self.assertTrue(cloud.check_key_path_and_mode("foo", key_file))

        # tmp file removed
        self.assertFalse(cloud.check_key_path_and_mode("foo", key_file))

    def test_deploy_windows_default_port(self):
        """
        Test deploy_windows with default port
        """
        mock_true = MagicMock(return_value=True)
        mock_tuple = MagicMock(return_value=(0, 0, 0))
        # fmt: off
        with patch("salt.utils.smb.get_conn", MagicMock()) as mock,\
                patch("salt.utils.smb.mkdirs", MagicMock()), \
                patch("salt.utils.smb.put_file", MagicMock()), \
                patch("salt.utils.smb.delete_file", MagicMock()), \
                patch("salt.utils.smb.delete_directory", MagicMock()), \
                patch("time.sleep", MagicMock()),\
                patch.object(cloud, "wait_for_port", mock_true), \
                patch.object(cloud, "fire_event", MagicMock()), \
                patch.object(cloud, "wait_for_psexecsvc", mock_true),\
                patch.object(cloud, "run_psexec_command", mock_tuple):

            cloud.deploy_windows(host="test", win_installer="")
            mock.assert_called_once_with("test", "Administrator", None, 445)
        # fmt: on

    def test_deploy_windows_custom_port(self):
        """
        Test deploy_windows with a custom port
        """
        mock_true = MagicMock(return_value=True)
        mock_tuple = MagicMock(return_value=(0, 0, 0))
        # fmt: off
        with patch("salt.utils.smb.get_conn", MagicMock()) as mock, \
                patch("salt.utils.smb.mkdirs", MagicMock()), \
                patch("salt.utils.smb.put_file", MagicMock()), \
                patch("salt.utils.smb.delete_file", MagicMock()), \
                patch("salt.utils.smb.delete_directory", MagicMock()), \
                patch("time.sleep", MagicMock()), \
                patch.object(cloud, "wait_for_port", mock_true), \
                patch.object(cloud, "fire_event", MagicMock()), \
                patch.object(cloud, "wait_for_psexecsvc", mock_true), \
                patch.object(cloud, "run_psexec_command", mock_tuple):

            cloud.deploy_windows(host="test", port=1234, win_installer="")
            mock.assert_called_once_with("test", "Administrator", None, 1234)
        # fmt: on
