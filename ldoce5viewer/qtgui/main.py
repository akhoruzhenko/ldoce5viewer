"""Main window"""

import sys
import webbrowser
from difflib import SequenceMatcher
from functools import partial
from operator import itemgetter

from PySide6 import QtGui, QtPrintSupport
from PySide6.QtPrintSupport import QPrintPreviewDialog, QPrintDialog, QPrinter
from PySide6.QtWebEngineCore import QWebEngineUrlScheme, QWebEngineFindTextResult
from PySide6.QtWidgets import *

try:
    import Cocoa
    import objc
except ImportError:
    objc = None

from PySide6.QtCore import *
from PySide6.QtGui import *
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

from .. import fulltext, incremental
from ..ldoce5.idmreader import is_ldoce5_dir
from ..utils.text import MATCH_CLOSE_TAG, MATCH_OPEN_TAG, ellipsis, normalize_index_key
from .access import MyUrlSchemeHandler, _load_static_data
from .advanced import AdvancedSearchDialog
from .asyncfts import AsyncFTSearcher
from .config import get_config
from .indexer import IndexerDialog
from .ui.custom import LineEdit, ToolButton
from .ui.main import Ui_MainWindow

# Config
_INDEX_SUPPORTED = "2013.02.25"
_FTS_HWDPHR_LIMIT = 10000
_INCREMENTAL_LIMIT = 500
_MAX_DELAY_UPDATE_INDEX = 100
_INTERVAL_AUTO_PRON = 500
_LOCAL_SCHEMES = frozenset(("dict", "static", "search", "audio", "lookup"))
_HELP_PAGE_URL = "https://forward-backward.co.jp/ldoce5viewer/manual/"

# Identifiers for lazy-loaded objects
_LAZY_INCREMENTAL = "incremental"
_LAZY_FTS_HWDPHR = "fts_hwdphr"
_LAZY_FTS_DEFEXA = "fts_defexa"
_LAZY_FTS_HWDPHR_ASYNC = "fts_hwdphr_async"
_LAZY_ADVSEARCH_WINDOW = "advsearch_window"
_LAZY_PRINTER = "printer"

_IS_OSX = sys.platform.startswith("darwin")


def _incr_delay_func(count):
    x = max(0.3, min(1, float(count) / _INCREMENTAL_LIMIT))
    return int(_MAX_DELAY_UPDATE_INDEX * x)


class MainWindow(QMainWindow):

    # ------------
    # MainWindow
    # ------------

    def __init__(self):
        super(MainWindow, self).__init__()

        self._okToClose = False
        # systray = QSystemTrayIcon(self)
        # systray.setIcon(QIcon(":/icons/icon.png"))
        # systray.show()
        # def systray_activated(reason):
        #    self.setVisible(self.isVisible() ^ True)
        # systray.activated.connect(systray_activated)

        # results
        self._incr_results = None
        self._fts_results = None
        self._found_items = None

        # status
        self._selection_pending = False
        self._loading_pending = False
        self._auto_fts_phrase = None

        # Lazy-loaded objects
        self._lazy = {}

        # Local URL scheme
        for name in _LOCAL_SCHEMES:
            scheme = QWebEngineUrlScheme(name.encode("ascii"))
            scheme.setFlags(
                QWebEngineUrlScheme.Flag.SecureScheme
                | QWebEngineUrlScheme.Flag.LocalScheme
                | QWebEngineUrlScheme.Flag.LocalAccessAllowed
                | QWebEngineUrlScheme.Flag.CorsEnabled
            )
            QWebEngineUrlScheme.registerScheme(scheme)

        self._scheme_handler = MyUrlSchemeHandler(self)
        profile = QWebEngineProfile.defaultProfile()
        for name in _LOCAL_SCHEMES:
            profile.installUrlSchemeHandler(name.encode("ascii"), self._scheme_handler)

        # Setup
        self._setup_ui()
        self._restore_from_config()

        # Timers
        def _makeSingleShotTimer(slot):
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(slot)
            return timer

        self._timerUpdateIndex = _makeSingleShotTimer(self._updateIndex)
        self._timerAutoFTS = _makeSingleShotTimer(self._onTimerAutoFullSearchTimeout)
        self._timerSpellCorrection = _makeSingleShotTimer(self._onTimerSpellCorrection)
        self._timerSearchingLabel = _makeSingleShotTimer(self._onTimerSearchingLabel)
        self._auto_pron_timer = _makeSingleShotTimer(self._on_timer_auto_pron_timeout)

        # Clipboard
        clipboard = QApplication.clipboard()
        clipboard.dataChanged.connect(
            partial(self._onClipboardChanged, mode=QClipboard.Mode.Clipboard)
        )
        clipboard.selectionChanged.connect(
            partial(self._onClipboardChanged, mode=QClipboard.Mode.Selection)
        )

        # Stylesheet for the item list pane
        try:
            self._ui.listWidgetIndex.setStyleSheet(
                _load_static_data("styles/list.css").decode("utf-8", "ignore")
            )
        except EnvironmentError:
            pass

        # Check index
        QTimer.singleShot(0, self._check_index)

        # Show
        self.show()

        # Click the dock icon (macOS)
        if objc:
            def applicationShouldHandleReopen_hasVisibleWindows_(s, a, f):
                self.show()

            objc.classAddMethods(
                Cocoa.NSApplication.sharedApplication().delegate().class__(),
                [applicationShouldHandleReopen_hasVisibleWindows_],
            )

    def close(self):
        self._okToClose = True
        super(MainWindow, self).close()

    def closeEvent(self, event):
        if not objc:
            self._okToClose = True

        lazy = self._lazy
        if self._okToClose:
            if _LAZY_ADVSEARCH_WINDOW in lazy:
                lazy[_LAZY_ADVSEARCH_WINDOW].close()
            self._save_to_configfile()
            self._unload_searchers()
            super(MainWindow, self).closeEvent(event)
        else:
            self.hide()
            event.ignore()

    def resizeEvent(self, event):
        ui = self._ui
        sp = self._ui.splitter
        width = event.size().width()
        if width < 350:
            sp.setOrientation(Qt.Orientation.Vertical)
            ui.actionSearchExamples.setText("E")
            ui.actionSearchDefinitions.setText("D")
            ui.actionAdvancedSearch.setText("A")
        elif width < 550:
            sp.setOrientation(Qt.Orientation.Vertical)
            ui.actionSearchExamples.setText("Exa")
            ui.actionSearchDefinitions.setText("Def")
            ui.actionAdvancedSearch.setText("Adv")
        elif width < 900:
            sp.setOrientation(Qt.Orientation.Horizontal)
            ui.actionSearchExamples.setText("Exa")
            ui.actionSearchDefinitions.setText("Def")
            ui.actionAdvancedSearch.setText("Advanced")
        else:
            sp.setOrientation(Qt.Orientation.Horizontal)
            ui.actionSearchExamples.setText("Examples")
            ui.actionSearchDefinitions.setText("Definitions")
            ui.actionAdvancedSearch.setText("Advanced")

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()
        ctrl = Qt.KeyboardModifier.MetaModifier if _IS_OSX else Qt.KeyboardModifier.ControlModifier
        le = self._ui.lineEditSearch

        if (
                key == Qt.Key.Key_Down
                or (key == Qt.Key.Key_J and modifiers == ctrl)
                or (key == Qt.Key.Key_Return and modifiers == Qt.KeyboardModifier.NoModifier)
        ):
            self.selectItemRelative(1)
        elif (
                key == Qt.Key.Key_Up
                or (key == Qt.Key.Key_K and modifiers == ctrl)
                or (key == Qt.Key.Key_Return and modifiers == Qt.KeyboardModifier.ShiftModifier)
        ):
            self.selectItemRelative(-1)
        elif key == Qt.Key.Key_Backspace:
            le.setFocus()
            le.setText(self._ui.lineEditSearch.text()[:-1])
        elif key in (
                Qt.Key.Key_Space,
                Qt.Key.Key_PageDown,
                Qt.Key.Key_PageUp,
                Qt.Key.Key_Home,
                Qt.Key.Key_End,
        ):
            self._ui.webView.setFocus()
            self._ui.webView.keyPressEvent(event)
        elif event.text().isalnum():
            le.setFocus()
            le.setText(event.text())
        else:
            super(MainWindow, self).keyPressEvent(event)

    def keyReleaseEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()
        mouse_buttons = QApplication.mouseButtons()

        ctrl = Qt.KeyboardModifier.MetaModifier if _IS_OSX else Qt.KeyboardModifier.ControlModifier

        if (not event.isAutoRepeat()) and mouse_buttons == Qt.MouseButton.NoButton:
            if (
                    key == Qt.Key.Key_Down
                    or (key == Qt.Key.Key_J and modifiers == ctrl)
                    or (key == Qt.Key.Key_Return and modifiers == Qt.KeyboardModifier.NoModifier)
            ):
                self._loadItem()
            elif (
                    key == Qt.Key.Key_Up
                    or (key == Qt.Key.Key_K and modifiers == ctrl)
                    or (key == Qt.Key.Key_Return and modifiers == Qt.KeyboardModifier.ShiftModifier)
            ):
                self._loadItem()

    def _updateTitle(self, title):
        title = title.strip()
        if title == "about:blank":
            title = None
        if title:
            self.setWindowTitle(
                "{title} - {appname}".format(
                    title=title, appname=QApplication.applicationName()
                )
            )
        else:
            self.setWindowTitle(QApplication.applicationName())

    def _onFocusLineEdit(self):
        self._ui.lineEditSearch.selectAll()
        self._ui.lineEditSearch.setFocus()

    # ---------
    # Index
    # ---------

    def _updateIndex(self):
        """Update the item list"""

        text_getter = itemgetter(0)
        path_getter = itemgetter(1)

        def _replace_htmltags(s):
            def opentag(m):
                return "".join(('<span class="', m.group(1), '">'))

            s = MATCH_CLOSE_TAG.sub("</span>", s)
            s = MATCH_OPEN_TAG.sub(opentag, s)
            return "".join(("<body>", s, "</body>"))

        lw = self._ui.listWidgetIndex

        incr_res = self._incr_results
        full_res = self._fts_results

        query = self._ui.lineEditSearch.text().strip()
        if (
                incr_res is not None
                and full_res is not None
                and len(incr_res) == 0
                and len(full_res) == 0
                and len(query.split()) == 1
        ):
            self._timerSpellCorrection.start(200)

        # Escape the previous selection
        row_prev = lw.currentRow()
        selected_prev = None
        if row_prev != -1:
            selected_prev = self._found_items[row_prev]

        # Update Index
        if incr_res and full_res:
            closed = set(map(path_getter, incr_res))
            self._found_items = incr_res + tuple(
                item for item in full_res if path_getter(item) not in closed
            )
        elif incr_res:
            self._found_items = tuple(incr_res)
        elif full_res:
            self._found_items = tuple(full_res)
        else:
            self._found_items = tuple()

        del incr_res
        del full_res

        # Create a new list
        items = tuple(
            _replace_htmltags(text_getter(item)) for item in self._found_items
        )
        lw.clear()
        lw.addItems(items)

        # Restore the previous selection
        if selected_prev:
            comparer = itemgetter(2, 3, 1)  # (sortkey, prio, path)
            current = comparer(selected_prev)
            for row in range(len(self._found_items)):
                if comparer(self._found_items[row]) == current:
                    lw.setCurrentRow(row)
                    break

        url = self._ui.webView.url().toString()
        sel_row = -1
        for (row, path) in enumerate(map(path_getter, self._found_items)):
            if "dict:" + path == url:
                sel_row = row
                break

        if sel_row >= 0:
            lw.setCurrentRow(sel_row)
            lw.scrollToItem(lw.item(sel_row), QAbstractItemView.ScrollHint.EnsureVisible)
        else:
            lw.scrollToTop()

        if self._selection_pending:
            self._selection_pending = False
            self.selectItemRelative()

        if self._loading_pending:
            self._loading_pending = False
            self._loadItem()

    def selectItemRelative(self, rel=0):
        if not self._found_items:
            self._selection_pending = True
            return

        if not self._found_items:
            return

        ui = self._ui
        lw = ui.listWidgetIndex
        row_prev = lw.currentRow()
        sortkey_getter = itemgetter(2)

        if row_prev == -1 or ui.lineEditSearch.hasFocus():
            # Find the prefix/exact match
            text = normalize_index_key(ui.lineEditSearch.text()).lower()
            sortkey_iter = map(sortkey_getter, self._found_items)
            for (row, sortkey) in enumerate(sortkey_iter):
                if sortkey.lower().startswith(text):
                    lw.setFocus()
                    lw.setCurrentRow(row)
                    return

            # find the most similar item
            row = -1
            sm = SequenceMatcher(a=text)
            max_ratio = 0
            sortkeys = map(sortkey_getter, self._found_items)
            for (r, sortkey) in enumerate(sortkeys):
                sm.set_seq2(sortkey)
                ratio = sm.quick_ratio()
                if ratio > max_ratio:
                    max_ratio = ratio
                    row = r
            lw.setFocus()
            lw.setCurrentRow(row)

        else:
            row = max(0, min(len(self._found_items) - 1, row_prev + rel))
            if row != row_prev:
                lw.setFocus()
                lw.setCurrentRow(row)

    def _loadItem(self, row=None):
        if not self._found_items:
            self._loading_pending = True
            return

        if row is None:
            row = self._ui.listWidgetIndex.currentRow()

        if 0 <= row < len(self._found_items):
            path = self._found_items[row][1]
            url = QUrl("dict://" + path)
            if url != self._ui.webView.url():
                self._ui.webView.page().load(url)

    def _onItemSelectionChanged(self):
        selitems = self._ui.listWidgetIndex.selectedItems()
        if selitems and QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            self._loadItem(self._ui.listWidgetIndex.row(selitems[0]))

    # ---------
    # Search
    # ---------

    def _instantSearch(self, pending=False, delay=True):
        query = self._ui.lineEditSearch.text()
        self._selection_pending = pending
        self._loading_pending = pending

        self._timerSearchingLabel.stop()
        self._ui.labelSearching.hide()

        if self._fts_hwdphr_async:
            self._fts_hwdphr_async.cancel()

        self._timerUpdateIndex.stop()
        self._timerAutoFTS.stop()
        self._timerSpellCorrection.stop()
        self._incr_results = None
        self._fts_results = None

        if query:
            contains_wild = any(c in query for c in "*?")

            if not contains_wild:
                results = self._incremental_search(query)
            else:
                results = []
            if results is not None:
                self._incr_results = tuple(results)
                self._auto_fts_phrase = query
                self._timerAutoFTS.start(0)
                self._timerUpdateIndex.start(
                    _incr_delay_func(len(results)) if delay else 0
                )
            else:
                self._ui.webView.setHtml(
                    """<p>The incremental search index"""
                    """ has not been created yet or broken.</p>"""
                )
                self._timerUpdateIndex.start(0)
        else:
            self._timerUpdateIndex.start(0)

    def _onTimerAutoFullSearchTimeout(self):
        query = self._auto_fts_phrase
        if self._fts_hwdphr_async:
            if any(c in query for c in "?*"):
                itemtypes = ("hm",)
            else:
                itemtypes = ()
            self._timerSearchingLabel.start(200)
            self._fts_hwdphr_async.update_query(
                query_str1=query,
                itemtypes=itemtypes,
                limit=_FTS_HWDPHR_LIMIT + 1,
                merge=True,
            )

    def _onTimerSpellCorrection(self):
        query = self._ui.lineEditSearch.text()
        if len(query.split()) == 1:
            words = self._fts_hwdphr.correct(query)
            cmpl = QCompleter(words, self)
            cmpl.setModelSorting(QCompleter.ModelSorting.UnsortedModel)
            cmpl.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
            self._ui.lineEditSearch.setCompleter(cmpl)
            cmpl.complete()

            def cmpl_activated(s):
                self._instantSearch()

            cmpl.activated.connect(cmpl_activated)

    def _incremental_search(self, key):
        if not self._incremental:
            return None
        else:
            try:
                return self._incremental.search(key, limit=_INCREMENTAL_LIMIT)
            except (EnvironmentError, incremental.IndexError):
                return None

    def _onAsyncFTSearchFinished(self):
        self._timerSearchingLabel.stop()
        self._ui.labelSearching.hide()
        r = self._fts_hwdphr_async.take_result()
        if r is None:
            return
        (merge, result) = r

        if not merge:
            self._incr_results = None
        self._fts_results = tuple(result)
        self._timerUpdateIndex.start(0)

    def _onAsyncFTSearchError(self):
        self._timerSearchingLabel.stop()
        self._ui.labelSearching.hide()
        self._ui.webView.setHtml(
            """<p>The full-text search index """
            """has not been created yet or broken.</p>"""
        )

    def _onTimerSearchingLabel(self):
        self._ui.labelSearching.show()

    # ------------
    # Search Box
    # ------------

    def _onTextChanged(self, text):
        text = text.strip()
        not_empty = bool(text)
        self._ui.actionSearchExamples.setEnabled(not_empty)
        self._ui.actionSearchDefinitions.setEnabled(not_empty)

    def _onTextEdited(self, text):
        self._ui.lineEditSearch.setCompleter(None)
        self._instantSearch()

    # ----------
    # WebView
    # ----------
    def _onWebViewWheelWithCtrl(self, delta):
        self.setZoom(delta / 120.0, relative=True)

    def setZoom(self, val, relative=False):
        config = get_config()
        zoom_power = val
        if relative:
            zoom_power += config.get("zoomPower", 0)
        config["zoomPower"] = max(-10, min(20, zoom_power))
        self._ui.webView.setZoomFactor(1.05 ** config["zoomPower"])

    def _onLoadFinished(self, succeeded):
        if succeeded:
            not_empty = bool(self._ui.lineEditSearch.text().strip())
            self._ui.actionSearchExamples.setEnabled(not_empty)
            self._ui.actionSearchDefinitions.setEnabled(not_empty)
            self._updateTitle(self._ui.webView.title())

    def _onUrlChanged(self, url):
        history = self._ui.webView.history()
        if history.currentItemIndex() == 1 and history.itemAt(0).url() == QUrl(
                "about:blank"
        ):
            history.clear()

        # Update history menu
        def update_navmenu(menu, items, curidx, back):
            def make_goto(idx):
                def f():
                    history = self._ui.webView.history()
                    if 0 <= idx < history.count():
                        history.goToItem(history.itemAt(idx))

                return f

            items = [(idx, item) for (idx, item) in enumerate(items)]
            if back:
                items = items[max(0, curidx - 20): curidx]
                items.reverse()
            else:
                items = items[curidx + 1: curidx + 1 + 20]
            urlset = set()
            menu.clear()
            for idx, hitem in items:
                title = hitem.title()
                if (not title) or hitem.url() in urlset:
                    continue
                urlset.add(hitem.url())
                title = ellipsis(title, 20)
                try:
                    menu.addAction(title, make_goto(idx))
                except:
                    pass
            menu.setEnabled(bool(menu.actions()))

        items = history.items()
        curidx = history.currentItemIndex()
        update_navmenu(self._ui.menuBackHistory, items, curidx, True)
        update_navmenu(self._ui.menuForwardHistory, items, curidx, False)

        # auto pronunciation playback
        if not history.canGoForward():
            self._auto_pron_playback()

        # FIXME: restore search phrase
        # hist_item = history.currentItem()
        # curr_query = self._ui.lineEditSearch.text()
        # hist_query = hist_item.userData()
        # if hist_query:
        #     if hist_query != curr_query:
        #         self._ui.lineEditSearch.setText(hist_query)
        #         self._instantSearch()
        #     else:
        #         self._timerUpdateIndex.start(0)
        # else:
        #     pass # FIXME: history.currentItem().setUserData(curr_query)

    # -----------------
    # Advanced Search
    # -----------------

    def fullSearch(self, phrase, filters, mode=None, only_web=False):
        self._selection_pending = False
        self._loading_pending = False
        self._ui.lineEditSearch.setText(phrase or "")

        if (not only_web) and self._fts_hwdphr_async:
            self._incr_results = tuple()
            self._fts_results = None
            self._timerSearchingLabel.start(200)
            self._ui.labelSearching.show()
            self._fts_hwdphr_async.update_query(
                query_str1=phrase,
                query_str2=filters,
                itemtypes=("hm",),
                limit=None,
                merge=False,
            )
            self._timerUpdateIndex.start(0)

        if self._fts_hwdphr and self._fts_defexa:
            q = QUrlQuery()
            if phrase:
                q.addQueryItem("phrase", phrase)
            if filters:
                q.addQueryItem("filters", filters)
            if mode:
                q.addQueryItem("mode", mode)
            url = QUrl("search:///?" + q.toString())
            self._ui.webView.page().load(url)

    def _onSearchExamples(self):
        query_str = self._ui.lineEditSearch.text().strip()
        self.fullSearch(query_str, None, mode="examples", only_web=True)
        self._ui.actionSearchExamples.setEnabled(False)

    def _onSearchDefinitions(self):
        query_str = self._ui.lineEditSearch.text().strip()
        self.fullSearch(query_str, None, mode="definitions", only_web=True)
        self._ui.actionSearchDefinitions.setEnabled(False)

    def _onAdvancedSearch(self):
        self._advsearch_window.show()
        self._advsearch_window.raise_()

    # ---------------
    # Search Phrase
    # ---------------

    def searchSelectedText(self):
        text = self._ui.webView.page().selectedText().strip()
        if len(text) > 100:
            text = "".join(text[:100].rsplit(None, 1)[:1])
        self._ui.lineEditSearch.setText(text)
        self._instantSearch(pending=True, delay=False)

    def _onMonitorClipboardChanged(self):
        get_config()["monitorClipboard"] = self._ui.actionMonitorClipboard.isChecked()

    def _onPaste(self):
        clipboard = QApplication.clipboard()
        text = clipboard.text(QClipboard.Clipboard)
        self._ui.lineEditSearch.setText(text)
        self._instantSearch(pending=True, delay=False)

    def _onClipboardChanged(self, mode):
        if self.isActiveWindow():
            return
        if not get_config().get("monitorClipboard", False):
            return

        clipboard = QApplication.clipboard()
        if mode == QClipboard.Mode.Selection:
            text = clipboard.text(QClipboard.Mode.Selection)
        elif mode == QClipboard.Mode.Clipboard:
            text = clipboard.text(QClipboard.Mode.Clipboard)
        # elif mode == QClipboard.FindBuffer:
        #    text = clipboard.text(QClipboard.FindBuffer)
        else:
            return

        text = " ".join(text[:100].splitlines()).strip()
        res = self._incremental_search(text)
        if res:
            self._ui.lineEditSearch.setText(text)
            self._instantSearch(pending=True, delay=False)

    # -------------
    # Nav Buttons
    # -------------

    def _onNavForward(self):
        self._ui.webView.page().triggerAction(QWebEnginePage.WebAction.Forward)

    def _onNavBack(self):
        self._ui.webView.page().triggerAction(QWebEnginePage.WebAction.Back)

    def _onNavActionChanged(self):
        webPage = self._ui.webView.page()
        ui = self._ui
        ui.toolButtonNavForward.setEnabled(
            webPage.action(QWebEnginePage.WebAction.Forward).isEnabled()
        )
        ui.toolButtonNavBack.setEnabled(webPage.action(QWebEnginePage.WebAction.Back).isEnabled())

    # -----------
    # Auto Pron
    # -----------

    def _auto_pron_playback(self):
        if self._auto_pron_timer.isActive():
            self._auto_pron_timer.stop()
        self._auto_pron_timer.setInterval(_INTERVAL_AUTO_PRON)
        self._auto_pron_timer.start()

    def _playback_audio(self, name):
        if name == "":
            return
        language = get_config().get("autoPronPlayback", None)
        path = f"/{language.lower()}_hwd_pron/{name}"
        self._scheme_handler.play_audio(path)

    def _on_timer_auto_pron_timeout(self):
        language = get_config().get("autoPronPlayback", None)
        if language in ("US", "GB"):
            js = f"""
            const meta = document.querySelector("meta[name='{language.lower()}_pron']");
            (meta) ? meta.getAttribute("content") : "";
            """
            self._ui.webView.page().runJavaScript(js, 0, self._playback_audio)

    def _on_auto_pron_changed(self, action):
        config = get_config()
        if action == self._ui.actionPronUS:
            config["autoPronPlayback"] = "US"
        elif action == self._ui.actionPronGB:
            config["autoPronPlayback"] = "GB"
        else:
            config["autoPronPlayback"] = ""

    # -----------
    # Find
    # -----------

    def setFindbarVisible(self, visible):
        ui = self._ui
        curr_visible = ui.frameFindbar.isVisible()
        ui.frameFindbar.setVisible(visible)

        if visible:
            ui.lineEditFind.setFocus()
            ui.lineEditFind.selectAll()
            text = ui.lineEditFind.text()
            if text:
                self.findText(text)
            else:
                ui.actionFindNext.setEnabled(False)
                ui.actionFindPrev.setEnabled(False)
        elif curr_visible:
            self.findText("")

    def _find_text_finished(self, result: QWebEngineFindTextResult):
        self._ui.actionFindNext.setEnabled(result.numberOfMatches() > 1)
        self._ui.actionFindPrev.setEnabled(result.numberOfMatches() > 1)
        if result.numberOfMatches() == 1:
            self._ui.labelFindResults.setText(f"{result.numberOfMatches()} match")
        elif result.numberOfMatches() == 0:
            self._ui.labelFindResults.setText("")
        else:
            self._ui.labelFindResults.setText(f"{result.activeMatch()} of {result.numberOfMatches()} matches")

    def findText(self, text):
        self._ui.webView.page().findText(text)

    def findNext(self):
        self._ui.webView.findText(
            self._ui.lineEditFind.text()
        )

    def findPrev(self):
        self._ui.webView.findText(
            self._ui.lineEditFind.text(),
            QWebEnginePage.FindFlag.FindBackward
        )

    # -------
    # Print
    # -------
    def print_preview(self):
        ui = self._ui

        # self._ui.webView.printToPdf("test.pdf")

        # printer = self._printer
        printer = QtPrintSupport.QPrinter(QtPrintSupport.QPrinter.HighResolution)
        printer.setDocName(ui.webView.title() or "")

        preview = QPrintPreviewDialog(printer)
        preview.paintRequested.connect(self.handle_paint_request)
        preview.exec()

    def handle_paint_request(self, printer):
        self._ui.webView.print(printer)

    def print(self):
        # printer = self._printer
        printer = QtPrintSupport.QPrinter(QtPrintSupport.QPrinter.HighResolution)
        printer.setDocName(self._ui.webView.title() or "")

        dialog = QPrintDialog(printer, self)
        if dialog.exec() == QDialog.Accepted:
            self._ui.webView.print(printer)

    # ------------
    # Debugging
    # ------------

    def setInspectorVisible(self, visible):
        ui = self._ui
        ui.webInspector.setVisible(visible)
        ui.inspectorContainer.setVisible(visible)

    # -------
    # Help
    # -------

    def _onHelp(self):
        webbrowser.open(_HELP_PAGE_URL)

    def _onAbout(self):
        self._ui.webView.page().load(QUrl("static:///documents/about.html"))

    # ----------
    # Indexer
    # ----------

    def _check_index(self):
        config = get_config()
        if "dataDir" in config:
            if config.get("versionIndexed", "") < _INDEX_SUPPORTED:
                # Index is obsolete
                msg = (
                    "The format of the index files has been changed.\n"
                    "Please recreate the index database."
                )
            elif not is_ldoce5_dir(config["dataDir"]):
                # dataDir has been dissapeared
                msg = (
                    "The 'ldoce5.data' folder is not found at '{0}'.\n"
                    "Please recreate the index database.".format(
                        config.get("dataDir", "")
                    )
                )
            else:
                return
        else:
            # not exist yet
            msg = (
                "This application has to construct an index database"
                " before you can use it.\n"
                "Create now?\n"
                "(It will take 3-10 minutes, "
                "depending on the speed of your machine)"
            )

        r = QMessageBox.question(
            self,
            "Welcome to the LDOCE5 Viewer",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if r == QMessageBox.StandardButton.Yes:
            self._show_indexer_dialog(autostart=True)
        else:
            self.close()

    def _show_indexer_dialog(self, autostart=False):
        """Show the Create Index dialog"""
        config = get_config()

        # Disable clipboard monitoring
        mc_enabled = config.get("monitorClipboard", False)
        config["monitorClipboard"] = False

        # Show the indexer dialog
        self._unload_searchers()
        dialog = IndexerDialog(self, autostart)
        if dialog.exec():
            config.save()
            text = "welcome"
            self._ui.lineEditSearch.setText(text)
            self._instantSearch(pending=True, delay=False)

        # Restore the value of monitorClipboard
        config["monitorClipboard"] = mc_enabled

    # -------
    # Setup
    # -------

    def _setup_ui(self):
        self._ui = ui = Ui_MainWindow()
        ui.setupUi(self)

        self._ui.labelSearching.hide()

        # Toolbar
        toolBar = ui.toolBar
        toolBar.toggleViewAction().setVisible(False)
        toolBar.setIconSize(QSize(24, 24))
        ui.actionNavBack = QAction(self)
        ui.actionNavBack.setToolTip("Go Back")
        ui.toolButtonNavBack = ToolButton()
        ui.toolButtonNavBack.setDefaultAction(ui.actionNavBack)
        ui.actionNavForward = QAction(self)
        ui.actionNavForward.setToolTip("Go Forward")
        ui.toolButtonNavForward = ToolButton()
        ui.toolButtonNavForward.setDefaultAction(ui.actionNavForward)
        ui.lineEditSearch = LineEdit(self)
        ui.lineEditSearch.setPlaceholderText("Search...")
        ui.lineEditSearch.setInputMethodHints(
            Qt.InputMethodHint.ImhUppercaseOnly | Qt.InputMethodHint.ImhLowercaseOnly | Qt.InputMethodHint.ImhDigitsOnly
        )
        toolBar.addWidget(ui.toolButtonNavBack)
        toolBar.addWidget(ui.toolButtonNavForward)
        toolBar.addWidget(ui.lineEditSearch)
        toolBar.addAction(ui.actionSearchDefinitions)
        toolBar.addAction(ui.actionSearchExamples)
        toolBar.addAction(ui.actionAdvancedSearch)

        # Icons
        def _set_icon(obj, name=None, var_suffix=""):
            if name:
                icon = QIcon.fromTheme(
                    name, QIcon(":/icons/" + name + var_suffix + ".png")
                )
                obj.setIcon(icon)
            else:
                obj.setIcon(QIcon())

        self.setWindowIcon(QIcon(":/icons/icon.png"))
        _set_icon(ui.actionFindClose, "window-close")
        _set_icon(ui.actionNavForward, "go-next", "24")
        _set_icon(ui.actionNavBack, "go-previous", "24")
        _set_icon(ui.actionFindNext, "go-down")
        _set_icon(ui.actionFindPrev, "go-up")
        _set_icon(ui.actionCloseInspector, "window-close")
        ui.actionSearchDefinitions.setIcon(QIcon())
        ui.actionSearchExamples.setIcon(QIcon())

        # FIXME(wontfix): QWebEngineSettings.setMaximumPagesInCache(32)
        # FIXME(wontfix): ui.webView.history().setMaximumItemCount(50)
        webpage = ui.webView.page()

        if not _IS_OSX:
            _set_icon(ui.actionCreateIndex, "document-properties")
            _set_icon(ui.actionFind, "edit-find")
            _set_icon(ui.actionQuit, "application-exit")
            _set_icon(ui.actionZoomIn, "zoom-in")
            _set_icon(ui.actionZoomOut, "zoom-out")
            _set_icon(ui.actionNormalSize, "zoom-original")
            _set_icon(ui.actionHelp, "help-contents")
            _set_icon(ui.actionAbout, "help-about")
            _set_icon(ui.actionPrint, "document-print")
            _set_icon(ui.actionPrintPreview, "document-print-preview")
            _set_icon(webpage.action(QWebEnginePage.WebAction.Forward), "go-next", "24")
            _set_icon(webpage.action(QWebEnginePage.WebAction.Back), "go-previous", "24")
            _set_icon(webpage.action(QWebEnginePage.WebAction.Reload), "reload")
            _set_icon(webpage.action(QWebEnginePage.WebAction.CopyImageToClipboard), "edit-copy")
            _set_icon(
                webpage.action(QWebEnginePage.WebAction.InspectElement), "document-properties"
            )
        else:
            ui.toolBar.setIconSize(QSize(16, 16))
            ui.actionNavForward.setIcon(QIcon(":/icons/go-next-mac.png"))
            ui.actionNavBack.setIcon(QIcon(":/icons/go-previous-mac.png"))
            _set_icon(webpage.action(QWebEnginePage.WebAction.Forward))
            _set_icon(webpage.action(QWebEnginePage.WebAction.Back))
            _set_icon(webpage.action(QWebEnginePage.WebAction.Reload))

        ui.frameFindbar.setStyleSheet(
            """#frameFindbar {
            border: 0px solid transparent;
            border-bottom: 1px solid palette(dark);
            background-color: qlineargradient(spread:pad,
            x1:0, y1:0, x2:0, y2:1,
            stop:0 palette(midlight), stop:1 palette(window));
            }"""
        )

        ui.labelSearching.setStyleSheet(
            """#labelSearching {
            background-color: qlineargradient(spread:pad,
            x1:0, y1:0, x2:0, y2:1,
            stop:0 palette(midlight), stop:1 palette(window));
            }"""
        )

        if _IS_OSX:
            self._ui.splitter.setStyleSheet(
                """
                #splitter::handle:horizontal {
                    border-right: 1px solid palette(dark);
                    width: 2px;
                }
                #splitter::handle:vertical {
                    border-bottom: 1px solid palette(dark);
                    height: 2px;
                }"""
            )
            # ui.toolButtonCloseFindbar.setStyleSheet(
            #        "QToolButton {border: none;}")
            # ui.toolButtonCloseInspector.setStyleSheet(
            #        "QToolButton {border: none;}")
            # ui.toolButtonFindNext.setStyleSheet("QToolButton {border: none;}")
            # ui.toolButtonFindPrev.setStyleSheet("QToolButton {border: none;}")

        # Nav Buttons
        ui.actionNavForward.triggered.connect(self._onNavForward)
        ui.actionNavBack.triggered.connect(self._onNavBack)
        webpage.action(QWebEnginePage.WebAction.Forward).changed.connect(self._onNavActionChanged)
        webpage.action(QWebEnginePage.WebAction.Back).changed.connect(self._onNavActionChanged)

        # ListView
        ui.listWidgetIndex.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)

        # WebView
        for web_act in (
                QWebEnginePage.WebAction.OpenLinkInNewWindow,
                QWebEnginePage.WebAction.DownloadLinkToDisk,
                QWebEnginePage.WebAction.DownloadImageToDisk,
                QWebEnginePage.WebAction.CopyLinkToClipboard,
                QWebEnginePage.WebAction.CopyImageToClipboard,
                # QWebEnginePage.OpenFrameInNewWindow,
                # QWebEnginePage.OpenImageInNewWindow,
        ):
            webpage.action(web_act).setEnabled(False)
            webpage.action(web_act).setVisible(False)

        if hasattr(QWebEnginePage, "CopyImageUrlToClipboard"):
            webpage.action(QWebEnginePage.CopyImageUrlToClipboard).setEnabled(False)
            webpage.action(QWebEnginePage.CopyImageUrlToClipboard).setVisible(False)

        ui.menuEdit.insertAction(ui.actionFind, ui.webView.actionCopyPlain)
        ui.menuEdit.insertSeparator(ui.actionFind)

        self.addAction(ui.webView.actionSearchText)
        ui.webView.actionSearchText.setShortcut(QKeySequence("Ctrl+E"))

        # Web Inspector (deprecated)
        # webpage.settings().setAttribute(QWebEngineSettings.DeveloperExtrasEnabled, True)
        # webpage.action(QWebEnginePage.InspectElement).setText('Inspect Element')
        # ui.webInspector.setPage(webpage)
        self.setInspectorVisible(False)

        # History Menu
        ui.menuBackHistory = QMenu(ui.toolButtonNavBack)
        ui.menuForwardHistory = QMenu(ui.toolButtonNavForward)
        ui.toolButtonNavBack.setMenu(ui.menuBackHistory)
        ui.toolButtonNavForward.setMenu(ui.menuForwardHistory)
        ui.menuBackHistory.setEnabled(False)
        ui.menuForwardHistory.setEnabled(False)

        # Signal -> Slot
        ui.lineEditSearch.textChanged.connect(self._onTextChanged)
        ui.lineEditSearch.textEdited.connect(self._onTextEdited)
        ui.lineEditFind.textChanged.connect(self.findText)
        ui.lineEditFind.returnPressed.connect(self.findNext)
        ui.lineEditFind.escapePressed.connect(
            partial(self.setFindbarVisible, visible=False)
        )
        ui.lineEditFind.shiftReturnPressed.connect(self.findPrev)
        ui.listWidgetIndex.itemSelectionChanged.connect(self._onItemSelectionChanged)
        # FIXME(wontfix): webpage.linkClicked.connect(self._onWebViewLinkClicked)
        ui.webView.loadStarted.connect(partial(self.setFindbarVisible, visible=False))
        ui.webView.wheelWithCtrl.connect(self._onWebViewWheelWithCtrl)
        ui.webView.urlChanged.connect(self._onUrlChanged)
        ui.webView.loadFinished.connect(self._onLoadFinished)
        ui.webView.page().findTextFinished.connect(self._find_text_finished)

        # ui.webView.page().printRequested.connect()
        # ui.webView.page().printFinished.connect()

        # Actions
        def act_conn(action, slot):
            action.triggered.connect(slot)

        act_conn(ui.actionAbout, self._onAbout)
        act_conn(ui.actionHelp, self._onHelp)
        act_conn(ui.actionCreateIndex, self._show_indexer_dialog)
        act_conn(ui.actionFindNext, self.findNext)
        act_conn(ui.actionFindPrev, self.findPrev)
        act_conn(ui.actionPrintPreview, self.print_preview)
        act_conn(ui.actionFocusLineEdit, self._onFocusLineEdit)
        act_conn(ui.actionPrint, self.print)
        act_conn(ui.actionSearchExamples, self._onSearchExamples)
        act_conn(ui.actionSearchDefinitions, self._onSearchDefinitions)
        act_conn(ui.actionAdvancedSearch, self._onAdvancedSearch)
        act_conn(ui.webView.actionSearchText, self.searchSelectedText)
        act_conn(ui.actionZoomIn, partial(self.setZoom, 1, relative=True))
        act_conn(ui.actionZoomOut, partial(self.setZoom, -1, relative=True))
        act_conn(ui.actionNormalSize, partial(self.setZoom, 0))
        act_conn(ui.actionMonitorClipboard, self._onMonitorClipboardChanged)
        act_conn(ui.actionFind, partial(self.setFindbarVisible, visible=True))
        act_conn(ui.actionFindClose, partial(self.setFindbarVisible, visible=False))
        act_conn(
            ui.actionCloseInspector, partial(self.setInspectorVisible, visible=False)
        )
        act_conn(
            webpage.action(QWebEnginePage.WebAction.InspectElement),
            partial(self.setInspectorVisible, visible=True),
        )

        ui.actionGroupAutoPron = QActionGroup(self)
        ui.actionGroupAutoPron.addAction(ui.actionPronOff)
        ui.actionGroupAutoPron.addAction(ui.actionPronGB)
        ui.actionGroupAutoPron.addAction(ui.actionPronUS)
        ui.actionGroupAutoPron.setExclusive(True)
        ui.actionGroupAutoPron.triggered.connect(self._on_auto_pron_changed)

        self.addAction(ui.actionFocusLineEdit)
        self.addAction(webpage.action(QWebEnginePage.WebAction.SelectAll))

        # Set an action to each ToolButton
        ui.toolButtonFindNext.setDefaultAction(ui.actionFindNext)
        ui.toolButtonFindPrev.setDefaultAction(ui.actionFindPrev)
        ui.toolButtonCloseFindbar.setDefaultAction(ui.actionFindClose)
        ui.toolButtonCloseInspector.setDefaultAction(ui.actionCloseInspector)

        actionPaste = QAction(self)
        actionPaste.triggered.connect(self._onPaste)
        actionPaste.setShortcut(QKeySequence("Ctrl+V"))
        self.addAction(actionPaste)

        # Shorcut keys
        ui.actionQuit.setShortcuts(QKeySequence.StandardKey.Quit)
        ui.actionHelp.setShortcuts(QKeySequence.StandardKey.HelpContents)
        ui.actionFind.setShortcuts(QKeySequence.StandardKey.Find)
        ui.actionFindNext.setShortcuts(QKeySequence.StandardKey.FindNext)
        ui.actionFindPrev.setShortcuts(QKeySequence.StandardKey.FindPrevious)
        ui.actionZoomIn.setShortcuts(QKeySequence.StandardKey.ZoomIn)
        ui.actionZoomOut.setShortcuts(QKeySequence.StandardKey.ZoomOut)
        ui.actionPrint.setShortcuts(QKeySequence.StandardKey.Print)
        ui.actionNormalSize.setShortcut(QKeySequence("Ctrl+0"))
        ui.actionFocusLineEdit.setShortcut(QKeySequence("Ctrl+L"))
        webpage.action(QWebEnginePage.WebAction.SelectAll).setShortcut(QKeySequence("Ctrl+A"))
        webpage.action(QWebEnginePage.WebAction.Back).setShortcuts(
            [
                k
                for k in QKeySequence.keyBindings(QKeySequence.StandardKey.Back)
                if not k.matches(QKeySequence("Backspace"))
            ]
        )
        webpage.action(QWebEnginePage.WebAction.Forward).setShortcuts(
            [
                k
                for k in QKeySequence.keyBindings(QKeySequence.StandardKey.Forward)
                if not k.matches(QKeySequence("Shift+Backspace"))
            ]
        )
        ui.actionNavBack.setShortcuts(
            [
                k
                for k in QKeySequence.keyBindings(QKeySequence.StandardKey.Back)
                if not k.matches(QKeySequence("Backspace"))
            ]
            + [QKeySequence("Ctrl+[")]
        )
        ui.actionNavForward.setShortcuts(
            [
                k
                for k in QKeySequence.keyBindings(QKeySequence.StandardKey.Forward)
                if not k.matches(QKeySequence("Shift+Backspace"))
            ]
            + [QKeySequence("Ctrl+]")]
        )

        # Reset
        self._updateTitle("")
        self._updateIndex()
        self.setFindbarVisible(False)
        self._onTextChanged(self._ui.lineEditSearch.text())
        self._onNavActionChanged()

    # ----------------
    # Configurations
    # ----------------

    def _restore_from_config(self):
        ui = self._ui
        config = get_config()
        try:
            if "windowGeometry" in config:
                self.restoreGeometry(config["windowGeometry"])
            if "splitterSizes" in config:
                ui.splitter.restoreState(config["splitterSizes"])
        except:
            pass

        try:
            pron = config.get("autoPronPlayback", None)
            acts = {"US": self._ui.actionPronUS, "GB": self._ui.actionPronGB}
            acts.get(pron, self._ui.actionPronOff).setChecked(True)
        except:
            pass

        try:
            ui.actionMonitorClipboard.setChecked(config.get("monitorClipboard", False))
        except:
            pass

        try:
            self.setZoom(0, relative=True)
        except:
            pass

    def _save_to_configfile(self):
        config = get_config()
        config["windowGeometry"] = bytes(self.saveGeometry())
        config["splitterSizes"] = bytes(self._ui.splitter.saveState())
        config.save()

    # -----------------
    # Resource Loader
    # -----------------

    def _updateNetworkAccessManager(self, fulltext_hp, fulltext_de):
        self._scheme_handler.update_searcher(fulltext_hp, fulltext_de)

    def _unload_searchers(self):
        self._updateNetworkAccessManager(None, None)

        obj = self._lazy.pop(_LAZY_FTS_HWDPHR_ASYNC, None)
        if obj:
            obj.shutdown()

        obj = self._lazy.pop(_LAZY_FTS_HWDPHR, None)
        if obj:
            obj.close()

        obj = self._lazy.pop(_LAZY_FTS_DEFEXA, None)
        if obj:
            obj.close()

        obj = self._lazy.pop(_LAZY_INCREMENTAL, None)
        if obj:
            obj.close()

    @property
    def _fts_hwdphr(self):
        obj = self._lazy.get(_LAZY_FTS_HWDPHR, None)
        if obj is None:
            config = get_config()
            try:
                obj = self._lazy[_LAZY_FTS_HWDPHR] = fulltext.Searcher(
                    config.fulltext_hwdphr_path, config.variations_path
                )
            except (EnvironmentError, fulltext.IndexError):
                pass
            self._updateNetworkAccessManager(
                self._lazy.get(_LAZY_FTS_HWDPHR, None),
                self._lazy.get(_LAZY_FTS_DEFEXA, None),
            )

        return obj

    @property
    def _fts_defexa(self):
        obj = self._lazy.get(_LAZY_FTS_DEFEXA, None)
        if obj is None:
            config = get_config()
            try:
                obj = self._lazy[_LAZY_FTS_DEFEXA] = fulltext.Searcher(
                    config.fulltext_defexa_path, config.variations_path
                )
            except (EnvironmentError, fulltext.IndexError):
                pass
            self._updateNetworkAccessManager(
                self._lazy.get(_LAZY_FTS_HWDPHR, None),
                self._lazy.get(_LAZY_FTS_DEFEXA, None),
            )

        return obj

    @property
    def _fts_hwdphr_async(self):
        obj = self._lazy.get(_LAZY_FTS_HWDPHR_ASYNC, None)
        if obj is None:
            searcher = self._fts_hwdphr
            if searcher:
                obj = self._lazy[_LAZY_FTS_HWDPHR_ASYNC] = AsyncFTSearcher(
                    self, searcher
                )
                obj.finished.connect(self._onAsyncFTSearchFinished)
                obj.error.connect(self._onAsyncFTSearchError)

        return obj

    @property
    def _incremental(self):
        obj = self._lazy.get(_LAZY_INCREMENTAL, None)
        if obj is None:
            try:
                obj = self._lazy[_LAZY_INCREMENTAL] = incremental.Searcher(
                    get_config().incremental_path
                )
            except (EnvironmentError, incremental.IndexError):
                pass

        return obj

    @property
    def _advsearch_window(self):
        obj = self._lazy.get(_LAZY_ADVSEARCH_WINDOW, None)
        if obj is None:
            obj = self._lazy[_LAZY_ADVSEARCH_WINDOW] = AdvancedSearchDialog(self)

        return obj

    @property
    def _printer(self):
        obj = self._lazy.get(_LAZY_PRINTER, None)
        if obj is None:
            obj = self._lazy[_LAZY_PRINTER] = QPrinter()

        return obj
