"""Offline tests (no Hermes imports): python3 -m unittest discover -s plugins/quiet-background/tests"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("quiet_background", os.path.join(HERE, "..", "__init__.py"))
qb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qb)


class T(unittest.TestCase):
    def test_internal_wake_gets_guidance(self):
        out = qb._on_pre_llm_call(user_message="[ASYNC DELEGATION] 3 tasks finished ...")
        self.assertEqual(out, {"context": qb.GUIDANCE})
        self.assertIn("[SILENT]", qb.GUIDANCE)

    def test_multimodal_parts(self):
        msg = [{"type": "text", "text": "[Background process 42 exited]"}, {"type": "image_url"}]
        self.assertIsNotNone(qb._on_pre_llm_call(user_message=msg))

    def test_normal_user_turn_untouched(self):
        self.assertIsNone(qb._on_pre_llm_call(user_message="what's the weather?"))
        self.assertIsNone(qb._on_pre_llm_call(user_message=None))

    def test_subagent_turn_untouched(self):
        self.assertIsNone(qb._on_pre_llm_call(user_message="[INTERNAL NOTIFICATION] x", parent_session_id="p1"))

    def test_register(self):
        calls = []

        class Ctx:
            def register_hook(self, name, fn):
                calls.append(name)
        qb.register(Ctx())
        self.assertEqual(calls, ["pre_llm_call"])


if __name__ == "__main__":
    unittest.main()
