import logging
import logging.handlers
import sys
from PySide6.QtWidgets import QApplication
from mdm.main_window import MainWindow
from mdm.browser_integration import ipc_protocol
from mdm.browser_integration.service import BrowserIntegrationService


def _configure_browser_integration_log():
    """Rotating log for browser integration diagnostics (tokens/URLs are never logged)."""
    try:
        log_dir = ipc_protocol.default_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "browser_integration.log", maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger = logging.getLogger("mdm.browser_integration")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
    except OSError:
        pass


def main():
    _configure_browser_integration_log()
    app = QApplication(sys.argv)
    app.setApplicationName("My Download Manager")
    app.setOrganizationName("MyDownloadManager")
    app.setStyle("Fusion")

    window = MainWindow()

    # Stage 6A: Chrome / Edge integration through the native messaging host.
    # Requests are added via the window's existing QueueManager on the GUI thread.
    browser_integration = BrowserIntegrationService(
        window.queue_mgr,
        show_window=window.bring_to_front,
        parent=window,
    )
    window.attach_browser_integration(browser_integration)
    browser_integration.start()
    window.update_browser_status()
    # Safety net: endpoint file is removed even if the window is not closed normally.
    app.aboutToQuit.connect(browser_integration.stop)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
