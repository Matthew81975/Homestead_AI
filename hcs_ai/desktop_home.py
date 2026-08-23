from . import desktop_host
from .gui_recent import App

# Reuse the existing tray/update/process-management host unchanged, replacing
# only the GUI class it instantiates.
desktop_host.App = App


if __name__ == "__main__":
    desktop_host.main()
