import unittest
from agents.constitution import (
    ConstitutionEngine,
    ObjectiveMismatchError,
    RuntimeStateViolationError,
    DeploymentNotVerifiedError,
    StalePatchTargetError,
    ConfirmationRequiredError,
    UnsupportedToolError,
)

class TestConstitutionEngine(unittest.TestCase):

    def setUp(self):
        self.engine = ConstitutionEngine()

    def test_objective_mismatch(self):
        with self.assertRaises(ObjectiveMismatchError):
            self.engine.check_objective("Deploy my app", "Launch a rocket")

    def test_fake_localhost_url(self):
        with self.assertRaises(RuntimeStateViolationError):
            self.engine.check_runtime_state("curl http://localhost:8080")

    def test_fake_https_url(self):
        # This check is not implemented yet in the constitution engine
        # but the test is here for when it is.
        pass

    def test_hardcoded_port(self):
        # As with the https url, the check is not implemented yet.
        pass

    def test_premature_success(self):
        with self.assertRaises(DeploymentNotVerifiedError):
            self.engine.check_success_contract({"process_running": True, "port_listening": True})

    def test_rm_without_confirmation(self):
        with self.assertRaises(ConfirmationRequiredError):
            self.engine.check_dangerous_commands("rm -rf /", confirmation=False)

    def test_kill_without_confirmation(self):
        with self.assertRaises(ConfirmationRequiredError):
            self.engine.check_dangerous_commands("kill -9 12345", confirmation=False)

    def test_stale_patch_target(self):
        with self.assertRaises(StalePatchTargetError):
            self.engine.check_patch_discipline("file content", "file content", "patch")

    def test_unsupported_tool(self):
        with self.assertRaises(UnsupportedToolError):
            self.engine.check_tool_discipline("new_tool", ["old_tool"])

if __name__ == '__main__':
    unittest.main()
