from . import desktop_host
from .gui_diagnostics import App

# Reuse the existing tray/update/process-management host, replacing only the GUI
# class it instantiates. Closing the visible GUI now means Exit HCS; minimizing
# to the tray remains available through the tray/startup workflow itself.
desktop_host.App = App
desktop_host.DesktopHost.hide_window = desktop_host.DesktopHost.exit


if __name__ == "__main__":
    desktop_host.main()
