#!/usr/bin/env python3
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2012 thomasv@gitorious
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import signal, sys, traceback, gc, os

try:
    import PyQt5
except Exception:
    sys.exit("Error: Could not import PyQt5 on Linux systems, you may try 'sudo apt-get install python3-pyqt5'")

from PyQt5.QtGui import *
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *

from electroncash.i18n import _, set_language
from electroncash.plugins import run_hook
from electroncash import WalletStorage
from electroncash.util import (UserCancelled, PrintError, print_error,
                               standardize_path, finalization_print_error,
                               get_new_wallet_name)
from electroncash import version

from .installwizard import InstallWizard, GoBack

from . import icons # This needs to be imported once app-wide then the :icons/ namespace becomes available for Qt icon filenames.
from .util import *   # * needed for plugins
from .main_window import ElectrumWindow
from .network_dialog import NetworkDialog
from .exception_window import Exception_Hook
from .update_checker import UpdateChecker


class ElectrumGui(QObject, PrintError):
    new_window_signal = pyqtSignal(str, object)

    instance = None

    def __init__(self, config, daemon, plugins):
        super(__class__, self).__init__() # QObject init
        assert __class__.instance is None, "ElectrumGui is a singleton, yet an instance appears to already exist! FIXME!"
        __class__.instance = self
        set_language(config.get('language'))
        # Uncomment this call to verify objects are being properly
        # GC-ed when windows are closed
        #if daemon.network:
        #    from electroncash.util import DebugMem
        #    from electroncash.wallet import Abstract_Wallet
        #    from electroncash.verifier import SPV
        #    from electroncash.synchronizer import Synchronizer
        #    daemon.network.add_jobs([DebugMem([Abstract_Wallet, SPV, Synchronizer,
        #                                       ElectrumWindow], interval=5)])
        QCoreApplication.setAttribute(Qt.AA_X11InitThreads)
        if hasattr(Qt, "AA_ShareOpenGLContexts"):
            QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        if sys.platform not in ('darwin',) and hasattr(Qt, "AA_EnableHighDpiScaling"):
            # The below only applies to non-macOS. On macOS this setting is
            # never used (because it is implicitly auto-negotiated by the OS
            # in a differernt way).
            #
            # qt_disable_highdpi will be set to None by default, or True if
            # specified on command-line.  The command-line override is intended
            # to supporess high-dpi mode just for this run for testing.
            #
            # The more permanent setting is qt_enable_highdpi which is the GUI
            # preferences option, so we don't enable highdpi if it's explicitly
            # set to False in the GUI.
            #
            # The default on Linux, Windows, etc is to enable high dpi
            disable_scaling = config.get('qt_disable_highdpi', False)
            enable_scaling = config.get('qt_enable_highdpi', True)
            if not disable_scaling and enable_scaling:
                QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
        if hasattr(Qt, "AA_UseHighDpiPixmaps"):
            QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
        if hasattr(QGuiApplication, 'setDesktopFileName'):
            QGuiApplication.setDesktopFileName('electron-cash.desktop')
        self.config = config
        self.daemon = daemon
        self.plugins = plugins
        self.windows = []
        self.app = QApplication(sys.argv)
        self._set_icon()
        self.app.installEventFilter(self)
        self.timer = QTimer(self); self.timer.setSingleShot(False); self.timer.setInterval(500) #msec
        self.gc_timer = QTimer(self); self.gc_timer.setSingleShot(True); self.gc_timer.timeout.connect(ElectrumGui.gc); self.gc_timer.setInterval(500) #msec
        self.nd = None
        # Dark Theme -- ideally set this before any widgets are created.
        self.set_dark_theme_if_needed()
        # /
        self.update_checker = UpdateChecker()
        self.update_checker_timer = QTimer(self); self.update_checker_timer.timeout.connect(self.on_auto_update_timeout); self.update_checker_timer.setSingleShot(False)
        self.update_checker.got_new_version.connect(lambda x: self.show_update_checker(parent=None, skip_check=True))
        # init tray
        self.dark_icon = self.config.get("dark_icon", False)
        self.tray = QSystemTrayIcon(self.tray_icon(), self)
        self.tray.setToolTip('Electron Cash')
        self.tray.activated.connect(self.tray_activated)
        self.build_tray_menu()
        self.tray.show()
        self.new_window_signal.connect(self.start_new_window)
        if self.has_auto_update_check():
            self._start_auto_update_timer(first_run = True)
        run_hook('init_qt', self)
        # We did this once already in the set_dark_theme call, but we do this
        # again here just in case some plugin modified the color scheme.
        ColorScheme.update_from_widget(QWidget())

    def __del__(self):
        stale = True
        if __class__.instance is self:
            stale = False
            __class__.instance = None
        print_error("[{}] finalized{}".format(__class__.__name__, ' (stale instance)' if stale else ''))
        if hasattr(super(), '__del__'):
            super().__del__()

    def is_dark_theme_available(self):
        try:
            import qdarkstyle
        except:
            return False
        return True

    def set_dark_theme_if_needed(self):
        use_dark_theme = self.config.get('qt_gui_color_theme', 'default') == 'dark'
        darkstyle_ver = None
        if use_dark_theme:
            try:
                import qdarkstyle
                self.app.setStyleSheet(qdarkstyle.load_stylesheet_pyqt5())
                try:
                    darkstyle_ver = version.normalize_version(qdarkstyle.__version__)
                except (ValueError, IndexError, TypeError, NameError, AttributeError) as e:
                    self.print_error("Warning: Could not determine qdarkstyle version:", repr(e))
            except BaseException as e:
                use_dark_theme = False
                self.print_error('Error setting dark theme: {}'.format(repr(e)))
        # Apply any necessary stylesheet patches. For now this only does anything
        # if the version is < 2.6.8.
        # 2.6.8+ seems to have fixed all the issues (for now!)
        from . import style_patcher
        style_patcher.patch(dark=use_dark_theme, darkstyle_ver=darkstyle_ver)
        # Even if we ourselves don't set the dark theme,
        # the OS/window manager/etc might set *a dark theme*.
        # Hence, try to choose colors accordingly:
        ColorScheme.update_from_widget(QWidget(), force_dark=use_dark_theme)

    def _set_icon(self):
        if sys.platform == 'darwin':
            # on macOS, in "running from source" mode, we want to set the app
            # icon, otherwise we get the generic Python icon.
            # In non-running-from-source mode, macOS will get the icon from
            # the .app bundle Info.plist spec (which ends up being
            # electron.icns anyway).
            icon = QIcon("electron.icns") if os.path.exists("electron.icns") else None
        else:
            # Unconditionally set this on all other platforms as it can only
            # help and never harm, and is always available.
            icon = QIcon(":icons/electron.ico")
        if icon:
            self.app.setWindowIcon(icon)

    def eventFilter(self, obj, event):
        ''' This event filter allows us to open bitcoincash: URIs on macOS '''
        if event.type() == QEvent.FileOpen:
            if len(self.windows) >= 1:
                self.windows[0].pay_to_URI(event.url().toString())
                return True
        return False

    def build_tray_menu(self):
        ''' Rebuild the tray menu by tearing it down and building it new again '''
        m_old = self.tray.contextMenu()
        if m_old is not None:
            # Tray does NOT take ownership of menu, so we are tasked with
            # deleting the old one. Note that we must delete the old one rather
            # than just clearing it because otherwise the old sub-menus stick
            # around in Qt. You can try calling qApp.topLevelWidgets() to
            # convince yourself of this.  Doing it this way actually cleans-up
            # the menus and they do not leak.
            m_old.clear()
            m_old.deleteLater()  # C++ object and its children will be deleted later when we return to the event loop
        m = QMenu()
        m.setObjectName("SysTray.QMenu")
        self.tray.setContextMenu(m)
        destroyed_print_error(m)
        for window in self.windows:
            submenu = m.addMenu(window.wallet.basename())
            submenu.addAction(_("Show/Hide"), window.show_or_hide)
            submenu.addAction(_("Close"), window.close)
        m.addAction(_("Dark/Light"), self.toggle_tray_icon)
        m.addSeparator()
        m.addAction(_("&Check for updates..."), lambda: self.show_update_checker(None))
        m.addSeparator()
        m.addAction(_("Exit Electron Cash"), self.close)
        self.tray.setContextMenu(m)

    def tray_icon(self):
        if self.dark_icon:
            return QIcon(':icons/electron_dark_icon.png')
        else:
            return QIcon(':icons/electron_light_icon.png')

    def toggle_tray_icon(self):
        self.dark_icon = not self.dark_icon
        self.config.set_key("dark_icon", self.dark_icon, True)
        self.tray.setIcon(self.tray_icon())

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            if all([w.is_hidden() for w in self.windows]):
                for w in self.windows:
                    w.bring_to_top()
            else:
                for w in self.windows:
                    w.hide()

    def close(self):
        for window in self.windows:
            window.close()

    def new_window(self, path, uri=None):
        # Use a signal as can be called from daemon thread
        self.new_window_signal.emit(path, uri)

    def show_network_dialog(self, parent):
        if self.warn_if_no_network(parent):
            return
        if self.nd:
            self.nd.on_update()
            self.nd.show()
            self.nd.raise_()
            return
        self.nd = NetworkDialog(self.daemon.network, self.config)
        self.nd.show()

    def create_window_for_wallet(self, wallet):
        w = ElectrumWindow(self, wallet)
        self.windows.append(w)
        finalization_print_error(w, "[{}] finalized".format(w.diagnostic_name()))
        self.build_tray_menu()
        # FIXME: Remove in favour of the load_wallet hook
        run_hook('on_new_window', w)
        return w

    def get_wallet_folder(self):
        ''' may raise FileNotFoundError '''
        return os.path.dirname(os.path.abspath(self.config.get_wallet_path()))

    def get_new_wallet_path(self):
        ''' may raise FileNotFoundError '''
        wallet_folder = self.get_wallet_folder()
        filename = get_new_wallet_name(wallet_folder)
        full_path = os.path.join(wallet_folder, filename)
        return full_path

    def start_new_window(self, path, uri):
        '''Raises the window for the wallet if it is open.  Otherwise
        opens the wallet and creates a new window for it.

        If path=None will raise whatever window is open or open last wallet if
        no windows are open.'''
        if not path and not self.windows:
            # This branch is taken if nothing is currently open but path=None,
            # in which case set path=last wallet
            self.config.open_last_wallet()
            path = self.config.get_wallet_path()

        path = path and standardize_path(path) # just make sure some plugin didn't give us a symlink
        for w in self.windows:
            if not path or w.wallet.storage.path == path:
                path = w.wallet.storage.path  # remember path in case it was None
                w.bring_to_top()
                break
        else:
            try:

                try:
                    wallet = self.daemon.load_wallet(path, None)
                    if wallet and self._slp_warn_if_wallet_not_compat(wallet):
                        # trigger exception catch which forces the wizard to kick in below
                        raise RuntimeWarning("User doesn't want to use this wallet")
                except BaseException as e:
                    self.print_error(repr(e))
                    if self.windows:
                        # *Not* starting up. Propagate exception out to present
                        # error message box to user.
                        raise e
                    # We're just starting up, so we are tolerant of bad wallets
                    # and just want to proceed to the InstallWizard so the user
                    # can either specify a different wallet or create a new one.
                    # (See issue #1189 where before they would get stuck)
                    path = self.get_new_wallet_path()  # give up on this unknown wallet and try a new name.. note if things get really bad this will raise FileNotFoundError and the app aborts here.
                    wallet = None  # fall thru to wizard
                if not wallet:
                    storage = WalletStorage(path, manual_upgrades=True)
                    wizard = InstallWizard(self.config, self.app, self.plugins, storage, 'New/Restore Wallet')
                    try:
                        wallet = wizard.run_and_get_wallet()
                    except UserCancelled:
                        pass
                    except GoBack as e:
                        self.print_error('[start_new_window] Exception caught (GoBack)', e)
                    finally:
                        wizard.terminate()
                        del wizard
                        gc.collect() # wizard sticks around in memory sometimes, otherwise :/
                    if not wallet:
                        return
                    wallet.start_threads(self.daemon.network)
                    self.daemon.add_wallet(wallet)
                if self._slp_warn_if_wallet_not_compat(wallet):
                    # basically, we reject hardware wallets
                    return # give up...
            except BaseException as e:
                traceback.print_exc(file=sys.stdout)
                if '2fa' in str(e):
                    self.warning(title=_('Error'), message = '2FA wallets for Bitcoin Cash are currently unsupported by <a href="https://api.trustedcoin.com/#/">TrustedCoin</a>. Follow <a href="https://github.com/Electron-Cash/Electron-Cash/issues/41#issuecomment-357468208">this guide</a> in order to recover your funds.')
                else:
                    self.warning(title=_('Error'), message = 'Cannot load wallet:\n' + str(e), icon=QMessageBox.Critical)
                return
            w = self.create_window_for_wallet(wallet)
        if uri:
            w.pay_to_URI(uri)
        w.bring_to_top()
        w.setWindowState(w.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)

        # this will activate the window
        w.activateWindow()
        return w

    def close_window(self, window):
        self.windows.remove(window)
        self.build_tray_menu()
        # save wallet path of last open window
        run_hook('on_close_window', window)
        # GC on ElectrumWindows takes forever to actually happen due to the
        # circular reference zoo they create around them (they end up stuck in
        # generation 2 for a long time before being collected). The below
        # schedules a more comprehensive GC to happen in the very near future.
        # This mechanism takes on the order of 40-100ms to execute (depending
        # on hardware) but frees megabytes of memory after closing a window
        # (which itslef is a relatively infrequent UI event, so it's
        # an acceptable tradeoff).
        self.gc_schedule()

        if not self.windows:
            self.config.save_last_wallet(window.wallet)
            # NB: we now unconditionally quit the app after the last wallet
            # window is closed, even if a network dialog or some other window is
            # open.  It was bizarre behavior to keep the app open when
            # things like a transaction dialog or the network dialog were still
            # up.
            __class__._quit_after_last_window()  # checks if qApp.quitOnLastWindowClosed() is True, and if so, calls qApp.quit()

        #window.deleteLater()  # <--- This has the potential to cause bugs (esp. with misbehaving plugins), so commented-out. The object gets deleted anyway when Python GC kicks in. Forcing a delete may risk python to have a dangling reference to a deleted C++ object.

    def gc_schedule(self):
        ''' Schedule garbage collection to happen in the near future.
        Note that rapid-fire calls to this re-start the timer each time, thus
        only the last call takes effect (it's rate-limited). '''
        self.gc_timer.start() # start/re-start the timer to fire exactly once in timeInterval() msecs

    @staticmethod
    def gc():
        ''' self.gc_timer timeout() slot '''
        gc.collect()

    def init_network(self):
        # Show network dialog if config does not exist
        if self.daemon.network:
            if self.config.get('auto_connect') is None:
                wizard = InstallWizard(self.config, self.app, self.plugins, None)
                wizard.init_network(self.daemon.network)
                wizard.terminate()

    def show_update_checker(self, parent, *, skip_check = False):
        if self.warn_if_no_network(parent):
            return
        self.update_checker.show()
        self.update_checker.raise_()
        if not skip_check:
            self.update_checker.do_check()

    def on_auto_update_timeout(self):
        if not self.daemon.network:
            # auto-update-checking never is done in offline mode
            self.print_error("Offline mode; update check skipped")
        elif not self.update_checker.did_check_recently():  # make sure auto-check doesn't happen right after a manual check.
            self.update_checker.do_check()
        if self.update_checker_timer.first_run:
            self._start_auto_update_timer(first_run = False)

    def _start_auto_update_timer(self, *, first_run = False):
        self.update_checker_timer.first_run = bool(first_run)
        if first_run:
            interval = 10.0*1e3 # do it very soon (in 10 seconds)
        else:
            interval = 3600.0*1e3 # once per hour (in ms)
        self.update_checker_timer.start(interval)
        self.print_error("Auto update check: interval set to {} seconds".format(interval//1e3))

    def _stop_auto_update_timer(self):
        self.update_checker_timer.stop()
        self.print_error("Auto update check: disabled")

    def warn_if_cant_import_qrreader(self, parent, show_warning=True):
        ''' Checks it QR reading from camera is possible.  It can fail on a
        system lacking QtMultimedia.  This can be removed in the future when
        we are unlikely to encounter Qt5 installations that are missing
        QtMultimedia '''
        try:
            from .qrreader import QrReaderCameraDialog
        except (ImportError, ModuleNotFoundError) as e:
            if show_warning:
                self.warning(parent=parent,
                             title=_("QR Reader Error"),
                             message=_("QR reader failed to load. This may "
                                       "happen if you are using an older version "
                                       "of PyQt5.<br><br>Detailed error: ") + str(e),
                             rich_text=True)
            return True
        return False

    def warn_if_no_network(self, parent):
        if not self.daemon.network:
            self.warning(message=_('You are using Electron Cash in offline mode; restart Electron Cash if you want to get connected'), title=_('Offline'), parent=parent)
            return True
        return False

    def warn_if_no_secp(self, parent=None, message=None, icon=QMessageBox.Warning, relaxed=False):
        ''' Returns True if it DID warn: ie if there's no secp and ecc operations
        are slow, otherwise returns False if we have secp.

        Pass message (rich text) to provide a custom message.

        Note that the URL link to the HOWTO will always be appended to the custom message.'''
        from electroncash import ecc_fast
        has_secp = ecc_fast.is_using_fast_ecc()
        if has_secp:
            return False

        # When relaxwarn is set return True without showing the warning
        from electroncash import get_config
        if relaxed and get_config().cmdline_options["relaxwarn"]:
            return True

        # else..
        howto_url='https://github.com/Electron-Cash/Electron-Cash/blob/master/contrib/secp_HOWTO.md#libsecp256k1-0-for-electron-cash'
        template = '''
        <html><body>
            <p>
            {message}
            <p>
            {url_blurb}
            </p>
            <p><a href="{url}">Electron Cash Secp Mini-HOWTO</a></p>
        </body></html>
        '''
        msg = template.format(
            message = message or _("Electron Cash was unable to find the secp256k1 library on this system. Elliptic curve cryptography operations will be performed in slow Python-only mode."),
            url=howto_url,
            url_blurb = _("Please visit this page for instructions on how to correct the situation:")
        )
        self.warning(parent=parent, title=_("Missing libsecp256k1"),
                     message=msg, rich_text=True)
        return True

    def warning(self, title, message, icon = QMessageBox.Warning, parent = None, rich_text=False):
        if not isinstance(icon, QMessageBox.Icon):
            icon = QMessageBox.Warning
        if isinstance(parent, MessageBoxMixin):
            parent.msg_box(title=title, text=message, icon=icon, parent=None, rich_text=rich_text)
        else:
            parent = parent if isinstance(parent, QWidget) else None
            d = QMessageBoxMixin(icon, title, message, QMessageBox.Ok, parent)
            if not rich_text:
                d.setTextFormat(Qt.PlainText)
                d.setTextInteractionFlags(Qt.TextSelectableByMouse)
            else:
                d.setTextFormat(Qt.AutoText)
                d.setTextInteractionFlags(Qt.TextSelectableByMouse|Qt.LinksAccessibleByMouse)
            d.setWindowModality(Qt.WindowModal if parent else Qt.ApplicationModal)
            d.exec_()
            d.setParent(None)

    def linux_maybe_show_highdpi_caveat_msg(self, parent):
        ''' Called from main_window.py -- tells user once and only once about
        the high DPI mode and its caveats on Linux only.  Is a no-op otherwise. '''
        if sys.platform not in ('linux',):
            return
        if (hasattr(Qt, "AA_EnableHighDpiScaling")
                and self.app.testAttribute(Qt.AA_EnableHighDpiScaling)
                # first run check:
                and self.config.get('qt_enable_highdpi', None) is None):
            self.config.set_key('qt_enable_highdpi', True)  # write to the config key to immediately suppress this warning in the future -- it only appears on first-run if None
            parent.show_message(
                title = _('High DPI Enabled'),
                msg = (_("Automatic high DPI scaling has been enabled for Electron Cash, which should result in improved graphics quality.")
                       + "\n\n" + _("However, on some esoteric Linux systems, this mode may cause disproportionately large status bar icons.")
                       + "\n\n" + _("If that is the case for you, then disable automatic DPI scaling in the preferences, under 'General'.")),
            )

    def has_auto_update_check(self):
        return bool(self.config.get('auto_update_check', True))

    def set_auto_update_check(self, b):
        was, b = self.has_auto_update_check(), bool(b)
        if was != b:
            self.config.set_key('auto_update_check', b, save=True)
            if b:
                self._start_auto_update_timer()
            else:
                self._stop_auto_update_timer()

    def _slp_warn_if_wallet_not_compat(self, wallet, *, stop=True):
        from electroncash.keystore import Hardware_KeyStore
        if any(isinstance(k, Hardware_KeyStore) for k in wallet.get_keystores()):
            if stop:
                try:
                    self.daemon.stop_wallet(wallet.storage.path)
                except:
                    # wasn't started
                    pass
            bn = wallet.basename()
            self.warning(title=_("Hardware Wallet"),
                         message = _("'{}' is a hardware wallet.").format(bn) + "\n\n" + _("Sorry, hardware wallets are not currently supported with this version of Electron Cash SLP. Please open a different wallet or create a new wallet."))
            return True
        return False

    @staticmethod
    def _quit_after_last_window():
        # on some platforms, not only does exec_ not return but not even
        # aboutToQuit is emitted (but following this, it should be emitted)
        if qApp.quitOnLastWindowClosed():
            qApp.quit()

    def main(self):
        try:
            self.init_network()
        except UserCancelled:
            return
        except GoBack:
            return
        except BaseException as e:
            traceback.print_exc(file=sys.stdout)
            return
        self.timer.start()
        self.config.open_last_wallet()
        path = self.config.get_wallet_path()
        if not self.start_new_window(path, self.config.get('url')):
            return
        signal.signal(signal.SIGINT, lambda *args: self.app.quit())

        self.app.setQuitOnLastWindowClosed(True)
        self.app.lastWindowClosed.connect(__class__._quit_after_last_window)

        def clean_up():
            # Just in case we get an exception as we exit, uninstall the Exception_Hook
            Exception_Hook.uninstall()
            # Shut down the timer cleanly
            self.timer.stop()
            self.gc_timer.stop()
            self._stop_auto_update_timer()
            # clipboard persistence. see http://www.mail-archive.com/pyqt@riverbankcomputing.com/msg17328.html
            event = QEvent(QEvent.Clipboard)
            self.app.sendEvent(self.app.clipboard(), event)
            self.tray.hide()
        self.app.aboutToQuit.connect(clean_up)

        Exception_Hook(self.config) # This wouldn't work anyway unless the app event loop is active, so we must install it once here and no earlier.
        # main loop
        self.app.exec_()
        # on some platforms the exec_ call may not return, so use clean_up()
