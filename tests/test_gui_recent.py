import inspect
import unittest

from hcs_ai.gui import App as BaseApp
from hcs_ai.gui_recent import RECENT_TAB_TITLES, format_telemetry


class GuiRecentHelpers(unittest.TestCase):
    def test_telemetry_text_includes_both_rates(self):
        text = format_telemetry({
            "model": "m.gguf",
            "generation_tokens_per_second": 18.4,
            "prompt_tokens_per_second": 72.0,
        })
        self.assertIn("18.4 tok/s", text)
        self.assertIn("72.0 prompt tok/s", text)

    def test_tab_titles_are_models_and_prompt_functions(self):
        self.assertEqual(RECENT_TAB_TITLES, ("Models", "Prompt Functions"))

    def test_ai_console_is_persistent_not_a_notebook_tab(self):
        init_source = inspect.getsource(BaseApp.__init__)
        chat_source = inspect.getsource(BaseApp.build_chat)
        self.assertIn('self.workspace_pane = ttk.Panedwindow', init_source)
        self.assertIn('self.workspace_pane.add(self.chat_tab', init_source)
        self.assertNotIn('["AI","Knowledge Base"', init_source)
        self.assertIn('Alexandria — HCS AI', chat_source)
        self.assertIn('Collapse', chat_source)
        self.assertIn('Expand', chat_source)


if __name__ == "__main__":
    unittest.main()
