from __future__ import annotations

import unittest

from sn_proactive_agent.packaged_resources import (
    connector_resource,
    materialize_hermes_resources,
    packaged_resource_inventory,
)


class PackagedResourceTests(unittest.TestCase):
    def test_promised_hermes_resources_exist_in_source_checkout(self) -> None:
        for relative_path in packaged_resource_inventory():
            self.assertTrue(connector_resource(relative_path).is_file(), relative_path)

    def test_resource_paths_reject_parent_traversal(self) -> None:
        with self.assertRaises(ValueError):
            connector_resource("hermes/../acp/client.py")

    def test_classic_only_materialization_does_not_claim_tui_files(self) -> None:
        with materialize_hermes_resources(include_tui=False) as resources:
            self.assertTrue(resources.classic_hook.is_file())
            self.assertIsNone(resources.tui_module)
            self.assertIsNone(resources.tui_plugin)
            self.assertIsNone(resources.tui_patch)


if __name__ == "__main__":
    unittest.main()
