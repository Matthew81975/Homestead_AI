import argparse

from . import desktop_host
from .gui_diagnostics import App


class DesktopHost(desktop_host.DesktopHost):
    """Home launcher policy: closing its visible window fully exits HCS."""

    hide_window = desktop_host.DesktopHost.exit

    def run(self):
        original = desktop_host.App
        desktop_host.App = App
        try:
            return super().run()
        finally:
            desktop_host.App = original


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimized", action="store_true")
    args = parser.parse_args()
    DesktopHost(minimized=args.minimized).run()


if __name__ == "__main__":
    main()
