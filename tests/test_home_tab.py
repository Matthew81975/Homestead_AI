import unittest

from hcs_ai.gui_home import HOME_TAB_TITLE, insert_home_tab


class FakeNotebook:
    def __init__(self):
        self.calls = []

    def insert(self, index, frame, **kwargs):
        self.calls.append((index, frame, kwargs))


class HomeTabTests(unittest.TestCase):
    def test_home_tab_is_inserted_first_with_expected_title(self):
        notebook = FakeNotebook()
        frame = object()

        insert_home_tab(notebook, frame)

        self.assertEqual(notebook.calls, [(0, frame, {"text": HOME_TAB_TITLE})])
        self.assertEqual(HOME_TAB_TITLE, "Home")


if __name__ == "__main__":
    unittest.main()
